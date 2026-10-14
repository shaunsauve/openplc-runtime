"""Headless scenario runner (E13.4): executes corpus oracles against a debug endpoint and emits a
per-step trace and a verdict, PASS, DIVERGE (first mismatch) or BLOCKED (cannot be judged here).

Usage:
  python3 tests/debug/harness/runner.py [--endpoint URL] [--token JWT] [--insecure]
      [--build-dir DIR] [--results PATH] [--trace] ORACLE_OR_DIR...
Without --results nothing is written (dry run): the lines that would be appended are printed.

See also:
  tests/debug/harness/interfaces.py - Endpoint, Session, Plant, Clock and BuildOutputs seams, Blocked.
  tests/debug/harness/build_views.py - build directory reader, `inspect` views, build-derived symbols.
  tests/debug/harness/matchers.py - `~` matchers and captures.
  tests/debug/harness/socketio_endpoint.py - the real endpoint (ADR-003 /api/debug-session).
  tests/debug/harness/fake_endpoint.py - simulated endpoint and plant used by test_runner.py.
  tests/debug/harness/run_all.sh - runs every corpus oracle and summarises the verdicts.
  tests/debug/corpus/README.md - the oracle format this file implements.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import platform
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple, cast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_views  # noqa: E402
from interfaces import (  # noqa: E402
    Blocked,
    BuildOutputs,
    Clock,
    Endpoint,
    Json,
    JsonObj,
    Plant,
    PlantSample,
    Session,
    UnavailableBuild,
    UnavailablePlant,
    WallClock,
)
from matchers import MISSING, Captures, Mismatch, Observed, is_matcher, match  # noqa: E402

__all__ = [
    "UnavailableBuild",
    "UnavailablePlant",
    "WallClock",
    "run_scenario",
    "results_lines",
    "append_results",
    "format_trace",
]

CORPUS = HERE.parent / "corpus"
TEST_ID = "tests/debug/harness/runner.py"
POLL_MS = 5.0
DEFAULT_WITHIN_MS = 5000.0
MARKER = re.compile(r"^@[A-Za-z0-9_]+$")
SECONDARY = re.compile(r"^(variant|reject)-.*\.st$")
ACTIONS = ("request", "event", "noEvent", "plant", "wait", "observe", "inspect", "inject", "loop")
MAIN = "main"  # the stream a step without `client` acts on
# stopped/event expectation key -> body key (ADR-003 D4 names the virtual time `vt`).
EVENT_KEYS = {
    "reason": "reason",
    "task": "task",
    "location": "location",
    "exception": "exception",
    "scan": "scan",
    "vtMs": "vt",
}


class Diverge(Exception):
    def __init__(self, reason: str, mismatch: Optional[Mismatch] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.mismatch = mismatch


@dataclass
class StepTrace:
    index: int
    action: str
    status: str  # match | DIVERGE | BLOCKED
    detail: str
    ms: float


@dataclass
class Verdict:
    status: str  # PASS | DIVERGE | BLOCKED
    step: Optional[int]
    reason: str


@dataclass
class RunResult:
    scenario: str
    criteria: List[str]
    verdict: Verdict
    trace: List[StepTrace] = field(default_factory=list)


# ---------------------------------------------------------------- markers


def _marker_table(corpus_dir: Path) -> Dict[str, Dict[str, int]]:
    """file name -> marker -> 1-based line, over the program's primary sources (and family members)."""
    dirs = [corpus_dir]
    family = corpus_dir / "family.txt"
    if family.exists():
        names = re.findall(r"\bbench_\w+", family.read_text(encoding="utf-8"))
        dirs = [corpus_dir.parent / n for n in names]
    table: Dict[str, Dict[str, int]] = {}
    for d in dirs:
        for src in sorted(d.glob("*.st")):
            if SECONDARY.match(src.name):
                continue
            entry = table.setdefault(src.name, {})
            for n, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
                for name in re.findall(r"\(\*(@[A-Za-z0-9_]+)\*\)", line):
                    entry[name] = n
    return table


def _resolve(node: Json, table: Dict[str, Dict[str, int]], file: Optional[str] = None) -> Json:
    """Replace `@NAME` markers with line numbers; a sibling `file` key picks the source."""
    if isinstance(node, dict):
        here = node.get("file")
        ctx = here if isinstance(here, str) else file
        return {k: _resolve(v, table, ctx) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, table, file) for v in node]
    if isinstance(node, str) and MARKER.match(node):
        if file is not None and node in table.get(file, {}):
            return table[file][node]
        hits = [t[node] for t in table.values() if node in t]
        if len(hits) == 1:
            return hits[0]
    return node


# ---------------------------------------------------------------- runner


def _obj(node: Json) -> JsonObj:
    return node if isinstance(node, dict) else {}


def _num(node: Json, default: float) -> float:
    return float(node) if isinstance(node, (int, float)) and not isinstance(node, bool) else default


def _flat(steps: Json) -> Iterator[JsonObj]:
    """Every step, loop bodies included."""
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict):
            yield step
            if "loop" in step:
                yield from _flat(step.get("steps"))


@dataclass
class _Stream:
    """One client stream and the events it received but no step has consumed yet."""

    session: Session
    inbox: List[Tuple[int, JsonObj]] = field(default_factory=list)


class _Run:
    def __init__(
        self,
        doc: JsonObj,
        table: Dict[str, Dict[str, int]],
        endpoint: Endpoint,
        plant: Plant,
        clock: Clock,
        build: BuildOutputs,
    ) -> None:
        self.doc = doc
        self.corpus = str(doc.get("corpus", ""))
        self.table = table
        self.endpoint = endpoint
        self.plant = plant
        self.clock = clock
        self.build = build
        self.setup = _obj(doc.get("setup"))
        self.captures: Captures = {}
        self.streams: Dict[str, _Stream] = {}  # insertion order = opening order
        self.arrivals = 0
        self.last_witness: Optional[Dict[str, int]] = None
        self.step_end_ms = clock.now_ms()

    # -- plumbing

    def _stream(self, name: str) -> _Stream:
        if name not in self.streams:
            self.streams[name] = _Stream(self.endpoint.open_session())
        return self.streams[name]

    def _clients(self, step: JsonObj) -> List[str]:
        """The stream names a step acts on (README § Clients)."""
        spec = step.get("client", MAIN)
        if spec == "*":
            names = list(self.streams)
        elif isinstance(spec, str):
            return [spec]
        else:
            group = str(_obj(spec).get("group"))
            count = _obj(spec).get("count")
            if isinstance(count, int):
                return [f"{group}.{i}" for i in range(1, count + 1)]
            names = [n for n in self.streams if n.startswith(group + ".")]
        if not names:
            raise Diverge(f"client {json.dumps(spec)} names no open stream")
        return names

    def _drain(self) -> None:
        for stream in self.streams.values():
            for ev in stream.session.events():
                self.arrivals += 1
                stream.inbox.append((self.arrivals, ev))

    def _request(self, client: str, command: str, args: JsonObj) -> JsonObj:
        resp = self._stream(client).session.request(command, args)
        self._drain()
        return resp

    def _check(self, expected: Json, observed: Observed, path: str) -> None:
        found = match(expected, observed, self.captures, path)
        if found is not None:
            raise Diverge(found.describe(), found)

    def _sample(self) -> PlantSample:
        return self.plant.sample()

    # -- argument symbols (README § Argument symbols)

    def _args(self, node: Json, args: JsonObj) -> Json:
        if is_matcher(node):
            return self._symbol(cast(JsonObj, node), args)
        if isinstance(node, dict):
            return {k: self._args(v, args) for k, v in node.items()}
        if isinstance(node, list):
            return [self._args(v, args) for v in node]
        return node

    def _count_arg(self, node: Json, args: JsonObj, what: str) -> int:
        value = self._args(node, args)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise Diverge(f"{what}: count {json.dumps(value)} is not a non-negative number")
        return int(value)

    def _symbol(self, sym: JsonObj, args: JsonObj) -> Json:
        if "~ref" in sym:
            name = str(sym["~ref"])
            if name not in self.captures or isinstance(self.captures[name], type(MISSING)):
                raise Diverge(f"argument ~ref {name!r}: nothing captured")
            base = self.captures[name]
            add = sym.get("~add", 0)
            if not add:
                return base
            if isinstance(base, bool) or not isinstance(base, (int, float)):
                raise Diverge(
                    f"argument ~ref {name!r} ~add: captured {json.dumps(base)} is not a number"
                )
            return base + add if isinstance(add, (int, float)) else base
        if "~build" in sym:
            what, _, variant = str(sym["~build"]).partition(":")
            if what != "identity":
                raise Blocked(f"unknown ~build symbol {what!r}")
            return build_views.identity(self.build, self.corpus, variant or None)
        if "~lines" in sym:
            file = args.get("file")
            if not isinstance(file, str):
                raise Diverge("~lines needs the request's `file` argument")
            n = self._count_arg(sym["~lines"], args, "~lines")
            return build_views.breakpoint_lines(self.build, self.corpus, file, n)
        if "~leaves" in sym:
            n = self._count_arg(sym.get("~count", 0), args, "~leaves")
            return build_views.leaf_paths(self.build, self.corpus, str(sym["~leaves"]), n)
        if "~token" in sym:
            return self.endpoint.credential(str(sym["~token"]))
        raise Blocked(f"unknown argument symbol {sorted(sym)}")

    # -- setup

    def start(self) -> None:
        if self.doc.get("kind") != "runtime":
            return  # build scenarios read compiler outputs; no endpoint or plant involved
        self.endpoint.prepare(self.setup)
        inputs = _obj(_obj(self.setup.get("plant")).get("inputs"))
        if inputs:
            self.plant.set_inputs(inputs)
        if self._uses_plant():
            self.last_witness = dict(self._sample().witness)
        self.step_end_ms = self.clock.now_ms()

    def _uses_plant(self) -> bool:
        return any(
            "plant" in s or s.get("observe") == "plant" or "wait" in s or "noEvent" in s
            for s in _flat(self.doc.get("steps"))
        )

    # -- steps

    def step(self, raw: JsonObj) -> str:
        step = cast(JsonObj, _resolve(raw, self.table))
        action = next((a for a in ACTIONS if a in step), "")
        handler = {
            "request": self._step_request,
            "event": self._step_event,
            "noEvent": self._step_no_event,
            "plant": self._step_plant,
            "wait": self._step_wait,
            "observe": self._step_observe,
            "inspect": self._step_inspect,
            "inject": self._step_inject,
            "loop": self._step_loop,
        }.get(action)
        if handler is None:
            raise Blocked(f"step has no known action ({sorted(step)})")
        detail = handler(step)
        self._drain()
        return detail

    def _step_loop(self, step: JsonObj) -> str:
        n = int(_num(step["loop"], 1))
        body = [s for s in cast(List[Json], step.get("steps", [])) if isinstance(s, dict)]
        for i in range(n):
            for j, sub in enumerate(body):
                try:
                    self.step(sub)
                except (Diverge, Blocked) as exc:
                    exc.reason = f"iteration {i + 1}/{n}, body step {j}: {exc.reason}"
                    raise
        return f"loop x{n} ({len(body)} steps)"

    def _step_request(self, step: JsonObj) -> str:
        command = str(step["request"])
        raw_args = _obj(step.get("args"))
        args = cast(JsonObj, self._args(raw_args, raw_args))
        clients = self._clients(step)
        expect = _obj(step.get("expect"))
        repeat = int(_num(step.get("repeat"), 1))
        for _ in range(repeat):
            for client in clients:
                sent = dict(args)
                if command == "initialize":
                    sent.setdefault("protocol", "1.0")
                    role = self.setup.get("session")
                    if client == MAIN and role in ("controller", "observer"):
                        sent.setdefault("role", role)
                self._check_response(command, expect, self._request(client, command, sent), client)
        who = "" if clients == [MAIN] else f" on {len(clients)} clients"
        return (f"{command} x{repeat}" if repeat > 1 else command) + who

    def _check_response(self, command: str, expect: JsonObj, resp: JsonObj, client: str) -> None:
        success = resp.get("success") is True
        error = _obj(resp.get("error"))
        code = error.get("code")
        if code == "UNSUPPORTED" and expect.get("error") != "UNSUPPORTED":
            raise Blocked(
                f"{command} refused UNSUPPORTED (capability absent): {error.get('message', '')}"
            )
        if not expect:
            return
        where = command if client == MAIN else f"{client}:{command}"
        self._check(expect.get("success"), success, f"{where}.success")
        if "error" in expect:
            self._check(expect["error"], code if code is not None else MISSING, f"{where}.error")
        if "body" in expect:
            body = _obj(resp.get("body"))
            observed: JsonObj = body if success else {**error, **body}
            self._check(expect["body"], observed, f"{where}.body")

    def _wait_event(self, client: str, name: str, deadline: float) -> Optional[JsonObj]:
        stream = self._stream(client)
        while True:
            self._drain()
            for i, (_, ev) in enumerate(stream.inbox):
                if ev.get("event") == name:
                    del stream.inbox[i]
                    return ev
            if self.clock.now_ms() >= deadline:
                return None
            self.clock.sleep_ms(POLL_MS)

    def _step_event(self, step: JsonObj) -> str:
        name = str(step["event"])
        within = _num(step.get("withinMs"), DEFAULT_WITHIN_MS)
        clients = self._clients(step)
        count = step.get("count")
        expect = _obj(step.get("expect"))
        repeat = int(_num(step.get("repeat"), 1))
        for r in range(repeat):
            rep = f" (repetition {r + 1})" if repeat > 1 else ""
            deadline = self.clock.now_ms() + within
            received = 0
            for client in clients:
                on = "" if client == MAIN else f" on {client}"
                ev = self._wait_event(client, name, deadline)
                try:
                    if ev is None:
                        raise Diverge(f"no '{name}' event{on} within {within:g} ms{rep}")
                    self._check_event(client, name, expect, _obj(ev.get("body")))
                except Diverge:
                    if count is None:
                        raise
                    continue  # with `count`, a stream without a matching event is not counted
                received += 1
            if count is not None:
                self._check(count, received, f"{name}.count{rep}")
            if step.get("thenContinue") is True:
                self._request(clients[0], "continue", {})
        who = "" if clients == [MAIN] else f" on {len(clients)} clients"
        return (f"{name} x{repeat}" if repeat > 1 else name) + who

    def _check_event(self, client: str, name: str, expect: JsonObj, body: JsonObj) -> None:
        where = name if client == MAIN else f"{client}:{name}"
        for key, body_key in EVENT_KEYS.items():
            if key in expect:
                self._check(
                    expect[key], body[body_key] if body_key in body else MISSING, f"{where}.{key}"
                )
        if "body" in expect:
            self._check(expect["body"], body, f"{where}.body")
        if "stack" in expect:
            self._check_stack(client, expect["stack"], expect.get("task"))
        for path, m in _obj(expect.get("variables")).items():
            self._check(m, self._read_variable(client, path), f"{where}.variables.{path}")

    def _check_stack(self, client: str, expected: Json, task: Json) -> None:
        # ASSUMED: stackTrace's body lists frames, innermost first, as DAP's `stackFrames`; the
        # oracle's frames must match the innermost ones in order (a prefix).
        resp = self._request(client, "stackTrace", {"task": task} if isinstance(task, str) else {})
        if resp.get("success") is not True:
            raise Diverge(f"stackTrace refused: {json.dumps(resp.get('error'))}")
        frames = _obj(resp.get("body")).get("stackFrames")
        if not isinstance(frames, list) or not isinstance(expected, list):
            raise Diverge("stackTrace body has no stackFrames list")
        if len(frames) < len(expected):
            raise Diverge(f"stack has {len(frames)} frames, oracle names {len(expected)}")
        for i, (want, got) in enumerate(zip(expected, frames)):
            want_obj = dict(_obj(want))
            scopes = _obj(want_obj.pop("scopes", None))
            self._check(want_obj, got, f"stack[{i}]")
            for scope, paths in scopes.items():
                for path, m in _obj(paths).items():
                    self._check(
                        m, self._read_variable(client, path), f"stack[{i}].scopes.{scope}.{path}"
                    )

    def _read_variable(self, client: str, path: str) -> Observed:
        resp = self._request(client, "variables", {"path": path})
        if resp.get("success") is not True:
            error = _obj(resp.get("error"))
            if error.get("code") == "UNSUPPORTED":
                raise Blocked(f"variables {path}: UNSUPPORTED")
            raise Diverge(f"variables {path} refused: {error.get('code')}")
        body = _obj(resp.get("body"))
        return body["value"] if "value" in body else MISSING

    def _step_no_event(self, step: JsonObj) -> str:
        name = str(step["noEvent"])
        clients = self._clients(step)
        self._drain()
        start = self.arrivals

        def forbid() -> None:
            self._drain()
            for client in clients:
                for seq, ev in self._stream(client).inbox:
                    if seq > start and ev.get("event") == name:
                        raise Diverge(
                            f"forbidden '{name}' event arrived on {client}: "
                            f"{json.dumps(ev.get('body'))[:160]}"
                        )

        if "forScans" in step:
            task = step.get("task")
            self._wait_scans(
                int(_num(step["forScans"], 1)), task if isinstance(task, str) else None, forbid
            )
            return f"no {name} for {step['forScans']} scans"
        deadline = self.clock.now_ms() + _num(step.get("forMs"), 0.0)
        while self.clock.now_ms() < deadline:
            forbid()
            self.clock.sleep_ms(min(POLL_MS * 20, deadline - self.clock.now_ms()))
        forbid()
        return f"no {name} for {step.get('forMs')} ms"

    def _wait_scans(
        self, n: int, task: Optional[str], tick: Optional[Callable[[], None]] = None
    ) -> None:
        base = self._sample().witness
        timeout = 5000.0 + n * 1000.0
        deadline = self.clock.now_ms() + timeout
        while True:
            now = self._sample().witness
            tasks = [task] if task else list(now)
            advanced = max((now.get(t, 0) - base.get(t, 0) for t in tasks), default=0)
            if tick is not None:
                tick()
            if advanced >= n:
                return
            if self.clock.now_ms() >= deadline:
                raise Diverge(f"plant counted {advanced} of {n} scans in {timeout:g} ms")
            self.clock.sleep_ms(POLL_MS * 2)

    def _step_wait(self, step: JsonObj) -> str:
        wait = _obj(step["wait"])
        if "wallMs" in wait:
            self.clock.sleep_ms(_num(wait["wallMs"], 0.0))
        elif "scans" in wait:
            self._wait_scans(int(_num(wait["scans"], 1)), None)
        elif "vtMs" in wait:
            target = _num(wait["vtMs"], 0.0)
            base = self._sample().vt_ms
            deadline = self.clock.now_ms() + 5000.0 + target * 2
            while (
                max((v - base.get(t, 0.0) for t, v in self._sample().vt_ms.items()), default=0.0)
                < target
            ):
                if self.clock.now_ms() >= deadline:
                    raise Diverge(f"plant virtual time did not advance {target:g} ms")
                self.clock.sleep_ms(POLL_MS * 2)
        return json.dumps(wait)

    def _step_plant(self, step: JsonObj) -> str:
        plant = _obj(step["plant"])
        if "set" in plant:
            self.plant.set_inputs(_obj(plant["set"]))
        for values in cast(List[Json], plant.get("sequence", [])):
            self.plant.set_inputs(_obj(values))
        if "failExchanges" in plant:
            self.plant.fail_exchanges(int(_num(plant["failExchanges"], 1)))
        return "plant " + ",".join(sorted(plant))

    def _step_observe(self, step: JsonObj) -> str:
        kind = str(step["observe"])
        expect = _obj(step.get("expect"))
        throughout = step.get("throughout") is True
        if kind == "plant":
            self._observe_plant(expect, throughout)
        elif kind == "status":
            # ASSUMED: status/log have no sample history, so `throughout` is judged on the final read.
            status = self.endpoint.status()
            if "state" in expect:
                self._check(
                    expect["state"],
                    status["state"] if "state" in status else MISSING,
                    "status.state",
                )
            if "fields" in expect:
                self._check(expect["fields"], status, "status.fields")
        elif kind == "log":
            self._check(expect.get("fields", {}), self.endpoint.log(), "log.fields")
        elif kind == "session":
            for client in self._clients(step):
                for path, m in _obj(expect.get("variables")).items():
                    self._check(m, self._read_variable(client, path), f"session.variables.{path}")
        return f"observe {kind}" + (" throughout" if throughout else "")

    def _observe_plant(self, expect: JsonObj, throughout: bool) -> None:
        final = self._sample()
        samples = self.plant.history(self.step_end_ms) if throughout or "maxGapMs" in expect else []
        prev: Optional[PlantSample] = None
        for s in samples if throughout else []:
            scratch = dict(self.captures)  # history must not bind captures; the final read does
            self._check_sample(expect, s, scratch, f"plant@{s.t_ms:g}ms")
            if prev is not None:
                for t, w in s.witness.items():
                    if w < prev.witness.get(t, w):
                        raise Diverge(
                            f"plant@{s.t_ms:g}ms: witness.{t} went back {prev.witness[t]} -> {w} (restart)"
                        )
            prev = s
        self._check_sample(expect, final, self.captures, "plant")
        if "scanDelta" in expect:
            base = self.last_witness or {}
            for t, m in _obj(expect["scanDelta"]).items():
                delta = final.witness[t] - base.get(t, 0) if t in final.witness else MISSING
                self._check(m, delta, f"plant.scanDelta.{t}")
        for t, m in _obj(expect.get("maxGapMs")).items():
            self._check(m, _max_gap(samples, t), f"plant.maxGapMs.{t}")
        self.last_witness = dict(final.witness)

    def _check_sample(
        self, expect: JsonObj, s: PlantSample, captures: Captures, where: str
    ) -> None:
        planes: Dict[str, Dict[str, Json]] = {
            "witness": dict(s.witness),
            "vtMs": dict(s.vt_ms),
            "outputs": s.outputs,
        }
        for key, plane in planes.items():
            for name, m in _obj(expect.get(key)).items():
                found = match(
                    m, plane[name] if name in plane else MISSING, captures, f"{where}.{key}.{name}"
                )
                if found is not None:
                    raise Diverge(found.describe(), found)

    def _step_inspect(self, step: JsonObj) -> str:
        kind = str(step["inspect"])
        expect = _obj(step.get("expect"))
        unavailable = build_views.UNAVAILABLE_KEYS.get(kind, {})
        for key in expect:
            if key in unavailable:
                raise Blocked(f"inspect {kind}.{key}: {unavailable[key]}")
        view = build_views.view(self.build, self.corpus, kind)
        if expect:
            self._check(expect, view, f"inspect.{kind}")
        return f"inspect {kind}"

    def _step_inject(self, step: JsonObj) -> str:
        kind = str(step["inject"])
        raw_args = _obj(step.get("args"))
        args = cast(JsonObj, self._args(raw_args, raw_args))
        if kind == "killClient":
            # The client is the harness's own stream: dropping its transport with no detach is
            # what the runtime sees of a SIGKILLed client process.
            for client in self._clients(step):
                stream = self.streams.pop(client, None)
                if stream is None:
                    raise Diverge(f"killClient: no open stream {client!r}")
                stream.session.kill()
            return f"inject {kind}"
        result = self.endpoint.inject(kind, args)
        if "expect" in step:
            self._check(step["expect"], result, f"inject.{kind}")
        return f"inject {kind}"

    def close(self) -> None:
        for stream in self.streams.values():
            try:
                stream.session.close()
            except Exception:  # noqa: BLE001 - closing must not replace the verdict
                pass


def _max_gap(samples: List[PlantSample], task: str) -> Observed:
    """Longest stretch, in ms, between scan-witness increments over the samples."""
    seen = [s for s in samples if task in s.witness]
    if len(seen) < 2:
        return MISSING
    gap, since, last = 0.0, seen[0].t_ms, seen[0].witness[task]
    for s in seen[1:]:
        if s.witness[task] != last:
            gap, since, last = max(gap, s.t_ms - since), s.t_ms, s.witness[task]
    return max(gap, seen[-1].t_ms - since)


def _load(path: Path) -> JsonObj:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(doc, dict)
        or doc.get("format") != "openplc-debug-oracle"
        or doc.get("major") != 1
    ):
        raise Blocked(f"{path}: not an openplc-debug-oracle 1.x file")
    return cast(JsonObj, doc)


def run_scenario(
    path: Path, endpoint: Endpoint, plant: Plant, clock: Clock, build: BuildOutputs
) -> RunResult:
    """Run one oracle. Never raises: every outcome is a verdict."""
    scenario = path.stem.replace(".oracle", "")
    result = RunResult(
        f"{path.parent.name}/{scenario}", [], Verdict("BLOCKED", None, "not started")
    )
    try:
        doc = _load(path)
        result.scenario = str(doc.get("scenario", result.scenario))
        result.criteria = [str(c) for c in cast(List[Json], doc.get("criteria", []))]
        run = _Run(doc, _marker_table(path.parent), endpoint, plant, clock, build)
    except Exception as exc:  # noqa: BLE001 - a broken oracle is a verdict, not a crash
        result.verdict = Verdict("BLOCKED", None, f"cannot load oracle: {exc}")
        return result
    index: Optional[int] = None
    try:
        run.start()
        for index, raw in enumerate(cast(List[JsonObj], doc["steps"])):
            action = next((a for a in ACTIONS if a in raw), "?")
            t0 = clock.now_ms()
            try:
                detail = run.step(raw)
            except (Diverge, Blocked) as exc:
                status = "DIVERGE" if isinstance(exc, Diverge) else "BLOCKED"
                result.trace.append(
                    StepTrace(index, action, status, exc.reason, clock.now_ms() - t0)
                )
                result.verdict = Verdict(status, index, f"step {index} ({action}): {exc.reason}")
                return result
            run.step_end_ms = clock.now_ms()
            result.trace.append(StepTrace(index, action, "match", detail, run.step_end_ms - t0))
        result.verdict = Verdict("PASS", None, f"all {len(result.trace)} steps matched")
    except Blocked as b:
        where = "setup" if index is None else f"step {index}"
        result.verdict = Verdict("BLOCKED", index, f"{where}: {b.reason}")
    except Exception as exc:  # noqa: BLE001 - the runner never crashes without a verdict
        tb = traceback.extract_tb(exc.__traceback__)[-1]
        result.verdict = Verdict(
            "BLOCKED",
            index,
            f"harness error: {type(exc).__name__}: {exc} ({Path(tb.filename).name}:{tb.lineno})",
        )
    finally:
        run.close()
    return result


# ---------------------------------------------------------------- reporting


def format_trace(result: RunResult) -> str:
    lines = [f"{result.verdict.status} {result.scenario}: {result.verdict.reason}"]
    for t in result.trace:
        lines.append(f"  [{t.index:3}] {t.action:<8} {t.status:<8} {t.ms:8.0f}ms  {t.detail}")
    return "\n".join(lines)


def results_lines(result: RunResult, ran_on: str, commit: str) -> List[JsonObj]:
    """One status/README.md `runs/*.jsonl` row per criterion the scenario discharges."""
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    return [
        {
            "criterion": c,
            "verdict": result.verdict.status,
            "test": f"{TEST_ID}::{result.scenario}",
            "ran_on": ran_on,
            "commit": commit,
            "repo": "openplc-runtime",
            "timestamp": stamp,
            "evidence": result.verdict.reason[:300],
        }
        for c in result.criteria
    ]


def append_results(path: Path, rows: Iterable[JsonObj]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def _commit() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _oracles(targets: List[str]) -> List[Path]:
    paths: List[Path] = []
    for t in targets:
        p = Path(t)
        paths.extend(sorted(p.rglob("*.oracle.json")) if p.is_dir() else [p])
    return paths


def main(argv: Optional[List[str]] = None) -> int:
    from socketio_endpoint import SocketIOEndpoint  # noqa: PLC0415 - keeps imports of the tests light

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("oracles", nargs="*", default=[str(CORPUS)])
    ap.add_argument(
        "--endpoint", default=os.environ.get("OPENPLC_DEBUG_URL", "https://127.0.0.1:8443")
    )
    ap.add_argument("--token", default=os.environ.get("OPENPLC_DEBUG_TOKEN"))
    ap.add_argument(
        "--insecure", action="store_true", help="accept the runtime's self-signed certificate"
    )
    ap.add_argument(
        "--build-dir",
        type=Path,
        default=HERE.parents[2] / "build" / "debug-corpus",
        help="build_all.sh's output directory (ADR-002 D1 files per corpus program)",
    )
    ap.add_argument(
        "--results", type=Path, help="append status/runs JSON lines here (default: dry run)"
    )
    ap.add_argument(
        "--trace", action="store_true", help="print the per-step trace of every scenario"
    )
    opts = ap.parse_args(argv)

    endpoint = SocketIOEndpoint(opts.endpoint, token=opts.token, insecure=opts.insecure)
    build: BuildOutputs = (
        build_views.DirectoryBuild(opts.build_dir)
        if opts.build_dir.is_dir()
        else UnavailableBuild()
    )
    ran_on = f"{endpoint.description} from {platform.system().lower()}-{platform.machine()}"
    commit = _commit()
    counts: Dict[str, int] = {"PASS": 0, "DIVERGE": 0, "BLOCKED": 0}
    errors = 0
    rows: List[JsonObj] = []
    for path in _oracles(opts.oracles):
        result = run_scenario(path, endpoint, UnavailablePlant(), WallClock(), build)
        counts[result.verdict.status] += 1
        errors += int("harness error" in result.verdict.reason)
        print(
            format_trace(result)
            if opts.trace
            else f"{result.verdict.status:<8} {result.scenario}: {result.verdict.reason}"
        )
        rows.extend(results_lines(result, ran_on, commit))
    if opts.results is not None:
        append_results(opts.results, rows)
        print(f"results: appended {len(rows)} lines to {opts.results}")
    else:
        print(f"results: dry run, {len(rows)} lines not written (pass --results PATH)")
    total = sum(counts.values())
    print(
        f"summary: {total} scenarios  PASS {counts['PASS']}  DIVERGE {counts['DIVERGE']}  "
        f"BLOCKED {counts['BLOCKED']}  harness errors {errors}"
    )
    return 2 if errors else (1 if counts["DIVERGE"] else 0)


if __name__ == "__main__":
    sys.exit(main())
