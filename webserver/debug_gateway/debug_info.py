# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Loads the program's `debug-info.json` (ADR-002 D3) and resolves source lines to probe IDs and
probe IDs back to source locations, the gateway's half of symbolic resolution (ADR-003 D1.3).

See also:
  webserver/debug_gateway/gateway.py - uses it for setBreakpoints, runToCursor and stop events.
  docs/DEBUG_PROTOCOL.md - "Session protocol v1", the location fields in requests and events.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from webserver.debug_gateway.protocol import ErrorCode, Json, JsonObj, Refusal

SUPPORTED_MAJOR = 1


def _obj(v: Json) -> JsonObj:
    return v if isinstance(v, dict) else {}


def _ints(v: Json) -> List[int]:
    return [x for x in v if isinstance(x, int)] if isinstance(v, list) else []


@dataclass(frozen=True)
class Location:
    file: str
    line: int
    col: int

    def as_json(self) -> JsonObj:
        return {"file": self.file, "line": self.line, "col": self.col}


class DebugInfo:
    def __init__(self, doc: JsonObj) -> None:
        self.major = doc.get("major") if isinstance(doc.get("major"), int) else None
        self.minor = doc.get("minor") if isinstance(doc.get("minor"), int) else None
        build = _obj(doc.get("build"))
        self.compiler = build.get("compiler") if isinstance(build.get("compiler"), str) else None
        self.build_id = build.get("buildId") if isinstance(build.get("buildId"), str) else None
        self.files: Dict[str, int] = {}
        self.paths: Dict[int, str] = {}
        for f in doc.get("files") or []:
            f_obj = _obj(f)
            fid, path = f_obj.get("id"), f_obj.get("path")
            if isinstance(fid, int) and isinstance(path, str):
                self.files[path] = fid
                self.paths[fid] = path
        probes = _obj(doc.get("probes"))
        self.p_file = _ints(probes.get("file"))
        self.p_line = _ints(probes.get("line"))
        self.p_col = _ints(probes.get("col"))
        self.lines: Dict[int, List[int]] = {}
        for key, lines in _obj(doc.get("lines")).items():
            if key.isdigit():
                self.lines[int(key)] = sorted(_ints(lines))

    @classmethod
    def load(cls, path: Path) -> Optional["DebugInfo"]:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return cls(doc if isinstance(doc, dict) else {})

    def version(self) -> Optional[JsonObj]:
        if self.major is None:
            return None
        return {"major": self.major, "minor": self.minor}

    def check_supported(self) -> None:
        if self.major != SUPPORTED_MAJOR:
            raise Refusal(
                ErrorCode.UNSUPPORTED_DEBUG_INFO,
                f"The program's debug information is major version {self.major}; this runtime "
                f"reads major {SUPPORTED_MAJOR}.",
                {"found": self.major, "supported": SUPPORTED_MAJOR},
            )

    def file_id(self, path: str) -> int:
        fid = self.files.get(path)
        if fid is None:
            raise Refusal(
                ErrorCode.INVALID_LOCATION,
                f"The running program has no source file named '{path}'.",
                {"file": path},
            )
        return fid

    def resolve_line(self, path: str, line: int) -> Tuple[int, int]:
        """The first probe on the first probe-bearing line at or after `line`: (probe, line)."""
        fid = self.file_id(path)
        lines = self.lines.get(fid, [])
        i = bisect.bisect_left(lines, line)
        if i == len(lines):
            raise Refusal(
                ErrorCode.NO_CODE,
                f"There is no executable statement at or after line {line} of '{path}'.",
                {"file": path, "line": line},
            )
        actual = lines[i]
        candidates = [
            (self.p_col[p] if p < len(self.p_col) else 0, p)
            for p in range(min(len(self.p_file), len(self.p_line)))
            if self.p_file[p] == fid and self.p_line[p] == actual
        ]
        if not candidates:
            raise Refusal(ErrorCode.NO_CODE, f"Line {actual} of '{path}' has no probe.")
        return min(candidates)[1], actual

    def lines_between(self, path: str, first: int, last: int) -> List[int]:
        fid = self.file_id(path)
        return [ln for ln in self.lines.get(fid, []) if first <= ln <= last]

    def location(self, probe: int) -> Optional[Location]:
        if not 0 <= probe < min(len(self.p_file), len(self.p_line)):
            return None
        path = self.paths.get(self.p_file[probe])
        if path is None:
            return None
        col = self.p_col[probe] if probe < len(self.p_col) else 0
        return Location(path, self.p_line[probe], col)
