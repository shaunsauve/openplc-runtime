"""Runs harness oracles against a live /api/debug-session over WSS, using the harness's own
runner and SocketIOSession with tests/debug/wire/wss_client.py as the Socket.IO client.

  python3 tests/debug/wire/run_harness.py --endpoint https://HOST:PORT --token JWT --insecure \
      tests/debug/wire/wire-control.oracle.json

See also:
  tests/debug/harness/runner.py - the oracle runner (verdicts, trace).
  tests/debug/harness/socketio_endpoint.py - SocketIOEndpoint/SocketIOSession reused here.
  tests/debug/wire/netns.sh - runs this from another network namespace (A068).
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "harness"))

import runner  # noqa: E402
from interfaces import Blocked, JsonObj, UnavailableBuild, UnavailablePlant, WallClock  # noqa: E402
from socketio_endpoint import NAMESPACE, SocketIOEndpoint, SocketIOSession  # noqa: E402
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


class WssEndpoint(SocketIOEndpoint):
    def open_session(self) -> WssSession:
        self._probe()
        return WssSession(self)


def run(oracles: List[Path], url: str, token: Optional[str], insecure: bool) -> List[runner.RunResult]:
    endpoint = WssEndpoint(url, token=token, insecure=insecure)
    return [
        runner.run_scenario(p, endpoint, UnavailablePlant(), WallClock(), UnavailableBuild())
        for p in oracles
    ]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("oracles", nargs="+", type=Path)
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--token")
    ap.add_argument("--insecure", action="store_true")
    opts = ap.parse_args(argv)
    results = run(opts.oracles, opts.endpoint, opts.token, opts.insecure)
    for result in results:
        print(runner.format_trace(result))
    return 0 if all(r.verdict.status == "PASS" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
