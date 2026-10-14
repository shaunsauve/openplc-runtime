#!/usr/bin/env bash
# Inside the runtime image (Linux): build plc_main and the reference plant, compile the probed
# corpus:plant-io payload with scripts/compile.sh, start plc_main with the plant, run
# runctl_e2e.py. Called by runctl_e2e.sh; not for direct use.
# Mounts: /src (repo, read-only), /payload (gen_payload.mjs output), /out (logs).
set -uo pipefail
PYINC=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')
PY=/workdir/venvs/runtime/bin/python
[ -x "$PY" ] || PY=python3
CORPUS=tests/debug/corpus/plant-io

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

echo "### compile the probed corpus:plant-io through scripts/compile.sh"
cp -r /payload/. core/generated/
bash scripts/compile.sh >/out/compile.log 2>&1 || { echo "COMPILE FAIL"; tail -15 /out/compile.log; exit 1; }
[ -f build/new_libplc.so ] && mv build/new_libplc.so "build/libplc_$(date +%s%N).so"
SO=$(ls build/libplc_*.so) || { echo "COMPILE FAIL: no libplc"; exit 1; }
echo "  probes in the .so: $(nm -D "$SO" | grep -c 'strucpp_dbg_')" \
     "symbols; plc_main exports strucpp_dbg_hit: $(nm -D build/plc_main | grep -c ' T strucpp_dbg_hit')"

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
PID=$!
for _ in $(seq 1 300); do
  [ -S /tmp/plant.sock ] && [ -S /run/runtime/plc_debug.socket ] && break
  sleep 0.1
done
sleep 2

echo "### run control checks"
"$PY" tests/debug/plant/runctl_e2e.py /payload /tmp/plant.sock | tee /out/runctl.txt
RC=${PIPESTATUS[0]}
kill -INT "$PID" 2>/dev/null
for _ in $(seq 1 100); do kill -0 "$PID" 2>/dev/null || break; sleep 0.1; done
kill -9 "$PID" 2>/dev/null
grep -E "ERROR|stuck|aborted" /out/plc_main.log | head -5
exit $RC
