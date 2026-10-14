"""The runner's three seams: the debug endpoint (ADR-003), the plant (scan witness, I/O, virtual
time) and the clock. A real run plugs the Socket.IO endpoint, the reference plant (F150) and the
wall clock; the tests plug a simulation that implements all three.

See also:
  tests/debug/harness/runner.py - executes oracle steps through these seams and emits verdicts.
  tests/debug/harness/socketio_endpoint.py - the real endpoint (ADR-003 D1/D2, ADR-005 D1/D2/D4).
  tests/debug/harness/fake_endpoint.py - the simulated endpoint, plant and clock (tests only).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Protocol, Union

Json = Union[None, bool, int, float, str, List["Json"], Dict[str, "Json"]]
JsonObj = Dict[str, Json]


class Blocked(Exception):
    """The scenario cannot be judged here: endpoint missing, capability absent, seam not built."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.offset = 0  # set by the runner when raised by the second step of an interleaved pair


class Session(Protocol):
    """One client stream on /api/debug-session."""

    def request(self, command: str, args: JsonObj) -> JsonObj:
        """Send a request; return its response envelope (`success`, `body`, `error`)."""

    def events(self) -> List[JsonObj]:
        """Drain events received since the last call, in arrival order: `{event, body}`."""

    def close(self) -> None: ...


class Endpoint(Protocol):
    description: str

    def prepare(self, setup: JsonObj) -> None:
        """Apply the oracle's setup (build, tasks, policies); raise Blocked if it cannot."""

    def open_session(self) -> Session: ...

    def status(self) -> JsonObj:
        """The `debug` block of GET /api/status (ADR-003 D9)."""

    def log(self) -> JsonObj: ...

    def inject(self, kind: str, args: JsonObj) -> None:
        """Fault injection (F154)."""


@dataclass(frozen=True)
class PlantSample:
    t_ms: float
    witness: Dict[str, int]
    vt_ms: Dict[str, float]
    outputs: Dict[str, Json] = field(default_factory=dict)


class Plant(Protocol):
    def set_inputs(self, values: Mapping[str, Json]) -> None: ...

    def fail_exchanges(self, n: int) -> None: ...

    def sample(self) -> PlantSample:
        """The latest sample (the plant's last received scan)."""

    def history(self, since_ms: float) -> List[PlantSample]:
        """Every sample taken after `since_ms`, oldest first."""


class Clock(Protocol):
    def now_ms(self) -> float: ...

    def sleep_ms(self, ms: float) -> None: ...


class WallClock:
    def now_ms(self) -> float:
        return time.monotonic() * 1000.0

    def sleep_ms(self, ms: float) -> None:
        time.sleep(max(ms, 0.0) / 1000.0)


class UnavailablePlant:
    """Stands in until the reference plant plugin (F150) exists."""

    REASON = "reference plant plugin (F150) not built: no plant adapter"

    def set_inputs(self, values: Mapping[str, Json]) -> None:
        raise Blocked(self.REASON)

    def fail_exchanges(self, n: int) -> None:
        raise Blocked(self.REASON)

    def sample(self) -> PlantSample:
        raise Blocked(self.REASON)

    def history(self, since_ms: float) -> List[PlantSample]:
        raise Blocked(self.REASON)
