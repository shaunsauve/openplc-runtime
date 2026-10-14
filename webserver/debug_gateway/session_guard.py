# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Session liveness settings and resume tokens for the debug gateway (ADR-005 D2.4, D4.1-D4.2).

P1 is the heartbeat interval and P2 the single value that is both the heartbeat timeout and the
reconnect window. Both are read from `<data dir>/debug.conf` as `P1 = <s>` and `P2 = <s>` lines;
other keys belong to other settings and are ignored here.

See also:
  webserver/debug_gateway/gateway.py - applies P2 to every session and serves resume.
  webserver/config.py - PERSISTENT_DATA_DIR, where debug.conf lives.
  docs/DEBUG_PROTOCOL.md - "Heartbeat, loss and resume", the client-facing rules.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

from webserver.debug_gateway.protocol import JsonObj

P1_RANGE_S: Tuple[float, float] = (0.5, 30.0)
P2_RANGE_S: Tuple[float, float] = (5.0, 600.0)
CONF_NAME = "debug.conf"


class ConfigError(ValueError):
    """debug.conf holds a value the runtime refuses at load."""


@dataclass(frozen=True)
class HeartbeatParams:
    p1_s: float = 2.0
    p2_s: float = 15.0

    def as_json(self) -> JsonObj:
        return {"P1": _number(self.p1_s), "P2": _number(self.p2_s)}


def _number(v: float) -> float:
    return int(v) if float(v).is_integer() else v


def parse_params(text: str) -> HeartbeatParams:
    """`key = value` lines, `#` comments. Raises ConfigError on a bad value or P2 < 3 x P1."""
    values: Dict[str, float] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep:
            raise ConfigError(f"line {n}: expected key = value")
        if key not in ("P1", "P2"):
            continue
        try:
            values[key] = float(value.strip())
        except ValueError as exc:
            raise ConfigError(f"line {n}: {key} is not a number of seconds") from exc
    default = HeartbeatParams()
    params = HeartbeatParams(values.get("P1", default.p1_s), values.get("P2", default.p2_s))
    for name, value, (low, high) in (
        ("P1", params.p1_s, P1_RANGE_S),
        ("P2", params.p2_s, P2_RANGE_S),
    ):
        if not low <= value <= high:
            raise ConfigError(f"{name} = {value:g} s is outside {low:g}-{high:g} s")
    if params.p2_s < 3 * params.p1_s:
        raise ConfigError(f"P2 = {params.p2_s:g} s is below 3 x P1 = {3 * params.p1_s:g} s")
    return params


def load_params(path: Path) -> HeartbeatParams:
    """The defaults when the file is absent; ConfigError when it is present and invalid."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return HeartbeatParams()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return parse_params(text)


def mint_resume_token() -> Tuple[str, str]:
    """A 128-bit random token and the hash the gateway keeps in its place."""
    token = secrets.token_urlsafe(16)
    return token, resume_hash(token)


def resume_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
