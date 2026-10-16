"""Red/green proof for validate.py: the real corpus passes, each seeded defect fails.

NO-FOLD: pure-logic (the validator has no runtime surface; it gates the oracles every harness reads).
Run: python3 -m unittest tests/debug/corpus/test_validate.py

See also:
  tests/debug/corpus/validate.py - the validator under test.
  tests/debug/corpus/plant-io/ - the program and oracle the defects are seeded into.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
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


class BuildRecordTest(unittest.TestCase):
    """A recorded build verdict that no longer describes what it was measured on.

    `build_all.sh` writes `<out>/<name>/build-record.txt`: the compiler head it built with, the
    corpus commit, and a hash of every source whose content the verdict depends on. Nothing read
    those records, so a program that stopped compiling because STruCpp moved was indistinguishable
    from one that never would -- and a `build: expect-fail` header records today's status, not the
    revision it was measured at, so the header silently becomes a lie when the compiler gains the
    missing operator.
    """

    HEAD = "4e24129"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "plant-io", self.tmp / "plant-io")
        for extra in (self.tmp / "plant-io").glob("*.oracle.json"):
            if extra.name != ORACLE:
                extra.unlink()
        self.out = self.tmp / "build" / "debug-corpus"
        (self.out / "plant-io").mkdir(parents=True)
        self._record(("plant-io/program.st",))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _record(self, sources, strucpp: str = HEAD) -> None:
        lines = [f"strucpp {strucpp}", "repo 2936e05"]
        for name in sources:
            data = (self.tmp / name).read_bytes()
            lines.append(f"source {Path(name).name} {hashlib.sha256(data).hexdigest()} {len(data)}")
        (self.out / "plant-io" / "build-record.txt").write_text("\n".join(lines) + "\n")

    def _run_with(self, *argv: str) -> Tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = validate.main(["--root", str(self.tmp), "--build-out", str(self.out), *argv])
        return code, buf.getvalue()

    def test_a_record_that_still_describes_the_sources_is_not_reported(self) -> None:
        code, out = self._run_with()
        self.assertEqual(code, 0, out)
        self.assertNotIn("warning", out)

    def test_a_source_that_moved_since_the_record_is_reported_with_the_compiler_it_was_measured_on(
        self,
    ) -> None:
        # A comment, so what changes is the bytes and nothing else: the check is about the record,
        # not about whether the program still compiles.
        source = self.tmp / "plant-io" / "program.st"
        source.write_text(source.read_text() + "\n(* touched since the build *)\n")
        code, out = self._run_with()
        self.assertEqual(code, 0, out)  # a warning, not a failure: the build has not been re-run
        self.assertIn(f"warning: plant-io: the build recorded at strucpp {self.HEAD} is stale", out)
        self.assertIn("program.st changed", out)
        self.assertIn("recorded at corpus 2936e05", out)
        # --strict is how it becomes a problem.
        code, out = self._run_with("--strict")
        self.assertEqual(code, 1, out)
        self.assertIn("is stale", out)

    def test_a_source_that_is_gone_since_the_record_is_reported(self) -> None:
        self._record(("plant-io/program.st", "plant-io/variant-stale.st"))
        (self.tmp / "plant-io" / "variant-stale.st").unlink()
        _, out = self._run_with()
        self.assertIn("variant-stale.st", out)
        self.assertIn("no longer exists", out)

    def test_a_record_that_names_no_source_at_all_cannot_be_checked_and_says_so(self) -> None:
        self._record(())
        _, out = self._run_with()
        self.assertIn("records no source, so nothing says what it measured", out)

    def test_no_build_directory_is_not_a_warning(self) -> None:
        # The corpus has to validate on a machine that has never built it (a CI job that only runs
        # the checker, a fresh clone), so an absent build directory says nothing at all.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = validate.main(["--root", str(self.tmp), "--build-out", str(self.tmp / "nowhere")])
        self.assertEqual(code, 0)
        self.assertNotIn("warning", buf.getvalue())

    @unittest.skipUnless(shutil.which("git"), "git is needed to make a checkout to compare heads")
    def test_a_compiler_that_has_moved_since_the_record_is_reported(self) -> None:
        compiler = self.tmp / "STruCpp"
        compiler.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@example", "GIT_COMMITTER_EMAIL": "t@example",
               "GIT_AUTHOR_DATE": "2026-01-01T00:00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00"}
        subprocess.run(["git", "init", "-q", str(compiler)], check=True, env=env)
        subprocess.run(["git", "-C", str(compiler), "commit", "-q", "--allow-empty", "-m", "one"],
                       check=True, env=env)
        head = subprocess.run(["git", "-C", str(compiler), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        with unittest.mock.patch.dict(os.environ, {"STRUCPP": str(compiler)}):
            self._record(("plant-io/program.st",), strucpp=head)
            _, out = self._run_with()  # the compiler is the one the record names
            self.assertNotIn("warning", out)
            (compiler / "moved").write_text("STruCpp moved\n")
            subprocess.run(["git", "-C", str(compiler), "add", "-A"], check=True, env=env)
            subprocess.run(["git", "-C", str(compiler), "commit", "-q", "-m", "two"],
                           check=True, env=env)
            _, out = self._run_with()
        self.assertIn(f"warning: the corpus was built with STruCpp {head}, which has moved to", out)


class CoverageIndexTest(unittest.TestCase):
    """`coverage.txt`'s index block is generated from the oracles, and a check says so.

    The file was last written at cd1249d and nothing regenerated it: 56 commits later it named 117
    of the 121 scenarios on disk and omitted exactly the four added since. Its header claimed it was
    "generated from each oracle's criteria" and the generator was in neither repo. So the index block
    is generated now, and everything a generator cannot know -- what a scenario does not check, and
    which criteria no scenario can express -- stays hand-written outside it.
    """

    BEGIN = "<!-- BEGIN generated: tests/debug/corpus/validate.py --write-coverage -->"
    END = "<!-- END generated -->"
    HAND = "# hand-written: a criterion no scenario can express, and why\n# A149 -- nothing does it\n"
    CLAIMS = "A031 tests/debug/harness/runner.py::plant-io/bp-before-statement\nA034 tests/debug/harness/runner.py::plant-io/bp-before-statement\n"

    def setUp(self) -> None:
        # The real layout: the corpus root and the harness are siblings, which is where validate.py
        # looks for coverage.txt when it is not told.
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "corpus"
        self.root.mkdir()
        shutil.copytree(ROOT / "plant-io", self.root / "plant-io")
        for extra in (self.root / "plant-io").glob("*.oracle.json"):
            if extra.name != ORACLE:
                extra.unlink()
        self.path = self.tmp / "harness" / "coverage.txt"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _write(self, block: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(f"# header prose\n\n{self.BEGIN}\n{block}{self.END}\n\n{self.HAND}")

    def _run(self, *argv: str) -> Tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = validate.main(["--root", str(self.root), *argv])
        return code, buf.getvalue()

    def test_a_block_that_matches_the_oracles_is_green(self) -> None:
        self._write(self.CLAIMS)
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertNotIn("warning", out)

    def test_a_block_that_has_not_been_regenerated_is_a_problem(self) -> None:
        self._write("A031 tests/debug/harness/runner.py::plant-io/bp-before-statement\n")
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn("A034", out)  # the criterion the oracles claim and the index does not
        self.assertIn("--write-coverage", out)
        # The whole difference is shown, not just the first thing that differs.
        code, out = self._run()
        self.assertNotIn("A013", out)  # and nothing is invented for a corpus this root does not have

    def test_a_block_that_claims_a_scenario_no_oracle_claims_is_a_problem(self) -> None:
        self._write(self.CLAIMS + "A035 tests/debug/harness/runner.py::plant-io/bp-before-statement\n")
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn("A035", out)

    def test_a_file_with_no_generated_block_at_all_is_a_problem(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("# A# -> the scenarios that cite it\nA034 something\n")
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn("has no generated block", out)

    def test_no_coverage_file_at_all_is_not_a_problem(self) -> None:
        # A corpus with no index file is not wrong; a corpus whose index file has drifted is.
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertNotIn("coverage", out)

    def test_write_coverage_rewrites_the_block_and_touches_nothing_else(self) -> None:
        self._write("A999 tests/debug/harness/runner.py::plant-io/gone\n")
        code, out = self._run("--write-coverage")
        self.assertEqual(code, 0, out)
        text = self.path.read_text()
        self.assertIn(self.CLAIMS, text)
        self.assertNotIn("A999", text)
        self.assertIn("# header prose", text)  # the prose before the block is hand-written too
        self.assertIn(self.HAND, text)  # and so is everything after it
        # A second write changes nothing, so regenerating is idempotent.
        code, out = self._run("--write-coverage")
        self.assertEqual(code, 0, out)
        self.assertEqual(text, self.path.read_text())

    def test_a_scenario_that_discharges_nothing_is_listed_as_a_bisect(self) -> None:
        diag = json.loads((self.root / "plant-io" / ORACLE).read_text())
        diag["scenario"] = "plant-io/bp-tail"
        diag["kind"] = "diagnostic"
        diag["criteria"] = []
        diag["steps"] = diag["steps"][:-1]
        diag["notes"] = ("a bisect of bp-before-statement with its last step cut out\n\n"
                         "Diagnostic of: plant-io/bp-before-statement\nSteps removed: 1")
        (self.root / "plant-io" / "bp-tail.oracle.json").write_text(json.dumps(diag))
        self._write(self.CLAIMS)
        code, out = self._run("--write-coverage")
        self.assertEqual(code, 0, out)
        self.assertIn("plant-io/bp-tail", self.path.read_text())
        self.assertIn("# Scenarios that discharge no criterion", self.path.read_text())
        code, out = self._run()
        self.assertEqual(code, 0, out)  # a bisect is valid; it just earns nothing


class DiagnosticTest(unittest.TestCase):
    """`criteria: []` is a claim of nothing, so it takes a `kind` to say so and a count to prove it.

    It used to be a sentence in `notes` plus a row for every criterion a scenario listed, with no
    check of any kind: a diagnostic could name no scenario, cut nothing out, and still validate.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "all-scopes-types", self.tmp / "all-scopes-types")
        self.path = self.tmp / "all-scopes-types" / "scopes-tail.oracle.json"
        self.pristine = json.loads((ROOT / "all-scopes-types" / "scopes-tail.oracle.json").read_text())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _write(self, mutate: Callable[[Doc], None]) -> None:
        doc = json.loads(json.dumps(self.pristine))
        mutate(doc)
        self.path.write_text(json.dumps(doc), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = validate.main(["--root", str(self.tmp)])
        self.code, self.out = code, buf.getvalue()

    def test_the_corpus_diagnostic_is_green(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = validate.main(["--root", str(self.tmp)])
        self.assertEqual(code, 0, buf.getvalue())

    def test_an_empty_criteria_without_the_kind_is_refused(self) -> None:
        self._write(lambda d: d.update(kind="runtime"))
        self.assertEqual(self.code, 1, self.out)
        self.assertIn("criteria is empty", self.out)
        self.assertIn("diagnostic", self.out)

    def test_a_diagnostic_that_claims_a_criterion_is_refused(self) -> None:
        self._write(lambda d: d.update(criteria=["A088"]))
        self.assertEqual(self.code, 1, self.out)
        self.assertIn("a diagnostic scenario claims", self.out)

    def test_a_diagnostic_that_names_no_scenario_is_refused(self) -> None:
        self._write(lambda d: d.update(notes=d["notes"].replace("Diagnostic of: all-scopes-types/scopes\n", "")))
        self.assertEqual(self.code, 1, self.out)
        self.assertIn("must name the scenario it is a bisect of", self.out)

    def test_a_diagnostic_that_counts_no_removed_step_is_refused(self) -> None:
        self._write(lambda d: d.update(notes=d["notes"].replace("Steps removed: 7", "Steps removed: 0")))
        self.assertEqual(self.code, 1, self.out)
        self.assertIn("removed 0 steps", self.out)

    def test_a_count_that_does_not_match_the_sibling_is_refused(self) -> None:
        # The point of checking a count rather than prose: scopes grows a step and this goes red.
        self._write(lambda d: d.update(notes=d["notes"].replace("Steps removed: 7", "Steps removed: 3")))
        self.assertEqual(self.code, 1, self.out)
        self.assertIn("says it removed 3 step(s)", self.out)
        self.assertIn("which has 16", self.out)
        self.assertIn("this one has 9", self.out)

    def test_a_diagnostic_of_a_scenario_that_does_not_exist_is_refused(self) -> None:
        self._write(lambda d: d.update(
            notes=d["notes"].replace("all-scopes-types/scopes", "all-scopes-types/nope")))
        self.assertEqual(self.code, 1, self.out)
        self.assertIn("which does not exist", self.out)

    def test_a_bisect_that_has_quietly_stopped_being_one_is_refused(self) -> None:
        # The hazard the count exists for: the scenario being bisected grows a step, and the
        # diagnostic silently stops being a cut of it. Prose would still read true; a count does not.
        sibling = self.tmp / "all-scopes-types" / "scopes.oracle.json"
        doc = json.loads(sibling.read_text())
        doc["steps"].append({"observe": "plant", "expect": {"witness": {"Main": 1}}})
        sibling.write_text(json.dumps(doc), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = validate.main(["--root", str(self.tmp)])
        self.assertEqual(code, 1, buf.getvalue())
        self.assertIn("says it removed 7 step(s)", buf.getvalue())
        self.assertIn("which has 17", buf.getvalue())


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
