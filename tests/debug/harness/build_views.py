"""Build outputs for the runner: the directory reader (build_all.sh's layout), the `inspect`
views of tests/debug/corpus/README.md § Inspect views, and the build-derived argument symbols
(`~build`, `~lines`, `~leaves`). Pure apart from DirectoryBuild's file reads.

See also:
  tests/debug/harness/interfaces.py - the BuildOutputs seam and UnavailableBuild.
  tests/debug/harness/runner.py - calls view() for `inspect` steps and the symbol helpers for args.
  tests/debug/corpus/build_all.sh - writes the directory DirectoryBuild reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple, cast

from interfaces import Blocked, BuildOutputs, Json, JsonObj

FILES = {
    "debugInfo": "debug-info.json",
    "debugMap": "debug-map.json",
    "generatedCpp": "generated_debug.cpp",
}
OBJECT_CODE_BLOCKED = (
    "objectCode: no disassembly adapter in the harness yet (probe-site and size reads)"
)
# View keys an oracle may name that no build output can supply yet; asking for one is BLOCKED,
# never a DIVERGE on an absent key.
UNAVAILABLE_KEYS: Dict[str, Dict[str, str]] = {
    "debugInfo": {
        "loadMs": "the gateway's debug-info load time has no read-back path yet",
        "linkedPousMissing": "needs the compiler's linked-POU list, which no build output carries",
    },
}


class DirectoryBuild:
    """`<root>/<corpus>/debug/<file>` for the program, `<root>/<corpus>/<variant-stem>.debug/<file>`
    for a variant (tests/debug/corpus/build_all.sh)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def read(self, corpus: str, kind: str, variant: Optional[str]) -> Json:
        if kind == "objectCode":
            raise Blocked(OBJECT_CODE_BLOCKED)
        name = FILES.get(kind)
        if name is None:
            raise Blocked(f"no build output named {kind!r}")
        sub = "debug" if variant is None else f"{Path(variant).stem}.debug"
        path = self.root / corpus / sub / name
        if not path.is_file():
            raise Blocked(f"no build outputs at {path} (run tests/debug/corpus/build_all.sh)")
        text = path.read_text(encoding="utf-8")
        return text if kind == "generatedCpp" else cast(Json, json.loads(text))


def _obj(node: Json) -> JsonObj:
    return node if isinstance(node, dict) else {}


def _list(node: Json) -> List[Json]:
    return node if isinstance(node, list) else []


def _file_paths(info: JsonObj) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for f in _list(info.get("files")):
        fid, path = _obj(f).get("id"), _obj(f).get("path")
        if isinstance(fid, int) and isinstance(path, str):
            out[fid] = path
    return out


def _probe_rows(info: JsonObj) -> List[JsonObj]:
    """The columnar `probes` table (ADR-002 D3) as one object per probe."""
    cols = _obj(info.get("probes"))
    n = len(_list(cols.get("line")))
    return [
        {k: _list(v)[i] if i < len(_list(v)) else None for k, v in cols.items()} for i in range(n)
    ]


def debug_info_view(info: JsonObj) -> JsonObj:
    files = _file_paths(info)
    rows = _probe_rows(info)
    view: JsonObj = dict(info)
    view["pous"] = {
        str(_obj(p).get("name")): p for p in _list(info.get("pous")) if isinstance(p, dict)
    }
    view["linesByFile"] = {
        files[int(fid)]: lines
        for fid, lines in _obj(info.get("lines")).items()
        if fid.isdigit() and int(fid) in files
    }
    view["probeCount"] = len(rows)
    view["instanceCount"] = len(_list(_obj(info.get("instances")).get("path")))
    generated: set[Tuple[str, int]] = set()
    origin_of: set[Tuple[str, int, str]] = set()
    origins = _list(info.get("origins"))
    for r in rows:
        f, line = files.get(cast(int, r.get("file")), "?"), r.get("line")
        if not isinstance(line, int):
            continue
        flags = r.get("flags")
        if isinstance(flags, int) and flags & 1:
            generated.add((f, line))
        o = r.get("origin")
        if isinstance(o, int) and 0 <= o < len(origins):
            origin_of.add((f, line, str(_obj(origins[o]).get("id"))))
    view["generatedLines"] = [{"file": f, "line": ln} for f, ln in sorted(generated)]
    view["originOf"] = [{"file": f, "line": ln, "origin": o} for f, ln, o in sorted(origin_of)]
    return view


def view(build: BuildOutputs, corpus: str, kind: str) -> JsonObj:
    """The object an `inspect` step's expectation matches (README § Inspect views)."""
    raw = build.read(corpus, kind, None)
    if kind == "debugInfo":
        return debug_info_view(_obj(raw))
    if kind == "debugMap":
        doc = dict(_obj(raw))
        doc["leafCount"] = len(_list(doc.get("leaves")))
        return doc
    if kind == "generatedCpp":
        return {"text": raw if isinstance(raw, str) else ""}
    raise Blocked(f"no inspect view for {kind!r}")


def identity(build: BuildOutputs, corpus: str, variant: Optional[str]) -> JsonObj:
    """`~build: identity[:variant]`: the `{md5, buildId}` a client presents (ADR-002 D5)."""
    b = _obj(_obj(build.read(corpus, "debugInfo", variant)).get("build"))
    if not isinstance(b.get("md5"), str) or not isinstance(b.get("buildId"), str):
        raise Blocked(f"debug info of {variant or 'the program'} has no build md5/buildId")
    return {"md5": b["md5"], "buildId": b["buildId"]}


def breakpoint_lines(build: BuildOutputs, corpus: str, file: str, n: int) -> List[Json]:
    """`~lines: n`: the first n breakpoint-able lines of `file` (debug info `lines`, F016)."""
    info = _obj(build.read(corpus, "debugInfo", None))
    ids = [fid for fid, path in _file_paths(info).items() if path == file]
    lines = _list(_obj(info.get("lines")).get(str(ids[0]))) if ids else []
    if len(lines) < n:
        raise Blocked(f"~lines {n}: {file} has only {len(lines)} breakpoint-able lines")
    return lines[:n]


def leaf_paths(build: BuildOutputs, corpus: str, prefix: str, n: int) -> List[Json]:
    """`~leaves: prefix, ~count: n`: the first n debug-map leaf paths under `prefix`."""
    leaves = _list(_obj(build.read(corpus, "debugMap", None)).get("leaves"))
    # DebugMapV2 writes instance names in upper case (debug-table-gen.ts DebugLeaf.path).
    want = prefix.upper() + "."
    paths = [
        p
        for p in (_obj(leaf).get("path") for leaf in leaves)
        if isinstance(p, str) and p.upper().startswith(want)
    ]
    if len(paths) < n:
        raise Blocked(f"~leaves {prefix}: only {len(paths)} leaves, {n} asked")
    return cast(List[Json], paths[:n])
