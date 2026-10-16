# Debug verification corpus

ST programs that the source-level debugger is measured against, each with one or more
**oracles**: machine-checkable expected traces. Every acceptance criterion (`A###` in the
orchestration root's `docs/ACCEPTANCE.md`) names the `corpus:<name>` it runs against; the
directory of that name is here.

```
tests/debug/corpus/
  README.md               this file: the program contract and the oracle format
  oracle.schema.json      JSON Schema 2020-12 for an oracle (format 1.1)
  validate.py             validates every header and oracle (Python stdlib only)
  test_validate.py        red/green proof for validate.py
  build_all.sh            compiles every program with STruCpp, then runs validate.py
  <name>/program.st       the program (main source; more .st files allowed)
  <name>/<id>.oracle.json one scenario each
  <name>/variant-*.st     a whole alternative build the scenarios apply (online change, downloads); must compile
  <name>/reject-*.st      a construct the compiler must refuse (ADR-001 D1.3 S8: shared-globals, multi-task);
                          must fail to compile, leave no code, and name its `expect-diagnostic:` header text
  <name>/lib/*.st         a user library, compiled with --no-source and linked (library-pou)
  <name>/gen.py           generator of program.st when it is too big to hand-write (large-data); `--check` in build_all.sh
  <name>/*.origin.json    the LD/FBD transpiler's source map (ADR-002 D6) for ld-sample and fbd-sample
  <name>/plugins.json     runtime plugin configuration the scenarios assume (consumer-shape)
  bench/family.txt        corpus:bench is a family: its members are bench_hot, bench_call, bench_mix
```

`build_all.sh` compiles with the STruCpp CLI (`node $STRUCPP/dist/node/cli.js`, building
`dist/` first if missing), adds `--debug-artifacts` once the CLI offers it (ADR-002 D7), and
ends by running `validate.py`.

Run `python3 tests/debug/corpus/validate.py` (add `--acceptance <root>/docs/ACCEPTANCE.md
--catalog <root>/docs/drafts/feature-catalog.md` to cross-check criteria and FC tags,
`--strict` to count the warnings below as problems), and
`python3 -m unittest tests/debug/corpus/test_validate.py`.

Three things are reported as warnings rather than problems, because each says the corpus has not
caught up with something outside itself and a run that fails on them cannot be made green without
doing that work: the oracle ↔ `ACCEPTANCE.md` join (a scenario claims a criterion whose `Test:` is
`—`, names other scenarios, or does not name it; and a `Test:` naming a scenario that does not cite
it), a `build-record.txt` whose sources or compiler have moved since the build, and an absent
`build/` directory. `--write-coverage` regenerates `tests/debug/harness/coverage.txt`'s index block,
which is checked as a problem, because a file this script owns can only be wrong by not being
regenerated.

## Program contract

**Header.** The first `(* … *)` comment of `program.st` holds `key: value` lines. A line
indented by 5+ spaces continues the previous key.

| Key | Meaning |
|---|---|
| `corpus:` | the directory name |
| `criteria:` | every `A###` whose `Corpus:` field names this program (checked by `--acceptance`) |
| `fc:` | FC IDs the program exercises (F147; checked by `--catalog`) |
| `witness:` | `<task> <instance>.<var> <%Q address>`, one line per task |
| `tags:` | `bench` for the bench family (ADR-001 D10.1) |
| `sources:` | compile order when there is more than one `.st` (default: `program.st` alone) |
| `build:` | `expect-fail <reason>` when the program needs a compiler or runtime feature that does not exist yet; `build_all.sh` reports it, and flags it when it starts compiling |
| `ASSUMED:` | a reading of a vague corpus description, to be confirmed |

**Scan witness** (ACCEPTANCE § Corpus contract, A149). Each task's first program declares
`<var> AT %QDn : DINT` and its first statement is `<var> := <var> + 1;`. The reference
plant counts scans from it. `validate.py` enforces both. `variant-*.st` files (online-change
variants) are checked by the build, not by the witness lint.

**Instance paths** follow ADR-002 `instances.path`: the configuration's program instance name
(`PROG0`, or `PROG_FAST`/`PROG_SLOW` for multi-task) then members, e.g. `PROG0.motor1.pid`.
Globals are addressed by their bare name.

**Markers.** A line an oracle refers to ends with a comment `(*@NAME*)`. Oracles say
`"line": "@NAME"`, never a number, so editing a program does not silently invalidate its
oracles; `validate.py` resolves each marker to exactly one line and checks that a stated
`col` (1-based) starts a statement there.

## Oracle format (1.1)

One JSON file per scenario, `<name>/<id>.oracle.json`, `scenario` = `<name>/<id>`.

```jsonc
{
  "format": "openplc-debug-oracle", "major": 1, "minor": 1,
  "scenario": "plant-io/bp-before-statement", "corpus": "plant-io",
  "kind": "runtime",                 // "build": a debug-info / generated-code inspection, no runtime;
                                     // "diagnostic": a bisect (§ Coverage)
  "criteria": ["A034", "A031"],      // the criteria this scenario discharges (⊆ the header's); [] only for a diagnostic
  "title": "...",
  "setup": {                          // all optional
    "build": {"probes": true, "variant": "variant-inplace.st", "path": "library|cli|editor", "control": true},
    "haltPolicy": "hold|safe", "safeValues": {"%QW0": 5}, "haltMode": "allStop|nonStop",
    "startHalted": false, "session": "none|controller|observer",
    "tasks": {"Main": {"intervalMs": 500}}, "params": {"P2_s": 15},
    "plant": {"samplePeriodMs": 10, "inputs": {"%IX0.0": true},
              "schedule": [{"atMs": 1000, "set": {"%IX0.0": true}}]}
  },
  "steps": [ ... ]                    // executed in order
}
```

`setup.plant.schedule` is the plant's scripted input schedule: each entry's inputs are set when
the plant clock, the fastest task's PLC virtual time (ADR-004 D4.3), reaches `atMs` after the
scenario starts, so a halt delays the schedule by exactly the halt (A152).

A **major** bump is incompatible; a **minor** bump only adds optional fields (as ADR-002 D2).
1.1 adds `client`, `loop`, argument symbols, the `inspect` views, `inject` results and
`maxGapMs`, and fixes one meaning 1.0 left open: a request `repeat` is a batch (§ Repetition).
1.0 runners paired a request `repeat n` with a following event `repeat n`; every corpus file
that relied on it now says `loop`, and no 1.0 file exists outside this corpus. 1.1 also pins the `throughout`
window to "since the previous plant observation": the 1.0 runner started it at the end of the
step just before, so after a `wait` it held no samples and checked nothing. Later 1.1 additions (2026-10-14): the `~contains`, `~notRef` and `~sha256` matchers and the
`~token: "user:<name>"` symbol; the runner now fails a matcher key it does not know instead of
skipping it, so an oracle can never pass on a runner that predates its matcher.

### Steps

Each step has exactly one action key, plus optional `note`, and on `request`, `event`, `noEvent`,
`observe: session` and `inject: killClient` an optional `client` (§ Clients).

| Step | Meaning |
|---|---|
| `{"request": <command>, "args": {…}, "expect": {"success": b, "error": <code>, "body": <match>}, "repeat": n}` | Send an ADR-003 D4 request (`initialize`, `setBreakpoints`, `continue`, `next`, `cycleStep`, `force`, …). `error` is an ADR-003 D2 code. Lines in `args` may be markers; values may be argument symbols (§ Argument symbols). `repeat` is a batch (§ Repetition) |
| `{"event": <name>, "withinMs": t, "expect": {…}, "count": <match>, "repeat": n, "thenContinue": b}` | The next event of that name (ADR-003 D4: `stopped`, `continued`, `runState`, `output`, `values`, `forces`, `time`, `sessionEnded`, …) arrives within `t` and matches, on every stream the step names; with `count`, the number of those streams that receive a matching one must match it instead. `repeat` waits for n successive events; `thenContinue` sends `continue` after each repetition (loop hit counts; one stream only) |
| `{"loop": n, "steps": [ … ]}` | Run the body steps in order, n times: the alternating form (§ Repetition). Bodies may nest |
| `{"noEvent": <name>, "forMs": t \| "forScans": n, "task": …}` | No such event in the window (scans counted by the plant) |
| `{"plant": {"set": {addr: v}, "sequence": [{…}, …], "failExchanges": n}}` | Drive plant inputs: one write, or successive writes (latest-wins tests, A055) |
| `{"wait": {"wallMs": t} \| {"vtMs": t} \| {"scans": n}}` | Wait on the harness wall clock, on PLC virtual time, or for n plant-counted scans |
| `{"observe": "plant\|status\|session\|log", "expect": {…}, "throughout": b}` | Read back through an independent path. `throughout: true` (plant) means the expectation held for every plant sample since the previous plant observation, or since the scenario started |
| `{"inspect": "debugInfo\|debugMap\|generatedCpp\|objectCode", "expect": <match>}` | Read the program's compiler outputs (ADR-002 D1 files), in a build or a runtime scenario; matched against the view of § Inspect views |
| `{"inject": <kind>, "args": {…}, "expect": <match>}` | Fault injection (F154) and drives from outside the session. `killClient` drops the step's stream with no detach. `dropNetwork`, `restoreNetwork`, `delayNetwork` (`rttMs`), `rawFrame` (`frame`: `truncated`, `invalid`, `oversize`, `garbage`, with `bytes`/`count`), `sigstopRuntime`, `sigcontRuntime`, `killRuntime`, `restartRuntime` act on the transport or the process; `upload` (`build`, `debugInfo` patch, `conf`), `rest` (`method`, `path`, `user`), `socket` (`command`: a legacy core-socket line), `plugin` (`name`, `action`) drive the runtime the way its other clients do. `writeFile` (`path` under the runtime's data directory, `content`) replaces a runtime file between a stop and a start, as an operator or a crash would leave it (A216's unreadable retain record). Until run control exists, `plugin` with `{"name": "reference-plant", "action": "hold"|"resume", "task": <task name>}` halts and resumes tasks through the reference plant's test hook (`plc_run_control_hold`); it answers `true` when applied. `expect` matches what the injection answered: the response envelope, the HTTP `{status, body}`, or the socket reply line |

### Expectations

A `stopped` event expectation (ADR-003 D4 `stopped`, F008/F148):

```jsonc
{"reason": "breakpoint|step|pause|entry|exception", "task": "Main",
 "location": {"file": "program.st", "line": "@K", "col": 3, "endLine": "@K2", "boundary": true},
 "stack": [ {"pou": "PID", "kind": "fb", "instance": "PROG0.motor1.pid",
             "location": {"file": "program.st", "line": "@PID_BODY"},
             "scopes": {"locals": {"PROG0.motor1.pid.err": 0.5}}} , … ],   // innermost first
 "variables": {"PROG0.counter": {"~capture": "c"}},
 "exception": {"kind": "divByZero"}, "scan": <match>, "vtMs": <match>}
```

The harness obtains `stack` with `stackTrace` and `variables` with `variables`/`evaluate`, both
on the session. A `location` with `"boundary": true` and `"line": null` is the scan boundary
(A043). `kind` and `pou` are ADR-002 `pous[].kind` and `pous[].name`; `instance` is
`instances.path` (`null` for a function).

An `observe` expectation uses these keys (any subset):

| Key | Read from | Holds |
|---|---|---|
| `witness` | plant | `{task: match}`: the plant's last received scan-witness value |
| `scanDelta` | plant | `{task: match}`: witness increments since the previous plant observation |
| `vtMs` | plant | `{task: match}`: task virtual time (ADR-004 D4) as the plant sees it |
| `outputs` | plant | `{"%QW0": match}`: last value received, by address |
| `variables` | session | `{path: match}` |
| `state` | status | the run state (F040: `RUNNING`, `PAUSED`, `HALTED_AT_BP`, `STOPPED`, `SAFE_STOPPED`, `ERROR`, plus `NOT_RESPONDING`) |
| `fields` | status / log | other fields, e.g. `{"reason": "heartbeat_timeout"}`, `{"lastFault": {…}}`; the log view is `{"lines": [text, …]}` (ASSUMED until the runtime has a log read-back path), matched with `~contains` |
| `maxGapMs` | plant | `{task: match}`: the longest stretch, in ms, between scan-witness increments over the same window as `throughout` (a freeze or a stall shows as a gap) |

**ADR-004 arithmetic the oracles rely on.** Halted mid-scan k: the session reads the witness
as k; the plant holds k − 1 until the scan completes (D1.1). A cycle step is exactly +1 scan
and +1 interval of virtual time; statement steps add 0 (D4). A halt adds at most one interval
of virtual time (D4.2).

### Clients

A step without `client` acts on the stream `main`, which opens on first use and takes the
setup's `session` role. `client` names others:

| `client` | Streams |
|---|---|
| `"c2"` | the stream named `c2`, opened on first use (its `initialize` carries its own `role`) |
| `{"group": "obs", "count": 16}` | `obs.1` … `obs.16`, opened as needed |
| `{"group": "obs"}` | every open stream of the group |
| `"*"` | every open stream, `main` included |

A request is sent on each stream in order and each response must match. An event step waits on
each stream with one shared deadline; without `count` every stream must receive a matching event,
with `count` the number that did is matched against it. `inject: killClient` closes the named
streams' transports with no detach; the stream name can be reused later for a new client. Each
stream has its own inbox, so one stream's events never satisfy another's step. A capture made
on several streams keeps the last stream's value.

### Argument symbols

Inside `args` (of a request or an injection), an object whose keys all start with `~` is replaced
before sending:

| Symbol | Value sent |
|---|---|
| `{"~ref": "max"}`, `{"~ref": "max", "~add": 1}` | the captured value (plus n): a declared limit, a resume token, an ID from an earlier response |
| `{"~build": "identity"}`, `{"~build": "identity:variant-x.st"}` | the `{md5, buildId}` of this program's build, or of that variant's build (ADR-002 D5), from the build outputs |
| `{"~lines": n}` | the first n breakpoint-able lines of the request's `file` (debug info `lines`, F016); n may itself be a symbol |
| `{"~leaves": "PROG0", "~count": n}` | the first n debug-map leaf paths under that instance |
| `{"~token": "valid\|none\|forged\|expired"}` | a JWT from the endpoint: its own, null, its own with the signature replaced, or an expired one (BLOCKED without the signing key) |
| `{"~token": "user:<name>"}` | the JWT of a named test user (runner `--user-token <name>=JWT`; BLOCKED without it): permission, lock and hand-over scenarios need several identities |

A symbol the build outputs cannot satisfy (no build directory, too few lines or leaves) makes the
scenario BLOCKED, not DIVERGE. `validate.py` refuses an unknown symbol and a `~build` variant
that is not a `variant-*.st` of the program.

### Repetition

Two forms, never inferred from neighbouring steps:

- **Batch:** `repeat: n` on a request sends it n times back to back, checking each response;
  on an event, it waits for n successive events. `[{"request": "next", "repeat": 3}, {"event":
  "stopped", "repeat": 3}]` sends three `next` before reading any stop, so a runtime that is not
  halted after the first answers `NOT_HALTED`: the scenario says so if that is what it means.
- **Alternating:** `{"loop": 3, "steps": [{"request": "next"}, {"event": "stopped"}]}` is
  next, stop, next, stop, next, stop. Run control is always written this way. A loop is one
  step in the trace; a failure inside it names the iteration and the body step.

### Inspect views

An `inspect` step reads the program's build outputs (`build_all.sh` writes `<OUT>/<name>/debug/`;
runner `--build-dir`) and matches this view; with no build outputs it is BLOCKED.

| `inspect` | View |
|---|---|
| `debugInfo` | `debug-info.json` as ADR-002 D3, except `pous` keyed by name; plus `linesByFile` (`lines` keyed by file path), `probeCount`, `instanceCount`, `generatedLines` (`[{file, line}]` of probes with flags bit 0) and `originOf` (`[{file, line, origin}]` with the `origins[].id`), both sorted by file then line |
| `debugMap` | `debug-map.json` plus `leafCount` |
| `generatedCpp` | `{"text": <generated_debug.cpp>}` |
| `objectCode` | none yet: BLOCKED until the harness has a disassembly adapter |

Keys no output carries yet (`loadMs`, `linkedPousMissing`) make the step BLOCKED rather than
DIVERGE on an absent key.

### Matchers

A plain JSON value matches by equality (arrays and objects recursively; an object's keys not
listed are ignored). Otherwise an object whose keys start with `~`:

| Matcher | Holds when |
|---|---|
| `~eq`, `~ne` | equal / not equal |
| `~lt`, `~le`, `~gt`, `~ge`, `~range: [a, b]` | numeric comparison (range inclusive) |
| `~approx: [v, tol]` | `|x − v| ≤ tol` (REAL, times) |
| `~oneOf: [...]` | equals one of |
| `~display: "T#1s200ms"` | the value's IEC display string (A090) |
| `~capture: "name"` | always; binds the value for later steps (combinable with other keys) |
| `~ref: "name"`, `~add: n`, `~tol: t` | equals the captured value (+ n), within ± t when `~tol` is given |
| `~notRef: "name"` | differs from the captured value (a build identity that changed) |
| `~sha256: "name"` | a string whose SHA-256 (UTF-8, hex) equals the captured value: a fetched source against the debug info's `files[].sha256` |
| `~contains: <match>` | an array with at least one element matching it, or a string containing it (an audit entry, a log line) |
| `~any`, `~absent` | present with any value / not present |
| `~len`, `~maxLen` | array or string length |

**Value encoding.** BOOL as JSON boolean; integers, REAL and bit strings as numbers; STRING
and WSTRING as JSON strings; enums by member name; TIME as integer milliseconds; DATE, DT
and TOD as integer milliseconds since their epoch. `~display` compares the IEC text instead.

`~ref` must follow its `~capture` in step order (validated). Captures are how an oracle says
"unchanged since the halt" or "k − 1" without knowing k in advance.

### Comparing a run

The harness walks the steps in order and reports, per step, `match` or `DIVERGE` with the
step index and the first differing path. A run's verdict is DIVERGE on the first failing
step; `BLOCKED` when a request is refused `UNSUPPORTED` before the feature exists (E13.4 c), or
when a seam the step needs (endpoint, plant, build outputs, injection) is not there.
Event steps consume events in arrival order; events the oracle does not mention are allowed,
except where a `noEvent` step forbids them.

## Coverage

A program header lists every criterion that cites the program; each oracle lists the ones it
discharges. That list is the claim an oracle earns, not a label on the file: an oracle that cites
a criterion is asserting that its steps cover that criterion's check, so cut a scenario down and
it stops citing the criterion. `ACCEPTANCE.md`'s `Test:` is what says the claim was paid for, and
`validate.py --acceptance` reads both and reports every way they disagree, in either direction.

A **diagnostic** scenario — `"kind": "diagnostic"`, another scenario with the broken steps removed,
to show that the rest still holds — is the only scenario allowed to cite none, and it says so
structurally rather than in prose: its `notes` must end with two lines,

```
Diagnostic of: <corpus>/<scenario>
Steps removed: <n>
```

and `validate.py` checks that `n` equals the difference in step count between the two files, so a
diagnostic cannot quietly stop being a bisect when the scenario it cuts grows. Its run writes one
`runs/*.jsonl` row with `"criterion": null`: a PASS from it is a bisect, visible in the ledger as
one and never evidence for a criterion. Criteria listed in a header but in no oracle still need a
scenario (E13.4: every A# gets a `Test:` naming one). `validate.py` reports nothing for those: the
gap is tracked in `docs/STATUS.md`, not here.

`tests/debug/harness/coverage.txt` maps each criterion to the scenarios that cite it. Its index
block is generated (`validate.py --write-coverage`) and checked; the sections around it — what a
scenario discharges only in part, and the MVP and core criteria no oracle can express — are
hand-written, because a generator cannot know what a scenario leaves out.
