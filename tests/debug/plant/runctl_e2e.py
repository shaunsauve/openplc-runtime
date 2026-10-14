"""Run control against a real plc_main: a probed corpus:plant-io, the reference plant, the core link.

The core's part of A031 A032 A033 A034 A047, judged by the reference plant's output log (sampled
at every cycle_end) and by the core link's events, never by the runtime's own scan number.
Breakpoints are set by probe ID, resolved from the generated probed clone: symbolic resolution is
the gateway's job (ADR-003 D1.3), not the core's.

Usage (inside the runtime container, plc_main running with the plant; see runctl_inner.sh):
  python3 runctl_e2e.py <payload dir> <plant socket>
Exit 0 when every check passes. Each line is PASS or FAIL with the criterion it serves.

See also:
  tests/debug/plant/runctl_e2e.sh - builds the runtime and the corpus program, runs this.
  tests/debug/harness/reference_plant.py - the plant adapter used here.
  docs/DEBUG_CORE_LINK.md - the core-link messages sent here.
"""

from __future__ import annotations

import glob
import json
import os
import random
import re
import socket
import struct
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "harness"))
from reference_plant import ReferencePlant  # noqa: E402

LINK = "/run/runtime/plc_debug.socket"
CORE = "/run/runtime/plc_runtime.socket"
INTERVAL_MS = 100.0  # corpus:plant-io's Main task
FAILS: List[str] = []


def check(crit: str, ok: bool, what: str) -> None:
    print(f"{'PASS' if ok else 'FAIL'} {crit}: {what}", flush=True)
    if not ok:
        FAILS.append(f"{crit}: {what}")


def core(cmd: str) -> str:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(CORE)
    s.sendall(cmd.encode() + b"\n")
    out = s.recv(65536).decode().strip()
    s.close()
    return out


class Link:
    """Core-link client: u32 BE length + JSON envelope; events collected with receipt times."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(LINK)
        self.msgs: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.seq = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _recv(self, n: int) -> bytes:
        b = b""
        while len(b) < n:
            c = self.sock.recv(n - len(b))
            if not c:
                raise EOFError
            b += c
        return b

    def _read(self) -> None:
        try:
            while True:
                (n,) = struct.unpack(">I", self._recv(4))
                m = json.loads(self._recv(n))
                m["_t"] = time.monotonic() * 1000.0
                with self.lock:
                    self.msgs.append(m)
        except (EOFError, OSError):
            pass

    def call(self, command: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.seq += 1
        seq = self.seq
        body = json.dumps({"type": "request", "seq": seq, "command": command,
                           "arguments": args or {}}).encode()
        self.sock.sendall(struct.pack(">I", len(body)) + body)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.lock:
                for m in self.msgs:
                    if m.get("request_seq") == seq:
                        return m
            time.sleep(0.001)
        raise TimeoutError(command)

    def mark(self) -> int:
        with self.lock:
            return len(self.msgs)

    def event(self, frm: int, name: str, pred: Callable[[Dict[str, Any]], bool] = lambda b: True,
              timeout: float = 2.0) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                for m in self.msgs[frm:]:
                    if m.get("event") == name and pred(m.get("body", {})):
                        return m
            time.sleep(0.002)
        return None

    def events(self, frm: int, name: str) -> List[Dict[str, Any]]:
        with self.lock:
            return [m for m in self.msgs[frm:] if m.get("event") == name]


def resolve_probes(payload: str) -> Dict[str, int]:
    """Statement text -> probe ID, from the probed clone STruCpp generated."""
    probes: Dict[str, int] = {}
    for f in glob.glob(os.path.join(payload, "pou_*.cpp")):
        src = open(f).read()
        clone = src[src.find("strucpp:probed-clone begin"):]
        for m in re.finditer(r"STRUCPP_PROBE\((\d+)\);\s*\{?\s*([^\n;{]+)", clone):
            probes.setdefault(m.group(2).strip(), int(m.group(1)))
    return probes


def stopped(reason: str, probe: Optional[int] = None) -> Callable[[Dict[str, Any]], bool]:
    return lambda b: b.get("reason") == reason and (probe is None or b.get("probe") == probe)


def main() -> int:
    payload, plant_sock = sys.argv[1], sys.argv[2]
    probes = resolve_probes(payload)
    K = probes["COUNTER = COUNTER + 1"]
    L = probes["X_SEEN = X * 2"]
    plant = ReferencePlant(plant_sock)
    plant.set_inputs({"%IX0.2": False})
    link = Link()

    def w_now() -> int:
        return plant.sample().witness["Main"]

    def plant_since(t_ms: float) -> List[Any]:
        return plant.history(t_ms)

    # ---- handshake: the declared limit and the probes (A032) ----
    r = link.call("initialize", {"protocol": "1.0"})
    check("A032", r["success"] and r["body"]["maxBreakpoints"] >= 256,
          f"handshake declares maxBreakpoints {r['body'].get('maxBreakpoints')} (P3 >= 256)")
    check("A032", r["body"]["probes"] == max(probes.values()) + 1,
          f"handshake declares {r['body']['probes']} probes")
    link.call("attach", {"by": "e2e"})
    m0 = link.mark()
    time.sleep(0.8)
    check("A047", len(link.events(m0, "beat")) >= 3, f"{len(link.events(m0, 'beat'))} beats in 800 ms")

    # ---- A034 / A031(a): stop before K, step, set L while halted ----
    m = link.mark()
    link.call("setBreakpoint", {"probe": K})
    st = link.event(m, "stopped", stopped("breakpoint", K))
    check("A034", st is not None, f"stop at K (probe {K})")
    rs = link.event(m, "runState", lambda b: b.get("state") == "HALTED_AT_BP", 1.0)
    check("A047", rs is not None and st is not None and rs["_t"] - st["_t"] <= 500,
          "runState HALTED_AT_BP pushed within 500 ms of the stop")
    s = link.call("status")["body"]
    t0 = s.get("tasks", [{}])[0]
    check("A047", s.get("state") == "HALTED_AT_BP" and str(t0.get("name", "")).lower() == "main" and
          t0.get("probe") == K and "since" in t0 and s.get("by") == "e2e",
          f"status names who, where, task and since: {json.dumps(s)[:200]}")
    t_halt = time.monotonic() * 1000.0
    time.sleep(2.2)  # past the 10-period stuck limit (1 s): the watchdog must stand down
    halt = plant_since(t_halt)
    wit = {x.witness["Main"] for x in halt}
    cnt = {x.outputs.get("%QD1") for x in halt}
    vts = {x.vt_ms["Main"] for x in halt}
    check("A034", len(halt) >= 15 and len(wit) == 1 and len(cnt) == 1,
          f"no plant output change in {len(halt)} frames while stopped before K")
    check("ADR-004 D4", len(vts) == 1, "the task's virtual clock did not move while parked")
    check("ADR-004 D2", core("STATUS") == "STATUS:RUNNING", "the PLC is still RUNNING after a 2.2 s park")
    w_k, c_k = wit.pop(), cnt.pop()

    m = link.mark()
    link.call("next", {"task": 0})
    check("A034", link.event(m, "stopped", stopped("step", K + 1)) is not None, "step over K lands on the next statement")
    link.call("setBreakpoint", {"probe": L})
    m = link.mark()
    t_c = time.monotonic() * 1000.0
    link.call("continue")
    check("A031", link.event(m, "stopped", stopped("breakpoint", L)) is not None, "stop at L after continue")
    time.sleep(0.3)
    check("A031", {x.witness["Main"] for x in plant_since(t_c)} <= {w_k},
          "plant witness unchanged between the K and L stops (the same scan)")
    m = link.mark()
    t_c = time.monotonic() * 1000.0
    link.call("continue")
    check("A034", link.event(m, "stopped", stopped("breakpoint", K)) is not None, "next scan stops at K")
    time.sleep(0.3)
    after = plant_since(t_c)
    check("A034", after and after[-1].outputs.get("%QD1") == c_k + 1 and after[-1].witness["Main"] == w_k + 1,
          f"the plant saw the stepped scan's counter {c_k + 1} once it completed "
          f"(saw {after[-1].outputs.get('%QD1') if after else None})")
    link.call("clearAllBreakpoints")
    link.call("continue")
    time.sleep(0.3)

    # ---- A031(b): set L while the scan dwells before it ----
    plant.set_inputs({"%IX0.2": True})
    time.sleep(1.0)  # dwell scans of 300 ms in a 100 ms task
    w0 = w_now()
    while w_now() == w0:  # a scan just completed; the next one starts within a tick and dwells
        time.sleep(0.002)
    time.sleep(0.15)
    m = link.mark()
    r = link.call("setBreakpoint", {"probe": L})
    w = w_now()
    t_resp = time.monotonic() * 1000.0
    st = link.event(m, "stopped", stopped("breakpoint", L))
    check("A031", r["success"] and st is not None, "stop at L armed inside the dwell")
    plant.set_inputs({"%IX0.2": False})
    time.sleep(0.6)
    halt = plant_since(t_resp)
    check("A031", halt and all(x.witness["Main"] == w for x in halt),
          f"plant witness held at w={w} for the whole halt ({sorted({x.witness['Main'] for x in halt})})")
    link.call("clearBreakpoint", {"probe": L})
    m = link.mark()
    t_c = time.monotonic() * 1000.0
    link.call("continue")
    time.sleep(10.5)  # 100 scans
    seq = [w]
    for x in plant_since(t_c):
        if x.witness["Main"] != seq[-1]:
            seq.append(x.witness["Main"])
    gaps = sum(1 for a, b in zip(seq, seq[1:]) if b != a + 1)
    check("A031", len(seq) > 1 and seq[1] == w + 1 and gaps == 0 and len(seq) >= 95,
          f"after the resume w+1 once, then +1 per scan ({len(seq) - 1} scans, {gaps} gaps)")
    check("A031", not link.events(m, "stopped"), f"{len(link.events(m, 'stopped'))} stops after the clear")

    # ---- A032: a probe past the program is refused; the corpus has fewer than P3 probes ----
    r = link.call("setBreakpoint", {"probe": 100000})
    check("A032", not r["success"] and r["error"]["code"] == "INVALID_LOCATION", "a probe past the program is refused")

    # ---- A033: pause 20 times at random points, idle and inside a dwell ----
    rng = random.Random(7)
    worst = 0.0
    for i in range(20):
        dwell = i >= 10
        plant.set_inputs({"%IX0.2": dwell})
        time.sleep(0.6 + rng.random() * 0.3)
        m = link.mark()
        t_req = time.monotonic() * 1000.0
        link.call("pause")
        ok = link.event(m, "stopped", stopped("pause")) is not None
        if i == 0:
            check("A047", link.event(m, "runState", lambda b: b.get("state") == "PAUSED", 1.0) is not None,
                  "runState PAUSED pushed")
        time.sleep(0.5)
        hist = plant_since(t_req - 1000)
        last = hist[-1].witness["Main"]
        first_last = next(x.t_ms for x in hist if x.witness["Main"] == last)
        worst = max(worst, first_last - t_req)
        if not ok:
            check("A033", False, f"pause {i} produced no stopped event")
        link.call("continue")
    plant.set_inputs({"%IX0.2": False})
    check("A033", worst <= INTERVAL_MS + 100, f"plant froze at most {worst:.0f} ms after a pause request (bound {INTERVAL_MS + 100:.0f})")
    time.sleep(0.5)
    w0 = w_now()
    time.sleep(0.5)
    check("A033", w_now() > w0, "the plant's scan count advances after continue")

    # ---- A033 stop: the program stops, the session does not ----
    m = link.mark()
    link.call("stop")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and core("STATUS") != "STATUS:STOPPED":
        time.sleep(0.05)
    check("A033", core("STATUS") == "STATUS:STOPPED", "STATUS:STOPPED after stop")
    w0 = w_now()
    time.sleep(0.5)
    check("A033", plant.sample().witness["Main"] == w0, "plant count frozen after stop")
    s = link.call("status")["body"]
    check("A033", s.get("attached") is True and not link.events(m, "sessionEnded"),
          "the session is still attached; no session-ended event")
    check("A047", link.event(m, "runState", lambda b: b.get("state") == "STOPPED", 1.0) is not None,
          "runState STOPPED pushed")

    # ---- stop while parked lands STOPPED, not ERROR (ADR-001 D7.4) ----
    core("START")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and core("STATUS") != "STATUS:RUNNING":
        time.sleep(0.1)
    time.sleep(0.5)
    m = link.mark()
    link.call("setBreakpoint", {"probe": K})
    check("ADR-001 D7.4", link.event(m, "stopped", stopped("breakpoint", K), 3.0) is not None,
          "breakpoint kept across the stop stops the restarted program")
    link.call("stop")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and core("STATUS") not in ("STATUS:STOPPED", "STATUS:ERROR"):
        time.sleep(0.05)
    check("ADR-001 D7.4", core("STATUS") == "STATUS:STOPPED", f"stop while parked lands {core('STATUS')}")

    print(f"runctl e2e: {len(FAILS)} failed" if FAILS else "runctl e2e: all checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
