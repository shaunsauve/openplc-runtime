"""A fake core agent: serves the internal core link (ADR-003 D1.3, `u32 BE length + UTF-8 JSON`
frames on a unix socket) and simulates one task, `Main`, walking a fixed probe sequence.

It stands in for core/src/plc_app/debug_agent.cpp until that lands. The framing is written here
independently of the gateway's, so a framing bug on either side shows up as a failed exchange.

See also:
  webserver/debug_gateway/core_link.py - the gateway side of the same link.
  docs/DEBUG_PROTOCOL.md - "Core link" lists every message this fake answers or pushes.
  tests/pytest/restapi/test_debug_gateway.py - the tests that drive the gateway against it.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

Json = Union[None, bool, int, float, str, List["Json"], Dict[str, "Json"]]
JsonObj = Dict[str, Json]

TASK = "Main"
FILE = "program.st"
# (line, col) of each probe of task Main, in execution order; probe ID = index.
PROBES: List[Tuple[int, int]] = [(3, 3), (4, 3), (6, 5), (7, 5), (9, 3)]
MD5 = "0123456789abcdef0123456789abcdef"
BUILD_ID = "b" * 64
COMPILER = "strucpp 0.9.0"
ENGINE_ABI = "1.0"

FULL_CAPS: JsonObj = {
    "probes": True,
    "maxBreakpoints": 8,
    "exceptionBreakpoints": True,
    "cycleStep": True,
    "runScans": True,
    "startHalted": True,
    "nonStop": True,
    "control": True,
    "modify": True,
    "trace": True,
    "liveWatch": True,
}


def debug_info(major: int = 1, minor: int = 0, build_id: str = BUILD_ID) -> JsonObj:
    """The ADR-002 D3 `debug-info.json` of the simulated program (only what the gateway reads)."""
    return {
        "format": "strucpp-debug-info",
        "major": major,
        "minor": minor,
        "build": {"md5": MD5, "buildId": build_id, "compiler": COMPILER, "probes": True},
        "files": [{"id": 0, "path": FILE, "sha256": "0" * 64, "library": None}],
        "probes": {
            "pou": [0] * len(PROBES),
            "file": [0] * len(PROBES),
            "line": [ln for ln, _ in PROBES],
            "col": [c for _, c in PROBES],
        },
        "lines": {"0": sorted({ln for ln, _ in PROBES})},
    }


def _send_frame(sock: socket.socket, msg: JsonObj) -> None:
    data = json.dumps(msg).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


@dataclass
class _Conn:
    sock: socket.socket
    lock: threading.Lock = field(default_factory=threading.Lock)
    seq: int = 0


class FakeCore:
    """`delays` holds per-command reply delays (seconds); requests are answered on their own
    threads, so a delayed reply overtakes nothing and is overtaken by later ones."""

    def __init__(
        self,
        path: str,
        capabilities: Optional[JsonObj] = None,
        delays: Optional[Dict[str, float]] = None,
        scan_period_s: float = 0.005,
    ) -> None:
        self.path = path
        self.capabilities = dict(FULL_CAPS if capabilities is None else capabilities)
        self.delays = dict(delays or {})
        self.scan_period_s = scan_period_s
        self.received: List[Tuple[str, JsonObj]] = []
        self.cv = threading.Condition()
        self.state = "RUNNING"  # RUNNING | HALTED | STOPPED | ERROR
        self.pc = 0
        self.scan = 0
        self.pending: Optional[str] = None  # reason to halt at the next probe reached
        self.cycle_target: Optional[int] = None
        self.scans_left: Optional[int] = None
        self.cursor: Optional[int] = None
        self.skip_once = False
        self.breakpoints: Dict[int, int] = {}  # probe -> breakpoint id
        self.next_bp = 1
        self.exception_filters: List[str] = []
        self.sessions: Dict[str, str] = {}
        self.next_session = 1
        self.conns: List[_Conn] = []
        self.closed = threading.Event()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if os.path.exists(path):
            os.unlink(path)
        self.server.bind(path)
        self.server.listen(4)
        threading.Thread(target=self._accept, name="fake-core-accept", daemon=True).start()
        threading.Thread(target=self._program, name="fake-core-task", daemon=True).start()

    # -- link

    def _accept(self) -> None:
        while not self.closed.is_set():
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            conn = _Conn(sock)
            self.conns.append(conn)
            self._send(conn, "hello", self.hello())
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def hello(self) -> JsonObj:
        return {
            "link": "1.0",
            "engineAbi": ENGINE_ABI,
            "capabilities": dict(self.capabilities),
            "image": {"md5": MD5, "buildId": BUILD_ID, "buildTime": "2026-10-14T00:00:00Z"},
            "params": {"P1": 2, "P2": 15},
        }

    def _read(self, conn: _Conn) -> None:
        while not self.closed.is_set():
            try:
                head = _recv_exact(conn.sock, 4)
                if head is None:
                    return
                body = _recv_exact(conn.sock, struct.unpack(">I", head)[0])
            except OSError:
                return
            if body is None:
                return
            msg = json.loads(body.decode("utf-8"))
            threading.Thread(target=self._answer, args=(conn, msg), daemon=True).start()

    def _send(self, conn: _Conn, event: str, body: JsonObj) -> None:
        with conn.lock:
            conn.seq += 1
            try:
                _send_frame(conn.sock, {"type": "event", "seq": conn.seq, "event": event, "body": body})
            except OSError:
                pass

    def push(self, event: str, body: JsonObj) -> None:
        for conn in list(self.conns):
            self._send(conn, event, body)

    def _answer(self, conn: _Conn, msg: JsonObj) -> None:
        command = str(msg.get("command"))
        args = msg.get("arguments")
        args = args if isinstance(args, dict) else {}
        self.received.append((command, args))
        time.sleep(self.delays.get(command, 0.0))
        try:
            body = self._handle(command, args)
            reply: JsonObj = {"success": True, "body": body}
        except _Refusal as r:
            reply = {"success": False, "error": {"code": r.code, "message": r.message}}
        with conn.lock:
            conn.seq += 1
            _send_frame(
                conn.sock,
                {"type": "response", "seq": conn.seq, "request_seq": msg.get("seq"), **reply},
            )

    def close(self) -> None:
        self.closed.set()
        with self.cv:
            self.cv.notify_all()
        self.server.close()
        for conn in self.conns:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.sock.close()

    # -- simulated task

    def witness(self) -> int:
        with self.cv:
            return self.scan

    def _program(self) -> None:
        tick = self.scan_period_s / len(PROBES)
        while not self.closed.is_set():
            with self.cv:
                while self.state != "RUNNING" and not self.closed.is_set():
                    self.cv.wait()
                reason = self._halt_reason()
                if reason is not None:
                    self._halt(reason)
                    continue
                self.skip_once = False
                self.pc += 1
                if self.pc == len(PROBES):
                    self.pc = 0
                    self.scan += 1
                    if self.scans_left is not None:
                        self.scans_left -= 1
            time.sleep(tick)

    def _halt_reason(self) -> Optional[str]:
        if self.skip_once:
            return None
        if self.pc in self.breakpoints:
            return "breakpoint"
        if self.pending in ("pause", "step", "entry"):
            return self.pending
        if self.cycle_target is not None and self.scan >= self.cycle_target and self.pc == 0:
            return "step"
        if self.scans_left is not None and self.scans_left <= 0 and self.pc == 0:
            return "step"
        if self.cursor is not None and self.pc == self.cursor:
            return "step"
        return None

    def _halt(self, reason: str, extra: Optional[JsonObj] = None) -> None:
        self.state = "HALTED"
        self.pending = None
        self.cycle_target = self.scans_left = self.cursor = None
        body: JsonObj = {"reason": reason, "task": TASK, "probe": self.pc, "scan": self.scan}
        if reason == "breakpoint":
            body["breakpointIds"] = [self.breakpoints[self.pc]]
        body.update(extra or {})
        self.push("stopped", body)
        self.push("runState", {"state": "HALTED", "tasks": [TASK], "since": time.time()})

    def _resume(self) -> None:
        self.state = "RUNNING"
        self.skip_once = True
        self.cv.notify_all()

    def inject_exception(self) -> None:
        with self.cv:
            if self.exception_filters and self.state == "RUNNING":
                self._halt("exception", {"exception": {"kind": "divByZero"}})
            else:
                self.state = "ERROR"
                self.push("runState", {"state": "ERROR", "tasks": [], "since": time.time()})

    def inject_print(self, text: str) -> None:
        with self.cv:
            scan = self.scan
        self.push("output", {"category": "print", "task": TASK, "scan": scan, "text": text})

    # -- requests

    def _handle(self, command: str, args: JsonObj) -> JsonObj:
        with self.cv:
            return self._handle_locked(command, args)

    def _need_halted(self) -> None:
        if self.state != "HALTED":
            raise _Refusal("NOT_HALTED", f"The task is {self.state.lower()}, not halted.")

    def _handle_locked(self, command: str, args: JsonObj) -> JsonObj:
        if command == "attach":
            sid = f"s{self.next_session}"
            self.next_session += 1
            self.sessions[sid] = str(args.get("role"))
            return {"session": sid}
        if command in ("detach", "terminate"):
            role = self.sessions.pop(str(args.get("session")), None)
            if role == "controller":
                self.breakpoints.clear()
                if command == "terminate":
                    self.state = "STOPPED"
                    self.push("runState", {"state": "STOPPED", "tasks": [], "since": time.time()})
                elif self.state == "HALTED":
                    self._resume()
                    self.push("continued", {"task": TASK})
            return {"forces": []}
        if command == "beatClient":
            return {}
        if command == "status":
            return {"state": self.state, "tasks": [{"name": TASK, "scan": self.scan}]}
        if command == "pause":
            if self.state in ("STOPPED", "ERROR"):
                raise _Refusal("NOT_RUNNING", "The program is not running.")
            self.pending = "pause"
            return {}
        if command == "continue":
            self._need_halted()
            self._resume()
            self.push("continued", {"task": TASK})
            self.push("runState", {"state": "RUNNING", "tasks": [TASK], "since": time.time()})
            return {}
        if command in ("next", "stepIn", "stepOut"):
            self._need_halted()
            self.pending = "step"
            self._resume()
            return {}
        if command == "cycleStep":
            self._need_halted()
            self.cycle_target = self.scan + 1
            self._resume()
            return {}
        if command == "runScans":
            self._need_halted()
            self.scans_left = int(_num(args.get("n")))
            self._resume()
            return {}
        if command == "runToCursor":
            self._need_halted()
            self.cursor = int(_num(args.get("probe")))
            self._resume()
            return {}
        if command == "stop":
            if self.state == "STOPPED":
                raise _Refusal("NOT_RUNNING", "The program is already stopped.")
            self.state = "STOPPED"
            self.push("runState", {"state": "STOPPED", "tasks": [], "since": time.time()})
            return {}
        if command == "restart":
            self.pc = 0
            if args.get("kind") == "cold":
                self.scan = 0
            self.pending = "entry" if args.get("startHalted") is True else None
            self.skip_once = False
            self.state = "RUNNING"
            self.cv.notify_all()
            self.push("runState", {"state": "RUNNING", "tasks": [TASK], "since": time.time()})
            return {}
        if command == "setHaltMode":
            return {"mode": args.get("mode")}
        if command == "setBreakpoints":
            probes = [int(_num(p)) for p in _list(args.get("probes"))]
            limit = int(_num(self.capabilities.get("maxBreakpoints")))
            if len(probes) > limit:
                raise _Refusal("LIMIT_EXCEEDED", f"At most {limit} breakpoints are supported.")
            self.breakpoints = {}
            out: List[Json] = []
            for p in probes:
                self.breakpoints[p] = self.next_bp
                out.append({"id": self.next_bp, "probe": p, "verified": True})
                self.next_bp += 1
            return {"breakpoints": out}
        if command == "clearAllBreakpoints":
            removed = len(self.breakpoints)
            self.breakpoints.clear()
            return {"removed": removed}
        if command == "setExceptionBreakpoints":
            self.exception_filters = [str(f) for f in _list(args.get("filters"))]
            return {"filters": list(self.exception_filters)}
        raise _Refusal("UNSUPPORTED", f"The core does not implement {command}.")


class _Refusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _num(v: Json) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _list(v: Json) -> List[Json]:
    return v if isinstance(v, list) else []
