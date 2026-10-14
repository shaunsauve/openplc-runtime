# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""The internal core link (ADR-003 D1.3): `u32 BE length + UTF-8 JSON` frames, full duplex, on
`/run/runtime/plc_debug.socket`. Requests are correlated by `seq`/`request_seq`; the core pushes
events unsolicited, starting with `hello` on every connect.

See also:
  webserver/debug_gateway/gateway.py - the only consumer of CoreLink.
  tests/debug/wire/fake_core.py - the core agent stand-in serving this link in tests.
  docs/DEBUG_PROTOCOL.md - "Core link" lists the messages each side sends.
"""

from __future__ import annotations

import json
import queue
import socket
import struct
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Protocol

from webserver.debug_gateway.protocol import Json, JsonObj
from webserver.logger import get_logger

logger, _ = get_logger("debug_gw", use_buffer=True)

LINK_MAJOR = 1
MAX_FRAME_BYTES = 16 * 1024 * 1024
RECONNECT_S = 0.5

EventSink = Callable[[str, JsonObj], None]


@dataclass(frozen=True)
class CoreReply:
    success: bool
    body: JsonObj = field(default_factory=dict)
    code: Optional[str] = None
    message: str = ""
    details: JsonObj = field(default_factory=dict)


class CoreUnavailable(Exception):
    pass


class CoreLink(Protocol):
    def set_event_sink(self, sink: EventSink) -> None:
        """Receive every core event except `hello`, which the link keeps (see hello())."""

    def hello(self) -> Optional[JsonObj]:
        """The current connection's `hello` body, or None while not connected."""

    def request(self, command: str, arguments: JsonObj, timeout_s: float = 2.0) -> CoreReply:
        """Send one request and wait for its response; CoreUnavailable if there is none."""

    def close(self) -> None: ...


def _obj(v: Json) -> JsonObj:
    return v if isinstance(v, dict) else {}


def encode_frame(msg: JsonObj) -> bytes:
    data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(data)) + data


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_frame(sock: socket.socket) -> Optional[JsonObj]:
    head = _recv_exact(sock, 4)
    if head is None:
        return None
    length = struct.unpack(">I", head)[0]
    if length > MAX_FRAME_BYTES:
        raise ValueError(f"core frame of {length} bytes exceeds {MAX_FRAME_BYTES}")
    body = _recv_exact(sock, length)
    if body is None:
        return None
    msg = json.loads(body.decode("utf-8"))
    if not isinstance(msg, dict):
        raise ValueError("core frame is not a JSON object")
    return msg


class UnixCoreLink:
    """Connects (and reconnects) to the core's debug socket on a background thread."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._sink: EventSink = lambda _event, _body: None
        self._sock: Optional[socket.socket] = None
        self._hello: Optional[JsonObj] = None
        self._ready = threading.Event()
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: Dict[int, "queue.Queue[JsonObj]"] = {}
        self._seq = 0
        self._closed = threading.Event()
        threading.Thread(target=self._run, name="debug-core-link", daemon=True).start()

    def set_event_sink(self, sink: EventSink) -> None:
        self._sink = sink

    def hello(self) -> Optional[JsonObj]:
        return self._hello

    def wait_ready(self, timeout_s: float) -> bool:
        return self._ready.wait(timeout_s)

    def _run(self) -> None:
        while not self._closed.is_set():
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.path)
            except OSError:
                sock.close()
                self._closed.wait(RECONNECT_S)
                continue
            self._sock = sock
            try:
                self._read(sock)
            except (OSError, ValueError) as exc:
                logger.warning("Debug core link dropped: %s", exc)
            finally:
                self._drop(sock)

    def _read(self, sock: socket.socket) -> None:
        while not self._closed.is_set():
            msg = read_frame(sock)
            if msg is None:
                return
            kind = msg.get("type")
            if kind == "response":
                rseq = msg.get("request_seq")
                with self._state_lock:
                    waiter = self._pending.get(rseq) if isinstance(rseq, int) else None
                if waiter is None:
                    logger.warning("Debug core link: response to unknown request %s", rseq)
                else:
                    waiter.put(msg)
            elif kind == "event":
                self._event(str(msg.get("event")), _obj(msg.get("body")))

    def _event(self, event: str, body: JsonObj) -> None:
        if event == "hello":
            link = str(body.get("link", ""))
            if not link.startswith(f"{LINK_MAJOR}."):
                raise ValueError(f"core link version {link!r} is not {LINK_MAJOR}.x")
            self._hello = body
            self._ready.set()
        self._sink(event, body)

    def _drop(self, sock: socket.socket) -> None:
        self._hello = None
        self._ready.clear()
        self._sock = None
        sock.close()
        with self._state_lock:
            waiters = list(self._pending.values())
        for waiter in waiters:
            waiter.put({"success": False, "error": {"code": "_LINK_LOST"}})
        self._sink("_linkLost", {})

    def request(self, command: str, arguments: JsonObj, timeout_s: float = 2.0) -> CoreReply:
        sock = self._sock
        if sock is None or self._hello is None:
            raise CoreUnavailable("the runtime core's debug link is not connected")
        waiter: "queue.Queue[JsonObj]" = queue.Queue(maxsize=1)
        with self._state_lock:
            self._seq += 1
            seq = self._seq
            self._pending[seq] = waiter
        try:
            frame = encode_frame(
                {"type": "request", "seq": seq, "command": command, "arguments": arguments}
            )
            with self._send_lock:
                sock.sendall(frame)
            try:
                msg = waiter.get(timeout=timeout_s)
            except queue.Empty as exc:
                raise CoreUnavailable(f"the runtime core did not answer {command}") from exc
        except OSError as exc:
            raise CoreUnavailable(f"the runtime core's debug link failed: {exc}") from exc
        finally:
            with self._state_lock:
                self._pending.pop(seq, None)
        err = _obj(msg.get("error"))
        if err.get("code") == "_LINK_LOST":
            raise CoreUnavailable("the runtime core's debug link dropped")
        if msg.get("success") is True:
            return CoreReply(True, _obj(msg.get("body")))
        return CoreReply(
            False,
            _obj(msg.get("body")),
            str(err.get("code", "")),
            str(err.get("message", "")),
            _obj(err.get("details")),
        )

    def close(self) -> None:
        self._closed.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
