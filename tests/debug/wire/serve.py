"""Serves /api/debug-session with the fake core on HOST:PORT until killed, and writes an admin
JWT to TOKEN_FILE: the server half of the other-network-namespace run (A068, netns.sh).

  python3 tests/debug/wire/serve.py HOST PORT TOKEN_FILE [CORE_SOCKET DEBUG_INFO]

With CORE_SOCKET and DEBUG_INFO the gateway links to a real core (plc_main's
/run/runtime/plc_debug.socket) and reads that program's debug-info.json, as webserver/app.py
wires it; without them it serves the fake core.

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

from webserver import restapi  # noqa: E402
from wire_server import WireServer  # noqa: E402


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
    server = WireServer(
        app,
        Path(_TMP),
        host=host,
        port=port,
        core_path=real[0] if real else None,
        info_path=Path(real[1]) if real else None,
    )
    token_file.write_text(token)
    print(f"serving {server.url} (bound {host}:{port})", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
