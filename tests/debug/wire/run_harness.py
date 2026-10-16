"""Runs harness oracles against a live /api/debug-session over WSS, using the harness's own
runner and SocketIOSession with tests/debug/wire/wss_client.py as the Socket.IO client.

  python3 tests/debug/wire/run_harness.py --endpoint https://HOST:PORT --token JWT --insecure \
      [--plant SOCKET] [--build-dir DIR] [--results PATH] ORACLE...

See also:
  tests/debug/harness/runner.py - the oracle runner (verdicts, trace).
  tests/debug/harness/socketio_endpoint.py - SocketIOEndpoint/SocketIOSession reused here.
  tests/debug/wire/netns.sh - runs this from another network namespace (A068).
  tests/debug/plant/session_e2e.sh - runs this against a real plc_main, the reference plant and
    the real gateway.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "harness"))

import build_views  # noqa: E402
import runner  # noqa: E402
from interfaces import (  # noqa: E402
    Blocked,
    BuildOutputs,
    JsonObj,
    Plant,
    UnavailableBuild,
    UnavailablePlant,
    WallClock,
)
from netfault import NetFault  # noqa: E402
from reference_plant import ReferencePlant  # noqa: E402
from socketio_endpoint import (  # noqa: E402
    CORE_SOCKET,
    NAMESPACE,
    SocketIOEndpoint,
    SocketIOSession,
)
from wss_client import WssSio  # noqa: E402


class WssSession(SocketIOSession):
    """SocketIOSession with its socketio.Client swapped for WssSio; everything else inherited."""

    def __init__(self, endpoint: SocketIOEndpoint) -> None:  # pylint: disable=super-init-not-called
        self.endpoint = endpoint
        self.seq = 0
        self.lock = threading.Lock()
        self.pending: Dict[int, "queue.Queue[JsonObj]"] = {}
        self.inbox: "queue.Queue[JsonObj]" = queue.Queue()
        self.stop = threading.Event()
        self.beat: Optional[threading.Thread] = None
        self.sio = WssSio(ssl_verify=not endpoint.insecure)
        self.sio.on("msg", self._on_msg, namespace=NAMESPACE)
        try:
            self.sio.connect(
                endpoint.url,
                namespaces=[NAMESPACE],
                auth={"token": endpoint.token} if endpoint.token else None,
                wait_timeout=5,
            )
        except (ConnectionError, OSError) as exc:
            raise Blocked(f"cannot attach to {NAMESPACE} at {endpoint.url}: {exc}") from exc

    def _raw_frame(self, text: str) -> None:
        """One text frame carrying `text`, on this connection's own TLS socket.

        simple_websocket masks and frames a string for us, so this is exactly what `emit` sends
        except that the harness chooses the payload: a message the Socket.IO client would never
        build, which is what `inject rawFrame` is.
        """
        with self.sio.lock:
            ws = self.sio.ws
            if ws is None:
                raise Blocked("inject rawFrame: this stream's transport is closed")
            ws.send(text)


class WssEndpoint(SocketIOEndpoint):
    def open_session(self) -> WssSession:
        self._probe()
        return WssSession(self)


LINK_FAULTS = ("delayNetwork", "dropNetwork", "restoreNetwork")


def _needs_link(oracles: List[Path]) -> bool:
    """Whether any of these oracles shapes or cuts the client link (F154's network kinds)."""
    for path in oracles:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for step in runner._flat(doc.get("steps")):
            if step.get("inject") in LINK_FAULTS:
                return True
    return False


def run(
    oracles: List[Path],
    url: str,
    token: Optional[str],
    insecure: bool,
    plant: Optional[Plant] = None,
    build: Optional[BuildOutputs] = None,
    deployed_tasks: Optional[Dict[str, float]] = None,
    deployed_params: Optional[Dict[str, float]] = None,
    deployed_build: Optional[Dict[str, str]] = None,
    deployed_halt_policy: Optional[str] = None,
    core_socket: Optional[str] = None,
) -> List[runner.RunResult]:
    # The client link (N013's tunnel profile, F154's network faults) is a splice the harness owns:
    # the gateway is reached through it, so `delayNetwork`, `dropNetwork` and `restoreNetwork` act
    # on the real transport rather than on the runtime's opinion of one. `delayNetwork` is step 0
    # of the scenarios that want it, so the splice has to be in place before the first connect —
    # and a run that asks for no link fault gets the gateway itself, unspliced.
    net: Optional[NetFault] = None
    if _needs_link(oracles):
        target = urlparse(url)
        net = NetFault(
            target.hostname or "127.0.0.1",
            target.port or (443 if target.scheme == "https" else 80),
        )
        endpoint_url = f"{target.scheme}://{net.host}:{net.port}"
    else:
        endpoint_url = url
    endpoint = WssEndpoint(
        endpoint_url, token=token, insecure=insecure,
        deployed_tasks=deployed_tasks, deployed_params=deployed_params,
        deployed_build=deployed_build, deployed_halt_policy=deployed_halt_policy,
        plant=plant, net=net, core_socket=core_socket or CORE_SOCKET,
    )
    try:
        return [
            runner.run_scenario(
                p, endpoint, plant or UnavailablePlant(), WallClock(), build or UnavailableBuild()
            )
            for p in oracles
        ]
    finally:
        if net is not None:
            net.close()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("oracles", nargs="+", type=Path)
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--token")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--plant", metavar="SOCKET", help="the reference plant plugin's Unix socket")
    ap.add_argument("--build-dir", type=Path, help="<dir>/<corpus>/debug/debug-info.json")
    ap.add_argument("--results", type=Path, help="append status/runs JSON lines here")
    ap.add_argument("--ran-on", default="", help="where the run happened, for --results rows")
    ap.add_argument("--deployed-task", action="append", default=[], metavar="NAME=MS",
                    help="a task of the deployed program and its interval (repeatable)")
    ap.add_argument("--deployed-param", action="append", default=[], metavar="KEY=SEC",
                    help="a gateway heartbeat param the driver deployed, as the oracle's "
                         "setup.params names it (P1_s=2 or P2_s=15; repeatable)")
    ap.add_argument("--deployed-build", action="append", default=[], metavar="FACT",
                    help="a fact of the deployed build, read back out of the deployed "
                         "debug-info.json: probes=on|off, origins=<count> (repeatable)")
    ap.add_argument("--deployed-halt-policy", default="none", metavar="KIND:N",
                    help="the halt policy the core itself reported it loaded at start "
                         "(`safe:4`, `hold:0`), or `none` when no policy was deployed")
    ap.add_argument("--core-socket", default=CORE_SOCKET, metavar="PATH",
                    help="the runtime's core command socket, which `inject socket` writes a "
                         "line to (the default is plc_main's)")
    opts = ap.parse_args(argv)
    plant = ReferencePlant(opts.plant) if opts.plant else None
    build = build_views.DirectoryBuild(opts.build_dir) if opts.build_dir else None
    deployed: Dict[str, float] = {}
    for item in opts.deployed_task:
        name, _, ms = item.partition("=")
        deployed[name] = float(ms)
    deployed_params: Dict[str, float] = {}
    for item in opts.deployed_param:
        key, _, seconds = item.partition("=")
        deployed_params[key] = float(seconds)
    deployed_build: Dict[str, str] = {}
    for item in opts.deployed_build:
        key, _, value = item.partition("=")
        deployed_build[key] = value
    results = run(opts.oracles, opts.endpoint, opts.token, opts.insecure, plant, build, deployed,
                  deployed_params, deployed_build, opts.deployed_halt_policy, opts.core_socket)
    rows = []
    for result in results:
        print(runner.format_trace(result), flush=True)
        rows.extend(runner.results_lines(result, opts.ran_on or f"wss {opts.endpoint}", runner._commit()))
    if opts.results is not None:
        runner.append_results(opts.results, rows)
        print(f"results: appended {len(rows)} lines to {opts.results}")
    return 0 if all(r.verdict.status == "PASS" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
