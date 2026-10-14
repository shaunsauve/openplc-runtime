# Debug core link

The internal link between the debug gateway (L3, `webserver/debug_gateway/`) and the core debug
agent (L2, `plc_main`), per ADR-003 D1.3, D2 and D9. It is not a client surface: the gateway
authenticates, owns the client sessions' roles, resolves symbols to probe IDs and serves the
public Socket.IO namespace in front of it (`docs/DEBUG_PROTOCOL.md`, "Session protocol v1").

This file is the **one** spec of the link. Both ends implement it, and the `linkspec` block below
is machine-checked against both: the fake core used by the gateway tests validates every request
it receives and is itself checked message by message (`tests/pytest/restapi/test_debug_gateway.py`,
`test_the_fake_core_speaks_the_link_spec`), and the real core's traffic is checked in the end to
end run (`tests/debug/plant/runctl_e2e.py`). The checker is `tests/debug/wire/link_spec.py`.

Code: `core/src/plc_app/debug_link.cpp` (the core end), `core/src/plc_app/debug_agent.cpp`
(breakpoint table, probe slow path), `core/src/plc_app/plc_state_manager.cpp` (park, resume,
pause), `webserver/debug_gateway/core_link.py` and `gateway.py` (the gateway end).
Tests: `tests/host/test_run_control.cpp` (core, host), `tests/pytest/restapi/test_debug_gateway.py`
(gateway against `tests/debug/wire/fake_core.py`), `tests/debug/plant/runctl_e2e.sh` (real
`plc_main`, probed `corpus:plant-io`, reference plant) and `tests/debug/plant/session_e2e.sh` (the
same plus the real gateway, driven by the headless harness).

Link version: `1.0`.

## Transport

- `/run/runtime/plc_debug.socket`, a unix stream socket. One peer at a time; a new connection
  replaces the old one. Frames are `u32 big-endian length` + UTF-8 JSON. A frame over 1 MiB, in
  either direction, closes the connection.
- The core pushes events unsolicited on the same connection, starting with `hello`.
- The same requests are accepted one per line on the legacy command socket
  (`/run/runtime/plc_runtime.socket`) as `DEBUG_LINK:<json>`, answered `DEBUG_LINK:<json>`. No
  events arrive there (so no `hello` either); it exists for callers that already hold that
  socket. Lines are capped at 8 KiB.

## Envelope (ADR-003 D2)

```jsonc
{"type":"request",  "seq":12, "command":"setBreakpoints", "arguments":{"session":"s1","fileId":0,"probes":[2]}}
{"type":"response", "seq":40, "request_seq":12, "command":"setBreakpoints", "success":true, "body":{…}}
{"type":"response", "seq":41, "request_seq":13, "command":"pause", "success":false,
 "error":{"code":"NOT_RUNNING", "message":"…"}}
{"type":"event",    "seq":42, "event":"stopped", "body":{…}}
```

`seq` is per sender and monotonic; a response echoes `request_seq` and `command`. Every failure
carries a code from ADR-003 D2's catalogue and a message. A code outside the catalogue reaches the
client as `INTERNAL` and is logged. Breakpoints are addressed by **probe ID** and file ID
(ADR-001 D1.2, ADR-002), never by name or line: resolution is the gateway's.

- An envelope that is not a request (not JSON, no string `command`, `type` not `request`) is
  refused `BAD_REQUEST`.
- A command the link does not define, or one this core does not implement (its capability is
  `false` in `hello`), is refused `UNSUPPORTED`.
- Arguments that do not match the shapes below are refused `BAD_REQUEST`.
- A `session` the core does not hold (detached, or from before the core restarted) is refused
  `SESSION_ENDED`.

## Handshake

The core sends `hello` as the first frame of every connection, and again whenever the loaded
program changes (load, unload, a different probe count or md5). The gateway serves nothing until
it arrives, and drops a link whose `link` major is not 1. `initialize` is **not** a link request:
it is the client's handshake with the gateway (ADR-003 D3), answered from `hello`.

`image` names the running program (ADR-002 D5). `md5` is the program's `strucpp_program_md5`.
`buildId` is the program's `strucpp_dbg_build_id`, and `null` while STruCpp does not emit that
symbol; the gateway then binds `debug-info.json` to the image by `md5` alone (see "pm rulings").
`buildTime` and `sourceHash` are `null` until a program exports them. Every field is `null` when
no program is loaded.

## Messages

Shapes: `{k: T}` an object (`k?` optional, `...` other keys allowed), `[T]` a list of `T`,
`A|B` either; leaf types `str`, `int`, `num`, `bool`, `null`, `obj`, `list`, `any`; a capitalised
name is defined in the block. `req` lines are `command req ARGUMENTS -> BODY`; `evt` lines are
`event evt BODY`. The core refuses a request whose arguments do not match; each body below is what
a successful response carries.

```linkspec
Image        = {md5: str|null, buildId: str|null, buildTime: str|null, sourceHash: str|null}
Caps         = {probes: bool, maxBreakpoints: int, control: bool, hitCount: bool, conditional: bool, ...}
Params       = {P1: num, P2: num}
Error        = {code: str, message: str, details?: obj}
TaskRef      = str|int
TaskState    = {task: str, taskId: int, state: str, reason?: str, probe?: int, heldAtBoundary?: bool, since?: num, scan?: int}
Location     = {task: str, taskId: int, probe?: int}
RunState     = {state: str, tasks: [TaskState], attached: bool, by?: str, location?: Location, since?: num}
BpResult     = {id: int|null, probe: int, verified: bool, error?: Error}

hello                   evt {link: str, engineAbi: int, capabilities: Caps, probes: int, image: Image, params?: Params}
beat                    evt {t: num}
stopped                 evt {reason: str, task: str, taskId: int, since: num, probe?: int, depth?: int, breakpointIds?: [int], hits?: int, heldAtBoundary?: bool, scan?: int, exception?: obj, session?: str}
continued               evt {allThreadsContinued?: bool, task?: str, taskId?: int}
runState                evt RunState
output                  evt {category: str, kind?: str, count?: int, task?: str, taskId?: int, scan?: int, text?: str}
sessionEnded            evt {session: str, reason: str}

attach                  req {user: str, role: str} -> {session: str}
detach                  req {session: str, reason?: str} -> {forces: list, safeStopped?: bool}
terminate               req {session: str} -> {}
beatClient              req {session: str} -> {}
status                  req {session?: str} -> RunState
setBreakpoints          req {session: str, fileId: int, probes: [int|obj]} -> {breakpoints: [BpResult]}
clearAllBreakpoints     req {session: str} -> {removed: int}
setExceptionBreakpoints req {session: str, filters: [str]} -> {filters: [str]}
continue                req {session: str, task?: TaskRef} -> {allThreadsContinued: bool}
pause                   req {session: str, task?: TaskRef} -> {}
next                    req {session: str, task?: TaskRef} -> {task: str, taskId: int}
stepIn                  req {session: str, task?: TaskRef} -> {task: str, taskId: int}
stepOut                 req {session: str, task?: TaskRef} -> {task: str, taskId: int}
cycleStep               req {session: str, task?: TaskRef} -> {}
runScans                req {session: str, n: int, task?: TaskRef} -> {}
runToCursor             req {session: str, probe: int, task?: TaskRef} -> {}
stop                    req {session: str} -> {}
restart                 req {session: str, kind?: str, startHalted?: bool} -> {}
setHaltMode             req {session: str, mode: str} -> {mode: str}
```

### Requests, in words

| Command | Semantics | Errors |
|---|---|---|
| `attach` | Opens a core session for a gateway client; `role` is `controller` or `observer`. The first session sets the program's session byte, so probed clones run; `by` in halted states is the controller's `user` | `BAD_REQUEST` |
| `detach` | Ends the session. Ending the controller's session clears its breakpoints and the trap and resumes every halted task; the last session clears the session byte. With `reason: "heartbeat_timeout"` (the gateway's P2 expiry, ADR-005 D4.6) the controller's session ends the same way except that, if any task is halted, the core safe-stops (`plc_request_safe_stop`, outputs held) instead of resuming, and answers `safeStopped: true` | `SESSION_ENDED` |
| `terminate` | As `detach`, then stops the program (DAP `terminate`) | `SESSION_ENDED`, `BUSY` |
| `beatClient` | The client's heartbeat (ADR-005 D4), forwarded by the gateway; the core keeps no P2 timer yet (see "Not built here") | `SESSION_ENDED` |
| `status` | The `runState` body, answered at once | — |
| `setBreakpoints` | Replaces the breakpoints of file `fileId` with `probes`. An item is a probe ID, or `{probe, condition?, hitCount?}` (`condition`: base64 bytecode ≤ 512 B, see below; `hitCount`: stop from the n-th hit). The result lists one `BpResult` per item, in order; one failing item (`INVALID_LOCATION`, `LIMIT_EXCEEDED`, `BAD_REQUEST`) does not fail the others. A breakpoint keeps its `id` while its probe stays armed | `NO_PROBES` |
| `clearAllBreakpoints` | Every file's breakpoints; `removed` counts them | `NO_PROBES` |
| `continue` | All-stop: resumes every halted task, whatever `task` says | `NOT_RUNNING` |
| `pause` | Halts `task`, or every task when absent; the `stopped` events follow | `NOT_RUNNING`, `BAD_REQUEST` (no such task) |
| `next`, `stepIn`, `stepOut` | Resumes only `task` (absent: the task of the most recent stop) to its next statement, over / into / out of calls; a `stopped` with reason `step` follows | `NOT_RUNNING`, `NOT_HALTED`, `BAD_REQUEST` |
| `stop` | Stops the program as STOP does; the session stays attached | `NOT_RUNNING`, `BUSY` |
| `setExceptionBreakpoints`, `cycleStep`, `runScans`, `runToCursor`, `restart`, `setHaltMode` | Defined by the link; the real core does not implement them yet and answers `UNSUPPORTED` (their capabilities are `false` in `hello`) | `UNSUPPORTED` |

Breakpoints survive a stop and are re-armed when the same program (same probe count) starts
again. The limit (`maxBreakpoints`, 1024) counts breakpoints, one per probe; an item past it is
refused and never displaces an armed one.

`condition` is a stand-in until ADR-007's VM: a stack program of `0x01 PUSH i32le`, `0x02 LT`,
`0x03 GT`, `0x04 EQ`, `0x05 AND`, `0x06 OR`, `0x07 NOT`, ending in `0x00 END` with one value on the
stack. It has no variable loads, so `capabilities.conditional` is `false`.

### Events, in words

| Event | When |
|---|---|
| `hello` | First frame on every connection; again when the loaded program changes |
| `beat` | Every 250 ms, from the agent thread (not blocked by `PLUGIN_CMD`); the gateway does not forward it |
| `stopped` | Once per halt of each task. `probe` is the halt probe (the gateway turns it into `location` and does not forward it); `breakpointIds` are the ids `setBreakpoints` returned; a task held at its scan boundary has `heldAtBoundary: true` and no probe |
| `continued` | On `continue` (`allThreadsContinued`) and on each step (`task`, `taskId`) |
| `runState` | On every change, and once to a newly connected peer. `state` is one of `RUNNING PAUSED HALTED_AT_BP STOPPED ERROR SAFE_STOPPED EMPTY INIT TRANSITIONING`: `HALTED_AT_BP` when any task is parked at a breakpoint, `PAUSED` when any task is otherwise halted. A task's `state` is `running held parked abandoning` |
| `output` | `category: "runtime"` with `kind` (`conditionError`, `breakpointSkippedLocked`) and `count`; `category: "print"` with `task`, `taskId`, `scan` and `text` |
| `sessionEnded` | The core ended `session` on its own (not sent by the real core yet) |

An event whose body has `session` goes only to that session's client; otherwise it is broadcast.

### pm rulings

Where ADR-003 is silent the link follows these rulings, applied 2026-10-14 when the gateway
(built against `fake_core.py`) and the core (built against `runctl_e2e.py`) were reconciled.
pm: please fold them into ADR-003 D1.3.

1. **The gateway's shapes win**, except that a task is carried both ways: `task` is the task's
   name and `taskId` its numeric index, in `stopped`, `continued`, `runState.tasks[]`,
   `runState.location` and the step responses. A request's `task` argument may be either.
2. **The core pushes `hello` on connect**, with `link`, `engineAbi`, `capabilities`, `probes`,
   `image` and optional `params`; `initialize` is a gateway↔client request only. The core
   **re-pushes `hello`** when the loaded program changes, so the gateway's image and probe count
   never go stale across a stop/start or an upload.
3. **Unknown command → `UNSUPPORTED`**, as is a link command the core has not built
   (capability `false`). A malformed envelope stays `BAD_REQUEST` (ADR-003 D2).
4. **`engineAbi` is an integer**, the probe ABI version (`STRUCPP_DBG_ABI_VERSION`, 1), not a
   `"major.minor"` string.
5. **`attach {user, role} → {session}`**: the core mints an opaque session ID; every other
   request except `status` carries it, and an unknown one is refused `SESSION_ENDED`. Role
   checks (controller only for control requests) stay in the gateway; the core tracks roles only
   to know whose detach clears breakpoints.
6. **`setBreakpoints {fileId, probes[]}` replaces that file's set**; an item is a probe ID or
   `{probe, condition?, hitCount?}`; failures are per item in `BpResult.error`, including
   `LIMIT_EXCEEDED`, never a failed request. The single-probe `setBreakpoint` and
   `clearBreakpoint` are gone.
7. **`stopped.breakpointIds`** (DAP `hitBreakpointIds`' role) carries breakpoint IDs, not probe
   IDs; `probe` is carried separately for the gateway's resolution.
8. **`continue` is all-stop** and answers `{allThreadsContinued: true}`; **a step without a
   `task`** steps the task of the most recent stop.
9. **`terminate`** ends the session and stops the program; **`detach`** answers `{forces: []}`
   until the force registry exists.
10. **The frame cap is 1 MiB in both directions** (the gateway had 16 MiB).
11. **Interim build binding:** while STruCpp does not emit `strucpp_dbg_build_id` (ADR-002 D1
    says it always does), `hello.image.buildId` is `null`, and the gateway binds
    `debug-info.json` and compares the client's identity by `md5` alone. Remove when STruCpp
    emits the symbol.
12. **Task names are IEC identifiers, compared case-insensitively.** The core reports a task's
    name as the program's configuration gives it, which STruCpp upper-cases (`TASK Main` runs as
    `MAIN`); the core matches a request's `task` name case-insensitively, and so does the harness.
    Showing the source spelling needs STruCpp to record task names in `debug-info.json`.

## Not built here

The gateway's status cache, `staleness_ms` and `NOT_RESPONDING` (D9.3-D9.4), the core's own P2
timer (ADR-005 D4.4: the P2 timer runs in the gateway for now and ends a lost session with
`detach {reason: "heartbeat_timeout"}`, so a gateway that dies while a task is halted leaves the
halt in place until the core timer exists), forces, variables and stack frames over the link, exception breakpoints, `cycleStep`,
`runScans`, `runToCursor`, `restart`, and non-stop halt mode (pause and breakpoints are all-stop).
