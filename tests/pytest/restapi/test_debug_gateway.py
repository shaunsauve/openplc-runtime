# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""The debug session gateway (/api/debug-session) over a real WSS port, with the fake core on the
internal link behind it. Criteria A064-A068, A070, A080 and A117-A120 (docs/ACCEPTANCE.md of the
orchestration root); what the fake core cannot stand in for is named in each test.

JOURNEY: wire-control (tests/debug/wire/wire-control.oracle.json) is the end-to-end beat; the
tests below cover what one oracle run cannot: concurrency, the refusal catalogue against the
spec, the advertisement against every request, and the legacy namespace beside it.

See also:
  webserver/debug_gateway/gateway.py - the gateway under test.
  tests/debug/wire/fake_core.py - the core agent stand-in.
  tests/debug/wire/link_spec.py - the core-link spec checker (docs/DEBUG_CORE_LINK.md).
  tests/debug/wire/wire_server.py - the served stack (TLS, both namespaces).
  docs/DEBUG_PROTOCOL.md - the spec the catalogue and the request list are checked against.
"""

from __future__ import annotations

import json
import os
import queue
import re
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import pytest

pytest.importorskip("flask_socketio", reason="flask_socketio not installed (no venv)")
pytest.importorskip("simple_websocket", reason="simple_websocket not installed (no venv)")

ROOT = Path(__file__).resolve().parents[3]
WIRE = ROOT / "tests" / "debug" / "wire"
sys.path.insert(0, str(WIRE))

from conftest import create_user, token_for  # noqa: E402
import link_spec  # noqa: E402
from fake_core import FULL_CAPS, FakeCore, Json, JsonObj, debug_info  # noqa: E402
from wire_server import WireServer, as_obj  # noqa: E402
from wss_client import WssSio  # noqa: E402

from webserver import restapi  # noqa: E402
from webserver.debug_gateway import protocol  # noqa: E402
from webserver.debug_gateway.session_guard import ConfigError, HeartbeatParams, parse_params  # noqa: E402
from webserver.version import RUNTIME_VERSION  # noqa: E402

NS = "/api/debug-session"
SPEC = ROOT / "docs" / "DEBUG_PROTOCOL.md"


class Client:
    """One /api/debug-session client: requests correlated by request_seq, events queued."""

    def __init__(self, url: str, token: Optional[str] = None) -> None:
        self.sio = WssSio(ssl_verify=False)
        self.sio.on("msg", self._on_msg, namespace=NS)
        self.sio.connect(url, namespaces=[NS])
        self.token = token
        self.seq = 0
        self.lock = threading.Lock()
        self.responses: Dict[int, "queue.Queue[JsonObj]"] = {}
        self.arrival: List[int] = []
        self.events: "queue.Queue[JsonObj]" = queue.Queue()
        # Every message both ways, for the spec check (A084).
        self.frames: List[JsonObj] = []
        self.sent: List[Tuple[str, JsonObj]] = []
        self.command_of: Dict[int, str] = {}

    def _on_msg(self, data: Json) -> None:
        msg = as_obj(json.loads(data) if isinstance(data, str) else data)
        with self.lock:
            self.frames.append(msg)
        if msg.get("type") == "response":
            rseq = msg.get("request_seq")
            if rseq is None:
                rseq = -1  # a message whose seq could not be read
            if isinstance(rseq, int):
                with self.lock:
                    self.arrival.append(rseq)
                    q = self.responses.setdefault(rseq, queue.Queue())
                q.put(msg)
        elif msg.get("type") == "event":
            self.events.put(msg)

    def send(self, command: str, args: Optional[JsonObj] = None, seq: Optional[int] = None) -> int:
        with self.lock:
            self.seq += 1
            use = self.seq if seq is None else seq
            self.responses.setdefault(use, queue.Queue())
            self.command_of[use] = command
            self.sent.append((command, dict(args or {})))
        self.sio.emit(
            "msg", {"type": "request", "seq": use, "command": command, "arguments": args or {}}, NS
        )
        return use

    def wait(self, seq: int, timeout: float = 5.0) -> JsonObj:
        return self.responses[seq].get(timeout=timeout)

    def request(self, command: str, args: Optional[JsonObj] = None) -> JsonObj:
        return self.wait(self.send(command, args))

    def raw(self, data: Json) -> JsonObj:
        """Send any payload on `msg`; the reply to an unreadable one has request_seq null."""
        with self.lock:
            q = self.responses.setdefault(-1, queue.Queue())
        self.sio.emit("msg", data, NS)
        return q.get(timeout=5.0)

    def initialize(self, **args: Json) -> JsonObj:
        full: JsonObj = {"protocol": "1.0", "role": "controller"}
        if self.token is not None:
            full["token"] = self.token
        full.update(args)
        return self.request("initialize", full)

    def event(self, name: str, where: Optional[JsonObj] = None, timeout: float = 2.0) -> JsonObj:
        """The next `name` event whose body has every key/value of `where`."""
        deadline = time.monotonic() + timeout
        while True:
            ev = self.events.get(timeout=max(deadline - time.monotonic(), 0.001))
            body = as_obj(ev.get("body"))
            if ev.get("event") == name and all(body.get(k) == v for k, v in (where or {}).items()):
                return body

    def drain(self) -> List[str]:
        names: List[str] = []
        while not self.events.empty():
            names.append(str(self.events.get().get("event")))
        return names

    def close(self) -> None:
        self.sio.disconnect()

    def kill(self) -> None:
        """Drop the transport with no Socket.IO disconnect, as a SIGKILLed client does."""
        self.sio.eio.disconnect(abort=True)


@pytest.fixture()
def tokens(client, admin_token) -> Dict[str, str]:
    create_user(client, "tech", "tech-pass", token=admin_token, role="user")
    return {"admin": admin_token, "user": token_for(client, "tech", "tech-pass")}


@pytest.fixture()
def serve(app, tmp_path) -> Iterator[Callable[..., WireServer]]:
    """start(**WireServer options) serves the stack; every server started is closed after."""
    servers: List[WireServer] = []

    def start(**kwargs: JsonObj) -> WireServer:
        workdir = tmp_path / f"s{len(servers)}"
        workdir.mkdir()
        server = WireServer(app, workdir, **kwargs)  # type: ignore[arg-type]
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


def _spec_section(title: str) -> str:
    text = SPEC.read_text()
    part = text.split("## Session protocol v1", 1)[1]
    return part.split(f"### {title}", 1)[1].split("\n### ", 1)[0]


def _spec_codes() -> List[str]:
    return re.findall(r"^\| `([A-Z_]+)` \|", _spec_section("Error codes"), re.M)


def _spec_requests() -> List[Tuple[str, str]]:
    rows = re.findall(r"^\| `(\w+)` \| ([^|]*) \|", _spec_section("Requests"), re.M)
    return [(name, caps.strip()) for name, caps in rows]


def _is_sentence(message: Json) -> bool:
    return isinstance(message, str) and len(message.split()) >= 3 and message.rstrip()[-1] in ".!?"


def _refusal(resp: JsonObj) -> Tuple[str, str]:
    assert resp["success"] is False, resp
    err = as_obj(resp["error"])
    return str(err["code"]), str(err.get("message"))


# --------------------------------------------------------------------------- A064, A065, A067,
# A068 (loopback), A070: the harness's own runner and session over a real WSS connection.


def test_wire_control_oracle_passes_through_the_harness(serve, admin_token):
    from run_harness import run  # noqa: PLC0415

    server = serve()
    [result] = run([WIRE / "wire-control.oracle.json"], server.url, admin_token, insecure=True)
    import runner  # noqa: PLC0415

    assert result.verdict.status == "PASS", runner.format_trace(result)


# --------------------------------------------------------------------------- A064 correlation


def test_concurrent_requests_get_their_own_correlated_responses(serve, admin_token):
    """Requests fired back to back are answered out of order (the core delays `status`); each
    response carries its own request's seq and its own command's body. NO-FOLD: concurrency."""
    server = serve(delays={"status": 0.3})
    c = Client(server.url, admin_token)
    assert c.initialize()["success"] is True
    sent = {
        c.send("status"): "status",
        c.send("setExceptionBreakpoints", {"filters": ["divByZero"]}): "setExceptionBreakpoints",
        c.send("version"): "version",
        c.send("heartbeat"): "heartbeat",
    }
    got = {command: c.wait(seq) for seq, command in sent.items()}
    for seq, command in sent.items():
        resp = got[command]
        assert resp["request_seq"] == seq and resp["command"] == command, resp
        assert resp["success"] is True, resp
    assert "state" in as_obj(got["status"]["body"])
    assert as_obj(got["setExceptionBreakpoints"]["body"])["filters"] == ["divByZero"]
    assert "protocol" in as_obj(got["version"]["body"])
    status_seq = next(seq for seq, cmd in sent.items() if cmd == "status")
    assert c.arrival[-1] == status_seq, f"status was delayed, so it arrives last: {c.arrival}"
    seqs = [as_obj(json.loads(p.split(",", 1)[1])[1])["seq"] for p in c.sio.sent]
    assert len(set(seqs)) == len(seqs)
    c.close()


# --------------------------------------------------------------------------- A065 push


def test_a_passive_observer_is_pushed_every_state_change(serve, admin_token):
    """The observer sends nothing after initialize; the controller pauses, resumes and detaches,
    and the program prints: the observer receives each as a pushed event."""
    server = serve()
    watcher, ctl = Client(server.url, admin_token), Client(server.url, admin_token)
    assert watcher.initialize(role="observer")["success"] is True
    sent_at_attach = len(watcher.sio.sent)
    assert ctl.initialize()["success"] is True
    assert watcher.event("control", {"controller": "admin"})["observers"] == 1
    assert ctl.request("pause")["success"] is True
    assert watcher.event("stopped")["reason"] == "pause"
    assert watcher.event("runState")["state"] == "PAUSED"
    assert ctl.request("continue")["success"] is True
    assert watcher.event("continued")["allThreadsContinued"] is True
    assert watcher.event("runState")["state"] == "RUNNING"
    server.core.inject_print("hello from Main")
    out = watcher.event("output")
    assert (out["category"], out["text"], out["task"], out["taskId"]) == ("print", "hello from Main", "Main", 0)
    assert ctl.request("detach")["success"] is True
    assert watcher.event("control", {"controller": None})["observers"] == 1
    assert len(watcher.sio.sent) == sent_at_attach, "the passive client sent a request"
    watcher.close()
    ctl.close()


# --------------------------------------------------------------------------- A066 refusals


def test_the_catalogue_and_the_spec_name_the_same_codes():
    """Each code the gateway can return is in the spec, and each spec code is in the catalogue,
    with a plain-language default message."""
    catalogue = {c.value for c in protocol.ErrorCode}
    assert set(_spec_codes()) == catalogue
    for code in protocol.ErrorCode:
        assert _is_sentence(protocol.DEFAULT_MESSAGES[code]), code


REFUSALS = [
    "PROTOCOL_VERSION",
    "UNAUTHORISED",
    "BAD_REQUEST",
    "LIMIT_EXCEEDED",
    "UNSUPPORTED",
    "NOT_CONTROLLER",
    "BUSY",
    "LOCKED",
    "FORBIDDEN",
    "INVALID_LOCATION",
    "NO_CODE",
    "NOT_HALTED",
    "NOT_RUNNING",
    "STALE_BUILD",
    "UNSUPPORTED_DEBUG_INFO",
    "NO_PROBES",
    "SESSION_ENDED",
]


def _provoke(code: str, server: WireServer, tokens: Dict[str, str]) -> JsonObj:
    url, admin = server.url, tokens["admin"]
    c = Client(url, admin)
    if code == "PROTOCOL_VERSION":
        return c.initialize(protocol="2.0")
    if code == "UNAUTHORISED":
        head, _, _ = admin.rpartition(".")
        return c.initialize(token=f"{head}.Zm9yZ2Vk")
    if code == "BAD_REQUEST":
        return c.request("pause")  # before initialize: out of order
    assert c.initialize(identity=_identity(code))["success"] is True
    if code == "LIMIT_EXCEEDED":
        return c.request("setExceptionBreakpoints", {"filters": ["x" * (2 * 1024 * 1024)]})
    if code == "UNSUPPORTED":
        return c.request("evaluate", {"expression": "x"})
    if code == "NOT_CONTROLLER":
        return Client(url, admin).initialize()  # a second controller
    if code == "BUSY":
        for _ in range(protocol.MAX_OBSERVERS):
            assert Client(url, admin).initialize(role="observer")["success"] is True
        return Client(url, admin).initialize(role="observer")
    if code == "LOCKED":
        assert c.request("setLock", {"on": True, "reason": "A066"})["success"] is True
        resp = c.request("pause")
        assert c.request("setLock", {"on": False})["success"] is True
        return resp
    if code == "FORBIDDEN":
        u = Client(url, tokens["user"])
        assert u.initialize(role="observer")["success"] is True
        return u.request("setLock", {"on": True})
    if code == "INVALID_LOCATION":
        return c.request("setBreakpoints", {"file": "missing.st", "lines": [1]})
    if code == "NO_CODE":
        return c.request("setBreakpoints", {"file": "program.st", "lines": [99]})
    if code == "NOT_HALTED":
        return c.request("next")
    if code == "NOT_RUNNING":
        assert c.request("stop")["success"] is True
        resp = c.request("stop")
        assert c.request("restart", {"kind": "warm"})["success"] is True
        return resp
    if code in ("STALE_BUILD", "UNSUPPORTED_DEBUG_INFO", "NO_PROBES"):
        return c.request("setBreakpoints", {"file": "program.st", "lines": [6]})
    if code == "SESSION_ENDED":
        assert c.request("detach")["success"] is True
        return c.request("pause")
    raise AssertionError(code)


def _identity(code: str) -> JsonObj:
    if code == "STALE_BUILD":
        return {"md5": "f" * 32, "buildId": "f" * 64}
    return {}


def _server_for(code: str, serve) -> WireServer:
    if code == "UNSUPPORTED_DEBUG_INFO":
        return serve(info=debug_info(major=2))
    if code == "NO_PROBES":
        return serve(capabilities={**FULL_CAPS, "probes": False})
    return serve()


@pytest.mark.parametrize("code", REFUSALS)
def test_each_refusal_has_its_code_a_sentence_and_changes_nothing(code, serve, tokens):
    server = _server_for(code, serve)
    before = server.core.witness()
    resp = _provoke(code, server, tokens)
    got, message = _refusal(resp)
    assert got == code, resp
    assert code in _spec_codes()
    assert _is_sentence(message), message
    if code == "PROTOCOL_VERSION":
        assert as_obj(as_obj(resp["error"])["details"]) == {"client": "2.0", "server": "1.0"}
    if code == "BUSY":
        assert as_obj(as_obj(resp["error"])["details"])["reason"] == "observer_limit"
    # The runtime state is unchanged: the task is still running and still scanning.
    assert server.core.state == "RUNNING"
    time.sleep(0.05)
    assert server.core.witness() > before
    changing = {"pause", "setBreakpoints", "stop", "restart", "continue", "next"}
    gateway_refusals = set(REFUSALS) - {"NOT_HALTED", "NOT_RUNNING"}
    if code in gateway_refusals:
        reached = [cmd for cmd, _ in server.core.received if cmd in changing]
        assert reached == [], f"{code} was refused only after reaching the core: {reached}"


def test_a_revoked_token_ends_the_session(serve, client, admin_token):
    """ADR-005 D2.1: control requests re-check revocation; a revoked user's session ends."""
    server = serve()
    c = Client(server.url, admin_token)
    assert c.initialize()["success"] is True
    assert client.post("/api/logout", headers={"Authorization": f"Bearer {admin_token}"}).status_code == 200
    code, _ = _refusal(c.request("pause"))
    assert code == "SESSION_ENDED"
    assert c.event("sessionEnded")["reason"] == "revoked"
    assert not [cmd for cmd, _ in server.core.received if cmd == "pause"]


# --------------------------------------------------------------------------- A117-A120 session


def _expired_token(app, username: str) -> str:
    from datetime import timedelta  # noqa: PLC0415

    from flask_jwt_extended import create_access_token  # noqa: PLC0415

    with app.app_context():
        user = restapi.User.query.filter_by(username=username).one()
        return create_access_token(identity=user, expires_delta=timedelta(seconds=-1))


def test_only_a_logged_in_user_over_tls_attaches(serve, app, tokens):
    """A119: no token, an expired JWT and a forged one are refused UNAUTHORISED and open no core
    session; a plain `user` account controls (MVP rights, R7). A120: the gateway refuses a
    plaintext connection outright, and a plaintext client cannot speak to the TLS port."""
    server = serve()
    head, _, _ = tokens["user"].rpartition(".")
    for bad in (None, _expired_token(app, "tech"), f"{head}.Zm9yZ2Vk"):
        resp = Client(server.url).initialize(**({"token": bad} if bad else {}))
        assert _refusal(resp)[0] == "UNAUTHORISED", (bad, resp)
    assert not [cmd for cmd, _ in server.core.received if cmd == "attach"]
    tech = Client(server.url, tokens["user"])
    assert tech.initialize()["success"] is True
    for command, args, then in (
        ("setBreakpoints", {"file": "program.st", "lines": [6]}, "stopped"),
        ("setBreakpoints", {"file": "program.st", "lines": []}, None),
        ("continue", {}, "continued"),
        ("pause", {}, "stopped"),
        ("continue", {}, "continued"),
        ("status", {}, None),
    ):
        assert tech.request(command, args)["success"] is True, command
        if then is not None:
            tech.event(then)
    assert [a["user"] for cmd, a in server.core.received if cmd == "attach"] == ["tech"]

    plain = serve(tls=False)
    with pytest.raises(ConnectionError):
        Client(plain.url, tokens["admin"])
    with pytest.raises(Exception):  # noqa: B017 - a TLS port answers ws:// with a TLS alert
        Client(server.url.replace("https://", "http://"), tokens["admin"])
    assert plain.core is not None and plain.core.received == []


def test_attach_and_detach_leave_the_program_alone(serve, admin_token, monkeypatch):
    """A117: 20 attach/detach cycles while running neither stop nor restart the program. A118:
    detach while halted with another breakpoint armed resumes the program and clears every
    breakpoint, so no stop follows. ADR-003 D9: while halted, the webserver's own GET
    /api/status handler (webserver/app.py) carries the gateway's `debug` block."""
    server = serve()
    core = server.core
    assert core is not None
    before = core.witness()
    for _ in range(20):
        c = Client(server.url, admin_token)
        assert c.initialize(role="observer")["success"] is True
        assert c.request("detach")["success"] is True
        c.close()
    assert core.state == "RUNNING" and core.witness() > before
    assert not [cmd for cmd, _ in core.received if cmd in ("stop", "restart", "pause", "terminate")]

    c = Client(server.url, admin_token)
    assert c.initialize()["success"] is True
    assert c.request("setBreakpoints", {"file": "program.st", "lines": [4, 7]})["success"] is True
    c.event("stopped", {"reason": "breakpoint"})
    from webserver import app as app_module  # noqa: PLC0415 - starts the runtime manager

    monkeypatch.setattr(app_module.runtime_manager, "status_plc", lambda: "STATUS:RUNNING")
    monkeypatch.setattr(app_module.runtime_manager, "switch_plc", lambda: "SWITCH:RUN")
    monkeypatch.setattr(app_module, "debug_gateway", server.gateway, raising=False)
    debug = as_obj(app_module.handle_status({}).get("debug"))
    assert debug.get("state") == "HALTED_AT_BP", debug
    assert [s.get("role") for s in debug.get("sessions") or []] == ["controller"], debug
    assert as_obj(c.request("detach")["body"])["forces"] == []
    time.sleep(0.05)
    halted_at = core.witness()
    time.sleep(0.1)
    assert core.state == "RUNNING" and core.breakpoints == {} and core.witness() > halted_at


# --------------------------------------------------------------------------- A121, A057, A058,
# A122 (gateway half): P1/P2, loss past P2, resume within it.


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", HeartbeatParams(2, 15)),
        ("# defaults\nlock = off\n", HeartbeatParams(2, 15)),
        ("P1 = 0.5\nP2 = 5\n", HeartbeatParams(0.5, 5)),
        ("P1=10\nP2=30", HeartbeatParams(10, 30)),
        ("P1 = 2\nP2 = 5\n", "below 3 x P1"),
        ("P1 = 0.4", "outside"),
        ("P2 = 601", "outside"),
        ("P2 = soon", "not a number"),
        ("P2", "key = value"),
    ],
)
def test_debug_conf_sets_p1_p2_and_refuses_bad_values(text, expected):
    """ADR-005 D4.2: P1 0.5-30 s, P2 5-600 s, P2 >= 3 x P1, else refused at load. NO-FOLD:
    parser."""
    if isinstance(expected, HeartbeatParams):
        assert parse_params(text) == expected
    else:
        with pytest.raises(ConfigError, match=expected):
            parse_params(text)


P = HeartbeatParams(0.2, 1.0)


def _wait_for(pred: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _detaches(core: FakeCore) -> List[JsonObj]:
    return [a for cmd, a in core.received if cmd == "detach"]


def test_losing_the_controller_past_p2_safe_stops_a_halt_and_detaches_a_run(serve, admin_token):
    """ADR-005 D4.5-D4.6, gateway half of A057/A058/A177: a dropped transport ends nothing
    within P2; past P2 the gateway detaches the session with reason heartbeat_timeout, which the
    core turns into a safe-stop when a task is halted and a plain detach when running; a client
    that comes back with the old resume token is told the session ended and why. A heartbeating
    idle controller outlives P2 several times over (A121)."""
    server = serve(params=P)
    core = server.core
    assert core is not None
    c = Client(server.url, admin_token)
    body = as_obj(c.initialize()["body"])
    assert body["params"] == {"P1": 0.2, "P2": 1}
    token = str(body["resumeToken"])
    for _ in range(int(3 * P.p2_s / P.p1_s)):
        assert c.request("heartbeat")["success"] is True
        time.sleep(P.p1_s)
    assert _detaches(core) == [] and "sessionEnded" not in c.drain()

    assert c.request("setBreakpoints", {"file": "program.st", "lines": [6]})["success"] is True
    c.event("stopped")
    c.kill()
    killed = time.monotonic()
    time.sleep(P.p2_s * 0.7)
    assert core.state == "HALTED" and _detaches(core) == [], "the session ended within P2"
    assert _wait_for(lambda: core.state == "SAFE_STOPPED", P.p2_s)
    lost_after = time.monotonic() - killed
    assert P.p2_s <= lost_after <= P.p2_s + 0.5, lost_after
    assert _detaches(core)[-1]["reason"] == "heartbeat_timeout"
    late = Client(server.url).initialize(resumeToken=token)
    assert _refusal(late)[0] == "SESSION_ENDED"
    assert as_obj(as_obj(late["error"])["details"])["reason"] == "heartbeat_timeout"

    running = serve(params=P)
    assert running.core is not None
    r = Client(running.url, admin_token)
    assert r.initialize()["success"] is True
    r.kill()
    assert _wait_for(lambda: len(_detaches(running.core)) == 1, P.p2_s + 0.5)  # type: ignore[arg-type]
    assert _detaches(running.core)[0]["reason"] == "heartbeat_timeout"
    before = running.core.witness()
    time.sleep(0.05)
    assert running.core.state == "RUNNING" and running.core.witness() > before


def test_a_resume_token_within_p2_keeps_the_session(serve, admin_token):
    """ADR-005 D2.4/D4.5: the resume token alone, within P2, rebinds the same core session (no
    second attach, still halted), rotates, and the spent token is dead. A clean Socket.IO
    disconnect is a leave, not a loss: it detaches at once."""
    server = serve(params=P)
    core = server.core
    assert core is not None
    c = Client(server.url, admin_token)
    token = str(as_obj(c.initialize()["body"])["resumeToken"])
    assert c.request("pause")["success"] is True
    c.event("stopped")
    c.kill()
    time.sleep(P.p2_s / 2)
    back = Client(server.url)
    body = as_obj(back.initialize(resumeToken=token)["body"])
    assert body["resumed"] is True and body["role"] == "controller"
    assert body["resumeToken"] != token
    assert [cmd for cmd, _ in core.received].count("attach") == 1 and core.state == "HALTED"
    for _ in range(int(P.p2_s / P.p1_s) + 2):  # past the old transport's P2, beating
        assert back.request("heartbeat")["success"] is True
        time.sleep(P.p1_s)
    assert back.request("continue")["success"] is True
    assert _refusal(Client(server.url).initialize(resumeToken=token))[0] == "UNAUTHORISED"
    back.close()
    assert _wait_for(lambda: len(_detaches(core)) == 1, 0.3)
    assert _detaches(core)[0].get("reason") == "disconnect"


# --------------------------------------------------------------------------- A087, A084: wire


GARBAGE: List[Json] = [
    '{"type":"request","seq":7,"command":"pau',  # truncated
    "{not json}",
    "",
    "null",
    "[1, 2]",
    "NaN",
    "[" * 100000,  # nested past the JSON parser's recursion limit
    {"type": "request", "seq": True, "command": "status"},
    {"type": "request", "seq": 3.5, "command": "status"},
    {"type": "request", "command": "status"},
    [{"type": "request", "seq": 1, "command": "status"}],
]


def test_malformed_messages_are_refused_and_change_nothing(serve, admin_token):
    """A087, gateway half: each malformed message gets a BAD_REQUEST reply with request_seq null,
    a binary one too; 1,000 garbage messages in a burst neither stall the program nor break the
    session, and a valid request then works. The scan-gap half needs the real core
    (tests/debug/plant/sec_e2e.sh)."""
    server = serve()
    core = server.core
    assert core is not None
    c = Client(server.url, admin_token)
    assert c.initialize()["success"] is True
    for frame in GARBAGE:
        resp = c.raw(frame)
        assert resp["request_seq"] is None and _refusal(resp)[0] == "BAD_REQUEST", (str(frame)[:40], resp)
    resp = server.gateway.handle("no-such-sid", b"\xff\x00binary")  # type: ignore[arg-type]
    assert _refusal(resp)[0] == "BAD_REQUEST", resp
    before = core.witness()
    for i in range(1000):
        c.sio.emit("msg", f"garbage {i}", NS)
    replies = [c.wait(-1) for _ in range(1000)]
    assert {_refusal(r)[0] for r in replies} == {"BAD_REQUEST"}
    assert core.witness() > before and core.state == "RUNNING"
    assert c.request("status")["success"] is True
    assert [cmd for cmd, _ in core.received if cmd not in ("attach", "status")] == []


def test_every_message_on_the_wire_is_in_the_spec_and_back(serve, admin_token):
    """A084 against the fake core: every message of a full session (every served request
    succeeding, every event, failures, an unreadable message, a P2 timeout, a resume) matches
    DEBUG_PROTOCOL.md's sessionspec block, and every request and event the block specifies was
    observed. The real core's half is the frame check in tests/debug/plant/sec_e2e.py.
    NO-FOLD: contract."""
    spec = link_spec.load(SPEC, block="sessionspec")
    server = serve(params=P)
    core = server.core
    assert core is not None
    watcher, c = Client(server.url, admin_token), Client(server.url, admin_token)
    assert watcher.initialize(role="observer")["success"] is True
    assert c.initialize()["success"] is True

    def ok(command: str, args: Optional[JsonObj] = None, then: Optional[str] = None) -> None:
        assert c.request(command, args)["success"] is True, command
        if then is not None:
            c.event(then)

    ok("pause", {}, "stopped")
    for command, args in (("next", {}), ("stepIn", {"task": "Main"}), ("stepOut", {}),
                          ("cycleStep", {}), ("runScans", {"n": 1}),
                          ("runToCursor", {"file": "program.st", "line": 7})):
        ok(command, args, "stopped")
    # Inspection of the halted task (debug-support's half of the wire, beside u4-sec's).
    for command in ("stackTrace", "scopes", "variables"):
        ok(command, VALID_ARGS.get(command, {}))
    ok("setBreakpoints", {"file": "program.st", "lines": [6, 4]})
    ok("continue", {}, "stopped")
    ok("setBreakpoints", {"file": "program.st", "lines": []})
    ok("continue", {}, "continued")
    core.inject_print("hello")
    c.event("output")
    for command in ("status", "version", "heartbeat", "attach", "setLock", "breakpointLocations",
                    "clearAllBreakpoints", "setExceptionBreakpoints", "setHaltMode", "stop",
                    "restart"):
        ok(command, VALID_ARGS.get(command, {}))
    assert _refusal(c.request("next"))[0] in STATE_REFUSALS
    assert c.raw("{not json}")["request_seq"] is None
    assert watcher.event("sessionEnded", timeout=3 * P.p2_s)["reason"] == "heartbeat_timeout"
    ok("detach")
    other = Client(server.url, admin_token)
    token = str(as_obj(other.initialize()["body"])["resumeToken"])
    other.kill()
    back = Client(server.url)
    assert as_obj(back.initialize(resumeToken=token)["body"])["resumed"] is True
    assert back.request("terminate")["success"] is True

    problems: List[str] = []
    seen: Dict[str, int] = {}
    for client in (watcher, c, other, back):
        for command, args in client.sent:
            problems += spec.check_request(command, args)
        for frame in client.frames:
            if frame.get("type") == "response" and frame.get("request_seq") is None:
                problems += spec._check(spec.types["Error"], frame.get("error"), "unreadable")
                continue
            problems += spec.check_frame(frame, client.command_of)  # type: ignore[arg-type]
            if frame.get("type") == "event":
                seen[str(frame.get("event"))] = 1
            elif frame.get("success") is True:
                seen[client.command_of[int(str(frame["request_seq"]))]] = 1
    assert not problems, problems[:10]
    unobserved = (set(spec.requests) | set(spec.events)) - set(seen)
    assert not unobserved, f"specified but never on the wire: {sorted(unobserved)}"


# --------------------------------------------------------------------------- A067 handshake


VALID_ARGS: Dict[str, JsonObj] = {
    "setBreakpoints": {"file": "program.st", "lines": [6]},
    "breakpointLocations": {"file": "program.st", "line": 1},
    "setExceptionBreakpoints": {"filters": ["divByZero"]},
    "runScans": {"n": 1},
    "runToCursor": {"file": "program.st", "line": 7},
    "restart": {"kind": "warm"},
    "setHaltMode": {"mode": "allStop"},
    "setLock": {"on": False},
    "pause": {"task": "Main"},
    "scopes": {"frameId": 0},
    "variables": {"path": "PROG0.witness"},
}
# Refusals that mean "valid, but not in this run state", which an advertised request may answer.
STATE_REFUSALS = {"NOT_HALTED", "NOT_RUNNING"}
LAST = ("terminate", "detach")


@pytest.mark.parametrize(
    "caps",
    [FULL_CAPS, {**FULL_CAPS, "control": False, "modify": False}, {**FULL_CAPS, "cycleStep": False}],
    ids=["full", "compiled-out", "no-cycle-step"],
)
def test_the_handshake_advertises_exactly_what_works(caps, serve, admin_token):
    server = serve(capabilities=caps)
    c = Client(server.url, admin_token)
    body = as_obj(c.initialize()["body"])
    advertised = set(str(r) for r in body["requests"] if isinstance(body["requests"], list))
    flags = as_obj(body["capabilities"])
    spec = _spec_requests()
    assert {name for name, _ in spec} >= advertised
    for name, needs in spec:
        if name in ("initialize",) + LAST:
            continue
        resp = c.request(name, VALID_ARGS.get(name, {}))
        if name in advertised:
            assert resp["success"] is True or _refusal(resp)[0] in STATE_REFUSALS, (name, resp)
        else:
            assert _refusal(resp)[0] == "UNSUPPORTED", (name, resp)
        for flag in filter(None, (n.strip(" `") for n in needs.split(","))):
            if flag != "-" and not flags.get(flag):
                assert name not in advertised, f"{name} needs {flag}, which is not advertised"
    assert ("pause" in advertised) is bool(caps["control"])
    # terminate and detach each end the session, so each runs on a fresh initialize.
    for name in LAST:
        resp = c.request(name)
        assert (resp["success"] is True) is (name in advertised), (name, resp)
        if resp["success"] is not True:
            assert _refusal(resp)[0] == "UNSUPPORTED", (name, resp)
        assert c.initialize()["success"] is True


# --------------------------------------------------------------------------- A070 versions


@pytest.mark.parametrize("major,minor,compiler", [(1, 0, "strucpp 0.9.0"), (1, 7, "strucpp 9.9.9")])
def test_the_version_report_names_the_loaded_build(major, minor, compiler, serve, client, admin_token):
    info = debug_info(major=major, minor=minor)
    as_obj(info["build"])["compiler"] = compiler
    server = serve(info=info)
    c = Client(server.url, admin_token)
    assert c.initialize()["success"] is True
    body = as_obj(c.request("version")["body"])
    assert body["protocol"] == f"{protocol.PROTOCOL_MAJOR}.{protocol.PROTOCOL_MINOR}" == "1.0"
    assert body["compiler"] == compiler
    assert body["debugInfo"] == {"major": major, "minor": minor}
    assert body["runtime"] == client.get("/api/version").get_json()["version"] == RUNTIME_VERSION
    assert body["engineAbi"] == 1
    assert "Protocol version: `1.0`" in SPEC.read_text()


# --------------------------------------------------------------------------- A080 legacy


LEGACY_SCRIPT = ["41 00 00", "42 00 03 01 00 01", "43 00 00", "44 00 02 00 00 00 01", "45 DE AD"]


def test_legacy_pdus_pass_through_unchanged_beside_the_gateway(serve, admin_token):
    """/api/debug forwards each 0x41-0x45 PDU as `DEBUG:<hex>` and returns the core's reply hex,
    byte for byte, with the session namespace live on the same Socket.IO instance. The core's own
    PDU handling (the hash field width) is the real core's half of A080."""
    server = serve()
    session = Client(server.url, admin_token)
    assert session.initialize()["success"] is True
    legacy = WssSio(ssl_verify=False)
    replies: "queue.Queue[Json]" = queue.Queue()
    legacy.on("debug_response", replies.put, namespace="/api/debug")
    legacy.on("connected", lambda _d: None, namespace="/api/debug")
    legacy.connect(server.url, namespaces=["/api/debug"], auth={"token": admin_token})
    for pdu in LEGACY_SCRIPT:
        legacy.emit("debug_command", {"command": pdu}, "/api/debug")
        assert replies.get(timeout=5) == {"success": True, "data": "41 00 01"}
    assert server.legacy.sent == [f"DEBUG:{pdu}\n" for pdu in LEGACY_SCRIPT]
    legacy.disconnect()


# --------------------------------------------------------------------------- the core link


class _RawLink:
    """A bare core-link peer: u32 BE + JSON frames, every frame the core sends kept in order."""

    def __init__(self, path: str) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.frames: List[JsonObj] = []
        self.seq = 0
        self.command_of: Dict[int, str] = {}
        self.cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            while True:
                head = self.sock.recv(4, socket.MSG_WAITALL)
                if len(head) < 4:
                    return
                body = self.sock.recv(struct.unpack(">I", head)[0], socket.MSG_WAITALL)
                with self.cv:
                    self.frames.append(json.loads(body))
                    self.cv.notify_all()
        except OSError:
            return

    def _wait(self, pred: Callable[[JsonObj], bool], start: int = 0, timeout: float = 2.0) -> JsonObj:
        with self.cv:
            assert self.cv.wait_for(
                lambda: any(pred(f) for f in self.frames[start:]), timeout
            ), "no matching frame"
            return next(f for f in self.frames[start:] if pred(f))

    def send(self, msg: JsonObj) -> int:
        self.seq += 1
        msg = {**msg, "seq": self.seq}
        self.command_of[self.seq] = str(msg.get("command"))
        data = json.dumps(msg).encode()
        self.sock.sendall(struct.pack(">I", len(data)) + data)
        return self.seq

    def call(self, command: str, args: JsonObj) -> JsonObj:
        seq = self.send({"type": "request", "command": command, "arguments": args})
        return self._wait(lambda f: f.get("request_seq") == seq)

    def stopped_after(self, mark: int) -> JsonObj:
        return self._wait(lambda f: f.get("event") == "stopped", mark)


def test_the_fake_core_speaks_the_link_spec():
    """docs/DEBUG_CORE_LINK.md is the one link spec: every request in its linkspec block is
    driven against the fake core, and every frame the fake sends is checked against the block,
    so the fake cannot drift from the spec the real core is held to (runctl_e2e.py checks the
    real core's frames with the same checker). The fake also refuses requests outside the spec,
    which holds the gateway's outgoing shapes to it in every test above. NO-FOLD: contract."""
    spec = link_spec.load()
    path = f"/tmp/fcspec-{os.getpid()}.sock"
    core = FakeCore(path)
    link = _RawLink(path)
    try:
        first = link._wait(lambda f: True)
        assert first.get("event") == "hello", f"the first frame is {first}"
        session = str(as_obj(link.call("attach", {"user": "t", "role": "controller"})["body"])["session"])
        s = {"session": session}

        def halt(command: str, args: JsonObj) -> None:
            mark = len(link.frames)
            assert link.call(command, {**s, **args})["success"] is True, command
            link.stopped_after(mark)

        halt("setBreakpoints", {"fileId": 0, "probes": [2, {"probe": 3, "hitCount": 1}, 99]})
        for command, args in [("next", {}), ("stepIn", {"task": "MAIN"}), ("stepOut", {"task": 0}),
                              ("cycleStep", {}), ("runScans", {"n": 1}), ("runToCursor", {"probe": 4})]:
            halt(command, args)
        driven = {"attach", "setBreakpoints", "next", "stepIn", "stepOut", "cycleStep", "runScans",
                  "runToCursor"}
        for command, args in [
            ("stackTrace", s), ("readLeaves", {**s, "leaves": [0, 7]}), ("status", {}), ("beatClient", s), ("setExceptionBreakpoints", {**s, "filters": ["divByZero"]}),
            ("setHaltMode", {**s, "mode": "allStop"}), ("clearAllBreakpoints", s), ("continue", s),
        ]:
            assert link.call(command, args)["success"] is True, command
            driven.add(command)
        halt("pause", {})
        core.inject_print("hello")
        for command, args in [("continue", s), ("stop", s), ("restart", {**s, "kind": "warm"}), ("detach", s)]:
            assert link.call(command, args)["success"] is True, command
            driven.add(command)
        other = str(as_obj(link.call("attach", {"user": "u", "role": "controller"})["body"])["session"])
        assert link.call("terminate", {"session": other})["success"] is True
        driven |= {"pause", "terminate"}
        assert driven == set(spec.requests), f"not driven: {set(spec.requests) - driven}"

        refusals = [
            ({"type": "request", "command": "initialize", "arguments": {}}, "UNSUPPORTED"),
            ({"type": "event", "command": "status"}, "BAD_REQUEST"),
            ({"type": "request", "command": "setBreakpoints",
              "arguments": {"session": session, "breakpoints": [{"probe": 1}]}}, "BAD_REQUEST"),
            ({"type": "request", "command": "pause", "arguments": {"session": "s999"}}, "SESSION_ENDED"),
        ]
        for msg, code in refusals:
            seq = link.send(msg)
            resp = link._wait(lambda f, q=seq: f.get("request_seq") == q)
            assert as_obj(resp.get("error")).get("code") == code, (msg, resp)

        time.sleep(0.1)
        with link.cv:
            frames = list(link.frames)
        problems = [p for f in frames for p in spec.check_frame(f, link.command_of)]
        seen = {str(f.get("event")) for f in frames if f.get("type") == "event"}
        assert not problems, problems[:10]
        assert {"hello", "stopped", "continued", "runState", "output"} <= seen, seen
    finally:
        link.sock.close()
        core.close()
