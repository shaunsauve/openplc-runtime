#!/usr/bin/env bash
# The first real end-to-end debug session: a real plc_main running the probed corpus:plant-io
# (STruCpp's codegen, probes on, built by the runtime's own compile.sh), the reference plant, and
# the real gateway (webserver/debug_gateway over TLS on 127.0.0.1:8443, linked to plc_main's
# /run/runtime/plc_debug.socket), driven by the headless harness over WSS with the
# simple-websocket client (webserver/dap/wss_client.py).
#
# Usage: tests/debug/plant/session_e2e.sh [--results PATH] [ORACLE...]   (default: the J1 run-control set)
#        Every ORACLE must be of one corpus program (the first one's directory), which is built and run
#        with the task intervals the oracles' setup.tasks state (tests/debug/plant/deploy_tasks.py),
#        the halt policy and build shape they state (tests/debug/plant/deploy_setup.py), and the
#        heartbeat params they state (below). Every `{"~token": "user:<name>"}` an oracle names is
#        created as a real account and logged in as, and its JWT is handed to the runner as
#        --user-token (below); COLLAPSE_USER=<name> serves one of them as the endpoint's own
#        identity instead, which is the red-proof for a second identity.
# Env:   STRUCPP  STruCpp checkout (default ../STruCpp), with dist/ built
#        OUT      logs and traces (default build/session-e2e)
#        RESULTS  status/runs JSON lines to append (default: none; e.g.
#                 ../openplc-debug/status/runs/u4-e2e.jsonl)
#        SEED     a seeded defect for a red proof: `late` arms every breakpoint one probe late
#                 (the stop comes one statement after the breakpoint's line); `leafoff` reads
#                 every leaf one past the asked one (the variables read are not the program's);
#                 `stepin-over` makes step into a step over (A035); `twostops` parks a step that
#                 lands on an armed breakpoint twice, as a step and as the breakpoint (A038);
#                 `twoscans` makes a cycle step run two scans (A036); `skipout` skips the
#                 copy-out of a scan that was halted and resumed (A037); `stopdetach` makes stop
#                 also end the session (A033); `forcecell` seeds a force that pins only the
#                 variable cell, not the I/O image (A109: the plant never sees a forced output);
#                 `forceowner` seeds a force registered with no owner (ADR-005 D5.1);
#                 `forcettl` seeds a TTL that is recorded but never expires (A116);
#                 `forceallowner` seeds a cancel-all that clears only the caller's own (A188);
#                 `safestop-early` shrinks the P2 timer to a tenth, so a lost controller safe-stops
#                 seconds early and only the notBeforeMs floor of A057/A177 can see it
#        IMAGE    Linux image with g++, cmake and the runtime venv (default the runtime image)
# Needs: node (host, STruCpp codegen) and docker.
#
# See also:
#   tests/debug/plant/session_inner.sh - the container half.
#   tests/debug/plant/runctl_e2e.sh - the same runtime and plant driven over the core link alone.
#   tests/debug/wire/run_harness.py - the harness runner over WSS.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
STRUCPP=${STRUCPP:-$REPO/../STruCpp}
OUT=${OUT:-$REPO/build/session-e2e}
IMAGE=${IMAGE:-ghcr.io/autonomy-logic/openplc-runtime:latest}
SEED=${SEED:-}
if [ "${1:-}" = "--results" ]; then export RESULTS=$2; shift 2; fi
ORACLES=("$@")
if [ ${#ORACLES[@]} -eq 0 ]; then
  # Two invocations: an oracle that ends in `stop` leaves the PLC STOPPED, and nothing in the
  # harness starts it again, so at most one such oracle can run per plc_main, and it runs last.
  C=tests/debug/corpus/plant-io
  RC=0
  "$0" "$C/j1-run-control.oracle.json" "$C/bp-before-statement.oracle.json" \
       "$C/j1-pause-resume-stop.oracle.json" || RC=1
  OUT=$OUT-2 "$0" "$C/pause-resume-stop.oracle.json" || RC=1
  exit $RC
fi
C=$(dirname "${ORACLES[0]}")
for o in "${ORACLES[@]}"; do
  [ "$(dirname "$o")" = "$C" ] || { echo "FAIL: $o is not of corpus $C (run one corpus per invocation)"; exit 2; }
done
COMMIT=$(git -C "$REPO" rev-parse --short HEAD)$(git -C "$REPO" diff --quiet HEAD -- core webserver tests || echo -dirty)
# The payload's debug info and probes come from STruCpp, so the runtime head alone does not
# identify what was measured. E13.5(c) requires a verdict to be measured at the current head of
# every member repo, which is unprovable if the compiler's head is never recorded.
STRUCPP_COMMIT=$(git -C "$STRUCPP" rev-parse --short HEAD 2>/dev/null || echo unknown)
COMMIT="$COMMIT strucpp:$STRUCPP_COMMIT"

rm -rf "$OUT"; mkdir -p "$OUT/payload"
OUT=$(cd "$OUT" && pwd)  # docker -v needs an absolute path
# T-VER-HLTDEPLOY / T-VER-BUILDDEPLOY: the halt policy, the build shape (probes on/off, the editor's
# origin map) and the halt mode come from the oracles' own setup (deploy_setup.py). What it cannot
# deploy — build.control, build.path "library" — it says nothing about, so the harness refuses the
# scenarios that asked for it instead of running them against whatever happened to be loaded.
SETUP=$(python3 "$HERE/deploy_setup.py" "$REPO/$C/program.st" "${ORACLES[@]/#/$REPO/}") \
  || { echo "FAIL: the oracles state different setup; run them separately"; exit 2; }
setup_of() { sed -n "s/^$1=//p" <<<"$SETUP"; }
PROBES=$(setup_of probes)
ORIGINS=$(setup_of origins)
HALT_POLICY=$(setup_of halt-policy)
HALT_MODE=$(setup_of halt-mode)
echo "### deployed build: probes=$PROBES${ORIGINS:+ origins=$ORIGINS}"
[ -n "$HALT_POLICY" ] && echo "### deployed halt policy: $HALT_POLICY (an oracle declares setup.haltPolicy)"
[ -n "$HALT_MODE" ] && echo "### requested halt mode: $HALT_MODE (the harness sends setHaltMode; it is a session setting)"
# T-VER-TASKDEPLOY: the program is deployed with the task intervals the oracles state.
mkdir -p "$OUT/src"
DEPLOYED=$(python3 "$HERE/deploy_tasks.py" "$REPO/$C/program.st" "$OUT/src/program.st" "${ORACLES[@]/#/$REPO/}") \
  || { echo "FAIL: the oracles state different task intervals; run them separately"; exit 2; }
echo "### deployed tasks: $(echo $DEPLOYED)"
# A scenario that declares setup.startHalted needs a program that has not run when the harness
# attaches. The harness cannot deploy that: a session that deploys it and then detaches releases
# the halt (ADR-005 D4.6 resumes on a clean detach), so the runtime boots halted instead. Off
# unless an oracle asks, so a run without the declaration boots exactly as before.
START_HALTED=$(python3 - "$@" <<'PY'
import json, sys
print("1" if any(json.load(open(p)).get("setup", {}).get("startHalted") is True
                for p in sys.argv[1:]) else "")
PY
) || { echo "FAIL: cannot read setup.startHalted from the oracles"; exit 2; }
[ -n "$START_HALTED" ] && echo "### boot start halted: $START_HALTED (an oracle declares setup.startHalted)"
# T-VER-PARAMDEPLOY: a scenario that declares setup.params needs the gateway running at that
# P1/P2. Nothing in the container does that itself, so the driver deploys it — debug.conf text
# read by serve.py (OPENPLC_DEBUG_PARAMS) — and the harness's prepare() refuses the scenario
# unless what was deployed is what the oracle states, and the handshake confirms the gateway's
# own P1/P2. Off unless an oracle asks, so a run without the declaration is unchanged.
PARAMS=$(python3 - "$@" <<'PY'
import json, sys
sets = []
for p in sys.argv[1:]:
    params = json.load(open(p)).get("setup", {}).get("params")
    if params is not None:
        sets.append((p, params))
if not sets:
    sys.exit(0)
first = sets[0][1]
for path, params in sets[1:]:
    if params != first:
        sys.exit(f"oracles state different heartbeat params ({path} vs {sets[0][0]})")
print("\n".join(f"{k.replace('_s', '')} = {v:g}" for k, v in sorted(first.items())))
PY
) || { echo "FAIL: cannot read setup.params from the oracles"; exit 2; }
[ -n "$PARAMS" ] && echo "### deployed heartbeat params: $(echo $PARAMS | tr '\n' ' ') (an oracle declares setup.params)"
# Named test users. A scenario's `{"~token": "user:<name>"}` is a second identity: the harness
# cannot mint a JWT, so the runtime is asked for the accounts (tests/debug/wire/serve.py,
# OPENPLC_DEBUG_USERS) and their real logins are handed to the runner as --user-token. The names
# come from the oracles being run, so a run that names none creates no account and is unchanged.
USERS=$(python3 - "$@" <<'PY'
import json, re, sys
names = []
for path in sys.argv[1:]:
    for name in re.findall(r'"~token"\s*:\s*"user:([A-Za-z0-9_.-]+)"', json.dumps(json.load(open(path)))):
        if name not in names:
            names.append(name)
print(",".join(names))
PY
) || { echo "FAIL: cannot read the named test users from the oracles"; exit 2; }
[ -n "$USERS" ] && echo "### named test users: $(echo "$USERS" | tr ',' ' ')"
# COLLAPSE_USER=<name> serves that one name as the endpoint's own identity instead of its own
# login: the red-proof for a second identity (a role check that cannot be shown failing is not a
# role check). It changes what the name resolves to and nothing else.
[ -n "${COLLAPSE_USER:-}" ] && echo "### RED PROOF: $COLLAPSE_USER is served as the endpoint's own identity"
node "$HERE/gen_payload.mjs" "$STRUCPP" "$OUT/src/program.st" "$OUT/payload" --probes "$PROBES" \
  ${ORIGINS:+--origins "$ORIGINS"}
# A probe-off build (A019) has no probe code at all: the STRUCPP_PROBE check is what a probe build
# must satisfy, so it does not apply to one.
if [ "$PROBES" = on ]; then
  grep -q 'STRUCPP_PROBE' "$OUT"/payload/pou_*.cpp || { echo "FAIL: the payload has no probes"; exit 1; }
else
  if grep -q 'STRUCPP_PROBE' "$OUT"/payload/pou_*.cpp; then
    echo "FAIL: a probe-off build has probe code"; exit 1
  fi
fi
[ -f "$OUT/payload/debug-info.json" ] || { echo "FAIL: the payload has no debug-info.json"; exit 1; }
# T-VER-HLTDEPLOY: the policy ships with the sources, as conf/halt_policy.json of program.zip does
# (core/src/plc_app/plc_output_hold.h), so `cp -r /payload/. core/generated/` puts it where the core
# looks for it. The core's own startup log says which policy it loaded; session_inner.sh reads that
# line back and the harness refuses a scenario whose policy is not the one the runtime reports.
if [ -n "$HALT_POLICY" ]; then
  mkdir -p "$OUT/payload/conf"
  printf '%s\n' "$HALT_POLICY" >"$OUT/payload/conf/halt_policy.json"
fi
RUNS_MOUNT=()
RESULTS_IN=""
if [ -n "${RESULTS:-}" ]; then
  mkdir -p "$(dirname "$RESULTS")"
  RUNS_MOUNT=(-v "$(cd "$(dirname "$RESULTS")" && pwd)":/runs)
  RESULTS_IN=/runs/$(basename "$RESULTS")
fi
docker run --rm --cap-add SYS_NICE --ulimit rtprio=99 --entrypoint bash \
  -v "$REPO":/src:ro -v "$OUT/payload":/payload:ro -v "$OUT":/out ${RUNS_MOUNT[@]+"${RUNS_MOUNT[@]}"} \
  -e SEED="$SEED" -e RESULTS="$RESULTS_IN" -e HARNESS_COMMIT="$COMMIT" -e CORPUS="$C" -e DEPLOYED="$(echo $DEPLOYED)" \
  -e START_HALTED="$START_HALTED" -e PARAMS="$PARAMS" -e USERS="$USERS" \
  -e COLLAPSE_USER="${COLLAPSE_USER:-}" \
  -e HALT_POLICY="$HALT_POLICY" -e HALT_MODE="$HALT_MODE" -e PROBES="$PROBES" \
  "$IMAGE" /src/tests/debug/plant/session_inner.sh "${ORACLES[@]}"
