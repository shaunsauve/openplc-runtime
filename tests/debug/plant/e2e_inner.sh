#!/usr/bin/env bash
# Inside the runtime image (Linux): build plc_main and the reference plant, compile the corpus
# payload with the runtime's own compile.sh, then for each plant clock start plc_main with the
# plant and run the corpus's oracles with no debugger. Called by e2e.sh; not for direct use.
# Mounts: /src (repo, read-only), /payload (gen_payload.mjs output), /out (logs and results).
# Env: CORPUS, MODES (plant clocks, default "virtual wall").
set -uo pipefail
CORPUS=${CORPUS:?}
MODES=${MODES:-virtual wall}
PYINC=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')
PY=/workdir/venvs/runtime/bin/python
[ -x "$PY" ] || PY=python3

echo "### build plc_main and the reference plant"
mkdir -p /work
(cd /src && tar cf - --exclude=build --exclude=venvs --exclude=.git --exclude=core/generated .) \
  | (cd /work && tar xf -)
cd /work || exit 1
mkdir -p build/plugins /run/runtime core/generated
(cd build && cmake .. >/out/cmake.log 2>&1 && make -j"$(nproc)" plc_main >/out/make.log 2>&1) \
  || { echo "BUILD FAIL"; tail -20 /out/make.log; exit 1; }
gcc -shared -fPIC -O1 -Wall -Wextra -o build/plugins/libreference_plant.so \
  tests/debug/plant/reference_plant.c -I core/src/drivers -I core/src -I "$PYINC" -lpthread -ldl \
  || { echo "PLANT BUILD FAIL"; exit 1; }
echo "  plc_main sha256 $(sha256sum build/plc_main | cut -c1-16)"

echo "### compile corpus:$CORPUS through scripts/compile.sh"
cp -r /payload/. core/generated/
bash scripts/compile.sh >/out/compile.log 2>&1 \
  || { echo "COMPILE FAIL"; tail -15 /out/compile.log; exit 1; }
[ -f build/new_libplc.so ] && mv build/new_libplc.so "build/libplc_$(date +%s%N).so"
ls build/libplc_*.so >/dev/null || { echo "COMPILE FAIL: no libplc"; exit 1; }

python3 tests/lifecycle/logserver.py &
sleep 0.3

# One `witness:` header line per task, in task order: <task> <instance.var> <%Q address>.
witness_conf() {
  sed -n '1,/\*)/p' "tests/debug/corpus/$CORPUS/program.st" | sed -n 's/^ *witness: *//p' \
    | awk '{print "witness=" $1 " " NR-1 " " $3}'
}

# Every located output the program declares (AT %Q...): what the plant observes.
output_conf() {
  grep -o 'AT %Q[XBWDL]\?[0-9][0-9.]*' "tests/debug/corpus/$CORPUS/program.st" \
    | sed 's/^AT /output=/; s/^output=%Q\([0-9]\)/output=%QX\1/' | sort -u
}

core() {  # core <command>: one line on the runtime's command socket
  "$PY" - "$1" <<'PYEOF'
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(10)
try:
    s.connect("/run/runtime/plc_runtime.socket"); s.sendall(sys.argv[1].encode() + b"\n")
    print(s.recv(4096).decode().strip())
except OSError as e:
    print(f"unreachable ({e})")
PYEOF
}

start_runtime() {  # start_runtime <clock>
  printf 'socket=/tmp/plant.sock\nlog=/out/plant-%s.log\nclock=%s\n%s\n%s\n' "$1" "$1" \
    "$(witness_conf)" "$(output_conf)" >/tmp/plant.conf
  echo "reference-plant,./build/plugins/libreference_plant.so,1,1,/tmp/plant.conf," >plugins.conf
  : >"/out/plant-$1.log"
  ./build/plc_main --print-logs >"/out/plc_main-$1.log" 2>&1 &
  PID=$!
  for _ in $(seq 1 300); do
    [ "$(core STATUS)" = "STATUS:RUNNING" ] && [ -S /tmp/plant.sock ] && break
    sleep 0.1
  done
  echo "  plc_main pid $PID ($(core STATUS)), plant clock $1"
}

stop_runtime() {
  kill -INT "$PID" 2>/dev/null
  for _ in $(seq 1 100); do kill -0 "$PID" 2>/dev/null || break; sleep 0.1; done
  kill -9 "$PID" 2>/dev/null
  wait "$PID" 2>/dev/null
}

# run_oracle <clock> <tag> <oracle>: one runner invocation; its plant-log slice is /out/<tag>.slice
run_oracle() {
  local log="/out/plant-$1.log" a b
  a=$(wc -l <"$log")
  "$PY" tests/debug/harness/runner.py --plant /tmp/plant.sock --no-debugger --trace "$3" \
    | tee -a "/out/runner-$1.txt"
  b=$(wc -l <"$log")
  sed -n "$((a + 1)),${b}p" "$log" >"/out/$2.slice"
}

HALT=tests/debug/corpus/$CORPUS/plant-schedule-halt.oracle.json
SHORT=tests/debug/corpus/$CORPUS/plant-schedule-halt-short.oracle.json
if [ -f "$HALT" ]; then
  # A152: the same scenario with a 0.3 s halt; indexed by virtual time, both logs must match.
  sed 's/"wallMs": 2000/"wallMs": 300/; s#plant-schedule-halt"#plant-schedule-halt-short"#' \
    "$HALT" >"$SHORT"
fi

for clock in $MODES; do
  echo "### plant clock: $clock"
  : >"/out/runner-$clock.txt"
  start_runtime "$clock"
  for o in tests/debug/corpus/"$CORPUS"/*.oracle.json; do
    tag="$clock-$(basename "$o" .oracle.json)"
    run_oracle "$clock" "$tag" "$o" | grep -E '^(PASS|DIVERGE|BLOCKED) '
  done
  stop_runtime
  if [ -f "$SHORT" ]; then
    printf '  A152 halt 2 s vs 0.3 s: '
    python3 tests/debug/plant/compare_logs.py "/out/$clock-plant-schedule-halt.slice" \
      "/out/$clock-plant-schedule-halt-short.slice"
  fi
done
