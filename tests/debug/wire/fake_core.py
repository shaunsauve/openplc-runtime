"""A fake core agent: serves the internal core link (ADR-003 D1.3, `u32 BE length + UTF-8 JSON`
frames on a unix socket) and simulates one task, `Main`, walking a fixed probe sequence.

It stands in for core/src/plc_app/debug_link.cpp and speaks the same link: docs/DEBUG_CORE_LINK.md.
Every request it receives is validated against that file's linkspec block (an argument outside
the spec is refused BAD_REQUEST, as the real core refuses it), so any gateway test that sends a
non-conforming request fails; test_the_fake_core_speaks_the_link_spec checks what it sends back.
It serves more of the link than the real core does (exceptions, cycleStep, runScans,
runToCursor, restart, setHaltMode), as a stand-in for capabilities still to be built. The framing
is written here independently of the gateway's, so a framing bug on either side shows up as a
failed exchange.

See also:
  webserver/debug_gateway/core_link.py - the gateway side of the same link.
  docs/DEBUG_CORE_LINK.md - the link spec; tests/debug/wire/link_spec.py - its checker.
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

import link_spec

Json = Union[None, bool, int, float, str, List["Json"], Dict[str, "Json"]]
JsonObj = Dict[str, Json]

TASK = "Main"
TASK_ID = 0
SPEC = link_spec.load()
FILE = "program.st"
# (line, col) of each probe of task Main, in execution order; probe ID = index.
PROBES: List[Tuple[int, int]] = [(3, 3), (4, 3), (6, 5), (7, 5), (9, 3)]
MD5 = "0123456789abcdef0123456789abcdef"
BUILD_ID = "b" * 64
COMPILER = "strucpp 0.9.0"
ENGINE_ABI = 1

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
        self.state = "RUNNING"  # RUNNING | HALTED | STOPPED | ERROR | SAFE_STOPPED
        self.pc = 0
        self.scan = 0
        self.pending: Optional[str] = None  # reason to halt at the next probe reached
        self.cycle_target: Optional[int] = None
        self.scans_left: Optional[int] = None
        self.cursor: Optional[int] = None
        self.skip_once = False
        self.breakpoints: Dict[int, int] = {}  # probe -> breakpoint id
        self.bp_file: Dict[int, int] = {}  # probe -> the fileId whose setBreakpoints armed it
        self.halt_reason: Optional[str] = None
        self.next_bp = 1
        self.exception_filters: List[str] = []
        self.sessions: Dict[str, str] = {}  # session -> role
        self.users: Dict[str, str] = {}  # session -> user
        self.since = 0.0
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
        caps = {"hitCount": False, "conditional": False, "control": False, **self.capabilities}
        return {
            "link": "1.0",
            "engineAbi": ENGINE_ABI,
            "capabilities": caps,
            "probes": len(PROBES) if caps.get("probes") else 0,
            "image": {
                "md5": MD5,
                "buildId": BUILD_ID,
                "buildTime": "2026-10-14T00:00:00Z",
                "sourceHash": None,
            },
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
        command = msg.get("command")
        raw = msg.get("arguments", {})
        args = raw if isinstance(raw, dict) else {}
        self.received.append((str(command), args))
        time.sleep(self.delays.get(str(command), 0.0))
        try:
            if msg.get("type") != "request" or not isinstance(command, str):
                raise _Refusal("BAD_REQUEST", "Not a request envelope.")
            if command not in SPEC.requests:
                raise _Refusal("UNSUPPORTED", f"The core does not implement {command}.")
            problems = SPEC.check_request(command, raw)
            if problems:
                raise _Refusal("BAD_REQUEST", "; ".join(problems))
            body = self._handle(command, args)
            reply: JsonObj = {"success": True, "body": body}
        except _Refusal as r:
            reply = {"success": False, "error": {"code": r.code, "message": r.message}}
        with conn.lock:
            conn.seq += 1
            _send_frame(
                conn.sock,
                {"type": "response", "seq": conn.seq, "request_seq": msg.get("seq"),
                 "command": command, **reply},
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
        self.halt_reason = reason
        self.since = time.time() * 1000.0
        self.pending = None
        self.cycle_target = self.scans_left = self.cursor = None
        body: JsonObj = {"reason": reason, "task": TASK, "taskId": TASK_ID, "probe": self.pc,
                         "scan": self.scan, "since": self.since}
        if reason == "breakpoint":
            body["breakpointIds"] = [self.breakpoints[self.pc]]
        body.update(extra or {})
        self.push("stopped", body)
        self.push("runState", self.run_state())

    def run_state(self) -> JsonObj:
        """The real core's runState shape; the fake's one task, HALTED refined as the core does."""
        if self.state != "HALTED":
            tasks: List[Json] = (
                [{"task": TASK, "taskId": TASK_ID, "state": "running", "scan": self.scan}]
                if self.state == "RUNNING" else []
            )
            return {"state": self.state, "tasks": tasks, "attached": bool(self.sessions)}
        reason = self.halt_reason or "pause"
        task: JsonObj = {"task": TASK, "taskId": TASK_ID, "state": "parked", "reason": reason,
                         "probe": self.pc, "since": self.since, "scan": self.scan}
        controller = next((sid for sid, role in self.sessions.items() if role == "controller"), None)
        return {
            "state": "HALTED_AT_BP" if reason == "breakpoint" else "PAUSED",
            "tasks": [task],
            "attached": bool(self.sessions),
            "by": self.users.get(controller, "") if controller else "",
            "location": {"task": TASK, "taskId": TASK_ID, "probe": self.pc},
            "since": self.since,
        }

    def _resume(self) -> None:
        self.state = "RUNNING"
        self.halt_reason = None
        self.skip_once = True
        self.cv.notify_all()

    def inject_exception(self) -> None:
        with self.cv:
            if self.exception_filters and self.state == "RUNNING":
                self._halt("exception", {"exception": {"kind": "divByZero"}})
            else:
                self.state = "ERROR"
                self.push("runState", self.run_state())

    def inject_print(self, text: str) -> None:
        with self.cv:
            scan = self.scan
        self.push("output", {"category": "print", "task": TASK, "taskId": TASK_ID, "scan": scan,
                             "text": text})

    # -- requests

    def _handle(self, command: str, args: JsonObj) -> JsonObj:
        with self.cv:
            return self._handle_locked(command, args)

    def _need_halted(self) -> None:
        if self.state != "HALTED":
            raise _Refusal("NOT_HALTED", f"The task is {self.state.lower()}, not halted.")

    def _handle_locked(self, command: str, args: JsonObj) -> JsonObj:
        """docs/DEBUG_CORE_LINK.md request semantics; arguments are already spec-checked."""
        if command == "attach":
            sid = f"s{self.next_session}"
            self.next_session += 1
            self.sessions[sid] = str(args.get("role"))
            self.users[sid] = str(args.get("user"))
            return {"session": sid}
        if command == "status":
            return self.run_state()
        session = str(args.get("session"))
        if session not in self.sessions:
            raise _Refusal("SESSION_ENDED", "The core holds no such session.")
        if command in ("detach", "terminate"):
            role = self.sessions.pop(session)
            self.users.pop(session, None)
            lost = command == "detach" and args.get("reason") == "heartbeat_timeout"
            safe_stopped = False
            if role == "controller":
                self.breakpoints.clear()
                self.bp_file.clear()
                if command == "terminate":
                    self.state = "STOPPED"
                    self.push("runState", self.run_state())
                elif self.state == "HALTED" and lost:
                    # ADR-005 D4.6: the controller lost past P2 while halted: safe-stop.
                    self.state = "SAFE_STOPPED"
                    safe_stopped = True
                    self.push("runState", self.run_state())
                elif self.state == "HALTED":
                    self._resume()
                    self.push("continued", {"allThreadsContinued": True})
            if command == "terminate":
                return {}
            return {"forces": [], "safeStopped": safe_stopped}
        if command == "beatClient":
            return {}
        if "task" in args and _task(args["task"]) is None:
            raise _Refusal("BAD_REQUEST", f"The program has no task {args['task']!r}.")
        if command == "pause":
            if self.state in ("STOPPED", "ERROR", "SAFE_STOPPED"):
                raise _Refusal("NOT_RUNNING", "The program is not running.")
            self.pending = "pause"
            return {}
        if command == "continue":
            self._need_halted()
            self._resume()
            self.push("continued", {"allThreadsContinued": True})
            self.push("runState", self.run_state())
            return {"allThreadsContinued": True}
        if command in ("next", "stepIn", "stepOut"):
            self._need_halted()
            self.pending = "step"
            self._resume()
            self.push("continued", {"task": TASK, "taskId": TASK_ID})
            return {"task": TASK, "taskId": TASK_ID}
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
            self.push("runState", self.run_state())
            return {}
        if command == "restart":
            self.pc = 0
            if args.get("kind") == "cold":
                self.scan = 0
            self.pending = "entry" if args.get("startHalted") is True else None
            self.skip_once = False
            self.state = "RUNNING"
            self.cv.notify_all()
            self.push("runState", self.run_state())
            return {}
        if command == "setHaltMode":
            return {"mode": args.get("mode")}
        if command == "setBreakpoints":
            return self._set_breakpoints(int(_num(args.get("fileId"))), _list(args.get("probes")))
        if command == "clearAllBreakpoints":
            removed = len(self.breakpoints)
            self.breakpoints.clear()
            self.bp_file.clear()
            return {"removed": removed}
        if command == "setExceptionBreakpoints":
            self.exception_filters = [str(f) for f in _list(args.get("filters"))]
            return {"filters": list(self.exception_filters)}
        raise _Refusal("UNSUPPORTED", f"The core does not implement {command}.")

    def _set_breakpoints(self, file_id: int, items: List[Json]) -> JsonObj:
        """Replaces file_id's set; per-item results with ids stable per armed probe (pm ruling 6)."""
        probes = [
            int(_num(it.get("probe"))) if isinstance(it, dict) else int(_num(it)) for it in items
        ]
        for p in [p for p, f in self.bp_file.items() if f == file_id and p not in probes]:
            self.breakpoints.pop(p, None)
            self.bp_file.pop(p, None)
        limit = int(_num(self.capabilities.get("maxBreakpoints")))
        out: List[Json] = []
        for p in probes:
            if not 0 <= p < len(PROBES):
                out.append({"id": None, "probe": p, "verified": False,
                            "error": {"code": "INVALID_LOCATION", "message": "No such probe."}})
                continue
            if p not in self.breakpoints and len(self.breakpoints) >= limit:
                out.append({"id": None, "probe": p, "verified": False,
                            "error": {"code": "LIMIT_EXCEEDED",
                                      "message": f"At most {limit} breakpoints are supported."}})
                continue
            if p not in self.breakpoints:
                self.breakpoints[p] = self.next_bp
                self.next_bp += 1
            self.bp_file[p] = file_id
            out.append({"id": self.breakpoints[p], "probe": p, "verified": True})
        return {"breakpoints": out}


class _Refusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _num(v: Json) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _task(v: Json) -> Optional[int]:
    """A task argument, by name (case-insensitive, pm ruling 12) or by index; None if no such task."""
    if isinstance(v, str) and v.casefold() == TASK.casefold():
        return TASK_ID
    if isinstance(v, int) and not isinstance(v, bool) and v == TASK_ID:
        return TASK_ID
    return None


def _list(v: Json) -> List[Json]:
    return v if isinstance(v, list) else []
