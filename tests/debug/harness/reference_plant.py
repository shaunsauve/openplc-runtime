"""The real plant seam: the reference plant plugin (F150) inside a running plc_main, reached over
its Unix socket, plus a debugger-less endpoint over the runtime's core command socket.

`ReferencePlant` is the harness's PlantAdapter: it drives inputs, reads the scan witness, the
outputs and each task's virtual time (ADR-004 D4) from the plugin's samples, and arms the
plugin's scripted input schedule, which runs on PLC virtual time. `CoreSocketEndpoint` lets a
scenario that needs no debug session run against a real runtime: status from `STATUS`, `inject:
socket` as a core-socket line, `inject: plugin` with name `reference-plant` as run control
through the plant's test hook. A step that needs a session is BLOCKED.

Sample times are CLOCK_MONOTONIC ms, the same clock as WallClock, so the runner and the plugin
must share a kernel (they run in one container).

See also:
  tests/debug/plant/reference_plant.c - the plugin this talks to, and its line protocol.
  tests/debug/harness/interfaces.py - the Plant and Endpoint protocols implemented here.
  tests/debug/plant/e2e.sh - starts the runtime with the plugin and runs the runner against it.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import List, Mapping, Optional, cast

from interfaces import Blocked, Json, JsonObj, PlantSample, Session

CORE_SOCKET = "/run/runtime/plc_runtime.socket"
PLANT_NAME = "reference-plant"
POLL_S = 0.01
STALE_MS = 2000.0  # no sample for this long: the plant (or the PLC) is not exchanging


class _LineClient:
    """One persistent Unix stream connection with a line protocol."""

    def __init__(self, path: str, what: str) -> None:
        self.path = path
        self.what = what
        self.lock = threading.Lock()
        self.sock: Optional[socket.socket] = None
        self.buf = b""

    def _connect(self) -> socket.socket:
        if self.sock is None:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(10.0)
            try:
                s.connect(self.path)
            except OSError as exc:
                s.close()
                raise Blocked(f"no {self.what} at {self.path} ({exc.strerror or exc})") from exc
            self.sock = s
        return self.sock

    def _readline(self, s: socket.socket) -> str:
        while b"\n" not in self.buf:
            chunk = s.recv(65536)
            if not chunk:
                raise OSError(f"{self.what} closed the connection")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8")

    def ask(self, command: str, until: Optional[str] = None) -> List[str]:
        """Send one command; return its reply line, or every line up to `until` (excluded)."""
        with self.lock:
            s = self._connect()
            try:
                s.sendall(command.encode("utf-8") + b"\n")
                lines = [self._readline(s)]
                while until is not None and lines[-1] != until:
                    lines.append(self._readline(s))
            except OSError as exc:
                self.close_locked()
                raise Blocked(f"{self.what}: {exc}") from exc
        return lines[:-1] if until is not None else lines

    def close_locked(self) -> None:
        if self.sock is not None:
            self.sock.close()
        self.sock = None
        self.buf = b""

    def close(self) -> None:
        with self.lock:
            self.close_locked()


def _value(v: Json) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(int(v))
    raise Blocked(f"plant input value {v!r}: only BOOL and integers are driven")


class ReferencePlant:
    """PlantAdapter over the reference plant plugin's socket."""

    def __init__(self, path: str) -> None:
        self.client = _LineClient(path, "reference plant")
        self.samples: List[PlantSample] = []
        self.seq = 0
        self.lock = threading.Lock()
        self.error: Optional[str] = None
        self.stop = threading.Event()
        self.poller: Optional[threading.Thread] = None

    # -- polling

    def _poll_once(self) -> None:
        rows = self.client.ask(f"SAMPLES {self.seq}", until="END")
        fresh: List[PlantSample] = []
        for row in rows:
            doc = cast(JsonObj, json.loads(row))
            self.seq = max(self.seq, int(cast(int, doc["seq"])))
            witness = cast(JsonObj, doc["witness"])
            vt = cast(JsonObj, doc["vt"])
            fresh.append(
                PlantSample(
                    t_ms=float(cast(float, doc["t"])),
                    witness={k: int(cast(int, v)) for k, v in witness.items()},
                    vt_ms={k: float(cast(float, v)) for k, v in vt.items()},
                    outputs=dict(cast(JsonObj, doc["outputs"])),
                )
            )
        with self.lock:
            self.samples.extend(fresh)
            del self.samples[: max(0, len(self.samples) - 100000)]

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                self._poll_once()
                self.error = None
            except Blocked as exc:
                self.error = exc.reason
            self.stop.wait(POLL_S)

    def _ensure_polling(self) -> None:
        if self.poller is None:
            self._poll_once()  # a plant that is not there is BLOCKED here, not later
            self.poller = threading.Thread(target=self._run, name="plant-poll", daemon=True)
            self.poller.start()

    # -- Plant protocol

    def prepare(self, plant_setup: JsonObj) -> None:
        self._ensure_polling()
        self.client.ask("RESET")
        for entry in cast(List[Json], plant_setup.get("schedule", [])):
            item = cast(JsonObj, entry)
            at = float(cast(float, item["atMs"]))
            for addr, v in cast(JsonObj, item["set"]).items():
                reply = self.client.ask(f"SCHEDULE {at:g} {addr} {_value(v)}")[0]
                if reply != "OK":
                    raise Blocked(f"plant refused the schedule entry {addr} at {at:g} ms: {reply}")

    def set_inputs(self, values: Mapping[str, Json]) -> None:
        self._ensure_polling()
        for addr, v in values.items():
            reply = self.client.ask(f"SET {addr} {_value(v)}")[0]
            if reply != "OK":
                raise Blocked(f"plant refused {addr} = {v!r}: {reply}")

    def fail_exchanges(self, n: int) -> None:
        raise Blocked("reference plant: failExchanges is not built")

    def sample(self) -> PlantSample:
        self._ensure_polling()
        deadline = time.monotonic() + STALE_MS / 1000.0
        while True:
            with self.lock:
                last = self.samples[-1] if self.samples else None
            if last is not None:
                return last
            if time.monotonic() >= deadline:
                raise Blocked(f"reference plant sent no sample ({self.error or 'no exchange'})")
            time.sleep(POLL_S)

    def history(self, since_ms: float) -> List[PlantSample]:
        self._ensure_polling()
        self._poll_once()
        with self.lock:
            return [s for s in self.samples if s.t_ms > since_ms]

    # -- beyond the protocol

    def run_control(self, action: str, task: str) -> Json:
        if action not in ("hold", "resume"):
            raise Blocked(f"reference plant: unknown run-control action {action!r}")
        reply = self.client.ask(f"{action.upper()} {task}")[0]
        return reply == "OK"

    def mark(self, text: str) -> None:
        self.client.ask(f"MARK {text}")

    def close(self) -> None:
        self.stop.set()
        if self.poller is not None:
            self.poller.join(timeout=1.0)
        self.client.close()


_SESSION_SETUP = ("build", "haltPolicy", "safeValues", "haltMode", "startHalted", "tasks", "params")


class CoreSocketEndpoint:
    """A runtime with no debugger: the core command socket and the reference plant."""

    def __init__(self, plant: ReferencePlant, path: str = CORE_SOCKET) -> None:
        self.plant = plant
        self.core = _LineClient(path, "runtime core socket")
        self.description = f"plc_main core socket {path} (no debugger)"

    def prepare(self, setup: JsonObj) -> None:
        needs = [k for k in _SESSION_SETUP if k in setup]
        if setup.get("session") not in (None, "none") or needs:
            raise Blocked(f"no debugger: setup needs {needs or ['session']}")
        # A scenario that ended while the PLC was held must not leave the next one halted.
        self.plant.run_control("resume", "all")
        state = self._state()
        if state != "RUNNING":
            raise Blocked(f"runtime is {state}, not RUNNING")

    def _state(self) -> str:
        reply = self.core.ask("STATUS")[0]
        return reply.partition(":")[2] if reply.startswith("STATUS:") else reply

    def open_session(self) -> Session:
        raise Blocked("no debugger: this endpoint has no debug session")

    def status(self) -> JsonObj:
        return {"state": self._state()}

    def log(self) -> JsonObj:
        raise Blocked("no debugger: no log read-back path")

    def inject(self, kind: str, args: JsonObj) -> Json:
        if kind == "socket":
            return self.core.ask(str(args.get("command", "")))[0]
        if kind == "plugin" and args.get("name") == PLANT_NAME:
            return self.plant.run_control(str(args.get("action")), str(args.get("task", "all")))
        raise Blocked(f"inject {kind}: not available without a debugger")

    def credential(self, kind: str) -> Json:
        raise Blocked("no debugger: no credentials")

