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
        OPENPLC_DEBUG_USERS  the extra accounts to create and log in as, comma separated
        (`userb,userc,obs_only`). Each is a real account (POST /api/create-user) and a real login
        (POST /api/login); the JWTs go to TOKEN_FILE.users as `name=JWT` lines, which the driver
        hands the harness as `--user-token`. This is the only way a second identity reaches a
        scenario: the harness cannot mint a JWT, so it can only be given one. Absent, no account
        is created and the run is byte-for-byte the run before - the deploy step is off unless an
        oracle names a `{"~token": "user:<name>"}`.
        The ROLES here are the runtime's own two (webserver/restapi.py:143-146) and no more: every
        name but `admin` is created with `user`. ADR-005 D3.2's five permissions are a per-user
        `debug_permissions` column the product does not have, so a name like `obs_only` or
        `ctl_only` cannot be given a narrower set than `plain` here. That is deliberate: a
        criterion that needs the permissions to differ must measure the runtime's answer to that,
        not a set the harness invented for it.

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


def _named_users() -> list:
    """OPENPLC_DEBUG_USERS' account names, in order, without duplicates."""
    names = []
    for raw in os.environ.get("OPENPLC_DEBUG_USERS", "").split(","):
        name = raw.strip()
        if name and name not in names:
            names.append(name)
    return names


def _login(http, username: str, password: str) -> str:
    """A real login's access token. Raises SystemExit rather than serving a half-built identity:
    a named user the gateway cannot be handed would be a silent BLOCK one step later."""
    reply = http.post("/api/login", json={"username": username, "password": password})
    body = reply.get_json() or {}
    token = body.get("access_token")
    if not token:
        raise SystemExit(f"login as {username!r} answered {reply.status_code}: {body}")
    return str(token)


def _create_and_login(http, admin_token: str, name: str) -> str:
    """The account `name`, then a JWT for it. `admin` is the one the harness runs as, so it is
    served the token it already has rather than a second login of the same identity."""
    if name == "admin":
        return admin_token
    password = f"{name}-pass"
    made = http.post(
        "/api/create-user",
        json={"username": name, "password": password, "role": "user"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    # 409 means the account is already there (a reused data dir); anything else must not be
    # ignored, because a login that then fails is the same failure with less to read.
    if made.status_code not in (201, 409):
        raise SystemExit(f"create-user {name!r} answered {made.status_code}: {made.get_json()}")
    return _login(http, name, password)


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
    token = _login(http, "admin", "admin-pass")
    names = _named_users()
    users_path = Path(f"{token_file}.users")
    if names:
        lines = [f"{name}={_create_and_login(http, token, name)}" for name in names]
        users_path.write_text("\n".join(lines) + "\n")
        print(f"serving as {len(names)} named user(s): {', '.join(names)}", flush=True)
    else:
        users_path.unlink(missing_ok=True)
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
