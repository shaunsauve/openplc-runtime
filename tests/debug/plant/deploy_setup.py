"""Deploys the halt policy, the build shape and the halt mode its scenarios state (T-VER-HLTDEPLOY,
T-VER-BUILDDEPLOY, T-VER-HALTMODE).

Three `setup.*` keys describe the state the runtime runs in, not the session's requests:

* `setup.haltPolicy` / `setup.safeValues` — the project's `conf/halt_policy.json`, which the core
  loads at every start (core/src/plc_app/plc_output_hold.h PLC_HALT_POLICY_PATH). Written into the
  payload, so it ships with the sources exactly as program.zip carries it.
* `setup.build` — the shape of the build: `probes` (the probe-off build) and `path` (`editor` when
  the program's ST came from the LD/FBD transpiler and carries an origin map). `control:false` and
  `path:"library"` are NOT deployed: the runtime has no debug-control compile-out switch and the
  payload builder has one producer, so the harness refuses them at prepare() instead. This script
  says nothing about them, which is what keeps that refusal.
* `setup.haltMode` — a *session* setting (ADR-004 setHaltMode), not a boot one, so there is no
  file to write: the harness sends the request itself once the session is up. Recorded here so the
  driver can report what the oracles asked for.

  python3 tests/debug/plant/deploy_setup.py PROGRAM.st ORACLE...

Prints one `key=value` line per deployable fact, and writes nothing. Absent a fact, nothing is
deployed and the harness's prepare() refuses any scenario that declared it — deploying a key by
ignoring it is the defect setup.params was closed for. Exit 2 only when two oracles state different
things for one key, which is a run-construction error, not a scenario's verdict.

See also:
  tests/debug/plant/session_e2e.sh - the caller.
  tests/debug/harness/socketio_endpoint.py - checks the declarations against the deployed facts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Optional


def _one(oracles: list[Path], key: str) -> Optional[object]:
    """The single value the oracles state for `setup.<key>`, or None when none states one.

    Two oracles that state different values cannot share a runtime, so this is the one thing that
    stops a run: the caller is told to run them separately.
    """
    found: Optional[str] = None
    where: Optional[Path] = None
    value_found: object = None
    for path in oracles:
        value = json.loads(path.read_text()).get("setup", {}).get(key)
        if value is None:
            continue
        text = json.dumps(value, sort_keys=True)
        if found is None:
            found, where, value_found = text, path, value
        elif text != found:
            sys.exit(f"{where} and {path} state different {key}: {value_found} vs {value}")
    return value_found if found is not None else None


def halt_policy(oracles: list[Path]) -> Optional[str]:
    """`setup.haltPolicy` + `setup.safeValues` as the core's conf/halt_policy.json text.

    The runtime's file takes numbers (core/src/plc_app/plc_output_hold.cpp halt_policy_load
    refuses a non-numeric `value`), so a JSON boolean safe value is written as the IEC BOOL it
    stands for: `false` is 0, `true` is 1. Nothing else is rewritten, and `safeValues` without a
    `haltPolicy: safe` states no policy at all, so it is not deployed.
    """
    policy = _one(oracles, "haltPolicy")
    values = _one(oracles, "safeValues")
    if policy is None:
        if values is not None:
            print(
                "note: setup.safeValues with no setup.haltPolicy states no policy; "
                "not deployed",
                file=sys.stderr,
            )
        return None
    if policy not in ("hold", "safe"):
        sys.exit(f"unknown haltPolicy {policy!r}")
    doc: Dict[str, object] = {"policy": policy}
    if policy == "safe" and isinstance(values, dict):
        doc["safe"] = [
            {"location": loc, "value": int(v) if isinstance(v, bool) else v}
            for loc, v in values.items()
        ]
    return json.dumps(doc, sort_keys=True)


def build_facts(program: Path, oracles: list[Path]) -> Dict[str, str]:
    """`setup.build` as the deployable facts: `probes=on|off` and the origin map to feed.

    `probes` defaults to `on` because that is the build every run has deployed; `path` only ever
    names the origin map (`editor` feeds `<program.st>.origin.json`, `cli` feeds nothing, `library`
    names no build path this driver can vary).
    """
    build = _one(oracles, "build") or {}
    if not isinstance(build, dict):
        sys.exit(f"setup.build is not an object: {build!r}")
    facts = {"probes": "off" if build.get("probes") is False else "on"}
    path = build.get("path")
    if path == "editor":
        origin = program.with_name(program.name + ".origin.json")
        if origin.is_file():
            facts["origins"] = str(origin)
        else:
            print(
                f"note: {origin} is missing, so the editor build's origins are not deployed",
                file=sys.stderr,
            )
    return facts


def main() -> None:
    program, oracles = Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]]
    facts = dict(build_facts(program, oracles))
    policy = halt_policy(oracles)
    if policy is not None:
        facts["halt-policy"] = policy
    mode = _one(oracles, "haltMode")
    if mode is not None:
        if mode not in ("allStop", "nonStop"):
            sys.exit(f"unknown haltMode {mode!r}")
        facts["halt-mode"] = str(mode)
    for key, value in facts.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(f"FAIL: {exc.code}", file=sys.stderr)
            sys.exit(2)
        raise