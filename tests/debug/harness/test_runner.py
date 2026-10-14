"""Red/green proof for the scenario runner against the fake endpoint and against no endpoint.

NO-FOLD: harness-instrument (the runner judges a debug endpoint that does not exist yet, unit U4;
its only runtime surface today is the fake below, so these assertions are the instrument's calibration).
Run: python3 -m unittest tests/debug/harness/test_runner.py

See also:
  tests/debug/harness/runner.py - the runner under test (verdicts, trace, results lines).
  tests/debug/harness/fake_endpoint.py - the simulated plant-io runtime and its seeded defects.
  tests/debug/corpus/plant-io/ - the real oracles these runs execute, unmodified.
"""

from __future__ import annotations

import json
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

PASSING = [
    "plant-io/bp-before-statement",
    "plant-io/cycle-step",
    "plant-io/pause-resume-stop",
    "plant-io/halt-holds-outputs",
]

# (defect, scenario, 0-based index of the step that must report DIVERGE, text the reason must name)
DEFECTS: List[Tuple[str, str, int, str]] = [
    ("late_stop", "plant-io/cycle-step", 7, "witness.Main"),
    ("late_stop", "plant-io/pause-resume-stop", 3, "within 600"),
    ("wrong_reason", "plant-io/bp-before-statement", 6, "reason"),
    ("missing_continued", "plant-io/pause-resume-stop", 9, "continued"),
]


def _oracle(scenario: str) -> Path:
    corpus, sid = scenario.split("/")
    return CORPUS / corpus / f"{sid}.oracle.json"


def _run_fake(scenario: str, defect: Optional[str] = None) -> runner.RunResult:
    world = FakeWorld(defects={defect} if defect else set())
    return runner.run_scenario(_oracle(scenario), world.endpoint(), world.plant(), world)


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

    def test_no_endpoint_is_blocked_and_results_are_one_line_per_criterion(self) -> None:
        endpoint = SocketIOEndpoint("https://127.0.0.1:1", token=None, insecure=True)
        oracle = _oracle("plant-io/bp-before-statement")
        result = runner.run_scenario(
            oracle, endpoint, runner.UnavailablePlant(), runner.WallClock()
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
