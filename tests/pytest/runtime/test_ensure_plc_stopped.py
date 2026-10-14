# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""An upload stops a loaded program first, including one held SAFE_STOPPED or ERROR (A206 N3)."""

# NO-FOLD: webserver-contract - the status strings come from core/src/plc_app/unix_socket.c.

from dataclasses import dataclass, field

import pytest

from webserver.plcapp_management import ensure_plc_stopped


@dataclass
class FakeRuntime:
    """Answers STATUS from a script; STOP moves it to the landing state."""

    status: str
    stop_reply: str
    landing: str
    stops: list[str] = field(default_factory=list)

    def status_plc(self) -> str:
        return f"STATUS:{self.status}\n"

    def stop_plc(self) -> str:
        self.stops.append(self.status)
        if self.stop_reply == "STOP:OK":
            self.status = self.landing
        return f"{self.stop_reply}\n"


@pytest.mark.parametrize(
    ("status", "stop_reply", "landing", "want_stop", "want_result"),
    [
        ("RUNNING", "STOP:OK", "STOPPED", True, (True, True)),
        ("SAFE_STOPPED", "STOP:OK", "STOPPED", True, (True, False)),
        ("ERROR", "STOP:OK", "STOPPED", True, (True, False)),  # ERROR with the output hold
        ("ERROR", "STOP:ERROR", "ERROR", True, (True, False)),  # ERROR without it: nothing loaded
        ("STOPPED", "STOP:ERROR", "STOPPED", False, (True, False)),
        ("EMPTY", "STOP:ERROR", "EMPTY", False, (True, False)),
    ],
)
def test_upload_stops_every_loaded_state(
    status: str, stop_reply: str, landing: str, want_stop: bool, want_result: tuple[bool, bool]
) -> None:
    rt = FakeRuntime(status, stop_reply, landing)
    assert ensure_plc_stopped(rt, timeout_s=1.0) == want_result  # type: ignore[arg-type]
    assert bool(rt.stops) == want_stop
