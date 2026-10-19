#!/usr/bin/env bash
# Inside the runtime image (Linux): build plc_main and the reference plant, compile the probed
# corpus payload, start both plus the real gateway, then run the DAP harness — which launches the
# DAP adapter as a subprocess and speaks DAP to it. Nothing in this script talks the session
# protocol on the harness's behalf; the adapter is the only client the runtime ever sees.
# Called by dap_e2e.sh; not for direct use.
# Mounts: /src (repo, read-only), /payload (gen_payload.mjs output), /out (logs), /runs (results).
# Env:     SEED HARNESS_COMMIT CORPUS RESULTS
set -uo pipefail
PYINC=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')
PY=/workdir/venvs/runtime/bin/python
CORPUS=${CORPUS:-tests/debug/corpus/nested-fb-loop}
NAME=$(basename "$CORPUS")

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

echo "### compile the probed corpus:$NAME through scripts/compile.sh"
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
echo "### the plant watches: $(tr '\n' ' ' </tmp/plant.conf)"
stdbuf -oL ./build/plc_main --print-logs >/out/plc_main.log 2>&1 &
PLC=$!
for _ in $(seq 1 300); do
  [ -S /tmp/plant.sock ] && [ -S /run/runtime/plc_debug.socket ] && break
  sleep 0.1
done
sleep 2

echo "### start the gateway (webserver/debug_gateway) on https://127.0.0.1:8443"
"$PY" tests/debug/wire/serve.py \
  127.0.0.1 8443 /tmp/token \
  /run/runtime/plc_debug.socket /work/core/generated/debug-info.json >/out/gateway.log 2>&1 &
GW=$!
for _ in $(seq 1 300); do [ -s /tmp/token ] && break; sleep 0.1; done
[ -s /tmp/token ] || { echo "GATEWAY FAIL"; tail -20 /out/gateway.log; exit 1; }
sleep 1

echo "### DAP harness: the adapter is launched as a subprocess and driven over DAP"
ARCH=$(uname -m)
# The plant's own inputs, per corpus: a scenario is only reproducible if what drives it is stated.
INPUTS=$(python3 - "$NAME" <<'PY'
import sys
# nested-fb-loop: motor2 runs only while %IX0.0 is set, and J1 reads motor1 alone.
# plant-io: the values j1-run-control's oracle drives. `addr=value` tokens, one per pair, because
# a bare "addr value" string cannot be split into pairs without knowing how many words each has.
print({"nested-fb-loop": "%IX0.0=0", "plant-io": "%IX0.0=1 %IW0=7 %ID0=70000"}
      .get(sys.argv[1], "%IX0.0=0"))
PY
)
for pair in $INPUTS; do
  ADDR=${pair%%=*}; VAL=${pair#*=}
  # The plant's SET is a line on its socket; the debug session does not exist yet and must not be
  # what puts these there, or the run would be measuring its own setup. Retried, because the
  # plugin takes its first exchange a moment after its socket appears.
  python3 - "$ADDR" "$VAL" <<'PY'
import socket, sys, time
addr, val = sys.argv[1], sys.argv[2]
last = "no reply"
for _ in range(200):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(5)
        s.connect("/tmp/plant.sock")
        s.sendall(("SET %s %s\n" % (addr, val)).encode())
        last = s.recv(64).decode().strip()
        s.close()
        if last.startswith("OK"):
            break
    except OSError as exc:
        last = str(exc)
    time.sleep(0.1)
else:
    raise SystemExit("the plant refused %s = %s (%s)" % (addr, val, last))
PY
done
echo "### plant inputs: $INPUTS"

"$PY" tests/debug/dap/dap_harness.py \
  --endpoint https://127.0.0.1:8443 --insecure --token "$(cat /tmp/token)" \
  --plant /tmp/plant.sock --program "$CORPUS/program.st" \
  ${SEED:+--seed "$SEED"} \
  ${RESULTS:+--results "$RESULTS"} \
  --ran-on "linux-$ARCH docker: plc_main + probed corpus:$NAME (STruCpp) + reference plant + gateway over WSS 127.0.0.1:8443, driven through tests/debug/dap/adapter.py${SEED:+ (DAP_SEED=$SEED)}" \
  ${ONLY:+--only $ONLY} "$@" 2>&1 | tee "/out/dap-harness-$NAME.txt"
RC=${PIPESTATUS[0]}
kill "$GW" 2>/dev/null
kill -INT "$PLC" 2>/dev/null
for _ in $(seq 1 100); do kill -0 "$PLC" 2>/dev/null || break; sleep 0.1; done
kill -9 "$PLC" 2>/dev/null
exit $RC