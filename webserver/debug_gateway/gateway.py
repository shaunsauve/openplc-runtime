# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""The debug session gateway: Socket.IO namespace /api/debug-session (ADR-003 D1.1), one event
name `msg` carrying DAP-shaped requests, correlated responses and pushed events (D2, D4), the
handshake and version report (D3), translated to and from the internal core link (D1.3).

See also:
  webserver/debug_gateway/protocol.py - request table, refusal catalogue, envelopes.
  webserver/debug_gateway/core_link.py - the core link and its CoreLink interface.
  webserver/debug_gateway/debug_info.py - line/probe resolution from debug-info.json.
  webserver/debug_websocket.py - the legacy /api/debug namespace on the same Socket.IO instance.
  docs/DEBUG_PROTOCOL.md - "Session protocol v1", the spec.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from flask import request
from flask_jwt_extended import decode_token, get_current_user, verify_jwt_in_request
from flask_jwt_extended.exceptions import JWTExtendedException
from flask_socketio import SocketIO
from jwt.exceptions import PyJWTError

from webserver.debug_gateway.core_link import CoreLink, CoreUnavailable
from webserver.debug_gateway.debug_info import DebugInfo
from webserver.debug_gateway.protocol import (
    COMMANDS,
    DEFAULT_MESSAGES,
    DEFAULT_PARAMS,
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
from webserver.logger import get_logger
from webserver.restapi import User, check_if_token_revoked, user_lookup_callback
from webserver.version import RUNTIME_VERSION

logger, _ = get_logger("debug_gw", use_buffer=True)

OBSERVER_EVENTS = frozenset(
    {"stopped", "continued", "runState", "output", "values", "time", "control", "sessionEnded",
     "programReplaced"}
)
_CODES = {c.value: c for c in ErrorCode}


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


class DebugGateway:
    def __init__(
        self,
        socketio: SocketIO,
        link: CoreLink,
        debug_info_path: Path,
        runtime_version: str = RUNTIME_VERSION,
    ) -> None:
        self.socketio = socketio
        self.link = link
        self.debug_info_path = debug_info_path
        self.runtime_version = runtime_version
        self.sessions: Dict[str, ClientSession] = {}
        self.ended: Dict[str, str] = {}
        self.locked = False
        self.lock = threading.Lock()
        self._seq = 0
        self._info: Optional[DebugInfo] = None
        self._info_stamp: Optional[Tuple[int, int]] = None
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

    def _on_connect(self, auth: Json = None) -> bool:
        del auth  # the JWT travels in initialize (ADR-005 D1.1)
        return True

    def _on_disconnect(self, reason: Json = None) -> None:
        del reason
        sid = str(request.sid)
        with self.lock:
            session = self.sessions.pop(sid, None)
            self.ended.pop(sid, None)
        if session is not None:
            # TODO: T-SEC-3/4 keep the session for P2 and resume it; until then loss detaches.
            self._core_detach(session, "disconnect")
            self._broadcast_control()

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

    def handle(self, sid: str, raw: Json) -> JsonObj:
        request_seq: Json = None
        command: Json = None
        try:
            msg, size = self._parse(raw)
            request_seq = msg.get("seq") if isinstance(msg.get("seq"), int) else None
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
    def _parse(raw: Json) -> Tuple[JsonObj, int]:
        if isinstance(raw, str):
            size = len(raw.encode("utf-8"))
            try:
                raw = json.loads(raw)
            except ValueError as exc:
                raise Refusal(ErrorCode.BAD_REQUEST, "The message is not valid JSON.") from exc
        else:
            size = len(json.dumps(raw, separators=(",", ":")).encode("utf-8"))
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
            "detach": lambda s, a, i: self._end_by_request(s, "detach"),
            "terminate": lambda s, a, i: self._end_by_request(s, "terminate"),
            "heartbeat": lambda s, a, i: self._core("beatClient", {"session": s.core_session}),
            "version": lambda s, a, i: self._versions(self.link.hello()),
            "setLock": self._set_lock,
            "setBreakpoints": self._set_breakpoints,
            "breakpointLocations": self._breakpoint_locations,
            "runToCursor": self._run_to_cursor,
        }

    def _core(self, command: str, args: JsonObj) -> JsonObj:
        reply = self.link.request(command, args)
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
        user, is_admin, token = self._authenticate(args.get("token"))
        if hello is None:
            raise CoreUnavailable("no hello from the core")
        role = str(args.get("role") or "controller")
        image = _obj(hello.get("image"))
        session = ClientSession(
            sid, user, is_admin, role, token, _compare(_obj(args.get("identity")), image)
        )
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
            session.core_session = self._core("attach", {"user": user, "role": role}).get("session")
        except (Refusal, CoreUnavailable):
            with self.lock:
                self.sessions.pop(sid, None)
            raise
        self._broadcast_control()
        return {
            **self._versions(hello),
            "image": image,
            "comparison": session.comparison,
            "params": {**DEFAULT_PARAMS, **_obj(hello.get("params"))},
            "capabilities": caps,
            "requests": advertised_requests(caps),
            "role": role,
            "locked": self.locked,
        }

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

    def _reverify(self, session: ClientSession) -> None:
        """ADR-005 D2.1: revocation and account deletion end the session; expiry does not."""
        try:
            payload = decode_token(session.token, allow_expired=True)
            valid = (
                payload.get("type") == "access"
                and not check_if_token_revoked({}, payload)
                and user_lookup_callback({}, payload) is not None
            )
        except (JWTExtendedException, PyJWTError):
            valid = False
        if not valid:
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
        running = _obj(_obj(hello).get("image")).get("buildId")
        if info.build_id != running:
            raise Refusal(
                ErrorCode.STALE_BUILD,
                "The debug information on the runtime does not belong to the running program.",
                {"reason": "debugInfo"},
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
            out.append(
                {
                    "id": _obj(bp).get("id"),
                    "verified": _obj(bp).get("verified") is True,
                    "line": actual,
                    "col": loc.col if loc is not None else None,
                    "requestedLine": requested,
                }
            )
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

    # ------------------------------------------------------------------ session end

    def _end_by_request(self, session: ClientSession, command: str) -> JsonObj:
        body = self._core(command, {"session": session.core_session})
        with self.lock:
            self.sessions.pop(session.sid, None)
            self.ended[session.sid] = "detached" if command == "detach" else "terminated"
        self._broadcast_control()
        return body

    def _end(self, session: ClientSession, reason: str) -> None:
        with self.lock:
            self.sessions.pop(session.sid, None)
            self.ended[session.sid] = reason
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
                self.sessions.clear()
                for s in lost:
                    self.ended[s.sid] = "core_lost"
            for s in lost:
                self._event(s.sid, "sessionEnded", {"reason": "core_lost"})
            return
        out = {k: v for k, v in body.items() if k not in ("session", "probe")}
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
                with self.lock:
                    for s in recipients:
                        self.sessions.pop(s.sid, None)
                        self.ended[s.sid] = str(out.get("reason", "ended"))
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
    if isinstance(build_id, str) and build_id != image.get("buildId"):
        return "build"
    return "match"


def init_debug_gateway(socketio: SocketIO, link: CoreLink, debug_info_path: Path) -> DebugGateway:
    gateway = DebugGateway(socketio, link, debug_info_path)
    gateway.register()
    logger.info("Debug session gateway initialized at %s", NAMESPACE)
    return gateway
