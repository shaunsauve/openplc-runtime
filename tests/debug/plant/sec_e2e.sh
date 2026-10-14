#!/usr/bin/env bash
# Session security, client loss (J7 headless) and wire hardening end to end: a real plc_main
# running the probed corpus:plant-io, the reference plant, and the real gateway over TLS on
# 127.0.0.1:8443 with P1 = 1 s, P2 = 5 s from debug.conf, driven by sec_e2e.py over WSS.
#
# Usage: tests/debug/plant/sec_e2e.sh
# Env:   STRUCPP  STruCpp checkout (default ../STruCpp), with dist/ built
#        OUT      logs (default build/sec-e2e)
#        N        requests per control type for A082 (default 1000)
#        IMAGE    Linux image with g++, cmake and the runtime venv (default the runtime image)
# Needs: node (host, STruCpp codegen) and docker.
#
# See also:
#   tests/debug/plant/sec_inner.sh - the container half.
#   tests/debug/plant/sec_e2e.py - the checks, by criterion.
#   tests/debug/plant/session_e2e.sh - the same stack driven by the headless harness.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
STRUCPP=${STRUCPP:-$REPO/../STruCpp}
OUT=${OUT:-$REPO/build/sec-e2e}
IMAGE=${IMAGE:-ghcr.io/autonomy-logic/openplc-runtime:latest}

rm -rf "$OUT"; mkdir -p "$OUT/payload"
OUT=$(cd "$OUT" && pwd)
node "$HERE/gen_payload.mjs" "$STRUCPP" "$REPO/tests/debug/corpus/plant-io/program.st" "$OUT/payload"
grep -q 'STRUCPP_PROBE' "$OUT"/payload/pou_*.cpp || { echo "FAIL: the payload has no probes"; exit 1; }
docker run --rm --cap-add SYS_NICE --ulimit rtprio=99 --entrypoint bash \
  -v "$REPO":/src:ro -v "$OUT/payload":/payload:ro -v "$OUT":/out -e N="${N:-1000}" \
  "$IMAGE" /src/tests/debug/plant/sec_inner.sh
