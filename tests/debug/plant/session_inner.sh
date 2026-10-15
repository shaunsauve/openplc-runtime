#!/usr/bin/env bash
# Inside the runtime image (Linux): build plc_main (with SEED's defect, if any) and the reference
# plant, compile the probed corpus payload (CORPUS, default plant-io), start plc_main with the plant and the real
# gateway, run the harness over WSS. Called by session_e2e.sh; not for direct use.
# Mounts: /src (repo, read-only), /payload (gen_payload.mjs output), /out (logs), /runs (results).
set -uo pipefail
PYINC=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')
PY=/workdir/venvs/runtime/bin/python
CORPUS=${CORPUS:-tests/debug/corpus/plant-io}
NAME=$(basename "$CORPUS")

echo "### build plc_main${SEED:+ with seeded defect '$SEED'} and the reference plant"
mkdir -p /work
(cd /src && tar cf - --exclude=build --exclude=venvs --exclude=.git --exclude=core/generated .) \
  | (cd /work && tar xf -)
cd /work || exit 1
case "$SEED" in
  "") ;;
  late)  # arm each breakpoint on the probe after its own: the stop is one statement late
    sed -i 's/debug_agent_set_breakpoint(\*probe, cond.data()/debug_agent_set_breakpoint(*probe + 1, cond.data()/' \
      core/src/plc_app/debug_link.cpp
    grep -q 'set_breakpoint(\*probe + 1' core/src/plc_app/debug_link.cpp || { echo "SEED late not applied"; exit 1; } ;;
  leafoff)  # read every leaf one past the asked one: the values read are another variable's
    sed -i 's/uint64_t idx = (uint64_t)item->valuedouble;/uint64_t idx = (uint64_t)item->valuedouble + 1;/' \
      core/src/plc_app/debug_link.cpp
    grep -q 'item->valuedouble + 1;' core/src/plc_app/debug_link.cpp || { echo "SEED leafoff not applied"; exit 1; } ;;
  stepin-over)  # A035 red-proof: step into behaves as step over
    sed -i 's/if (!std::strcmp(command, "next") \&\& !v.at_boundary)/if ((!std::strcmp(command, "next") || !std::strcmp(command, "stepIn")) \&\& !v.at_boundary)/' \
      core/src/plc_app/debug_link.cpp
    grep -q '|| !std::strcmp(command, "stepIn")) && !v.at_boundary' core/src/plc_app/debug_link.cpp \
      || { echo "SEED stepin-over not applied"; exit 1; } ;;
  twostops)  # A038 red-proof: a step landing on an armed breakpoint parks once as a step, then again as the breakpoint
    python3 - <<'PY' || { echo "SEED twostops not applied"; exit 1; }
p = "core/src/plc_app/debug_agent.cpp"
s = open(p).read()
anchor = "    timespec now;\n    clock_gettime(CLOCK_MONOTONIC, &now);\n    t->stop.reason"
assert anchor in s
extra = ("    if (bp_wants && trap_wants) {\n"
         "        t->stop.reason = DBG_STOP_STEP; t->stop.probe = id; t->stop.hits = 0;\n"
         "        t->stop.depth = tl_depth; t->stop.head = tl_head; t->stop.since_ns = 0;\n"
         "        t->stop_pending.store(true, std::memory_order_release);\n"
         "        if (g_host.park(tl_idx) == DBG_PARK_ABANDON) throw plc_dbg::Abandon{};\n"
         "    }\n")
open(p, "w").write(s.replace(anchor, extra + anchor, 1))
PY
    ;;
  twoscans)  # A036 red-proof: a cycle step runs two scans
    sed -i 's/plc_run_control_run_scans(task, 1, 2000)/plc_run_control_run_scans(task, 2, 2000)/' core/src/plc_app/debug_link.cpp
    grep -q 'run_scans(task, 2, 2000)' core/src/plc_app/debug_link.cpp || { echo "SEED twoscans not applied"; exit 1; } ;;
  skipout)  # A037 red-proof: a scan that was halted and resumed skips its copy-out
    python3 - <<'PY' || { echo "SEED skipout not applied"; exit 1; }
p = "core/src/plc_app/plc_state_manager.cpp"
s = open(p).read()
a1 = "            ctx->in_body = 1;\n            for (size_t p = 0; p < task->program_count; ++p)\n                task->programs[p]->run();"
a2 = "            for (size_t p = 0; p < task->program_count; ++p)\n            {\n                uint32_t off = 0, cnt = 0;\n                task->programs[p]->located_range(&off, &cnt);\n                if (cnt) image_tables_threaded_copy_out(off, cnt);"
assert a1 in s and a2 in s
s = s.replace(a1, "            const long seed_r0 = ctx->resumes.load();\n" + a1, 1)
s = s.replace(a2, "            if (ctx->resumes.load() == seed_r0)\n" + a2, 1)
open(p, "w").write(s)
PY
    ;;
  stopdetach)  # A033 red-proof: stop also ends the session
    python3 - <<'PY' || { echo "SEED stopdetach not applied"; exit 1; }
p = "core/src/plc_app/debug_link.cpp"
s = open(p).read()
a = "        /* Stop the program, not the session (F005): the session stays attached. */\n"
assert a in s
open(p, "w").write(s.replace(a, a + "        end_session_locked(session, running);\n", 1))
PY
    ;;
  *) echo "unknown SEED $SEED"; exit 1 ;;
esac
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
mkdir -p "/out/build/$NAME/debug" && cp /payload/debug-info.json "/out/build/$NAME/debug/"

python3 tests/lifecycle/logserver.py &
sleep 0.3
{
  echo "socket=/tmp/plant.sock"; echo "log=/out/plant.log"; echo "clock=virtual"
  sed -n '1,/\*)/p' "$CORPUS/program.st" | sed -n 's/^ *witness: *//p' | awk '{print "witness=" $1 " " NR-1 " " $3}'
  grep -o 'AT %Q[XBWDL]\?[0-9][0-9.]*' "$CORPUS/program.st" \
    | sed 's/^AT /output=/; s/^output=%Q\([0-9]\)/output=%QX\1/' | sort -u
} >/tmp/plant.conf
echo "reference-plant,./build/plugins/libreference_plant.so,1,1,/tmp/plant.conf," >plugins.conf
# START_HALTED (session_e2e.sh, from the oracles' setup.startHalted) boots the program halted at
# its first statement: the harness attaches to a program that has not run. Nothing else does
# this — a session that restarts start-halted and then detaches releases the halt.
./build/plc_main --print-logs ${START_HALTED:+--start-halted} >/out/plc_main.log 2>&1 &
PLC=$!
for _ in $(seq 1 300); do
  [ -S /tmp/plant.sock ] && [ -S /run/runtime/plc_debug.socket ] && break
  sleep 0.1
done
sleep 2

echo "### start the gateway (webserver/debug_gateway) on https://127.0.0.1:8443, linked to plc_main"
"$PY" tests/debug/wire/serve.py 127.0.0.1 8443 /tmp/token \
  /run/runtime/plc_debug.socket /work/core/generated/debug-info.json >/out/gateway.log 2>&1 &
GW=$!
for _ in $(seq 1 300); do [ -s /tmp/token ] && break; sleep 0.1; done
[ -s /tmp/token ] || { echo "GATEWAY FAIL"; tail -20 /out/gateway.log; exit 1; }
sleep 1

echo "### harness over WSS"
ARCH=$(uname -m)
"$PY" tests/debug/wire/run_harness.py --endpoint https://127.0.0.1:8443 --insecure \
  --token "$(cat /tmp/token)" --plant /tmp/plant.sock --build-dir /out/build \
  ${RESULTS:+--results "$RESULTS"} $(for t in $DEPLOYED; do printf -- '--deployed-task %s ' "$t"; done) \
  --ran-on "linux-$ARCH docker: plc_main + probed corpus:$NAME (STruCpp) + reference plant + gateway over WSS 127.0.0.1:8443${SEED:+ (SEED=$SEED)}" \
  "$@" | tee /out/harness.txt
RC=${PIPESTATUS[0]}
kill "$GW" 2>/dev/null
kill -INT "$PLC" 2>/dev/null
for _ in $(seq 1 100); do kill -0 "$PLC" 2>/dev/null || break; sleep 0.1; done
kill -9 "$PLC" 2>/dev/null
grep -E "ERROR|stuck|aborted" /out/plc_main.log | head -5
exit $RC
