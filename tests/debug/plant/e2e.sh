#!/usr/bin/env bash
# Run a corpus program's scenarios against a real plc_main with the reference plant (F150) and
# no debugger, once with the plant on PLC virtual time and once on wall time (A152 red-proof).
#
# Usage: tests/debug/plant/e2e.sh [corpus]        (default ton-timer)
# Env:   STRUCPP  STruCpp checkout (default ../STruCpp), built (dist/) by build_all.sh
#        OUT      logs, plant logs and runner traces (default build/plant-e2e)
#        IMAGE    Linux image with g++, cmake and Python headers (default the runtime image)
#        MODES    plant clocks to run (default "virtual wall")
# Needs: node (host, STruCpp codegen) and docker. A scenario that needs a debug session is
#        BLOCKED; the virtual-clock run should PASS the rest and the wall-clock run DIVERGE where
#        the plant's schedule meets a halt.
#
# See also:
#   tests/debug/plant/e2e_inner.sh - the container half: build, compile, start, run.
#   tests/debug/plant/gen_payload.mjs - the editor-shaped payload from a corpus program.
#   tests/debug/harness/reference_plant.py - the plant adapter and the debugger-less endpoint.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
CORPUS=${1:-ton-timer}
STRUCPP=${STRUCPP:-$REPO/../STruCpp}
OUT=${OUT:-$REPO/build/plant-e2e}
IMAGE=${IMAGE:-ghcr.io/autonomy-logic/openplc-runtime:latest}

rm -rf "$OUT"; mkdir -p "$OUT/payload"
node "$HERE/gen_payload.mjs" "$STRUCPP" "$REPO/tests/debug/corpus/$CORPUS/program.st" "$OUT/payload"
docker run --rm --cap-add SYS_NICE --ulimit rtprio=99 --entrypoint bash \
  -e CORPUS="$CORPUS" -e MODES="${MODES:-virtual wall}" \
  -v "$REPO":/src:ro -v "$OUT/payload":/payload:ro -v "$OUT":/out \
  "$IMAGE" /src/tests/debug/plant/e2e_inner.sh
