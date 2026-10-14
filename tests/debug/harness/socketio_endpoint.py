"""The real debug endpoint: ADR-003 D1 Socket.IO namespace /api/debug-session (one event name
`msg`, D2 envelope), ADR-005 D1/D2 attach by `initialize` with the JWT, D4 heartbeat every P1, and
ADR-003 D9 status from GET /api/status.

The python-socketio client is imported only when a session opens, so a host without it reports
BLOCKED instead of failing to import. A port that does not answer is BLOCKED before that.

See also:
  tests/debug/harness/interfaces.py - the Endpoint/Session protocols implemented here.
  tests/debug/harness/runner.py - the only caller.
  webserver/debug_websocket.py - the legacy /api/debug namespace and its JWT checks.
"""

from __future__ import annotations

import json
import queue
import socket
import ssl
import threading
import urllib.error
import urllib.request
from typing import Dict, List, Optional, cast
from urllib.parse import urlparse

from interfaces import Blocked, Json, JsonObj

NAMESPACE = "/api/debug-session"
RESPONSE_TIMEOUT_S = 5.0


class SocketIOEndpoint:
    def __init__(self, url: str, token: Optional[str], insecure: bool) -> None:
        parsed = urlparse(url)
        self.url = url
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.token = token
        self.insecure = insecure
        self.description = f"endpoint {self.host}:{self.port}"

    def _probe(self) -> None:
        try:
            with socket.create_connection((self.host, self.port), timeout=2.0):
                pass
        except OSError as exc:
            raise Blocked(
                f"no debug endpoint at {self.host}:{self.port} ({exc.strerror or exc})"
            ) from exc

    def prepare(self, setup: JsonObj) -> None:
        self._probe()
        # Nothing deploys a build or rewrites task/halt configuration for a run yet; a scenario
        # that needs one cannot be judged against whatever happens to be loaded.
        needs = [
            k
            for k in (
                "build",
                "tasks",
                "params",
                "haltPolicy",
                "safeValues",
                "haltMode",
                "startHalted",
            )
            if k in setup
        ]
        if needs:
            raise Blocked(
                f"setup {', '.join(needs)} needs a deploy step the harness does not have yet"
            )

    def open_session(self) -> "SocketIOSession":
        self._probe()
        return SocketIOSession(self)

    def status(self) -> JsonObj:
        req = urllib.request.Request(f"https://{self.host}:{self.port}/api/status")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        ctx = ssl.create_default_context()
        if self.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=5.0, context=ctx) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise Blocked(f"GET /api/status failed: {exc}") from exc
        debug = doc.get("debug") if isinstance(doc, dict) else None
        if not isinstance(debug, dict):
            raise Blocked("GET /api/status has no `debug` block (ADR-003 D9 not built)")
        return cast(JsonObj, debug)

    def log(self) -> JsonObj:
        raise Blocked("no log read-back path is defined for the runtime yet")

    def inject(self, kind: str, args: JsonObj) -> Json:
        raise Blocked(f"inject {kind}: fault injection (F154) not built")

    def credential(self, kind: str) -> Json:
        if kind == "none":
            return None
        if kind == "expired":
            raise Blocked("an expired JWT needs the runtime's signing key, which the harness lacks")
        if not self.token:
            raise Blocked(f"credential {kind}: no --token given")
        if kind == "forged":
            # Same header and claims, signature replaced: verification must refuse it.
            head, _, _sig = self.token.rpartition(".")
            return f"{head}.Zm9yZ2Vk"
        return self.token


class SocketIOSession:
    def __init__(self, endpoint: SocketIOEndpoint) -> None:
        try:
            import socketio  # noqa: PLC0415 - optional at import time, see module doc
        except ImportError as exc:
            raise Blocked(
                "python-socketio client not installed on this host (no dependency added)"
            ) from exc
        self.endpoint = endpoint
        self.seq = 0
        self.lock = threading.Lock()
        self.pending: Dict[int, "queue.Queue[JsonObj]"] = {}
        self.inbox: "queue.Queue[JsonObj]" = queue.Queue()
        self.stop = threading.Event()
        self.beat: Optional[threading.Thread] = None
        self.sio = socketio.Client(ssl_verify=not endpoint.insecure, reconnection=False)
        self.sio.on("msg", self._on_msg, namespace=NAMESPACE)
        try:
            self.sio.connect(
                endpoint.url,
                namespaces=[NAMESPACE],
                transports=["websocket"],
                auth={"token": endpoint.token} if endpoint.token else None,
                wait_timeout=5,
            )
        except Exception as exc:  # noqa: BLE001 - socketio raises its own and transport errors
            raise Blocked(f"cannot attach to {NAMESPACE} at {endpoint.url}: {exc}") from exc

    def _on_msg(self, data: Json) -> None:
        msg = json.loads(data) if isinstance(data, str) else data
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "response":
            rseq = msg.get("request_seq")
            with self.lock:
                waiter = self.pending.get(rseq) if isinstance(rseq, int) else None
            if waiter is not None:
                waiter.put(msg)
        elif msg.get("type") == "event":
            self.inbox.put({"event": msg.get("event"), "body": msg.get("body")})

    def request(self, command: str, args: JsonObj) -> JsonObj:
        if command == "initialize" and self.endpoint.token and "token" not in args:
            args = {**args, "token": self.endpoint.token}
        with self.lock:
            self.seq += 1
            seq = self.seq
            waiter: "queue.Queue[JsonObj]" = queue.Queue()
            self.pending[seq] = waiter
        self.sio.emit(
            "msg",
            {"type": "request", "seq": seq, "command": command, "arguments": args},
            namespace=NAMESPACE,
        )
        try:
            resp = waiter.get(timeout=RESPONSE_TIMEOUT_S)
        except queue.Empty:
            resp = {
                "success": False,
                "error": {"code": "TIMEOUT", "message": f"no response in {RESPONSE_TIMEOUT_S:g} s"},
            }
        finally:
            with self.lock:
                self.pending.pop(seq, None)
        if command == "initialize" and resp.get("success") is True and self.beat is None:
            self._start_heartbeat(resp)
        return resp

    def _start_heartbeat(self, resp: JsonObj) -> None:
        # ADR-005 D4.3: an idle controller sends `heartbeat` every P1, or P2 expiry safe-stops a halt.
        body = resp.get("body")
        params = body.get("params") if isinstance(body, dict) else None
        p1 = params.get("P1") if isinstance(params, dict) else None
        period = float(p1) if isinstance(p1, (int, float)) and not isinstance(p1, bool) else 2.0

        def beat() -> None:
            while not self.stop.wait(period):
                with self.lock:
                    self.seq += 1
                    seq = self.seq
                try:
                    self.sio.emit(
                        "msg",
                        {"type": "request", "seq": seq, "command": "heartbeat", "arguments": {}},
                        namespace=NAMESPACE,
                    )
                except Exception:  # noqa: BLE001 - a lost transport shows up in the next request
                    return

        self.beat = threading.Thread(target=beat, name="debug-heartbeat", daemon=True)
        self.beat.start()

    def events(self) -> List[JsonObj]:
        out: List[JsonObj] = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        self.stop.set()
        self.sio.disconnect()

    def kill(self) -> None:
        # ASSUMED: python-engineio's abort drops the socket with no Socket.IO disconnect packet,
        # which is what the runtime sees of a SIGKILLed client.
        self.stop.set()
        self.sio.eio.disconnect(abort=True)
