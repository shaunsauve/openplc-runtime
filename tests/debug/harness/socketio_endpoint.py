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
import os
import queue
import signal
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, cast
from urllib.parse import urlparse

from interfaces import Blocked, Json, JsonObj
from reference_plant import _LineClient

NAMESPACE = "/api/debug-session"
RESPONSE_TIMEOUT_S = 5.0
# `restart` stops the program and starts it again before it answers (F037), which is the
# program's lifecycle rather than a control request and can outlast any control deadline.
RESPONSE_TIMEOUTS_S = {"restart": 30.0}
CORE_SOCKET = "/run/runtime/plc_runtime.socket"
PLANT_NAME = "reference-plant"
# How long `killRuntime`/`restartRuntime` wait for the runtime to open its sockets again.
RUNTIME_UP_TIMEOUT_S = 30.0
# The processes a scenario may signal or kill, by the name its args name.
RUNTIME_PROCESSES = ("plc_main", "webserver")
WEBERVER_ABSENT = (
    "no process named 'webserver' in this container: the session driver serves the gateway "
    "(tests/debug/wire/serve.py over webserver/debug_gateway/gateway.py), not webserver/app.py, "
    "whose own RuntimeManager and EtherDog would be a second PLC runtime. So there is no "
    "webserver process here to signal — that half of A047 needs a deployment of app.py."
)
# The fault kinds this endpoint drives itself, each with the document that says what it is.
# Every other kind still refuses, by name: a kind that reports success without injecting
# anything is worse than the stub, because it turns a loud BLOCK into a silent wrong answer.
REFUSED = {
    "rest": (
        "the runtime REST API is only reachable through webserver/app.py's run_https() "
        "(webserver/app.py:558), and importing app.py starts its own RuntimeManager, EtherDog "
        "and discovery responder — a second PLC runtime, which this container already has. "
        "The gateway here (tests/debug/wire/serve.py:97) registers a stub GET callback that "
        "answers 200 {\"error\": \"Unknown argument\"} for every command, so an inject that "
        "called it would pass an oracle's status matcher without a start ever happening. "
        "Wiring the product's own handlers behind the gateway is runtime-deployment work "
        "(a second RuntimeManager attached to the running plc_main), not harness work."
    ),
    "upload": (
        "the upload path is webserver/app.py:374 handle_upload_file over "
        "webserver/plcapp_management.py (run_compile, apply_retain_conf, "
        "update_plugin_configurations), reachable only through the same run_https() as `rest`. "
        "A harness that zipped a variant and swapped the build itself would be measuring its "
        "own download, not the runtime's."
    ),
}


def _obj(value: Json) -> JsonObj:
    """The object in `value`, or an empty one: a body that is not an object says nothing."""
    return cast(JsonObj, value) if isinstance(value, dict) else {}


# The core command socket speaks one line per request, the same protocol the reference plant's
# socket does; one line client serves both, so `inject: socket` reuses it rather than growing a
# second implementation of the same thing.
_Line = _LineClient


def _cmdline(entry: Path) -> List[str]:
    raw = (entry / "cmdline").read_bytes()
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _processes(name: str) -> List[int]:
    """Every pid whose command line names `name`: the runtime processes a scenario may signal."""
    found: List[int] = []
    for entry in sorted(Path("/proc").iterdir()):
        if not entry.name.isdigit():
            continue
        try:
            argv = _cmdline(entry)
        except OSError:
            continue
        if name == "webserver":
            # `python3 -m webserver.app` or `python3 webserver/app.py`: the runtime's own REST
            # server, which this container does not run (see WEBERVER_ABSENT).
            hit = any("webserver.app" in a or a.endswith("webserver/app.py") for a in argv)
        else:
            hit = any(Path(a).name == name for a in argv)
        if hit:
            found.append(int(entry.name))
    return found


def _runtime_cwd() -> Optional[str]:
    pids = _processes("plc_main")
    if not pids:
        return None
    try:
        return str(Path(f"/proc/{pids[0]}/cwd").resolve())
    except OSError:
        return None


def _proc_spawn(pid: int) -> "tuple[List[str], str, Dict[str, str]]":
    """The argv, working directory and environment a process was started with, out of /proc."""
    try:
        argv = _cmdline(Path(f"/proc/{pid}"))
        cwd = str(Path(f"/proc/{pid}/cwd").resolve())
        env_raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return [], "", {}
    env: Dict[str, str] = {}
    for part in env_raw.split(b"\0"):
        key, sep, value = part.partition(b"=")
        if sep:
            env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return argv, cwd, env


def _signal(pid: int, number: int, name: str) -> None:
    try:
        os.kill(pid, number)
    except OSError as exc:
        raise Blocked(f"inject: cannot signal pid {pid} ({exc.strerror or exc})") from exc
    del name


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
        plant: Optional[object] = None,
        net: Optional[object] = None,
        core_socket: str = CORE_SOCKET,
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
        # The seams `inject` drives besides the session's own transport: the reference plant
        # (run control through its test hook), the client link (netfault.NetFault, the splice
        # that can add an RTT or cut a connection) and the runtime's core command socket.
        self.plant = plant
        self.net = net
        self.core_socket = core_socket
        self._core: Optional["_Line"] = None
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

    # -- fault injection (F154)

    def core(self) -> "_Line":
        """The runtime's core command socket, opened on first use (`inject: socket`)."""
        if self._core is None:
            self._core = _Line(self.core_socket)
        return self._core

    def _plant(self) -> object:
        if self.plant is None:
            raise Blocked(
                "inject plugin: this run has no reference plant (--plant SOCKET), so the plant's "
                "test hook is not there to hold or resume a task"
            )
        return self.plant

    def _net(self) -> object:
        if self.net is None:
            raise Blocked(
                f"inject {self._pending}: this run has no link splice in front of the endpoint, "
                "so the harness cannot shape or cut what it is connected over"
            )
        return self.net

    def inject(self, kind: str, args: JsonObj) -> Json:
        """Every fault kind F154 asks for, on the seam that carries it.

        A kind this endpoint cannot drive refuses, by name and with the file and line that would
        have to change. It never answers as though it had injected something.
        """
        self._pending = kind
        if kind in REFUSED:
            raise Blocked(f"inject {kind}: {REFUSED[kind]}")
        if kind == "socket":
            return self.core().ask(str(args.get("command", "")))
        if kind == "plugin":
            return self._inject_plugin(args)
        if kind in ("delayNetwork", "dropNetwork", "restoreNetwork"):
            return self._inject_link(kind, args)
        if kind in ("sigstopRuntime", "sigcontRuntime", "killRuntime", "restartRuntime"):
            return self._inject_process(kind, args)
        if kind == "writeFile":
            return self._inject_write(args)
        raise Blocked(
            f"inject {kind}: no seam in this harness drives that fault, so nothing was injected"
        )

    def _inject_plugin(self, args: JsonObj) -> Json:
        """A plugin drive. Only the reference plant's own test hook is a harness seam: the
        other plugins a scenario names (host_io, vpp-switch, opcua_server, test-plugin) are
        product code with actions of their own, and answering for them would be the harness
        inventing the runtime's behaviour."""
        name = str(args.get("name", ""))
        if name != PLANT_NAME:
            raise Blocked(
                f"inject plugin: no plugin named {name!r} is loaded in this runtime "
                f"(plugins.conf loads {PLANT_NAME} alone), and its `"
                f"{args.get('action', '?')}` would be that plugin's own action to implement — "
                "product code, not a fault the harness injects"
            )
        plant = self._plant()
        action = str(args.get("action", ""))
        task = str(args.get("task", "all"))
        run_control = getattr(plant, "run_control", None)
        if run_control is None:
            raise Blocked(
                f"inject plugin: the plant at {getattr(plant, 'path', '?')} has no run-control hook"
            )
        return run_control(action, task)

    def _inject_link(self, kind: str, args: JsonObj) -> Json:
        """The client link: an added RTT, a cut, or the link as it was."""
        net = self._net()
        if kind == "delayNetwork":
            rtt = args.get("rttMs")
            jitter = args.get("jitterMs", 0)
            if not isinstance(rtt, (int, float)) or isinstance(rtt, bool):
                raise Blocked(f"inject delayNetwork: rttMs must be a number, not {rtt!r}")
            net.delay(float(rtt), float(jitter) if isinstance(jitter, (int, float)) else 0.0)
        elif kind == "dropNetwork":
            net.drop()
        else:
            net.restore()
        return net.shape()

    def _find_process(self, name: str) -> int:
        """The pid of a runtime process, by the name its args name (`plc_main`, `webserver`)."""
        if name not in RUNTIME_PROCESSES:
            raise Blocked(
                f"inject: {name!r} is not a runtime process this harness may signal; it knows "
                f"{list(RUNTIME_PROCESSES)}"
            )
        if not Path("/proc").is_dir():
            raise Blocked(
                f"inject: this host has no /proc, so the harness cannot find the {name} process "
                "to signal; the injection runs in the container the runtime does"
            )
        matches = _processes(name)
        if not matches:
            raise Blocked(WEBERVER_ABSENT if name == "webserver" else
                          f"inject: no process named {name!r} is running in this container")
        if len(matches) > 1:
            raise Blocked(
                f"inject: {len(matches)} processes named {name!r} are running "
                f"({matches}); the injection cannot say which one it signalled"
            )
        return matches[0]

    def _inject_process(self, kind: str, args: JsonObj) -> Json:
        """Signals the harness can deliver, and the process lifetimes behind them."""
        pid = self._find_process(str(args.get("process", "plc_main")) if kind.startswith("sig") else "plc_main")
        if kind == "sigstopRuntime":
            _signal(pid, signal.SIGSTOP, str(args.get("signal", "SIGSTOP")))
        elif kind == "sigcontRuntime":
            _signal(pid, signal.SIGCONT, str(args.get("signal", "SIGCONT")))
        elif kind == "killRuntime":
            self._kill_runtime(pid, str(args.get("signal", "SIGKILL")))
        else:
            # The argv, cwd and environment are read before the kill: /proc keeps nothing after.
            spawn = _proc_spawn(pid)
            self._kill_runtime(pid, str(args.get("signal", "SIGKILL")))
            self._start_runtime(spawn)
        return {"process": "plc_main" if kind in ("killRuntime", "restartRuntime") else
                str(args.get("process", "plc_main")), "pid": pid, "state": self._state(pid)}

    @staticmethod
    def _state(pid: int) -> str:
        """`T` (stopped), `R`/`S` (running) or `gone`, read out of /proc."""
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "gone"
        return stat.rpartition(")")[2].split()[0] or "?"

    def _kill_runtime(self, pid: int, name: str) -> None:
        _signal(pid, signal.SIGKILL, name)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and self._state(pid) != "gone":
            time.sleep(0.05)

    def _start_runtime(self, spawn: "tuple[List[str], str, Dict[str, str]]") -> None:
        """Relaunch the runtime the way session_inner.sh launched it: the same argv, the same
        working directory and the same environment, read out of /proc of the process just killed.
        Nothing here invents a command line, so the runtime that comes back is the one that died.
        """
        argv, cwd, env = spawn
        if not argv:
            raise Blocked(
                "inject restartRuntime: the killed process is gone, so its argv, working "
                "directory and environment cannot be read back out of /proc; the driver must "
                "pass them (--runtime-argv) for a restart to be the same runtime"
            )
        proc = subprocess.Popen(  # noqa: S603 - the argv is the one the killed process had
            argv, cwd=cwd, env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + RUNTIME_UP_TIMEOUT_S
        while time.monotonic() < deadline:
            if Path("/run/runtime/plc_debug.socket").exists() and self._state(proc.pid) != "gone":
                return
            time.sleep(0.1)
        raise Blocked(
            f"inject restartRuntime: pid {proc.pid} is running but "
            f"{CORE_SOCKET} never reappeared within {RUNTIME_UP_TIMEOUT_S:g} s"
        )

    def _inject_write(self, args: JsonObj) -> Json:
        """Replace a file under the runtime's data directory, as an operator or a crash would.

        The path is the one a scenario names, resolved the way the runtime itself resolves it:
        relative to the running runtime's working directory. What the runtime makes of the
        content afterwards is its own business; this only reports what it wrote.
        """
        rel = str(args.get("path", ""))
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise Blocked(
                f"inject writeFile: {rel!r} is not a path under the runtime's data directory"
            )
        root = Path(_runtime_cwd() or ".")
        path = root / rel
        content = args.get("content", "")
        blob = content if isinstance(content, str) else json.dumps(content, sort_keys=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(blob, encoding="utf-8")
        return {
            "path": str(path),
            "bytes": len(blob.encode("utf-8")),
            "existed": True,
            "readBack": json.loads(path.read_text(encoding="utf-8"))
            if blob.strip().startswith(("{", "["))
            else None,
        }

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
    # Refusals that carried no request_seq to correlate on; `inject_frame` reads them. Class
    # attribute, so a subclass that swaps the transport and does not call super().__init__()
    # (tests/debug/wire/run_harness.py's WssSession) gets one too.
    uncorrelated: "queue.Queue[JsonObj]" = queue.Queue()

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
            elif rseq is None:
                # A refusal to a message the gateway could not parse carries no seq to correlate
                # on (gateway.py `_failure` with request_seq=None). An injected frame is exactly
                # such a message, so it is kept here for inject_frame to answer with.
                self.uncorrelated.put(msg)
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

    # -- a raw frame on this stream's own transport

    def _raw_frame(self, text: str) -> None:
        """Put one text frame carrying `text` on the wire, through whatever transport this
        session has. A subclass whose transport cannot be written at raises Blocked instead."""
        raise Blocked(
            "inject rawFrame: this session's transport cannot be written frame by frame; "
            "only the harness's own WSS client (tests/debug/wire/run_harness.py) can"
        )

    def inject_frame(self, kind: str, args: JsonObj) -> Json:
        """Send a message the Socket.IO client would never build, and answer with what the
        gateway said about it (ADR-003 D2, the refusal codes A087 and A129 read).

        The gateway takes a message as JSON text or as an object (`_parse`), so the wire form
        used here is the text one: a `msg` event whose argument is the message document. That is
        what puts the fault inside the gateway's own parser, which is where a refused message is
        decided, rather than in the Socket.IO framing above it (a packet cut off at the framing
        layer is dropped in silence, with no refusal to read).

        `truncated` is a message document cut off inside its JSON; `invalid` is a whole document
        that is not a request; `oversize` is a well-formed request over P6, which does carry a
        seq, so its refusal is correlated; `garbage` is a burst of frames that are not messages
        at all (A087's burst), which answers nothing.
        """
        frame = str(args.get("frame", ""))
        size = args.get("bytes", 2 * 1024 * 1024)
        if not isinstance(size, (int, float)) or isinstance(size, bool):
            raise Blocked(f"inject rawFrame: bytes must be a number, not {size!r}")
        if frame not in ("truncated", "invalid", "oversize", "garbage"):
            raise Blocked(
                f"inject rawFrame: {frame!r} is not a frame this harness builds; it builds "
                "truncated, invalid, oversize and garbage"
            )
        if frame == "garbage":
            count = args.get("count", 1)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise Blocked(
                    f"inject rawFrame: count must be a non-negative integer, not {count!r}"
                )
            for _ in range(count):
                self._raw_frame("!" * max(int(size), 1))
            return {"frames": count, "bytes": int(size) * count}
        with self.lock:
            self.seq += 1
            seq = self.seq
            waiter: "queue.Queue[JsonObj]" = queue.Queue()
            self.pending[seq] = waiter
        try:
            if frame == "truncated":
                document = f'{{"type":"request","seq":{seq},"command":'
            elif frame == "invalid":
                document = f'{{"type":"notice","seq":{seq},"body":"hello"}}'
            else:
                pad = "x" * max(int(size), 1)
                document = (
                    f'{{"type":"request","seq":{seq},"command":"status",'
                    f'"arguments":{{"pad":"{pad}"}}}}'
                )
            self._raw_frame(f"42{NAMESPACE}," + json.dumps(["msg", document]))
            box = waiter if frame == "oversize" else self.uncorrelated
            try:
                resp = box.get(timeout=RESPONSE_TIMEOUT_S)
            except queue.Empty:
                raise Blocked(
                    f"inject rawFrame {frame}: the gateway answered nothing on this stream in "
                    f"{RESPONSE_TIMEOUT_S:g} s"
                ) from None
        finally:
            with self.lock:
                self.pending.pop(seq, None)
        return resp

    def kill(self) -> None:
        # ASSUMED: python-engineio's abort drops the socket with no Socket.IO disconnect packet,
        # which is what the runtime sees of a SIGKILLed client.
        self.stop.set()
        self.sio.eio.disconnect(abort=True)
