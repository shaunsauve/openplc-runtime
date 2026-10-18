"""The runner's four seams: the debug endpoint (ADR-003), the plant (scan witness, I/O, virtual
time), the clock and the build outputs (ADR-002 D1 files). A real run plugs the Socket.IO
endpoint (or, with no debugger, the core socket), the reference plant (F150), the wall clock and
the build directory; the tests plug a simulation that implements all four.

See also:
  tests/debug/harness/runner.py - executes oracle steps through these seams and emits verdicts.
  tests/debug/harness/build_views.py - the build directory reader and the `inspect` views.
  tests/debug/harness/socketio_endpoint.py - the real endpoint (ADR-003 D1/D2, ADR-005 D1/D2/D4).
  tests/debug/harness/fake_endpoint.py - the simulated endpoint, plant and clock (tests only).
  tests/debug/harness/reference_plant.py - the real plant adapter and the debugger-less endpoint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Protocol, Set, Union

Json = Union[None, bool, int, float, str, List["Json"], Dict[str, "Json"]]
JsonObj = Dict[str, Json]


class Blocked(Exception):
    """The scenario cannot be judged here: endpoint missing, capability absent, seam not built."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Session(Protocol):
    """One client stream on /api/debug-session."""

    def request(self, command: str, args: JsonObj) -> JsonObj:
        """Send a request; return its response envelope (`success`, `body`, `error`)."""

    def events(self) -> List[JsonObj]:
        """Drain events received since the last call, in arrival order: `{event, body}`."""

    def close(self) -> None:
        """Detach in order (the session ends normally)."""

    def kill(self) -> None:
        """Drop the transport with no detach, as a SIGKILLed client would (F154 killClient)."""


class Endpoint(Protocol):
    description: str

    def prepare(self, setup: JsonObj) -> None:
        """Apply the oracle's setup (build, tasks, policies); raise Blocked if it cannot."""

    def open_session(self) -> Session: ...

    def status(self) -> JsonObj:
        """The `debug` block of GET /api/status (ADR-003 D9)."""

    def log(self) -> JsonObj: ...

    def inject(self, kind: str, args: JsonObj) -> Json:
        """Fault injection (F154) and out-of-session drives (REST, core socket, plugin); returns
        what the injection answered (an oracle's `inject.expect` matches it)."""

    def credential(self, kind: str) -> Json:
        """A JWT for `initialize`: `valid`, `none` (null), `forged` (bad signature) or `expired`."""

    def requests(self) -> Optional[Set[str]]:
        """The requests this endpoint says it serves — the handshake's `requests`
        (DEBUG_PROTOCOL.md `Handshake`, ADR-003 D3) — or None when it cannot say.

        `None` is neither a yes nor a no, and the runner reads it as "no opinion": a scenario is
        then left to discover an unserved request the way it always has, by sending it and reading
        the refusal. Reporting one is optional; pretending to it is not.
        """


@dataclass(frozen=True)
class PlantSample:
    t_ms: float
    witness: Dict[str, int]
    vt_ms: Dict[str, float]
    outputs: Dict[str, Json] = field(default_factory=dict)


class Plant(Protocol):
    def prepare(self, plant_setup: JsonObj) -> None:
        """Arm the oracle's `setup.plant` (its `schedule` runs on the plant clock, PLC virtual
        time, counted from now); raise Blocked if this plant cannot."""

    def set_inputs(self, values: Mapping[str, Json]) -> None: ...

    def fail_exchanges(self, n: int) -> None: ...

    def sample(self) -> PlantSample:
        """The latest sample (the plant's last received scan)."""

    def history(self, since_ms: float) -> List[PlantSample]:
        """Every sample taken after `since_ms`, oldest first."""


class BuildOutputs(Protocol):
    def read(self, corpus: str, kind: str, variant: Optional[str]) -> Json:
        """One ADR-002 D1 output of the corpus program's build (`variant` names a variant-*.st
        build): `debugInfo`/`debugMap` as parsed JSON, `generatedCpp` as text. Raise Blocked when
        the output is not available here."""


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

    REASON = "no reference plant: pass --plant SOCKET (tests/debug/plant/e2e.sh)"

    def prepare(self, plant_setup: JsonObj) -> None:
        if plant_setup.get("schedule"):
            raise Blocked(self.REASON)

    def set_inputs(self, values: Mapping[str, Json]) -> None:
        raise Blocked(self.REASON)

    def fail_exchanges(self, n: int) -> None:
        raise Blocked(self.REASON)

    def sample(self) -> PlantSample:
        raise Blocked(self.REASON)

    def history(self, since_ms: float) -> List[PlantSample]:
        raise Blocked(self.REASON)


class UnavailableBuild:
    """Stands in when no build directory was given (or found)."""

    REASON = "no build outputs (ADR-002 D1 files): pass --build-dir after tests/debug/corpus/build_all.sh"

    def read(self, corpus: str, kind: str, variant: Optional[str]) -> Json:
        raise Blocked(self.REASON)
