"""The real debug endpoint: ADR-003 D1 Socket.IO namespace /api/debug-session (one event name
`msg`, D2 envelope), ADR-005 D1/D2 attach by `initialize` with the JWT, D4 heartbeat every P1, and
ADR-003 D9 status from GET /api/status.

The python-socketio client is imported only when a session opens, so a host without it reports
BLOCKED instead of failing to import. A port that does not answer is BLOCKED before that.

See also:
  tests/debug/harness/interfaces.py - the Endpoint/Session protocols implemented here.
  tests/debug/harness/runner.py - the only caller.
  webserver/debug_websocket.py - the legacy /api/debug namespace and its JWT checks.
"""

from __future__ import annotations

import json
import queue
import socket
import ssl
import threading
import urllib.error
import urllib.request
from typing import Dict, List, Optional, cast
from urllib.parse import urlparse

from interfaces import Blocked, Json, JsonObj

NAMESPACE = "/api/debug-session"
RESPONSE_TIMEOUT_S = 5.0
# `restart` stops the program and starts it again before it answers (F037), which is the
# program's lifecycle rather than a control request and can outlast any control deadline.
RESPONSE_TIMEOUTS_S = {"restart": 30.0}


def _obj(value: Json) -> JsonObj:
    """The object in `value`, or an empty one: a body that is not an object says nothing."""
    return cast(JsonObj, value) if isinstance(value, dict) else {}


class SocketIOEndpoint:
    def __init__(
        self,
        url: str,
        token: Optional[str],
        insecure: bool,
        user_tokens: Optional[Dict[str, str]] = None,
        deployed_tasks: Optional[Dict[str, float]] = None,
        deployed_params: Optional[Dict[str, float]] = None,
        deployed_build: Optional[Dict[str, str]] = None,
        deployed_halt_policy: Optional[str] = None,
    ) -> None:
        parsed = urlparse(url)
        # The task intervals (ms) of the program the caller deployed (T-VER-TASKDEPLOY): a
        # scenario's setup.tasks is met when it names these intervals, and refused otherwise.
        self.deployed_tasks = {k.casefold(): v for k, v in (deployed_tasks or {}).items()}
        # The gateway's heartbeat params the caller deployed (T-VER-PARAMDEPLOY), as the
        # oracle's setup.params names them (`P1_s`/`P2_s`, seconds).
        self.deployed_params = dict(deployed_params or {})
        # The build the caller deployed (T-VER-BUILDDEPLOY), read back out of the deployed
        # debug-info.json: `probes` = "on"|"off", `origins` = how many origin records the
        # transpiler's source map put in it. A scenario's setup.build is met when it names
        # these, and refused otherwise.
        self.deployed_build = dict(deployed_build or {})
        # The halt policy the core reported it loaded (T-VER-HLTDEPLOY), as `kind:count` from
        # its own startup log ([halt] halt policy: safe (4 configured output(s))). "none" when
        # no policy was deployed, which is what the runtime loads when the file is absent.
        self.deployed_halt_policy = deployed_halt_policy or "none"
        # Named test users' JWTs (`~token: "user:<name>"`): permission, lock and hand-over
        # scenarios need several identities, and the harness cannot mint them.
        self.user_tokens = dict(user_tokens or {})
        self.url = url
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.token = token
        self.insecure = insecure
        # `setup.startHalted` states that the scenario needs a program that has not run when
        # the harness attaches. The driver deploys it (plc_main --start-halted) and prepare()
        # refuses the scenario when it is not in place; the handshake is where the runtime says
        # whether it can serve it at all.
        self.requires_start_halted = False
        # Set by prepare() when the scenario's setup.params names the deployed heartbeat params.
        self.requires_params = False
        # Set by prepare() when the scenario's setup.build names the deployed build, and to the
        # probes capability that must be true at the handshake (`None` when it names neither).
        self.requires_probes: Optional[bool] = None
        # Set by prepare() from setup.haltMode: the mode the session must be in before step 0.
        # `haltMode` needs no deploy step — it is a request, sent the moment the session is up.
        self.halt_mode: Optional[str] = None
        self.description = f"endpoint {self.host}:{self.port}"

    def _probe(self) -> None:
        try:
            with socket.create_connection((self.host, self.port), timeout=2.0):
                pass
        except OSError as exc:
            raise Blocked(
                f"no debug endpoint at {self.host}:{self.port} ({exc.strerror or exc})"
            ) from exc

    def _tasks_deployed(self, tasks: Json) -> bool:
        """True when the deployed program runs every task setup.tasks names at its interval."""
        if not isinstance(tasks, dict) or not self.deployed_tasks:
            return False
        for name, spec in tasks.items():
            want = spec.get("intervalMs") if isinstance(spec, dict) else None
            if self.deployed_tasks.get(str(name).casefold()) != want:
                return False
        return True

    def _params_deployed(self, params: Json) -> bool:
        """True when the heartbeat params deployed are the ones setup.params states.

        The P1/P2 a session runs against is the gateway's own, and it reports them in the
        handshake; the driver deploys the oracles' setup.params (T-VER-PARAMDEPLOY) and this
        says whether the declaration matches what was deployed, so a scenario cannot be judged
        against a P2 it did not ask for.
        """
        if not isinstance(params, dict) or not self.deployed_params:
            return False
        for key, want in params.items():
            if key not in ("P1_s", "P2_s"):
                return False
            got = self.deployed_params.get(key)
            if got is None or float(got) != float(want):
                return False
        return True

    def _build_deployed(self, build: Json) -> Optional[str]:
        """True when the deployed build is the one setup.build names, else what is wrong.

        `probes` is the probe-off build (A019) and `path` is how the ST reached the compiler:
        `editor` transpiles LD/FBD and carries an origin map into debug-info.json (A024),
        `cli` compiles ST as written and carries none. The count is the evidence, read out of
        the deployed debug-info.json, so a build that lost its origins on the way is refused
        rather than judged as if it had them.

        `control` (A127) is refused with its reason rather than assumed: the runtime has no
        debug-control compile-out switch, so there is nothing to deploy and nothing to check.
        """
        if not isinstance(build, dict):
            return "setup build is not an object"
        probes, path = build.get("probes"), build.get("path")
        deployed_probes = self.deployed_build.get("probes")
        if probes is not None:
            want = "on" if probes is True else "off"
            if deployed_probes != want:
                return (
                    f"setup build.probes={json.dumps(probes)} needs a probe-{want} build, and "
                    f"the deployed one is probe-{deployed_probes or 'unknown'}"
                )
        if path == "library":
            return (
                "setup build.path=library: the payload builder has one producer, so a build "
                "linked against a .stlib is not something the harness can deploy"
            )
        if "control" in build:
            return (
                f"setup build.control={json.dumps(build['control'])}: this runtime has no "
                f"debug-control compile-out switch (ADR-005 D6.2 `OPENPLC_DEBUG_CONTROL` is in "
                f"no cmake option and no source), so neither build is one the harness can deploy"
            )
        if path in ("editor", "cli"):
            origins = self.deployed_build.get("origins")
            count = int(origins) if origins not in (None, "") else 0
            if path == "editor" and count == 0:
                return (
                    "setup build.path=editor: the deployed debug-info.json carries no origins, "
                    "so the LD/FBD transpiler's source map did not reach the build"
                )
            if path == "cli" and count != 0:
                return (
                    f"setup build.path=cli needs a build with no origins, and the deployed one "
                    f"carries {count}"
                )
        return None

    def _halt_policy_deployed(self, setup: JsonObj) -> Optional[str]:
        """True when the core's own report of its halt policy is the one setup declares.

        `haltPolicy` is the project's conf/halt_policy.json (the driver ships it in the payload)
        and the core names what it loaded once, in its startup log; `safeValues` are the values
        that file's `safe` list carries, so the count the core reports must be theirs. A BOOL
        safe value is written as the number it stands for, which does not change the count.
        """
        policy, values = setup.get("haltPolicy"), setup.get("safeValues")
        if policy is None and values is not None:
            return (
                "setup safeValues states safe values but no policy: it needs "
                "haltPolicy=safe to be deployed"
            )
        if policy not in ("hold", "safe"):
            return f"setup haltPolicy={json.dumps(policy)} is not a policy this runtime loads"
        if values is not None and policy != "safe":
            return "setup safeValues needs haltPolicy=safe; this states no policy of its own"
        if self.deployed_halt_policy == "none":
            return (
                f"setup haltPolicy={policy} needs the runtime loaded with that policy, and no "
                f"halt policy was deployed (no conf/halt_policy.json in the payload)"
            )
        kind, _, count = self.deployed_halt_policy.partition(":")
        if kind != policy:
            return (
                f"setup haltPolicy={policy}, but the core reports it loaded the {kind} policy"
            )
        if values is not None and count.isdigit() and int(count) != len(values):
            return (
                f"setup safeValues names {len(values)} output(s) and the core reports "
                f"{count} configured"
            )
        return None

    def prepare(self, setup: JsonObj) -> None:
        self._probe()
        # Nothing deploys a key by ignoring it: a scenario that declares something the run does
        # not carry is refused here, because a silent wrong answer is the only thing worse than
        # no answer. Task intervals (setup.tasks), the heartbeat params (setup.params), the
        # build (setup.build) and the halt policy (setup.haltPolicy/setup.safeValues) are
        # deployed by the caller (tests/debug/plant/session_e2e.sh) and checked here; the halt
        # mode (setup.haltMode) is a session request this endpoint sends itself, checked at the
        # handshake, where the runtime says whether it can serve it at all.
        self.requires_start_halted = setup.get("startHalted") is True
        self.requires_params = self._params_deployed(setup["params"]) if "params" in setup else False
        self.requires_probes = None
        if "build" in setup and isinstance(setup["build"], dict):
            probes = setup["build"].get("probes")
            self.requires_probes = probes if isinstance(probes, bool) else None
        self.halt_mode = (
            setup.get("haltMode") if setup.get("haltMode") in ("allStop", "nonStop") else None
        )
        needs = []
        for key, value in (
            ("tasks", setup.get("tasks")),
            ("params", setup.get("params")),
            ("build", setup.get("build")),
            ("haltPolicy", setup.get("haltPolicy", setup.get("safeValues"))),
            ("haltMode", setup.get("haltMode")),
        ):
            if key not in setup and not (key == "haltPolicy" and "safeValues" in setup):
                continue
            if key == "tasks":
                bad = None if self._tasks_deployed(value) else ""
            elif key == "params":
                bad = None if self.requires_params else ""
            elif key == "build":
                bad = self._build_deployed(value)
            elif key == "haltPolicy":
                bad = self._halt_policy_deployed(setup)
            else:
                bad = (
                    None
                    if self.halt_mode
                    else f"haltMode={json.dumps(value)} is not a halt mode (allStop, nonStop)"
                )
            if bad is not None:
                needs.append(
                    bad
                    or f"setup {key} needs a deploy step the harness does not have yet"
                )
        if needs:
            raise Blocked("; ".join(needs))
        if self.requires_start_halted:
            self._require_start_halted()

    def _require_start_halted(self) -> None:
        """Refuse `setup.startHalted` unless the program really is halted, having not run.

        The harness cannot deploy this one. `restart {startHalted}` parks every task at the entry
        trap, but a session that restarts and then detaches gives the halt back: ADR-005 D4.6
        resumes on a clean detach, and only a controller lost past P2 keeps it, as a safe-stop. So
        a deploy on a session of the harness's own cannot survive to the scenario's first step.

        It is deployed by the driver instead — `plc_main --start-halted`, from the oracles' own
        setup.startHalted (tests/debug/plant/session_e2e.sh) — and checked here, so a run that did
        not ask for it says so instead of counting the scans that ran before the harness attached.
        """
        status = self.status()
        tasks = status.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise Blocked(
                f"setup startHalted, but the runtime reports no task: state {status.get('state')!r}"
            )
        running = [
            f"{t.get('task', t.get('name', '?'))} is {t.get('state', 'unknown')}"
            for t in cast(List[Json], tasks)
            if isinstance(t, dict) and t.get("state") == "running"
        ]
        if running:
            raise Blocked(
                "setup startHalted, but the program is running ("
                + "; ".join(running)
                + "). Start the runtime with --start-halted: a session that restarts startHalted "
                "and detaches resumes the program, so the harness cannot deploy this itself "
                "(tests/debug/plant/session_e2e.sh deploys it from the oracles)."
            )

    def open_session(self) -> "SocketIOSession":
        self._probe()
        return SocketIOSession(self)

    def status(self) -> JsonObj:
        req = urllib.request.Request(f"https://{self.host}:{self.port}/api/status")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        ctx = ssl.create_default_context()
        if self.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=5.0, context=ctx) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise Blocked(f"GET /api/status failed: {exc}") from exc
        debug = doc.get("debug") if isinstance(doc, dict) else None
        if not isinstance(debug, dict):
            raise Blocked("GET /api/status has no `debug` block (ADR-003 D9 not built)")
        return cast(JsonObj, debug)

    def log(self) -> JsonObj:
        raise Blocked("no log read-back path is defined for the runtime yet")

    def inject(self, kind: str, args: JsonObj) -> Json:
        raise Blocked(f"inject {kind}: fault injection (F154) not built")

    def credential(self, kind: str) -> Json:
        if kind == "none":
            return None
        if kind.startswith("user:"):
            name = kind[len("user:"):]
            if name not in self.user_tokens:
                raise Blocked(f"credential {kind}: no --user-token {name}=JWT given")
            return self.user_tokens[name]
        if kind == "expired":
            raise Blocked("an expired JWT needs the runtime's signing key, which the harness lacks")
        if not self.token:
            raise Blocked(f"credential {kind}: no --token given")
        if kind == "forged":
            # Same header and claims, signature replaced: verification must refuse it.
            head, _, _sig = self.token.rpartition(".")
            return f"{head}.Zm9yZ2Vk"
        return self.token


class SocketIOSession:
    # The core's nonStop capability, from the handshake, and whether setup.haltMode has been put
    # in place (one request per session, however many times it is initialized). Class defaults, so
    # a subclass that swaps the transport and does not call super().__init__()
    # (tests/debug/wire/run_harness.py's WssSession) still has them.
    non_stop = False
    halt_mode_deployed = False

    def __init__(self, endpoint: SocketIOEndpoint) -> None:
        try:
            import socketio  # noqa: PLC0415 - optional at import time, see module doc
        except ImportError as exc:
            raise Blocked(
                "python-socketio client not installed on this host (no dependency added)"
            ) from exc
        self.endpoint = endpoint
        self.seq = 0
        self.lock = threading.Lock()
        self.pending: Dict[int, "queue.Queue[JsonObj]"] = {}
        self.inbox: "queue.Queue[JsonObj]" = queue.Queue()
        self.stop = threading.Event()
        self.beat: Optional[threading.Thread] = None
        self.sio = socketio.Client(ssl_verify=not endpoint.insecure, reconnection=False)
        self.sio.on("msg", self._on_msg, namespace=NAMESPACE)
        try:
            self.sio.connect(
                endpoint.url,
                namespaces=[NAMESPACE],
                transports=["websocket"],
                auth={"token": endpoint.token} if endpoint.token else None,
                wait_timeout=5,
            )
        except Exception as exc:  # noqa: BLE001 - socketio raises its own and transport errors
            raise Blocked(f"cannot attach to {NAMESPACE} at {endpoint.url}: {exc}") from exc

    def _on_msg(self, data: Json) -> None:
        msg = json.loads(data) if isinstance(data, str) else data
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "response":
            rseq = msg.get("request_seq")
            with self.lock:
                waiter = self.pending.get(rseq) if isinstance(rseq, int) else None
            if waiter is not None:
                waiter.put(msg)
        elif msg.get("type") == "event":
            self.inbox.put({"event": msg.get("event"), "body": msg.get("body")})

    def request(self, command: str, args: JsonObj) -> JsonObj:
        if command == "initialize" and self.endpoint.token and "token" not in args:
            args = {**args, "token": self.endpoint.token}
        with self.lock:
            self.seq += 1
            seq = self.seq
            waiter: "queue.Queue[JsonObj]" = queue.Queue()
            self.pending[seq] = waiter
        self.sio.emit(
            "msg",
            {"type": "request", "seq": seq, "command": command, "arguments": args},
            namespace=NAMESPACE,
        )
        timeout_s = RESPONSE_TIMEOUTS_S.get(command, RESPONSE_TIMEOUT_S)
        try:
            resp = waiter.get(timeout=timeout_s)
        except queue.Empty:
            resp = {
                "success": False,
                "error": {"code": "TIMEOUT", "message": f"no response in {timeout_s:g} s"},
            }
        finally:
            with self.lock:
                self.pending.pop(seq, None)
        if command == "initialize" and resp.get("success") is True:
            self._check_capabilities(resp)
            self._deploy_halt_mode()
            if self.beat is None:
                self._start_heartbeat(resp)
        return resp

    def _check_capabilities(self, resp: JsonObj) -> None:
        """A scenario whose setup asks for `startHalted` needs a runtime that serves it, one
        whose setup.params names P1/P2 needs a gateway running at them, one whose setup.build
        names the probe build needs a runtime that advertises it, and one that asks for non-stop
        needs a core that serves non-stop.

        The refusal belongs here, at the handshake that declares the capability, and not at
        setup: the runtime's own advertisement is the evidence, so a scenario that cannot run
        here says which runtime would run it."""
        if self.endpoint.requires_params:
            self._check_params(resp)
        caps = _obj(_obj(resp.get("body")).get("capabilities"))
        self.non_stop = caps.get("nonStop") is True
        if self.endpoint.requires_probes is not None:
            want = self.endpoint.requires_probes
            got = caps.get("probes") is True
            if got is not want:
                raise Blocked(
                    f"setup build.probes={json.dumps(want)}: this runtime "
                    f"{'advertises' if got else 'does not advertise'} the probes capability, so "
                    f"it is not the build the scenario was written for"
                )
        if self.endpoint.halt_mode == "nonStop" and caps.get("nonStop") is not True:
            raise Blocked(
                "setup haltMode=nonStop: this runtime does not advertise the nonStop capability, "
                "so its core halts every task (all-stop only) and no session of it can be non-stop"
            )
        if self.endpoint.requires_start_halted and caps.get("startHalted") is not True:
            raise Blocked(
                "setup startHalted: this runtime does not advertise the startHalted "
                "capability, so it cannot restart the program halted at its first statement"
            )

    def _deploy_halt_mode(self) -> None:
        """Put the session in the halt mode the scenario declared, once it is up (ADR-004).

        `setup.haltMode` is a session setting, not a boot one, so this is a request rather than
        a file the driver writes: nothing in the runtime can be in the wrong mode before the
        session exists. It is sent between `initialize` and step 0, so no oracle step sees it.

        all-stop is the one mode that may need no request: a core that does not advertise
        `nonStop` cannot be anything but all-stop, which is the handshake's own evidence that
        the declared mode is what a session of this runtime gets. A core that *can* be non-stop
        is asked, and its refusal is a disagreement.
        """
        mode = self.endpoint.halt_mode
        if mode is None or self.halt_mode_deployed:
            return
        self.halt_mode_deployed = True
        if mode == "allStop" and not self.non_stop:
            return
        resp = self.request("setHaltMode", {"mode": mode})
        if resp.get("success") is not True:
            error = _obj(resp.get("error"))
            raise Blocked(
                f"setup haltMode={mode}: the runtime refused setHaltMode "
                f"({error.get('code', 'no code')}: {error.get('message', 'no message')})"
            )

    def _check_params(self, resp: JsonObj) -> None:
        """The gateway reports the P1/P2 it runs the heartbeat and the safe-stop timer on."""
        body = resp.get("body")
        params = body.get("params") if isinstance(body, dict) else None
        if not isinstance(params, dict):
            raise Blocked("setup params: this runtime reports no heartbeat params")
        want = self.endpoint.deployed_params
        for key, name in (("P1_s", "P1"), ("P2_s", "P2")):
            if key not in want:
                continue
            got = params.get(name)
            if not isinstance(got, (int, float)) or float(got) != float(want[key]):
                raise Blocked(
                    f"setup params: {name} is deployed at {want[key]:g} s but the runtime runs "
                    f"at {got!r}, so the scenario is not the one it was written for"
                )

    def _start_heartbeat(self, resp: JsonObj) -> None:
        # ADR-005 D4.3: an idle controller sends `heartbeat` every P1, or P2 expiry safe-stops a halt.
        body = resp.get("body")
        params = body.get("params") if isinstance(body, dict) else None
        p1 = params.get("P1") if isinstance(params, dict) else None
        period = float(p1) if isinstance(p1, (int, float)) and not isinstance(p1, bool) else 2.0

        def beat() -> None:
            while not self.stop.wait(period):
                with self.lock:
                    self.seq += 1
                    seq = self.seq
                try:
                    self.sio.emit(
                        "msg",
                        {"type": "request", "seq": seq, "command": "heartbeat", "arguments": {}},
                        namespace=NAMESPACE,
                    )
                except Exception:  # noqa: BLE001 - a lost transport shows up in the next request
                    return

        self.beat = threading.Thread(target=beat, name="debug-heartbeat", daemon=True)
        self.beat.start()

    def events(self) -> List[JsonObj]:
        out: List[JsonObj] = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        self.stop.set()
        self.sio.disconnect()

    def kill(self) -> None:
        # ASSUMED: python-engineio's abort drops the socket with no Socket.IO disconnect packet,
        # which is what the runtime sees of a SIGKILLed client.
        self.stop.set()
        self.sio.eio.disconnect(abort=True)
