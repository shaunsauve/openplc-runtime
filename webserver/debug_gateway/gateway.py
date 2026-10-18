# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""The debug session gateway: Socket.IO namespace /api/debug-session (ADR-003 D1.1), one event
name `msg` carrying DAP-shaped requests, correlated responses and pushed events (D2, D4), the
handshake and version report (D3), translated to and from the internal core link (D1.3).

See also:
  webserver/debug_gateway/protocol.py - request table, refusal catalogue, envelopes.
  webserver/debug_gateway/core_link.py - the core link and its CoreLink interface.
  docs/DEBUG_CORE_LINK.md - the core link's messages, and the pm rulings applied here.
  webserver/debug_gateway/debug_info.py - line/probe resolution from debug-info.json.
  webserver/debug_gateway/session_guard.py - P1/P2 from debug.conf and resume tokens.
  webserver/debug_websocket.py - the legacy /api/debug namespace on the same Socket.IO instance.
  docs/DEBUG_PROTOCOL.md - "Session protocol v1", the spec.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from flask import request
from flask_jwt_extended import decode_token, get_current_user, verify_jwt_in_request
from flask_jwt_extended.exceptions import JWTExtendedException
from flask_socketio import SocketIO
from jwt.exceptions import PyJWTError

from webserver.debug_gateway.core_link import CoreLink, CoreUnavailable
from webserver.debug_gateway.debug_info import SCOPE_OF_SECTION, DebugInfo, Node, Pou
from webserver.debug_gateway.protocol import (
    COMMANDS,
    DEFAULT_MESSAGES,
    EVENT_NAME,
    MAX_MESSAGE_BYTES,
    MAX_OBSERVERS,
    NAMESPACE,
    PROTOCOL_MAJOR,
    PROTOCOL_VERSION,
    ErrorCode,
    Json,
    JsonObj,
    Kind,
    Refusal,
    advertised_capabilities,
    advertised_requests,
    check_args,
    parse_version,
)
from webserver.debug_gateway.session_guard import (
    CONF_NAME,
    P5_DEFAULT_S,
    P5_MAX_S,
    ConfigError,
    HeartbeatParams,
    load_params,
    mint_resume_token,
    resume_hash,
)
from webserver.logger import get_logger
from webserver.restapi import User, check_if_token_revoked, user_lookup_callback
from webserver.version import RUNTIME_VERSION

logger, _ = get_logger("debug_gw", use_buffer=True)

OBSERVER_EVENTS = frozenset(
    {"stopped", "continued", "runState", "output", "values", "time", "control", "sessionEnded",
     "programReplaced", "forces"}
)
_CODES = {c.value: c for c in ErrorCode}
# A restart stops the program and starts it again, so the core answers it only once the new
# program is RUNNING. The link's default 2 s is shorter than a program's own stop budget
# (plc_stop_budget_ms), which would turn a slow-but-fine restart into a lost link.
_CORE_TIMEOUT_S: Mapping[str, float] = {"restart": 120.0}
# frameId = taskId * _FRAMES_PER_TASK + the frame's index, innermost 0. Valid while the task stays
# halted on the stop it was listed for, as DAP's frame ids are.
_FRAMES_PER_TASK = 4096
_MAX_LEAVES = 1024  # per core readLeaves request (docs/DEBUG_CORE_LINK.md)
WATCH_TICK_S = 0.1  # P2 expiry granularity (ADR-005 D4.6: within P2 + 1 s)
ENDED_TOKENS_KEPT = 256


def _obj(v: Json) -> JsonObj:
    return v if isinstance(v, dict) else {}


def _list(v: Json) -> List[Json]:
    return v if isinstance(v, list) else []


def _int(v: Json) -> int:
    """For arguments check_args has already typed as int."""
    if not isinstance(v, int):
        raise Refusal(ErrorCode.BAD_REQUEST, "A line number must be an integer.")
    return v


@dataclass
class ClientSession:
    sid: str
    user: str
    is_admin: bool
    role: str
    token: str
    comparison: str
    core_session: Json = None
    resume_hash: str = ""
    # Any message from the session's client is a heartbeat (ADR-005 D4.3).
    last_seen: float = field(default_factory=time.monotonic)
    # When a beat last reached the core's own P2 timer (ADR-005 D4.4).
    core_beat: float = field(default_factory=time.monotonic)


class DebugGateway:
    def __init__(
        self,
        socketio: SocketIO,
        link: CoreLink,
        debug_info_path: Path,
        runtime_version: str = RUNTIME_VERSION,
        params: HeartbeatParams = HeartbeatParams(),
        p5_s: float = P5_DEFAULT_S,
    ) -> None:
        self.socketio = socketio
        self.link = link
        self.debug_info_path = debug_info_path
        self.runtime_version = runtime_version
        self.sessions: Dict[str, ClientSession] = {}
        self.ended: Dict[str, str] = {}
        self.ended_tokens: Dict[str, str] = {}  # resume hash -> why its session ended
        self.params = params
        # ADR-005 D5.1: P5, the TTL a force with no `ttl` of its own gets.
        self.p5_s = p5_s
        self._closed = threading.Event()
        self.locked = False
        self.lock = threading.Lock()
        self._seq = 0
        self._info: Optional[DebugInfo] = None
        self._info_stamp: Optional[Tuple[int, int]] = None
        # variablesReference -> what it expands; stable per (debug info, key), never reused.
        self._refs: Dict[int, Tuple[str, ...]] = {}
        self._ref_ids: Dict[Tuple[str, ...], int] = {}
        # flat leaf index -> the force the core holds on it, for `variables`'s `forced` mark.
        # The core owns the registry and pushes a `forces` event on every change, so this is
        # kept in step by _on_core_event and by every listForces reply.
        self._forced: Dict[int, JsonObj] = {}
        link.set_event_sink(self._on_core_event)

    # ------------------------------------------------------------------ transport

    def register(self) -> None:
        eio = self.socketio.server.eio
        # The engine must accept frames above MAX_MESSAGE_BYTES so that the gateway, not the
        # transport, refuses them with LIMIT_EXCEEDED and a correlated response.
        eio.max_http_buffer_size = max(eio.max_http_buffer_size, 4 * MAX_MESSAGE_BYTES)
        self.socketio.on_event("connect", self._on_connect, namespace=NAMESPACE)
        self.socketio.on_event("disconnect", self._on_disconnect, namespace=NAMESPACE)
        self.socketio.on_event(EVENT_NAME, self._on_msg, namespace=NAMESPACE)
        threading.Thread(target=self._watch, name="debug-session-p2", daemon=True).start()

    def close(self) -> None:
        self._closed.set()

    def _on_connect(self, auth: Json = None) -> bool:
        del auth  # the JWT travels in initialize (ADR-005 D1.1)
        # ADR-005 D2.2: TLS only, loopback included; a plaintext upgrade never gets a namespace.
        if not request.is_secure:
            logger.warning("Debug session refused: plaintext from %s", request.remote_addr)
            return False
        return True

    def _on_disconnect(self, reason: Json = None) -> None:
        sid = str(request.sid)
        with self.lock:
            self.ended.pop(sid, None)
            session = self.sessions.get(sid)
        if session is None:
            return
        # A client's own Socket.IO disconnect is a leave. A dropped transport is not loss
        # (ADR-005 D4.5): the session stays for P2, resumable by its token, then _watch ends it.
        if reason == self.socketio.server.reason.CLIENT_DISCONNECT:
            self._retire(session, "detached")
            self._core_detach(session, "disconnect")
            self._broadcast_control()
        else:
            # The reconnect window is P2 from the drop, so a loss never ends a session early.
            with self.lock:
                session.last_seen = max(session.last_seen, time.monotonic())
            logger.info("Debug session of %s lost its transport (%s)", session.user, reason)

    def _beat_core(self, session: ClientSession) -> None:
        """ADR-005 D4.3/D4.4: any client message is a heartbeat, and the core runs the timer that
        survives this process. Requests answered here never reach the core, so a beat is forwarded
        at most once per P1 for whatever the client sends."""
        now = time.monotonic()
        if session.core_session is None or now - session.core_beat < self.params.p1_s:
            return
        session.core_beat = now
        try:
            self._core("beatClient", {"session": session.core_session})
        except (Refusal, CoreUnavailable):
            pass  # a lost or ended core session is reported by its own sessionEnded

    def _watch(self) -> None:
        while not self._closed.wait(WATCH_TICK_S):
            now = time.monotonic()
            with self.lock:
                lost = [s for s in self.sessions.values() if now - s.last_seen > self.params.p2_s]
            for session in lost:
                logger.warning("Debug session of %s timed out after P2 = %g s", session.user,
                               self.params.p2_s)
                self._end(session, "heartbeat_timeout")

    def _on_msg(self, data: Json) -> None:
        sid = str(request.sid)
        self._send(sid, self.handle(sid, data))

    def _next_seq(self) -> int:
        with self.lock:
            self._seq += 1
            return self._seq

    def _send(self, sid: str, msg: JsonObj) -> None:
        self.socketio.emit(EVENT_NAME, {**msg, "seq": self._next_seq()}, namespace=NAMESPACE, to=sid)

    def _event(self, sid: str, event: str, body: JsonObj) -> None:
        self._send(sid, {"type": "event", "event": event, "body": body})

    # ------------------------------------------------------------------ requests

    def handle(self, sid: str, raw: object) -> JsonObj:
        request_seq: Json = None
        command: Json = None
        with self.lock:
            live = self.sessions.get(sid)
            if live is not None:
                live.last_seen = time.monotonic()
        if live is not None:
            self._beat_core(live)
        try:
            msg, size = self._parse(raw)
            seq = msg.get("seq")
            request_seq = seq if isinstance(seq, int) and not isinstance(seq, bool) else None
            command = msg.get("command") if isinstance(msg.get("command"), str) else None
            if size > MAX_MESSAGE_BYTES:
                raise Refusal(
                    ErrorCode.LIMIT_EXCEEDED,
                    f"The message is {size} bytes; the limit is {MAX_MESSAGE_BYTES} bytes.",
                    {"limit": MAX_MESSAGE_BYTES, "size": size},
                )
            args = msg.get("arguments", {})
            if msg.get("type") != "request" or request_seq is None or command is None:
                raise Refusal(
                    ErrorCode.BAD_REQUEST, "A request needs type 'request', an integer seq and a command."
                )
            if not isinstance(args, dict):
                raise Refusal(ErrorCode.BAD_REQUEST, "The request's arguments must be an object.")
            body = self.dispatch(sid, str(command), args)
            return {
                "type": "response",
                "request_seq": request_seq,
                "command": command,
                "success": True,
                "body": body,
            }
        except Refusal as r:
            return self._failure(request_seq, command, r)
        except CoreUnavailable as exc:
            logger.warning("Debug request %s: %s", command, exc)
            return self._failure(
                request_seq,
                command,
                Refusal(ErrorCode.INTERNAL, "The runtime core is not reachable over its debug link."),
            )

    @staticmethod
    def _parse(raw: object) -> Tuple[JsonObj, int]:
        # Anything a client can put on the wire ends here as a Refusal, never as an exception:
        # binary payloads, invalid UTF-8, JSON nested past the parser's recursion limit.
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = bytes(raw).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise Refusal(ErrorCode.BAD_REQUEST, "The message is not UTF-8 JSON text.") from exc
        try:
            if isinstance(raw, str):
                size = len(raw.encode("utf-8"))
                raw = json.loads(raw)
            else:
                size = len(json.dumps(raw, separators=(",", ":")).encode("utf-8"))
        except (ValueError, TypeError, RecursionError) as exc:
            raise Refusal(ErrorCode.BAD_REQUEST, "The message is not valid JSON.") from exc
        if not isinstance(raw, dict):
            raise Refusal(ErrorCode.BAD_REQUEST, "The message must be a JSON object.")
        return raw, size

    @staticmethod
    def _failure(request_seq: Json, command: Json, r: Refusal) -> JsonObj:
        error: JsonObj = {"code": r.code.value, "message": r.message}
        if r.details:
            error["details"] = r.details
        return {
            "type": "response",
            "request_seq": request_seq,
            "command": command,
            "success": False,
            "error": error,
        }

    def dispatch(self, sid: str, command: str, args: JsonObj) -> JsonObj:
        with self.lock:
            session = self.sessions.get(sid)
            ended = self.ended.get(sid)
        if session is None and command != "initialize":
            if ended is not None:
                raise Refusal(
                    ErrorCode.SESSION_ENDED,
                    f"This debug session has ended ({ended}); initialize a new one.",
                    {"reason": ended},
                )
            raise Refusal(ErrorCode.BAD_REQUEST, "The first request of a session must be initialize.")
        hello = self.link.hello()
        caps = advertised_capabilities(_obj(hello.get("capabilities")) if hello else {})
        cmd = COMMANDS.get(command)
        if cmd is None or not all(caps.get(flag) is True for flag in cmd.requires):
            raise Refusal(
                ErrorCode.UNSUPPORTED,
                f"This runtime does not support the request '{command}'.",
                {"command": command},
            )
        args = check_args(cmd, args)
        if session is None:
            return self._initialize(sid, args, hello, caps)
        if cmd.kind in (Kind.CONTROL, Kind.MODIFY):
            if session.role != "controller":
                raise Refusal(
                    ErrorCode.NOT_CONTROLLER,
                    f"An observer cannot make the '{command}' request; only the controller can.",
                )
            self._reverify(session)
            if self.locked:
                raise Refusal(ErrorCode.LOCKED, "Debugging control is locked on this runtime.")
        if cmd.kind == Kind.ADMIN and not session.is_admin:
            raise Refusal(
                ErrorCode.FORBIDDEN, f"The '{command}' request needs an administrator account."
            )
        info = self._probe_check(session, hello) if cmd.probe_dependent else None
        if command == "initialize":
            return self._initialize(sid, args, hello, caps)
        handler = self._local_handlers().get(command) if cmd.local else None
        if handler is not None:
            return handler(session, args, info)
        return self._core(command, {**args, "session": session.core_session})

    def _local_handlers(
        self,
    ) -> Dict[str, Callable[[ClientSession, JsonObj, Optional[DebugInfo]], JsonObj]]:
        return {
            "attach": lambda s, a, i: {"role": s.role},
            "detach": lambda s, a, i: self._detach(s),
            "terminate": lambda s, a, i: self._end_by_request(s, "terminate"),
            "heartbeat": lambda s, a, i: self._core("beatClient", {"session": s.core_session}),
            "status": lambda s, a, i: {
                k: v for k, v in self._core("status", {}).items() if k != "sessions"
            },
            "version": lambda s, a, i: self._versions(self.link.hello()),
            "setLock": self._set_lock,
            "setBreakpoints": self._set_breakpoints,
            "breakpointLocations": self._breakpoint_locations,
            "runToCursor": self._run_to_cursor,
            "stackTrace": self._stack_trace,
            "scopes": self._scopes,
            "variables": self._variables,
            "force": self._force,
            "unforce": self._unforce,
            "unforceAll": self._unforce_all,
            "listForces": self._list_forces,
        }

    def _core(self, command: str, args: JsonObj) -> JsonObj:
        timeout = _CORE_TIMEOUT_S.get(command)
        reply = (
            self.link.request(command, args)
            if timeout is None
            else self.link.request(command, args, timeout)
        )
        if reply.success:
            return reply.body
        code = _CODES.get(reply.code or "")
        if code is None:
            logger.error("Debug core answered %s with an uncatalogued code %r", command, reply.code)
            raise Refusal(ErrorCode.INTERNAL)
        raise Refusal(code, reply.message or DEFAULT_MESSAGES[code], reply.details)

    # ------------------------------------------------------------------ handshake

    def _initialize(
        self, sid: str, args: JsonObj, hello: Optional[JsonObj], caps: JsonObj
    ) -> JsonObj:
        client_version = str(args["protocol"])
        parsed = parse_version(client_version)
        if parsed is None:
            raise Refusal(ErrorCode.BAD_REQUEST, "The protocol version must look like '1.0'.")
        if parsed[0] != PROTOCOL_MAJOR:
            raise Refusal(
                ErrorCode.PROTOCOL_VERSION,
                f"The client speaks protocol {client_version}; this runtime speaks "
                f"{PROTOCOL_VERSION}.",
                {"client": client_version, "server": PROTOCOL_VERSION},
            )
        if hello is None:
            raise CoreUnavailable("no hello from the core")
        if "resumeToken" in args:
            return self._resume(sid, str(args["resumeToken"]), hello, caps)
        user, is_admin, token = self._authenticate(args.get("token"))
        role = str(args.get("role") or "controller")
        image = _obj(hello.get("image"))
        session = ClientSession(
            sid, user, is_admin, role, token, _compare(_obj(args.get("identity")), image)
        )
        resume_token, session.resume_hash = mint_resume_token()
        with self.lock:
            old = self.sessions.pop(sid, None)
        if old is not None:
            self._core_detach(old, "reinitialize")
        with self.lock:
            others = [s for s in self.sessions.values() if s.sid != sid]
            if role == "controller" and any(s.role == "controller" for s in others):
                holder = next(s.user for s in others if s.role == "controller")
                raise Refusal(
                    ErrorCode.NOT_CONTROLLER,
                    f"User '{holder}' is already the controller; attach as an observer.",
                    {"controller": holder},
                )
            if role == "observer" and sum(s.role == "observer" for s in others) >= MAX_OBSERVERS:
                raise Refusal(
                    ErrorCode.BUSY,
                    f"This runtime already has {MAX_OBSERVERS} observers attached.",
                    {"reason": "observer_limit"},
                )
            self.sessions[sid] = session  # reserves the slot while the core attaches
            self.ended.pop(sid, None)
        try:
            session.core_session = self._core(
                "attach", {"user": user, "role": role, "p2": self.params.p2_s}
            ).get("session")
        except (Refusal, CoreUnavailable):
            with self.lock:
                self.sessions.pop(sid, None)
            raise
        self._broadcast_control()
        return self._handshake(hello, caps, session, resume_token)

    def _handshake(
        self, hello: JsonObj, caps: JsonObj, session: ClientSession, resume_token: str
    ) -> JsonObj:
        return {
            **self._versions(hello),
            "image": _obj(hello.get("image")),
            "comparison": session.comparison,
            # The gateway runs the P2 timer, so it reports its own P1/P2, not the core's.
            "params": self.params.as_json(),
            "capabilities": caps,
            "requests": advertised_requests(caps),
            "role": session.role,
            "locked": self.locked,
            "resumeToken": resume_token,
        }

    def _resume(self, sid: str, token: str, hello: JsonObj, caps: JsonObj) -> JsonObj:
        """ADR-005 D2.4: the resume token alone rebinds a session within P2, then rotates."""
        spent = resume_hash(token)
        with self.lock:
            session = next((s for s in self.sessions.values() if s.resume_hash == spent), None)
            ended = self.ended_tokens.get(spent)
        if session is None:
            if ended is not None:
                raise Refusal(
                    ErrorCode.SESSION_ENDED,
                    f"The debug session to resume has ended ({ended}); initialize a new one.",
                    {"reason": ended},
                )
            raise Refusal(ErrorCode.UNAUTHORISED, "The resume token is not valid.")
        if not self._login_valid(session.token):
            self._end(session, "revoked")
            raise Refusal(
                ErrorCode.SESSION_ENDED,
                "The debug session ended because its login was revoked or its account removed.",
                {"reason": "revoked"},
            )
        fresh, fresh_hash = mint_resume_token()
        with self.lock:
            if self.sessions.get(session.sid) is session:
                del self.sessions[session.sid]
            replaced = self.sessions.pop(sid, None)
            session.sid, session.resume_hash, session.last_seen = sid, fresh_hash, time.monotonic()
            self.sessions[sid] = session
            self.ended.pop(sid, None)
        if replaced is not None and replaced is not session:
            self._core_detach(replaced, "reinitialize")
        logger.info("Debug session of %s resumed", session.user)
        self._broadcast_control()
        return {**self._handshake(hello, caps, session, fresh), "resumed": True}

    def _versions(self, hello: Optional[JsonObj]) -> JsonObj:
        info = self._debug_info()
        return {
            "protocol": PROTOCOL_VERSION,
            "runtime": self.runtime_version,
            "compiler": info.compiler if info is not None else None,
            "debugInfo": info.version() if info is not None else None,
            "engineAbi": _obj(hello).get("engineAbi") if hello is not None else None,
        }

    def _authenticate(self, token: Json) -> Tuple[str, bool, str]:
        if not isinstance(token, str) or not token:
            raise Refusal(ErrorCode.UNAUTHORISED, "The initialize request carries no login token.")
        try:
            request.environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
            verify_jwt_in_request()
            user = get_current_user()
        except (JWTExtendedException, PyJWTError) as exc:
            logger.warning("Debug session refused at initialize: %s", exc)
            raise Refusal(
                ErrorCode.UNAUTHORISED, "The login token is invalid, expired or revoked."
            ) from exc
        if not isinstance(user, User):
            raise Refusal(ErrorCode.UNAUTHORISED, "The login token names no existing user.")
        return user.username, user.is_admin(), token

    @staticmethod
    def _login_valid(token: str) -> bool:
        """ADR-005 D2.1: revocation and account deletion end the session; expiry does not."""
        try:
            payload = decode_token(token, allow_expired=True)
            return (
                payload.get("type") == "access"
                and not check_if_token_revoked({}, payload)
                and user_lookup_callback({}, payload) is not None
            )
        except (JWTExtendedException, PyJWTError):
            return False

    def _reverify(self, session: ClientSession) -> None:
        if not self._login_valid(session.token):
            self._end(session, "revoked")
            raise Refusal(
                ErrorCode.SESSION_ENDED,
                "This debug session ended because its login was revoked or its account removed.",
                {"reason": "revoked"},
            )

    def _probe_check(self, session: ClientSession, hello: Optional[JsonObj]) -> DebugInfo:
        if _obj(_obj(hello).get("capabilities")).get("probes") is not True:
            raise Refusal(ErrorCode.NO_PROBES)
        info = self._debug_info()
        if info is None:
            raise Refusal(ErrorCode.NO_PROBES, "The running program has no debug information.")
        info.check_supported()
        image = _obj(_obj(hello).get("image"))
        # ADR-002 D1/D5: debug info binds to the loaded .so by buildId alone. A program with no
        # strucpp_dbg_build_id (built before STruCpp emitted it) reports null and is not bound:
        # md5 is the program's identity, not its build's.
        running = image.get("buildId")
        if not isinstance(running, str) or info.build_id != running:
            raise Refusal(
                ErrorCode.STALE_BUILD,
                "The debug information on the runtime does not belong to the running program; "
                "rebuild and upload it."
                if isinstance(running, str)
                else "The running program carries no build ID, so its debug information cannot "
                "be bound to it; rebuild and upload it with a current compiler.",
                {"reason": "debugInfo", "expected": running, "found": info.build_id},
            )
        if session.comparison in ("source", "build"):
            raise Refusal(
                ErrorCode.STALE_BUILD,
                "The client's build differs from the running program; initialize again with "
                "the running build.",
                {"comparison": session.comparison},
            )
        return info

    def _debug_info(self) -> Optional[DebugInfo]:
        try:
            st = self.debug_info_path.stat()
        except FileNotFoundError:
            self._info, self._info_stamp = None, None
            return None
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp != self._info_stamp:
            self._refs.clear()
            self._ref_ids.clear()
            # A new build is a new leaf space: a force the core still holds names leaves this
            # file cannot place, so the cached mapping goes with the rest.
            self._forced.clear()
            try:
                self._info = DebugInfo.load(self.debug_info_path)
            except ValueError as exc:
                logger.error("Cannot read %s: %s", self.debug_info_path, exc)
                self._info = None
            self._info_stamp = stamp
        return self._info

    # ------------------------------------------------------------------ local handlers

    def _set_lock(self, session: ClientSession, args: JsonObj, _info: Optional[DebugInfo]) -> JsonObj:
        # TODO: T-SEC-5 persists the lock in debug.conf and gates the legacy namespace too.
        self.locked = args["on"] is True
        logger.info("Debug lock set to %s by %s", self.locked, session.user)
        return {"on": self.locked}

    def _set_breakpoints(
        self, session: ClientSession, args: JsonObj, info: Optional[DebugInfo]
    ) -> JsonObj:
        assert info is not None
        path = str(args["file"])
        lines = [ln for ln in _list(args["lines"]) if isinstance(ln, int)]
        fid = info.file_id(path)
        resolved = [info.resolve_line(path, ln) for ln in lines]
        body = self._core(
            "setBreakpoints",
            {"session": session.core_session, "fileId": fid, "probes": [p for p, _ in resolved]},
        )
        core_bps = body.get("breakpoints")
        core_list = core_bps if isinstance(core_bps, list) else []
        out: List[Json] = []
        for requested, (probe, actual), bp in zip(lines, resolved, core_list):
            loc = info.location(probe)
            item: JsonObj = {
                "id": _obj(bp).get("id"),
                "verified": _obj(bp).get("verified") is True,
                "line": actual,
                "col": loc.col if loc is not None else None,
                "requestedLine": requested,
            }
            # The core refuses per item (pm ruling 6); DAP carries the reason as `message`.
            error = _obj(_obj(bp).get("error"))
            if error:
                item["message"] = str(error.get("message") or error.get("code"))
            out.append(item)
        return {"breakpoints": out}

    def _breakpoint_locations(
        self, _session: ClientSession, args: JsonObj, info: Optional[DebugInfo]
    ) -> JsonObj:
        assert info is not None
        first = _int(args["line"])
        last = args.get("endLine")
        lines = info.lines_between(str(args["file"]), first, _int(last) if last is not None else first)
        return {"breakpoints": [{"line": ln} for ln in lines]}

    def _run_to_cursor(
        self, session: ClientSession, args: JsonObj, info: Optional[DebugInfo]
    ) -> JsonObj:
        assert info is not None
        probe, _line = info.resolve_line(str(args["file"]), _int(args["line"]))
        forwarded: JsonObj = {"session": session.core_session, "probe": probe}
        if "task" in args:
            forwarded["task"] = args["task"]
        return self._core("runToCursor", forwarded)

    # ------------------------------------------------------------------ forces

    # ADR-005 D5: owner, reason, TTL. The owner is the session's user — a client does not get
    # to name it, which is what makes "who left this forced" answerable — and the reason is the
    # client's own words, required and non-empty.

    def _resolve(self, info: DebugInfo, path: str) -> Node:
        """A force target: an elementary leaf in the static debug table. A composite, a frame
        local and a stale build are refused here, by name, before anything is pinned."""
        node = info.resolve(path)
        if node.frame is not None:
            raise Refusal(
                ErrorCode.UNSUPPORTED,
                f"'{path}' is a task's own stack variable; it is not forceable from a session.",
                {"path": path},
            )
        if not info.is_elementary(node.type_id):
            raise Refusal(
                ErrorCode.BAD_REQUEST,
                f"'{path}' is a {info.type_name(node.type_id)}; force one of its members.",
                {"path": path},
            )
        return node

    def _force(
        self, session: ClientSession, args: JsonObj, info: Optional[DebugInfo]
    ) -> JsonObj:
        assert info is not None
        path = str(args["path"])
        reason = str(args["reason"])
        if not reason.strip():
            # D5.1: an empty reason is BAD_REQUEST, not a force nobody can later explain.
            raise Refusal(
                ErrorCode.BAD_REQUEST,
                "A force needs a reason: it is what the listing shows its owner and anyone "
                "else reading it.",
                {"path": path},
            )
        ttl_ms = args.get("ttl")
        if ttl_ms is None:
            ttl_ms = self.p5_s * 1000.0
        if ttl_ms > P5_MAX_S * 1000.0:
            raise Refusal(
                ErrorCode.RANGE_ERROR,
                f"A force may live at most {P5_MAX_S / 3600:g} h; {ttl_ms / 3600000:g} h is longer.",
                {"ttl": ttl_ms, "max": P5_MAX_S * 1000.0},
            )
        node = self._resolve(info, path)
        hexed = info.encode(node.type_id, args["value"], node.leaf)
        body = self._core(
            "force",
            {
                "session": session.core_session,
                "leaf": node.leaf,
                "value": hexed,
                "owner": session.user,
                "reason": reason,
                # Seconds on the wire, the unit docs/DEBUG_CORE_LINK.md names. The TTL counts
                # from the core's own receipt of this request, which is the force response.
                "ttlS": ttl_ms / 1000.0,
            },
        )
        return body

    def _unforce(self, session: ClientSession, args: JsonObj, info: Optional[DebugInfo]) -> JsonObj:
        assert info is not None
        node = self._resolve(info, str(args["path"]))
        return self._core("unforce", {"session": session.core_session, "leaf": node.leaf})

    def _unforce_all(
        self, session: ClientSession, _args: JsonObj, _info: Optional[DebugInfo]
    ) -> JsonObj:
        """D5.5: one action over every force, every owner's and legacy ones included."""
        return self._core("unforceAll", {"session": session.core_session})

    def _list_forces(
        self, session: ClientSession, _args: JsonObj, _info: Optional[DebugInfo]
    ) -> JsonObj:
        info = self._debug_info()
        body = self._core("listForces", {"session": session.core_session})
        self._remember_forces({"list": body.get("forces")})
        return {"forces": self._forces_of(info, body.get("forces"))}

    def _remember_forces(self, body: Json) -> None:
        """Adopt a `forces` body (event or reply) as the gateway's view of the registry."""
        entries = _list(body.get("list"))
        self._forced.clear()
        for entry in entries:
            item = _obj(entry)
            leaf = item.get("leaf")
            if isinstance(leaf, int) and not isinstance(leaf, bool):
                self._forced[leaf] = item

    def _forces_of(self, info: Optional[DebugInfo], core_forces: Json) -> List[JsonObj]:
        """The core's registry in the terms a client reads: each entry's symbolic path, its value
        decoded by that leaf's IEC type, its owner, its reason and the time left on it (null for
        a force with no end, which is a legacy 0x42 one)."""
        out: List[JsonObj] = []
        now = time.time() * 1000.0
        for raw in _list(core_forces):
            item = _obj(raw)
            leaf = item.get("leaf")
            if not isinstance(leaf, int) or isinstance(leaf, bool):
                continue
            hexed = item.get("value")
            node = None
            path: Optional[str] = None
            if info is not None:
                path = info.path_of_leaf(leaf)
                if path is not None:
                    try:
                        node = info.resolve(path)
                    except Refusal:
                        node = None
                    if node is not None and not info.is_elementary(node.type_id):
                        node = None  # not a leaf after all: fall back to the bytes
            expires = item.get("expiresAt")
            remaining: Json = None
            if isinstance(expires, (int, float)) and not isinstance(expires, bool):
                remaining = max(0.0, float(expires) - now)
            entry: JsonObj = {
                "path": path if path is not None else f"leaf {leaf}",
                "leaf": leaf,
                "value": (
                    info.decode(node.type_id, hexed if isinstance(hexed, str) else None, leaf)
                    if node is not None and isinstance(hexed, str)
                    else hexed
                ),
                "owner": str(item.get("owner") or ""),
                "reason": str(item.get("reason") or ""),
                "remaining": remaining,
            }
            out.append(entry)
        return out

    # ------------------------------------------------------------------ inspection

    def _core_frames(
        self, session: ClientSession, info: DebugInfo, task: Json
    ) -> Tuple[JsonObj, List[JsonObj]]:
        """The core's stackTrace, each frame resolved to its POU, instance and location."""
        args: JsonObj = {"session": session.core_session}
        if task is not None:
            args["task"] = task
        body = self._core("stackTrace", args)
        out: List[JsonObj] = []
        for f in _list(body.get("frames")):
            f_obj = _obj(f)
            pou = info.pou_by_entry.get(f_obj.get("pou")) if isinstance(f_obj.get("pou"), int) else None
            at = f_obj.get("at")
            loc = info.location(at) if isinstance(at, int) else None
            leaf = f_obj.get("leaf") if isinstance(f_obj.get("leaf"), int) else None
            out.append({
                "pou": pou,
                "instance": info.instance_of(leaf, pou) if pou is not None and pou.kind != "function" else None,
                "location": loc.as_json() if loc is not None else None,
            })
        return body, out

    def _stack_trace(self, session: ClientSession, args: JsonObj, info: Optional[DebugInfo]) -> JsonObj:
        assert info is not None
        body, frames = self._core_frames(session, info, args.get("task"))
        task_id = body.get("taskId") if isinstance(body.get("taskId"), int) else 0
        listed: List[JsonObj] = []
        for i, f in enumerate(frames):
            pou: Optional[Pou] = f["pou"]
            loc = _obj(f["location"])
            listed.append({
                "id": task_id * _FRAMES_PER_TASK + i,
                "name": pou.name if pou is not None else "?",
                "pou": pou.name if pou is not None else None,
                "kind": pou.kind if pou is not None else None,
                "instance": f["instance"],
                "location": f["location"],
                "line": loc.get("line", 0),
                "column": loc.get("col", 0),
                "source": {"path": loc["file"]} if "file" in loc else None,
            })
        # ADR-001 D3.4: a walk that stopped at an older attach, or never reached the program's
        # run() frame, is a truncated stack.
        outermost = frames[-1]["pou"] if frames else None
        truncated = body.get("truncated") is True or (
            bool(frames) and (outermost is None or outermost.kind != "program")
        )
        start = args.get("startFrame") if isinstance(args.get("startFrame"), int) else 0
        levels = args.get("levels") if isinstance(args.get("levels"), int) else 0
        page = listed[start: start + levels] if levels else listed[start:]
        return {"stackFrames": page, "totalFrames": len(listed), "truncated": truncated,
                "task": body.get("task"), "taskId": task_id}

    def _frame(
        self, session: ClientSession, info: DebugInfo, frame_id: int
    ) -> Tuple[JsonObj, int]:
        task_id, index = divmod(frame_id, _FRAMES_PER_TASK)
        _body, frames = self._core_frames(session, info, task_id)
        if index >= len(frames):
            raise Refusal(ErrorCode.BAD_REQUEST, f"There is no frame {frame_id}; list the stack again.")
        return frames[index], index

    def _ref(self, *key: str) -> int:
        ref = self._ref_ids.get(key)
        if ref is None:
            ref = len(self._ref_ids) + 1
            self._ref_ids[key] = ref
            self._refs[ref] = key
        return ref

    def _scopes(self, session: ClientSession, args: JsonObj, info: Optional[DebugInfo]) -> JsonObj:
        """F074: a frame's variables grouped by section. An instance's members come from the
        static tree; the frame's own variables (a function's inputs and temps, an FB's or a
        program's VAR_TEMP) come from the POU's `__dbg_locals` table, which is read per slot."""
        assert info is not None
        frame_id = _int(args["frameId"])
        frame, _ = self._frame(session, info, frame_id)
        pou: Optional[Pou] = frame["pou"]
        groups: Dict[str, List[str]] = {}
        instance = frame["instance"]
        if instance is not None:
            for name, child in info.children(info.resolve(instance)):
                section = self._section_of(info, info.resolve(instance), name)
                groups.setdefault(SCOPE_OF_SECTION.get(section, "members"), []).append(name)
        for name, _tid, section, _slot in pou.locals if pou is not None else ():
            groups.setdefault(SCOPE_OF_SECTION.get(section, "locals"), []).append(name)
        scopes = [
            {"name": scope,
             "variablesReference": self._ref(
                 "scope", str(frame_id), str(frame_id // _FRAMES_PER_TASK), scope, *names),
             "namedVariables": len(names), "expensive": False}
            for scope, names in groups.items()
        ]
        return {"scopes": scopes}

    @staticmethod
    def _section_of(info: DebugInfo, node: Node, name: str) -> str:
        t = info.types.get(node.type_id)
        for m in t.members if t is not None else []:
            if str(m.get("name")).upper() == name.upper():
                return str(m.get("section") or "VAR")
        return "VAR_EXTERNAL"

    def _read(self, info: DebugInfo, session: ClientSession, nodes: List[Node],
              frame: Optional[Tuple[int, int]] = None) -> Tuple[List[Json], List[Json]]:
        """Decoded values per node, and the core's `pending` per leaf alongside them.

        `pending` is what the image slot a located %I leaf will next copy in, while
        the program member stays frozen at what the halted scan took (ADR-004
        D5.2). The core has always returned it; this is where it was being
        dropped, which is why A055's oracle saw the field absent rather than
        wrong. A non-input leaf, or one with no pending write, yields None and the
        field is omitted — an output is not 'pending', the copy-out decides it.

        A node whose leaves are in the halted task's stack (a frame's own variables) is read by
        slot through `readFrameLocals` instead: those leaves are in no static table, so they have
        no `pending` either — a local is not a located member. `frame` is that frame's
        (task id, index in the halted stack).
        """
        out_values: List[Json] = [None] * len(nodes)
        out_pending: List[Json] = [None] * len(nodes)
        static = [i for i, n in enumerate(nodes) if n.frame is None]
        leaves = [nodes[i].leaf for i in static]
        values: List[Json] = []
        pending: List[Json] = []
        for i in range(0, len(leaves), _MAX_LEAVES):
            body = self._core("readLeaves", {"session": session.core_session,
                                             "leaves": leaves[i: i + _MAX_LEAVES]})
            values.extend(_list(body.get("values")))
            pending.extend(_list(body.get("pending")))
        # zip, not index: a short reply must not shift one leaf's pending onto another's
        for at, (v, p) in zip(static, zip(values, pending)):
            node = nodes[at]
            out_values[at] = info.decode(node.type_id, v if isinstance(v, str) else None, node.leaf)
            out_pending[at] = p
        if frame is not None:
            groups: Dict[int, List[int]] = {}
            for at, node in enumerate(nodes):
                if node.frame is not None:
                    groups.setdefault(node.frame[0], []).append(at)
            for pou_id, ats in groups.items():
                self._read_frame_leaves(info, session, pou_id, frame, [nodes[at] for at in ats], ats,
                                        out_values)
        return out_values, out_pending

    def _read_frame_leaves(self, info: DebugInfo, session: ClientSession, pou_id: int,
                           frame: Tuple[int, int], nodes: List[Node], ats: List[int],
                           out: List[Json]) -> None:
        """One frame's own variables, read by slot out of its `__dbg_locals`. The core has the
        table's addresses and this has each leaf's IEC type and byte size (both from the same
        build's debug information, ADR-002 D1), so the pair is a hex string decoded exactly like
        a static leaf. `ats` are the positions in the caller's `nodes` the answers belong to."""
        pou = info.pous.get(pou_id)
        if pou is None:
            return
        task, index = frame
        ask: List[JsonObj] = []
        for node in nodes:
            slot = node.frame[1] if node.frame is not None else -1
            # A slot past the table the debug info describes is asked for at size 0, which the
            # core answers null: a frame variable with no leaf reads as no value, not as garbage.
            size = pou.frame_leaves[slot][2] if 0 <= slot < len(pou.frame_leaves) else 0
            ask.append({"slot": max(slot, 0), "size": size})
        body = self._core("readFrameLocals", {
            "session": session.core_session, "task": task, "frame": index, "leaves": ask,
        })
        raw = _list(body.get("values"))
        for at, node, value in zip(ats, nodes, raw):
            slot = node.frame[1] if node.frame is not None else -1
            out[at] = info.decode(
                node.type_id, value if isinstance(value, str) else None, slot)

    def _listing(
        self, session: ClientSession, info: DebugInfo, named: List[Tuple[str, Optional[Node]]],
        native: bool = False, frame: Optional[Tuple[int, int, int]] = None,
    ) -> List[JsonObj]:
        """One level of variables: an elementary one with its value, a composite one with a
        variablesReference to expand it (A089: no grandchild is read before it is asked for).
        DAP's `value` is display text, with the JSON value in `data`; `native` (the by-path form)
        puts the JSON value in `value`. `frame` is (frame id, task id, index in the halted stack)
        of the frame these names are relative to: frame variables are read by slot, and a
        composite one expands within the frame, so its reference has to name the frame too."""
        readable = [n for _, n in named if n is not None and info.is_elementary(n.type_id)]
        raw_values, raw_pending = self._read(info, session, readable, frame[1:] if frame else None)
        values = dict(zip((n.path for n in readable), raw_values))
        pendings = dict(zip((n.path for n in readable), raw_pending))
        out: List[JsonObj] = []
        for name, node in named:
            item: JsonObj = {"name": name, "variablesReference": 0}
            if node is None:
                item.update({"value": "<not readable>", "data": None, "type": "?"})
            elif info.is_elementary(node.type_id):
                data = values.get(node.path)
                text = data if isinstance(data, str) or native else json.dumps(data)
                item.update({"value": text, "data": data, "type": info.type_name(node.type_id),
                             "evaluateName": node.path})
                pend = pendings.get(node.path)
                if pend is not None:
                    # Omitted rather than null when nothing is pending: an absent field
                    # means "no next copy-in", which null would blur into "unknown".
                    item["pending"] = info.decode(node.type_id, pend, node.leaf)
            else:
                item.update({"value": info.type_name(node.type_id), "type": info.type_name(node.type_id),
                             "evaluateName": node.path,
                             "variablesReference": self._ref(
                                 *(("frame", str(frame[0]), node.path) if frame and node.frame is not None
                                   else ("path", node.path))),
                             "namedVariables": len(info.children(node))})
            out.append(item)
        return out

    def _variables(self, session: ClientSession, args: JsonObj, info: Optional[DebugInfo]) -> JsonObj:
        assert info is not None
        ref = args.get("variablesReference")
        if isinstance(ref, int):
            key = self._refs.get(ref)
            if key is None:
                raise Refusal(ErrorCode.BAD_REQUEST, f"There is no variablesReference {ref}.")
            if key[0] == "path":
                node = info.resolve(key[1])
                return {"variables": self._listing(session, info, info.children(node))}
            if key[0] == "frame":
                # A composite frame variable, expanded in place: its children are the slots
                # after it in the same `__dbg_locals` table.
                frame_id, path = int(key[1]), key[2]
                f, index = self._frame(session, info, frame_id)
                node = info.resolve(path, f["instance"], f["pou"])
                return {"variables": self._listing(
                    session, info, info.children(node),
                    frame=(frame_id, frame_id // _FRAMES_PER_TASK, index))}
            frame_id, task = int(key[1]), _int(key[2])
            f, index = self._frame(session, info, frame_id)
            instance, pou = f["instance"], f["pou"]
            named: List[Tuple[str, Optional[Node]]] = []
            for name in key[4:]:
                try:
                    named.append((name, info.resolve(name, instance, pou)))
                except Refusal:
                    named.append((name, None))  # a name the frame does not have
            return {"variables": self._listing(
                session, info, named, frame=(frame_id, task, index))}
        path = args.get("path")
        if not isinstance(path, str):
            raise Refusal(ErrorCode.BAD_REQUEST, "variables needs a variablesReference or a path.")
        instance, pou, frame = None, None, None
        if isinstance(args.get("frameId"), int):
            frame_id = _int(args["frameId"])
            f, index = self._frame(session, info, frame_id)
            instance, pou = f["instance"], f["pou"]
            frame = (frame_id, frame_id // _FRAMES_PER_TASK, index)
        node = info.resolve(path, instance, pou)
        if info.is_elementary(node.type_id):
            value, pending = self._read(info, session, [node], frame[1:] if frame else None)
            out: JsonObj = {"path": node.path, "type": info.type_name(node.type_id),
                            "value": value[0]}
            # E45 ruling 2: `types: true` asks for the structure behind that string. Omitted
            # otherwise, so a client that only displays the type sees the body it always saw.
            if args.get("types") is True:
                out.update(info.type_detail(node.type_id))
            # F096/J5.1: every view of a variable says whether it is forced. The core's registry
            # is authoritative and pushes `forces` on every change, so the mark is current.
            if node.frame is None and node.leaf in self._forced:
                out["forced"] = True
            if pending[0] is not None:
                # The by-path form carries it too: a client asking for one variable by
                # path wants the same facts the listing gives (ADR-004 D5.2), and an
                # absent field means "no next copy-in", not "unknown".
                out["pending"] = info.decode(node.type_id, pending[0], node.leaf)
            return out
        out = {
            "path": node.path,
            "type": info.type_name(node.type_id),
            "variablesReference": self._ref(
                *(("frame", str(frame[0]), node.path) if frame and node.frame is not None
                  else ("path", node.path))),
            "children": self._listing(session, info, info.children(node), native=True, frame=frame),
        }
        # An array's bounds and an enum's members are asked for on this form too: both are
        # composite, so neither reaches the elementary branch above (E45 ruling 2).
        if args.get("types") is True:
            out.update(info.type_detail(node.type_id))
        return out

    # ------------------------------------------------------------------ status API

    def status_block(self) -> JsonObj:
        """The `debug` block of GET /api/status (ADR-003 D9): the core's run state and the
        attached sessions. The webserver's status handler adds it as `debug`."""
        try:
            body = self._core("status", {})
        except (Refusal, CoreUnavailable) as exc:
            return {"state": None, "error": str(exc)}
        held = body.get("sessions")
        with self.lock:
            # Only sessions the core still holds: a stop must not read as a detach (A033).
            sessions = [
                {"user": s.user, "role": s.role}
                for s in self.sessions.values()
                if not isinstance(held, list) or s.core_session in held
            ]
        return {**body, "sessions": sessions}

    # ------------------------------------------------------------------ session end

    def _end_by_request(self, session: ClientSession, command: str) -> JsonObj:
        body = self._core(command, {"session": session.core_session})
        self._retire(session, "detached" if command == "detach" else "terminated")
        self._broadcast_control()
        return body

    def _detach(self, session: ClientSession) -> JsonObj:
        """A detach ends the session, not the forces: D5.3 keeps them, and the response lists
        what is still active so the leaving client (and A113's next session) can see them."""
        info = self._debug_info()
        body = self._core("detach", {"session": session.core_session})
        self._retire(session, "detached")
        self._broadcast_control()
        out: JsonObj = {"forces": self._forces_of(info, body.get("forces"))}
        if body.get("safeStopped") is True:
            out["safeStopped"] = True
        return out

    def _retire(self, session: ClientSession, reason: str) -> bool:
        """Forget a live session; False if something else already ended it."""
        with self.lock:
            if self.sessions.get(session.sid) is not session:
                return False
            del self.sessions[session.sid]
            self.ended[session.sid] = reason
            # A resume after the end is told why (ADR-005 D4.6), so the reason outlives the session.
            self.ended_tokens[session.resume_hash] = reason
            while len(self.ended_tokens) > ENDED_TOKENS_KEPT:
                del self.ended_tokens[next(iter(self.ended_tokens))]
        return True

    def _end(self, session: ClientSession, reason: str) -> None:
        if not self._retire(session, reason):
            return
        self._core_detach(session, reason)
        self._event(session.sid, "sessionEnded", {"reason": reason})
        self._broadcast_control()

    def _core_detach(self, session: ClientSession, reason: str) -> None:
        try:
            self.link.request("detach", {"session": session.core_session, "reason": reason})
        except CoreUnavailable as exc:
            logger.warning("Debug session detach (%s) did not reach the core: %s", reason, exc)

    def _broadcast_control(self) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
        controller = next((s.user for s in sessions if s.role == "controller"), None)
        body: JsonObj = {
            "controller": controller,
            "observers": sum(s.role == "observer" for s in sessions),
        }
        for s in sessions:
            self._event(s.sid, "control", body)

    # ------------------------------------------------------------------ core events

    def _on_core_event(self, event: str, body: JsonObj) -> None:
        if event in ("hello", "beat"):
            return
        if event == "_linkLost":
            with self.lock:
                lost = list(self.sessions.values())
            for s in lost:
                if self._retire(s, "core_lost"):
                    self._event(s.sid, "sessionEnded", {"reason": "core_lost"})
            return
        if event == "forces":
            # ADR-005 D5.3: the core reports every change to its registry, including a TTL
            # expiring with no client asking. The gateway's `forced` marks follow it.
            self._remember_forces(body)
            listed = self._forces_of(self._debug_info(), body.get("list"))
            out = {"list": listed}
            with self.lock:
                recipients = list(self.sessions.values())
            for s in recipients:
                self._event(s.sid, "forces", out)
            return
        # The core's session IDs are the link's, never a client's (status_block uses them).
        out = {k: v for k, v in body.items() if k not in ("session", "probe", "sessions", "list")}
        probe = body.get("probe")
        if event == "stopped" and isinstance(probe, int):
            info = self._debug_info()
            loc = info.location(probe) if info is not None else None
            if loc is not None:
                out["location"] = loc.as_json()
        with self.lock:
            recipients = list(self.sessions.values())
        target = body.get("session")
        if target is not None:
            recipients = [s for s in recipients if s.core_session == target]
            if event == "sessionEnded":
                for s in recipients:
                    self._retire(s, str(out.get("reason", "ended")))
        for s in recipients:
            if s.role == "controller" or event in OBSERVER_EVENTS:
                self._event(s.sid, event, out)


def _compare(identity: JsonObj, image: JsonObj) -> str:
    """ADR-003 D3.2 comparison of the client's build with the running image."""
    md5, build_id = identity.get("md5"), identity.get("buildId")
    if not isinstance(md5, str):
        return "none"
    if md5 != image.get("md5"):
        return "source"
    # A client that names its build is compared by buildId (ADR-002 D5); an image with none
    # (built before strucpp_dbg_build_id) cannot be that build.
    if isinstance(build_id, str) and build_id != image.get("buildId"):
        return "build"
    return "match"


def init_debug_gateway(
    socketio: SocketIO,
    link: CoreLink,
    debug_info_path: Path,
    params: Optional[HeartbeatParams] = None,
    conf_path: Optional[Path] = None,
    p5_s: Optional[float] = None,
) -> DebugGateway:
    """`params` None reads P1/P2 and P5 from `conf_path` (default `<data dir>/debug.conf`)."""
    if params is None or p5_s is None:
        if conf_path is None:
            from webserver.config import PERSISTENT_DATA_DIR  # noqa: PLC0415 - import side effects

            conf_path = PERSISTENT_DATA_DIR / CONF_NAME
        try:
            loaded_params, loaded_p5 = load_params(conf_path)
        except ConfigError as exc:
            # ADR-005 D4.2 / D5.1: refused at load; the defaults apply, never the bad values.
            logger.error(
                "Refusing %s: %s; using P1 = 2 s, P2 = 15 s, P5 = 600 s", conf_path, exc
            )
            loaded_params, loaded_p5 = HeartbeatParams(), P5_DEFAULT_S
        if params is None:
            params = loaded_params
        if p5_s is None:
            p5_s = loaded_p5
    gateway = DebugGateway(socketio, link, debug_info_path, params=params, p5_s=p5_s)
    gateway.register()
    logger.info("Debug session gateway initialized at %s", NAMESPACE)
    return gateway
