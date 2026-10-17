#!/usr/bin/env python3
"""Validate the debug corpus: every program header and every oracle scenario.

Checks, in order:
  1. Each oracle against oracle.schema.json (a stdlib JSON Schema 2020-12 subset;
     a schema keyword outside the subset is an error, never silently ignored).
  2. Oracle semantics the schema cannot express: scenario id and corpus match the
     path, criteria are cited by the program header, `@MARKER` lines resolve once
     in the program sources (wherever they appear), files and tasks exist, every `~ref` has an earlier
     `~capture`, a runtime scenario reads the plant at least once, argument symbols
     are in the vocabulary (a `~build` variant exists), and `client` is used only
     where it means something. Loop bodies are checked as steps, in order.
  3. Program headers: corpus name, criteria, FC tags, and the scan witness
     contract (one per task, first statement of the task's first program).
  4. Optional cross-checks: --acceptance (header criteria equal the criteria that
     cite the corpus, and every oracle's `criteria` list against those criteria's
     `Test:` field in both directions) and --catalog (every FC tag exists).

Usage: validate.py [--acceptance ACCEPTANCE.md] [--catalog feature-catalog.md] [--strict] [ORACLE ...]
Exit status 0 when everything validates, 1 otherwise; one `path: message` per problem.
Warnings are printed as `warning: ...` and are not failures unless --strict is given:
they are the parts of the corpus that have not caught up with their acceptance document,
and a run that fails on them cannot be made green without doing that work.

See also:
  tests/debug/corpus/README.md - the oracle format and the program header contract.
  tests/debug/corpus/oracle.schema.json - the schema checked in step 1.
  tests/debug/corpus/test_validate.py - red/green proof that this validator rejects defects.
  tests/debug/corpus/build_all.sh - compiles every program, then runs this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple, Union

Json = Union[None, bool, int, float, str, List["Json"], Dict[str, "Json"]]

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "oracle.schema.json"
ORACLE_SUFFIX = ".oracle.json"
ORACLE_GLOB = f"*{ORACLE_SUFFIX}"
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
TOKENS = {"valid", "none", "forged", "expired"}
USER_TOKEN = re.compile(r"^user:[a-z][a-z0-9_]*$")  # a named test user's JWT (README § Argument symbols)
MARKER = re.compile(r"^@[A-Za-z0-9_]+$")  # the runner resolves this form wherever it appears


def flat_steps(steps: Json, prefix: str = "steps") -> Iterator[Tuple[str, Json]]:
    """Every step with its path; a loop contributes its body, in order, not itself."""
    for i, step in enumerate(steps if isinstance(steps, list) else []):
        where = f"{prefix}[{i}]"
        if isinstance(step, dict) and "loop" in step:
            yield from flat_steps(step.get("steps"), f"{where}.steps")
        else:
            yield where, step


def symbol_problem(node: Dict[str, Json], prog: "Program") -> Optional[str]:
    """README § Argument symbols: None when `node` is a well-formed symbol."""
    keys = set(node)
    if "~ref" in keys and keys <= {"~ref", "~add"}:
        return None
    if keys == {"~build"}:
        what, _, variant = str(node["~build"]).partition(":")
        if what != "identity":
            return f"unknown ~build symbol {what!r}"
        if variant and not (SECONDARY.match(variant) and variant in prog.sources):
            return f"~build identity names {variant!r}, which is not a variant-*.st of {prog.name}"
        return None
    if keys == {"~lines"} or keys == {"~leaves", "~count"}:
        return None
    if keys == {"~token"}:
        tok = node["~token"]
        if tok in TOKENS or (isinstance(tok, str) and USER_TOKEN.match(tok)):
            return None
        return f"~token must be one of {sorted(TOKENS)} or user:<name>"
    return f"unknown argument symbol {sorted(keys)}"


def client_problem(step: Dict[str, Json]) -> Optional[str]:
    if "client" not in step:
        return None
    if "observe" in step and step["observe"] != "session":
        return f"client applies to session reads, not observe {step['observe']!r}"
    if "inject" in step and step["inject"] != "killClient":
        return f"client applies to killClient, not inject {step['inject']!r}"
    if step.get("thenContinue") is True and not (isinstance(step["client"], str) and step["client"] != "*"):
        return "thenContinue needs exactly one client stream"
    return None


# The corpus could state that something happened *late* and never that it happened *soon*. This is
# the rule that tells the two apart, and it is a rule about what the runtime is doing, not about how
# the number was typed:
#
#   A step whose expectation names a TERMINAL run state, or a force list that has emptied, is
#   waiting for the runtime to reach the end of something -- a P2 heartbeat timeout, a force TTL.
#   The runtime does not get there by being fast; it gets there by waiting the interval out. So the
#   interval is one contention can only lengthen, which is the one case where a wall-clock floor
#   (`notBeforeMs`) is safe to assert, and its absence leaves a safety claim half-stated: a runtime
#   that safe-stops at once satisfies every ceiling in the corpus.
#
# The converse is not a defect. A step waiting for a *response* to a request the harness just sent
# (`stopped` after `continue`, `programReplaced` after `upload`) has no floor worth asserting: the
# right answer is as fast as possible, and on a loaded host the interval is longer for reasons that
# say nothing about the runtime. Those steps are genuinely one-sided and this rule leaves them alone.
FLOOR_MIN_MS = 2000
TERMINAL_STATES = ("SAFE_STOPPED", "STOPPED", "ERROR")


def _waits_out_something(step: Dict[str, Json]) -> Optional[str]:
    """Why this event step is a timeout-to-effect, or None if it is not one."""
    expect = step.get("expect")
    expect = expect if isinstance(expect, dict) else {}
    body = expect.get("body")
    body = body if isinstance(body, dict) else {}
    state = body.get("state")
    if state in TERMINAL_STATES:
        return f"the {state} run state"
    for _key, value in _flat_nodes(body):
        if isinstance(value, str) and value.endswith("_timeout"):
            return f"the {value} reason"
    # An emptied force list is a TTL ending: the runtime waited the force's own time out.
    forces = _obj(body.get("list"))
    if forces.get("~len") == 0:
        return "an emptied force list (a TTL ending)"
    return None


def _obj(node: Json) -> Dict[str, Json]:
    return node if isinstance(node, dict) else {}


def _flat_nodes(node: Json, prefix: str = "") -> Iterator[Tuple[str, Json]]:
    yield prefix, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _flat_nodes(v, f"{prefix}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _flat_nodes(v, f"{prefix}[{i}]")


def missing_floor(step: Dict[str, Json]) -> Optional[str]:
    """The complaint on an event step whose ceiling is asserted and whose floor is not."""
    within = step.get("withinMs")
    if not isinstance(within, (int, float)) or isinstance(within, bool) or within < FLOOR_MIN_MS:
        return None
    if "notBeforeMs" in step:
        return None
    what = _waits_out_something(step)
    if what is None:
        return None
    return (f"waits for {what} within {within:g} ms but sets no notBeforeMs: the ceiling is "
            "asserted and the floor is not, so a runtime that gets there at once passes. Add "
            "notBeforeMs with the earliest instant the declared interval allows (README § Timing "
            "bounds), or re-word the claim if that instant is not knowable here.")


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
    for here, step in flat_steps(doc.get("steps")):
        if isinstance(step, dict) and step.get("observe") == "plant":
            plant_reads += 1
        if isinstance(step, dict):
            bad = client_problem(step)
            if bad:
                out.append(f"{here}: {bad}")
            if "event" in step:
                bad = missing_floor(step)
                if bad:
                    out.append(f"{here}: {bad}")
            for where, node in walk(step.get("args"), f"{here}.args"):
                if isinstance(node, dict) and node and all(k.startswith("~") for k in node):
                    bad = symbol_problem(node, prog)
                    if bad:
                        out.append(f"{where}: {bad}")
        for where, node in walk(step, here):
            if isinstance(node, str) and MARKER.match(node) and not re.search(r"\.(line|endLine)$", where):
                out.extend(f"{where}: {p}" for p in marker_lines(prog, node, programs)[0])
            if isinstance(node, dict):
                for key in ("~ref", "~notRef", "~sha256"):
                    ref = node.get(key)
                    if isinstance(ref, str) and ref not in captured:
                        out.append(f"{where}: {key} {ref!r} has no earlier ~capture")
                cap = node.get("~capture")
                if isinstance(cap, str):
                    captured.add(cap)
                fname = node.get("file")
                if isinstance(fname, str) and "line" in node and fname not in files:
                    out.append(f"{where}: file {fname!r} is not a source of {prog.name}")
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
        for key in ("witness", "scanDelta", "vtMs", "maxGapMs", "sinceVtMs"):
            per_task = expect.get(key) if isinstance(expect, dict) else None
            if isinstance(per_task, dict) and tasks:
                out.extend(f"{here}.expect.{key}: task {t!r} is not a task of {prog.name}" for t in per_task if t not in tasks)
    if doc.get("kind") == "runtime" and plant_reads == 0:
        out.append("a runtime scenario must read the plant at least once (observe: plant)")
    out.extend(diagnostic_problems(path, doc, programs))
    return out


# A diagnostic says which scenario it is a bisect of and how many steps it removed. Both are checked
# as counts, not prose: `Steps removed:` is compared against the sibling oracle's own step count, so
# a diagnostic that has quietly stopped being a bisect (the sibling grew steps, or the notes were
# copied) fails rather than decaying into a scenario nobody has read.
DIAGNOSTIC_OF = re.compile(r"^Diagnostic of:[ \t]*(\S+)[ \t]*$", re.M)
DIAGNOSTIC_REMOVED = re.compile(r"^Steps removed:[ \t]*(\d+)[ \t]*$", re.M)


def diagnostic_problems(path: Path, doc: Json, programs: Dict[str, Program]) -> List[str]:
    """`criteria: []` is a claim of nothing, so it takes a `kind` to say so, and a count to prove it."""
    criteria = doc.get("criteria")
    listed = isinstance(criteria, list) and bool(criteria)
    if doc.get("kind") == "diagnostic":
        if listed:
            return [f"a diagnostic scenario claims {criteria}; it is a bisect and earns nothing, so "
                    "its criteria must be empty"]
    elif not listed and isinstance(criteria, list):
        return ["criteria is empty: say why with \"kind\": \"diagnostic\", which names the scenario "
                "this one is a bisect of and how many steps it removed"]
    if doc.get("kind") != "diagnostic":
        return []
    notes = str(doc.get("notes", ""))
    of = DIAGNOSTIC_OF.search(notes)
    removed = DIAGNOSTIC_REMOVED.search(notes)
    out: List[str] = []
    if of is None:
        out.append("a diagnostic scenario's notes must name the scenario it is a bisect of, "
                   "as a line 'Diagnostic of: <corpus>/<scenario>'")
    if removed is None:
        out.append("a diagnostic scenario's notes must count the steps it removed, as a line "
                   "'Steps removed: <n>'")
    if out:
        return out
    name = of.group(1)  # type: ignore[union-attr]
    sibling = path.parent / f"{name.split('/', 1)[-1]}{ORACLE_SUFFIX}"
    if not sibling.exists():
        return [f"a diagnostic scenario is a bisect of {name}, which does not exist"]
    try:
        other = json.loads(sibling.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{sibling}: {exc}"]
    if not isinstance(other, dict):
        return [f"{sibling}: not an object"]
    if str(other.get("scenario")) != name:
        out.append(f"a diagnostic scenario is a bisect of {name}, whose `scenario` is "
                   f"{other.get('scenario')!r}")
    other_steps = len(flat_steps_of(other))
    steps = len(flat_steps_of(doc))
    want = int(removed.group(1))  # type: ignore[union-attr]
    if want < 1:
        out.append(f"a diagnostic scenario says it removed {want} steps of {name}; a bisect that "
                   "removed nothing is not a bisect")
    elif steps + want != other_steps:
        out.append(f"a diagnostic scenario says it removed {want} step(s) of {name}, which has "
                   f"{other_steps}; this one has {steps}")
    return out


def flat_steps_of(doc: Json) -> List[Tuple[str, Json]]:
    """`flat_steps` for any document, including one that failed to parse."""
    return list(flat_steps(doc.get("steps")) if isinstance(doc, dict) else [])


# ---------------------------------------------------------------- cross-checks


def acceptance_citations(path: Path) -> Dict[str, Set[str]]:
    cited: Dict[str, Set[str]] = {}
    for block in _acceptance_blocks(path):
        head = re.match(r"### (A\d+) ", block)
        corpus = re.search(r"^Corpus:\s*(.*)$", block, re.M)
        if head is None or corpus is None:
            continue
        for name in re.findall(r"corpus:([a-z0-9_-]+)", corpus.group(1)):
            cited.setdefault(name, set()).add(head.group(1))
    return cited


def _acceptance_blocks(path: Path) -> List[str]:
    return re.split(r"\n(?=### A\d+ )", path.read_text(encoding="utf-8"))


def acceptance_tests(path: Path) -> Dict[str, Optional[List[str]]]:
    """Criterion -> the paths its `Test:` names, or None when the field is `—` or absent.

    ACCEPTANCE.md's `Test:` is the only authority over what an oracle earns (README § Coverage): an
    oracle's `criteria` list is the claim, this is what says the claim was paid for. `Corpus:` is the
    weaker field -- it names a program directory, and every criterion of that program cites it -- so
    a header cross-check can be green while the oracle that does the work names nothing. A `Test:`
    value continues on following lines indented by two or more spaces.
    """
    out: Dict[str, Optional[List[str]]] = {}
    for block in _acceptance_blocks(path):
        head = re.match(r"### (A\d+) ", block)
        found = re.search(r"^Test:[ \t]*(\S.*)$", block, re.M)
        if head is None:
            continue
        value = found.group(1).strip() if found else ""
        if found is not None:
            lines = block[found.end():].splitlines()
            take = 0
            for line in lines:
                if not line.startswith("  ") or not line.strip():
                    break
                value += " " + line.strip()
                take += 1
        out[head.group(1)] = None if value in ("", "—") else value.split(", ")
    return out


# A `Test:` that names a corpus scenario. Anything else (a shell script, an ADR review, `—`) is left
# to whoever owns that criterion: this check joins two documents, it does not judge either.
TESTED_SCENARIO = re.compile(r"corpus/([a-z0-9_-]+)/([a-z0-9_.-]+)\.oracle\.json")


def join_problems(oracles: List[Tuple[Path, Json]], acceptance: Path) -> List[str]:
    """Every way an oracle's `criteria` and ACCEPTANCE.md's `Test:` can disagree.

    Both directions, because either one alone lets a claim through: forward, a scenario claims a
    criterion whose `Test:` is `—` or names other scenarios (the scenario earns nothing); reverse, a
    criterion names a scenario that does not cite it (the criterion is not exercised by anything).
    """
    out: List[str] = []
    tests = acceptance_tests(acceptance)
    on_disk = {f"{p.parent.name}/{p.name[: -len(ORACLE_SUFFIX)]}" for p, _ in oracles}
    claims: Dict[str, Set[str]] = {}
    for path, doc in oracles:
        scenario = str(doc.get("scenario", ""))
        for c in [str(x) for x in (doc.get("criteria") or [])]:
            claims.setdefault(scenario, set()).add(c)
            where = f"{path}: claims {c}"
            named = tests.get(c, False)  # False = no such block; None = the field is —
            if named is False:
                out.append(f"{where}, which no ACCEPTANCE criterion block names")
            elif named is None:
                out.append(f"{where}, whose ACCEPTANCE Test: is —")
            elif not any(TESTED_SCENARIO.search(t) for t in named):
                out.append(f"{where}, whose ACCEPTANCE Test: names no corpus scenario: {', '.join(named)}")
            elif not any(
                (found := TESTED_SCENARIO.search(t)) is not None
                and "/".join(found.groups()) == scenario
                for t in named
            ):
                out.append(f"{where}, whose ACCEPTANCE Test: names other scenarios: {', '.join(named)}")
    for c, named in sorted(tests.items()):
        for value in named or []:
            for found in TESTED_SCENARIO.finditer(value):
                scenario = "/".join(found.groups())
                if scenario not in on_disk:
                    out.append(f"{c}'s Test: names {scenario}, which does not exist")
                elif c not in claims.get(scenario, set()):
                    what = ", ".join(sorted(claims.get(scenario, set()))) or "no criterion at all"
                    out.append(
                        f"{c}'s Test: names {scenario}, which does not cite it (it claims {what})"
                    )
    return out


# `build_all.sh`'s per-program record of what a verdict was measured on: the compiler head, the
# corpus commit, and a hash of every source the verdict depends on. Plain text, one `key value...`
# per line, so a person can read what a verdict meant without this script.
BUILD_RECORD = "build-record.txt"
COVERAGE_FILE = "coverage.txt"


def read_build_record(path: Path) -> Dict[str, Json]:
    """`key value...` lines into a dict; `source` lines become a list of [name, sha256, bytes].

    A truncated or hand-edited record must not crash the validator: the caller reports what it can
    read and says so, rather than the run dying on a file nobody's build produced.
    """
    out: Dict[str, Json] = {"source": []}
    sources: List[List[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "source" and len(parts) >= 3:
            sources.append(parts[1:])
        else:
            out[parts[0]] = parts[1] if len(parts) > 1 else ""
    out["source"] = sources
    return out


def build_record_warnings(root: Path, out: Path) -> List[str]:
    """Every recorded build verdict that no longer describes the sources it was measured on.

    A warning, never a failure: the corpus has to validate on a machine that has not built it, and a
    stale record says the build is due, not that the corpus is wrong. A missing build directory says
    nothing at all -- there is no record to be stale.
    """
    found: List[str] = []
    if not out.is_dir():
        return found
    for record in sorted(out.glob(f"*/{BUILD_RECORD}")):
        prog = root / record.parent.name
        if not prog.is_dir():
            continue  # a record for a program this root does not have is not this root's business
        where = f"{prog.name}: "
        try:
            doc = read_build_record(record)
        except OSError as exc:
            found.append(f"{where}{BUILD_RECORD} is unreadable: {exc}")
            continue
        head = str(doc.get("strucpp", "unknown"))
        sources = [list(x) for x in (doc.get("source") or [])]  # type: ignore[union-attr]
        if not sources:
            found.append(f"{where}the build recorded at strucpp {head} records no source, "
                         "so nothing says what it measured")
            continue
        stale: List[str] = []
        for name, digest in [(str(s[0]), str(s[1])) for s in sources]:
            source = prog / name
            if not source.exists():
                stale.append(f"{name} no longer exists")
            elif hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                stale.append(f"{name} changed")
        if stale:
            found.append(f"{where}the build recorded at strucpp {head} is stale "
                         f"(recorded at corpus {doc.get('repo', 'unknown')}): " + ", ".join(stale))
    moved = compiler_moved(out)
    return found + ([moved] if moved else [])


def compiler_moved(out: Path) -> Optional[str]:
    """None unless a STruCpp checkout is here to compare against, and it has moved.

    `STRUCPP` (the same variable `build_all.sh` reads) or a sibling `../STruCpp`. This is the whole
    reason the records exist: "does not compile" is a statement about a revision, so the revision is
    recorded, and a compiler that has moved since is the one thing that invalidates every verdict at
    once. Silently absent when there is no checkout -- a checker-only CI job has none.
    """
    named = os.environ.get("STRUCPP", "")
    compiler = Path(named).expanduser() if named else out.parents[2] / "STruCpp"
    if not (compiler / ".git").is_dir() or not shutil.which("git"):
        return None
    try:
        head = subprocess.run(
            ["git", "-C", str(compiler), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    heads = {
        str(read_build_record(record).get("strucpp", ""))
        for record in sorted(out.glob(f"*/{BUILD_RECORD}"))
        if (record.parent.is_dir())
    }
    behind = sorted(h for h in heads if h and h != head)
    if not behind:
        return None
    return (f"the corpus was built with STruCpp {', '.join(behind)}, "
            f"which has moved to {head}: every build verdict in {out} predates it")


# The index block of tests/debug/harness/coverage.txt is generated from the oracles; the prose around
# it is not, because a generator cannot know what a scenario does not check. The markers are what
# makes that split mechanical.
COVERAGE_BEGIN = "<!-- BEGIN generated: tests/debug/corpus/validate.py --write-coverage -->"
COVERAGE_END = "<!-- END generated -->"
COVERAGE_TEST_ID = "tests/debug/harness/runner.py::"


def coverage_block(loaded: List[Tuple[Path, Json]]) -> str:
    """The generated index: every criterion and the scenarios that claim it, then the scenarios that
    claim nothing (each a bisect, which is why it earns no criterion)."""
    claims: Dict[str, List[str]] = {}
    silent: List[str] = []
    for _, doc in loaded:
        scenario = str(doc.get("scenario", ""))
        criteria = [str(c) for c in (doc.get("criteria") or [])]
        if not criteria:
            silent.append(scenario)
        for c in criteria:
            claims.setdefault(c, []).append(scenario)
    lines = [f"{c} " + " ".join(f"{COVERAGE_TEST_ID}{s}" for s in sorted(claims[c]))
             for c in sorted(claims)]
    if silent:
        lines.append("")
        lines.append("# Scenarios that discharge no criterion: a bisect of another scenario, whose")
        lines.append("# verdict is evidence about that scenario's evidence and never coverage of its own.")
        lines.extend(f"#   {s}" for s in sorted(silent))
    return "\n".join(lines) + "\n"


def coverage_problems(loaded: List[Tuple[Path, Json]], path: Optional[Path]) -> List[str]:
    """[] when the file is absent (nothing to keep in step) or its generated block is current."""
    if path is None or not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    want = coverage_block(loaded)
    begin = text.find(COVERAGE_BEGIN)
    end = text.find(COVERAGE_END)
    if begin < 0 or end < 0:
        return [f"{path}: has no generated block (the markers {COVERAGE_BEGIN!r} / "
                f"{COVERAGE_END!r} are missing), so nothing says when its index was written"]
    have = text[begin + len(COVERAGE_BEGIN):end].strip("\n")
    if have.strip("\n") == want.strip("\n"):
        return []
    missing = [line for line in want.splitlines() if line and line not in have.splitlines()]
    extra = [line for line in have.splitlines() if line and line not in want.splitlines()]
    return (
        [f"{path}: its generated index is not what the oracles say "
         f"({len(missing)} line(s) missing, {len(extra)} line(s) no oracle claims)"]
        + [f"{path}:   missing: {line}" for line in missing[:20]]
        + [f"{path}:   stale:   {line}" for line in extra[:20]]
        + [f"{path}: rewrite it with `validate.py --write-coverage {path}` (the prose outside the "
            f"markers is left alone)"]
    )


def write_coverage(loaded: List[Tuple[Path, Json]], path: Path) -> None:
    """Rewrite the generated block in place; every other byte of the file is left as it was."""
    text = path.read_text(encoding="utf-8")
    begin, end = text.find(COVERAGE_BEGIN), text.find(COVERAGE_END)
    if begin < 0 or end < 0:
        raise SystemExit(f"{path}: has no generated block to write ({COVERAGE_BEGIN} / {COVERAGE_END})")
    body = "\n".join(
        [text[: begin + len(COVERAGE_BEGIN)], coverage_block(loaded).rstrip("\n"), text[end:]]
    )
    path.write_text(body, encoding="utf-8")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--acceptance", type=Path, help="docs/ACCEPTANCE.md of the orchestration root")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="count the warnings (the oracle<->ACCEPTANCE.md join, a stale build record, a stale "
        "coverage.txt) as problems. They are warnings by default so a corpus that has not caught up "
        "with its acceptance document still validates; the number going to zero is the work.",
    )
    parser.add_argument("--catalog", type=Path, help="docs/drafts/feature-catalog.md, to check FC tags exist")
    parser.add_argument(
        "--coverage",
        type=Path,
        help="tests/debug/harness/coverage.txt, whose generated index block is checked against the "
        "oracles (default: ../harness/coverage.txt when it is there; absent means nothing to check)",
    )
    parser.add_argument(
        "--write-coverage",
        nargs="?",
        const="",
        type=str,  # not Path: argparse would turn the empty `const` into "."
        metavar="COVERAGE_TXT",
        help="rewrite coverage.txt's generated index block from the oracles and check it (default: "
        "the harness's coverage.txt); the prose outside the markers is left exactly as it was",
    )
    parser.add_argument(
        "--build-out",
        type=Path,
        default=ROOT.parents[2] / "build" / "debug-corpus",
        help="build_all.sh's output directory, read for the per-program build records (a directory "
        "that does not exist means the corpus has not been built here, which is not a problem)",
    )
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
    warnings: List[str] = []
    loaded: List[Tuple[Path, Json]] = []
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
        loaded.append((path, doc))

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
        warnings.extend(join_problems(loaded, args.acceptance))
    if args.catalog:
        known = set(re.findall(r"FC-[A-Z]+-\d+", args.catalog.read_text(encoding="utf-8")))
        for prog in programs.values():
            problems.extend(f"{prog.directory}: unknown FC tag {t}" for t in sorted(prog.fc - known))
    warnings.extend(build_record_warnings(root, args.build_out.resolve()))
    coverage = args.coverage
    if coverage is None:
        default = root.parent / "harness" / COVERAGE_FILE
        coverage = default if default.exists() else None
    if args.write_coverage is not None:
        default = root.parent / "harness" / COVERAGE_FILE
        target = Path(str(args.write_coverage)) if str(args.write_coverage) else (
            coverage or default
        )
        write_coverage(loaded, target)
        coverage = target
    # A generated index that has drifted is a problem, not a warning: this script owns that file,
    # and the only thing that can be wrong with it is that nobody regenerated it.
    problems.extend(coverage_problems(loaded, coverage))

    if args.strict:
        problems.extend(warnings)
        warnings = []
    for p in problems:
        print(p)
    for w in warnings:
        print(f"warning: {w}")
    if warnings:
        print(f"{len(warnings)} warnings (--strict to count them as problems)")
    scenarios = len(oracles)
    print(f"{'FAIL' if problems else 'OK'}: {len(programs)} programs, {scenarios} scenarios, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
