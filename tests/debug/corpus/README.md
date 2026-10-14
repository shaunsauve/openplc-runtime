# Debug verification corpus

ST programs that the source-level debugger is measured against, each with one or more
**oracles**: machine-checkable expected traces. Every acceptance criterion (`A###` in the
orchestration root's `docs/ACCEPTANCE.md`) names the `corpus:<name>` it runs against; the
directory of that name is here.

```
tests/debug/corpus/
  README.md               this file: the program contract and the oracle format
  oracle.schema.json      JSON Schema 2020-12 for an oracle (format 1.0)
  validate.py             validates every header and oracle (Python stdlib only)
  test_validate.py        red/green proof for validate.py
  build_all.sh            compiles every program with STruCpp, then runs validate.py
  <name>/program.st       the program (main source; more .st files allowed)
  <name>/<id>.oracle.json one scenario each
  bench/family.txt        corpus:bench is a family: its members are bench_hot, bench_call, bench_mix
```

Run `python3 tests/debug/corpus/validate.py` (add `--acceptance <root>/docs/ACCEPTANCE.md
--catalog <root>/docs/drafts/feature-catalog.md` to cross-check criteria and FC tags), and
`python3 -m unittest tests/debug/corpus/test_validate.py`.

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

## Oracle format (1.0)

One JSON file per scenario, `<name>/<id>.oracle.json`, `scenario` = `<name>/<id>`.

```jsonc
{
  "format": "openplc-debug-oracle", "major": 1, "minor": 0,
  "scenario": "plant-io/bp-before-statement", "corpus": "plant-io",
  "kind": "runtime",                 // "build": a debug-info / generated-code inspection, no runtime
  "criteria": ["A034", "A031"],      // the criteria this scenario discharges (⊆ the header's)
  "title": "...",
  "setup": {                          // all optional
    "build": {"probes": true, "variant": "variant-inplace.st", "path": "library|cli|editor", "control": true},
    "haltPolicy": "hold|safe", "safeValues": {"%QW0": 5}, "haltMode": "allStop|nonStop",
    "startHalted": false, "session": "none|controller|observer",
    "tasks": {"Main": {"intervalMs": 500}}, "params": {"P2_s": 15},
    "plant": {"samplePeriodMs": 10, "inputs": {"%IX0.0": true}}
  },
  "steps": [ ... ]                    // executed in order
}
```

A **major** bump is incompatible; a **minor** bump only adds optional fields (as ADR-002 D2).

### Steps

Each step has exactly one action key, plus optional `note`.

| Step | Meaning |
|---|---|
| `{"request": <command>, "args": {…}, "expect": {"success": b, "error": <code>, "body": <match>}, "repeat": n}` | Send an ADR-003 D4 request (`initialize`, `setBreakpoints`, `continue`, `next`, `cycleStep`, `force`, …). `error` is an ADR-003 D2 code. Lines in `args` may be markers |
| `{"event": <name>, "withinMs": t, "expect": {…}, "count": <match>, "repeat": n}` | The next event of that name (ADR-003 D4: `stopped`, `continued`, `runState`, `output`, `values`, `forces`, `time`, `sessionEnded`, …) arrives within `t` and matches |
| `{"noEvent": <name>, "forMs": t \| "forScans": n, "task": …}` | No such event in the window (scans counted by the plant) |
| `{"plant": {"set": {addr: v}, "sequence": [{…}, …], "failExchanges": n}}` | Drive plant inputs: one write, or successive writes (latest-wins tests, A055) |
| `{"wait": {"wallMs": t} \| {"vtMs": t} \| {"scans": n}}` | Wait on the harness wall clock, on PLC virtual time, or for n plant-counted scans |
| `{"observe": "plant\|status\|session\|log", "expect": {…}, "throughout": b}` | Read back through an independent path. `throughout: true` means the expectation held for every sample since the previous step |
| `{"inspect": "debugInfo\|debugMap\|generatedCpp\|objectCode", "expect": <match>}` | Build scenarios: read compiler outputs (ADR-002 D1 files) |
| `{"inject": "killClient\|dropNetwork\|restoreNetwork\|sigstopRuntime\|sigcontRuntime\|killRuntime\|restartRuntime\|upload", "args": {…}}` | Fault injection (F154) |

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
| `fields` | status / log | other fields, e.g. `{"reason": "heartbeat_timeout"}`, `{"lastFault": {…}}` |

**ADR-004 arithmetic the oracles rely on.** Halted mid-scan k: the session reads the witness
as k; the plant holds k − 1 until the scan completes (D1.1). A cycle step is exactly +1 scan
and +1 interval of virtual time; statement steps add 0 (D4). A halt adds at most one interval
of virtual time (D4.2).

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
| `~capture: "name"` | always; binds the value for later steps (combinable with one other key) |
| `~ref: "name"`, `~add: n` | equals the captured value (+ n) |
| `~any`, `~absent` | present with any value / not present |
| `~len`, `~maxLen` | array or string length |

`~ref` must follow its `~capture` in step order (validated). Captures are how an oracle says
"unchanged since the halt" or "k − 1" without knowing k in advance.

### Comparing a run

The harness walks the steps in order and reports, per step, `match` or `DIVERGE` with the
step index and the first differing path. A run's verdict is DIVERGE on the first failing
step; `BLOCKED` when a request is refused `UNSUPPORTED` before the feature exists (E13.4 c).
Event steps consume events in arrival order; events the oracle does not mention are allowed,
except where a `noEvent` step forbids them.

## Coverage

A program header lists every criterion that cites the program; each oracle lists the ones it
discharges. Criteria listed in a header but in no oracle still need a scenario (E13.4: every
A# gets a `Test:` naming one). `validate.py` reports nothing for those: the gap is tracked in
`docs/STATUS.md`, not here.
