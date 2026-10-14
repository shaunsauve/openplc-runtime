#!/usr/bin/env bash
# Session security, client loss (J7 headless) and wire hardening end to end: a real plc_main
# running the probed corpus:plant-io, the reference plant, and the real gateway over TLS on
# 127.0.0.1:8443 with P1 = 1 s, P2 = 5 s from debug.conf, driven by sec_e2e.py over WSS.
#
# Usage: tests/debug/plant/sec_e2e.sh [--results PATH]
#        --results appends one status/runs JSON line per criterion (PASS only if all its checks pass)
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
RESULTS=${RESULTS:-}
if [ "${1:-}" = "--results" ]; then RESULTS=$2; shift 2; fi
COMMIT=$(git -C "$REPO" rev-parse --short HEAD)$(git -C "$REPO" diff --quiet HEAD -- core webserver tests || echo -dirty)

rm -rf "$OUT"; mkdir -p "$OUT/payload"
OUT=$(cd "$OUT" && pwd)
node "$HERE/gen_payload.mjs" "$STRUCPP" "$REPO/tests/debug/corpus/plant-io/program.st" "$OUT/payload"
grep -q 'STRUCPP_PROBE' "$OUT"/payload/pou_*.cpp || { echo "FAIL: the payload has no probes"; exit 1; }
docker run --rm --cap-add SYS_NICE --ulimit rtprio=99 --entrypoint bash \
  -v "$REPO":/src:ro -v "$OUT/payload":/payload:ro -v "$OUT":/out -e N="${N:-1000}" \
  "$IMAGE" /src/tests/debug/plant/sec_inner.sh && RC=0 || RC=$?
if [ -n "$RESULTS" ]; then
  python3 - "$OUT/sec.txt" "$RESULTS" "$COMMIT" "linux-$(docker run --rm --entrypoint uname "$IMAGE" -m) docker: plc_main + probed corpus:plant-io (STruCpp) + reference plant + gateway over WSS 127.0.0.1:8443, P1 1 s, P2 5 s" <<'PY'
import datetime, json, re, sys
src, dst, commit, ran_on = sys.argv[1:5]
by = {}
for line in open(src, encoding="utf-8"):
    m = re.match(r"(PASS|FAIL) (A\d+): (.*)", line.strip())
    if m:
        by.setdefault(m.group(2), []).append((m.group(1), m.group(3)))
stamp = datetime.datetime.now().isoformat(timespec="seconds")
with open(dst, "a", encoding="utf-8") as fh:
    for crit, checks in by.items():
        failed = [w for v, w in checks if v == "FAIL"]
        fh.write(json.dumps({
            "criterion": crit, "verdict": "FAIL" if failed else "PASS",
            "test": f"tests/debug/plant/sec_e2e.py::{crit}", "ran_on": ran_on, "commit": commit,
            "repo": "openplc-runtime", "timestamp": stamp,
            "evidence": ("; ".join(failed) if failed else f"{len(checks)} checks passed: " + "; ".join(w for _, w in checks))[:300],
        }, separators=(",", ":")) + "\n")
print(f"results: appended {len(by)} lines to {dst}")
PY
fi
exit $RC
