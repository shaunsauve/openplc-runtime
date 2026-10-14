#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# Host tests: plain C++17 executables, no framework, no device. For
# runtime code Ceedling cannot reach (uses std::thread/mutex) and that
# needs no plc_main. Add a test by appending it to TESTS below.

set -euo pipefail

cd "$(dirname "$0")/../.."

CXX="${CXX:-c++}"
CXXFLAGS="-std=c++17 -Wall -Wextra -Wno-unused-parameter -g"
INCLUDES="-Icore/src/plc_app -Icore/src"
# plugin_driver.h pulls in Python.h.
PY_INCLUDE=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])' 2>/dev/null || true)
if [ -n "$PY_INCLUDE" ]; then INCLUDES="$INCLUDES -I$PY_INCLUDE"; fi

OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT

# test source : extra sources it links
TESTS=(
  "tests/host/test_plc_retain_file_store.cpp:core/src/plc_app/plc_retain_file_store.cpp"
  "tests/host/test_rt_mutex.cpp:"
  "tests/host/test_task_policy.cpp:core/src/plc_app/task_policy.c"
  "tests/host/test_image_outputs.cpp:core/src/plc_app/image_tables.cpp:core/src/plc_app/located_globals.c"
  "tests/host/test_safe_stop.cpp:core/src/plc_app/plc_state_manager.cpp:core/src/plc_app/image_tables.cpp:core/src/plc_app/located_globals.c:core/src/plc_app/journal_buffer.c:core/src/plc_app/utils/utils.c:core/src/plc_app/scan_cycle_manager.c:core/src/plc_app/task_policy.c:core/src/plc_app/unix_socket.c:core/src/plc_app/plc_switch.c:core/src/plc_app/plc_output_hold.cpp:core/src/drivers/plugins/native/cjson/cJSON.c:core/src/plc_app/debug_agent.cpp:core/src/plc_app/debug_link.cpp"
  "tests/host/test_virtual_clock.cpp:core/src/plc_app/plc_state_manager.cpp:core/src/plc_app/image_tables.cpp:core/src/plc_app/located_globals.c:core/src/plc_app/journal_buffer.c:core/src/plc_app/utils/utils.c:core/src/plc_app/scan_cycle_manager.c:core/src/plc_app/task_policy.c:core/src/plc_app/unix_socket.c:core/src/plc_app/plc_switch.c:core/src/plc_app/plc_output_hold.cpp:core/src/drivers/plugins/native/cjson/cJSON.c:core/src/plc_app/debug_agent.cpp:core/src/plc_app/debug_link.cpp"
  "tests/host/test_breakpoint_table.cpp:core/src/plc_app/debug_agent.cpp"
  "tests/host/test_run_control.cpp:core/src/plc_app/plc_state_manager.cpp:core/src/plc_app/image_tables.cpp:core/src/plc_app/located_globals.c:core/src/plc_app/journal_buffer.c:core/src/plc_app/utils/utils.c:core/src/plc_app/scan_cycle_manager.c:core/src/plc_app/task_policy.c:core/src/plc_app/unix_socket.c:core/src/plc_app/plc_switch.c:core/src/plc_app/plc_output_hold.cpp:core/src/drivers/plugins/native/cjson/cJSON.c:core/src/plc_app/debug_agent.cpp:core/src/plc_app/debug_link.cpp"
)

# Also built with ThreadSanitizer, where the compiler supports it: any report fails the run
# (A205's race check on the breakpoint table).
TSAN_TESTS=(
  "tests/host/test_breakpoint_table.cpp:core/src/plc_app/debug_agent.cpp"
)

# ONLY=<substring>: build and run only the tests whose name contains it (red-proof scripts).
ONLY="${ONLY:-}"

failures=0
for entry in "${TESTS[@]}"; do
  test_src="${entry%%:*}"
  deps="${entry#*:}"
  name=$(basename "$test_src" .cpp)
  [[ -n "$ONLY" && "$name" != *"$ONLY"* ]] && continue

  printf '\n=== %s ===\n' "$name"
  # C sources are compiled as C, not C++.
  objs=()
  for dep in ${deps//:/ }; do
    if [[ "$dep" == *.c ]]; then
      obj="$OUT/$(basename "$dep" .c).o"
      # shellcheck disable=SC2086
      ${CC:-cc} -std=gnu11 -Wall -Wextra -g $INCLUDES -c "$dep" -o "$obj" || { objs=(); break; }
      objs+=("$obj")
    else
      objs+=("$dep")
    fi
  done
  # shellcheck disable=SC2086
  if ! $CXX $CXXFLAGS $INCLUDES "$test_src" ${objs[@]+"${objs[@]}"} -o "$OUT/$name" -lpthread -lm; then
    echo "  FAIL  $name did not compile"
    failures=$((failures + 1))
    continue
  fi

  if ! "$OUT/$name"; then
    failures=$((failures + 1))
  fi
done

if echo 'int main(){}' | $CXX -x c++ -fsanitize=thread - -o "$OUT/tsan_probe" 2>/dev/null &&
   "$OUT/tsan_probe" 2>/dev/null; then
  for entry in "${TSAN_TESTS[@]}"; do
    test_src="${entry%%:*}"
    deps="${entry#*:}"
    name="$(basename "$test_src" .cpp)_tsan"
    [[ -n "$ONLY" && "$name" != *"$ONLY"* ]] && continue
    printf '\n=== %s ===\n' "$name"
    # shellcheck disable=SC2086
    if ! $CXX $CXXFLAGS -O1 -fsanitize=thread $INCLUDES "$test_src" ${deps//:/ } -o "$OUT/$name" -lpthread; then
      echo "  FAIL  $name did not compile"
      failures=$((failures + 1))
      continue
    fi
    if ! TSAN_OPTIONS="halt_on_error=1 exitcode=66" "$OUT/$name"; then
      failures=$((failures + 1))
    fi
  done
else
  printf '\n=== ThreadSanitizer unavailable (needs Linux; in Docker, --security-opt seccomp=unconfined): TSan builds SKIPPED ===\n'
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
  echo "host tests: all passed"
else
  echo "host tests: $failures failed"
  exit 1
fi
