"""The core link's `linkspec` block (docs/DEBUG_CORE_LINK.md) as a checker: parses the shapes and
validates request arguments, response bodies and event bodies against them.

WHY a parsed spec and not a hand-written schema here: the doc is the one spec both ends follow.
A schema copied into a test drifts from the doc the same way the fake core drifted from the real
core; parsing the doc makes the doc itself the thing the fake (pytest) and the real core
(runctl_e2e.py) are held to.

  python3 tests/debug/wire/link_spec.py    # parses the block, prints the message names

See also:
  docs/DEBUG_CORE_LINK.md - the spec, `linkspec` block and its shape grammar.
  tests/debug/wire/fake_core.py - validates every request it receives with check_request.
  tests/pytest/restapi/test_debug_gateway.py - test_the_fake_core_speaks_the_link_spec.
  tests/debug/plant/runctl_e2e.py - checks every frame the real core sends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

SPEC = Path(__file__).resolve().parents[3] / "docs" / "DEBUG_CORE_LINK.md"
LEAVES = ("str", "int", "num", "bool", "null", "obj", "list", "any")


@dataclass(frozen=True)
class Obj:
    fields: Tuple[Tuple[str, bool, "Shape"], ...]  # (name, optional, shape)
    open: bool


@dataclass(frozen=True)
class Lst:
    item: "Shape"


@dataclass(frozen=True)
class Alt:
    options: Tuple["Shape", ...]


Shape = Union[Obj, Lst, Alt, str]  # str: a leaf type or a defined name


_TOKEN = re.compile(r"\s*(->|\.\.\.|[{}\[\],:?|]|[A-Za-z_][A-Za-z0-9_]*)")


def _tokens(text: str) -> List[str]:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None:
            raise ValueError(f"linkspec: cannot read {text[pos:]!r}")
        out.append(m.group(1))
        pos = m.end()
    return out


class _Parser:
    def __init__(self, toks: List[str]) -> None:
        self.t, self.i = toks, 0

    def peek(self) -> Optional[str]:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, want: Optional[str] = None) -> str:
        tok = self.peek()
        if tok is None or (want is not None and tok != want):
            raise ValueError(f"linkspec: expected {want!r}, found {tok!r} in {' '.join(self.t)}")
        self.i += 1
        return tok

    def shape(self) -> Shape:
        options = [self.single()]
        while self.peek() == "|":
            self.take("|")
            options.append(self.single())
        return options[0] if len(options) == 1 else Alt(tuple(options))

    def single(self) -> Shape:
        tok = self.peek()
        if tok == "{":
            self.take("{")
            fields: List[Tuple[str, bool, Shape]] = []
            is_open = False
            while self.peek() != "}":
                if self.peek() == "...":
                    self.take("...")
                    is_open = True
                else:
                    name = self.take()
                    optional = self.peek() == "?"
                    if optional:
                        self.take("?")
                    self.take(":")
                    fields.append((name, optional, self.shape()))
                if self.peek() == ",":
                    self.take(",")
            self.take("}")
            return Obj(tuple(fields), is_open)
        if tok == "[":
            self.take("[")
            item = self.shape()
            self.take("]")
            return Lst(item)
        return self.take()


@dataclass(frozen=True)
class Spec:
    types: Dict[str, Shape]
    requests: Dict[str, Tuple[Shape, Shape]]  # command -> (arguments, body)
    events: Dict[str, Shape]

    # -- validation; each returns a list of problems, empty when the value conforms

    def check_request(self, command: str, args: object) -> List[str]:
        if command not in self.requests:
            return [f"request {command!r} is not in the link spec"]
        return self._check(self.requests[command][0], args, f"{command}.arguments")

    def check_response(self, command: str, msg: Dict[str, object]) -> List[str]:
        where = f"{command} response"
        if command not in self.requests:
            # A command outside the link is answered UNSUPPORTED (pm ruling 3), never served.
            error = msg.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if msg.get("success") is False and code == "UNSUPPORTED":
                return self._check(self.types["Error"], error, f"{where}.error")
            return [f"{where}: {command!r} is not in the link spec, yet answered {code or 'success'}"]
        problems = [f"{where}: {k} missing" for k in ("seq", "request_seq") if not isinstance(msg.get(k), int)]
        if msg.get("type") != "response":
            problems.append(f"{where}: type {msg.get('type')!r}")
        if msg.get("success") is True:
            return problems + self._check(self.requests[command][1], msg.get("body"), f"{where}.body")
        if msg.get("success") is False:
            return problems + self._check(self.types["Error"], msg.get("error"), f"{where}.error")
        return problems + [f"{where}: success is {msg.get('success')!r}"]

    def check_event(self, name: str, body: object) -> List[str]:
        if name not in self.events:
            return [f"event {name!r} is not in the link spec"]
        return self._check(self.events[name], body, f"{name}")

    def check_frame(self, msg: Dict[str, object], command_of: Dict[int, str]) -> List[str]:
        """One frame the core sent; `command_of` maps request seq to command for responses."""
        if msg.get("type") == "event":
            return self.check_event(str(msg.get("event")), msg.get("body"))
        if msg.get("type") == "response":
            rseq = msg.get("request_seq")
            command = command_of.get(rseq) if isinstance(rseq, int) else None
            if command is None:
                return [f"response to unknown request_seq {rseq!r}"]
            if msg.get("command") not in (None, command):
                return [f"response to {command} echoes command {msg.get('command')!r}"]
            return self.check_response(command, msg)
        return [f"frame type {msg.get('type')!r}"]

    def _check(self, shape: Shape, value: object, path: str) -> List[str]:
        if isinstance(shape, Alt):
            for option in shape.options:
                if not self._check(option, value, path):
                    return []
            return [f"{path}: {_show(value)} matches none of {len(shape.options)} alternatives"]
        if isinstance(shape, Lst):
            if not isinstance(value, list):
                return [f"{path}: expected a list, got {_show(value)}"]
            return [p for i, v in enumerate(value) for p in self._check(shape.item, v, f"{path}[{i}]")]
        if isinstance(shape, Obj):
            if not isinstance(value, dict):
                return [f"{path}: expected an object, got {_show(value)}"]
            problems: List[str] = []
            names = set()
            for name, optional, sub in shape.fields:
                names.add(name)
                if name not in value:
                    if not optional:
                        problems.append(f"{path}.{name}: missing")
                    continue
                problems += self._check(sub, value[name], f"{path}.{name}")
            if not shape.open:
                problems += [f"{path}.{k}: not in the spec" for k in value if k not in names]
            return problems
        if shape in self.types:
            return self._check(self.types[shape], value, path)
        ok = {
            "str": isinstance(value, str),
            "int": isinstance(value, int) and not isinstance(value, bool),
            "num": isinstance(value, (int, float)) and not isinstance(value, bool),
            "bool": isinstance(value, bool),
            "null": value is None,
            "obj": isinstance(value, dict),
            "list": isinstance(value, list),
            "any": True,
        }.get(shape)
        if ok is None:
            raise ValueError(f"linkspec: unknown type {shape!r}")
        return [] if ok else [f"{path}: expected {shape}, got {_show(value)}"]


def _show(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def load(path: Path = SPEC, block: str = "linkspec") -> Spec:
    """`block` names the fenced block: `linkspec` (the core link) or `sessionspec` (the public
    session protocol in docs/DEBUG_PROTOCOL.md), both in this grammar."""
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"```{block}\n(.*?)```", text, re.S)
    if m is None:
        raise ValueError(f"{path}: no {block} block")
    types: Dict[str, Shape] = {}
    requests: Dict[str, Tuple[Shape, Shape]] = {}
    events: Dict[str, Shape] = {}
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line:
            continue
        head = line.split(None, 2)
        if len(head) >= 3 and head[1] == "=":
            types[head[0]] = _Parser(_tokens(line.split("=", 1)[1])).shape()
        elif len(head) == 3 and head[1] == "evt":
            events[head[0]] = _whole(head[2])
        elif len(head) == 3 and head[1] == "req":
            args, _, body = head[2].partition("->")
            requests[head[0]] = (_whole(args), _whole(body))
        else:
            raise ValueError(f"linkspec: cannot read line {line!r}")
    shapes = list(types.values()) + list(events.values()) + [s for r in requests.values() for s in r]
    for shape in shapes:
        _names_known(shape, types)
    return Spec(types, requests, events)


def _names_known(shape: Shape, types: Dict[str, Shape]) -> None:
    if isinstance(shape, Alt):
        for option in shape.options:
            _names_known(option, types)
    elif isinstance(shape, Lst):
        _names_known(shape.item, types)
    elif isinstance(shape, Obj):
        for _name, _optional, sub in shape.fields:
            _names_known(sub, types)
    elif shape not in LEAVES and shape not in types:
        raise ValueError(f"linkspec: unknown type {shape!r}")


def _whole(text: str) -> Shape:
    p = _Parser(_tokens(text))
    shape = p.shape()
    if p.peek() is not None:
        raise ValueError(f"linkspec: trailing {p.peek()!r} in {text!r}")
    return shape


if __name__ == "__main__":
    s = load()
    print("requests:", " ".join(s.requests))
    print("events:", " ".join(s.events))
