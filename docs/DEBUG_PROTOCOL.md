# Debug Protocol

## Overview

OpenPLC Runtime v4 provides a WebSocket-based debug interface for real-time variable inspection and modification. This allows debuggers (like OpenPLC Editor) to monitor and control PLC execution without the overhead of repeated HTTPS connections.

For detailed WebSocket connection and authentication information, see [webserver/DEBUG_WEBSOCKET.md](../webserver/DEBUG_WEBSOCKET.md).

## Connection Details

**Endpoint:** `wss://<host>:8443/api/debug`

**Authentication:** JWT token (via query parameter or auth dict)

**Protocol:** Socket.IO over WebSocket

## Debug Command Format

Debug commands are sent as hex strings representing binary protocol messages. The format follows the Modbus-style function code pattern.

### Command Structure

```
[Function Code] [Data Bytes...]
```

All commands and responses are represented as space-separated hex bytes (e.g., `"41 DE AD 00 00"`).

## Function Codes

### 0x41 - DEBUG_INFO

Get debug information about the PLC program.

**Request:**
```
41 DE AD 00 00
```

**Response:**
```
7E [info bytes...]
```

**Purpose:** Retrieve metadata about the loaded PLC program, including variable count and program hash.

---

### 0x42 - DEBUG_SET

Set trace flags for variables.

**Request:**
```
42 [variable index] [trace flag] [additional data...]
```

**Purpose:** Enable or disable tracing for specific variables. When tracing is enabled, variable values are logged or transmitted for monitoring.

**Trace Flags:**
- `0x00` - Disable tracing
- `0x01` - Enable tracing

---

### 0x43 - DEBUG_GET

Get trace data for variables.

**Request:**
```
43 [variable index] [count] [additional data...]
```

**Response:**
```
7E [trace data bytes...]
```

**Purpose:** Retrieve current values and trace information for specified variables.

---

### 0x44 - DEBUG_GET_LIST

Get values for a list of variables.

**Request:**
```
44 [count high] [count low] [index1 high] [index1 low] [index2 high] [index2 low] ...
```

**Example:**
```
44 00 03 00 00 00 01 00 02
```
(Get 3 variables: indexes 0, 1, 2)

**Response:**
```
7E [variable data...]
```

**Response Format:**
Each variable in the response includes:
- Variable index (2 bytes)
- Variable value (size depends on type)
- Variable type indicator

**Purpose:** Efficiently retrieve multiple variable values in a single request. This is the primary method used for polling variables during debug sessions.

---

### 0x45 - DEBUG_GET_MD5

Get MD5 hash of the loaded PLC program.

**Request:**
```
45 DE AD 00 00
```

**Response:**
```
7E [32 hex characters representing MD5 hash] 00
```

**Example Response:**
```
7E 61 62 63 64 65 66 31 32 33 34 35 36 37 38 39 30 31 32 33 34 35 36 37 38 39 30 31 32 33 34 35 36 00
```
(MD5: abcdef1234567890123456789012345678)

**Purpose:** Verify that the debugger is connected to the expected PLC program. The hash changes when a new program is loaded.

---

## Response Format

All successful responses start with `0x7E` (126 decimal, `~` character) as a success indicator.

Error responses may return different status codes or empty responses.

## Variable Types

Variables in the PLC program have different types based on IEC 61131-3 standard:

### Boolean Types
- `BOOL` - 1 bit (transmitted as 1 byte)

### Integer Types
- `SINT` - 8-bit signed integer
- `USINT` - 8-bit unsigned integer
- `INT` - 16-bit signed integer
- `UINT` - 16-bit unsigned integer
- `DINT` - 32-bit signed integer
- `UDINT` - 32-bit unsigned integer
- `LINT` - 64-bit signed integer
- `ULINT` - 64-bit unsigned integer

### Real Types
- `REAL` - 32-bit floating point
- `LREAL` - 64-bit floating point

### Time Types
- `TIME` - Duration
- `DATE` - Calendar date
- `TIME_OF_DAY` / `TOD` - Time of day
- `DATE_AND_TIME` / `DT` - Date and time

### String Types
- `STRING` - Variable-length string

### Bit String Types
- `BYTE` - 8 bits
- `WORD` - 16 bits
- `DWORD` - 32 bits
- `LWORD` - 64 bits

## Usage Examples

### Python Client

```python
import socketio

# Obtain JWT token first (see webserver/DEBUG_WEBSOCKET.md)
TOKEN = "your_jwt_token_here"

sio = socketio.Client(ssl_verify=False)

@sio.event(namespace='/api/debug')
def debug_response(data):
    if data.get('success'):
        print(f"Response: {data.get('data')}")
    else:
        print(f"Error: {data.get('error')}")

# Connect
sio.connect(
    'https://localhost:8443',
    auth={'token': TOKEN},
    namespaces=['/api/debug'],
    transports=['websocket']
)

# Get MD5 hash
sio.emit('debug_command', {
    'command': '45 DE AD 00 00'
}, namespace='/api/debug')

# Get variable list (variables 0, 1, 2)
sio.emit('debug_command', {
    'command': '44 00 03 00 00 00 01 00 02'
}, namespace='/api/debug')

sio.sleep(2)
sio.disconnect()
```

### JavaScript Client

```javascript
import io from 'socket.io-client';

// Obtain JWT token first
const token = 'your_jwt_token_here';

const socket = io('wss://localhost:8443/api/debug', {
  transports: ['websocket'],
  query: { token: token },
  rejectUnauthorized: false
});

socket.on('connected', (data) => {
  console.log('Connected:', data);

  // Get MD5 hash
  socket.emit('debug_command', {
    command: '45 DE AD 00 00'
  });
});

socket.on('debug_response', (response) => {
  if (response.success) {
    console.log('Data:', response.data);
  } else {
    console.error('Error:', response.error);
  }
});
```

## Polling Strategy

For continuous monitoring of variables:

1. **Connect** to WebSocket with JWT authentication
2. **Get MD5** to verify program identity
3. **Poll variables** using DEBUG_GET_LIST at regular intervals (e.g., every 100ms)
4. **Parse responses** to extract variable values
5. **Disconnect** when debug session ends

**Benefits over REST API:**
- Single TLS handshake for entire session
- Lower latency (no HTTP overhead)
- Bidirectional communication
- Persistent connection

## Implementation Details

### Runtime Side

The debug protocol is implemented in the PLC runtime core:

**Handler:** `core/src/plc_app/debug_handler.c`

**Key Functions:**
- `process_debug_data()` - Main dispatcher for function codes
- `debugInfo()` - Handles DEBUG_INFO (0x41)
- `debugSetTrace()` - Handles DEBUG_SET (0x42)
- `debugGetTrace()` - Handles DEBUG_GET (0x43)
- `debugGetList()` - Handles DEBUG_GET_LIST (0x44)
- `debugGetMD5()` - Handles DEBUG_GET_MD5 (0x45)

### Web Server Side

The WebSocket interface is implemented in the web server:

**Handler:** `webserver/debug_websocket.py`

**Key Functions:**
- `init_debug_websocket()` - Initialize Socket.IO server
- `handle_connect()` - Authenticate connections
- `handle_debug_command()` - Forward commands to runtime via Unix socket

### Communication Flow

```
[Debugger] --WebSocket--> [Web Server] --Unix Socket--> [PLC Runtime]
                                                              |
                                                         [Debug Handler]
                                                              |
                                                         [Variable Access]
```

## Security

### Authentication
- JWT token required for WebSocket connection
- Token obtained via REST API login endpoint
- Token validated on connection and can be revoked

### Authorization
- Debug access requires authenticated user
- Commands validated before execution
- Buffer overflow protection in command parsing

### Data Protection
- All communication encrypted via WSS (WebSocket Secure)
- Variable data transmitted in binary format
- No sensitive information in error messages

## Performance Considerations

### Latency
- WebSocket: ~1-5ms per command (local network)
- REST API: ~10-50ms per request (includes TLS handshake)

### Throughput
- Can handle hundreds of variable reads per second
- Limited by scan cycle time (typically 50ms)
- Batch requests with DEBUG_GET_LIST for efficiency

### Resource Usage
- One WebSocket connection per debug session
- Minimal CPU overhead (<1% for typical polling rates)
- Memory usage proportional to number of traced variables

## Troubleshooting

### Connection Refused

**Cause:** Invalid JWT token or authentication failure

**Solution:**
1. Obtain fresh JWT token via `/api/login`
2. Verify token is passed correctly (query param or auth dict)
3. Check token expiration

### No Response

**Cause:** Runtime not responding or command malformed

**Solution:**
1. Check PLC runtime status via `/api/status`
2. Verify command format (hex string with spaces)
3. Check runtime logs for errors

### Invalid Data

**Cause:** Variable index out of range or type mismatch

**Solution:**
1. Verify variable indexes with DEBUG_INFO
2. Check variable types in PLC program
3. Ensure program is loaded and running

### Disconnection

**Cause:** Network issue, timeout, or runtime restart

**Solution:**
1. Implement reconnection logic in client
2. Re-authenticate after reconnection
3. Verify program MD5 after reconnection

## Session protocol v1

The source-level debugger's protocol (ADR-003). It runs beside the legacy 0x41-0x45 part above,
which it does not change.

Protocol version: `1.0`. A client whose major differs is refused `PROTOCOL_VERSION`; minors are
additive, so a 1.x client and a 1.y runtime interoperate.

### Transport

- Socket.IO namespace `/api/debug-session` on the runtime's HTTPS port 8443, websocket
  transport only. The local path is the same endpoint at `wss://127.0.0.1:8443`.
- TLS only, loopback included (ADR-005 D2.2): the gateway refuses the namespace on any connection
  that did not arrive over TLS, so there is no plaintext fallback.
- One Socket.IO event name, `msg`, carries every message in both directions. A message is a
  JSON object (a JSON string of one is accepted from clients).
- A message above 1 MiB (1,048,576 bytes, serialized) is refused `LIMIT_EXCEEDED`, with the
  request's `seq` echoed when it could be read.
- Every other malformed message (not JSON, truncated, not an object, binary, nested deeper than
  the parser allows, no integer `seq`) is refused `BAD_REQUEST`; when no integer `seq` could be
  read the response has `request_seq: null` and `command: null`. A refusal never ends the
  session or touches the program.
- Connecting needs no credential; the JWT travels in `initialize`. Every request before a
  successful `initialize` is refused `BAD_REQUEST`.

### Envelope

```jsonc
{"type":"request",  "seq":12, "command":"pause", "arguments":{"task":"Main"}}
{"type":"response", "seq":40, "request_seq":12, "command":"pause", "success":true, "body":{}}
{"type":"response", "seq":41, "request_seq":13, "command":"next", "success":false,
 "error":{"code":"NOT_HALTED", "message":"The task is running, not halted.", "details":{}}}
{"type":"event",    "seq":42, "event":"stopped", "body":{"reason":"breakpoint", "task":"Main"}}
```

- `seq` is per sender and increasing. Every response echoes its request's `seq` as
  `request_seq`, so a client matches responses by `request_seq`, never by arrival order:
  responses to concurrent requests can arrive in any order.
- A failure carries `error.code` from the table under "Error codes", a plain-language
  `error.message` and, where useful, `error.details`.
- Undeclared arguments are ignored; a declared argument of the wrong type is `BAD_REQUEST`.

### Handshake

`initialize` must be the first request. Arguments:

| Argument | Type | Meaning |
|---|---|---|
| `protocol` | string, required | The client's protocol version, `"1.0"` |
| `token` | string | A JWT from `POST /api/login`; verified in full (signature, expiry, revocation, user). Required unless `resumeToken` is given |
| `role` | `"controller"` or `"observer"` | Default `controller`. One controller; up to 16 observers |
| `identity` | `{md5, buildId}` | The client's build, compared with the running image |
| `resumeToken` | string | Resume a session whose transport dropped, within P2 (see "Heartbeat, loss and resume"); `token`, `role` and `identity` are then ignored |

The response body:

| Field | Meaning |
|---|---|
| `protocol` | This runtime's protocol version (`"1.0"`) |
| `runtime` | The runtime's version string, as `GET /api/version` |
| `compiler` | `build.compiler` of the loaded `debug-info.json`, or null |
| `debugInfo` | `{major, minor}` of the loaded `debug-info.json`, or null |
| `engineAbi` | The core's engine ABI version |
| `image` | The running image `{md5, buildId, buildTime, sourceHash}` |
| `comparison` | `identity` against `image`: `match`, `source` (md5 differs), `build` (buildId differs) or `none` (no identity sent) |
| `params` | `{P1, P2}`: heartbeat interval, and the heartbeat timeout that is also the reconnect window, in seconds |
| `capabilities` | The flags below, each true only when this build serves it |
| `requests` | Every request name this runtime serves; a request not listed is refused `UNSUPPORTED` |
| `role` | The role granted |
| `locked` | Whether the production lock is on |
| `resumeToken` | A 128-bit random token bound to this session; present it to resume. A new one is issued on every resume and the old one is dead |
| `resumed` | `true` when the request resumed an existing session (absent otherwise) |

Capability flags: `probes`, `maxBreakpoints` (a count), `conditional`, `hitCount`, `logpoints`,
`functionBreakpoints`, `instanceBreakpoints`, `exceptionBreakpoints`, `nonStop`, `cycleStep`,
`runScans`, `startHalted`, `write`, `force`, `trace`, `liveWatch`, `sourceFetch`,
`onlineChange`, `control`, `modify`. `control:false` and `modify:false` mean a build with debug
control compiled out.

A stale client (`comparison` `source` or `build`) stays attached; every probe-dependent request
(marked "probe" below) is refused `STALE_BUILD` until it initializes again with the running
build. `initialize` on an attached connection replaces its session.

`version` returns the same `protocol`, `runtime`, `compiler`, `debugInfo` and `engineAbi`.

### Heartbeat, loss and resume

ADR-005 D2.4 and D4. P1 (default 2 s, 0.5-30 s) and P2 (default 15 s, 5-600 s) are read from
`<data dir>/debug.conf` as `P1 = <seconds>` and `P2 = <seconds>` lines when the webserver starts.
A file with a value out of range or with P2 < 3 x P1 is refused (logged) and the defaults apply.

- Any message from a client counts as its heartbeat; an otherwise idle client sends `heartbeat`
  every P1.
- A client's own Socket.IO disconnect is a detach. A dropped transport (a closed socket, a network
  blip) ends nothing: the session, its breakpoints and a halt are kept for P2, and
  `initialize {protocol, resumeToken}` on a new connection resumes it.
- When P2 passes with no message, the session ends with `sessionEnded {reason:
  "heartbeat_timeout"}`. A lost controller with a task halted safe-stops the PLC (outputs held,
  state `SAFE_STOPPED`, ADR-004 D7.3); with the program running it is a detach (breakpoints
  cleared, the program keeps running). A resume after that is refused `SESSION_ENDED` with
  `details.reason: "heartbeat_timeout"`.
- A resume is refused `UNAUTHORISED` for an unknown token, and ends the session (`revoked`) when the
  user was deleted or the session's login was revoked since.

### Requests

The "Needs" column lists the capability flags a request requires; a request is in the
handshake's `requests` exactly when this runtime implements it and every flag it needs is true.
Requests marked "planned" are part of protocol 1 but not served yet, so they are never in
`requests` and are refused `UNSUPPORTED`. "Gate": `control` requests need the controller role
and the lock off; `admin` requests need an administrator account.

| Request | Needs | Gate | Arguments, body |
|---|---|---|---|
| `initialize` | - | - | See "Handshake" |
| `attach` | - | - | Body `{role}`; DAP compatibility, the session is attached by `initialize` |
| `detach` | - | - | Ends the session. A controller's breakpoints are cleared and a halted program resumes. Body `{forces, safeStopped?}` |
| `terminate` | `control` | control | Stops the PLC and ends the session |
| `heartbeat` | - | - | Keeps the session alive (P1/P2); body `{}` |
| `status` | - | - | Body: the `runState` event's body, answered at once |
| `version` | - | - | See "Handshake" |
| `setLock` | - | admin | `{on, reason?}`; body `{on}` |
| `setBreakpoints` | `control` | control, probe | `{file, lines[]}` replaces the file's breakpoints. A line moves to the next line with code. Body `{breakpoints:[{id, verified, line, col, requestedLine}]}` |
| `breakpointLocations` | - | probe | `{file, line, endLine?}`; body `{breakpoints:[{line}]}` |
| `clearAllBreakpoints` | `control` | control | Body `{removed}` |
| `setExceptionBreakpoints` | `control`, `exceptionBreakpoints` | control | `{filters[]}`; body `{filters}` |
| `continue` | `control` | control | `{task?}`; all-stop, every halted task resumes. Body `{allThreadsContinued}` |
| `pause` | `control` | control | `{task?}` |
| `next` | `control` | control | `{task?}`; absent, the task of the latest stop. Body `{task, taskId}` |
| `stepIn` | `control` | control | `{task?}`; body `{task, taskId}` |
| `stepOut` | `control` | control | `{task?}`; body `{task, taskId}` |
| `cycleStep` | `control`, `cycleStep` | control | `{task?}` |
| `runScans` | `control`, `runScans` | control | `{n, task?}` |
| `runToCursor` | `control` | control, probe | `{file, line, task?}` |
| `stop` | `control` | control | Stops the PLC |
| `restart` | `control` | control | `{kind: warm\|cold\|reset, startHalted?}` |
| `setHaltMode` | `control`, `nonStop` | control | `{mode: allStop\|nonStop}`; body `{mode}` |
| `setFunctionBreakpoints` | `control`, `functionBreakpoints` | control | planned |
| `setInstanceBreakpoints` | `control`, `instanceBreakpoints` | control | planned |
| `threads` | - | - | planned |
| `stackTrace` | - | - | planned |
| `scopes` | - | - | planned |
| `variables` | - | - | planned |
| `evaluate` | - | - | planned |
| `setVariable` | `modify`, `write` | control | planned |
| `force` | `modify`, `force` | control | planned |
| `unforce` | `modify`, `force` | control | planned |
| `unforceAll` | `modify`, `force` | control | planned |
| `listForces` | `force` | - | planned |
| `subscribe` | `liveWatch` | - | planned |
| `unsubscribe` | `liveWatch` | - | planned |
| `traceStart` | `trace` | - | planned |
| `traceStop` | `trace` | - | planned |
| `traceRead` | `trace` | - | planned |
| `source` | `sourceFetch` | - | planned |
| `sync` | `sourceFetch` | - | planned |
| `requestControl` | - | - | planned |
| `answerControlRequest` | - | - | planned |
| `takeControl` | - | - | planned |
| `releaseControl` | - | - | planned |
| `auditRead` | - | admin | planned |

### Events

Pushed, never polled. The controller receives every event; observers receive `stopped`,
`continued`, `runState`, `output`, `values`, `time`, `control`, `sessionEnded` and
`programReplaced`.

| Event | Body |
|---|---|
| `stopped` | `{reason: breakpoint\|step\|pause\|entry\|exception, task, taskId, since, scan?, location?:{file, line, col}, depth?, breakpointIds?, hits?, heldAtBoundary?, exception?}`; one per stop. `task` is the task's name (an IEC identifier: compare it case-insensitively), `taskId` its index. `location` is absent for a task held at its scan boundary (`heldAtBoundary: true`); `depth` is the call depth of the stop and `hits` the breakpoint's hit count |
| `continued` | `{allThreadsContinued: true}` on `continue` (all-stop), `{task, taskId}` on a step |
| `runState` | `{state, tasks:[{task, taskId, state, reason?, probe?, heldAtBoundary?, since?, scan?}], attached, by?, location?:{task, taskId, probe?}, since?}`; on every run-state change. `state` is `RUNNING`, `PAUSED`, `HALTED_AT_BP`, `STOPPED`, `ERROR`, `SAFE_STOPPED`, `EMPTY`, `INIT` or `TRANSITIONING` |
| `output` | `{category: logpoint\|print\|runtime\|plugin, task?, taskId?, scan?, text?, kind?, count?}`: `print` carries `task`, `taskId`, `scan` and `text`; `runtime` carries `kind` (`conditionError`, `breakpointSkippedLocked`) and `count` |
| `control` | `{controller, observers}`: the controller's user name (or null) and the observer count; on every attach and detach |
| `sessionEnded` | `{reason}`: `revoked` (the login was revoked or the account removed), `heartbeat_timeout` (no message for P2), `core_lost` (the core link dropped) or a reason from the core |

### Message shapes

Every served request's arguments and success body, and every event's body, in the grammar of
[DEBUG_CORE_LINK.md](DEBUG_CORE_LINK.md) ("Messages": `{k: T}` an object, `k?` optional, `...`
other keys allowed, `[T]` a list, `A|B` either). The gateway tests and the real-core end-to-end
run check every message on the wire against this block (`tests/debug/wire/link_spec.py`), so a
field the runtime sends that is not here, or a field here the runtime never sends, fails them.
A failure response carries `Error` instead of `body`; a reply to a message whose `seq` could not
be read has `request_seq: null` and `command: null`.

```sessionspec
Error        = {code: str, message: str, details?: obj}
Image        = {md5: str|null, buildId: str|null, buildTime: str|null, sourceHash: str|null}
Versions     = {protocol: str, runtime: str, compiler: str|null, debugInfo: {major: int, minor: int}|null, engineAbi: int|null}
Handshake    = {protocol: str, runtime: str, compiler: str|null, debugInfo: {major: int, minor: int}|null, engineAbi: int|null, image: Image, comparison: str, params: {P1: num, P2: num}, capabilities: obj, requests: [str], role: str, locked: bool, resumeToken: str, resumed?: bool}
Location     = {file: str, line: int, col: int|null}
TaskState    = {task: str, taskId: int, state: str, reason?: str, probe?: int, heldAtBoundary?: bool, since?: num, scan?: int}
RunState     = {state: str, tasks: [TaskState], attached: bool, by?: str, location?: {task: str, taskId: int, probe?: int}, since?: num}
Breakpoint   = {id: int|null, verified: bool, line: int, col: int|null, requestedLine: int, message?: str}
Step         = {task: str, taskId: int}

stopped                 evt {reason: str, task: str, taskId: int, since: num, scan?: int, location?: Location, depth?: int, breakpointIds?: [int], hits?: int, heldAtBoundary?: bool, exception?: obj}
continued               evt {allThreadsContinued?: bool, task?: str, taskId?: int}
runState                evt RunState
output                  evt {category: str, kind?: str, count?: int, task?: str, taskId?: int, scan?: int, text?: str}
control                 evt {controller: str|null, observers: int}
sessionEnded            evt {reason: str}

initialize              req {protocol: str, role?: str, token?: str, identity?: obj, resumeToken?: str} -> Handshake
attach                  req {} -> {role: str}
detach                  req {} -> {forces: list, safeStopped?: bool}
terminate               req {} -> {}
heartbeat               req {} -> {}
status                  req {} -> RunState
version                 req {} -> Versions
setLock                 req {on: bool, reason?: str} -> {on: bool}
setBreakpoints          req {file: str, lines: [int]} -> {breakpoints: [Breakpoint]}
breakpointLocations     req {file: str, line: int, endLine?: int} -> {breakpoints: [{line: int}]}
clearAllBreakpoints     req {} -> {removed: int}
setExceptionBreakpoints req {filters: [str]} -> {filters: [str]}
continue                req {task?: str} -> {allThreadsContinued: bool}
pause                   req {task?: str} -> {}
next                    req {task?: str} -> Step
stepIn                  req {task?: str} -> Step
stepOut                 req {task?: str} -> Step
cycleStep               req {task?: str} -> {}
runScans                req {n: int, task?: str} -> {}
runToCursor             req {file: str, line: int, task?: str} -> {}
stop                    req {} -> {}
restart                 req {kind?: str, startHalted?: bool} -> {}
setHaltMode             req {mode: str} -> {mode: str}
```

### Error codes

Every failure carries one of these codes. The message names the problem in a sentence.

| Code | When |
|---|---|
| `PROTOCOL_VERSION` | `initialize` with an incompatible major; `details: {client, server}` |
| `UNAUTHORISED` | `initialize` with no token, or a token that is invalid, expired or revoked, or an unknown `resumeToken` |
| `FORBIDDEN` | The user lacks the permission (an `admin` request from a non-administrator) |
| `NOT_CONTROLLER` | A control or modify request from an observer, or a second controller; `details.controller` names the holder |
| `LOCKED` | A control or modify request while the production lock is on |
| `UNSUPPORTED` | A request this runtime does not serve (not in the handshake's `requests`), including a compiled-out build |
| `NO_PROBES` | A probe-dependent request on a probe-off build, or with no debug information loaded |
| `STALE_BUILD` | A probe-dependent request while the client's build or the loaded debug information differs from the running program; `details.comparison` or `details.reason` |
| `UNSUPPORTED_DEBUG_INFO` | The loaded debug information has an unsupported major; `details: {found, supported}` |
| `INVALID_LOCATION` | No such source file |
| `NO_CODE` | No line with code at or after the requested line |
| `NOT_HALTED` | A step or continue while the task is not halted |
| `NOT_RUNNING` | A request that needs the program running (a second `stop`, a `pause` while stopped) |
| `LIMIT_EXCEEDED` | A message above 1 MiB, or more breakpoints than `maxBreakpoints` |
| `BAD_REQUEST` | A malformed message, a wrong argument type, or a request before `initialize` |
| `TYPE_ERROR` | A write, force or expression value of the wrong type |
| `RANGE_ERROR` | A write, force or expression value out of range, naming the constraint |
| `EXPR_SIDE_EFFECT` | An expression that would change program state |
| `EXPR_LIMIT` | An expression over the evaluation limits |
| `EXPR_SYNTAX` | An expression that does not parse |
| `NO_SOURCE` | The runtime cannot supply a file's source |
| `BUSY` | A PLC state transition is in flight; or a 17th observer (`details.reason: "observer_limit"`) |
| `SESSION_ENDED` | A request on a session that ended, or a resume of one; `details.reason` (`detached`, `terminated`, `revoked`, `heartbeat_timeout`, `core_lost`) |
| `INTERNAL` | A runtime bug, or the core unreachable over its debug link; logged |
| `SAFE_STOPPED` | A write, force or online change while the runtime is safe-stopped |
| `CHANGE_REFUSED` | An online change the classifier refuses; `details.reason` |
| `CHANGE_FAILED` | An online change that failed and was rolled back; `details.phase` |

### Core link

Internal, not a client surface: the gateway (webserver) and the core agent (`plc_main`) over
`/run/runtime/plc_debug.socket`, `u32` BE length + UTF-8 JSON frames in this protocol's envelope.
Its one spec, with every request, event and shape both ends implement, is
[DEBUG_CORE_LINK.md](DEBUG_CORE_LINK.md). Symbolic resolution stays in the gateway: the core sees
probe IDs and file IDs from `debug-info.json`, never names.

## Related Documentation

- [Editor Integration](EDITOR_INTEGRATION.md) - How OpenPLC Editor connects to runtime
- [webserver/DEBUG_WEBSOCKET.md](../webserver/DEBUG_WEBSOCKET.md) - WebSocket connection details
- [API Reference](API.md) - REST API endpoints
- [Architecture](ARCHITECTURE.md) - System overview
- [Security](SECURITY.md) - Authentication and authorization
