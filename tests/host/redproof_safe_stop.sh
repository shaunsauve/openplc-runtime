#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# Red-proofs for test_safe_stop: injects each defect the acceptance criteria name into a
# scratch copy of the tree and requires the test to fail with the expected message.
# Linux only (run.sh's requirements). Usage: tests/host/redproof_safe_stop.sh

set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT=$(pwd)

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# name ^ file ^ text to find (\n = newline) ^ replacement ^ FAIL text expected
read -r -d '' MUTATIONS <<'EOF' || true
A206-1 skip the restore after fill_null_pointers^core/src/plc_app/plc_state_manager.cpp^        output_hold_restore();\n        image_tables_invalidate_output_snapshot();^        image_tables_invalidate_output_snapshot();^the first frame after the start did not carry the held values
A206-2 one flag for hold and gate^core/src/plc_app/plc_state_manager.cpp^        g_hold_outputs.store(false, std::memory_order_seq_cst);\n        if (plc_state == PLC_STATE_ERROR)^        if (plc_state == PLC_STATE_ERROR)^the plant's last frame after STOP is not all 0
A207-1 keep the STOP refusal from ERROR^core/src/plc_app/unix_socket.c^current_state == PLC_STATE_SAFE_STOPPED ||\n            (current_state == PLC_STATE_ERROR && plc_output_hold_is_set()))^current_state == PLC_STATE_SAFE_STOPPED)^STOP from ERROR with the hold answered STOP:ERROR
A207-2 skip the outputs-off after the STOPPED landing from ERROR^core/src/plc_app/plc_state_manager.cpp^        if (g_zero_after_stop.exchange(false, std::memory_order_acq_rel) &&\n            !plc_outputs_off_without_program())^        if (g_zero_after_stop.exchange(false, std::memory_order_acq_rel) && false)^outputs not 0 after the STOP from ERROR
A059-1 clear the gate in the explicit STOP claim^core/src/plc_app/plc_state_manager.cpp^        g_hold_outputs.store(false, std::memory_order_seq_cst);\n        if (plc_state == PLC_STATE_ERROR)^        g_hold_outputs.store(false, std::memory_order_seq_cst);\n        g_start_gate.store(false, std::memory_order_seq_cst);\n        if (plc_state == PLC_STATE_ERROR)^the explicit STOP cleared the start gate
A059-2 ignore a gate-only hold file at boot^core/src/plc_app/plc_state_manager.cpp^    if (kind == OUTPUT_HOLD_FILE_NONE) return false;^    if (kind != OUTPUT_HOLD_FILE_VALUES) return false;^a gate-only file did not set the gate
A049-1 apply the safe values under the hold policy^core/src/plc_app/plc_output_hold.cpp^        else if (std::strcmp(pol->valuestring, "hold") != 0)\n            break;^        else if (std::strcmp(pol->valuestring, "hold") != 0)\n            break;\n        p.kind = HALT_POLICY_SAFE;^while halted (want
A049-2 zero only the BOOL outputs at a halt^core/src/plc_app/plc_state_manager.cpp^    if (!wait_run_state(task_idx, PLC_TASK_RUN_HELD, false, timeout_ms)) return false;^    if (!wait_run_state(task_idx, PLC_TASK_RUN_HELD, false, timeout_ms)) return false;\n    image_lock();\n    for (int i = 0; i < BUFFER_SIZE; ++i)\n        for (int b = 0; b < 8; ++b)\n            if (bool_output[i][b]) *bool_output[i][b] = 0;\n    image_unlock();^while halted (want
A049-3 reset the outputs to their initial values at a halt^core/src/plc_app/plc_state_manager.cpp^    if (!wait_run_state(task_idx, PLC_TASK_RUN_HELD, false, timeout_ms)) return false;^    if (!wait_run_state(task_idx, PLC_TASK_RUN_HELD, false, timeout_ms)) return false;\n    image_lock();\n    image_tables_zero_outputs();\n    image_unlock();^while halted (want
A050 never re-arm the watchdog on resume^core/src/plc_app/plc_state_manager.cpp^            c->run_state.store(PLC_TASK_RUN_RUNNING, std::memory_order_release);\n        }\n    }\n}^            (void)0;\n        }\n    }\n}^the overrun after resume was never caught
EOF

failures=0
while IFS='^' read -r name file find repl expect; do
  [ -z "$name" ] && continue
  rm -rf "$WORK/tree"
  mkdir -p "$WORK/tree"
  (cd "$ROOT" && tar cf - --exclude=build --exclude=venvs --exclude=.git core tests/host) |
    (cd "$WORK/tree" && tar xf -)
  if ! python3 - "$WORK/tree/$file" "$find" "$repl" <<'PY'
import sys
path, find, repl = sys.argv[1], sys.argv[2].replace('\\n', '\n'), sys.argv[3].replace('\\n', '\n')
s = open(path).read()
if s.count(find) != 1:
    sys.exit("mutation target not found exactly once")
open(path, 'w').write(s.replace(find, repl))
PY
  then
    echo "  FAIL  $name: could not apply"
    failures=$((failures + 1))
    continue
  fi
  out=$(cd "$WORK/tree" && bash tests/host/run.sh </dev/null 2>&1 || true)
  if grep -q "test_safe_stop did not compile" <<<"$out"; then
    echo "  FAIL  $name: mutant did not compile"
    failures=$((failures + 1))
  elif grep -qF -- "$expect" <<<"$out"; then
    echo "  red   $name"
  else
    echo "  FAIL  $name: survived (expected \"$expect\")"
    grep "FAIL" <<<"$out" | head -5
    failures=$((failures + 1))
  fi
done <<<"$MUTATIONS"

if [ "$failures" -eq 0 ]; then
  echo "red-proofs: every injected defect was caught"
else
  echo "red-proofs: $failures not caught"
  exit 1
fi
