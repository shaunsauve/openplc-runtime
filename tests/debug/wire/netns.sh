#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®
#
# A068's second run: the gateway (fake core behind it) serves WSS in one container and the
# harness runs wire-control.oracle.json from another container, so a separate network namespace,
# reaching the server by its address on a docker network, never by loopback.
#
#   tests/debug/wire/netns.sh [IMAGE] [BIND]
#
# IMAGE defaults to ghcr.io/autonomy-logic/openplc-runtime:latest (its venvs/runtime has every
# dependency). BIND defaults to 0.0.0.0, as webserver/app.py binds; BIND=127.0.0.1 is the red
# proof (no route from another namespace, so the run cannot attach).
set -euo pipefail

IMAGE="${1:-ghcr.io/autonomy-logic/openplc-runtime:latest}"
BIND="${2:-0.0.0.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
NET="wire-netns-$$"
SHARE="$(mktemp -d)"
PY=/workdir/venvs/runtime/bin/python

cleanup() {
    docker rm -f "$NET-server" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -rf "$SHARE"
}
trap cleanup EXIT

docker network create "$NET" >/dev/null
docker run -d --name "$NET-server" --network "$NET" -v "$ROOT:/wt:ro" -v "$SHARE:/share" \
    "$IMAGE" sh -c "cp -r /wt /src && cd /src && $PY tests/debug/wire/serve.py $BIND 8443 /share/token" \
    >/dev/null

for _ in $(seq 1 100); do
    [ -s "$SHARE/token" ] && break
    sleep 0.2
done
[ -s "$SHARE/token" ] || { docker logs "$NET-server"; echo "server did not start"; exit 2; }
ADDR="$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" "$NET-server")"
echo "server at $ADDR (bound $BIND), client in its own network namespace"

docker run --rm --network "$NET" -v "$ROOT:/wt:ro" "$IMAGE" sh -c \
    "cp -r /wt /src && cd /src && $PY tests/debug/wire/run_harness.py --insecure \
     --endpoint https://$ADDR:8443 --token $(cat "$SHARE/token") \
     tests/debug/wire/wire-control.oracle.json"
