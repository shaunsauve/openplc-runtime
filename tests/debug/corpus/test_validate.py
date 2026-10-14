"""Red/green proof for validate.py: the real corpus passes, each seeded defect fails.

NO-FOLD: pure-logic (the validator has no runtime surface; it gates the oracles every harness reads).
Run: python3 -m unittest tests/debug/corpus/test_validate.py

See also:
  tests/debug/corpus/validate.py - the validator under test.
  tests/debug/corpus/plant-io/ - the program and oracle the defects are seeded into.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import validate

ROOT = Path(__file__).resolve().parent
ORACLE = "bp-before-statement.oracle.json"

Doc = Dict[str, object]


def _steps(doc: Doc) -> List[Dict[str, object]]:
    steps = doc["steps"]
    assert isinstance(steps, list)
    return steps


def _stop_expect(doc: Doc) -> Dict[str, object]:
    expect = _steps(doc)[3]["expect"]
    assert isinstance(expect, dict)
    return expect


def bad_reason(doc: Doc) -> None:
    _stop_expect(doc)["reason"] = "brekpoint"


def missing_marker(doc: Doc) -> None:
    _stop_expect(doc)["location"] = {"file": "program.st", "line": "@NOPE"}


def ref_before_capture(doc: Doc) -> None:
    _steps(doc).insert(0, {"observe": "plant", "expect": {"witness": {"Main": {"~ref": "later"}}}})


def uncited_criterion(doc: Doc) -> None:
    doc["criteria"] = ["A999"]


def unknown_task(doc: Doc) -> None:
    _steps(doc).append({"observe": "plant", "expect": {"witness": {"Fast": 1}}})


def no_plant_read(doc: Doc) -> None:
    doc["steps"] = [s for s in _steps(doc) if s.get("observe") != "plant"]


def wrong_column(doc: Doc) -> None:
    location = _stop_expect(doc)["location"]
    assert isinstance(location, dict)
    location["col"] = 4


def unknown_error_code(doc: Doc) -> None:
    _steps(doc)[0]["expect"] = {"success": False, "error": "STALE"}


def marker_absent_in_body_match(doc: Doc) -> None:
    _steps(doc).append({"inspect": "debugInfo", "expect": {"linesByFile": {"program.st": ["@GONE"]}}})


def unknown_arg_symbol(doc: Doc) -> None:
    _steps(doc)[0]["args"] = {"identity": {"~nope": 1}}


def missing_variant_identity(doc: Doc) -> None:
    _steps(doc)[0]["args"] = {"identity": {"~build": "identity:variant-nope.st"}}


def then_continue_on_all_clients(doc: Doc) -> None:
    _steps(doc).append({"client": "*", "event": "stopped", "thenContinue": True})


def ref_before_capture_in_loop(doc: Doc) -> None:
    _steps(doc).insert(0, {"loop": 2, "steps": [{"observe": "plant", "expect": {"witness": {"Main": {"~ref": "zz"}}}}]})


def not_ref_before_capture(doc: Doc) -> None:
    _steps(doc).insert(0, {"observe": "plant", "expect": {"witness": {"Main": {"~notRef": "later"}}}})


def token_user_without_name(doc: Doc) -> None:
    _steps(doc)[0]["args"] = {"token": {"~token": "user:"}}


def client_on_plant_read(doc: Doc) -> None:
    _steps(doc).append({"client": "c2", "observe": "plant", "expect": {"witness": {"Main": {"~ge": 0}}}})


ORACLE_DEFECTS: List[Tuple[str, Callable[[Doc], None], str]] = [
    ("bad stop reason", bad_reason, "steps[3]"),
    ("marker absent from source", missing_marker, "@NOPE"),
    ("~ref with no earlier ~capture", ref_before_capture, "no earlier ~capture"),
    ("criterion the header does not cite", uncited_criterion, "A999"),
    ("task the program does not have", unknown_task, "'Fast'"),
    ("runtime scenario with no plant read", no_plant_read, "read the plant"),
    ("column not at a statement start", wrong_column, "not the start of a statement"),
    ("error code outside ADR-003's catalogue", unknown_error_code, "steps[0]"),
    ("marker absent, outside a line key", marker_absent_in_body_match, "@GONE"),
    ("argument symbol outside the vocabulary", unknown_arg_symbol, "unknown argument symbol"),
    ("identity of a variant that does not exist", missing_variant_identity, "variant-nope.st"),
    ("thenContinue on several streams", then_continue_on_all_clients, "thenContinue"),
    ("~ref in a loop body before its ~capture", ref_before_capture_in_loop, "no earlier ~capture"),
    ("client on a plant read", client_on_plant_read, "client"),
    ("~notRef with no earlier ~capture", not_ref_before_capture, "no earlier ~capture"),
    ("~token user: with no user name", token_user_without_name, "~token"),
]

SOURCE_DEFECTS: List[Tuple[str, Tuple[str, str], str]] = [
    ("witness is not the first statement",
     ("  witness := witness + 1;", "  out_a := x;\n  witness := witness + 1;"), "first statement"),
    ("witness not located DINT", ("witness AT %QD0 : DINT;", "witness : DINT;"), "is not declared"),
    ("header names another corpus", ("(* corpus: plant-io", "(* corpus: plant-oi"), "differs from directory"),
]


def run(root: Path) -> Tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = validate.main(["--root", str(root)])
    return code, buf.getvalue()


class ValidatorRedGreen(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "plant-io", self.tmp / "plant-io")
        for extra in (self.tmp / "plant-io").glob("*.oracle.json"):
            if extra.name != ORACLE:
                extra.unlink()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def test_real_corpus_is_green(self) -> None:
        code, out = run(ROOT)
        self.assertEqual(code, 0, out)

    def test_seeded_copy_is_green_before_seeding(self) -> None:
        code, out = run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_each_seeded_defect_is_red(self) -> None:
        oracle = self.tmp / "plant-io" / ORACLE
        source = self.tmp / "plant-io" / "program.st"
        pristine_oracle, pristine_source = oracle.read_text(), source.read_text()
        for name, mutate, needle in ORACLE_DEFECTS:
            with self.subTest(defect=name):
                doc = json.loads(pristine_oracle)
                mutate(doc)
                oracle.write_text(json.dumps(doc))
                code, out = run(self.tmp)
                self.assertEqual(code, 1, out)
                self.assertIn(needle, out)
        oracle.write_text(pristine_oracle)
        for name, (old, new), needle in SOURCE_DEFECTS:
            with self.subTest(defect=name):
                self.assertIn(old, pristine_source)
                source.write_text(pristine_source.replace(old, new, 1))
                code, out = run(self.tmp)
                self.assertEqual(code, 1, out)
                self.assertIn(needle, out)
        source.write_text(pristine_source)


if __name__ == "__main__":
    unittest.main()
