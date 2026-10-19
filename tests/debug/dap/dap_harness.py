#!/usr/bin/env python3
"""The `dap-harness` driver: it LAUNCHES the DAP adapter as a subprocess and speaks DAP to it.

The property this file exists to establish is negative, and everything else here follows from it:
**nothing below reaches the runtime except through the adapter's stdin/stdout (or its TCP socket).**
There is no import of `webserver/debug_gateway`, no `socketio` import, no `tests/debug/harness/`
import — the driver cannot speak the wire even by accident. It reads two things directly, and both
are measurement rather than a protocol shortcut:

  * the reference plant's own Unix socket, to witness a stop (the plant's scan count is frozen
    while the PLC is halted, and it is the only channel that says so independently of the
    debugger), and
  * the corpus program's own text, to turn an `@MARKER` into the line number DAP wants.

A row produced by this file is therefore evidence about the adapter. A row produced by importing
the gateway would be evidence about the gateway wearing the adapter's name.

  python3 tests/debug/dap/dap_harness.py --endpoint https://127.0.0.1:8443 --token JWT \\
      --insecure --plant /tmp/plant.sock --program tests/debug/corpus/nested-fb-loop/program.st \\
      [--seed steplie|varcache|nocred] [--listen-port 0] [--results PATH] [--ran-on TEXT] \\
      [--only SCENARIO ...]

SCENARIOS
  run-control           A073 — stdio and TCP, breakpoints, run control, and every advertised
                        capability exercised.
  inspect               A074 — halted inside the nested FB: threads, stackTrace, scopes, variables,
                        then the two requests the wire does not serve.
  attach-credentials    A075 — attach by URL and credential, right and wrong.

SEEDING (`DAP_SEED`, read by adapter.py): each value is the defect one criterion's `Red-proof:`
names, injected into the ADAPTER rather than into the runtime, so the criterion can be watched
DIVERGE against a real plc_main.

See also:
  tests/debug/dap/adapter.py       - the adapter this launches.
  tests/debug/plant/dap_e2e.sh     - the driver that boots the runtime and runs this in it.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
ADAPTER = HERE / "adapter.py"
DRIVER = "tests/debug/dap/dap_harness.py"
REPO = HERE.parents[2]

# Every capability the adapter may advertise `true`, and the request that discharges it. The
# harness reads this from the OUTSIDE: it takes the adapter's own `initialize` response, looks up
# each `true` here, and fails if one is not in this table or is never sent. A capability with no
# entry here cannot be advertised without the run going DIVERGE, so "advertised and unimplemented"
# is a defect this file detects rather than one a reader has to spot.
CAPABILITY_DISCHARGED_BY: Dict[str, Tuple[str, ...]] = {
    "supportsConfigurationDoneRequest": ("configurationDone",),
    "supportsTerminateRequest": ("terminate",),
    # `terminate` is one request and one effect; both names name the same evidence.
    "supportTerminateDebuggee": ("terminate",),
    "supportsRestartRequest": ("restart",),
    "supportsBreakpointLocationsRequest": ("breakpointLocations",),
}

# Requests E47.1 names that this runtime's session protocol does not serve. The harness sends each
# one and requires an honest refusal rather than silence, then BLOCKS the criterion: a criterion
# that names a request the wire cannot answer is not satisfied by an adapter that refuses it, and
# not satisfied by silence either. See the Red-proof note in each scenario's module comment.
UNSERVED = {
    "setFunctionBreakpoints": "the wire does not serve it (DEBUG_PROTOCOL.md marks it planned)",
    "evaluate": "the wire does not serve it (DEBUG_PROTOCOL.md marks it planned)",
    "setVariable": "the wire does not serve it (DEBUG_PROTOCOL.md marks it planned)",
    "setExceptionBreakpoints": "no core advertises the `exceptionBreakpoints` capability it "
                               "needs, so the gateway refuses it on every build",
}

MARKER = re.compile(r"\(\*(@[A-Za-z0-9_]+)\*\)")


class Diverged(Exception):
    """An assertion that did not hold. The scenario records it and carries on, so one run reports
    every disagreement rather than only the first."""


class Blocked(Exception):
    """Something the harness cannot get past: no session, no stop, a request the wire will not
    serve. Distinct from Diverged because a blocked measurement is not a failing one."""


class Checks:
    """A scenario's tally.

    A failed assertion STOPS the scenario: past a disagreement the run is not measuring what it
    thinks it is, and everything after it would be a number nobody should read. A refusal BLOCKS
    and does not stop — that is the whole difference between the two, and it is why a scenario with
    an unbuildable request at its end still reports every step before it."""

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.diverge: List[str] = []
        self.blocked: List[str] = []
        self.notes: List[str] = []
        self.sent: List[str] = []
        self._index = 0

    def step(self) -> int:
        self._index += 1
        return self._index

    def ok(self, label: str, condition: bool, detail: str = "") -> None:
        """One assertion. It stops the scenario when it fails, so the last line printed before a
        verdict IS the step that failed — a harness that reports "no stop within 8 s" without
        saying which of six stops it was is a harness that has to be re-run to be understood."""
        n = self.step()
        if not condition:
            raise Diverged("step %d: %s%s" % (n, label, (" — " + detail) if detail else ""))
        print("      %2d. %s" % (n, label), flush=True)

    def eq(self, label: str, got: Any, want: Any) -> None:
        self.ok(label, got == want, "expected %r, observed %r" % (want, got))

    def near(self, label: str, got: float, want: float, tol: float) -> None:
        self.ok(label, abs(float(got) - want) <= tol,
                "expected %r +/- %g, observed %r" % (want, tol, got))

    def note(self, text: str) -> None:
        self.notes.append(text)
        print("    note: %s" % text, flush=True)

    def hold(self, what: str) -> None:
        self.blocked.append(what)

    def record(self, name: str) -> None:
        self.sent.append(name)

    @property
    def verdict(self) -> str:
        if self.diverge:
            return "DIVERGE"
        if self.blocked:
            return "BLOCKED"
        return "PASS"

    def evidence(self) -> str:
        if self.diverge:
            return "step %s" % "; ".join(self.diverge)
        if self.blocked:
            return "not judgeable past here: %s" % "; ".join(self.blocked)
        return "all %d requests driven over the adapter's own protocol, every advertised " \
               "capability exercised, every plant witness read independently" % len(self.sent)


# --------------------------------------------------------------------------
# a DAP client that only knows the protocol
# --------------------------------------------------------------------------


class Dap:
    """Content-Length framing over a byte stream, over a subprocess pipe or a TCP socket.

    It is written out rather than imported because there is no DAP library in this tree and E47.1
    adds no dependency: the framing is a header line and a body, and a client that cannot read a
    frame cannot be evidence about an adapter that writes one.
    """

    def __init__(self, reader: Any, writer: Any, closer: Optional[Any] = None) -> None:
        self.reader, self.writer, self.closer = reader, writer, closer
        self.seq = 0
        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        # Messages read while waiting for something else, put back in arrival order. Dropping them
        # would lose a `stopped` that arrived while a slow `restart` was outstanding, and the
        # scenario would then report "no stop within N s" — a defect that is not one.
        self._held: List[Dict[str, Any]] = []
        self._closed = False
        self.proc: Optional[subprocess.Popen] = None
        self.stop = threading.Event()
        self.pump = threading.Thread(target=self._read_loop, name="dap-client-reader", daemon=True)
        self.pump.start()

    # -- transport

    @classmethod
    def over_stdio(cls, argv: List[str]) -> "Dap":
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)
        client = cls(proc.stdout, proc.stdin, closer=proc)
        client.proc = proc
        threading.Thread(target=client._drain_stderr, name="dap-adapter-stderr",
                         daemon=True).start()
        return client

    @classmethod
    def over_tcp(cls, host: str, port: int, timeout_s: float = 10.0) -> "Dap":
        deadline = time.monotonic() + timeout_s
        last: Optional[BaseException] = None
        while time.monotonic() < deadline:
            try:
                sock = socket.create_connection((host, port), timeout=5.0)
                break
            except OSError as exc:      # the adapter is still binding its socket
                last = exc
                time.sleep(0.1)
        else:
            raise Blocked("no DAP listener on %s:%d (%s)" % (host, port, last))
        sock.settimeout(120.0)
        return cls(sock.makefile("rb", buffering=0), sock.makefile("wb", buffering=0),
                   closer=sock)

    def _drain_stderr(self) -> None:
        # readline() in a loop, not `for line in stderr`: with bufsize=0 the pipe is a raw FileIO,
        # which is not iterable, and a drain that dies on its first line leaves the adapter
        # blocked on a full stderr pipe and looking like an adapter that says nothing.
        assert self.proc is not None and self.proc.stderr is not None
        while True:
            line = self.proc.stderr.readline()
            if not line:
                return
            print("    adapter: %s" % line.decode("utf-8", "replace").rstrip(), flush=True)

    def _read_loop(self) -> None:
        try:
            while not self.stop.is_set():
                raw = self._read_frame()
                if raw is None:
                    self.events.put({"event": "__closed__", "body": {}})
                    return
                # EVERY message is queued, responses included. A reader that kept only events
                # would be a client that can watch a debugger and never ask it anything — and
                # every request in this file would time out against an adapter that answered.
                self.events.put(json.loads(raw.decode("utf-8")))
        except Exception:               # a closed pipe is how a terminated adapter says goodbye
            self.events.put({"event": "__closed__", "body": {}})

    def _read_frame(self) -> Optional[bytes]:
        headers: Dict[str, str] = {}
        while True:
            line = self.reader.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n"):
                break
            name, _, value = line.decode("utf-8", "replace").partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers["content-length"])
        body = b""
        while len(body) < length:
            chunk = self.reader.read(length - len(body))
            if not chunk:
                return None
            body += chunk
        return body

    def _write_frame(self, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.writer.write(b"Content-Length: %d\r\n\r\n" % len(raw))
        self.writer.write(raw)
        self.writer.flush()

    # -- protocol

    def request(self, command: str, arguments: Optional[Dict[str, Any]] = None,
                timeout: float = 60.0) -> Dict[str, Any]:
        """One request and its response. Correlated by `request_seq`, which DAP guarantees; every
        message that is not that response is kept, in arrival order, for whoever waits next."""
        self.seq += 1
        seq = self.seq
        self._write_frame({"seq": seq, "type": "request", "command": command,
                           "arguments": arguments or {}})
        held = self._find(lambda m: m.get("request_seq") == seq)
        if held is not None:
            return held
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self._next(max(0.05, deadline - time.monotonic()))
            if msg.get("request_seq") == seq:
                return msg
            self._keep(msg)
        raise Blocked("no response to %s within %g s" % (command, timeout))

    def wait_event(self, name: str, within_s: float = 5.0) -> Dict[str, Any]:
        held = self._find(lambda m: m.get("event") == name)
        if held is not None:
            return held
        deadline = time.monotonic() + within_s
        while time.monotonic() < deadline:
            msg = self._next(max(0.02, deadline - time.monotonic()))
            if msg.get("event") == name:
                return msg
            self._keep(msg)
        raise Blocked("no %s event within %g s" % (name, within_s))

    def expect_no_event(self, name: str, for_s: float) -> None:
        if self._find(lambda m: m.get("event") == name) is not None:
            raise Diverged("a %s event arrived within %g s of a run that should not stop"
                           % (name, for_s))
        deadline = time.monotonic() + for_s
        while time.monotonic() < deadline:
            try:
                msg = self._next(max(0.02, deadline - time.monotonic()))
            except Blocked:
                return
            if msg.get("event") == name:
                raise Diverged("a %s event arrived within %g s of a run that should not stop"
                               % (name, for_s))
            self._keep(msg)

    def _find(self, predicate) -> Optional[Dict[str, Any]]:
        """The first already-read message `predicate` accepts, removed from the kept list.

        Searching the kept list rather than re-reading it is what stops `wait_event` spinning on
        one message it has already decided not to return: a reader that puts a message back and
        then reads the list again gets the same message forever, and a client like that reports
        "no stop within N s" for a run that stopped on time."""
        for i, msg in enumerate(self._held):
            if predicate(msg):
                return self._held.pop(i)
        return None

    def _next(self, timeout: float) -> Dict[str, Any]:
        try:
            msg = self.events.get(timeout=timeout)
        except queue.Empty:
            raise Blocked("nothing arrived within %g s" % timeout)
        if msg.get("event") == "__closed__":
            raise Blocked("the adapter closed its stream")
        return msg

    def _keep(self, msg: Dict[str, Any]) -> None:
        self._held.append(msg)

    def close(self) -> None:
        # A DAP client disconnects before it goes. The runtime keeps a session — and the single
        # controller slot — for P2 after a transport drops (ADR-005 D4), so an adapter that is
        # merely killed holds the next scenario's attach out for fifteen seconds. Best effort: a
        # client that cannot reach its adapter at all still has to be closable.
        try:
            if self.proc is not None and self.proc.poll() is None:
                self.request("disconnect", {"terminateDebuggee": False}, timeout=5.0)
        except Exception:               # noqa: BLE001 - closing is best-effort
            pass
        if self._closed:
            return
        self._closed = True
        self.stop.set()
        try:
            if self.closer is not None:
                if self.proc is not None:
                    self.proc.stdin.close()
                    self.proc.terminate()
                    self.proc.wait(timeout=10)
                else:
                    self.closer.close()
        except Exception:               # noqa: BLE001 - closing is best-effort
            pass


# --------------------------------------------------------------------------
# the plant: an independent witness, read over its own socket
# --------------------------------------------------------------------------


class Plant:
    """The reference plant plugin's line protocol, read directly.

    `SAMPLES <seq>` answers one JSON line per sample taken since that cursor and then `END`. The
    harness keeps the cursor and remembers every sample, so "the scan count did not move while the
    PLC was halted" is read off the plant rather than inferred from the debugger that halted it."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.sock: Optional[socket.socket] = None
        self.buf = b""
        self.seq = 0
        self.samples: List[Dict[str, Any]] = []
        self.lock = threading.Lock()

    def _line(self) -> Optional[str]:
        assert self.sock is not None
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8")

    def ask(self, command: str) -> List[str]:
        if self.sock is None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(10.0)
            self.sock.connect(self.path)
        self.sock.sendall(command.encode("utf-8") + b"\n")
        lines = [self._line() or ""]
        while lines[-1] != "END":
            lines.append(self._line() or "")
        return lines[:-1]

    def poll(self) -> None:
        fresh = []
        for row in self.ask("SAMPLES %d" % self.seq):
            doc = json.loads(row)
            self.seq = max(self.seq, int(doc["seq"]))
            fresh.append(doc)
        with self.lock:
            self.samples.extend(fresh)

    def witness(self) -> int:
        """The plant's scan count for the one task the corpus programs declare."""
        with self.lock:
            if not self.samples:
                return 0
            w = self.samples[-1].get("witness") or {}
        for name, value in w.items():
            if name.lower() == "main":
                return int(value)
        return int(next(iter(w.values()), 0))

    def output(self, addr: str) -> Any:
        with self.lock:
            outs = (self.samples[-1].get("outputs") or {}) if self.samples else {}
        return outs.get(addr)

    def settle(self, scans: int, within_s: float = 20.0) -> int:
        """Wait until the plant has counted `scans` more scans than it had when called."""
        start = self.witness()
        deadline = time.monotonic() + within_s
        while time.monotonic() < deadline:
            self.poll()
            if self.witness() - start >= scans:
                return self.witness()
            time.sleep(0.02)
        raise Blocked("the plant counted %d scans in %g s, not %d"
                      % (self.witness() - start, within_s, scans))

    def frozen(self, for_s: float) -> bool:
        """True when the scan count did not move across a wait — the witness that the PLC is
        halted. Read twice, once either side of the wait, from the plant's own samples."""
        before = self.witness()
        time.sleep(for_s)
        self.poll()
        return self.witness() == before


def pou_name(raw: Any) -> str:
    """A POU's name for comparison: an IEC identifier is case-insensitive (61131-3 6.1.2), and
    debug-info.json carries the generated C++ symbol (`PIDCTL`) where the source spells `PidCtl`.
    The adapter hands that casing on unchanged; this is the harness comparing the two."""
    return str(raw or "").lower()


def markers(program: Path) -> Dict[str, int]:
    """`@NAME` -> 1-based line, over the corpus program's own text."""
    table: Dict[str, int] = {}
    for n, line in enumerate(program.read_text(encoding="utf-8").splitlines(), 1):
        for name in MARKER.findall(line):
            table[name] = n
    return table


def forged(token: str) -> str:
    """The same header and claims, the signature replaced. Verification must refuse it — which is
    the only kind of "wrong credential" a client that cannot mint a JWT can produce."""
    head, _, _sig = token.rpartition(".")
    return "%s.Zm9yZ2Vk" % head


def scope_holding(chk: Checks, client: Dap, frame_id: int, name: str) -> int:
    """The `variablesReference` of the scope that actually holds `name` — a client picks the
    container it can see the name in, not the first one it is handed."""
    for scope in client.request("scopes", {"frameId": frame_id})["body"].get("scopes") or []:
        ref = scope.get("variablesReference")
        if not ref:
            continue
        listed = client.request("variables", {"variablesReference": ref})["body"].get("variables")
        for var in listed or []:
            if str(var.get("name")).lower() == name.lower():
                return int(ref)
    raise Blocked("no scope of frame %d holds a variable named %s" % (frame_id, name))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --------------------------------------------------------------------------
# what the scenarios need from the outside
# --------------------------------------------------------------------------


class Rig:
    def __init__(self, opts: argparse.Namespace) -> None:
        self.opts = opts
        self.plant = Plant(opts.plant)
        self.mark = markers(Path(opts.program))
        self.sent: Dict[str, bool] = {}
        self._frames_checked = False
        self._adapters = 0

    def adapter_argv(self, extra: Optional[List[str]] = None) -> List[str]:
        argv = [sys.executable, str(ADAPTER), "--endpoint", self.opts.endpoint]
        if self.opts.insecure:
            argv.append("--insecure")
        if self.opts.seed:
            argv += ["--seed", self.opts.seed]
        if self.opts.trace_dir:
            # The adapter's own record of everything it wrote to the client, so a run that ends
            # "no stop within 8 s" can be read rather than re-run.
            self._adapters += 1
            trace_dir = Path(self.opts.trace_dir)
            trace_dir.mkdir(parents=True, exist_ok=True)
            argv += ["--trace", str(trace_dir / ("adapter-%d.jsonl" % self._adapters))]
        argv += extra or []
        return argv

    def line(self, mark: str) -> int:
        n = self.mark.get(mark)
        if n is None:
            raise Blocked("the corpus program has no %s marker" % mark)
        return n

    def open(self, extra: Optional[List[str]] = None,
             token: Optional[str] = None) -> Tuple[Dap, Dict[str, Any]]:
        """Launch the adapter as a subprocess, then `initialize` and `attach` over DAP."""
        argv = self.adapter_argv(extra)
        if token:
            argv += ["--token", token]
        client = Dap.over_stdio(argv)
        caps = client.request("initialize", {"adapterID": "openplc", "linesStartAt1": True,
                                             "columnsStartAt1": True})
        if not caps.get("success"):
            raise Blocked("initialize refused: %s" % caps.get("message"))
        attached = client.request("attach", {"token": self.opts.token})
        if not attached.get("success"):
            client.close()
            raise Blocked("attach refused: %s" % attached.get("message"))
        return client, caps.get("body") or {}

    # -- assertions shared by the scenarios

    def call(self, chk: Checks, client: Dap, command: str,
             arguments: Optional[Dict[str, Any]] = None,
             expect_success: bool = True) -> Dict[str, Any]:
        """Send one request and record it. A request whose response the scenario never looks at
        is a request that can fail silently — and `pause` failing silently is how a scenario ends
        up waiting eight seconds for a stop the runtime was never asked for."""
        chk.record(command)
        resp = client.request(command, arguments)
        if expect_success:
            chk.ok("%s is accepted" % command, resp.get("success") is True,
                   "refused: %s" % (resp.get("message"),))
        return resp

    def check_capabilities(self, chk: Checks, caps: Dict[str, Any]) -> None:
        """Every capability the adapter advertised `true` is one this harness knows how to
        discharge, and no capability is advertised that the adapter has no handler for.

        This is the guard a seeded `--seed steplie` fires: the adapter then advertises
        `supportsStepBack`, which is in neither table, so a run against a real runtime DIVERGEs on
        a capability nobody can exercise."""
        promised = sorted(k for k, v in caps.items() if v is True)
        unknown = [k for k in promised if k not in CAPABILITY_DISCHARGED_BY]
        chk.ok("every advertised capability has a request that discharges it", not unknown,
               "advertised with no handler behind it: %s" % ", ".join(unknown))
        for name in promised:
            chk.note("advertised: %s (discharged by %s)"
                     % (name, ", ".join(CAPABILITY_DISCHARGED_BY[name])))

    def one_thread(self, chk: Checks, client: Dap) -> int:
        body = client.request("threads")["body"]
        threads = body.get("threads") or []
        chk.eq("threads names one task", len(threads), 1)
        chk.eq("the thread is the program's task", threads[0]["name"].lower(), "main")
        chk.ok("the thread id is an integer DAP can carry", isinstance(threads[0]["id"], int),
               "observed %r" % (threads[0]["id"],))
        return int(threads[0]["id"])

    def stop_here(self, chk: Checks, client: Dap, thread: int, reason: str,
                  line: Optional[int] = None, within_s: float = 8.0) -> Dict[str, Any]:
        """The next `stopped`, checked against what the program says. `line` is omitted only where
        the program's next statement is not knowable in advance (a `pause` stops wherever the scan
        happened to be), and then only the reason and the thread are checked."""
        body = client.wait_event("stopped", within_s)["body"]
        chk.eq("the stop's thread is the one `threads` issued", body.get("threadId"), thread)
        chk.eq("the stop's reason", body.get("reason"), reason)
        if line is not None:
            chk.eq("the stop is at the line the program has there", body.get("line"), line)
            chk.eq("the stop's source is the corpus program", body.get("description"), "program.st")
        chk.ok("every task is stopped at an all-stop", body.get("allThreadsStopped") is True,
               "observed %r" % (body.get("allThreadsStopped"),))
        return body

    def frames(self, chk: Checks, client: Dap, thread: int) -> List[Dict[str, Any]]:
        body = self.call(chk, client, "stackTrace", {"threadId": thread})["body"]
        frames = body.get("stackFrames") or []
        chk.ok("the stack has at least one frame", bool(frames))
        chk.eq("totalFrames matches the frames listed", body.get("totalFrames"), len(frames))
        # The two per-frame checks are about how the adapter maps ONE frame, so they run once:
        # repeated on every frame of every stop they bury the step that actually failed.
        if not self._frames_checked and frames:
            self._frames_checked = True
            chk.ok("a frame carries a handle `scopes` can take", isinstance(frames[0].get("id"), int))
            chk.eq("a frame names its source", (frames[0].get("source") or {}).get("name"),
                   "program.st")
        return frames

    def scope_values(self, chk: Checks, client: Dap, frame_id: int) -> Dict[str, Any]:
        """Every scope of a frame, flattened by name. DAP reaches children by handle, so this is
        the walk a client does; a name it cannot find is a name the adapter did not surface."""
        body = client.request("scopes", {"frameId": frame_id})["body"]
        flat: Dict[str, Any] = {}
        for scope in body.get("scopes") or []:
            ref = scope.get("variablesReference")
            chk.ok("scope %r carries a handle" % scope.get("name"), bool(ref))
            if not ref:
                continue
            variables = client.request("variables", {"variablesReference": ref})["body"]
            listed = variables.get("variables") or []
            chk.ok("scope %r lists what it counts" % scope.get("name"), bool(listed),
                   "variables(%s) answered %r" % (ref, variables))
            chk.eq("scope %r counts its namedVariables" % scope.get("name"),
                   scope.get("namedVariables"), len(listed))
            for var in listed:
                flat[str(var.get("name")).lower()] = var
        return flat

    def unserved(self, chk: Checks, client: Dap, request: str, arguments: Dict[str, Any]) -> None:
        """Drive a request the wire does not serve and require an honest refusal.

        The refusal BLOCKS the criterion rather than passing it: the criterion names the request,
        and an adapter that answers it correctly — by saying no — has still not implemented it."""
        resp = client.request(request, arguments)
        chk.record(request)
        chk.ok("%s is refused, not silently dropped" % request, resp.get("success") is False,
               "the adapter answered %r" % (resp.get("body"),))
        err = (resp.get("body") or {}).get("error") or {}
        chk.eq("%s refuses with UNSUPPORTED" % request, err.get("code"), "UNSUPPORTED")
        chk.ok("%s says why, naming the request" % request,
               request in str(resp.get("message")) and bool(resp.get("message")),
               "message was %r" % (resp.get("message"),))
        chk.hold("%s: %s" % (request, UNSERVED[request]))


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------


def run_control(rig: Rig, chk: Checks) -> None:
    """A073 — the DAP adapter on stdio and on TCP: breakpoints, run control, and every capability
    it advertised.

    The step sequence is J1's own (nested-fb-loop/j1-step-sequence): break on the call, step into
    the FB, into the nested FB, into the function, out, out, over. Every stop's line is checked
    against the marker in the corpus program, so "the stop is where the program says" is not the
    adapter agreeing with itself."""
    client, caps = rig.open()
    try:
        rig.check_capabilities(chk, caps)
        thread = rig.one_thread(chk, client)
        rig.plant.settle(2)
        chk.record("initialize, attach, threads")

        # -- breakpoints over stdio
        body = rig.call(chk, client, "setBreakpoints",
                        {"source": {"path": "program.st"},
                         "breakpoints": [{"line": rig.line("@CALL_M1")}]})["body"]
        bps = body.get("breakpoints") or []
        chk.eq("one breakpoint came back", len(bps), 1)
        chk.eq("it is verified", bps[0].get("verified"), True)
        chk.eq("it is on the line the program has there", bps[0].get("line"), rig.line("@CALL_M1"))
        rig.stop_here(chk, client, thread, "breakpoint", line=rig.line("@CALL_M1"), within_s=15.0)
        chk.ok("the plant's scan count is frozen while halted", rig.plant.frozen(1.2))

        frames = rig.frames(chk, client, thread)
        chk.eq("one frame at a call in the program", len(frames), 1)
        chk.eq("the frame is the program's", pou_name(frames[0].get("name")), "cell")
        values = rig.scope_values(chk, client, frames[0]["id"])
        chk.near("`total` is the FOR loop's 1+2+3 of the scan before",
                 float(values["total"]["value"]), 6.0, 1e-6)

        rig.call(chk, client, "setBreakpoints", {"source": {"path": "program.st"}, "breakpoints": []})
        for request, mark, names in (
                ("stepIn", "@MOTOR_FIRST", ["fb_motor", "cell"]),
                ("next", "@MOTOR_IF", ["fb_motor", "cell"]),
                ("next", "@MOTOR_PID", ["fb_motor", "cell"]),
                ("stepIn", "@PID_FIRST", ["pidctl", "fb_motor", "cell"]),
                ("next", "@PID_INTEG", ["pidctl", "fb_motor", "cell"]),
                ("next", "@PID_CALL", ["pidctl", "fb_motor", "cell"]),
                ("stepIn", "@CLAMP_FIRST", ["clamp", "pidctl", "fb_motor", "cell"]),
                ("stepOut", "@PID_AFTER", ["pidctl", "fb_motor", "cell"]),
                ("stepOut", "@MOTOR_AFTER", ["fb_motor", "cell"]),
                ("next", "@CALL_M2", ["cell"])):
            rig.call(chk, client, request, {"threadId": thread})
            rig.stop_here(chk, client, thread, "step", line=rig.line(mark))
            got = [pou_name(f.get("name")) for f in rig.frames(chk, client, thread)]
            chk.eq("the stack at %s is innermost-first" % mark, got, names)
        chk.ok("the plant's scan count is frozen while stepped", rig.plant.frozen(1.2))

        # -- resume, then pause
        resumed = rig.call(chk, client, "continue", {"threadId": thread})
        chk.eq("continue says every thread resumed", resumed["body"].get("allThreadsContinued"), True)
        client.wait_event("continued", 5.0)
        client.expect_no_event("stopped", 1.5)

        rig.call(chk, client, "pause", {"threadId": thread})
        rig.stop_here(chk, client, thread, "pause")
        chk.ok("the plant's scan count is frozen while paused", rig.plant.frozen(1.2))
        rig.call(chk, client, "continue", {"threadId": thread})
        client.wait_event("continued", 5.0)

        # -- breakpointLocations, and the two requests the wire does not serve
        body = rig.call(chk, client, "breakpointLocations",
                        {"source": {"path": "program.st"},
                         "line": rig.line("@MOTOR_PID")})["body"]
        chk.eq("breakpointLocations finds the line that has code",
               [b.get("line") for b in body.get("breakpoints") or []], [rig.line("@MOTOR_PID")])
        rig.unserved(chk, client, "setFunctionBreakpoints", {"breakpoints": [{"name": "PidCtl"}]})
        rig.unserved(chk, client, "setExceptionBreakpoints", {"filters": []})

        # -- restart and configurationDone: the rest of what initialize advertised
        rig.call(chk, client, "restart")
        rig.plant.settle(2)
        chk.note("the program is running again after restart")
        rig.call(chk, client, "configurationDone")
        client.close()
        rig.plant.settle(2)
        chk.note("the program is still scanning after disconnect")

        # -- the same adapter over TCP, and terminate
        port = free_port()
        tcp_adapter = rig.adapter_argv(["--listen", "127.0.0.1:%d" % port])
        server = subprocess.Popen(tcp_adapter, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            tcp = Dap.over_tcp("127.0.0.1", port)
            caps = tcp.request("initialize", {"adapterID": "openplc", "linesStartAt1": True,
                                              "columnsStartAt1": True})
            rig.check_capabilities(chk, caps["body"])
            attached = tcp.request("attach", {"token": rig.opts.token})
            chk.ok("attach over TCP establishes a session", attached["success"],
                   attached.get("message"))
            tcp_thread = rig.one_thread(chk, tcp)
            rig.call(chk, tcp, "setBreakpoints",
                     {"source": {"path": "program.st"},
                      "breakpoints": [{"line": rig.line("@CALL_M1")}]})
            rig.stop_here(chk, tcp, tcp_thread, "breakpoint", line=rig.line("@CALL_M1"))
            chk.ok("the plant's scan count is frozen at a TCP stop", rig.plant.frozen(1.0))
            chk.eq("the TCP stack is the program's frame",
                   [pou_name(f.get("name")) for f in rig.frames(chk, tcp, tcp_thread)], ["cell"])
            rig.call(chk, tcp, "continue", {"threadId": tcp_thread})
            tcp.wait_event("continued", 5.0)
            rig.call(chk, tcp, "terminate")
            tcp.wait_event("terminated", 5.0)
            chk.ok("the PLC stops on terminate", rig.plant.frozen(1.5))
            tcp.close()
        finally:
            server.terminate()
            server.wait(timeout=10)
        chk.record("initialize, attach, setBreakpoints, stackTrace, continue, terminate over TCP")
    finally:
        client.close()


def inspect(rig: Rig, chk: Checks) -> None:
    """A074 — halted inside the nested function block, read through DAP.

    The oracle is the program itself. At the first stop in `PidCtl` the source says `kp` starts at
    0.5, `ki` at 0.1, the caller passes `sp := 50.0` and `pv := motor1.speed`, and `err := sp - pv`
    — so `err` must equal `sp - pv` at every stop, and `pv` must grow between two stops because the
    program ran in between. A `variables` answered from a cache is caught by the second of those,
    which is what the `--seed varcache` red-proof does.

    `evaluate` and `setVariable` are driven last, so everything above is measured even though they
    block the criterion."""
    client, caps = rig.open()
    try:
        rig.check_capabilities(chk, caps)
        thread = rig.one_thread(chk, client)
        rig.plant.settle(2)
        chk.record("initialize, attach, threads")

        rig.call(chk, client, "setBreakpoints",
                {"source": {"path": "program.st"},
                 "breakpoints": [{"line": rig.line("@PID_CALL")}]})
        rig.stop_here(chk, client, thread, "breakpoint", line=rig.line("@PID_CALL"))
        frames = rig.frames(chk, client, thread)
        chk.eq("halted inside the nested FB, innermost-first",
               [pou_name(f.get("name")) for f in frames], ["pidctl", "fb_motor", "cell"])
        first = rig.scope_values(chk, client, frames[0]["id"])
        for name in ("sp", "pv", "kp", "ki", "integ", "err"):
            chk.ok("the innermost frame exposes %s" % name, name in first,
                   "scopes carried %s" % ", ".join(sorted(first)))
        chk.near("sp is the constant the caller passes", float(first["sp"]["value"]), 50.0, 1e-6)
        chk.near("kp is the initialiser in the source", float(first["kp"]["value"]), 0.5, 1e-6)
        # 1e-6, not tighter: the corpus's own oracle reads ki as {"~approx": [0.1, 1e-6]}, and a
        # REAL that has been through a 32-bit slot is not 0.1 to the last bit. The tolerance is
        # the project's, taken from the oracle that already judges this value.
        chk.near("ki is the initialiser in the source", float(first["ki"]["value"]), 0.1, 1e-6)
        err = float(first["err"]["value"])
        chk.near("err is sp - pv, as the source assigns it", err,
                 float(first["sp"]["value"]) - float(first["pv"]["value"]), 1e-6)
        chk.ok("the plant's scan count is frozen while halted", rig.plant.frozen(1.2))
        chk.record("stackTrace, scopes, variables")

        # Run on, and stop in the same statement again: the program's own integrator has moved, so
        # a fresh read and a cached one are no longer the same numbers.
        rig.call(chk, client, "setBreakpoints", {"source": {"path": "program.st"}, "breakpoints": []})
        rig.call(chk, client, "continue", {"threadId": thread})
        client.wait_event("continued", 5.0)
        # The breakpoint is cleared before the resume, and re-armed after the program has run.
        # Resuming with it still armed parks the task on the very probe it is standing on (A038's
        # step-onto-breakpoint), which is a second stop in the same scan and no reading a second
        # time can tell apart from a cached one.
        scans_before = rig.plant.settle(3)
        rig.call(chk, client, "setBreakpoints",
                 {"source": {"path": "program.st"},
                  "breakpoints": [{"line": rig.line("@PID_CALL")}]})
        rig.stop_here(chk, client, thread, "breakpoint", line=rig.line("@PID_CALL"), within_s=15.0)
        again = rig.frames(chk, client, thread)
        chk.eq("the second stop is in the same statement",
               [pou_name(f.get("name")) for f in again], ["pidctl", "fb_motor", "cell"])
        second = rig.scope_values(chk, client, again[0]["id"])
        chk.ok("the program ran between the two stops, on the plant's own count",
               rig.plant.witness() > scans_before,
               "the plant counted %d scans before the resume and %d at the second stop"
               % (scans_before, rig.plant.witness()))
        chk.ok("the second read is of a program that has run since the first",
               float(second["pv"]["value"]) > float(first["pv"]["value"]),
               "pv was %r at the first stop and %r at the second — a value that did not move is a "
               "cached one" % (first["pv"]["value"], second["pv"]["value"]))
        chk.near("err is sp - pv again", float(second["err"]["value"]),
                 float(second["sp"]["value"]) - float(second["pv"]["value"]), 1e-6)
        chk.ok("the plant's scan count is frozen at the second stop too", rig.plant.frozen(1.2))

        # The two requests the wire does not serve. A074's Negative asks for a type-invalid
        # setVariable to be refused with the value unchanged, so it is sent with one.
        rig.unserved(chk, client, "evaluate", {"expression": "err * 2", "frameId": again[0]["id"]})
        before = float(second["err"]["value"])
        rig.unserved(chk, client, "setVariable",
                     {"variablesReference": scope_holding(chk, client, again[0]["id"], "err"),
                      "name": "err", "value": "not a real"})
        after = float(rig.scope_values(chk, client, again[0]["id"])["err"]["value"])
        chk.eq("the refused write left the value unchanged", after, before)

        rig.call(chk, client, "disconnect")
        client.close()
        rig.plant.settle(2)
        chk.note("the program is still scanning after disconnect")
    finally:
        client.close()


def attach_credentials(rig: Rig, chk: Checks) -> None:
    """A075 — attach by URL and credential, without disturbing the program.

    Two adapter processes, both configured with the runtime's own login, so the only thing that
    differs between the two attaches is the credential the client sends. The right one establishes
    a session and the scan count never pauses; a wrong one is refused and the second adapter holds
    no session at all. `--seed nocred` is the red-proof: the adapter then uses its own credential
    whatever the client sent, and the wrong one is accepted."""
    client, caps = rig.open(token=rig.opts.token)
    try:
        rig.check_capabilities(chk, caps)
        thread = rig.one_thread(chk, client)
        before = rig.plant.witness()
        rig.plant.settle(5)
        chk.ok("the plant kept scanning across the attach (%d -> %d scans)"
               % (before, rig.plant.witness()), rig.plant.witness() > before)
        chk.note("attach did not restart the program")
        chk.record("initialize, attach, threads")

        # A second adapter, a wrong credential.
        wrong = forged(rig.opts.token)
        other = Dap.over_stdio(rig.adapter_argv() + ["--token", rig.opts.token])
        try:
            other.request("initialize", {"adapterID": "openplc"})
            resp = other.request("attach", {"token": wrong})
            chk.ok("attach with a wrong credential is refused", resp.get("success") is False,
                   "the adapter answered %r" % (resp.get("body"),))
            err = (resp.get("body") or {}).get("error") or {}
            chk.eq("the refusal is the runtime's own UNAUTHORISED", err.get("code"), "UNAUTHORISED")
            threads = other.request("threads")
            chk.ok("the refused attach established no session", threads.get("success") is False,
                   "threads answered %r" % (threads.get("body"),))
            chk.ok("the refused adapter answers an unknown threadId with a refusal too",
                   other.request("stackTrace", {"threadId": 1}).get("success") is False)
        finally:
            other.close()
        chk.ok("the program was not disturbed by the refused attach",
               rig.plant.settle(3) > 0)

        rig.call(chk, client, "disconnect")
        client.close()
        chk.ok("the program is still scanning after disconnect", rig.plant.settle(2) > 0)
    finally:
        client.close()


SCENARIOS: Dict[str, Any] = {
    "run-control": (run_control, ["A073"]),
    "inspect": (inspect, ["A074"]),
    "attach-credentials": (attach_credentials, ["A075"]),
}


# --------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------


def commit() -> str:
    """`<runtime head> strucpp:<compiler head>`, the pair E13.5(c) asks a row to carry. The run is
    in a container with no .git, so the driver that launched it names both (HARNESS_COMMIT)."""
    named = os.environ.get("HARNESS_COMMIT")
    if named:
        return named
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def rows_for(name: str, chk: Checks, ran_on: str, test_suffix: str) -> List[Dict[str, Any]]:
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    _, criteria = SCENARIOS[name]
    return [{"criterion": c, "verdict": chk.verdict,
             "test": "%s::%s" % (DRIVER, test_suffix),
             "ran_on": ran_on, "commit": commit(), "repo": "openplc-runtime",
             "timestamp": stamp, "evidence": chk.evidence()[:300]}
            for c in criteria]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--endpoint", default="https://127.0.0.1:8443")
    ap.add_argument("--token", required=True, help="a JWT from POST /api/login")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--plant", required=True, metavar="SOCKET",
                    help="the reference plant plugin's Unix socket")
    ap.add_argument("--program", required=True, metavar="PATH",
                    help="the corpus program's .st, for its @MARKER line numbers")
    ap.add_argument("--seed", default="", choices=("", "steplie", "varcache", "nocred"),
                    help="a seeded adapter defect, for a criterion's Red-proof")
    ap.add_argument("--results", type=Path, help="append status/runs JSON lines here")
    ap.add_argument("--ran-on", default="", help="where the run happened, for the rows")
    ap.add_argument("--only", action="append", default=[], metavar="SCENARIO",
                    help="run only these scenarios (default: all)")
    ap.add_argument("--trace-dir", metavar="DIR",
                    help="have every adapter write everything it sent to its client here")
    opts = ap.parse_args(argv)

    rig = Rig(opts)
    names = opts.only or sorted(SCENARIOS)
    suffix = ("redproof/%s/%s" % (opts.seed, ",".join(names))) if opts.seed else ",".join(names)
    rows: List[Dict[str, Any]] = []
    rc = 0
    for name in names:
        fn = SCENARIOS[name][0]
        chk = Checks(name)
        print("== %s" % name, flush=True)
        try:
            fn(rig, chk)
        except (Diverged, Blocked) as exc:
            chk.diverge.append(str(exc)) if isinstance(exc, Diverged) else chk.hold(str(exc))
        except Exception as exc:                       # noqa: BLE001 - a crash is a failed run
            chk.diverge.append("the scenario raised %s: %s" % (type(exc).__name__, exc))
        print("   %s %s: %s" % (chk.verdict, name, chk.evidence()), flush=True)
        rows.extend(rows_for(name, chk, opts.ran_on or "dart %s" % opts.endpoint, suffix))
        if chk.verdict != "PASS":
            rc = 1
    if opts.results is not None:
        opts.results.parent.mkdir(parents=True, exist_ok=True)
        with opts.results.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        print("results: appended %d lines to %s" % (len(rows), opts.results))
    return rc


if __name__ == "__main__":
    sys.exit(main())