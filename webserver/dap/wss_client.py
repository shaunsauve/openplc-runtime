"""A minimal Socket.IO v5 / Engine.IO v4 client over a real WSS connection, built on
simple-websocket (already installed with Flask-SocketIO; python-engineio depends on it).

It exists because python-socketio's sync client needs `websocket-client` for the websocket
transport, which is not installed (a pending user decision). It offers the slice of
`socketio.Client` that tests/debug/harness/socketio_endpoint.py uses (`on`, `connect`, `emit`,
`disconnect`, `eio.disconnect(abort=True)`), so the harness's own SocketIOSession runs over it.

It lives beside `webserver/dap/adapter.py` rather than under `tests/debug/wire/`, because the
adapter falls back to it and that fallback is not optional: the runtime image ships no
`websocket-client`, so on a real runtime this file is the only Socket.IO client there is. A file the
shipped product cannot start without is product. The harness imports this same copy rather than
keeping a second one — two Socket.IO clients could only ever drift.

See also:
  webserver/dap/adapter.py        - the shipped consumer; reads this by sibling path.
  tests/debug/harness/socketio_endpoint.py - the harness session this stands under.
  tests/debug/wire/run_harness.py - runs harness oracles through this client.
  webserver/debug_gateway/gateway.py - the server side of /api/debug-session.
"""

from __future__ import annotations

import json
import socket
import ssl
import threading
from typing import Callable, Dict, List, Optional, Union
from urllib.parse import urlparse

import simple_websocket

Json = Union[None, bool, int, float, str, List["Json"], Dict[str, "Json"]]
Handler = Callable[[Json], None]


class _DaemonThread(threading.Thread):
    """simple-websocket's reader thread, made daemon so an unclosed client never holds exit."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.daemon = True


class _Eio:
    def __init__(self, owner: "WssSio") -> None:
        self.owner = owner

    def disconnect(self, abort: bool = False) -> None:
        self.owner.close(abort=abort)


class WssSio:
    def __init__(self, ssl_verify: bool = True, reconnection: bool = False) -> None:
        del reconnection
        self.ssl_verify = ssl_verify
        self.handlers: Dict[str, Handler] = {}
        self.ws: Optional[simple_websocket.Client] = None
        self.connected = threading.Event()
        # Set when the reader stops: the transport is gone (the peer closed it, or it sent a
        # frame this client could not parse). A client that has not connected yet has not either.
        self.closed = threading.Event()
        self.refused: Optional[str] = None
        # Why the reader stopped, when it stopped because of the peer.
        self.close_reason: Optional[str] = None
        self.sent: List[str] = []  # outbound Socket.IO event packets, for passive-client checks
        self.lock = threading.Lock()
        self.eio = _Eio(self)
        self.namespace = "/"

    def on(self, event: str, handler: Handler, namespace: str = "/") -> None:
        self.namespace = namespace
        self.handlers[event] = handler

    def connect(
        self,
        url: str,
        namespaces: Optional[List[str]] = None,
        transports: Optional[List[str]] = None,
        auth: Optional[Dict[str, str]] = None,
        wait_timeout: float = 5.0,
    ) -> None:
        del transports
        parsed = urlparse(url)
        scheme = "wss" if parsed.scheme in ("https", "wss") else "ws"
        ctx = ssl.create_default_context()
        if not self.ssl_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.namespace = (namespaces or [self.namespace])[0]
        self.ws = simple_websocket.Client.connect(
            f"{scheme}://{parsed.netloc}/socket.io/?EIO=4&transport=websocket",
            ssl_context=ctx if scheme == "wss" else None,
            thread_class=_DaemonThread,
        )
        opened = self.ws.receive(timeout=wait_timeout)
        if not isinstance(opened, str) or not opened.startswith("0"):
            raise ConnectionError(f"no Engine.IO open packet: {opened!r}")
        self._raw(f"40{self.namespace}," + (json.dumps(auth) if auth else ""))
        threading.Thread(target=self._reader, name="wss-sio-reader", daemon=True).start()
        if not self.connected.wait(wait_timeout) or self.refused or self.ws is None:
            raise ConnectionError(self.refused or f"namespace {self.namespace} did not connect")

    def _raw(self, packet: str) -> None:
        with self.lock:
            if self.ws is None:
                raise ConnectionError("not connected")
            self.ws.send(packet)

    def _reader(self) -> None:
        prefix = f"4{{}}{self.namespace},"
        while self.ws is not None:
            try:
                pkt = self.ws.receive()
            except simple_websocket.ConnectionClosed as exc:
                self.close_reason = str(exc)
                break
            if not isinstance(pkt, str):
                continue
            if pkt == "2":
                self._raw("3")
            elif pkt.startswith(prefix.format("0")):
                self.connected.set()
            elif pkt.startswith(prefix.format("4")):
                self.refused = pkt
                self.connected.set()
            elif pkt.startswith(prefix.format("2")):
                name, *args = json.loads(pkt[len(prefix.format("2")):])
                handler = self.handlers.get(str(name))
                if handler is not None:
                    handler(args[0] if args else None)
        if not self.connected.is_set():
            self.refused = "connection closed before the namespace connected"
        self.connected.set()
        self.closed.set()

    def emit(self, event: str, data: Json, namespace: Optional[str] = None) -> None:
        packet = f"42{namespace or self.namespace}," + json.dumps([event, data])
        self.sent.append(packet)
        self._raw(packet)

    def disconnect(self) -> None:
        try:
            self._raw(f"41{self.namespace},")
        except (ConnectionError, simple_websocket.ConnectionClosed):
            pass
        self.close()

    def close(self, abort: bool = False) -> None:
        """`abort` drops the connection without a close frame, as a killed process does."""
        ws, self.ws = self.ws, None
        if ws is None:
            return
        # simple-websocket's Client.close() closes the socket while its reader thread is still in
        # recv(). The TLS object keeps the raw fd number, so once the next connection in this
        # process reuses that number, the old reader consumes the new connection's bytes (bad
        # record MAC / wrong version number on the second session). Shut the socket down first so
        # the reader returns, and join it, before the fd is released.
        if not abort:
            try:
                simple_websocket.ws.Base.close(ws)  # the close frame, as Client.close() sends it
            except simple_websocket.ConnectionClosed:
                pass
        try:
            ws.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        reader = getattr(ws, "thread", None)
        if reader is not None and reader is not threading.current_thread():
            reader.join(2.0)
        ws.sock.close()
        self.closed.set()
