#!/usr/bin/env bash
# Run control end to end: a real plc_main with a probed corpus:plant-io (STruCpp's codegen with
# probes on, compiled by the runtime's own compile.sh), the reference plant, and the core link.
#
# Usage: tests/debug/plant/runctl_e2e.sh
# Env:   STRUCPP  STruCpp checkout (default ../STruCpp), with dist/ built
#        OUT      logs (default build/runctl-e2e)
#        IMAGE    Linux image with g++, cmake and Python headers (default the runtime image)
# Needs: node (host, STruCpp codegen) and docker.
#
# See also:
#   tests/debug/plant/runctl_inner.sh - the container half: build, compile, start, run.
#   tests/debug/plant/runctl_e2e.py - the checks, by criterion.
#   tests/debug/plant/e2e.sh - the debugger-less scenarios against the same plant.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
STRUCPP=${STRUCPP:-$REPO/../STruCpp}
OUT=${OUT:-$REPO/build/runctl-e2e}
IMAGE=${IMAGE:-ghcr.io/autonomy-logic/openplc-runtime:latest}

rm -rf "$OUT"; mkdir -p "$OUT/payload"
node "$HERE/gen_payload.mjs" "$STRUCPP" "$REPO/tests/debug/corpus/plant-io/program.st" "$OUT/payload"
grep -q 'STRUCPP_PROBE' "$OUT"/payload/pou_*.cpp || { echo "FAIL: the payload has no probes"; exit 1; }
docker run --rm --cap-add SYS_NICE --ulimit rtprio=99 --entrypoint bash \
  -v "$REPO":/src:ro -v "$OUT/payload":/payload:ro -v "$OUT":/out \
  "$IMAGE" /src/tests/debug/plant/runctl_inner.sh
