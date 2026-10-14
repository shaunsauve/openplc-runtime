#!/usr/bin/env bash
# Run every corpus oracle against the debug endpoint and summarise the verdict counts.
# Extra arguments go to runner.py (e.g. --endpoint URL --insecure --results PATH --trace).
# Exit: 0 no DIVERGE, 1 a DIVERGE, 2 a harness error (a scenario that ended without a judgment).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$here/runner.py" "$here/../corpus" "$@"
