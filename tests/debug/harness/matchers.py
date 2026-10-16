"""Oracle matchers (tests/debug/corpus/README.md § Matchers): literal equality, `~` matchers and
captures. Pure; returns the first mismatch with its path so the runner can report it.

See also:
  tests/debug/harness/runner.py - applies these to responses, events and observations.
  tests/debug/corpus/oracle.schema.json - the `match` and `matcher` definitions.
"""

from __future__ import annotations

import hashlib
import json
import re
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


# Keys whose value is an IEC identifier, compared case-insensitively (DEBUG_PROTOCOL's `variables`,
# pm ruling 13): the runtime reports debug-info.json's spelling, which STruCpp upper-cases, and
# an oracle writes the source's. Every comparison folds, not only the ones a caller remembered to
# fold, which is why the fold lives here, beside the comparison, rather than at the call site.
IDENTIFIER_KEYS = frozenset({"pou", "instance", "name", "path"})
# A `type` is an IEC identifier too, and a compound one is more than a name: `ARRAY[0..1] OF Seg`
# and `STRING[80]` carry bounds, which are part of the type and not decoration. A fold may only
# erase a difference the specification declares insignificant, and the specification declares one:
# case (IEC 61131-3 6.1.2, pm ruling 13). Nothing declares a bound insignificant, so every token a
# type is made of is tokenised -- names AND the numbers that bound it -- and the two spellings must
# agree on all of them, in the same order. The token pattern used to be able to see no digit that did
# not follow a letter, so every width and every array bound tokenised away: `STRING[8]` matched
# `STRING[80]` and `ARRAY[0..1] OF Seg` matched `ARRAY[0..9] OF Seg`. The only difference left that
# this fold still erases is the whitespace between two tokens, which no type can distinguish.
TYPE_TOKEN = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|\d+")


def _fold_type(expected: str, observed: str) -> str:
    """`observed` with the oracle's spelling when the two name the same type.

    Every token has to match case-insensitively and the token lists have to agree, so this never
    makes two different types equal: `INT` and `DINT` are not, `STRING` and `STRING[8]` are not,
    `Seg` and `Point` are not, and neither are `STRING[8]` and `STRING[80]`."""
    if expected == observed or expected.casefold() == observed.casefold():
        return expected
    want = [t.casefold() for t in TYPE_TOKEN.findall(expected)]
    got = [t.casefold() for t in TYPE_TOKEN.findall(observed)]
    return expected if want == got else observed


def fold_identifiers(expected: Json, observed: Observed) -> Observed:
    """`observed` with each identifier-valued string that equals the oracle's but for case
    replaced by the oracle's spelling, so the matchers compare everything else exactly.

    Applied inside a matcher too (`~contains` over a list of records, say), which is the case a
    caller-side fold missed: pm ruling 13 makes every identifier comparison case-insensitive, not
    the ones that happen to sit at the top of an expectation."""
    if isinstance(expected, dict) and isinstance(observed, dict) and not is_matcher(expected):
        out = dict(observed)
        for k, want in expected.items():
            if k not in out:
                continue
            got = out[k]
            if k in IDENTIFIER_KEYS and isinstance(want, str) and isinstance(got, str):
                out[k] = want if got.casefold() == want.casefold() else got
            elif k == "type" and isinstance(want, str) and isinstance(got, str):
                out[k] = _fold_type(want, got)
            else:
                out[k] = fold_identifiers(want, got)
        return out
    if isinstance(expected, list) and isinstance(observed, list):
        return [fold_identifiers(w, g) for w, g in zip(expected, observed)] + observed[len(expected):]
    return observed


KNOWN = {
    "~eq", "~ne", "~lt", "~le", "~gt", "~ge", "~range", "~approx", "~oneOf", "~display",
    "~capture", "~ref", "~add", "~tol", "~any", "~absent", "~len", "~maxLen", "~notRef",
    "~contains", "~sha256",
}


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
    unknown = sorted(set(m) - KNOWN)
    if unknown:
        # An unknown key used to be skipped, so a matcher this runner predates passed silently.
        return fail(f"unknown matcher {unknown}")
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
        if key == "~contains":
            if isinstance(observed, str):
                if not isinstance(arg, str) or arg not in observed:
                    return fail("does not contain")
            elif isinstance(observed, list):
                # A trial match must not leak captures from elements that then fail, and each
                # element is folded for identifiers first: a record inside a list is compared the
                # same way a record at the top of an expectation is (pm ruling 13).
                if not any(
                    match(arg, fold_identifiers(arg, item), dict(captures)) is None
                    for item in observed
                ):
                    return fail("no element matches")
            else:
                return fail("neither an array nor a string")
        if key == "~sha256":
            # A fetched source must hash to the debug info's per-file sha256 (ADR-002 D3 files[]).
            if not isinstance(arg, str) or arg not in captures:
                return fail(f"capture {arg!r} not bound")
            if not isinstance(observed, str):
                return fail("not a string")
            if hashlib.sha256(observed.encode("utf-8")).hexdigest() != str(captures[arg]).lower():
                return fail(f"SHA-256 differs from capture {arg}")
        if key == "~notRef":
            if not isinstance(arg, str) or arg not in captures:
                return fail(f"capture {arg!r} not bound")
            if _equal(captures[arg], observed):
                return fail(f"equals capture {arg}, expected a different value")
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
