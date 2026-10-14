"""Compare two reference-plant log slices indexed by PLC virtual time (A152).

Each slice is the plant log from one scenario run: its `reset` mark gives the PLC virtual time at
the scenario's start, and every later sample is keyed by the task's virtual time minus that.
The two runs must show the same outputs at every key both reached. The scan witness and outputs
that carry an absolute clock (`--ignore`, e.g. %QD2 = TIME()) are not compared.

Usage: compare_logs.py <log-a> <log-b> [--task Main] [--ignore %QD0,%QD2]
Exit: 0 identical, 1 different, 2 unusable input.

See also:
  tests/debug/plant/reference_plant.c - writes the log (one JSON object per line).
  tests/debug/plant/e2e.sh - runs a halt scenario with two halt durations and calls this.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Union

Value = Union[bool, int, float, str]


def by_vt(path: Path, task: str, ignore: List[str]) -> Dict[int, Dict[str, Value]]:
    origin = None
    rows: Dict[int, Dict[str, Value]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        doc = json.loads(line)
        if doc.get("mark") == "reset":
            origin, rows = float(doc["vt"]), {}
        elif origin is not None and "vt" in doc and task in doc["vt"]:
            key = round(float(doc["vt"][task]) - origin)
            if key >= 0:
                rows[key] = {k: v for k, v in doc["outputs"].items() if k not in ignore}
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("a", type=Path)
    ap.add_argument("b", type=Path)
    ap.add_argument("--task", default="Main")
    ap.add_argument("--ignore", default="%QD0,%QD2")
    opts = ap.parse_args()
    ignore = [s for s in opts.ignore.split(",") if s]
    a, b = by_vt(opts.a, opts.task, ignore), by_vt(opts.b, opts.task, ignore)
    common = sorted(set(a) & set(b))
    if len(common) < 10:
        print(f"UNUSABLE: {len(common)} common virtual-time keys")
        return 2
    diffs: List[Tuple[int, Dict[str, Value], Dict[str, Value]]] = [
        (k, a[k], b[k]) for k in common if a[k] != b[k]
    ]
    span = f"{len(common)} samples, vt {common[0]}..{common[-1]} ms after the schedule origin"
    if not diffs:
        print(f"IDENTICAL: {span}")
        return 0
    k, x, y = diffs[0]
    changed = {n: (x.get(n), y.get(n)) for n in sorted(set(x) | set(y)) if x.get(n) != y.get(n)}
    print(f"DIFFER: {len(diffs)} of {span}; first at vt +{k} ms: {changed}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
