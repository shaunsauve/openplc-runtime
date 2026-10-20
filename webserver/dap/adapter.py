#!/usr/bin/env python3
"""The DAP adapter: the Debug Adapter Protocol on stdin/stdout (or on a TCP socket), translated
onto the runtime's session protocol v1 over TLS + Socket.IO.

This is a TRANSLATOR, not a second client. Every fact it reports to a DAP client is a fact the
runtime reported to it; the only thing it computes is DAP's own vocabulary (capabilities, thread
ids, frame handles, event names) and the mapping between that vocabulary and the wire's. It reads
`docs/DEBUG_PROTOCOL.md` for the wire it speaks, and nothing in `tests/debug/harness/` — the
adapter is not a second view of the protocol, it is a view of the same one.

  python3 webserver/dap/adapter.py --endpoint https://127.0.0.1:8443 --token JWT [--insecure]
  python3 webserver/dap/adapter.py --listen 127.0.0.1:5678 [--insecure]   # TCP, token per attach

It lives here, under `webserver/`, because it is PRODUCT: an application launches it, and an
application cannot launch a test rig. It was at `tests/debug/dap/adapter.py` until E48.1, where the
defect was not the code but the path — nothing a run is measured with, and nothing shippable.
`webserver/dap/wss_client.py` travels with it for the reason `_socketio_client` gives.

WHAT IT REFUSES, AND WHY (docs/DEBUG_PROTOCOL.md "Requests" marks each of these `planned`, so the
runtime answers them UNSUPPORTED and the adapter does not pretend otherwise):

  setFunctionBreakpoints  the wire does not serve it; `supportsFunctionBreakpoints` is false.
  evaluate                the wire does not serve it; `supportsEvaluateName` is false.
  setVariable             the wire does not serve it; `supportsSetVariable` is false.
  threads                 the wire serves it now, but DAP's thread list is an id and a name and
                          `status`'s `tasks[]` already carries both, so the wire's own request
                          (with the interval, the priority and the scan statistics) is not what
                          DAP asks for.

CAPABILITIES ARE A PROMISE, so the block below is written as one: every `true` is a request this
file answers, and nothing else is true. `supportsStepBack`, `supportsSetVariable`,
`supportsFunctionBreakpoints`, `supportsConditionalBreakpoints` and every other capability whose
request is not served here are false, because a capability a client believes in and the adapter
cannot honour is the defect this whole tree keeps removing. The harness asserts the promise from
the outside (tests/debug/dap/dap_harness.py) rather than trusting this comment.

SEEDED DEFECTS (`--seed NAME`), each named by the acceptance criterion whose `Red-proof:` it
proves, and each a defect in the ADAPTER rather than in the runtime, so the harness can show the
criterion DIVERGE against a real plc_main:

  steplie     A073: advertise `supportsStepBack` with no handler behind it.
  varcache    A074: answer `variables` from the previous stop's cache instead of asking again.
  nocred      A075: `attach` never passes its credential to the runtime, so a wrong one is not
              refused and a session is established anyway.

See also:
  webserver/dap/wss_client.py    - the Socket.IO client this falls back to when the runtime image
                                   has no `websocket-client`.
  tests/debug/dap/dap_harness.py - the driver that launches this process and speaks DAP to it.
  docs/DEBUG_PROTOCOL.md         - the session protocol v1 this translates.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socketserver
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

NAMESPACE = "/api/debug-session"
PROTOCOL_VERSION = "1.0"
# Where the fallback Socket.IO client lives: THIS directory. Named rather than written
# inline because it is the one path a move can silently break — point it anywhere that
# does not hold `wss_client.py` and the adapter still starts, still answers
# `initialize`, and then fails every `attach`. The guard is in
# tests/debug/dap/test_dap.py::Placement and asserts this constant, not a copy of it.
FALLBACK_CLIENT_DIR = Path(__file__).resolve().parent
# The wire's own deadline is 5 s for a request and 30 s for `restart`; the adapter adds a little so
# a refusal is never mistaken for a hang, and a hang is never mistaken for a refusal.
RESPONSE_TIMEOUT_S = 10.0
RESTART_TIMEOUT_S = 35.0
HEARTBEAT_DIVISOR = 3.0
MAX_HEADER_BYTES = 8192
MAX_MESSAGE_BYTES = 16 * 1024 * 1024

# What `initialize` promises. Every `true` here is a request `_handle_*` below answers; a reader who
# doubts one can grep the name and find its handler, which is the property that makes the block a
# promise rather than a list of wishes.
#
# `exceptionBreakpointFilters` is absent on purpose. The wire's `setExceptionBreakpoints` exists
# (DEBUG_PROTOCOL.md lists it) but is refused on every build today, because no core advertises the
# `exceptionBreakpoints` capability it needs (core/src/plc_app/debug_link.cpp's `hello_locked` never
# sends one). An absent key reads as false, so a client is never told exception breakpoints are
# here; the request is still forwarded if one sends it, and the refusal it gets back is the
# runtime's own. See the harness's `run-control` scenario, which measures exactly that.
CAPABILITIES: Dict[str, Any] = {
    "supportsConfigurationDoneRequest": True,
    "supportsTerminateRequest": True,
    "supportTerminateDebuggee": True,
    "supportsRestartRequest": True,
    "supportsBreakpointLocationsRequest": True,
    # Everything below is false because no handler exists for it. DAP reads a missing key as false,
    # but saying so explicitly is what makes "advertised and unimplemented" a thing a reader can
    # grep for rather than infer.
    "supportsSingleThreadExecutionRequests": False,
    "supportsStepBack": False,
    "supportsRestartFrame": False,
    "supportsGotoTargetsRequest": False,
    "supportsStepInTargetsRequest": False,
    "supportsCompletionsRequest": False,
    "supportsModulesRequest": False,
    "supportsFunctionBreakpoints": False,
    "supportsDataBreakpoints": False,
    "supportsConditionalBreakpoints": False,
    "supportsHitConditionalBreakpoints": False,
    "supportsInstructionBreakpoints": False,
    "supportsLogPoints": False,
    "supportsLogAndContinue": False,
    "supportsTerminateThreadsRequest": False,
    "supportsDelayedStackTraceLoading": False,
    "supportsEvaluateName": False,
    "supportsSetVariable": False,
    "supportsSetExpression": False,
    "supportsExceptionInfoRequest": False,
    "supportsExceptionOptions": False,
    "supportsLoadedSourcesRequest": False,
    "supportsValueFormattingOptions": False,
    "supportsClipboardContext": False,
    "supportsProgressStart": False,
    "supportsInvalidatedEvent": False,
    "supportsMemoryReferences": False,
    "supportsArgsCanBeInterpretedByShell": False,
    "supportsMemoryEvent": False,
    "supportsReadMemoryRequest": False,
    "supportsDisassembleRequest": False,
    "supportsSteppingGranularity": False,
    "supportsBreakpointType": False,
    "supportsBreakpointTypes": False,
}

# Every DAP request name this adapter answers. Derived from the dispatch table rather than written
# out a second time, so a handler cannot be added without this list saying so — the harness checks
# against it from the outside.


class DapError(Exception):
    """A DAP failure response: `success: false`, a message, and an optional body."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


# --------------------------------------------------------------------------
# Content-Length framing
# --------------------------------------------------------------------------
#
# A few dozen lines, written out rather than taken from a dependency: this tree ships no DAP library
# and takes on no third-party dependency for a framing layer this small (see the epic). The wire is
# exactly what DAP specifies — `Content-Length: <n>\r\n`, optional extra headers, blank line, then
# `n` bytes of UTF-8 JSON.


def write_message(stream: Any, payload: bytes) -> None:
    """One framed message on a binary stream."""
    stream.write(b"Content-Length: %d\r\n\r\n" % len(payload))
    stream.write(payload)
    stream.flush()


def read_message(stream: Any) -> Optional[bytes]:
    """The next framed message body, or None at end of stream.

    Raises DapError on a header over `MAX_HEADER_BYTES` or a length that cannot be a message this
    adapter would ever be sent: an unbounded length is a way to make the reader allocate whatever
    the peer names, and the peer here is a debug client on the same machine.
    """
    headers: Dict[str, str] = {}
    total = 0
    while True:
        line = stream.readline()
        if not line:
            return None
        total += len(line)
        if total > MAX_HEADER_BYTES:
            raise DapError("the request headers are longer than %d bytes" % MAX_HEADER_BYTES)
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode("utf-8", "replace").partition(":")
        headers[name.strip().lower()] = value.strip()
    raw = headers.get("content-length")
    if raw is None:
        raise DapError("a request carried no Content-Length header")
    try:
        length = int(raw)
    except ValueError as exc:
        raise DapError("Content-Length %r is not an integer" % raw) from exc
    if length < 0 or length > MAX_MESSAGE_BYTES:
        raise DapError("Content-Length %d is out of range" % length)
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            raise DapError("the stream ended %d bytes into a %d byte message"
                           % (len(body), length))
        body += chunk
    return body


# --------------------------------------------------------------------------
# the wire: TLS + Socket.IO to the runtime's session protocol v1
# --------------------------------------------------------------------------


def _socketio_client(ssl_verify: bool):
    """The Socket.IO client this process speaks over.

    `socketio.Client` is used when `websocket-client` is installed. It is not, in the runtime image
    (python-socketio's sync client needs it for the websocket transport, and the image ships only
    `simple_websocket`), and the gateway serves `/api/debug-session` on the websocket transport
    only — so the fallback is the `WssSio` BESIDE THIS FILE, `webserver/dap/wss_client.py`, which is
    the same minimal client the corpus harness already uses for exactly this reason. It sits here
    rather than under `tests/` because this fallback is not optional: on the runtime image it is the
    only client there is, so an adapter without it starts and then cannot attach to anything.

    Reusing it is deliberate. It is generic Socket.IO/Engine.IO framing and nothing about the debug
    protocol: the envelope, the sequence numbers, the request/response correlation and every event
    below are this file's own. Writing a second Socket.IO client here would be a second transport to
    keep in step with the first, for no capability the first does not have."""
    try:
        import socketio  # noqa: PLC0415 - optional at import time, so --help works without it
        import websocket  # noqa: F401,PLC0415 - socketio's websocket transport needs this one
        return socketio.Client(ssl_verify=ssl_verify, reconnection=False)
    except ImportError:
        pass
    # This directory, not a package: the adapter runs as a script, so its own directory is the only
    # one on sys.path for free, and the fallback client is a sibling rather than an installed module.
    fallback_dir = str(FALLBACK_CLIENT_DIR)
    if fallback_dir not in sys.path:
        sys.path.insert(0, fallback_dir)
    try:
        from wss_client import WssSio  # noqa: PLC0415 - the fallback, only when it is needed
    except ImportError as exc:
        raise DapError(
            "no Socket.IO client: neither websocket-client nor %s is importable (%s)"
            % (fallback_dir, exc)) from exc
    return WssSio(ssl_verify=ssl_verify, reconnection=False)


class Wire:
    """One session on `/api/debug-session`. Requests are correlated by `seq`, never by arrival
    order (DEBUG_PROTOCOL.md "Envelope"), and every event lands on `events`."""

    def __init__(self, url: str, insecure: bool) -> None:
        self.url = url
        self.seq = 0
        self.lock = threading.Lock()
        self.pending: Dict[int, "queue.Queue[Dict[str, Any]]"] = {}
        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.closed = threading.Event()
        self.beat: Optional[threading.Thread] = None
        self.p1 = 2.0
        self.sio = _socketio_client(not insecure)
        self.sio.on("msg", self._on_msg, namespace=NAMESPACE)
        self.sio.connect(url, namespaces=[NAMESPACE], transports=["websocket"], wait_timeout=5)

    def _on_msg(self, data: Any) -> None:
        msg = json.loads(data) if isinstance(data, str) else data
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "response":
            with self.lock:
                waiter = self.pending.get(msg.get("request_seq"))
            if waiter is not None:
                waiter.put(msg)
        elif msg.get("type") == "event":
            self.events.put(msg)

    def request(self, command: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self.lock:
            self.seq += 1
            seq = self.seq
            waiter: "queue.Queue[Dict[str, Any]]" = queue.Queue()
            self.pending[seq] = waiter
        try:
            self.sio.emit("msg", {"type": "request", "seq": seq, "command": command,
                                  "arguments": args or {}}, namespace=NAMESPACE)
            timeout = RESTART_TIMEOUT_S if command == "restart" else RESPONSE_TIMEOUT_S
            try:
                return waiter.get(timeout=timeout)
            except queue.Empty:
                raise DapError("the runtime did not answer %s within %g s" % (command, timeout))
        finally:
            with self.lock:
                self.pending.pop(seq, None)

    def start_heartbeat(self, params: Dict[str, Any]) -> None:
        """Any message counts as a heartbeat (DEBUG_PROTOCOL.md "Heartbeat, loss and resume"), so
        an idle adapter sends `heartbeat` at a third of P1. Without it a client that only reads
        would lose its session at P2."""
        p1 = params.get("P1")
        self.p1 = float(p1) if isinstance(p1, (int, float)) and p1 > 0 else 2.0
        if self.beat is None:
            self.beat = threading.Thread(target=self._beat, name="dap-heartbeat", daemon=True)
            self.beat.start()

    def _beat(self) -> None:
        while not self.closed.wait(self.p1 / HEARTBEAT_DIVISOR):
            try:
                self.request("heartbeat", {})
            except Exception:  # noqa: BLE001 - the heartbeat is best-effort; the next one retries
                return

    def close(self) -> None:
        self.closed.set()
        try:
            self.sio.disconnect()
        except Exception:  # noqa: BLE001 - the socket is going away either way
            pass


# --------------------------------------------------------------------------
# the adapter
# --------------------------------------------------------------------------


class Adapter:
    """One DAP connection's worth of state: a wire session, the thread table DAP needs, and the
    variable cache a seeded defect can serve from."""

    def __init__(self, endpoint: str, token: Optional[str], insecure: bool,
                 seed: str = "", trace: Optional[str] = None) -> None:
        self.endpoint = endpoint
        self.token = token
        self.insecure = insecure
        self.seed = seed
        self.trace_path = trace
        self.wire: Optional[Wire] = None
        # DAP's threadId is an int; the wire names its tasks. `threads` is built from `status`, and
        # every request that carries a threadId is resolved through this table, so a threadId the
        # adapter never issued is a refusal rather than a guess.
        self.threads: Dict[int, str] = {}
        # `variablesReference` -> the children last read under it. Only the `varcache` seed reads
        # or writes it; an adapter with no defect has no cache, which is the point.
        self.variables_cache: Dict[int, List[Dict[str, Any]]] = {}
        self.stop = threading.Event()
        self.exit_code = 0
        self.out: Any = None
        self._events_sent: List[Dict[str, Any]] = []
        self._listener = threading.Thread(target=self._pump_events, name="dap-events", daemon=True)

    # -- plumbing

    def send(self, payload: Dict[str, Any]) -> None:
        if self.trace_path:
            # A trace that cannot be written must not take the session down with it: the file is a
            # diagnostic, and a diagnostic that is a new way to crash is not one.
            try:
                with open(self.trace_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
            except OSError:
                pass
        write_message(self.out, json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def event(self, name: str, body: Optional[Dict[str, Any]] = None) -> None:
        self._events_sent.append({"event": name, "body": body or {}})
        self.send({"seq": len(self._events_sent), "type": "event", "event": name,
                   "body": body or {}})

    def close(self) -> None:
        self.stop.set()

    # -- wire helpers

    def _need_wire(self) -> Wire:
        if self.wire is None:
            raise DapError("no debug session: send `initialize` then `attach` first")
        return self.wire

    def _call(self, command: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """One wire request, with the wire's typed failure raised as a DAP failure. The wire's code
        travels in `details.code`, so a client can tell UNSUPPORTED from NOT_HALTED."""
        resp = self._need_wire().request(command, args)
        if resp.get("success") is not True:
            err = resp.get("error") or {}
            details = dict(err.get("details") or {})
            details["code"] = err.get("code") or "INTERNAL"
            details["command"] = command
            raise DapError(err.get("message") or "the runtime refused %s" % command, **details)
        body = resp.get("body")
        return body if isinstance(body, dict) else {}

    def _task_name(self, thread_id: Any) -> str:
        try:
            tid = int(thread_id)
        except (TypeError, ValueError) as exc:
            raise DapError("threadId %r is not an integer" % (thread_id,)) from exc
        name = self.threads.get(tid)
        if name is None:
            raise DapError("no thread %d; call `threads` first (the adapter issues ids from it)" % tid)
        return name

    def _thread_id(self, task: str) -> Optional[int]:
        for tid, name in self.threads.items():
            if name.lower() == str(task).lower():   # an IEC identifier compares case-insensitively
                return tid
        return None

    # -- DAP request handlers. Each returns the response `body`.

    def _h_launch(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """DAP `launch` opens the session; it does NOT start the program.

        The PLC is already running — that is what an adapter for it is for, and A075's whole point
        is that attaching leaves the scan count uninterrupted. `restart` is the explicit request
        for the lifecycle, so `launch` saying it here would be the adapter taking a decision the
        client never asked for."""
        return self._attach(dict(args))

    def _h_attach(self, args: Dict[str, Any]) -> Dict[str, Any]:
        token = args.get("token") or self.token
        endpoint = args.get("endpoint") or self.endpoint
        role = args.get("role") or "controller"
        if role not in ("controller", "observer"):
            raise DapError("role %r is not 'controller' or 'observer'" % (role,))
        if self.seed == "nocred":
            # A075's seeded defect: the credential never reaches the runtime, so a wrong one is
            # accepted and a session is established against a program the client is not entitled to.
            token = self.token
        if not token:
            raise DapError("no credential: pass `token` in the attach request, or --token")
        self.wire = Wire(endpoint, self.insecure)
        try:
            handshake = self._call("initialize", {"protocol": PROTOCOL_VERSION, "token": token,
                                                  "role": role})
            self._call("attach", {"role": role})
        except DapError:
            self.wire.close()
            self.wire = None
            raise
        self._listener.start()
        self.wire.start_heartbeat(handshake.get("params") or {})
        self._refresh_threads()
        return {"runtime": handshake.get("runtime"), "protocol": handshake.get("protocol"),
                "role": handshake.get("role"), "locked": handshake.get("locked")}

    def _h_configuration_done(self, _args: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    def _h_set_breakpoints(self, args: Dict[str, Any]) -> Dict[str, Any]:
        source = args.get("source") or {}
        path = source.get("path") or source.get("name")
        if not path:
            raise DapError("setBreakpoints needs source.path (or source.name)")
        # The wire's per-breakpoint form: each breakpoint carries its own properties, and one
        # the runtime does not serve is refused on its own (the answer says so, and the client
        # sees the breakpoint as unverified). The shorthand is the same request without them.
        items: List[Dict[str, Any]] = []
        for bp in (args.get("breakpoints") or []):
            if not isinstance(bp, dict) or bp.get("line") is None:
                continue
            item: Dict[str, Any] = {"line": int(bp["line"])}
            if bp.get("condition"):
                item["condition"] = str(bp["condition"])
            if bp.get("logMessage"):
                item["logMessage"] = str(bp["logMessage"])
            hit = _hit_count(bp.get("hitCondition"))
            if hit is not None:
                item["hitCount"] = hit
            items.append(item)
        body = self._call("setBreakpoints", {"file": path, "breakpoints": items})
        return {"breakpoints": [_bp(bp) for bp in (body.get("breakpoints") or [])]}

    def _h_set_exception_breakpoints(self, args: Dict[str, Any]) -> Dict[str, Any]:
        filters = args.get("filters")
        if filters is None:
            filters = []
        if not isinstance(filters, list) or not all(isinstance(f, str) for f in filters):
            raise DapError("filters must be a list of strings")
        self._call("setExceptionBreakpoints", {"filters": filters})
        return {}

    def _h_breakpoint_locations(self, args: Dict[str, Any]) -> Dict[str, Any]:
        source = args.get("source") or {}
        path = source.get("path") or source.get("name")
        if not path:
            raise DapError("breakpointLocations needs source.path (or source.name)")
        wire_args: Dict[str, Any] = {"file": path, "line": int(args.get("line") or 1)}
        if args.get("endLine") is not None:
            wire_args["endLine"] = int(args["endLine"])
        body = self._call("breakpointLocations", wire_args)
        return {"breakpoints": [{"line": int(bp["line"])}
                                for bp in (body.get("breakpoints") or []) if "line" in bp]}

    def _h_threads(self, _args: Dict[str, Any]) -> Dict[str, Any]:
        self._refresh_threads()
        return {"threads": [{"id": tid, "name": name}
                            for tid, name in sorted(self.threads.items())]}

    def _refresh_threads(self) -> None:
        """The DAP thread list, answered from `status` — the wire's run-state body.

        The wire has no `threads` request (DEBUG_PROTOCOL.md marks it planned); what it does serve
        is `status`, whose `tasks[]` is exactly DAP's thread list: an id and a name per task."""
        body = self._call("status", {})
        table: Dict[int, str] = {}
        for task in body.get("tasks") or []:
            if isinstance(task, dict) and isinstance(task.get("taskId"), int):
                table[int(task["taskId"])] = str(task.get("task"))
        if table:
            self.threads = table

    def _h_stack_trace(self, args: Dict[str, Any]) -> Dict[str, Any]:
        wire_args: Dict[str, Any] = {"task": self._task_name(args.get("threadId"))}
        if args.get("startFrame") is not None:
            wire_args["startFrame"] = int(args["startFrame"])
        if args.get("levels") is not None:
            wire_args["levels"] = int(args["levels"])
        body = self._call("stackTrace", wire_args)
        frames = [_frame(f) for f in (body.get("stackFrames") or [])]
        return {"stackFrames": frames, "totalFrames": int(body.get("totalFrames") or len(frames))}

    def _h_scopes(self, args: Dict[str, Any]) -> Dict[str, Any]:
        body = self._call("scopes", {"frameId": int(args.get("frameId") or 0)})
        return {"scopes": [{"name": s.get("name"),
                            "variablesReference": int(s.get("variablesReference") or 0),
                            "namedVariables": int(s.get("namedVariables") or 0),
                            "expensive": bool(s.get("expensive"))}
                           for s in (body.get("scopes") or [])]}

    def _h_variables(self, args: Dict[str, Any]) -> Dict[str, Any]:
        ref = int(args.get("variablesReference") or 0)
        if self.seed == "varcache" and ref in self.variables_cache:
            # A074's seeded defect: the second stop's children are answered from the first stop's.
            # Keyed by the reference, which is what "the previous stop's cache" means — one
            # container's cache answering another container's request would be a different bug,
            # and one a client could not tell from a missing scope.
            return {"variables": self.variables_cache[ref]}
        body = self._call("variables", {"variablesReference": ref})
        out = [_variable(v) for v in (body.get("variables") or [])]
        if self.seed == "varcache":
            self.variables_cache[ref] = out
        return {"variables": out}

    def _h_continue(self, args: Dict[str, Any]) -> Dict[str, Any]:
        # All-stop, with no task. The wire's `continue` is all-stop by definition ("all-stop,
        # every halted task resumes"), and so is DAP's, since this adapter does not advertise
        # supportsSingleThreadExecutionRequests. The threadId is still resolved, so a client that
        # names a thread the adapter never issued is told so rather than silently resumed.
        self._task_name(args.get("threadId"))
        body = self._call("continue", {})
        return {"allThreadsContinued": bool(body.get("allThreadsContinued", True))}

    def _h_pause(self, args: Dict[str, Any]) -> Dict[str, Any]:
        # All-stop, for the same reason as `continue`. Naming a task here asks the core for
        # DBG_TRAP_PAUSE — a trap at the next probe, not a pause — which is a different request
        # with a different answer, and it is not what DAP's `pause` means.
        self._task_name(args.get("threadId"))
        self._call("pause", {})
        return {}

    def _step(self, command: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self._call(command, {"task": self._task_name(args.get("threadId"))})
        return {}

    def _h_restart(self, args: Dict[str, Any]) -> Dict[str, Any]:
        wire_args: Dict[str, Any] = {}
        kind = args.get("restartKind") or args.get("kind")
        if kind is not None:
            if kind not in ("warm", "cold", "reset"):
                raise DapError("restartKind %r is not 'warm', 'cold' or 'reset'" % (kind,))
            wire_args["kind"] = kind
        self._call("restart", wire_args)
        self.variables_cache.clear()
        return {}

    def _h_terminate(self, _args: Dict[str, Any]) -> Dict[str, Any]:
        self._call("terminate", {})
        self.event("terminated", {})
        if self.wire is not None:
            self.wire.close()
        self.close()
        return {}

    def _h_disconnect(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if self.wire is not None:
            if args.get("terminateDebuggee"):
                try:
                    self._call("terminate", {})
                except DapError:
                    pass          # the session may already be gone; the disconnect is what matters
            else:
                try:
                    self._call("detach", {})
                except DapError:
                    pass
            self.wire.close()
            self.wire = None
        self.event("terminated", {})
        self.close()
        return {}

    def _not_served(self, dap_request: str, wire_request: str) -> Callable:
        """A handler for a request the wire does not serve. It refuses, and it says which layer
        the gap is in — a client that gets this knows the adapter is not at fault."""
        def handler(_args: Dict[str, Any]) -> Dict[str, Any]:
            raise DapError(
                "this adapter does not implement %s: the runtime's session protocol v1 does not "
                "serve '%s' (docs/DEBUG_PROTOCOL.md marks it planned), so the adapter does not "
                "advertise the matching capability" % (dap_request, wire_request),
                code="UNSUPPORTED", request=dap_request, wireRequest=wire_request)
        return handler

    def handlers(self) -> Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]:
        caps = dict(CAPABILITIES)
        if self.seed == "steplie":
            # A073's seeded defect: a capability with no handler behind it.
            caps["supportsStepBack"] = True
        return {
            "initialize": lambda a: self._h_initialize_with(caps, a),
            "launch": self._h_launch,
            "attach": self._h_attach,
            "configurationDone": self._h_configuration_done,
            "setBreakpoints": self._h_set_breakpoints,
            "setExceptionBreakpoints": self._h_set_exception_breakpoints,
            "breakpointLocations": self._h_breakpoint_locations,
            "threads": self._h_threads,
            "stackTrace": self._h_stack_trace,
            "scopes": self._h_scopes,
            "variables": self._h_variables,
            "continue": self._h_continue,
            "pause": self._h_pause,
            "next": lambda a: self._step("next", a),
            "stepIn": lambda a: self._step("stepIn", a),
            "stepOut": lambda a: self._step("stepOut", a),
            "restart": self._h_restart,
            "terminate": self._h_terminate,
            "disconnect": self._h_disconnect,
            "setFunctionBreakpoints": self._not_served("setFunctionBreakpoints",
                                                       "setFunctionBreakpoints"),
            "evaluate": self._not_served("evaluate", "evaluate"),
            "setVariable": self._not_served("setVariable", "setVariable"),
        }

    @staticmethod
    def _h_initialize_with(caps: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        if args.get("linesStartAt1") is False or args.get("columnsStartAt1") is False:
            raise DapError("this adapter numbers lines and columns from 1 only")
        return dict(caps)

    # -- wire events -> DAP events

    def _pump_events(self) -> None:
        # The wire is captured once: `disconnect` sets `self.wire = None` from the request thread,
        # and a pump that read the attribute every pass died with an AttributeError at that moment
        # — on the one teardown path that most needs the event pump still alive.
        wire = self.wire
        if wire is None:
            return
        while not self.stop.is_set():
            try:
                msg = wire.events.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._on_wire_event(msg)
            except Exception:  # noqa: BLE001 - an event we cannot map must not kill the session
                continue

    def _on_wire_event(self, msg: Dict[str, Any]) -> None:
        name, body = msg.get("event"), msg.get("body") or {}
        if name == "stopped":
            tid = self._thread_id(body.get("task"))
            if tid is None:
                tid = int(body.get("taskId") or 0)
                self.threads.setdefault(tid, str(body.get("task")))
            event: Dict[str, Any] = {"reason": str(body.get("reason") or "pause"), "threadId": tid,
                                     "allThreadsStopped": True}
            if isinstance(body.get("location"), dict):
                event["description"] = body["location"].get("file")
                event["line"] = int(body["location"].get("line") or 1)
                if body["location"].get("col") is not None:
                    event["column"] = int(body["location"]["col"])
            # A task held at its scan boundary stops with no location, and so does a pause that
            # took the scan between two statements. DAP's StoppedEvent makes line and column
            # optional for exactly this, so nothing is invented to fill them in.
            self.event("stopped", event)
        elif name == "continued":
            tid = self._thread_id(body.get("task")) if body.get("task") else None
            event = {"allThreadsContinued": bool(body.get("allThreadsContinued", True))}
            if tid is not None:
                event["threadId"] = tid
            self.event("continued", event)
        elif name == "output":
            text = body.get("text")
            if text:
                self.event("output", {"category": "console", "output": str(text)})
        elif name == "runState":
            self._on_run_state(body)
        elif name == "sessionEnded":
            self.event("terminated", {"reason": body.get("reason")})
            self.close()

    def _on_run_state(self, body: Dict[str, Any]) -> None:
        """A run-state change is how the adapter learns the task table, not something a client needs
        told: this is an all-stop adapter, and DAP's `thread` event exists for non-stop mode. An
        event on every state change would be noise a client has to filter, and a filtered event is
        one a client eventually drops on the floor."""
        table: Dict[int, str] = {}
        for task in body.get("tasks") or []:
            if isinstance(task, dict) and isinstance(task.get("taskId"), int):
                table[int(task["taskId"])] = str(task.get("task"))
        if table:
            self.threads = table

    # -- the loop

    def serve(self, stream_in: Any, stream_out: Any) -> None:
        self.out = stream_out
        handlers = self.handlers()
        while not self.stop.is_set():
            try:
                raw = read_message(stream_in)
            except DapError as exc:
                self.send({"seq": 0, "type": "response", "request_seq": None, "command": None,
                           "success": False, "message": str(exc), "body": {}})
                self.close()
                return
            if raw is None:
                self.close()
                return
            try:
                request = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.send({"seq": 0, "type": "response", "request_seq": None, "command": None,
                           "success": False, "message": "not UTF-8 JSON: %s" % exc, "body": {}})
                continue
            if not isinstance(request, dict):
                self.send({"seq": 0, "type": "response", "request_seq": None, "command": None,
                           "success": False, "message": "a request must be a JSON object",
                           "body": {}})
                continue
            self._dispatch(request, handlers)

    def _dispatch(self, request: Dict[str, Any], handlers: Dict[str, Callable]) -> None:
        seq = request.get("seq")
        command = request.get("command")
        args = request.get("arguments") or {}
        if not isinstance(seq, int):
            self.send({"seq": 0, "type": "response", "request_seq": None, "command": command,
                       "success": False, "message": "a request needs an integer seq", "body": {}})
            return
        handler = handlers.get(str(command))
        if handler is None:
            self.send({"seq": 0, "type": "response", "request_seq": seq, "command": command,
                       "success": False,
                       "message": "unrecognized request %r; this adapter answers %s"
                                  % (command, ", ".join(sorted(SUPPORTED_REQUESTS))),
                       "body": {}})
            return
        try:
            body = handler(args if isinstance(args, dict) else {})
            self.send({"seq": 0, "type": "response", "request_seq": seq, "command": command,
                       "success": True, "body": body})
        except DapError as exc:
            self.send({"seq": 0, "type": "response", "request_seq": seq, "command": command,
                       "success": False, "message": str(exc),
                       "body": {"error": {"id": seq, "format": str(exc), **exc.details}}})
        except Exception as exc:  # noqa: BLE001 - a bug must read as a failure, not a dead pipe
            self.send({"seq": 0, "type": "response", "request_seq": seq, "command": command,
                       "success": False,
                       "message": "adapter error: %s" % exc,
                       "body": {"error": {"id": seq, "format": "%s" % exc,
                                          "showUser": False,
                                          "trace": traceback.format_exc()}}})


# Every DAP request name this adapter answers, read off the dispatch table so a handler cannot be
# added without this list saying so. The harness checks its scenario's requests against it from the
# OUTSIDE, which is the only check of "advertised and implemented" that is not the adapter marking
# its own homework.
SUPPORTED_REQUESTS: Tuple[str, ...] = tuple(sorted(Adapter("", None, False).handlers()))


# --------------------------------------------------------------------------
# DAP shapes built from wire shapes
# --------------------------------------------------------------------------


def _hit_count(raw: Any) -> Optional[int]:
    """DAP's `hitCondition` as the wire's `hitCount`.

    The wire's hit count is a number: the breakpoint stops on the n-th hit. DAP writes it as
    an expression over the hit count, and the form that says the same thing is `== n` (a bare
    `n` is read the same way). A form the wire cannot say is refused rather than guessed at.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.startswith("=="):
        text = text[2:].strip()
    elif text.startswith(">=") or text.startswith("<") or text.startswith("!="):
        return None
    try:
        n = int(text)
    except ValueError:
        return None
    return n if n > 0 else None


def _bp(wire: Dict[str, Any]) -> Dict[str, Any]:
    """The wire's `Breakpoint` as DAP's. `col` is DAP's `column`; `requestedLine` has no DAP field
    and is dropped rather than smuggled into `message`."""
    out: Dict[str, Any] = {"verified": bool(wire.get("verified")),
                           "line": int(wire.get("line") or 0)}
    if wire.get("id") is not None:
        out["id"] = int(wire["id"])
    if wire.get("col") is not None:
        out["column"] = int(wire["col"])
    if wire.get("message"):
        out["message"] = wire["message"]
    return out


def _pou_name(raw: Any) -> str:
    """A POU's name, upper-cased as a C++ symbol would be.

    debug-info.json carries STruCpp's generated symbol (`PIDCTL`, `FB_MOTOR`, `CELL`), which is not
    the POU name the source spells (`PidCtl`, `FB_Motor`, `Cell`). IEC 61131-3 6.1.2 makes an
    identifier case-insensitive, so the adapter does not lose information by saying so, and a
    client's variable view shows the source's spelling. The wire keeps its own casing untouched."""
    name = str(raw or "")
    return name if name.isupper() else name.upper()


def _frame(wire: Dict[str, Any]) -> Dict[str, Any]:
    """The wire's `StackFrame` as DAP's. `id` is the handle `scopes` takes, `source` gains the
    `name` DAP's Source requires (the wire carries only `path`)."""
    out: Dict[str, Any] = {"id": int(wire.get("id") or 0), "name": _pou_name(wire.get("name")),
                           "line": int(wire.get("line") or 1)}
    if wire.get("column") is not None:
        out["column"] = int(wire["column"])
    source = wire.get("source")
    if isinstance(source, dict) and source.get("path"):
        path = str(source["path"])
        out["source"] = {"name": os.path.basename(path), "path": path}
    return out


def _variable(wire: Dict[str, Any]) -> Dict[str, Any]:
    """The wire's `Variable` as DAP's. `data` is the wire's JSON value for the matcher; DAP's
    `Variable` has no field for it, and a client that wants the typed value reads `type`."""
    out: Dict[str, Any] = {"name": str(wire.get("name") or ""),
                           "value": "" if wire.get("value") is None else str(wire.get("value")),
                           "variablesReference": int(wire.get("variablesReference") or 0)}
    if wire.get("type"):
        out["type"] = str(wire["type"])
    if wire.get("evaluateName"):
        out["evaluateName"] = str(wire["evaluateName"])
    if wire.get("namedVariables") is not None:
        out["namedVariables"] = int(wire["namedVariables"])
    return out


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------


def _build(args: argparse.Namespace) -> Adapter:
    return Adapter(args.endpoint, args.token, args.insecure, args.seed, args.trace)


def serve_stdio(args: argparse.Namespace) -> int:
    adapter = _build(args)
    try:
        adapter.serve(sys.stdin.buffer, sys.stdout.buffer)
    finally:
        if adapter.wire is not None:
            adapter.wire.close()
    return adapter.exit_code


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:  # noqa: D102 - socketserver's contract
        args = self.server.args          # type: ignore[attr-defined]
        adapter = _build(args)
        try:
            adapter.serve(self.rfile, self.wfile)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if adapter.wire is not None:
                adapter.wire.close()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve_tcp(args: argparse.Namespace) -> int:
    server = _Server((args.listen.rsplit(":", 1)[0], int(args.listen.rsplit(":", 1)[1])),
                     _Handler)
    server.args = args                # type: ignore[attr-defined]
    sys.stderr.write("openplc DAP adapter listening on %s\n" % args.listen)
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="the OpenPLC Debug Adapter Protocol endpoint")
    ap.add_argument("--endpoint", default=os.environ.get("OPENPLC_DEBUG_URL",
                                                         "https://127.0.0.1:8443"),
                    help="the runtime's debug-session endpoint (URL)")
    ap.add_argument("--token", default=os.environ.get("OPENPLC_DEBUG_TOKEN"),
                    help="a JWT; a client may also pass `token` in its attach request")
    ap.add_argument("--insecure", action="store_true", help="do not verify the runtime's TLS cert")
    ap.add_argument("--listen", metavar="HOST:PORT",
                    help="serve DAP on a TCP socket instead of stdin/stdout")
    ap.add_argument("--seed", default="", choices=("", "steplie", "varcache", "nocred"),
                    help="a seeded adapter defect, for a criterion's Red-proof")
    ap.add_argument("--trace", metavar="PATH", help="append every message sent to the client here")
    args = ap.parse_args(argv)
    return serve_tcp(args) if args.listen else serve_stdio(args)


if __name__ == "__main__":
    sys.exit(main())