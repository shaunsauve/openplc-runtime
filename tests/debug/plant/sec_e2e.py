"""Session security, client loss and wire hardening against a real plc_main, through the real
gateway over WSS: the runtime halves of A117-A120 (T-SEC-2), A057/A058/A177 and the P2 rules of
A121 (T-SEC-3, J7 headless), and A082/A087 (T-WIRE-4).

Judged by the reference plant's output log and the core's STATUS, never by the gateway's word.

Usage (inside the runtime container; see sec_inner.sh):
  python3 sec_e2e.py <url> <token file> <plant socket> <P2 seconds> <plc_main log>
Env: N (default 1000) requests per control type for A082.
Exit 0 when every check passes; each line is PASS or FAIL with its criterion.

See also:
  tests/debug/plant/sec_e2e.sh - builds the runtime and the corpus program, runs this.
  tests/debug/wire/wss_client.py - the WSS client.
  tests/debug/wire/link_spec.py - checks every gateway frame against docs/DEBUG_PROTOCOL.md.
"""

from __future__ import annotations

import json
import os
import queue
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE.parent / "wire"))
import link_spec  # noqa: E402
from reference_plant import ReferencePlant  # noqa: E402
from wss_client import WssSio  # noqa: E402

Json = Union[None, bool, int, float, str, List["Json"], Dict[str, "Json"]]
JsonObj = Dict[str, Json]

NS = "/api/debug-session"
CORE = "/run/runtime/plc_runtime.socket"
PROGRAM = HERE.parent / "corpus" / "plant-io" / "program.st"
INTERVAL_MS = 100.0  # corpus:plant-io's Main task
SPEC = link_spec.load(link_spec.SPEC.parent / "DEBUG_PROTOCOL.md", block="sessionspec")
FAILS: List[str] = []
VIOLATIONS: List[str] = []


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


def wait_status(want: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if core("STATUS") == f"STATUS:{want}":
            return True
        time.sleep(0.05)
    return False


def markers() -> Dict[str, int]:
    out: Dict[str, int] = {}
    for n, line in enumerate(PROGRAM.read_text().splitlines(), 1):
        m = re.search(r"\(\*@(\w+)\*\)", line)
        if m:
            out[m.group(1)] = n
    return out


def obj(v: Json) -> JsonObj:
    return v if isinstance(v, dict) else {}


class Client:
    """One /api/debug-session client; every frame it receives is checked against the spec."""

    def __init__(self, url: str, token: Optional[str] = None) -> None:
        self.sio = WssSio(ssl_verify=False)
        self.sio.on("msg", self._on_msg, namespace=NS)
        self.sio.connect(url, namespaces=[NS])
        self.token = token
        self.seq = 0
        self.lock = threading.Lock()
        self.command_of: Dict[int, str] = {}
        self.waiters: Dict[int, "queue.Queue[JsonObj]"] = {}
        self.events: "queue.Queue[JsonObj]" = queue.Queue()
        self.beat_stop = threading.Event()

    def _on_msg(self, data: Json) -> None:
        msg = obj(json.loads(data) if isinstance(data, str) else data)
        with self.lock:
            rseq = msg.get("request_seq")
            if msg.get("type") == "response" and rseq is None:
                problems = SPEC._check(SPEC.types["Error"], msg.get("error"), "unread request")
            else:
                problems = SPEC.check_frame(msg, self.command_of)  # type: ignore[arg-type]
            VIOLATIONS.extend(problems[:3])
            # A reply to a frame with no readable seq has request_seq null (waiter key -1).
            key = rseq if isinstance(rseq, int) else (-1 if msg.get("type") == "response" else None)
            waiter = self.waiters.get(key) if key is not None else None
        if waiter is not None:
            waiter.put(msg)
        elif msg.get("type") == "event":
            self.events.put(msg)

    def send_raw(self, data: Json, seq: Optional[int] = None) -> "queue.Queue[JsonObj]":
        waiter: "queue.Queue[JsonObj]" = queue.Queue()
        if seq is not None:
            with self.lock:
                self.waiters[seq] = waiter
        self.sio.emit("msg", data, NS)
        return waiter

    def request(
        self, command: str, args: Optional[JsonObj] = None, timeout: float = 5.0
    ) -> JsonObj:
        with self.lock:
            self.seq += 1
            seq = self.seq
            self.command_of[seq] = command
        waiter = self.send_raw(
            {"type": "request", "seq": seq, "command": command, "arguments": args or {}}, seq
        )
        try:
            return waiter.get(timeout=timeout)
        except queue.Empty:
            return {"success": False, "error": {"code": "TIMEOUT"}}

    def initialize(self, **args: Json) -> JsonObj:
        full: JsonObj = {"protocol": "1.0", "role": "controller"}
        if self.token is not None:
            full["token"] = self.token
        full.update(args)
        return self.request("initialize", full)

    def heartbeat_every(self, period_s: float) -> None:
        def beat() -> None:
            while not self.beat_stop.wait(period_s):
                try:
                    self.request("heartbeat", timeout=period_s)
                except (ConnectionError, OSError):
                    return

        threading.Thread(target=beat, daemon=True).start()

    def event(self, name: str, timeout: float = 3.0) -> Optional[JsonObj]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ev = self.events.get(timeout=max(deadline - time.monotonic(), 0.001))
            except queue.Empty:
                return None
            if ev.get("event") == name:
                return obj(ev.get("body"))
        return None

    def no_event(self, name: str, for_s: float) -> bool:
        return self.event(name, for_s) is None

    def close(self) -> None:
        self.beat_stop.set()
        self.sio.disconnect()

    def kill(self) -> None:
        self.beat_stop.set()
        self.sio.eio.disconnect(abort=True)


def code_of(resp: JsonObj) -> Optional[str]:
    return None if resp.get("success") is True else str(obj(resp.get("error")).get("code"))


def plc_pid() -> Optional[int]:
    for comm in Path("/proc").glob("[0-9]*/comm"):
        try:
            if comm.read_text().strip() == "plc_main":
                return int(comm.parent.name)
        except OSError:
            continue
    return None


def max_gap_ms(samples: List[Tuple[float, int]]) -> float:
    gap, since, last = 0.0, samples[0][0], samples[0][1]
    for t, w in samples[1:]:
        if w != last:
            gap, since, last = max(gap, t - since), t, w
    return gap


def main() -> int:
    url, token_file, plant_sock, p2_s, plc_log = sys.argv[1:6]
    P2 = float(p2_s)
    token = Path(token_file).read_text().strip()
    expired = Path(f"{token_file}.expired").read_text().strip()
    m = markers()
    plant = ReferencePlant(plant_sock)
    plant.set_inputs({"%IX0.0": True, "%IX0.1": True, "%IW0": 1234, "%ID0": 99999, "%IX0.2": False})
    time.sleep(0.5)

    def now_ms() -> float:
        return time.monotonic() * 1000.0

    def witness_since(t: float) -> List[Tuple[float, int]]:
        return [(s.t_ms, s.witness["Main"]) for s in plant.history(t)]

    # ---- A120: TLS only ----
    try:
        Client(url.replace("https://", "http://"), token)
        plain = "accepted"
    except Exception as exc:  # noqa: BLE001 - any failure to connect is the refusal
        plain = type(exc).__name__
    check("A120", plain != "accepted", f"plaintext ws:// to the TLS port refused ({plain})")

    # ---- A119: only a logged-in user ----
    head, _, _ = token.rpartition(".")
    for name, bad in (("no token", None), ("expired", expired), ("forged", f"{head}.Zm9yZ2Vk")):
        c = Client(url)
        resp = c.initialize(**({"token": bad} if bad else {}))
        check("A119", code_of(resp) == "UNAUTHORISED", f"{name} JWT refused ({code_of(resp)})")
        c.close()

    # ---- A117: 20 attaches while running ----
    pid = plc_pid()
    t0 = now_ms()
    stats0 = core("STATS")
    for _ in range(20):
        c = Client(url, token)
        ok = (
            c.initialize(role="observer").get("success") is True
            and c.request("detach").get("success") is True
        )
        c.close()
        if not ok:
            check("A117", False, "an attach/detach failed")
            break
    time.sleep(0.3)
    w = witness_since(t0)
    back = any(b < a for (_, a), (_, b) in zip(w, w[1:]))
    check(
        "A117",
        len(w) > 5 and not back and max_gap_ms(w) <= INTERVAL_MS + 50,
        f"20 attaches: witness {w[0][1]}->{w[-1][1]}, never back, max gap {max_gap_ms(w):.0f} ms",
    )
    check(
        "A117",
        core("STATUS") == "STATUS:RUNNING" and plc_pid() == pid is not None,
        f"still RUNNING in the same plc_main (pid {pid})",
    )
    ov0 = re.search(r'"overruns?"\s*:\s*(\d+)', stats0)
    ov1 = re.search(r'"overruns?"\s*:\s*(\d+)', core("STATS"))
    check(
        "A117",
        ov0 is None or (ov1 is not None and ov1.group(1) == ov0.group(1)),
        f"overrun counter unchanged ({ov0.group(1) if ov0 else 'n/a'} -> {ov1.group(1) if ov1 else 'n/a'})",
    )

    # ---- A118: detach while halted with another breakpoint armed ----
    c = Client(url, token)
    c.initialize()
    r = c.request("setBreakpoints", {"file": "program.st", "lines": [m["K"], m["L"]]})
    st = c.event("stopped")
    check("A118", r.get("success") is True and st is not None, "halted at K with L armed")
    t_det = now_ms()
    r = c.request("detach")
    check("A118", r.get("success") is True, f"detach answered {code_of(r) or 'success'}")
    c.close()
    nxt = Client(url, token)
    nxt.initialize()
    quiet = nxt.no_event("stopped", 10.5)  # 100 scans of the 100 ms task
    w = witness_since(t_det)
    check(
        "A118",
        quiet and w[-1][1] - w[0][1] >= 95,
        f"resumed, no stop in {w[-1][1] - w[0][1]} scans after detach",
    )
    nxt.close()

    # ---- A087: malformed, oversize, out of order, garbage ----
    c = Client(url, token)
    c.initialize()
    t_bad = now_ms()
    cases: List[Tuple[str, Json, str]] = [
        ("truncated", '{"type":"request","seq":900,"command":"pau', "BAD_REQUEST"),
        ("invalid", "{not json}", "BAD_REQUEST"),
        ("not an object", "[1,2,3]", "BAD_REQUEST"),
        (
            "oversize",
            {
                "type": "request",
                "seq": 901,
                "command": "setExceptionBreakpoints",
                "arguments": {"filters": ["x" * (2 * 1024 * 1024)]},
            },
            "LIMIT_EXCEEDED",
        ),
    ]
    with c.lock:
        c.command_of[901] = "setExceptionBreakpoints"
    for name, frame, want in cases:
        if name == "oversize":
            try:
                got = c.send_raw(frame, 901).get(timeout=10)
            except queue.Empty:
                got = {"success": False, "error": {"code": "TIMEOUT"}}
        else:
            got = _unread_reply(c, frame)
        check("A087", code_of(got) == want, f"{name} refused {code_of(got)}")
    check(
        "A087", code_of(c.request("next")) == "NOT_HALTED", "step while running refused NOT_HALTED"
    )
    fresh = Client(url, token)
    check(
        "A087", code_of(fresh.request("continue")) == "BAD_REQUEST", "resume before attach refused"
    )
    fresh.close()
    replies: List[str] = []
    for i in range(1000):
        replies.append(str(code_of(_unread_reply(c, f"garbage {i} \x00\xff{i}"))))
    check(
        "A087",
        replies.count("BAD_REQUEST") == 1000,
        f"1000 garbage frames refused BAD_REQUEST ({len(set(replies))} distinct codes)",
    )
    time.sleep(0.6)
    w = witness_since(t_bad)
    check(
        "A087",
        max_gap_ms(w) <= INTERVAL_MS + 50,
        f"no scan gap over one interval + jitter while refusing ({max_gap_ms(w):.0f} ms)",
    )
    r = c.request("setBreakpoints", {"file": "program.st", "lines": [m["K"]]})
    check(
        "A087",
        r.get("success") is True and c.event("stopped") is not None,
        "a valid request works afterwards (breakpoint at K stops)",
    )
    c.request("setBreakpoints", {"file": "program.st", "lines": []})
    c.request("continue")

    # ---- A082: control request latency on loopback ----
    n = int(os.environ.get("N", "1000"))
    lat: Dict[str, List[float]] = {
        k: []
        for k in ("pause", "continue", "next", "setBreakpoints", "clearAllBreakpoints", "status")
    }

    def timed(command: str, args: Optional[JsonObj] = None) -> JsonObj:
        t = time.perf_counter()
        resp = c.request(command, args)
        lat[command].append((time.perf_counter() - t) * 1000.0)
        return resp

    for i in range(n):
        timed("pause")
        c.event("stopped")
        timed("next")
        c.event("stopped")
        timed("continue")
        timed("setBreakpoints", {"file": "program.st", "lines": []})
        timed("clearAllBreakpoints")
        timed("status")
    for command, xs in lat.items():
        xs.sort()
        p95 = xs[int(0.95 * (len(xs) - 1))] if xs else float("inf")
        check(
            "A082",
            len(xs) >= n and p95 <= 200.0,
            f"{command}: p95 {p95:.1f} ms over {len(xs)} (loopback, gateway + real core, plant 100 ms task)",
        )
    c.close()

    # ---- A057 / A177: lose the controller while halted ----
    c = Client(url, token)
    body = obj(c.initialize().get("body"))
    p1 = float(obj(body.get("params")).get("P1") or 2)
    check(
        "A121",
        obj(body.get("params")).get("P2") == P2,
        f"handshake reports params {body.get('params')}",
    )
    c.heartbeat_every(p1)
    time.sleep(3 * P2 / 2)
    check(
        "A121",
        core("STATUS") == "STATUS:RUNNING" and c.request("status").get("success") is True,
        f"a heartbeating idle controller outlives P2 ({1.5 * P2:g} s)",
    )
    c.request("setBreakpoints", {"file": "program.st", "lines": [m["K"]]})
    check("A057", c.event("stopped") is not None, "halted at K")
    time.sleep(0.5)
    held = plant.sample()
    t_kill = now_ms()
    c.kill()
    time.sleep(P2 - 1.0)
    check(
        "A057",
        core("STATUS") == "STATUS:RUNNING",
        f"no safe-stop within P2 - 1 s ({core('STATUS')})",
    )
    safe = wait_status("SAFE_STOPPED", 3.0)
    lost_s = (now_ms() - t_kill) / 1000.0
    check(
        "A057",
        safe and P2 <= lost_s + 0.05 and lost_s <= P2 + 1.0 + 0.2,
        f"SAFE_STOPPED {lost_s:.2f} s after the kill (P2 {P2:g} s, bound P2 + 1 s)",
    )
    time.sleep(1.0)
    after = plant.history(t_kill)
    frozen = {s.witness["Main"] for s in after}
    changed = sorted({k for s in after for k, v in s.outputs.items() if held.outputs.get(k) != v})
    check("A057", len(frozen) == 1, f"plant scan count frozen ({sorted(frozen)[:3]})")
    check(
        "A057",
        not changed and any(v not in (0, False) for v in held.outputs.values()),
        f"every output held at its pre-halt value, none zeroed (changed: {changed})",
    )
    del plc_log  # stdout is block-buffered while plc_main runs; sec_inner.sh checks the reason
    t_new = now_ms()
    again = Client(url, token)
    r = again.initialize()
    st = again.request("status")
    time.sleep(1.0)
    after = plant.history(t_new)
    changed = sorted({k for s in after for k, v in s.outputs.items() if held.outputs.get(k) != v})
    check(
        "A177",
        r.get("success") is True and obj(st.get("body")).get("state") == "SAFE_STOPPED",
        f"a new attach succeeds and reads SAFE_STOPPED ({obj(st.get('body')).get('state')})",
    )
    check(
        "A177",
        not changed and core("STATUS") == "STATUS:SAFE_STOPPED",
        f"outputs still held after the new attach and its read (changed: {changed})",
    )
    again.close()

    # ---- A058: lose the controller while running ----
    check(
        "A058",
        core("START_AUTHORISED:e2e") == "START:OK" and wait_status("RUNNING", 15),
        "a granted start leaves SAFE_STOPPED",
    )
    # The start re-initialises the plugins, so the plant restarts its sample numbering.
    time.sleep(1.0)
    plant.stop.set()
    plant = ReferencePlant(plant_sock)
    plant.set_inputs({"%IX0.0": False, "%IX0.1": False})
    time.sleep(0.5)
    c = Client(url, token)
    c.initialize()
    r = c.request("setBreakpoints", {"file": "program.st", "lines": [m["THEN"], m["LOAD_X"]]})
    check(
        "A058",
        r.get("success") is True and c.no_event("stopped", 0.5),
        "breakpoints armed on lines the plant keeps unreached",
    )
    t_kill = now_ms()
    c.kill()
    time.sleep(P2 + 2.0)
    w = witness_since(t_kill)
    check(
        "A058",
        core("STATUS") == "STATUS:RUNNING"
        and max_gap_ms(w) <= INTERVAL_MS + 150
        and w[-1][1] - w[0][1] >= (P2 + 2.0) * 1000 / INTERVAL_MS * 0.9,
        f"kept running through P2 + 2 s ({w[-1][1] - w[0][1]} scans, max gap {max_gap_ms(w):.0f} ms)",
    )
    nxt = Client(url, token)
    check(
        "A058",
        nxt.initialize().get("success") is True,
        "a new controller attaches (the old session is gone)",
    )
    plant.set_inputs({"%IX0.0": True, "%IX0.1": True})
    check(
        "A058", nxt.no_event("stopped", 10.5), "no stop over 100 scans on the old breakpoint lines"
    )
    nxt.close()

    check(
        "A084",
        not VIOLATIONS,
        f"every gateway frame matches docs/DEBUG_PROTOCOL.md sessionspec ({'; '.join(VIOLATIONS[:5]) or 'no violations'})",
    )
    # ---- A057 / R6: the gateway dies while halted; the core's own P2 timer safe-stops ----
    # Last, because it kills the gateway. The timer runs from the core's last beat (ADR-005
    # D4.4), which the heartbeat put at most P1 before the kill.
    gw_pid = int(os.environ.get("GW_PID", "0"))
    c = Client(url, token)
    c.initialize()
    c.heartbeat_every(p1)
    c.request("setBreakpoints", {"file": "program.st", "lines": [m["K"]]})
    check("A057", c.event("stopped") is not None and gw_pid > 0, f"halted at K (gateway pid {gw_pid})")
    time.sleep(0.5)
    held = plant.sample()
    t_kill = now_ms()
    os.kill(gw_pid, 9)
    c.beat_stop.set()
    time.sleep(P2 - p1 - 0.3)
    check(
        "A057",
        core("STATUS") == "STATUS:RUNNING",
        f"gateway killed: no safe-stop within P2 - P1 ({core('STATUS')})",
    )
    safe = wait_status("SAFE_STOPPED", 3.0)
    lost_s = (now_ms() - t_kill) / 1000.0
    check(
        "A057",
        safe and lost_s <= P2 + 1.0 + 0.2,
        f"gateway killed: {'SAFE_STOPPED' if safe else 'still ' + core('STATUS')} {lost_s:.2f} s after "
        f"(P2 {P2:g} s from the last beat, bound P2 + 1 s)",
    )
    time.sleep(1.0)
    after = plant.history(t_kill)
    frozen = {s.witness["Main"] for s in after}
    changed = sorted({k for s in after for k, v in s.outputs.items() if held.outputs.get(k) != v})
    check(
        "A057",
        len(frozen) == 1 and not changed and any(v not in (0, False) for v in held.outputs.values()),
        f"gateway killed: scan frozen and every output held, none zeroed (scans {sorted(frozen)[:3]}, changed: {changed})",
    )

    print(f"sec e2e: {len(FAILS)} failed" if FAILS else "sec e2e: all checks passed", flush=True)
    return 1 if FAILS else 0


def _unread_reply(c: Client, frame: Json) -> JsonObj:
    """Send a frame the gateway cannot read a seq from; its reply has request_seq null."""
    with c.lock:
        waiter: "queue.Queue[JsonObj]" = queue.Queue()
        c.waiters[-1] = waiter
    try:
        c.sio.emit("msg", frame, NS)
        return waiter.get(timeout=5)
    except queue.Empty:
        return {"success": False, "error": {"code": "TIMEOUT"}}
    finally:
        with c.lock:
            c.waiters.pop(-1, None)


if __name__ == "__main__":
    sys.exit(main())
