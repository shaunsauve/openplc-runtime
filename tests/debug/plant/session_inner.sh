#!/usr/bin/env bash
# Inside the runtime image (Linux): build plc_main (with SEED's defect, if any) and the reference
# plant, compile the probed corpus payload (CORPUS, default plant-io), start plc_main with the plant and the real
# gateway, run the harness over WSS. Called by session_e2e.sh; not for direct use.
# Mounts: /src (repo, read-only), /payload (gen_payload.mjs output, with conf/halt_policy.json when
#         an oracle states setup.haltPolicy), /out (logs), /runs (results).
# Env:     SEED CORPUS DEPLOYED START_HALTED PARAMS PROBES HALT_POLICY HALT_MODE RESULTS HARNESS_COMMIT
#          — what the driver deployed, each from the oracles' own setup. PROBES says whether the
#            payload was built with probes (the payload itself is the authority: this script reads
#            debug-info.json back rather than believing the flag). USERS are the account names the
#            oracles asked for and COLLAPSE_USER the one of them served as the endpoint's own
#            identity (the red-proof for a second identity).
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
  forcecell)  # A109 red-proof: force only the variable cell, not the I/O image
    sed -i 's/        journal_force_set(jt, byte_index, bit_index, val);/        (void)val; \/* seeded: the image pin is gone, so copy_out owns the slot *\//' \
      core/src/plc_app/debug_write_journal.cpp
    grep -q 'seeded: the image pin is gone' core/src/plc_app/debug_write_journal.cpp \
      || { echo "SEED forcecell not applied"; exit 1; } ;;
  forceowner)  # A116 red-proof: a force is registered with no owner at all
    python3 - <<'PY' || { echo "SEED forceowner not applied"; exit 1; }
p = "webserver/debug_gateway/gateway.py"
s = open(p).read()
a = '                "owner": session.user,\n'
assert a in s
open(p, "w").write(s.replace(a, '                "owner": "",\n', 1))
PY
    ;;
  forcettl)  # A116 red-proof: a TTL is recorded but never expires
    sed -i 's/            if (f.expires_ms != 0 \&\& f.expires_ms <= now_ms) gone.push_back(f.leaf);/            if (false \&\& f.expires_ms != 0 \&\& f.expires_ms <= now_ms) gone.push_back(f.leaf);/' \
      core/src/plc_app/debug_force.cpp
    grep -q 'if (false && f.expires_ms' core/src/plc_app/debug_force.cpp \
      || { echo "SEED forcettl not applied"; exit 1; } ;;
  forceallowner)  # A188 red-proof: cancel-all clears only the caller's own forces
    python3 - <<'PY' || { echo "SEED forceallowner not applied"; exit 1; }
p = "core/src/plc_app/debug_link.cpp"
s = open(p).read()
a = """    static dbgforce_t buf[256];
    for (int at = 0; at < dbgforce_count();)
    {
        const int n = dbgforce_copy(&buf[0], (int)(sizeof buf / sizeof buf[0]));
        if (n <= 0) break;
        for (int i = 0; i < n; ++i)
        {
            uint8_t  arr = 0;
            uint16_t elem = 0;
            if (dbgforce_arr_elem_of(buf[i].leaf, &arr, &elem) == 0)
                runtime_external_write(arr, elem, DBGW_OP_UNFORCE, nullptr, 0);
        }
        at += n;
    }
    const int removed = dbgforce_clear_all();
"""
assert a in s, "cancel-all body not found"
b = """    static dbgforce_t buf[256];
    const std::string me = g_sessions.count(session) ? g_sessions[session].user : std::string();
    for (int at = 0; at < dbgforce_count();)
    {
        const int n = dbgforce_copy(&buf[0], (int)(sizeof buf / sizeof buf[0]));
        if (n <= 0) break;
        for (int i = 0; i < n; ++i)
        {
            if (me != buf[i].owner) continue;
            uint8_t  arr = 0;
            uint16_t elem = 0;
            if (dbgforce_arr_elem_of(buf[i].leaf, &arr, &elem) == 0)
                runtime_external_write(arr, elem, DBGW_OP_UNFORCE, nullptr, 0);
        }
        at += n;
    }
    int removed = 0;
    for (int at = 0; at < dbgforce_count();)
    {
        static dbgforce_t mine[256];
        const int n = dbgforce_copy(&mine[0], (int)(sizeof mine / sizeof mine[0]));
        if (n <= 0) break;
        for (int i = 0; i < n; ++i)
            if (me == mine[i].owner) removed += dbgforce_clear(mine[i].leaf);
        at += n;
        if (removed > 0) break;
    }
"""
open(p, "w").write(s.replace(a, b, 1))
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
# stdbuf -oL: --print-logs writes to a file, which libc block-buffers, so the halt policy the core
# reports (T-VER-HLTDEPLOY, read back below) would still be in the buffer when the harness starts.
stdbuf -oL ./build/plc_main --print-logs ${START_HALTED:+--start-halted} >/out/plc_main.log 2>&1 &
PLC=$!
for _ in $(seq 1 300); do
  [ -S /tmp/plant.sock ] && [ -S /run/runtime/plc_debug.socket ] && break
  sleep 0.1
done
sleep 2

echo "### start the gateway (webserver/debug_gateway) on https://127.0.0.1:8443, linked to plc_main"
# PARAMS (session_e2e.sh, from the oracles' setup.params) deploys the gateway's P1/P2; absent, the
# gateway reads <data dir>/debug.conf as the runtime does, which is the run before any.
OPENPLC_DEBUG_PARAMS="$PARAMS" OPENPLC_DEBUG_USERS="${USERS:-}" "$PY" tests/debug/wire/serve.py \
  127.0.0.1 8443 /tmp/token \
  /run/runtime/plc_debug.socket /work/core/generated/debug-info.json >/out/gateway.log 2>&1 &
GW=$!
for _ in $(seq 1 300); do [ -s /tmp/token ] && break; sleep 0.1; done
[ -s /tmp/token ] || { echo "GATEWAY FAIL"; tail -20 /out/gateway.log; exit 1; }
# USERS: the runtime's own logins for the names the oracles asked for (serve.py). Each is a real
# account; the harness cannot mint a JWT, so this file is the only way a second identity reaches a
# scenario. A run that names none leaves the file absent and the harness with one identity.
USER_ARGS=()
if [ -n "${USERS:-}" ]; then
  for _ in $(seq 1 300); do [ -s /tmp/token.users ] && break; sleep 0.1; done
  [ -s /tmp/token.users ] || { echo "USERS FAIL: no login for $(echo "$USERS" | tr ',' ' ')"; \
    tail -20 /out/gateway.log; exit 1; }
  while IFS='=' read -r name jwt; do
    [ -n "$name" ] || continue
    # COLLAPSE_USER: the red-proof for a second identity — this name resolves to the endpoint's own
    # token, so a scenario that needs two identities gets one identity wearing two names. What the
    # runtime then answers (a force's owner, a `control` body's controller) is the measurement.
    [ "$name" = "${COLLAPSE_USER:-}" ] && jwt=$(cat /tmp/token)
    USER_ARGS+=(--user-token "$name=$jwt")
  done </tmp/token.users
  echo "### named test users handed to the harness: $((${#USER_ARGS[@]} / 2)) ($(echo "${USER_ARGS[@]}" | tr ' ' '\n' | grep -c '^--user-token'))"
fi
sleep 1

echo "### harness over WSS"
ARCH=$(uname -m)
# The heartbeat params the oracles state are what PARAMS deployed (T-VER-PARAMDEPLOY); the harness
# refuses setup.params that names anything else, and the handshake confirms the gateway's own.
PARAM_ARGS=()
while IFS= read -r line; do
  case "$line" in
    P1\ *) PARAM_ARGS+=(--deployed-param "P1_s=${line#*= }") ;;
    P2\ *) PARAM_ARGS+=(--deployed-param "P2_s=${line#*= }") ;;
  esac
done <<<"${PARAMS:-}"
# T-VER-HLTDEPLOY: the halt policy the oracles state is in the payload's conf/halt_policy.json, and
# the core has loaded it by now. It says so itself, once, at load time:
#   [halt] halt policy: safe (4 configured output(s))
# That line is the read-back. The protocol has no handshake field for the policy (A049's oracle asks
# for one and the handshake does not carry it), so the runtime's own log is the only place it names
# what it loaded, and the harness refuses a scenario whose declared policy is not what the core says.
HALT_ARGS=()
LOADED=$(sed -n 's/.*\[halt\] halt policy: \([a-z]*\) (\([0-9]*\) configured output(s)).*/\1:\2/p' \
  /out/plc_main.log | head -1)
if [ -n "$HALT_POLICY" ]; then
  [ -n "$LOADED" ] || { echo "HALT POLICY FAIL: the core loaded no halt policy"; tail -5 /out/plc_main.log; exit 1; }
  echo "### the core loaded halt policy $LOADED"
  HALT_ARGS=(--deployed-halt-policy "$LOADED")
else
  HALT_ARGS=(--deployed-halt-policy none)
fi
# T-VER-BUILDDEPLOY: the build shape is read back out of the deployed debug-info.json — the same
# file the gateway loaded (core/generated/debug-info.json is a copy of it) — not asserted by the
# driver. `probes` is what the runtime will advertise; the harness confirms that at the handshake.
DEPLOYED_PROBES=$(python3 -c \
  'import json; print("on" if json.load(open("/payload/debug-info.json"))["build"]["probes"] else "off")')
DEPLOYED_ORIGINS=$(python3 -c \
  'import json; print(len(json.load(open("/payload/debug-info.json")).get("origins") or []))')
echo "### deployed build: probes=$DEPLOYED_PROBES origins=$DEPLOYED_ORIGINS (from debug-info.json)"
BUILD_ARGS=(--deployed-build "probes=$DEPLOYED_PROBES" --deployed-build "origins=$DEPLOYED_ORIGINS")
"$PY" tests/debug/wire/run_harness.py --endpoint https://127.0.0.1:8443 --insecure \
  --token "$(cat /tmp/token)" --plant /tmp/plant.sock --build-dir /out/build \
  ${USER_ARGS[@]+"${USER_ARGS[@]}"} \
  ${RESULTS:+--results "$RESULTS"} $(for t in $DEPLOYED; do printf -- '--deployed-task %s ' "$t"; done) \
  ${PARAM_ARGS[@]+"${PARAM_ARGS[@]}"} ${HALT_ARGS[@]+"${HALT_ARGS[@]}"} \
  ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} \
  --ran-on "linux-$ARCH docker: plc_main + probed corpus:$NAME (STruCpp) + reference plant + gateway over WSS 127.0.0.1:8443${SEED:+ (SEED=$SEED)}${COLLAPSE_USER:+ (COLLAPSE_USER=$COLLAPSE_USER)}" \
  "$@" | tee /out/harness.txt
RC=${PIPESTATUS[0]}
kill "$GW" 2>/dev/null
kill -INT "$PLC" 2>/dev/null
for _ in $(seq 1 100); do kill -0 "$PLC" 2>/dev/null || break; sleep 0.1; done
kill -9 "$PLC" 2>/dev/null
grep -E "ERROR|stuck|aborted" /out/plc_main.log | head -5
exit $RC
