"""Tests only: a simulated runtime running corpus:plant-io at statement granularity, with the
endpoint, plant and clock seams of interfaces.py. Time is simulated, so a 30 s halt costs
milliseconds; nothing here sleeps.

Just enough of ADR-003 for the plant-io run-control scenarios: initialize, setBreakpoints,
continue, pause, next, cycleStep, stop, stackTrace, variables/evaluate by path; events stopped,
continued, runState. Anything else answers UNSUPPORTED. ADR-004 arithmetic: the plant sees the
last complete scan (copy-out at scan end); virtual time is completed scans x interval.

Seeded defects (FakeWorld(defects={...})):
  late_stop          pause and cycleStep take effect one scan late
  wrong_reason       a statement step reports reason "breakpoint"
  missing_continued  `continued` is never sent

See also:
  tests/debug/harness/test_runner.py - the red/green proof that uses this fake.
  tests/debug/corpus/plant-io/program.st - the program modelled here, by its markers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Set

from interfaces import Blocked, Json, JsonObj, PlantSample

PROGRAM = Path(__file__).resolve().parent.parent / "corpus" / "plant-io" / "program.st"
TASK = "Main"
DEFECTS = {"late_stop", "wrong_reason", "missing_continued"}

Vars = Dict[str, Json]
OUTPUTS = {
    "%QD0": "witness",
    "%QD1": "counter",
    "%QD2": "out_dint",
    "%QD3": "out_real",
    "%QX0.0": "out_bool",
    "%QW0": "out_int",
    "%QW1": "out_a",
    "%QW2": "out_b",
    "%QW3": "x_seen",
    "%QW100": "out_plug",
}
INPUTS = {
    "%IX0.0": "in_enable",
    "%IX0.1": "load_x",
    "%IX0.2": "dwell_on",
    "%IW0": "in_value",
    "%ID0": "in_dint",
}


def _i(v: Json) -> int:
    return int(v) if isinstance(v, (int, float)) else 0


def _set(**changes: Json) -> Callable[[Vars], None]:
    def apply(v: Vars) -> None:
        v.update(changes)

    return apply


@dataclass(frozen=True)
class Stmt:
    marker: str
    guard: Callable[[Vars], bool]
    effect: Callable[[Vars], None]


def _always(v: Vars) -> bool:
    return True


# One entry per marked line; an entry's effect covers the unmarked statements that follow it.
# The 300 ms dwell (DWELL) costs no simulated time.
STATEMENTS: List[Stmt] = [
    Stmt("@WITNESS", _always, lambda v: v.update(witness=_i(v["witness"]) + 1)),
    Stmt("@COPY_A", _always, lambda v: v.update(out_a=v["x"])),
    Stmt("@K", _always, lambda v: v.update(counter=_i(v["counter"]) + 1)),
    Stmt("@IF", _always, lambda v: None),
    Stmt("@THEN", lambda v: v["in_enable"] is True, _set(out_bool=True, branch=1)),
    Stmt("@ELSE", lambda v: v["in_enable"] is not True, _set(out_bool=False, branch=2)),
    Stmt(
        "@INT",
        _always,
        lambda v: v.update(
            out_int=v["in_value"],
            out_dint=v["in_dint"],
            out_real=_i(v["in_value"]) / 2.0,
            out_plug=_i(v["in_value"]) + 1,
        ),
    ),
    Stmt("@LOAD_X", lambda v: v["load_x"] is True, lambda v: v.update(x=v["in_value"])),
    Stmt("@DWELL", _always, lambda v: None),
    Stmt("@L", _always, lambda v: v.update(x_seen=_i(v["x"]) * 2)),
    Stmt("@COPY_B", _always, lambda v: v.update(out_b=v["x"])),
]


def _locations() -> Dict[str, JsonObj]:
    out: Dict[str, JsonObj] = {}
    for n, line in enumerate(PROGRAM.read_text(encoding="utf-8").splitlines(), 1):
        m = re.search(r"\(\*(@[A-Za-z0-9_]+)\*\)", line)
        if m:
            out[m.group(1)] = {
                "file": "program.st",
                "line": n,
                "col": len(line) - len(line.lstrip()) + 1,
            }
    return out


class FakeWorld:
    """The simulated PLC + plant; also the Clock."""

    def __init__(self, defects: Optional[Set[str]] = None) -> None:
        self.defects = set(defects or ())
        unknown = self.defects - DEFECTS
        if unknown:
            raise ValueError(f"unknown defects {sorted(unknown)}")
        self.loc = _locations()
        self.t = 0.0
        self.interval = 100.0
        self.sample_period = 10.0
        self.halt_policy = "hold"
        self.vars: Vars = {
            "witness": 0,
            "counter": 0,
            "out_dint": 0,
            "out_real": 0.0,
            "out_bool": False,
            "out_int": 0,
            "out_a": 0,
            "out_b": 0,
            "x_seen": 0,
            "out_plug": 0,
            "x": 0,
            "branch": 0,
            "spins": 0,
            "in_enable": False,
            "load_x": False,
            "dwell_on": False,
            "in_value": 0,
            "in_dint": 0,
        }
        self.plant_inputs: Dict[str, Json] = {}
        self.published: Dict[str, Json] = {addr: self.vars[name] for addr, name in OUTPUTS.items()}
        self.completed = 0
        self.state = "RUNNING"
        self.by: Optional[str] = None
        self.since = 0.0
        self.pc: Optional[int] = None  # next statement to execute when mid-scan
        self.halted: Optional[str] = None
        self.scan_start = 0.0
        self.next_scan = 0.0
        self.next_sample = 0.0
        self.samples: List[PlantSample] = []
        self.breakpoints: Set[int] = set()
        self.step_pending = False
        self.cycle_target: Optional[tuple[int, int]] = None  # (scan number, statement index)
        self.pause_after: Optional[int] = None
        self.sessions: List[FakeSession] = []

    # -- seams

    def endpoint(self) -> "FakeEndpoint":
        return FakeEndpoint(self)

    def plant(self) -> "FakePlant":
        return FakePlant(self)

    def now_ms(self) -> float:
        return self.t

    def sleep_ms(self, ms: float) -> None:
        self.advance(self.t + max(ms, 0.0))

    # -- simulation

    def advance(self, target: float) -> None:
        while True:
            scan_due = self.state == "RUNNING" and self.pc is None
            when = min(self.next_sample, self.next_scan) if scan_due else self.next_sample
            if when > target:
                break
            self.t = when
            if self.next_sample <= self.t:
                self._sample()
                self.next_sample += self.sample_period
            if scan_due and self.next_scan <= self.t:
                self._start_scan()
        self.t = target

    def _sample(self) -> None:
        self.samples.append(
            PlantSample(
                self.t,
                {TASK: _i(self.published["%QD0"])},
                {TASK: self.completed * self.interval},
                dict(self.published),
            )
        )

    def _start_scan(self) -> None:
        self.scan_start = self.t
        self.next_scan = self.t + self.interval
        for addr, value in self.plant_inputs.items():  # copy-in
            if addr in INPUTS:
                self.vars[INPUTS[addr]] = value
        self.pc = 0
        if self.pause_after is not None and self.completed >= self.pause_after:
            self.pause_after = None
            self._halt("pause", 0)
            return
        self._execute(skip_bp=False)

    def _scan_number(self) -> int:
        return self.completed + 1

    def _execute(self, skip_bp: bool) -> None:
        """Run from self.pc until a halt or the end of the scan."""
        first = True
        while self.pc is not None and self.pc < len(STATEMENTS):
            stmt = STATEMENTS[self.pc]
            if not stmt.guard(self.vars):
                self.pc += 1
                continue
            line = _i(self.loc[stmt.marker]["line"])
            resuming = first and skip_bp
            first = False
            if not resuming:
                if self.cycle_target == (self._scan_number(), self.pc):
                    self.cycle_target = None
                    self._halt("step", self.pc)
                    return
                if self.step_pending:
                    self.step_pending = False
                    bp = line in self.breakpoints or "wrong_reason" in self.defects
                    self._halt("breakpoint" if bp else "step", self.pc)
                    return
                if line in self.breakpoints:
                    self._halt("breakpoint", self.pc)
                    return
            stmt.effect(self.vars)
            self.pc += 1
        self.completed += 1  # copy-out
        self.published = {addr: self.vars[name] for addr, name in OUTPUTS.items()}
        self.pc = None
        if self.next_scan < self.t:
            self.next_scan = self.t

    def _halt(self, reason: str, pc: int) -> None:
        self.pc = pc
        self.halted = reason
        self._set_state("PAUSED" if reason == "pause" else "HALTED_AT_BP")
        loc = self.loc[STATEMENTS[pc].marker]
        self._emit(
            "stopped",
            {
                "reason": reason,
                "task": TASK,
                "location": dict(loc),
                "scan": self._scan_number(),
                "vt": self.completed * self.interval,
                "wall": self.t,
                "haltPolicy": self.halt_policy,
            },
        )

    def _resume(self) -> None:
        self.halted = None
        self._set_state("RUNNING")
        if "missing_continued" not in self.defects:
            self._emit("continued", {"task": "all"})
        self._execute(skip_bp=True)

    def _set_state(self, state: str) -> None:
        self.state = state
        self.since = self.t
        self._emit("runState", {"state": state, "tasks": [TASK], "by": self.by, "since": self.t})

    def _emit(self, name: str, body: JsonObj) -> None:
        for s in self.sessions:
            s.inbox.append({"event": name, "body": body})

    # -- requests

    def handle(self, session: "FakeSession", command: str, args: JsonObj) -> JsonObj:
        late = 1 if "late_stop" in self.defects else 0
        halted = self.halted is not None
        if command == "initialize":
            self.by = "fake-user"
            return _ok(
                {
                    "protocol": "1.0",
                    "comparison": "match",
                    "haltPolicy": self.halt_policy,
                    "params": {"P1": 2, "P2": 15},
                    "resumeToken": "fake-token",
                    "image": {"md5": "fake", "buildId": "fake"},
                    "capabilities": {
                        "probes": True,
                        "cycleStep": True,
                        "control": True,
                        "modify": False,
                    },
                }
            )
        if command == "setBreakpoints":
            lines = args.get("lines")
            wanted = [_i(x) for x in lines] if isinstance(lines, list) else []
            valid = {loc["line"] for loc in self.loc.values()}
            self.breakpoints = {ln for ln in wanted if ln in valid}
            return _ok({"breakpoints": [{"verified": ln in valid, "line": ln} for ln in wanted]})
        if command == "continue":
            if halted:
                self._resume()
            return _ok({})
        if command == "pause":
            self.pause_after = self.completed + late
            return _ok({})
        if command in ("next", "cycleStep"):
            if not halted or self.pc is None:
                return _err("NOT_HALTED")
            if command == "next":
                self.step_pending = True
            else:
                self.cycle_target = (self._scan_number() + 1 + late, self.pc)
            self._resume()
            return _ok({})
        if command == "stop":
            self.state, self.halted, self.pc = "STOPPED", None, None
            self._set_state("STOPPED")
            return _ok({})
        if command == "stackTrace":
            if not halted or self.pc is None:
                return _err("NOT_HALTED")
            loc = self.loc[STATEMENTS[self.pc].marker]
            return _ok(
                {
                    "stackFrames": [
                        {
                            "pou": "PlantIO",
                            "kind": "program",
                            "instance": "PROG0",
                            "location": dict(loc),
                        }
                    ]
                }
            )
        if command in ("variables", "evaluate"):
            path = args.get("path", args.get("expression"))
            name = (
                path[len("PROG0.") :]
                if isinstance(path, str) and path.startswith("PROG0.")
                else None
            )
            if name is None or name not in self.vars:
                return _err("BAD_REQUEST")
            return _ok({"value": self.vars[name]})
        return _err("UNSUPPORTED")

    def status(self) -> JsonObj:
        return {
            "state": self.state,
            "tasks": [TASK],
            "by": self.by,
            "since": self.since,
            "staleness_ms": 0,
            "sessions": [{"user": "fake-user", "role": "controller"} for _ in self.sessions],
        }


def _ok(body: JsonObj) -> JsonObj:
    return {"type": "response", "success": True, "body": body}


def _err(code: str) -> JsonObj:
    return {
        "type": "response",
        "success": False,
        "error": {"code": code, "message": f"fake: {code}"},
    }


class FakeSession:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world
        self.inbox: List[JsonObj] = []

    def request(self, command: str, args: JsonObj) -> JsonObj:
        return self.world.handle(self, command, args)

    def events(self) -> List[JsonObj]:
        out, self.inbox = self.inbox, []
        return out

    def close(self) -> None:
        if self in self.world.sessions:
            self.world.sessions.remove(self)


class FakeEndpoint:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world
        self.description = "fake endpoint (plant-io simulation)"

    def prepare(self, setup: JsonObj) -> None:
        tasks = setup.get("tasks")
        main = tasks.get(TASK) if isinstance(tasks, dict) else None
        if isinstance(main, dict) and isinstance(main.get("intervalMs"), (int, float)):
            self.world.interval = float(_i(main["intervalMs"]))
        plant = setup.get("plant")
        if isinstance(plant, dict) and isinstance(plant.get("samplePeriodMs"), (int, float)):
            self.world.sample_period = float(_i(plant["samplePeriodMs"])) or 1.0
        if setup.get("haltPolicy") in ("hold", "safe"):
            self.world.halt_policy = str(setup["haltPolicy"])

    def open_session(self) -> FakeSession:
        s = FakeSession(self.world)
        self.world.sessions.append(s)
        return s

    def status(self) -> JsonObj:
        return self.world.status()

    def log(self) -> JsonObj:
        return {}

    def inject(self, kind: str, args: JsonObj) -> None:
        raise Blocked(f"fake endpoint has no {kind} injection")


class FakePlant:
    def __init__(self, world: FakeWorld) -> None:
        self.world = world

    def set_inputs(self, values: Mapping[str, Json]) -> None:
        self.world.plant_inputs.update(values)

    def fail_exchanges(self, n: int) -> None:
        raise Blocked("fake plant cannot fail exchanges")

    def sample(self) -> PlantSample:
        w = self.world
        return PlantSample(
            w.t,
            {TASK: _i(w.published["%QD0"])},
            {TASK: w.completed * w.interval},
            dict(w.published),
        )

    def history(self, since_ms: float) -> List[PlantSample]:
        return [s for s in self.world.samples if s.t_ms > since_ms]
