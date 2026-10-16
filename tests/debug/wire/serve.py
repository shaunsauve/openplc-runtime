"""Serves /api/debug-session with the fake core on HOST:PORT until killed, and writes an admin
JWT to TOKEN_FILE (and an expired one to TOKEN_FILE.expired): the server half of the
other-network-namespace run (A068, netns.sh) and of the real-core end-to-end runs.

  python3 tests/debug/wire/serve.py HOST PORT TOKEN_FILE [CORE_SOCKET DEBUG_INFO]

With CORE_SOCKET and DEBUG_INFO the gateway links to a real core (plc_main's
/run/runtime/plc_debug.socket) and reads that program's debug-info.json, as webserver/app.py
wires it; without them it serves the fake core.

Env:   OPENPLC_DEBUG_PARAMS  deploys the gateway's P1/P2, as `P1 = <s>` `P2 = <s>` (the same
        text debug.conf holds, from an oracle's setup.params). Absent, the gateway reads
        <data dir>/debug.conf as the runtime does. This is the deploy step for a scenario's
        setup.params; tests/debug/harness/socketio_endpoint.py checks the gateway reports it.

See also:
  tests/debug/wire/wire_server.py - the stack being served.
  tests/debug/wire/netns.sh - the caller.
"""

from __future__ import annotations

import os
import secrets
import sys
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))

_TMP = tempfile.mkdtemp(prefix="wire-serve-")
os.environ.setdefault("OPENPLC_RUNTIME_DIR", os.path.join(_TMP, "run"))
os.environ.setdefault("OPENPLC_PERSISTENT_DATA_DIR", os.path.join(_TMP, "data"))
os.environ.setdefault("SQLALCHEMY_DATABASE_URI", f"sqlite:///{os.path.join(_TMP, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", secrets.token_hex(32))
os.environ.setdefault("PEPPER", secrets.token_hex(32))

from flask_jwt_extended import create_access_token  # noqa: E402

from webserver import restapi  # noqa: E402
from webserver.debug_gateway.session_guard import HeartbeatParams  # noqa: E402
from wire_server import WireServer  # noqa: E402


def _deployed_params() -> HeartbeatParams | None:
    """OPENPLC_DEBUG_PARAMS (`P1 = <s>` `P2 = <s>` lines) as the gateway's heartbeat params.

    A scenario's setup.params states the P1/P2 it is written against; this is the deploy step
    for it (T-VER-PARAMDEPLOY, the sibling of the task intervals and of --start-halted). Absent
    the env var, params is None and the gateway reads debug.conf, as the runtime does.
    """
    text = os.environ.get("OPENPLC_DEBUG_PARAMS")
    if not text:
        return None
    values = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise SystemExit(f"OPENPLC_DEBUG_PARAMS: {raw!r} is not `key = value`")
        values[key.strip()] = float(value.strip())
    unknown = set(values) - {"P1", "P2", "P5"}
    if unknown:
        raise SystemExit(f"OPENPLC_DEBUG_PARAMS: unknown key(s) {sorted(unknown)}")
    return HeartbeatParams(values.get("P1", 2.0), values.get("P2", 15.0))


def _deployed_p5() -> float | None:
    """OPENPLC_DEBUG_PARAMS's `P5` (ADR-005 D5.1), the TTL a force with no `ttlMs` of its own
    gets. None leaves the gateway on debug.conf's P5, as the runtime does; A210 measures the
    default on a default debug.conf, so no oracle states it."""
    text = os.environ.get("OPENPLC_DEBUG_PARAMS", "")
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        key, sep, value = line.partition("=")
        if sep and key.strip() == "P5":
            return float(value.strip())
    return None


def main() -> None:
    host, port, token_file = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
    real = sys.argv[4:6]
    app = restapi.app_restapi
    app.register_blueprint(restapi.restapi_bp, url_prefix="/api")
    with app.app_context():
        restapi.db.create_all()
        restapi.apply_user_schema_migrations()
    http = app.test_client()
    http.post("/api/create-user", json={"username": "admin", "password": "admin-pass"})
    login = http.post("/api/login", json={"username": "admin", "password": "admin-pass"})
    token = str(login.get_json()["access_token"])
    with app.app_context():
        # An already-expired JWT for the same user, signed with this server's key (A119).
        admin = restapi.User.query.filter_by(username="admin").one()
        expired = create_access_token(identity=admin, expires_delta=timedelta(seconds=-1))
    Path(f"{token_file}.expired").write_text(expired)
    server = WireServer(
        app,
        Path(_TMP),
        host=host,
        port=port,
        core_path=real[0] if real else None,
        info_path=Path(real[1]) if len(real) > 1 else None,
        params=_deployed_params(),
        p5_s=_deployed_p5(),
    )
    # GET /api/status's `debug` block (ADR-003 D9), as the webserver's status handler adds it
    # from the gateway (webserver/app.py handle_status; this server does not import app.py,
    # whose import starts a runtime manager).
    restapi.register_callback_get(
        lambda command, _data: {"debug": server.gateway.status_block()}
        if command == "status" else {"error": "Unknown argument"}
    )
    token_file.write_text(token)
    print(f"serving {server.url} (bound {host}:{port})", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
