#!/usr/bin/env python3
"""Validate the debug corpus: every program header and every oracle scenario.

Checks, in order:
  1. Each oracle against oracle.schema.json (a stdlib JSON Schema 2020-12 subset;
     a schema keyword outside the subset is an error, never silently ignored).
  2. Oracle semantics the schema cannot express: scenario id and corpus match the
     path, criteria are cited by the program header, `@MARKER` lines resolve once
     in the program sources, files and tasks exist, every `~ref` has an earlier
     `~capture`, and a runtime scenario reads the plant at least once.
  3. Program headers: corpus name, criteria, FC tags, and the scan witness
     contract (one per task, first statement of the task's first program).
  4. Optional cross-checks: --acceptance (header criteria equal the criteria that
     cite the corpus) and --catalog (every FC tag exists).

Usage: validate.py [--acceptance ACCEPTANCE.md] [--catalog feature-catalog.md] [ORACLE ...]
Exit status 0 when everything validates, 1 otherwise; one `path: message` per problem.

See also:
  tests/debug/corpus/README.md - the oracle format and the program header contract.
  tests/debug/corpus/oracle.schema.json - the schema checked in step 1.
  tests/debug/corpus/test_validate.py - red/green proof that this validator rejects defects.
  tests/debug/corpus/build_all.sh - compiles every program, then runs this script.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple, Union

Json = Union[None, bool, int, float, str, List["Json"], Dict[str, "Json"]]

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "oracle.schema.json"
ORACLE_GLOB = "*.oracle.json"
FAMILY_FILE = "family.txt"
MAIN_SOURCE = "program.st"

SUPPORTED_KEYWORDS = {
    "$schema", "$id", "$ref", "$defs", "title", "description", "type", "enum", "const",
    "required", "properties", "additionalProperties", "propertyNames", "items", "minItems",
    "maxItems", "minProperties", "maxProperties", "pattern", "minLength", "minimum", "oneOf",
}


# ---------------------------------------------------------------- JSON Schema subset


def _type_ok(value: Json, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    raise ValueError(f"unknown schema type {name!r}")


class Schema:
    """A JSON Schema 2020-12 subset sufficient for oracle.schema.json."""

    def __init__(self, document: Dict[str, Json]) -> None:
        self.document = document
        self._check_keywords(document, "#")

    def _check_keywords(self, node: Json, where: str) -> None:
        if not isinstance(node, dict):
            return
        for key, sub in node.items():
            if key not in SUPPORTED_KEYWORDS:
                raise ValueError(f"schema {where}: unsupported keyword {key!r}")
            if key in ("properties", "$defs") and isinstance(sub, dict):
                for name, child in sub.items():
                    self._check_keywords(child, f"{where}/{key}/{name}")
            elif key in ("items", "additionalProperties", "propertyNames"):
                self._check_keywords(sub, f"{where}/{key}")
            elif key == "oneOf" and isinstance(sub, list):
                for i, child in enumerate(sub):
                    self._check_keywords(child, f"{where}/oneOf/{i}")

    def _resolve(self, ref: str) -> Dict[str, Json]:
        if not ref.startswith("#/"):
            raise ValueError(f"only local $ref is supported: {ref}")
        node: Json = self.document
        for part in ref[2:].split("/"):
            if not isinstance(node, dict) or part not in node:
                raise ValueError(f"dangling $ref {ref}")
            node = node[part]
        if not isinstance(node, dict):
            raise ValueError(f"$ref {ref} is not a schema")
        return node

    def errors(self, value: Json, schema: Optional[Dict[str, Json]] = None, path: str = "$") -> List[str]:
        node = self.document if schema is None else schema
        out: List[str] = []
        ref = node.get("$ref")
        if isinstance(ref, str):
            out.extend(self.errors(value, self._resolve(ref), path))
        types = node.get("type")
        if types is not None:
            names = types if isinstance(types, list) else [types]
            if not any(_type_ok(value, str(n)) for n in names):
                return out + [f"{path}: expected {'/'.join(str(n) for n in names)}, got {json.dumps(value)[:60]}"]
        if "const" in node and value != node["const"]:
            out.append(f"{path}: must be {json.dumps(node['const'])}")
        enum = node.get("enum")
        if isinstance(enum, list) and value not in enum:
            out.append(f"{path}: {json.dumps(value)[:60]} is not one of the allowed values")
        if isinstance(value, str):
            pattern = node.get("pattern")
            if isinstance(pattern, str) and re.search(pattern, value) is None:
                out.append(f"{path}: {value!r} does not match {pattern}")
            min_len = node.get("minLength")
            if isinstance(min_len, int) and len(value) < min_len:
                out.append(f"{path}: shorter than {min_len}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = node.get("minimum")
            if isinstance(minimum, (int, float)) and value < minimum:
                out.append(f"{path}: below minimum {minimum}")
        if isinstance(value, list):
            out.extend(self._array_errors(value, node, path))
        if isinstance(value, dict):
            out.extend(self._object_errors(value, node, path))
        one_of = node.get("oneOf")
        if isinstance(one_of, list):
            passing = [i for i, alt in enumerate(one_of) if isinstance(alt, dict) and not self.errors(value, alt, path)]
            if len(passing) != 1:
                if passing:
                    out.append(f"{path}: matches {len(passing)} alternatives of oneOf, expected exactly 1")
                else:
                    best = min((self.errors(value, alt, path) for alt in one_of if isinstance(alt, dict)), key=len)
                    out.append(f"{path}: matches no alternative of oneOf; closest: {best[0] if best else '?'}")
        return out

    def _array_errors(self, value: List[Json], node: Dict[str, Json], path: str) -> List[str]:
        out: List[str] = []
        lo, hi = node.get("minItems"), node.get("maxItems")
        if isinstance(lo, int) and len(value) < lo:
            out.append(f"{path}: fewer than {lo} items")
        if isinstance(hi, int) and len(value) > hi:
            out.append(f"{path}: more than {hi} items")
        items = node.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                out.extend(self.errors(item, items, f"{path}[{i}]"))
        return out

    def _object_errors(self, value: Dict[str, Json], node: Dict[str, Json], path: str) -> List[str]:
        out: List[str] = []
        required = node.get("required")
        if isinstance(required, list):
            out.extend(f"{path}: missing required {k!r}" for k in required if k not in value)
        lo, hi = node.get("minProperties"), node.get("maxProperties")
        if isinstance(lo, int) and len(value) < lo:
            out.append(f"{path}: fewer than {lo} properties")
        if isinstance(hi, int) and len(value) > hi:
            out.append(f"{path}: more than {hi} properties")
        props = node.get("properties")
        props = props if isinstance(props, dict) else {}
        extra = node.get("additionalProperties", True)
        names = node.get("propertyNames")
        for key, sub in value.items():
            if isinstance(names, dict):
                out.extend(self.errors(key, names, f"{path}.{key}(name)"))
            if key in props and isinstance(props[key], dict):
                out.extend(self.errors(sub, props[key], f"{path}.{key}"))
            elif extra is False:
                out.append(f"{path}: unexpected property {key!r}")
            elif isinstance(extra, dict):
                out.extend(self.errors(sub, extra, f"{path}.{key}"))
        return out


# ---------------------------------------------------------------- program headers


@dataclass
class Program:
    name: str
    directory: Path
    criteria: Set[str] = field(default_factory=set)
    fc: Set[str] = field(default_factory=set)
    tags: Set[str] = field(default_factory=set)
    members: List[str] = field(default_factory=list)
    witnesses: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    tasks: Set[str] = field(default_factory=set)
    sources: Dict[str, List[str]] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)


HEADER_KEY = re.compile(r"^\s*([A-Za-z]+):\s*(.*)$")
LIST_KEYS = {"criteria", "fc", "tags", "members"}


def read_header(text: str) -> Dict[str, List[str]]:
    """Parse `key: value` lines (indented continuations allowed) from the first (* *) comment."""
    m = re.match(r"\s*\(\*(.*?)\*\)", text, re.S)
    if m is None:
        return {}
    fields: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for line in m.group(1).splitlines():
        km = HEADER_KEY.match(line)
        if km and (km.group(1).lower() in LIST_KEYS | {"corpus", "witness", "build", "sources"} or km.group(1) == "ASSUMED"):
            current = km.group(1).lower()
            fields.setdefault(current, []).append(km.group(2).strip())
        elif current is not None and line.startswith("     ") and line.strip():
            fields[current][-1] += " " + line.strip()
        else:
            current = None
    return fields


def strip_comments(text: str) -> str:
    """Blank out ST comments and pragmas, keeping line numbers."""
    def blank(m: "re.Match[str]") -> str:
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"\(\*.*?\*\)", blank, text, flags=re.S)
    text = re.sub(r"//[^\n]*", blank, text)
    out: List[str] = []
    depth = 0
    for ch in text:
        depth += ch == "{"
        out.append(ch if depth == 0 or ch == "\n" else " ")
        depth -= ch == "}" and depth > 0
    return "".join(out)


def first_statement(code: str, pou_type: str) -> Optional[str]:
    """The first statement of PROGRAM `pou_type`'s body, after its declaration blocks."""
    m = re.search(rf"^\s*PROGRAM\s+{re.escape(pou_type)}\b(?!\s+WITH)(.*?)^\s*END_PROGRAM", code, re.S | re.M | re.I)
    if m is None:
        return None
    body = m.group(1)
    while True:
        vm = re.match(r"\s*VAR(_[A-Z]+)?\b(\s+(RETAIN|CONSTANT|NON_RETAIN))?.*?\bEND_VAR\b", body, re.S | re.I)
        if vm is None:
            break
        body = body[vm.end():]
    stmt = body.split(";", 1)[0]
    return re.sub(r"\s+", " ", stmt).strip()


def load_program(directory: Path) -> Program:
    prog = Program(directory.name, directory)
    family = directory / FAMILY_FILE
    main = family if family.exists() else directory / MAIN_SOURCE
    if not main.exists():
        prog.problems.append(f"{directory}: no {MAIN_SOURCE} (or {FAMILY_FILE})")
        return prog
    header = read_header(main.read_text(encoding="utf-8"))
    corpus = header.get("corpus", [""])[0]
    if corpus != prog.name:
        prog.problems.append(f"{main}: header corpus {corpus!r} differs from directory {prog.name!r}")
    prog.criteria.update(" ".join(header.get("criteria", [])).split())
    prog.fc.update(" ".join(header.get("fc", [])).split())
    prog.tags.update(" ".join(header.get("tags", [])).split())
    prog.members.extend(" ".join(header.get("members", [])).split())
    if not prog.criteria:
        prog.problems.append(f"{main}: header lists no criteria")
    prog.problems.extend(f"{main}: bad criterion id {c!r}" for c in sorted(prog.criteria) if not re.fullmatch(r"A\d{3}", c))
    prog.problems.extend(f"{main}: bad FC tag {t!r}" for t in sorted(prog.fc) if not re.fullmatch(r"FC-[A-Z]+-\d+", t))
    if family.exists():
        if not prog.members:
            prog.problems.append(f"{family}: a family names its members")
        return prog
    if not prog.fc:
        prog.problems.append(f"{main}: header lists no FC tags")
    for path in sorted(directory.glob("*.st")):
        prog.sources[path.name] = path.read_text(encoding="utf-8").splitlines()
    for entry in header.get("witness", []):
        parts = entry.split()
        if len(parts) != 3 or "." not in parts[1] or not parts[2].startswith("%Q"):
            prog.problems.append(f"{main}: witness line {entry!r} is not '<task> <instance>.<var> <%Q address>'")
            continue
        prog.witnesses[parts[0]] = (parts[1], parts[2])
    check_witness(prog, main)
    return prog


def check_witness(prog: Program, main: Path) -> None:
    """Enforce the corpus contract: each task's first program increments a located DINT witness first."""
    for fname, lines in prog.sources.items():
        if SECONDARY.match(fname):
            continue
        code = strip_comments("\n".join(lines))
        tasks = re.findall(r"^\s*TASK\s+(\w+)\s*\(", code, re.M | re.I)
        bindings = re.findall(r"^\s*PROGRAM\s+(\w+)\s+WITH\s+(\w+)\s*:\s*(\w+)", code, re.M | re.I)
        prog.tasks.update(tasks)
        for task in tasks:
            bound = [(inst, typ) for inst, t, typ in bindings if t == task]
            if not bound:
                prog.problems.append(f"{main}: task {task} runs no program")
                continue
            if task not in prog.witnesses:
                prog.problems.append(f"{main}: task {task} has no scan witness header line")
                continue
            path, addr = prog.witnesses[task]
            inst, var = path.split(".", 1)
            if inst != bound[0][0]:
                prog.problems.append(f"{main}: witness of {task} must be on its first program {bound[0][0]}, not {inst}")
                continue
            pou_code = "\n".join("\n".join(v) for f, v in prog.sources.items() if not SECONDARY.match(f))
            stmt = first_statement(strip_comments(pou_code), bound[0][1])
            want = f"{var} := {var} + 1"
            if stmt is None or stmt.replace(" ", "").lower() != want.replace(" ", "").lower():
                prog.problems.append(f"{main}: first statement of {bound[0][1]} is {stmt!r}, expected '{want};'")
            decl = re.search(rf"\b{re.escape(var)}\s+AT\s+{re.escape(addr)}\s*:\s*DINT\b", pou_code, re.I)
            if decl is None:
                prog.problems.append(f"{main}: witness {var} is not declared '{var} AT {addr} : DINT'")


# ---------------------------------------------------------------- oracle semantics


def walk(node: Json, path: str = "$") -> Iterator[Tuple[str, Json]]:
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")


SECONDARY = re.compile(r"^(variant|reject)-.*\.st$")


def marker_lines(prog: Program, marker: str, programs: Dict[str, Program],
                 fname: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """Resolve `marker` in `fname`, or in each member's primary sources: (problems, matched line texts)."""
    targets = [programs[m] for m in prog.members if m in programs] if prog.members else [prog]
    out: List[str] = []
    texts: List[str] = []
    tag = f"(*{marker}*)"
    for target in targets:
        files = [fname] if fname else [f for f in target.sources if not SECONDARY.match(f)]
        hits = [line for f in files for line in target.sources.get(f, []) if tag in line]
        if len(hits) != 1:
            where = fname or "its primary sources"
            out.append(f"marker {marker} found {len(hits)} times in {target.name} ({where}), expected once")
        texts.extend(hits[:1])
    return out, texts


def oracle_problems(path: Path, doc: Json, programs: Dict[str, Program]) -> List[str]:
    if not isinstance(doc, dict):
        return ["not an object"]
    out: List[str] = []
    corpus = doc.get("corpus")
    expected_id = f"{path.parent.name}/{path.name[: -len('.oracle.json')]}"
    if doc.get("scenario") != expected_id:
        out.append(f"scenario {doc.get('scenario')!r} must be {expected_id!r}")
    if corpus != path.parent.name:
        out.append(f"corpus {corpus!r} must be the directory name {path.parent.name!r}")
    prog = programs.get(str(corpus))
    if prog is None:
        return out + [f"no program directory for corpus {corpus!r}"]
    criteria = doc.get("criteria")
    if isinstance(criteria, list):
        out.extend(f"criterion {c} is not cited by {prog.name}'s header" for c in criteria if c not in prog.criteria)
    members = [programs[m] for m in prog.members if m in programs] or [prog]
    files = set().union(*(set(m.sources) for m in members))
    tasks = set().union(*(m.tasks for m in members))
    captured: Set[str] = set()
    plant_reads = 0
    steps = doc.get("steps")
    for i, step in enumerate(steps if isinstance(steps, list) else []):
        if isinstance(step, dict) and step.get("observe") == "plant":
            plant_reads += 1
        for where, node in walk(step, f"steps[{i}]"):
            if isinstance(node, dict):
                ref = node.get("~ref")
                if isinstance(ref, str) and ref not in captured:
                    out.append(f"{where}: ~ref {ref!r} has no earlier ~capture")
                cap = node.get("~capture")
                if isinstance(cap, str):
                    captured.add(cap)
                fname = node.get("file")
                if isinstance(fname, str) and "line" in node and fname not in files:
                    out.append(f"{where}: file {fname!r} is not a source of {prog.name}")
                listed = node.get("lines")
                for val in listed if isinstance(listed, list) else []:
                    if isinstance(val, str) and val.startswith("@"):
                        out.extend(f"{where}.lines: {p}" for p in marker_lines(prog, val, programs)[0])
                for key in ("line", "endLine"):
                    val = node.get(key)
                    if not (isinstance(val, str) and val.startswith("@")):
                        continue
                    fname = node.get("file")
                    missing, texts = marker_lines(prog, val, programs, fname if isinstance(fname, str) else None)
                    out.extend(f"{where}.{key}: {p}" for p in missing)
                    col = node.get("col")
                    if key == "line" and isinstance(col, int):
                        out.extend(f"{where}.col: column {col} of {val} is not the start of a statement"
                                   for t in texts if col > len(t) or t[col - 1].isspace() or (col > 1 and not t[col - 2].isspace()))
                task = node.get("task")
                if isinstance(task, str) and tasks and task not in tasks:
                    out.append(f"{where}: task {task!r} is not a task of {prog.name}")
        expect = step.get("expect") if isinstance(step, dict) and "observe" in step else None
        for key in ("witness", "scanDelta", "vtMs"):
            per_task = expect.get(key) if isinstance(expect, dict) else None
            if isinstance(per_task, dict) and tasks:
                out.extend(f"steps[{i}].expect.{key}: task {t!r} is not a task of {prog.name}" for t in per_task if t not in tasks)
    if doc.get("kind") == "runtime" and plant_reads == 0:
        out.append("a runtime scenario must read the plant at least once (observe: plant)")
    return out


# ---------------------------------------------------------------- cross-checks


def acceptance_citations(path: Path) -> Dict[str, Set[str]]:
    cited: Dict[str, Set[str]] = {}
    text = path.read_text(encoding="utf-8")
    for block in re.split(r"\n(?=### A\d+ )", text):
        head = re.match(r"### (A\d+) ", block)
        corpus = re.search(r"^Corpus:\s*(.*)$", block, re.M)
        if head is None or corpus is None:
            continue
        for name in re.findall(r"corpus:([a-z0-9_-]+)", corpus.group(1)):
            cited.setdefault(name, set()).add(head.group(1))
    return cited


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--acceptance", type=Path, help="docs/ACCEPTANCE.md of the orchestration root")
    parser.add_argument("--catalog", type=Path, help="docs/drafts/feature-catalog.md, to check FC tags exist")
    parser.add_argument("--root", type=Path, default=ROOT, help="corpus root (default: this directory)")
    parser.add_argument("oracles", nargs="*", type=Path, help="oracle files (default: every one under the root)")
    args = parser.parse_args(argv)

    schema_doc = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(schema_doc, dict):
        print(f"{SCHEMA_PATH}: schema is not an object")
        return 1
    schema = Schema(schema_doc)
    root: Path = args.root.resolve()
    programs = {d.name: load_program(d) for d in sorted(root.iterdir())
                if d.is_dir() and ((d / MAIN_SOURCE).exists() or (d / FAMILY_FILE).exists())}
    problems: List[str] = [p for prog in programs.values() for p in prog.problems]
    if not programs:
        problems.append(f"{root}: no program directories")

    oracles = [p.resolve() for p in args.oracles] or sorted(root.glob(f"*/{ORACLE_GLOB}"))
    for prog in programs.values():
        if not any(o.parent == prog.directory for o in oracles) and not args.oracles:
            problems.append(f"{prog.directory}: no {ORACLE_GLOB} scenario")
    for path in oracles:
        try:
            doc: Json = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{path}: {exc}")
            continue
        found = schema.errors(doc)
        if not found:
            found = oracle_problems(path, doc, programs)
        problems.extend(f"{path}: {p}" for p in found)

    if args.acceptance:
        cited = acceptance_citations(args.acceptance)
        for name, prog in programs.items():
            want = cited.get(name, set())
            if not want:
                problems.append(f"{prog.directory}: no criterion in {args.acceptance} cites corpus:{name}")
            for c in sorted(want - prog.criteria):
                problems.append(f"{prog.directory}: header omits {c}, which cites corpus:{name}")
            for c in sorted(prog.criteria - want):
                problems.append(f"{prog.directory}: header lists {c}, which does not cite corpus:{name}")
        problems.extend(f"{args.acceptance}: corpus:{n} has no program directory" for n in sorted(set(cited) - set(programs)))
    if args.catalog:
        known = set(re.findall(r"FC-[A-Z]+-\d+", args.catalog.read_text(encoding="utf-8")))
        for prog in programs.values():
            problems.extend(f"{prog.directory}: unknown FC tag {t}" for t in sorted(prog.fc - known))

    for p in problems:
        print(p)
    scenarios = len(oracles)
    print(f"{'FAIL' if problems else 'OK'}: {len(programs)} programs, {scenarios} scenarios, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
