# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""Loads the program's `debug-info.json` (ADR-002 D3) and resolves source lines to probe IDs and
probe IDs back to source locations, the gateway's half of symbolic resolution (ADR-003 D1.3).
It also resolves variable paths to debug-table leaves and decodes their bytes (ADR-002 D3
"Addressing"), and maps a core frame to its POU and instance.

WHY leaves are counted here: `debug-info.json` gives each instance's first leaf (`leafBase`) and
each type's members, not each member's leaf, so a member's leaf is its instance's `leafBase` plus
the leaves of the members before it, in STruCpp's pass-1 walk order (`debug-table-gen.ts`). The
count is checked against every instance the table lists, and every value read is checked against
its type's byte size, so a walk that drifts from STruCpp's is refused rather than shown.

See also:
  webserver/debug_gateway/gateway.py - uses it for setBreakpoints, runToCursor, stop events,
    stackTrace, scopes and variables.
  docs/DEBUG_CORE_LINK.md - readLeaves and stackTrace, the core's half.
  docs/DEBUG_PROTOCOL.md - "Session protocol v1", the location fields in requests and events.
"""

from __future__ import annotations

import bisect
import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from webserver.debug_gateway.protocol import ErrorCode, Json, JsonObj, Refusal

SUPPORTED_MAJOR = 1


def _obj(v: Json) -> JsonObj:
    return v if isinstance(v, dict) else {}


def _ints(v: Json) -> List[int]:
    return [x for x in v if isinstance(x, int)] if isinstance(v, list) else []


def _frame_leaves(v: Json) -> Tuple[Tuple[str, str, int], ...]:
    """A POU's `__dbg_locals` table as (path, type, size) per slot, in slot order. The three
    arrays are parallel and of one length in every build STruCpp emits; a table the gateway cannot
    line up is read as no table at all rather than as the wrong bytes."""
    leaves = _obj(v)
    paths, types, sizes = leaves.get("path"), leaves.get("type"), leaves.get("size")
    if not (isinstance(paths, list) and isinstance(types, list) and isinstance(sizes, list)):
        return ()
    if not len(paths) == len(types) == len(sizes):
        return ()
    return tuple(
        (str(p), str(t), s) for p, t, s in zip(paths, types, sizes) if isinstance(s, int)
    )


@dataclass(frozen=True)
class Location:
    file: str
    line: int
    col: int

    def as_json(self) -> JsonObj:
        return {"file": self.file, "line": self.line, "col": self.col}


# Elementary IEC types: struct format of the bytes strucpp_debug_read writes (little-endian).
_FORMATS: Dict[str, str] = {
    "BOOL": "<B", "SINT": "<b", "USINT": "<B", "BYTE": "<B",
    "INT": "<h", "UINT": "<H", "WORD": "<H",
    "DINT": "<i", "UDINT": "<I", "DWORD": "<I",
    "LINT": "<q", "ULINT": "<Q", "LWORD": "<Q", "__XWORD": "<Q",
    "REAL": "<f", "LREAL": "<d",
    "TIME": "<q", "LTIME": "<q", "DATE": "<q", "LDATE": "<q", "TOD": "<q", "TIME_OF_DAY": "<q",
    "LTOD": "<q", "DT": "<q", "DATE_AND_TIME": "<q", "LDT": "<q",
}
# STRING/WSTRING leaves are fixed windows: a length byte, then 126 UTF-8 bytes or 252 UTF-16LE.
_STRING_SIZES = {"STRING": 127, "WSTRING": 253}
# The range each scalar struct format admits, for an RANGE_ERROR that names the constraint.
_FORMAT_BOUNDS: Dict[str, Tuple[int, int]] = {
    "<b": (-128, 127), "<B": (0, 255), "<h": (-32768, 32767), "<H": (0, 65535),
    "<i": (-2147483648, 2147483647), "<I": (0, 4294967295),
    "<q": (-9223372036854775808, 9223372036854775807), "<Q": (0, 18446744073709551615),
}
# The only string capacity the debug table registers (debug-table-gen.ts DISPATCH_STRING_CAPACITY).
_DISPATCH_STRING_CAPACITY = 254
# Scope names by declaration section (ADR-002 F074).
SCOPE_OF_SECTION = {
    "VAR_INPUT": "inputs", "VAR_OUTPUT": "outputs", "VAR_IN_OUT": "inOuts", "VAR": "members",
    "VAR_TEMP": "temps", "VAR_EXTERNAL": "externals",
}
# Sections the pass-1 leaf walk covers in an FB, and in a program (no VAR_IN_OUT there).
_FB_SECTIONS = ("VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT")
_PROGRAM_SECTIONS = ("VAR", "VAR_INPUT", "VAR_OUTPUT")
_SUBSCRIPT = re.compile(r"\[([^\]]*)\]")


@dataclass
class TypeInfo:
    id: int
    kind: str
    name: Optional[str]
    members: List[JsonObj] = field(default_factory=list)
    dims: List[Tuple[int, int]] = field(default_factory=list)
    elem: Optional[int] = None
    values: List[Tuple[str, int]] = field(default_factory=list)
    base: Optional[int] = None
    length: Optional[int] = None


def _frame_step(base: str, path: str) -> Optional[str]:
    """The one-level name `path` is under `base`, a frame variable's path in the `__dbg_locals`
    table: the token up to the next `.` or `[`, kept as `[i]` for a subscript. None when `path` is
    not under `base`. Used to group a variable's leaves into its immediate children: the walk is a
    depth-first pre-order, so the leaves of one child are consecutive."""
    if not path.startswith(base):
        return None
    m = re.match(r"\.([^\.\[]+)|\[([^\[\]]*)\]", path[len(base):])
    if m is None:
        return None
    return m.group(1) if m.group(1) is not None else f"[{m.group(2)}]"


@dataclass(frozen=True)
class Pou:
    id: int
    name: str
    kind: str
    entry_probe: int
    type_id: Optional[int]
    # The POU's source span, (first line, first column, last line, last column), and the file it
    # is in. A breakpoint never moves across a POU boundary (F026), so the span is what says
    # which statements a line may fall forward to.
    span: Tuple[int, int, int, int] = (0, 0, 0, 0)
    file_id: Optional[int] = None
    # (name, typeId, section, localsSlot): a frame variable, and the slot its first leaf takes in
    # the frame's `__dbg_locals` table (None when the variable has no readable leaf).
    locals: Tuple[Tuple[str, Optional[int], str, Optional[int]], ...]
    # The frame's `__dbg_locals` table, index = slot: (leaf path, IEC type name, byte size).
    # Empty when the POU's body declares no frame variable.
    frame_leaves: Tuple[Tuple[str, str, int], ...]


@dataclass(frozen=True)
class Node:
    """A resolved variable: its display path, type and first leaf. `program` marks a program
    instance's root, whose leaf walk omits VAR_IN_OUT. `frame` marks a leaf of a probed body's
    `__dbg_locals` table (a function's inputs and temps, an FB's or program's VAR_TEMP), which
    lives on the halted task's stack: it is (POU id, slot) and `leaf` carries the slot, not a flat
    leaf index, because those leaves are in no static table."""

    path: str
    type_id: int
    leaf: int
    program: bool = False
    pou: Optional[int] = None  # the POU whose VAR_EXTERNALs are reachable from this node
    frame: Optional[Tuple[int, int]] = None  # (pou id, __dbg_locals slot)
    frame_path: Optional[str] = None  # the frame variable this node belongs to, in table paths


class DebugInfo:
    def __init__(self, doc: JsonObj) -> None:
        self.major = doc.get("major") if isinstance(doc.get("major"), int) else None
        self.minor = doc.get("minor") if isinstance(doc.get("minor"), int) else None
        build = _obj(doc.get("build"))
        self.compiler = build.get("compiler") if isinstance(build.get("compiler"), str) else None
        self.build_id = build.get("buildId") if isinstance(build.get("buildId"), str) else None
        self.md5 = build.get("md5") if isinstance(build.get("md5"), str) else None
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
        self.p_pou = _ints(probes.get("pou"))
        self.lines: Dict[int, List[int]] = {}
        for key, lines in _obj(doc.get("lines")).items():
            if key.isdigit():
                self.lines[int(key)] = sorted(_ints(lines))
        self.types: Dict[int, TypeInfo] = {}
        for t in doc.get("types") or []:
            t_obj = _obj(t)
            tid = t_obj.get("id")
            if not isinstance(tid, int):
                continue
            name = t_obj.get("name")
            self.types[tid] = TypeInfo(
                tid,
                str(t_obj.get("kind") or ""),
                name if isinstance(name, str) else None,
                [_obj(m) for m in t_obj.get("members") or []],
                [(d[0], d[1]) for d in t_obj.get("dims") or [] if isinstance(d, list) and len(d) == 2],
                t_obj.get("elem") if isinstance(t_obj.get("elem"), int) else None,
                [(str(v[0]), int(v[1])) for v in t_obj.get("values") or [] if isinstance(v, list) and len(v) == 2],
                t_obj.get("base") if isinstance(t_obj.get("base"), int) else None,
                t_obj.get("length") if isinstance(t_obj.get("length"), int) else None,
            )
        self.pous: Dict[int, Pou] = {}
        self.pou_by_entry: Dict[int, Pou] = {}
        for p in doc.get("pous") or []:
            p_obj = _obj(p)
            if not isinstance(p_obj.get("id"), int) or not isinstance(p_obj.get("entryProbe"), int):
                continue
            span = p_obj.get("span")
            pou = Pou(
                p_obj["id"], str(p_obj.get("name")), str(p_obj.get("kind")), p_obj["entryProbe"],
                p_obj.get("typeId") if isinstance(p_obj.get("typeId"), int) else None,
                tuple(
                    (str(_obj(v).get("name")), _obj(v).get("typeId") if isinstance(_obj(v).get("typeId"), int) else None,
                     str(_obj(v).get("kind")),
                     _obj(v).get("localsSlot") if isinstance(_obj(v).get("localsSlot"), int) else None)
                    for v in p_obj.get("locals") or []
                ),
                _frame_leaves(p_obj.get("frameLeaves")),
                tuple(int(v) for v in span) if isinstance(span, list) and len(span) == 4 else (0, 0, 0, 0),
                p_obj.get("file") if isinstance(p_obj.get("file"), int) else None,
            )
            self.pous[pou.id] = pou
            self.pou_by_entry[pou.entry_probe] = pou
        inst = _obj(doc.get("instances"))
        paths = inst.get("path") if isinstance(inst.get("path"), list) else []
        self.instances: List[Tuple[str, int, int]] = [
            (str(path), tid, base)
            for path, tid, base in zip(paths, inst.get("typeId") or [], inst.get("leafBase") or [])
            if isinstance(tid, int) and isinstance(base, int)
        ]
        self.globals: Dict[str, Tuple[str, int, int]] = {}
        for g in doc.get("globals") or []:
            g_obj = _obj(g)
            if isinstance(g_obj.get("typeId"), int) and isinstance(g_obj.get("leafBase"), int):
                name = str(g_obj.get("name"))
                self.globals[name.upper()] = (name, g_obj["typeId"], g_obj["leafBase"])
        self.externals: Dict[int, List[str]] = {}
        for e in doc.get("externals") or []:
            e_obj = _obj(e)
            if isinstance(e_obj.get("pou"), int) and isinstance(e_obj.get("name"), str):
                self.externals.setdefault(e_obj["pou"], []).append(e_obj["name"])
        self._leaf_counts: Dict[Tuple[int, bool], int] = {}
        # flat leaf index -> its debug-information path, built on demand by path_of_leaf.
        self._leaf_paths: Dict[int, str] = {}

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
        """The first probe on the first probe-bearing line at or after `line`: (probe, line).

        The search never crosses a POU boundary (F026): a line that is not a statement falls
        forward to the next statement of the POU it is in, and a POU with no statement after it
        has no code to bind to.
        """
        fid = self.file_id(path)
        lines = self.lines.get(fid, [])
        pou = self.pou_at(fid, line)
        if pou is None:
            raise Refusal(
                ErrorCode.NO_CODE,
                f"Line {line} of '{path}' is not in a POU, so it has no code to bind to.",
                {"file": path, "line": line},
            )
        last = pou.span[2] if pou.span[2] >= pou.span[0] else None
        ahead = [ln for ln in lines if ln >= line and (last is None or ln <= last)]
        if not ahead:
            raise Refusal(
                ErrorCode.NO_CODE,
                f"There is no executable statement at or after line {line} of '{path}'.",
                {"file": path, "line": line},
            )
        actual = ahead[0]
        candidates = [
            (self.p_col[p] if p < len(self.p_col) else 0, p)
            for p in range(min(len(self.p_file), len(self.p_line)))
            if self.p_file[p] == fid and self.p_line[p] == actual
        ]
        if not candidates:
            raise Refusal(ErrorCode.NO_CODE, f"Line {actual} of '{path}' has no probe.")
        return min(candidates)[1], actual

    def pou_at(self, fid: int, line: int) -> Optional[Pou]:
        """The POU of `fid` whose source span holds `line`, or None."""
        for pou in self.pous.values():
            if pou.file_id is not None and pou.file_id != fid:
                continue
            if pou.span[0] <= line <= pou.span[2]:
                return pou
        return None

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

    # ------------------------------------------------------------------ frames and variables

    def pou_of_type(self, type_id: int) -> Optional[Pou]:
        return next((p for p in self.pous.values() if p.type_id == type_id), None)

    def instance_of(self, leaf: Optional[int], pou: Pou) -> Optional[str]:
        """The instance of `pou` whose `this` the core mapped to `leaf` (DEBUG_CORE_LINK.md,
        "Interim instance binding"): the same type, the greatest leafBase at or below the leaf."""
        if leaf is None or pou.type_id is None:
            return None
        best: Optional[Tuple[int, str]] = None
        for path, tid, base in self.instances:
            if tid == pou.type_id and base <= leaf and (best is None or base > best[0]):
                best = (base, path)
        return best[1] if best else None

    def leaf_count(self, type_id: int, program: bool = False) -> int:
        """Leaves of one value of the type in STruCpp's pass-1 walk."""
        key = (type_id, program)
        if key in self._leaf_counts:
            return self._leaf_counts[key]
        self._leaf_counts[key] = 0  # a recursive type has no leaves rather than no end
        t = self.types.get(type_id)
        n = 0
        if t is None:
            n = 0
        elif t.kind == "elementary":
            n = 1 if (t.name or "").upper() in _FORMATS or (t.name or "").upper() in _STRING_SIZES else 0
        elif t.kind == "string":
            n = 1 if t.length in (None, _DISPATCH_STRING_CAPACITY) else 0
        elif t.kind in ("enum", "subrange"):
            n = 1
        elif t.kind in ("struct", "fb"):
            sections = _PROGRAM_SECTIONS if program else _FB_SECTIONS
            for m in t.members:
                if t.kind == "fb" and m.get("section") not in sections:
                    continue
                if isinstance(m.get("typeId"), int):
                    n += self.leaf_count(m["typeId"])
        elif t.kind == "array" and t.elem is not None:
            count = 1
            for lo, hi in t.dims:
                count *= max(0, hi - lo + 1)
            n = count * self.leaf_count(t.elem)
        self._leaf_counts[key] = n
        return n

    def type_name(self, type_id: Optional[int]) -> str:
        t = self.types.get(type_id) if type_id is not None else None
        if t is None:
            return "?"
        if t.kind == "array":
            dims = ", ".join(f"{lo}..{hi}" for lo, hi in t.dims)
            return f"ARRAY[{dims}] OF {self.type_name(t.elem)}"
        if t.kind == "string" and t.length is not None:
            return f"STRING({t.length})"
        name = t.name
        # A declared name, not the symbol table's: IEC identifiers are case-insensitive, and a
        # type shown as `Seg` where it was declared `Seg` reads as a different type to whoever
        # wrote the program. Elementary types have no declaration to spell, so they stay as the
        # symbol table writes them (`INT`, `DINT`, `STRING`).
        if name and t.kind in ("struct", "fb", "enum"):
            for pou in self.pous.values():
                for declared in getattr(pou, "declared", ()) or ():
                    if str(declared).casefold() == name.casefold():
                        return str(declared)
        return name or t.kind

    def type_detail(self, type_id: Optional[int]) -> JsonObj:
        """The structural facts `type_name` folds into its string, structured (E45 ruling 2).

        `type` stays a display string, as DAP's `Variable.type` is; a client that has to compute
        from the structure — an editor laying out an array, an enum picker — asks for these with
        `types: true` rather than parsing the string back apart. Nothing is added for a type with
        no structure to describe, so an elementary variable answers exactly as it did before.
        """
        t = self.types.get(type_id) if type_id is not None else None
        if t is None:
            return {}
        out: JsonObj = {}
        if t.dims:
            out["dims"] = [[lo, hi] for lo, hi in t.dims]
        if t.kind == "enum" and t.values:
            out["values"] = [[name, value] for name, value in t.values]
        elem = self.types.get(t.elem) if t.elem is not None else None
        if elem is not None and (elem.members or elem.values):
            detail: JsonObj = {"name": self.type_name(elem.id)}
            if elem.members:
                detail["members"] = [
                    {"name": str(m.get("name")), "type": self.type_name(m.get("typeId"))}
                    for m in elem.members
                ]
            if elem.values:
                detail["values"] = [[name, value] for name, value in elem.values]
            out["elementType"] = detail
        return out

    def _member_leaf(self, node: Node, name: str) -> Optional[Node]:
        t = self.types.get(node.type_id)
        if t is None or t.kind not in ("struct", "fb"):
            return None
        sections = _PROGRAM_SECTIONS if node.program else _FB_SECTIONS
        leaf = node.leaf
        for m in t.members:
            tid = m.get("typeId")
            if t.kind == "fb" and m.get("section") not in sections:
                continue
            if not isinstance(tid, int):
                continue
            if str(m.get("name")).upper() == name.upper():
                path = f"{node.path}.{m.get('name')}"
                sub_pou = self.pou_of_type(tid) if self.types.get(tid, TypeInfo(0, "", None)).kind == "fb" else None
                return Node(path, tid, self._instance_leaf(path, leaf), pou=sub_pou.id if sub_pou else None)
            leaf += self.leaf_count(tid)
        return None

    def _instance_leaf(self, path: str, computed: int) -> int:
        """The instance table's leafBase where it lists the path; it must agree with the walk."""
        for p, _tid, base in self.instances:
            if p.upper() == path.upper():
                if base != computed:
                    raise Refusal(
                        ErrorCode.INTERNAL,
                        f"The debug information's leaf count for '{path}' ({computed}) disagrees "
                        f"with its instance table ({base}).",
                    )
                return base
        return computed

    def frame_local(self, pou: Optional[Pou], tokens: List[str]) -> Optional[Node]:
        """The frame variable `tokens[0]` names in `pou`, or None: a function's VAR_INPUT /
        VAR_IN_OUT / VAR_TEMP, an FB's or program's VAR_TEMP. These live on the halted task's
        stack, not in the program's static debug table, so they resolve to a slot of the frame's
        `__dbg_locals` rather than to a flat leaf index. The frame's own variables come first: a
        local shadows the globals and the instances the same path would otherwise reach."""
        if pou is None or not tokens:
            return None
        for name, type_id, _section, slot in pou.locals:
            if slot is None or name.upper() != tokens[0].upper():
                continue
            return Node(f"{pou.name}.{name}", type_id if type_id is not None else -1, slot,
                        frame=(pou.id, slot), frame_path=name)
        return None

    def frame_children(self, node: Node) -> List[Tuple[str, Node]]:
        """The immediate children of a frame variable or leaf, one level (A089). The table is a
        depth-first walk of one frame variable at a time, so a variable's leaves are the slots
        from its own first slot to the next variable's, and a child's leaves are consecutive:
        group them by the one-level name each leaf sits under. The names and types come from the
        static type walk, as everywhere else; only the slot is the frame's."""
        if node.frame is None or node.frame_path is None:
            return []
        pou = self.pous.get(node.frame[0])
        if pou is None:
            return []
        base = node.frame_path
        end = self._frame_var_end(pou, node.frame[1])
        out: List[Tuple[str, Node]] = []
        seen: Optional[str] = None
        # Inclusive of the node's own slot: a composite's first slot is already its first child,
        # and an elementary's is `_frame_step`'s own path, which names no child.
        for slot in range(node.frame[1], end):
            name = _frame_step(base, pou.frame_leaves[slot][0])
            if name is None:
                break  # past this variable's leaves, or out of the table
            if name == seen:
                continue  # another leaf of the child already listed
            seen = name
            sep = "" if name.startswith("[") else "."
            child_path = f"{base}{sep}{name}"
            out.append((name, Node(child_path, self._frame_child_type(node.type_id, name), slot,
                                   frame=(pou.id, slot), frame_path=child_path)))
        return out

    @staticmethod
    def _frame_var_end(pou: Pou, slot: int) -> int:
        """Where the frame variable that starts at or before `slot` ends: the next variable's
        first slot, or the end of the table. Its leaves are the slots in between."""
        starts = [s for _n, _t, _k, s in pou.locals if s is not None and s > slot]
        return min(starts) if starts else len(pou.frame_leaves)

    def _frame_child_type(self, type_id: int, name: str) -> int:
        """A frame leaf's owner's type for one child: an array element's element type, else the
        member's. -1 (no type) where the static walk has none, which reads as `?`, never as a
        wrong type."""
        t = self.types.get(type_id)
        if t is None:
            return -1
        if name.startswith("["):
            return t.elem if t.kind == "array" and t.elem is not None else -1
        for m in t.members:
            if str(m.get("name")).upper() == name.upper() and isinstance(m.get("typeId"), int):
                return int(m["typeId"])
        return -1

    def resolve(self, path: str, frame_instance: Optional[str] = None,
                frame_pou: Optional[Pou] = None) -> Node:
        """A variable path (`PROG0.motor1.pid.out`, `G_SEG.a.x`, `PROG0.arr[4]`; case-insensitive),
        or a name relative to a frame's own variables, its instance, or its POU's externals."""
        tokens = self._tokens(path)
        if not tokens:
            raise Refusal(ErrorCode.BAD_REQUEST, "A variable path must not be empty.")
        node = self.frame_local(frame_pou, tokens)
        if node is None:
            node = self._root(tokens[0])
            if node is None and frame_instance is not None:
                node = self._root(frame_instance.split(".")[0])
                if node is not None:
                    tokens = self._tokens(frame_instance) + tokens
            if node is None and frame_pou is not None:
                for ext in self.externals.get(frame_pou.id, []):
                    if ext.upper() == tokens[0].upper():
                        node = self._root(ext)
            if node is None:
                raise self._no_such(path)
        for tok in tokens[1:]:
            nxt = self._step(node, tok)
            if nxt is None:
                raise self._no_such(path)
            node = nxt
        return node

    @staticmethod
    def _tokens(path: str) -> List[str]:
        out: List[str] = []
        for part in path.split("."):
            m = re.match(r"^([^\[]*)((?:\[[^\]]*\])*)$", part.strip())
            if m is None:
                raise Refusal(ErrorCode.BAD_REQUEST, f"'{path}' is not a variable path.")
            if m.group(1):
                out.append(m.group(1))
            out.extend(f"[{s}]" for s in _SUBSCRIPT.findall(m.group(2)))
        return out

    def _no_such(self, path: str) -> Refusal:
        return Refusal(ErrorCode.INVALID_LOCATION, f"The running program has no variable '{path}'.",
                       {"path": path})

    def _root(self, name: str) -> Optional[Node]:
        for path, tid, base in self.instances:
            if "." not in path and path.upper() == name.upper():
                pou = self.pou_of_type(tid)
                return Node(path, tid, base, program=True, pou=pou.id if pou else None)
        g = self.globals.get(name.upper())
        if g is not None:
            return Node(g[0], g[1], g[2])
        return None

    def _step(self, node: Node, tok: str) -> Optional[Node]:
        if node.frame is not None:
            for name, child in self.frame_children(node):
                if name.upper() == tok.upper():
                    return child
            return None
        t = self.types.get(node.type_id)
        if t is None:
            return None
        if tok.startswith("["):
            if t.kind != "array" or t.elem is None:
                return None
            try:
                idx = [int(x) for x in tok[1:-1].split(",")]
            except ValueError:
                return None
            if len(idx) != len(t.dims):
                return None
            flat = 0
            for i, (lo, hi) in zip(idx, t.dims):
                if not lo <= i <= hi:
                    return None
                flat = flat * (hi - lo + 1) + (i - lo)
            return Node(f"{node.path}{tok}", t.elem, node.leaf + flat * self.leaf_count(t.elem))
        found = self._member_leaf(node, tok)
        if found is None and node.pou is not None:
            for ext in self.externals.get(node.pou, []):
                if ext.upper() == tok.upper():
                    found = self._root(ext)
        return found

    def children(self, node: Node) -> List[Tuple[str, Node]]:
        """The immediate children of a composite value, one level (A089)."""
        if node.frame is not None:
            return self.frame_children(node)
        t = self.types.get(node.type_id)
        if t is None:
            return []
        out: List[Tuple[str, Node]] = []
        if t.kind == "array" and t.elem is not None:
            ranges = [list(range(lo, hi + 1)) for lo, hi in t.dims]
            combos: List[List[int]] = [[]]
            for r in ranges:
                combos = [c + [i] for c in combos for i in r]
            for c in combos:
                tok = "[" + ",".join(str(i) for i in c) + "]"
                child = self._step(node, tok)
                if child is not None:
                    out.append((tok, child))
            return out
        if t.kind in ("struct", "fb"):
            for m in t.members:
                if t.kind == "fb" and m.get("section") not in (_PROGRAM_SECTIONS if node.program else _FB_SECTIONS):
                    continue
                child = self._member_leaf(node, str(m.get("name")))
                if child is not None:
                    out.append((str(m.get("name")), child))
            for ext in self.externals.get(node.pou, []) if node.pou is not None else []:
                g = self._root(ext)
                if g is not None:
                    out.append((ext, g))
        return out

    def is_elementary(self, type_id: int) -> bool:
        t = self.types.get(type_id)
        return t is not None and t.kind in ("elementary", "enum", "subrange", "string")

    def decode(self, type_id: int, hex_value: Optional[str], leaf: int) -> Json:
        """A leaf's bytes as a JSON value; an enum as its name. Refuses a size that is not the
        type's: the debug information then does not describe the running program."""
        t = self.types.get(type_id)
        if t is None or hex_value is None:
            return None
        if t.kind in ("enum", "subrange") and t.base is not None:
            raw = self.decode(t.base, hex_value, leaf)
            if t.kind == "enum":
                return next((n for n, v in t.values if v == raw), raw)
            return raw
        name = (t.name or "").upper() if t.kind == "elementary" else "STRING"
        data = bytes.fromhex(hex_value)
        if name in _STRING_SIZES:
            if len(data) != _STRING_SIZES[name] or not data:
                raise self._size_mismatch(leaf, name, len(data))
            n = data[0]
            body = data[1:]
            if name == "STRING":
                return body[:n].decode("utf-8", errors="replace")
            return body[: 2 * n].decode("utf-16-le", errors="replace")
        fmt = _FORMATS.get(name)
        if fmt is None:
            return None
        if len(data) != struct.calcsize(fmt):
            raise self._size_mismatch(leaf, name, len(data))
        value = struct.unpack(fmt, data)[0]
        if name == "BOOL":
            return value != 0
        return value

    @staticmethod
    def _size_mismatch(leaf: int, name: str, got: int) -> Refusal:
        return Refusal(
            ErrorCode.STALE_BUILD,
            f"Leaf {leaf} of the running program is {got} bytes, not a {name}: the debug "
            f"information does not describe the running program.",
            {"reason": "debugInfo", "leaf": leaf},
        )

    # -- writing a leaf's bytes (ADR-005 D5: a force carries a value)

    def encode(self, type_id: int, value: Json, leaf: int) -> str:
        """A JSON value as the lowercase hex of the bytes a force pins the leaf to: the same
        bytes `decode` reads, so the round trip is exact and the core's `validate_payload`
        (STruCpp) sees a length it accepts.

        A refusal is `TYPE_ERROR` for a value the variable's type cannot hold and `RANGE_ERROR`
        for one of the right type outside its range, naming the constraint, as
        docs/DEBUG_PROTOCOL.md's two codes require."""
        t = self.types.get(type_id)
        if t is None:
            raise Refusal(
                ErrorCode.STALE_BUILD, f"Leaf {leaf} has no type in the debug information."
            )
        if t.kind in ("enum", "subrange") and t.base is not None:
            # An enum's display value is its label; its stored value is the base type's.
            if isinstance(value, str):
                named = next((v for n, v in t.values if n.casefold() == value.casefold()), None)
                if named is None:
                    raise Refusal(
                        ErrorCode.RANGE_ERROR,
                        f"Leaf {leaf} is a {self.type_name(type_id)}; {value!r} is not one of its "
                        f"labels ({', '.join(n for n, _ in t.values)}).",
                        {"leaf": leaf, "type": self.type_name(type_id)},
                    )
                return self.encode(t.base, named, leaf)
            return self.encode(t.base, value, leaf)
        name = (t.name or "").upper() if t.kind == "elementary" else "STRING"
        if name in _STRING_SIZES:
            width = _STRING_SIZES[name]
            unit = 2 if name == "WSTRING" else 1
            max_chars = (width - 1) // unit
            if not isinstance(value, str):
                raise Refusal(
                    ErrorCode.TYPE_ERROR,
                    f"Leaf {leaf} is a {name}, which takes text, not {value!r}.",
                    {"leaf": leaf, "type": name},
                )
            try:
                body = value.encode("utf-16-le" if name == "WSTRING" else "utf-8")
            except UnicodeEncodeError as exc:
                raise Refusal(
                    ErrorCode.RANGE_ERROR,
                    f"Leaf {leaf} is a {name} of at most {max_chars} characters, and this text "
                    "does not fit it.",
                    {"leaf": leaf, "maxChars": max_chars},
                ) from exc
            count = len(body) // unit
            if count > max_chars:
                raise Refusal(
                    ErrorCode.RANGE_ERROR,
                    f"Leaf {leaf} is a {name} of at most {max_chars} characters; this text "
                    f"needs {count}.",
                    {"leaf": leaf, "maxChars": max_chars},
                )
            return (bytes([count]) + body).ljust(width, b"\x00").hex()
        fmt = _FORMATS.get(name)
        if fmt is None:
            raise Refusal(
                ErrorCode.TYPE_ERROR,
                f"Leaf {leaf} is a {self.type_name(type_id)}, which this build cannot write.",
                {"leaf": leaf, "type": self.type_name(type_id)},
            )
        if name == "BOOL":
            if isinstance(value, bool):
                number = 1 if value else 0
            elif isinstance(value, int):
                number = value
            else:
                raise Refusal(
                    ErrorCode.TYPE_ERROR,
                    f"Leaf {leaf} is a BOOL, which takes true or false, not {value!r}.",
                    {"leaf": leaf, "type": "BOOL"},
                )
            if number not in (0, 1):
                raise Refusal(
                    ErrorCode.RANGE_ERROR,
                    f"Leaf {leaf} is a BOOL; {value!r} is neither FALSE (0) nor TRUE (1).",
                    {"leaf": leaf, "constraint": "FALSE..TRUE"},
                )
            return struct.pack("<B", number).hex()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Refusal(
                ErrorCode.TYPE_ERROR,
                f"Leaf {leaf} is a {name}, which takes a number, not {value!r}.",
                {"leaf": leaf, "type": name},
            )
        if fmt not in ("<f", "<d") and isinstance(value, float) and not float(value).is_integer():
            raise Refusal(
                ErrorCode.RANGE_ERROR,
                f"Leaf {leaf} is a {name}, an integer type; {value!r} is not a whole number.",
                {"leaf": leaf, "type": name, "constraint": "an integer"},
            )
        try:
            packed = struct.pack(fmt, value)
        except (struct.error, OverflowError, ValueError) as exc:
            lo, hi = _FORMAT_BOUNDS[fmt]
            raise Refusal(
                ErrorCode.RANGE_ERROR,
                f"Leaf {leaf} is a {name}; {value!r} is outside its range {lo} to {hi}.",
                {"leaf": leaf, "type": name, "min": lo, "max": hi},
            ) from exc
        return packed.hex()

    def path_of_leaf(self, leaf: int) -> Optional[str]:
        """The debug information's own path for a flat leaf index, for a report that names the
        variable a force is on rather than the leaf the core holds. The walk is the reverse of
        `resolve`: every instance and global, depth-first in the same order `children` lists
        them, so the paths are the ones a client would have written."""
        if leaf < 0:
            return None
        cached = self._leaf_paths.get(leaf)
        if cached is not None:
            return cached
        roots: List[Node] = [
            Node(path, tid, base, program=True,
                 pou=(pou.id if (pou := self.pou_of_type(tid)) else None))
            for path, tid, base in self.instances
        ]
        roots += [Node(g[0], g[1], g[2]) for g in self.globals.values()]
        for root in roots:
            self._walk_paths(root, leaf)
            if leaf in self._leaf_paths:
                break
        return self._leaf_paths.get(leaf)

    def _walk_paths(self, node: Node, leaf: int) -> None:
        # A frame node's leaves are `__dbg_locals` slots, not flat leaves, so its children
        # would answer with a slot index that collides with a static leaf.
        if node.frame is not None:
            return
        for _name, child in self.children(node):
            if self.is_elementary(child.type_id):
                if child.leaf == leaf:
                    self._leaf_paths[leaf] = child.path
                    return
                continue
            # A composite's own `.leaf` is its first member's, so it never names a variable
            # that can be forced; the path wanted is somewhere inside it.
            self._walk_paths(child, leaf)
            if leaf in self._leaf_paths:
                return
