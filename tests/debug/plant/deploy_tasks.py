"""Deploys a corpus program with the task intervals its scenarios state (T-VER-TASKDEPLOY).

An oracle's `setup.tasks` ({"Main": {"intervalMs": 500}}) names the interval the scenario needs,
which is a property of the program the runtime runs, not of the session: the program's
CONFIGURATION is rewritten before it is compiled, and the harness is told what was deployed
(run_harness.py --deployed-task), so a scenario whose stated interval was not deployed is still
BLOCKED rather than judged against the wrong program.

  python3 tests/debug/plant/deploy_tasks.py PROGRAM.st OUT.st ORACLE...

Writes OUT.st (PROGRAM.st with every stated task's INTERVAL replaced) and prints one
`NAME=MS` line per task of the deployed program. Exit 2 when two oracles state different
intervals for one task, or a stated task is not in the program: run them in separate
invocations.

See also:
  tests/debug/plant/session_e2e.sh - the caller.
  tests/debug/harness/socketio_endpoint.py - checks setup.tasks against the deployed tasks.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict

# TASK Main(INTERVAL := T#100ms, PRIORITY := 1);
TASK_LINE = re.compile(r"(\bTASK\s+(\w+)\s*\(\s*INTERVAL\s*:=\s*T#)(\d+(?:\.\d+)?)(ms|s)\b", re.I)


def stated(oracles: list[Path]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for path in oracles:
        tasks = json.loads(path.read_text()).get("setup", {}).get("tasks", {})
        for name, spec in tasks.items():
            ms = spec.get("intervalMs") if isinstance(spec, dict) else None
            if not isinstance(ms, (int, float)):
                continue
            key = name.casefold()
            if key in out and out[key] != ms:
                sys.exit(f"{path}: task {name} at {ms} ms, another oracle states {out[key]} ms")
            out[key] = float(ms)
    return out


def main() -> None:
    program, target, oracles = Path(sys.argv[1]), Path(sys.argv[2]), [Path(p) for p in sys.argv[3:]]
    want = stated(oracles)
    deployed: Dict[str, float] = {}

    def rewrite(m: "re.Match[str]") -> str:
        name = m.group(2)
        ms = float(m.group(3)) * (1000.0 if m.group(4).lower() == "s" else 1.0)
        ms = want.pop(name.casefold(), ms)
        deployed[name] = ms
        return f"{m.group(1)}{ms:g}ms"

    text = TASK_LINE.sub(rewrite, program.read_text())
    if want:
        sys.exit(f"stated tasks not in {program}: {', '.join(sorted(want))}")
    target.write_text(text)
    for name, ms in deployed.items():
        print(f"{name}={ms:g}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(f"FAIL: {exc.code}", file=sys.stderr)
            sys.exit(2)
        raise
