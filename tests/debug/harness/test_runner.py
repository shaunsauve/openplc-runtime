"""Red/green proof for the scenario runner against the fake endpoint and against no endpoint.

NO-FOLD: harness-instrument (the runner judges a debug endpoint that does not exist yet, unit U4;
its only runtime surface today is the fake below, so these assertions are the instrument's calibration).
Run: python3 -m unittest tests/debug/harness/test_runner.py

See also:
  tests/debug/harness/runner.py - the runner under test (verdicts, trace, results lines).
  tests/debug/harness/fake_endpoint.py - the simulated plant-io runtime and its seeded defects.
  tests/debug/corpus/plant-io/ - the real oracles these runs execute, unmodified.
  tests/debug/harness/fixtures/ - oracles for the 1.1 constructs (clients, symbols, inspect, repeat).
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import runner  # noqa: E402
from fake_endpoint import FakeWorld  # noqa: E402
from socketio_endpoint import SocketIOEndpoint  # noqa: E402

CORPUS = HERE.parent / "corpus"
FIXTURES = HERE / "fixtures"
sys.path.insert(0, str(CORPUS))

import validate  # noqa: E402

PASSING = [
    "plant-io/bp-before-statement",
    "plant-io/cycle-step",
    "plant-io/pause-resume-stop",
    "plant-io/halt-holds-outputs",
    "plant-io/stale-build",
]

# (defect, scenario, 0-based index of the step that must report DIVERGE, text the reason must name)
DEFECTS: List[Tuple[str, str, int, str]] = [
    ("late_stop", "plant-io/cycle-step", 7, "witness.Main"),
    ("late_stop", "plant-io/pause-resume-stop", 3, "within 600"),
    ("wrong_reason", "plant-io/bp-before-statement", 6, "reason"),
    ("missing_continued", "plant-io/pause-resume-stop", 9, "continued"),
    ("zero_in_halt_samples", "plant-io/halt-holds-outputs", 8, "plant@"),
]


# Format 1.1 constructs, each run against the fake (or with no build outputs) and, where a seeded
# defect is named, turned DIVERGE by it: (fixture, defect, build outputs present, verdict,
# 0-based step, text the reason must name).
CONSTRUCTS: List[Tuple[str, Optional[str], bool, str, Optional[int], str]] = [
    ("multi-client", None, True, "PASS", None, "steps matched"),
    ("multi-client", "observer_controls", True, "DIVERGE", 4, "continue.success"),
    ("multi-client", "events_to_first_only", True, "DIVERGE", 3, "count"),
    ("symbolic-args", None, True, "PASS", None, "steps matched"),
    ("symbolic-args", "skip_identity_check", True, "DIVERGE", 0, "initialize.success"),
    ("symbolic-args", "no_bp_limit", True, "DIVERGE", 2, "setBreakpoints.success"),
    ("inspect", None, True, "BLOCKED", 1, "disassembl"),
    ("inspect", "short_debug_info", True, "DIVERGE", 0, "probeCount"),
    ("inspect", None, False, "BLOCKED", 0, "build outputs"),
    ("repeat-batch", None, True, "DIVERGE", 4, "cycleStep.success"),
    ("contains-notref", None, True, "DIVERGE", 6, "sessions"),
    ("notref-same", None, True, "DIVERGE", 3, "capture s"),
]


def _oracle(scenario: str) -> Path:
    corpus, sid = scenario.split("/")
    return CORPUS / corpus / f"{sid}.oracle.json"


def _run_fake(scenario: str, defect: Optional[str] = None) -> runner.RunResult:
    return _run_path(_oracle(scenario), defect)


def _run_path(path: Path, defect: Optional[str], build: bool = True) -> runner.RunResult:
    world = FakeWorld(defects={defect} if defect else set())
    outputs = world.build() if build else runner.UnavailableBuild()
    return runner.run_scenario(path, world.endpoint(), world.plant(), world, outputs)


class RunnerTest(unittest.TestCase):
    def test_correct_fake_passes_real_corpus_scenarios(self) -> None:
        for scenario in PASSING:
            with self.subTest(scenario=scenario):
                result = _run_fake(scenario)
                self.assertEqual(result.verdict.status, "PASS", runner.format_trace(result))
                self.assertEqual(
                    len(result.trace), len(json.loads(_oracle(scenario).read_text())["steps"])
                )

    def test_each_seeded_defect_diverges_on_its_step(self) -> None:
        for defect, scenario, step, needle in DEFECTS:
            with self.subTest(defect=defect, scenario=scenario):
                result = _run_fake(scenario, defect)
                self.assertEqual(result.verdict.status, "DIVERGE", runner.format_trace(result))
                self.assertEqual(result.verdict.step, step, runner.format_trace(result))
                self.assertIn(needle, result.verdict.reason)

    def test_format_1_1_constructs_validate_run_and_catch_their_defects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "plant-io"
            corpus.mkdir()
            for src in ("program.st", "variant-stale.st"):
                shutil.copy(CORPUS / "plant-io" / src, corpus / src)
            for fixture in FIXTURES.glob("*.oracle.json"):
                shutil.copy(fixture, corpus / fixture.name)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = validate.main(["--root", tmp])
            self.assertEqual(code, 0, out.getvalue())
            for fixture, defect, build, status, step, needle in CONSTRUCTS:
                with self.subTest(fixture=fixture, defect=defect, build=build):
                    result = _run_path(corpus / f"{fixture}.oracle.json", defect, build)
                    trace = runner.format_trace(result)
                    self.assertEqual(result.verdict.status, status, trace)
                    self.assertEqual(result.verdict.step, step, trace)
                    self.assertIn(needle, result.verdict.reason, trace)

    def test_sha256_matcher_compares_content_to_a_captured_hash(self) -> None:
        import hashlib  # noqa: PLC0415

        from matchers import match  # noqa: PLC0415

        caps = {"h": hashlib.sha256(b"PROGRAM P\n").hexdigest()}
        self.assertIsNone(match({"~sha256": "h"}, "PROGRAM P\n", caps))
        self.assertIsNotNone(match({"~sha256": "h"}, "PROGRAM Q\n", caps))

    def test_no_endpoint_is_blocked_and_results_are_one_line_per_criterion(self) -> None:
        endpoint = SocketIOEndpoint("https://127.0.0.1:1", token=None, insecure=True)
        oracle = _oracle("plant-io/bp-before-statement")
        result = runner.run_scenario(
            oracle,
            endpoint,
            runner.UnavailablePlant(),
            runner.WallClock(),
            runner.UnavailableBuild(),
        )
        self.assertEqual(result.verdict.status, "BLOCKED")
        self.assertIn("127.0.0.1:1", result.verdict.reason)

        lines = runner.results_lines(result, ran_on="test", commit="0000000")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "runs" / "harness.jsonl"
            runner.append_results(out, lines)
            runner.append_results(out, lines)
            rows = [json.loads(line) for line in out.read_text().splitlines()]
        criteria = json.loads(oracle.read_text())["criteria"]
        self.assertEqual([r["criterion"] for r in rows], criteria * 2)
        self.assertEqual(
            set(rows[0]),
            {"criterion", "verdict", "test", "ran_on", "commit", "repo", "timestamp", "evidence"},
        )
        self.assertEqual(
            rows[0]["test"], "tests/debug/harness/runner.py::plant-io/bp-before-statement"
        )
        self.assertEqual(rows[0]["verdict"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
