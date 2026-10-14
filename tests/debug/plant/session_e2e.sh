#!/usr/bin/env bash
# The first real end-to-end debug session: a real plc_main running the probed corpus:plant-io
# (STruCpp's codegen, probes on, built by the runtime's own compile.sh), the reference plant, and
# the real gateway (webserver/debug_gateway over TLS on 127.0.0.1:8443, linked to plc_main's
# /run/runtime/plc_debug.socket), driven by the headless harness over WSS with the
# simple-websocket client (tests/debug/wire/wss_client.py).
#
# Usage: tests/debug/plant/session_e2e.sh [--results PATH] [ORACLE...]   (default: the J1 run-control set)
#        Every ORACLE must be of one corpus program (the first one's directory), which is built and run
#        with the task intervals the oracles' setup.tasks state (tests/debug/plant/deploy_tasks.py).
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
#                 also end the session (A033)
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

rm -rf "$OUT"; mkdir -p "$OUT/payload"
OUT=$(cd "$OUT" && pwd)  # docker -v needs an absolute path
# T-VER-TASKDEPLOY: the program is deployed with the task intervals the oracles state.
mkdir -p "$OUT/src"
DEPLOYED=$(python3 "$HERE/deploy_tasks.py" "$REPO/$C/program.st" "$OUT/src/program.st" "${ORACLES[@]/#/$REPO/}") \
  || { echo "FAIL: the oracles state different task intervals; run them separately"; exit 2; }
echo "### deployed tasks: $(echo $DEPLOYED)"
node "$HERE/gen_payload.mjs" "$STRUCPP" "$OUT/src/program.st" "$OUT/payload"
grep -q 'STRUCPP_PROBE' "$OUT"/payload/pou_*.cpp || { echo "FAIL: the payload has no probes"; exit 1; }
[ -f "$OUT/payload/debug-info.json" ] || { echo "FAIL: the payload has no debug-info.json"; exit 1; }
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
  "$IMAGE" /src/tests/debug/plant/session_inner.sh "${ORACLES[@]}"
