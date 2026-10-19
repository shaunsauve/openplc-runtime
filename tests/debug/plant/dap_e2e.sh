#!/usr/bin/env bash
# Run the DAP harness (tests/debug/dap/dap_harness.py) against a real plc_main: the adapter is
# launched as a subprocess INSIDE the container and driven over DAP, so every fact the run reports
# came through the adapter's own protocol.
#
# Usage: tests/debug/plant/dap_e2e.sh [--results PATH] [--seed NAME] [CORPUS...]
#          Each CORPUS is one of tests/debug/corpus/<name> (default: the two E47.1 criteria run on),
#          built with probes and run with the task intervals its program declares. One corpus per
#          invocation: a container serves one program, and the adapter's `terminate` stops it.
# Env:   STRUCPP  STruCpp checkout (default ../STruCpp), with dist/ built
#        OUT      logs and traces (default build/dap-harness)
#        RESULTS  status/runs JSON lines to append (default: none)
#        SEED     a seeded adapter defect for a red proof: `steplie` (A073: advertise
#                 supportsStepBack with no handler), `varcache` (A074: answer `variables` from the
#                 previous stop's cache), `nocred` (A075: attach ignores the credential it is given)
#        IMAGE    Linux image with g++, cmake and the runtime venv
#
# See also:
#   tests/debug/plant/dap_inner.sh    - the container half.
#   tests/debug/plant/session_e2e.sh   - the same runtime driven over the wire directly.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
STRUCPP=${STRUCPP:-$REPO/../STruCpp}
OUT=${OUT:-$REPO/build/dap-harness}
IMAGE=${IMAGE:-ghcr.io/autonomy-logic/openplc-runtime:latest}
SEED=${SEED:-}
RESULTS=${RESULTS:-}
CORPORA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --results) RESULTS=$2; shift 2 ;;
    --seed) SEED=$2; shift 2 ;;
    --) shift; CORPORA+=("$@"); break ;;
    *) CORPORA+=("$1"); shift ;;
  esac
done
[ ${#CORPORA[@]} -eq 0 ] && CORPORA=(nested-fb-loop plant-io)
COMMIT=$(git -C "$REPO" rev-parse --short HEAD)$(git -C "$REPO" diff --quiet HEAD -- core webserver tests || echo -dirty)
STRUCPP_COMMIT=$(git -C "$STRUCPP" rev-parse --short HEAD 2>/dev/null || echo unknown)
COMMIT="$COMMIT strucpp:$STRUCPP_COMMIT"

rm -rf "$OUT"; mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
RUNS_MOUNT=()
RESULTS_IN=""
if [ -n "$RESULTS" ]; then
  mkdir -p "$(dirname "$RESULTS")"
  RUNS_MOUNT=(-v "$(cd "$(dirname "$RESULTS")" && pwd)":/runs)
  RESULTS_IN=/runs/$(basename "$RESULTS")
fi
RC=0
for name in "${CORPORA[@]}"; do
  C=tests/debug/corpus/$name
  [ -d "$REPO/$C" ] || { echo "FAIL: no corpus $C"; exit 2; }
  PAY="$OUT/$name"
  mkdir -p "$PAY"
  node "$HERE/gen_payload.mjs" "$STRUCPP" "$REPO/$C/program.st" "$PAY" --probes on
  grep -q 'STRUCPP_PROBE' "$PAY"/pou_*.cpp || { echo "FAIL: $PAY has no probes"; exit 1; }
  [ -f "$PAY/debug-info.json" ] || { echo "FAIL: $PAY has no debug-info.json"; exit 1; }
  echo "### $name: payload built with probes"
  docker run --rm --cap-add SYS_NICE --ulimit rtprio=99 --entrypoint bash \
    -v "$REPO":/src:ro -v "$PAY":/payload:ro -v "$OUT":/out ${RUNS_MOUNT[@]+"${RUNS_MOUNT[@]}"} \
    -e SEED="$SEED" -e RESULTS="$RESULTS_IN" -e HARNESS_COMMIT="$COMMIT" -e CORPUS="$C" \
    -e ONLY="${ONLY:-}" -e TRACE_DIR="${TRACE_DIR:-/out/trace}" \
    "$IMAGE" /src/tests/debug/plant/dap_inner.sh || RC=1
done
exit $RC