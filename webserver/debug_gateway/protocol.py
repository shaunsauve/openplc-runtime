# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Session protocol v1: version constant, error-code catalogue, request table and envelopes.

The request table is the single source for dispatch and for the handshake's advertisement, so a
request is advertised exactly when it has a handler and its capabilities are present.

See also:
  webserver/debug_gateway/gateway.py - dispatch and events over this table.
  docs/DEBUG_PROTOCOL.md - "Session protocol v1", the spec this module implements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Mapping, Optional, Tuple, Union

Json = Union[None, bool, int, float, str, List["Json"], Dict[str, "Json"]]
JsonObj = Dict[str, Json]

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
PROTOCOL_VERSION = f"{PROTOCOL_MAJOR}.{PROTOCOL_MINOR}"
NAMESPACE = "/api/debug-session"
EVENT_NAME = "msg"
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_OBSERVERS = 16


class ErrorCode(str, Enum):
    PROTOCOL_VERSION = "PROTOCOL_VERSION"
    UNAUTHORISED = "UNAUTHORISED"
    FORBIDDEN = "FORBIDDEN"
    NOT_CONTROLLER = "NOT_CONTROLLER"
    LOCKED = "LOCKED"
    UNSUPPORTED = "UNSUPPORTED"
    NO_PROBES = "NO_PROBES"
    STALE_BUILD = "STALE_BUILD"
    UNSUPPORTED_DEBUG_INFO = "UNSUPPORTED_DEBUG_INFO"
    INVALID_LOCATION = "INVALID_LOCATION"
    NO_CODE = "NO_CODE"
    NOT_HALTED = "NOT_HALTED"
    NOT_RUNNING = "NOT_RUNNING"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    BAD_REQUEST = "BAD_REQUEST"
    TYPE_ERROR = "TYPE_ERROR"
    RANGE_ERROR = "RANGE_ERROR"
    EXPR_SIDE_EFFECT = "EXPR_SIDE_EFFECT"
    EXPR_LIMIT = "EXPR_LIMIT"
    EXPR_SYNTAX = "EXPR_SYNTAX"
    NO_SOURCE = "NO_SOURCE"
    BUSY = "BUSY"
    SESSION_ENDED = "SESSION_ENDED"
    INTERNAL = "INTERNAL"
    SAFE_STOPPED = "SAFE_STOPPED"
    CHANGE_REFUSED = "CHANGE_REFUSED"
    CHANGE_FAILED = "CHANGE_FAILED"


DEFAULT_MESSAGES: Mapping[ErrorCode, str] = {
    ErrorCode.PROTOCOL_VERSION: "The client and the runtime speak incompatible protocol versions.",
    ErrorCode.UNAUTHORISED: "The request carries no valid login token.",
    ErrorCode.FORBIDDEN: "This user does not have the permission this request needs.",
    ErrorCode.NOT_CONTROLLER: "Only the session's single controller may make this request.",
    ErrorCode.LOCKED: "Debugging control is locked on this runtime.",
    ErrorCode.UNSUPPORTED: "This runtime build does not support the request.",
    ErrorCode.NO_PROBES: "The running program was built without debug probes.",
    ErrorCode.STALE_BUILD: "The client's build does not match the program the runtime is running.",
    ErrorCode.UNSUPPORTED_DEBUG_INFO: "The program's debug information has an unsupported version.",
    ErrorCode.INVALID_LOCATION: "There is no such source file or line in the running program.",
    ErrorCode.NO_CODE: "There is no executable statement at or after that line.",
    ErrorCode.NOT_HALTED: "The request needs the task to be halted, and it is not.",
    ErrorCode.NOT_RUNNING: "The request needs the program to be running, and it is not.",
    ErrorCode.LIMIT_EXCEEDED: "The request exceeds a size or count limit of the runtime.",
    ErrorCode.BAD_REQUEST: "The message is malformed or arrived out of order.",
    ErrorCode.TYPE_ERROR: "The value does not have the variable's type.",
    ErrorCode.RANGE_ERROR: "The value is outside the variable's allowed range.",
    ErrorCode.EXPR_SIDE_EFFECT: "The expression would change program state, which is not allowed.",
    ErrorCode.EXPR_LIMIT: "The expression exceeds the evaluation limits.",
    ErrorCode.EXPR_SYNTAX: "The expression is not valid Structured Text.",
    ErrorCode.NO_SOURCE: "The runtime cannot supply the source of this file.",
    ErrorCode.BUSY: "The runtime is busy with another transition; try again shortly.",
    ErrorCode.SESSION_ENDED: "This debug session has ended.",
    ErrorCode.INTERNAL: "The runtime hit an internal error; the debug log has the details.",
    ErrorCode.SAFE_STOPPED: "The runtime is safe-stopped, so no program is running to change.",
    ErrorCode.CHANGE_REFUSED: "The online change was refused; the program was not changed.",
    ErrorCode.CHANGE_FAILED: "The online change failed and was rolled back.",
}


class Kind(str, Enum):
    """Gating class of a request: CONTROL and MODIFY need the controller and the lock off."""

    SESSION = "session"
    READ = "read"
    CONTROL = "control"
    MODIFY = "modify"
    ADMIN = "admin"


@dataclass(frozen=True)
class Arg:
    """One request argument: `kind` is str, int, bool, obj, list[int] or list[str]."""

    kind: str
    required: bool = False
    choices: Tuple[str, ...] = ()
    minimum: Optional[int] = None


@dataclass(frozen=True)
class Command:
    name: str
    kind: Kind
    requires: FrozenSet[str] = frozenset()
    args: Mapping[str, Arg] = field(default_factory=dict)
    probe_dependent: bool = False
    local: bool = False  # answered by the gateway; otherwise forwarded to the core link


_TASK = {"task": Arg("str")}


def _cmd(
    name: str,
    kind: Kind,
    requires: Tuple[str, ...] = (),
    args: Optional[Mapping[str, Arg]] = None,
    probe_dependent: bool = False,
    local: bool = False,
) -> Command:
    return Command(name, kind, frozenset(requires), dict(args or {}), probe_dependent, local)


COMMANDS: Mapping[str, Command] = {
    c.name: c
    for c in (
        _cmd(
            "initialize",
            Kind.SESSION,
            args={
                "protocol": Arg("str", required=True),
                "role": Arg("str", choices=("controller", "observer")),
                "token": Arg("str"),
                "identity": Arg("obj"),
                "resumeToken": Arg("str"),
            },
            local=True,
        ),
        _cmd("attach", Kind.SESSION, local=True),
        _cmd("detach", Kind.SESSION, local=True),
        _cmd("terminate", Kind.CONTROL, ("control",), local=True),
        _cmd("heartbeat", Kind.SESSION, local=True),
        _cmd("status", Kind.READ, local=True),
        _cmd("version", Kind.READ, local=True),
        _cmd("setLock", Kind.ADMIN, args={"on": Arg("bool", True), "reason": Arg("str")}, local=True),
        _cmd(
            "setBreakpoints",
            Kind.CONTROL,
            ("control",),
            {"file": Arg("str", True), "lines": Arg("list[int]", True)},
            probe_dependent=True,
            local=True,
        ),
        _cmd(
            "breakpointLocations",
            Kind.READ,
            (),
            {"file": Arg("str", True), "line": Arg("int", True, minimum=1), "endLine": Arg("int")},
            probe_dependent=True,
            local=True,
        ),
        _cmd("clearAllBreakpoints", Kind.CONTROL, ("control",)),
        _cmd(
            "setExceptionBreakpoints",
            Kind.CONTROL,
            ("control", "exceptionBreakpoints"),
            {"filters": Arg("list[str]", True)},
        ),
        _cmd("continue", Kind.CONTROL, ("control",), _TASK),
        _cmd("pause", Kind.CONTROL, ("control",), _TASK),
        _cmd("next", Kind.CONTROL, ("control",), _TASK),
        _cmd("stepIn", Kind.CONTROL, ("control",), _TASK),
        _cmd("stepOut", Kind.CONTROL, ("control",), _TASK),
        _cmd("cycleStep", Kind.CONTROL, ("control", "cycleStep"), _TASK),
        _cmd("runScans", Kind.CONTROL, ("control", "runScans"), {**_TASK, "n": Arg("int", True, minimum=1)}),
        _cmd(
            "runToCursor",
            Kind.CONTROL,
            ("control",),
            {**_TASK, "file": Arg("str", True), "line": Arg("int", True, minimum=1)},
            probe_dependent=True,
            local=True,
        ),
        _cmd("stop", Kind.CONTROL, ("control",)),
        # Inspection of a halted task (ADR-001 D3, ADR-002 D3): answered by the gateway from the
        # core's stackTrace and readLeaves and the program's debug-info.json.
        _cmd(
            "stackTrace",
            Kind.READ,
            ("probes",),
            {**_TASK, "startFrame": Arg("int", minimum=0), "levels": Arg("int", minimum=0)},
            probe_dependent=True,
            local=True,
        ),
        _cmd("scopes", Kind.READ, ("probes",), {"frameId": Arg("int", True, minimum=0)},
             probe_dependent=True, local=True),
        _cmd(
            "variables",
            Kind.READ,
            ("probes",),
            {"variablesReference": Arg("int", minimum=1), "path": Arg("str"),
             "frameId": Arg("int", minimum=0)},
            probe_dependent=True,
            local=True,
        ),
        _cmd(
            "restart",
            Kind.CONTROL,
            ("control",),
            {"kind": Arg("str", choices=("warm", "cold", "reset")), "startHalted": Arg("bool")},
        ),
        _cmd(
            "setHaltMode",
            Kind.CONTROL,
            ("control", "nonStop"),
            {"mode": Arg("str", True, choices=("allStop", "nonStop"))},
        ),
    )
}

# Capability flags (ADR-003 D3.2). Those in SERVED_FLAGS are advertised as the core reports
# them; the rest are false until the gateway serves their requests.
CAPABILITY_FLAGS: Tuple[str, ...] = (
    "probes",
    "maxBreakpoints",
    "conditional",
    "hitCount",
    "logpoints",
    "functionBreakpoints",
    "instanceBreakpoints",
    "exceptionBreakpoints",
    "nonStop",
    "cycleStep",
    "runScans",
    "startHalted",
    "write",
    "force",
    "trace",
    "liveWatch",
    "sourceFetch",
    "onlineChange",
    "control",
    "modify",
)
SERVED_FLAGS: FrozenSet[str] = frozenset(
    {"probes", "maxBreakpoints", "exceptionBreakpoints", "nonStop", "cycleStep", "runScans",
     "startHalted", "control"}
)


def advertised_capabilities(core_caps: JsonObj) -> JsonObj:
    out: JsonObj = {}
    for flag in CAPABILITY_FLAGS:
        value = core_caps.get(flag)
        if flag == "maxBreakpoints":
            n = value if isinstance(value, int) and not isinstance(value, bool) else 0
            out[flag] = n if flag in SERVED_FLAGS and core_caps.get("probes") is True else 0
        else:
            out[flag] = flag in SERVED_FLAGS and value is True
    return out


def advertised_requests(caps: JsonObj) -> List[str]:
    return [c.name for c in COMMANDS.values() if all(caps.get(f) is True for f in c.requires)]


class Refusal(Exception):
    """A typed failure response; raised anywhere in dispatch."""

    def __init__(
        self, code: ErrorCode, message: Optional[str] = None, details: Optional[JsonObj] = None
    ) -> None:
        super().__init__(message or DEFAULT_MESSAGES[code])
        self.code = code
        self.message = message or DEFAULT_MESSAGES[code]
        self.details = details or {}


def check_args(command: Command, args: JsonObj) -> JsonObj:
    """The declared arguments only, type-checked; anything undeclared is dropped."""
    out: JsonObj = {}
    for name, spec in command.args.items():
        if name not in args or args[name] is None:
            if spec.required:
                raise Refusal(ErrorCode.BAD_REQUEST, f"The argument '{name}' is required.")
            continue
        value = args[name]
        if not _has_kind(value, spec.kind):
            raise Refusal(
                ErrorCode.BAD_REQUEST, f"The argument '{name}' must be of type {spec.kind}."
            )
        if spec.choices and value not in spec.choices:
            raise Refusal(
                ErrorCode.BAD_REQUEST,
                f"The argument '{name}' must be one of {', '.join(spec.choices)}.",
            )
        if spec.minimum is not None and isinstance(value, int) and value < spec.minimum:
            raise Refusal(
                ErrorCode.BAD_REQUEST, f"The argument '{name}' must be at least {spec.minimum}."
            )
        out[name] = value
    return out


def _is_int(v: Json) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _has_kind(value: Json, kind: str) -> bool:
    if kind == "str":
        return isinstance(value, str)
    if kind == "int":
        return _is_int(value)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "obj":
        return isinstance(value, dict)
    if kind == "list[int]":
        return isinstance(value, list) and all(_is_int(v) for v in value)
    if kind == "list[str]":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    raise ValueError(f"unknown argument kind {kind}")


def parse_version(text: str) -> Optional[Tuple[int, int]]:
    major, sep, minor = text.partition(".")
    if not sep or not major.isdigit() or not minor.isdigit():
        return None
    return int(major), int(minor)
