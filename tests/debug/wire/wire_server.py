"""Serves the real webserver stack for /api/debug-session over TLS on a real port, with the fake
core behind the gateway: the legacy /api/debug namespace and the session namespace share one
Socket.IO instance, as in webserver/app.py.

See also:
  tests/debug/wire/fake_core.py - the core agent stand-in on the internal link.
  tests/debug/wire/wss_client.py - the WSS client the tests and the harness use.
  tests/debug/wire/serve.py - runs this as a process (another network namespace, A068).
  webserver/app.py - the production wiring this mirrors (run_https).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from flask import Flask
from werkzeug.serving import BaseWSGIServer, make_server

from fake_core import FakeCore, Json, JsonObj, debug_info
from webserver.credentials import CertGen
from webserver.debug_gateway import UnixCoreLink, init_debug_gateway
from webserver.debug_websocket import init_debug_websocket


class LegacyCore:
    """The legacy command socket (`DEBUG:<hex>` lines), recording what the webserver sent."""

    def __init__(self, reply: str = "DEBUG:41 00 01") -> None:
        self.reply = reply
        self.sent: List[str] = []

    def is_connected(self) -> bool:
        return True

    def send_and_receive(self, command: str, timeout: Optional[float] = None) -> Optional[str]:
        del timeout
        self.sent.append(command)
        return self.reply


class WireServer:
    def __init__(
        self,
        app: Flask,
        workdir: Path,
        host: str = "127.0.0.1",
        port: int = 0,
        capabilities: Optional[JsonObj] = None,
        delays: Optional[Dict[str, float]] = None,
        info: Optional[JsonObj] = None,
        core_path: Optional[str] = None,
        info_path: Optional[Path] = None,
    ) -> None:
        """`core_path`/`info_path` serve a real core (plc_main's plc_debug.socket and its
        debug-info.json) instead of the fake; `core` is then None."""
        self.workdir = workdir
        self.core: Optional[FakeCore] = None
        if core_path is not None and info_path is not None:
            self.info_path, self.core_path = info_path, core_path
        else:
            self.info_path = workdir / "debug-info.json"
            self.info_path.write_text(json.dumps(info if info is not None else debug_info()))
            # AF_UNIX paths are capped near 104 bytes; the pytest tmp path can exceed that.
            self.core_path = f"/tmp/fc-{os.getpid()}-{id(self)}.sock"
            self.core = FakeCore(self.core_path, capabilities=capabilities, delays=delays)
        self.legacy = LegacyCore()
        self.socketio = init_debug_websocket(app, self.legacy)
        self.link = UnixCoreLink(self.core_path)
        self.gateway = init_debug_gateway(self.socketio, self.link, self.info_path)
        cert, key = _certificate()
        self.server: BaseWSGIServer = make_server(
            host, port, app, threaded=True, ssl_context=(cert, key)
        )
        self.url = f"https://{'127.0.0.1' if host == '0.0.0.0' else host}:{self.server.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        if not self.link.wait_ready(5.0):
            raise RuntimeError("the gateway never received the core's hello")

    def close(self) -> None:
        self.server.shutdown()
        self.link.close()
        if self.core is not None:
            self.core.close()


_CERT: List[str] = []


def _certificate() -> Tuple[str, str]:
    """One self-signed pair per process, by the runtime's own CertGen (RSA 4096 is slow)."""
    if not _CERT:
        folder = Path(tempfile.mkdtemp(prefix="wire-cert-"))
        cert, key = str(folder / "cert.pem"), str(folder / "key.pem")
        CertGen(hostname="localhost", ip_addresses=["127.0.0.1"]).generate_self_signed_cert(
            cert_file=cert, key_file=key
        )
        _CERT.extend([cert, key])
    return _CERT[0], _CERT[1]


def as_obj(v: Json) -> JsonObj:
    return v if isinstance(v, dict) else {}
