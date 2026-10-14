#!/usr/bin/env bash
# Inside the runtime image (Linux): build plc_main and the reference plant, compile the probed
# corpus:plant-io payload, start plc_main with the plant and the real gateway (P1 1 s, P2 5 s),
# run sec_e2e.py. Called by sec_e2e.sh; not for direct use.
# Mounts: /src (repo, read-only), /payload (gen_payload.mjs output), /out (logs).
set -uo pipefail
PYINC=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')
PY=/workdir/venvs/runtime/bin/python
CORPUS=tests/debug/corpus/plant-io
P2=5

echo "### build plc_main and the reference plant"
mkdir -p /work
(cd /src && tar cf - --exclude=build --exclude=venvs --exclude=.git --exclude=core/generated .) \
  | (cd /work && tar xf -)
cd /work || exit 1
mkdir -p build/plugins /run/runtime core/generated /var/lib/openplc-runtime
(cd build && cmake .. >/out/cmake.log 2>&1 && make -j"$(nproc)" plc_main >/out/make.log 2>&1) \
  || { echo "BUILD FAIL"; tail -20 /out/make.log; exit 1; }
gcc -shared -fPIC -O1 -Wall -Wextra -o build/plugins/libreference_plant.so \
  tests/debug/plant/reference_plant.c -I core/src/drivers -I core/src -I "$PYINC" -lpthread -ldl \
  || { echo "PLANT BUILD FAIL"; exit 1; }

echo "### compile the probed corpus:plant-io through scripts/compile.sh"
cp -r /payload/. core/generated/
bash scripts/compile.sh >/out/compile.log 2>&1 || { echo "COMPILE FAIL"; tail -15 /out/compile.log; exit 1; }
[ -f build/new_libplc.so ] && mv build/new_libplc.so "build/libplc_$(date +%s%N).so"
ls build/libplc_*.so >/dev/null || { echo "COMPILE FAIL: no libplc"; exit 1; }
cp /payload/debug-info.json core/generated/debug-info.json

python3 tests/lifecycle/logserver.py &
sleep 0.3
{
  echo "socket=/tmp/plant.sock"; echo "log=/out/plant.log"; echo "clock=virtual"
  sed -n '1,/\*)/p' "$CORPUS/program.st" | sed -n 's/^ *witness: *//p' | awk '{print "witness=" $1 " " NR-1 " " $3}'
  grep -o 'AT %Q[XBWDL]\?[0-9][0-9.]*' "$CORPUS/program.st" \
    | sed 's/^AT /output=/; s/^output=%Q\([0-9]\)/output=%QX\1/' | sort -u
} >/tmp/plant.conf
echo "reference-plant,./build/plugins/libreference_plant.so,1,1,/tmp/plant.conf," >plugins.conf
./build/plc_main --print-logs >/out/plc_main.log 2>&1 &
PLC=$!
for _ in $(seq 1 300); do
  [ -S /tmp/plant.sock ] && [ -S /run/runtime/plc_debug.socket ] && break
  sleep 0.1
done
sleep 2

echo "### start the gateway on https://127.0.0.1:8443 with P1 = 1 s, P2 = $P2 s (debug.conf)"
export OPENPLC_PERSISTENT_DATA_DIR=/tmp/gw-data
mkdir -p "$OPENPLC_PERSISTENT_DATA_DIR"
printf 'P1 = 1\nP2 = %s\n' "$P2" >"$OPENPLC_PERSISTENT_DATA_DIR/debug.conf"
"$PY" tests/debug/wire/serve.py 127.0.0.1 8443 /tmp/token \
  /run/runtime/plc_debug.socket /work/core/generated/debug-info.json >/out/gateway.log 2>&1 &
GW=$!
for _ in $(seq 1 300); do [ -s /tmp/token ] && break; sleep 0.1; done
[ -s /tmp/token ] || { echo "GATEWAY FAIL"; tail -20 /out/gateway.log; exit 1; }
sleep 1

echo "### session security, client loss and wire checks"
"$PY" tests/debug/plant/sec_e2e.py https://127.0.0.1:8443 /tmp/token /tmp/plant.sock "$P2" \
  /out/plc_main.log | tee /out/sec.txt
RC=${PIPESTATUS[0]}
kill "$GW" 2>/dev/null
kill -INT "$PLC" 2>/dev/null
for _ in $(seq 1 100); do kill -0 "$PLC" 2>/dev/null || break; sleep 0.1; done
kill -9 "$PLC" 2>/dev/null
# plc_main's stdout is block-buffered into the log, so the reason is read once it has exited.
if grep -q "SAFE_STOPPED (heartbeat_timeout): outputs held" /out/plc_main.log; then
  echo "PASS A057: plc_main records SAFE_STOPPED with reason heartbeat_timeout" | tee -a /out/sec.txt
else
  echo "FAIL A057: no SAFE_STOPPED (heartbeat_timeout) in plc_main's log" | tee -a /out/sec.txt
  RC=1
fi
grep -E "ERROR|stuck|aborted" /out/plc_main.log | head -5
exit $RC
