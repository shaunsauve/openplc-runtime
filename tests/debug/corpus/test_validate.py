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


def run(root: Path, argv: List[str] = ()) -> Tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = validate.main(["--root", str(root), *argv])
    return code, buf.getvalue()


CLAIMS = "openplc-runtime:tests/debug/corpus/plant-io/bp-before-statement.oracle.json"

# One ACCEPTANCE.md block per criterion the program header cites, as the real file has. The
# `--acceptance` header cross-check needs all of them, so a fixture that names one or two is not a
# fixture for the join alone.
BLOCK = """### {c} — criterion {c}
Covers:    F001
Tier:      MVP
Gate:      none
Method:    gesture — headless-harness
Batch:     b-test
Corpus:    corpus:plant-io
Check:     what is driven
Evidence:  what must be observed
Negative:  —
Red-proof: the seeded defect
Test:      {test}
Ran-on:    as declared
Verdict:   PENDING — UNTESTED

"""


def acceptance(root: Path, **tests: str) -> Path:
    """ACCEPTANCE.md over `plant-io`'s header criteria; `tests` overrides a criterion's `Test:`,
    and a `None` override omits that criterion's block entirely."""
    doc = validate.load_program(root / "plant-io")
    out = ["# Acceptance Criteria\n\n"]
    for c in sorted(doc.criteria):
        test = tests.get(c, CLAIMS if c in ("A034", "A031") else "—")
        if test is None:
            continue
        out.append(BLOCK.format(c=c, test=test))
    path = root / "ACCEPTANCE.md"
    path.write_text("".join(out), encoding="utf-8")
    return path


class JoinTest(unittest.TestCase):
    """The oracle -> ACCEPTANCE.md join (validate.py `join_problems`).

    An oracle's `criteria` list is the claim it earns, and `ACCEPTANCE.md`'s `Test:` is what says
    the claim was paid for. Nothing read both: `acceptance_citations` cross-checks the *program
    header* against the criteria whose `Corpus:` names the directory, and never an oracle's own
    list. So 39 claims named a criterion whose `Test:` was `—` or named other scenarios, one named
    a scenario that does not cite it, and nothing said so.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "plant-io", self.tmp / "plant-io")
        for extra in (self.tmp / "plant-io").glob("*.oracle.json"):
            if extra.name != ORACLE:
                extra.unlink()
        self.oracle = self.tmp / "plant-io" / ORACLE  # claims A034, A031

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _run_with(self, **tests: str) -> Tuple[int, str]:
        return run(self.tmp, ["--acceptance", str(acceptance(self.tmp, **tests))])

    def test_a_claim_the_acceptance_names_is_not_reported(self) -> None:
        code, out = self._run_with()
        self.assertEqual(code, 0, out)
        self.assertNotIn("warning", out)
        self.assertIn("OK: 1 programs, 1 scenarios, 0 problems", out)

    def test_a_claim_whose_test_is_a_dash_is_a_warning_not_a_failure(self) -> None:
        code, out = self._run_with(A031="—")
        self.assertEqual(code, 0, out)  # the corpus stays green while the number is not zero
        self.assertIn("warning: ", out)
        self.assertIn("claims A031, whose ACCEPTANCE Test: is —", out)
        self.assertIn("OK: 1 programs, 1 scenarios, 0 problems", out)

    def test_a_claim_whose_test_names_other_scenarios_is_reported(self) -> None:
        _, out = self._run_with(A031="openplc-runtime:tests/debug/corpus/plant-io/elsewhere.oracle.json")
        self.assertIn("claims A031, whose ACCEPTANCE Test: names other scenarios", out)
        self.assertIn("elsewhere.oracle.json", out)

    def test_a_claim_whose_test_names_no_corpus_scenario_is_reported_as_such(self) -> None:
        _, out = self._run_with(A034="openplc-debug:tools/adr_review.py")
        self.assertIn("claims A034, whose ACCEPTANCE Test: names no corpus scenario", out)
        self.assertIn("adr_review.py", out)

    def test_a_claim_no_criterion_block_names_at_all_is_reported(self) -> None:
        # Defensive branch: `--acceptance` already refuses a header criterion nothing cites, so this
        # can only be reached with a document that is not the real one. It still has to say so.
        _, out = run(self.tmp, ["--acceptance", str(acceptance(self.tmp, **{"A031": None}))])
        self.assertIn("claims A031", out)

    def test_strict_turns_every_warning_into_a_problem(self) -> None:
        code, out = self._run_with(A031="—")
        self.assertEqual(code, 0, out)
        code, out = run(self.tmp, ["--acceptance", str(acceptance(self.tmp, A031="—")), "--strict"])
        self.assertEqual(code, 1, out)
        self.assertIn("claims A031", out)
        self.assertIn("FAIL: 1 programs, 1 scenarios, 1 problems", out)
        # One switch, both directions: A033 names this scenario but the scenario does not cite it.
        doc = acceptance(self.tmp, A033=CLAIMS)
        code, out = run(self.tmp, ["--acceptance", str(doc), "--strict"])
        self.assertEqual(code, 1, out)
        self.assertIn("A033's Test: names plant-io/bp-before-statement, which does not cite it", out)
        # And --strict with nothing to join against is still green.
        code, out = run(self.tmp, ["--strict"])
        self.assertEqual(code, 0, out)

    def test_a_test_naming_a_scenario_that_does_not_exist_is_reported(self) -> None:
        # One slip, both directions: the criterion's Test: points at an oracle nobody wrote, and the
        # scenario that claims it is therefore claiming nothing.
        _, out = self._run_with(A034="openplc-runtime:tests/debug/corpus/plant-io/nowhere.oracle.json")
        self.assertIn("A034's Test: names plant-io/nowhere, which does not exist", out)
        self.assertIn("claims A034, whose ACCEPTANCE Test: names other scenarios", out)


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
