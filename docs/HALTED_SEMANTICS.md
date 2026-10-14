# Behaviour while halted and after a safe-stop

What the runtime does to the plant while the debugger holds a task, and after a safe-stop.
Each statement names the scenario that checks it. A statement whose scenario is `gap` is
specified but not yet built or not yet checked.

Scenario ids: `host:<case>` is a case in `tests/host/test_safe_stop.cpp` (run by
`tests/host/run.sh`); `corpus:<dir>/<name>` is an oracle under `tests/debug/corpus/`.

## Outputs

| # | Statement | Scenario |
|---|---|---|
| O1 | While any task is halted, the output image keeps each task's last complete scan. The halted scan's own writes are published only when it completes. | `host:A049 hold`; `corpus:plant-io/halt-holds-outputs` |
| O2 | The halt policy is per project, in `conf/halt_policy.json` of `program.zip`: `{"policy": "hold" \| "safe", "safe": [{"location": "%QX0.1", "value": 0}]}`. A missing or invalid file means `hold`. It is loaded at every start. | `host:A049 safe` |
| O3 | Under `safe`, entering a halt saves the image value of each configured output in the halted scope and writes its safe value through the journal; resuming writes the saved value back before the task is released. No safe value outlives the halt. | `host:A049 safe` |
| O4 | Under a halt of one task, only configured outputs inside that task's located ranges change. Configuration-scope located globals change only when every task is halted. | gap |
| O5 | A forced output keeps its forced value under either policy (the journal drops writes to forced slots). | gap |
| O6 | The policy can be read back (`halt_policy_get`), including the configured outputs under `hold`. | `host:A049 hold`, `host:A049 safe` |

## Watchdog

| # | Statement | Scenario |
|---|---|---|
| W1 | A held task is not released, and its overruns, stuck ticks and first-scan time are not counted. The dispatcher keeps ticking and feeding the watchdog. There is no maximum halt duration. | `host:A050`; `corpus:watchdog-overrun/long-halt-then-overrun` |
| W2 | On resume the stuck count is 0 and the release time moves forward by the halt, so the stuck limit measures execution time only. An overrun in the first scan after resume still trips. | `host:A050` |

## Virtual time and scan statistics

| # | Statement | Scenario |
|---|---|---|
| T1 | Each task has its own virtual clock. It advances one interval on every due tick where the task is running, released or skipped as an overrun, and does not move while the task is held. With no halts it equals the wall schedule. | `host:D4.1 no halt`, `host:R4xR8 halt-one` |
| T2 | `TIME()` and the timer FBs (TON, TOF, TP) read the clock through the stamp of their scan, so a halt of any length advances what they read by at most one interval, and a halted TON resumes at the elapsed time it had. | `host:A052 all-stop 1 s`, `host:A052 all-stop 3 s`, `host:A053 TON`; `corpus:ton-timer/halt-mid-timing` |
| T3 | Holding one task stops only its clock; the other tasks' clocks keep the wall schedule. Holding every task stops every clock. | `host:R4xR8 halt-one`, `host:A052 all-stop 1 s` |
| T4 | Plugins read the clocks through `get_task_virtual_time_ns(task)` and `get_plc_virtual_time_ns()` (the fastest task); both agree with the program within one interval. Tasks are still released on the wall schedule. `CURRENT_DT` stays wall time. | `host:D4.1 no halt`, `host:A052 all-stop 1 s` |
| T5 | The first scan after a resume records no cycle-time or latency sample, and its expected start is re-anchored, so a halt never enters STATS and never counts as an overrun. Resume releases at the next due tick with no catch-up burst. | `host:A051 statistics`; `corpus:bench/halt-no-catch-up` |

## Safe-stop

| # | Statement | Scenario |
|---|---|---|
| S1 | A safe-stop abandons the scan in progress, holds every output at the last complete scan's values (or the configured safe values under `safe`), and reports `STATUS:SAFE_STOPPED`. A STOP already in flight wins over a safe-stop. | `host:A206 hold`; `corpus:plant-io/safe-stop-outputs-held` |
| S2 | In SAFE_STOPPED no program code runs. The dispatcher keeps running without tasks: it feeds the watchdog and fires `cycle_start`/`cycle_end` in pairs at the fastest task's period, so plugins keep exchanging the held image. | `host:A206 hold` |
| S3 | Plugins see SAFE_STOPPED as 0 (STOPPED) from `get_plc_state`. | gap |
| S4 | The runtime never writes 0 to an output on any path into, through or out of SAFE_STOPPED, except an explicit STOP. A crash on the dispatcher in SAFE_STOPPED lands ERROR with the outputs still held. | `host:A206 hold`, `host:A207 crash` |
| S5 | Every start is refused while the start gate is set unless it carries a grant: socket `START` answers `START:ERROR_SAFE_STOPPED`; plugin, VPP switch, switch reconciliation and boot auto-start are refused and logged. | `host:A059 SAFE_STOPPED`, `host:A059 stop-then-start`, `host:A059 boot gate-only` |
| S6 | `START_AUTHORISED:<user>` carries a one-shot grant. It starts the program from SAFE_STOPPED, from STOPPED with the gate set, and from ERROR with the hold set. The plant sees the held values until scan 1's copy-out, then the program's own values, never 0. | `host:A206 granted start`, `host:A208 error`, `host:A206 boot granted start` |
| S7 | The hold and the gate are cleared together at the first quiescent window in which every task of a granted run has completed a scan; `safe_stop.hold` is deleted then. | `host:A206 granted start` |
| S8 | An explicit STOP (socket, plugin, switch, upload) from SAFE_STOPPED, or from ERROR while the hold is set, answers `STOP:OK`, drives every output to 0 and lands STOPPED. It clears the hold but keeps the gate, and `safe_stop.hold` is rewritten as gate-only. | `host:A206 stop`, `host:A207 stop` |
| S9 | `safe_stop.hold` (`/var/lib/openplc-runtime/`) survives a process restart. With values, the runtime boots into programless SAFE_STOPPED: plugins started with the held values, no auto-start, no boot outputs-off, and the record says the runtime restarted. Gate-only, it boots as usual with every start gated. | `host:A206 boot values`, `host:A059 boot gate-only` |
| S10 | An upload stops first from RUNNING, SAFE_STOPPED and ERROR, and does not clear the gate. | `pytest:tests/pytest/runtime/test_ensure_plc_stopped.py` |

## Known limits

- While the runtime process is dead the plant receives no frames. A death in the few ms
  between the safe-stop and the hold file reaching disk boots with no hold file.
- A start from SAFE_STOPPED reloads the program, which stops every plugin. What a field
  device does while its plugin is stopped is plugin-defined (EtherCAT stops the bus).

## Not yet specified here

Input buffering, halt-one shared data, the halt offset (ADR-004 D4.4) and the debugger's
probes arrive with run control; their statements are added with it. A halt inside a scan needs
probes, so T5 covers halts at the scan boundary only.
