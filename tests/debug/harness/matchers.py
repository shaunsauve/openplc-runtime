"""Oracle matchers (tests/debug/corpus/README.md § Matchers): literal equality, `~` matchers and
captures. Pure; returns the first mismatch with its path so the runner can report it.

See also:
  tests/debug/harness/runner.py - applies these to responses, events and observations.
  tests/debug/corpus/oracle.schema.json - the `match` and `matcher` definitions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

from interfaces import Json


class _Missing:
    """An absent key, distinct from JSON null."""

    def __repr__(self) -> str:
        return "<absent>"


MISSING = _Missing()
Observed = Union[Json, _Missing]
Captures = Dict[str, Json]


@dataclass(frozen=True)
class Mismatch:
    path: str
    expected: Json
    observed: Observed
    why: str

    def describe(self) -> str:
        return f"{self.path}: {self.why} (expected {_show(self.expected)}, observed {_show(self.observed)})"


def _show(value: Observed) -> str:
    if isinstance(value, _Missing):
        return "<absent>"
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= 160 else text[:157] + "..."


def _number(value: Observed) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _equal(expected: Json, observed: Observed) -> bool:
    if isinstance(expected, bool) or isinstance(observed, bool):
        return type(expected) is type(observed) and expected == observed
    en, on = _number(expected), _number(observed)
    if en is not None and on is not None:
        return en == on
    return not isinstance(observed, _Missing) and expected == observed


def is_matcher(node: Json) -> bool:
    return isinstance(node, dict) and bool(node) and all(k.startswith("~") for k in node)


def match(
    expected: Json, observed: Observed, captures: Captures, path: str = "$"
) -> Optional[Mismatch]:
    """None when `observed` satisfies `expected`; binds `~capture` names into `captures`."""
    if is_matcher(expected):
        assert isinstance(expected, dict)
        return _match_matcher(expected, observed, captures, path)
    if isinstance(expected, dict):
        if not isinstance(observed, dict):
            return Mismatch(path, expected, observed, "not an object")
        for key, sub in expected.items():
            found = _match_key(sub, observed, key, captures, f"{path}.{key}")
            if found is not None:
                return found
        return None
    if isinstance(expected, list):
        if not isinstance(observed, list):
            return Mismatch(path, expected, observed, "not an array")
        if len(expected) != len(observed):
            return Mismatch(path, expected, observed, f"length {len(observed)} != {len(expected)}")
        for i, (e, o) in enumerate(zip(expected, observed)):
            found = match(e, o, captures, f"{path}[{i}]")
            if found is not None:
                return found
        return None
    if not _equal(expected, observed):
        return Mismatch(path, expected, observed, "not equal")
    return None


def _match_key(
    sub: Json, observed: Dict[str, Json], key: str, captures: Captures, path: str
) -> Optional[Mismatch]:
    return match(sub, observed[key] if key in observed else MISSING, captures, path)


def _match_matcher(
    m: Dict[str, Json], observed: Observed, captures: Captures, path: str
) -> Optional[Mismatch]:
    expected: Json = m

    def fail(why: str) -> Mismatch:
        return Mismatch(path, expected, observed, why)

    if "~absent" in m:
        return None if isinstance(observed, _Missing) else fail("present, expected absent")
    if isinstance(observed, _Missing):
        return fail("absent")
    num = _number(observed)
    for key, arg in m.items():
        if key in ("~capture", "~add", "~tol", "~any"):
            continue
        if key == "~eq" and not _equal(arg, observed):
            return fail("not equal")
        if key == "~ne" and _equal(arg, observed):
            return fail("equal, expected different")
        if key in ("~lt", "~le", "~gt", "~ge"):
            bound = _number(arg)
            if num is None or bound is None:
                return fail("not a number")
            ok = {"~lt": num < bound, "~le": num <= bound, "~gt": num > bound, "~ge": num >= bound}[
                key
            ]
            if not ok:
                return fail(f"fails {key} {bound:g}")
        if key == "~range":
            lo, hi = _pair(arg)
            if num is None or lo is None or hi is None or not lo <= num <= hi:
                return fail("outside range")
        if key == "~approx":
            centre, tol = _pair(arg)
            if num is None or centre is None or tol is None or abs(num - centre) > tol:
                return fail("not within tolerance")
        if key == "~oneOf":
            if not isinstance(arg, list) or not any(_equal(v, observed) for v in arg):
                return fail("not one of")
        if key == "~display":
            # The observed value carries its IEC text only when the endpoint sent a string.
            if observed != arg:
                return fail("display text differs")
        if key in ("~len", "~maxLen"):
            if not isinstance(observed, (list, str)):
                return fail("has no length")
            limit = _number(arg)
            if limit is None:
                return fail("bad length bound")
            if (key == "~len" and len(observed) != limit) or (
                key == "~maxLen" and len(observed) > limit
            ):
                return fail(f"length {len(observed)}")
        if key == "~ref":
            if not isinstance(arg, str) or arg not in captures:
                return fail(f"capture {arg!r} not bound")
            base = captures[arg]
            add = _number(m.get("~add", 0)) or 0.0
            tol = _number(m.get("~tol", 0)) or 0.0
            base_n = _number(base)
            if base_n is not None:
                if num is None or abs(num - (base_n + add)) > tol:
                    return fail(
                        f"not {arg}{add:+g} = {base_n + add:g}" + (f" +/- {tol:g}" if tol else "")
                    )
            elif not _equal(base, observed):
                return fail(f"differs from capture {arg}")
    name = m.get("~capture")
    if isinstance(name, str):
        captures[name] = observed
    return None


def _pair(arg: Json) -> List[Optional[float]]:
    if isinstance(arg, list) and len(arg) == 2:
        return [_number(arg[0]), _number(arg[1])]
    return [None, None]
