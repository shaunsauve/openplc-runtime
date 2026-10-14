# Debug core link

The internal link between the debug gateway (L3, `webserver/`) and the core debug agent (L2,
`plc_main`), per ADR-003 D1.3 and D9. It is not a client surface: the gateway authenticates,
resolves symbols to probe IDs and serves the public Socket.IO namespace in front of it.

Code: `core/src/plc_app/debug_link.cpp` (this protocol), `core/src/plc_app/debug_agent.cpp`
(breakpoint table, probe slow path), `core/src/plc_app/plc_state_manager.cpp` (park, resume,
pause). Tests: `tests/host/test_run_control.cpp` (host), `tests/debug/plant/runctl_e2e.sh` (real
`plc_main`, probed `corpus:plant-io`, reference plant).

## Transport

- `/run/runtime/plc_debug.socket`, a unix stream socket. One peer at a time; a new connection
  replaces the old one. Frames are `u32 big-endian length` + UTF-8 JSON. A frame over 1 MiB
  closes the connection.
- The core pushes events unsolicited on the same connection.
- The same requests are accepted one per line on the legacy command socket
  (`/run/runtime/plc_runtime.socket`) as `DEBUG_LINK:<json>`, answered `DEBUG_LINK:<json>`. No
  events arrive there; it exists for callers that already hold that socket. Lines are capped at
  8 KiB.

## Envelope (ADR-003 D2)

```jsonc
{"type":"request",  "seq":12, "command":"setBreakpoint", "arguments":{"probe":2}}
{"type":"response", "seq":40, "request_seq":12, "command":"setBreakpoint", "success":true, "body":{…}}
{"type":"response", "seq":41, "request_seq":13, "command":"setBreakpoint", "success":false,
 "error":{"code":"LIMIT_EXCEEDED", "message":"…"}}
{"type":"event",    "seq":42, "event":"stopped", "body":{…}}
```

`seq` is per sender and monotonic. Breakpoints are addressed by **probe ID** (ADR-001 D1.2),
never by file and line: resolution is the gateway's.

## Requests

| Command | Arguments | Body on success | Errors |
|---|---|---|---|
| `initialize` | `protocol` | `protocol` (`"1.0"`), `engineAbi` (1), `probes` (count, 0 = probe-off build), `maxBreakpoints` (1024), `capabilities {probes, maxBreakpoints, hitCount, conditional, control}` | — |
| `attach` | `by` (who, shown in halted states) | `{}`; sets the program's session byte, so probed clones run | — |
| `detach` | — | `{}`; clears every breakpoint and the trap, resumes every task, clears the session byte | — |
| `status` | — | the `runState` body below | — |
| `setBreakpoint` | `probe`, `condition` (base64 bytecode ≤ 512 B, optional), `hitCount` (stop from the n-th hit, optional) | `{probe, verified:true}`; a set on an armed probe replaces it and keeps its hit count | `NO_PROBES`, `INVALID_LOCATION`, `LIMIT_EXCEEDED`, `BAD_REQUEST` (condition) |
| `setBreakpoints` | `breakpoints: [{probe, condition?, hitCount?}]` | `{breakpoints: [{probe, verified, error?:{code,message}}]}`, in order; one item failing does not fail the others | `NO_PROBES`, `BAD_REQUEST` |
| `clearBreakpoint` | `probe` | `{}` | `NO_PROBES`, `INVALID_LOCATION` |
| `clearAllBreakpoints` | — | `{removed}` | `NO_PROBES` |
| `pause` | `task` (optional; absent = every task) | `{}`; the `stopped` events follow | `NOT_RUNNING`, `BAD_REQUEST` (no session, no such task) |
| `continue` | — | `{allThreadsContinued:true}`; resumes every halted task | `NOT_RUNNING` |
| `next`, `stepIn`, `stepOut` | `task` | `{task}`; only that task resumes; a `stopped` with reason `step` follows | `NOT_RUNNING`, `NOT_HALTED`, `BAD_REQUEST` |
| `stop` | — | `{}`; stops the program as STOP does; the session stays attached | `NOT_RUNNING`, `BUSY` |

Breakpoints survive a stop and are re-armed when the same program (same probe count) starts
again. The limit counts breakpoints, one per probe; a request past it is refused and never
displaces an armed one.

`condition` is a stand-in until ADR-007's VM: a stack program of `0x01 PUSH i32le`, `0x02 LT`,
`0x03 GT`, `0x04 EQ`, `0x05 AND`, `0x06 OR`, `0x07 NOT`, ending in `0x00 END` with one value on the
stack. It has no variable loads, so `capabilities.conditional` is `false`.

## Events

| Event | Body | When |
|---|---|---|
| `stopped` | `reason` (`breakpoint`, `pause`, `step`), `task`, `taskName`, `since` (epoch ms); at a probe also `probe`, `depth`, and for a breakpoint `hitBreakpointIds`, `hits`; a task held at its scan boundary has `heldAtBoundary:true` and no probe | once per halt of each task |
| `continued` | `allThreadsContinued` or `task` | on `continue` and steps |
| `runState` | see below | on every change, and once to a newly connected peer |
| `beat` | `t` (epoch ms) | every 250 ms, from the agent thread (not blocked by `PLUGIN_CMD`) |
| `output` | `category:"runtime"`, `kind` (`conditionError`, `breakpointSkippedLocked`), `count` | when a condition could not be evaluated, or a breakpoint was reached while its task held a shared-global lock (ADR-001 D1.3) |

`runState` body (also the `status` response):

```jsonc
{"state": "HALTED_AT_BP",          // RUNNING PAUSED HALTED_AT_BP STOPPED ERROR SAFE_STOPPED EMPTY INIT TRANSITIONING
 "tasks": [{"task":0, "name":"MAIN", "state":"parked",   // running held parked abandoning
            "reason":"breakpoint", "probe":2, "since":1791972049648.6}],
 "by": "alice", "location": {"task":0, "probe":2}, "since": 1791972049648.6,   // while halted
 "attached": true}
```

HALTED_AT_BP when any task is parked at a breakpoint, PAUSED when any task is otherwise halted.

## Not built here

The gateway's cache, `staleness_ms` and `NOT_RESPONDING` (D9.3-D9.4), `hello`/`beatClient`,
session ownership and the P2 timer (ADR-005), forces, variables and stack frames over the link,
`cycleStep`, `runScans`, `runToCursor`, and non-stop halt mode (pause and breakpoints are
all-stop).
