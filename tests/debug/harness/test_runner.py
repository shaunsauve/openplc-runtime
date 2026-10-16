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
import threading
import unittest
import unittest.mock
from pathlib import Path
from typing import List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import runner  # noqa: E402
from fake_endpoint import FakeWorld  # noqa: E402
from interfaces import Blocked  # noqa: E402
from netfault import NetFault  # noqa: E402
from socketio_endpoint import SocketIOEndpoint, SocketIOSession  # noqa: E402

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


class DeployTest(unittest.TestCase):
    """The setup keys the driver deploys are checked, not assumed (T-VER-HLTDEPLOY,
    T-VER-BUILDDEPLOY, T-VER-HALTMODE): prepare() takes a scenario only when what was deployed
    is what the scenario declared, and the handshake refuses a runtime whose own report
    disagrees. Every refusal here is a real one a run can hit."""

    @staticmethod
    def _endpoint(**kwargs) -> SocketIOEndpoint:
        endpoint = SocketIOEndpoint("https://127.0.0.1:1", token=None, insecure=True, **kwargs)
        endpoint._probe = lambda: None  # the TCP liveness check; these tests never open a socket
        return endpoint

    @staticmethod
    def _prepare(endpoint: SocketIOEndpoint, setup: dict) -> None:
        endpoint.prepare(setup)

    def _blocked(self, endpoint: SocketIOEndpoint, setup: dict, needle: str) -> str:
        with self.assertRaises(Blocked) as caught:
            self._prepare(endpoint, setup)
        self.assertIn(needle, str(caught.exception))
        return str(caught.exception)

    # -- haltPolicy / safeValues

    def test_halt_policy_is_refused_unless_the_core_reports_the_one_declared(self) -> None:
        declared = json.loads(_oracle("plant-io/halt-holds-outputs").read_text())["setup"]
        self.assertEqual(declared["haltPolicy"], "hold")
        self._blocked(
            self._endpoint(deployed_halt_policy="none"), declared, "no halt policy was deployed"
        )
        self._blocked(
            self._endpoint(deployed_halt_policy="safe:0"), declared, "the core reports it loaded"
        )
        # What was deployed is what was declared: accepted.
        self._prepare(self._endpoint(deployed_halt_policy="hold:0"), declared)

    def test_safe_values_must_be_the_ones_the_core_configured(self) -> None:
        declared = json.loads(_oracle("plant-io/force-while-halted-safe").read_text())["setup"]
        self.assertEqual((declared["haltPolicy"], len(declared["safeValues"])), ("safe", 4))
        self._blocked(
            self._endpoint(deployed_halt_policy="safe:2"), declared, "names 4 output(s)"
        )
        self._prepare(self._endpoint(deployed_halt_policy="safe:4"), declared)

    # -- build

    def test_probe_off_build_is_refused_unless_it_was_deployed(self) -> None:
        declared = json.loads(_oracle("plant-io/probe-off").read_text())["setup"]
        self.assertEqual(declared["build"], {"probes": False})
        self._blocked(
            self._endpoint(deployed_build={"probes": "on"}), declared, "probe-off build"
        )
        endpoint = self._endpoint(deployed_build={"probes": "off"})
        self._prepare(endpoint, declared)
        self.assertIs(endpoint.requires_probes, False)

    def test_editor_build_needs_the_origin_map_it_was_written_against(self) -> None:
        declared = json.loads(_oracle("fbd-sample/origins").read_text())["setup"]
        self.assertEqual(declared["build"], {"path": "editor"})
        self._blocked(
            self._endpoint(deployed_build={"origins": "0"}), declared, "carries no origins"
        )
        self._prepare(self._endpoint(deployed_build={"origins": "3"}), declared)
        # The other direction too: a build with origins is not the cli build.
        cli = json.loads(_oracle("print-builtin/with-and-without-session").read_text())["setup"]
        self.assertEqual(cli["build"], {"path": "cli"})
        self._blocked(
            self._endpoint(deployed_build={"origins": "2"}), cli, "needs a build with no origins"
        )

    def test_the_two_undeclarable_build_keys_keep_their_refusal(self) -> None:
        control = json.loads(_oracle("plant-io/control-compiled-out").read_text())["setup"]
        self.assertEqual(control["build"], {"control": False})
        # ADR-005 D6.2's OPENPLC_DEBUG_CONTROL is in no cmake option and no source, so the
        # compile-out build is not something the harness can produce.
        self._blocked(self._endpoint(), control, "compile-out")
        # `path: library` names a producer the payload builder does not vary.
        self._blocked(
            self._endpoint(deployed_build={"probes": "on"}),
            {"build": {"path": "library"}},
            "not something the harness can deploy",
        )

    # -- the handshake, which is the runtime's own report

    @staticmethod
    def _session(endpoint: SocketIOEndpoint) -> "SocketIOSession":
        session = object.__new__(SocketIOSession)  # no socket: these tests call the checks only
        session.endpoint = endpoint
        session.non_stop = False
        session.halt_mode_deployed = False
        session.sent: List[tuple] = []
        return session

    @staticmethod
    def _reply(**caps) -> dict:
        return {"success": True, "body": {"capabilities": caps}}

    def test_the_handshake_refuses_a_runtime_whose_probe_build_is_not_the_one_deployed(self) -> None:
        endpoint = self._endpoint(deployed_build={"probes": "off"})
        self._prepare(endpoint, {"build": {"probes": False}})
        session = self._session(endpoint)
        # A probe build: not the one this scenario was written against.
        with self.assertRaises(Blocked) as caught:
            session._check_capabilities(self._reply(probes=True))
        self.assertIn("this runtime advertises the probes capability", str(caught.exception))
        # The probe-off build it declared: accepted.
        session._check_capabilities(self._reply(probes=False))
        # And the other way round, a scenario that declares a probe build.
        endpoint = self._endpoint(deployed_build={"probes": "on"})
        self._prepare(endpoint, {"build": {"probes": True}})
        session = self._session(endpoint)
        with self.assertRaises(Blocked) as caught:
            session._check_capabilities(self._reply(probes=False))
        self.assertIn("does not advertise the probes capability", str(caught.exception))
        session._check_capabilities(self._reply(probes=True))

    def test_non_stop_is_refused_when_the_core_does_not_advertise_it(self) -> None:
        endpoint = self._endpoint()
        self._prepare(endpoint, {"haltMode": "nonStop"})
        session = self._session(endpoint)
        with self.assertRaises(Blocked) as caught:
            session._check_capabilities(self._reply(control=True))
        self.assertIn("all-stop only", str(caught.exception))
        # Advertised: the mode is put in place with the request, before step 0.
        session = self._session(endpoint)
        replies = [self._reply(control=True, nonStop=True), {"success": True}]
        session.request = lambda command, args: replies.pop(0)  # type: ignore[method-assign]
        session._deploy_halt_mode()

    def test_all_stop_asks_a_core_that_can_be_non_stop_and_notices_a_refusal(self) -> None:
        allstop = self._endpoint()
        self._prepare(allstop, {"haltMode": "allStop"})
        session = self._session(allstop)
        session.request = lambda command, args: {"success": True}  # type: ignore[method-assign]
        # A core that cannot be non-stop: all-stop is what it gives, so nothing is asked.
        session.non_stop = False
        session._deploy_halt_mode()
        # A core that can: the harness asks, so a session is never all-stop by accident.
        session = self._session(allstop)
        asked: List[tuple] = []
        session.request = lambda c, a: (asked.append((c, a)) or {"success": True})  # type: ignore
        session.non_stop = True
        session._deploy_halt_mode()
        self.assertEqual(asked, [("setHaltMode", {"mode": "allStop"})])
        # And a refusal is a disagreement, not a run.
        session = self._session(allstop)
        session.request = lambda c, a: {  # type: ignore[method-assign]
            "success": False,
            "error": {"code": "UNSUPPORTED", "message": "not served"},
        }
        session.non_stop = True
        with self.assertRaises(Blocked) as caught:
            session._deploy_halt_mode()
        self.assertIn("refused setHaltMode", str(caught.exception))


class InjectTest(unittest.TestCase):
    """What each fault kind of F154 does, and what the ones without a seam still say.

    The rule these hold: a kind either acts on a seam the harness owns or refuses by name. None
    of them may answer as though it had injected something, because that turns a loud BLOCK into
    a silent wrong answer (a `rest` call to the gateway's stub callback answers 200 to a start it
    never made, which passes an oracle's status matcher).
    """

    @staticmethod
    def _endpoint(**kwargs) -> SocketIOEndpoint:
        endpoint = SocketIOEndpoint("https://127.0.0.1:1", token=None, insecure=True, **kwargs)
        endpoint._probe = lambda: None
        return endpoint

    def test_a_kind_with_no_seam_refuses_by_name_and_says_what_would_have_to_change(self) -> None:
        endpoint = self._endpoint()
        for kind in ("rest", "upload"):
            with self.subTest(kind=kind):
                with self.assertRaises(Blocked) as caught:
                    endpoint.inject(kind, {})
                self.assertIn(f"inject {kind}", str(caught.exception))
        with self.assertRaises(Blocked) as caught:
            endpoint.inject("rest", {})
        self.assertIn("webserver/app.py", str(caught.exception))
        with self.assertRaises(Blocked) as caught:
            endpoint.inject("upload", {})
        self.assertIn("handle_upload_file", str(caught.exception))

    def test_a_kind_nobody_named_is_refused_too(self) -> None:
        with self.assertRaises(Blocked) as caught:
            self._endpoint().inject("meltdown", {})
        self.assertIn("nothing was injected", str(caught.exception))

    def test_plugin_run_control_is_the_plants_own_hook_and_nothing_else(self) -> None:
        asked = []

        class Plant:
            def run_control(self, action: str, task: str) -> bool:
                asked.append((action, task))
                return True

        endpoint = self._endpoint(plant=Plant())
        self.assertIs(endpoint.inject("plugin", {"name": "reference-plant", "action": "hold",
                                                 "task": "Main"}), True)
        self.assertEqual(asked, [("hold", "Main")])
        # A product plugin's own action is not a fault the harness injects.
        with self.assertRaises(Blocked) as caught:
            endpoint.inject("plugin", {"name": "vpp-switch", "action": "run"})
        self.assertIn("no plugin named 'vpp-switch'", str(caught.exception))

    def test_the_link_faults_change_the_link_and_answer_what_it_now_is(self) -> None:
        net = NetFault("127.0.0.1", 9)  # a port nothing answers: nothing is spliced through yet
        try:
            endpoint = self._endpoint(net=net)
            self.assertEqual(
                endpoint.inject("delayNetwork", {"rttMs": 300, "jitterMs": 20}),
                {"rttMs": 300.0, "jitterMs": 20.0, "drops": 0},
            )
            self.assertEqual(endpoint.inject("restoreNetwork", {}),
                             {"rttMs": 0.0, "jitterMs": 0.0, "drops": 0})
            endpoint.inject("dropNetwork", {})
            self.assertEqual(net.shape()["drops"], 1)
            with self.assertRaises(Blocked) as caught:
                endpoint.inject("delayNetwork", {"rttMs": "soon"})
            self.assertIn("rttMs must be a number", str(caught.exception))
        finally:
            net.close()

    def test_the_link_faults_refuse_when_there_is_no_link_splice(self) -> None:
        with self.assertRaises(Blocked) as caught:
            self._endpoint().inject("dropNetwork", {})
        self.assertIn("no link splice", str(caught.exception))

    def test_a_write_names_the_file_it_wrote_and_reads_it_back(self) -> None:
        endpoint = self._endpoint()
        record = {"kind": "warm", "md5": "0", "version": 99, "writer": "upload"}
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch("socketio_endpoint._runtime_cwd", return_value=tmp):
                result = endpoint.inject("writeFile", {"path": "core/retain_prev/record.json",
                                                       "content": record})
            self.assertEqual(result["readBack"], record)
            self.assertEqual(result["path"], str(Path(tmp) / "core/retain_prev/record.json"))
        # A path out of the runtime's data directory is not one the harness may write.
        with self.assertRaises(Blocked) as caught:
            endpoint.inject("writeFile", {"path": "/etc/passwd", "content": record})
        self.assertIn("data directory", str(caught.exception))

    def test_signalling_a_process_that_is_not_there_says_so_by_name(self) -> None:
        if Path("/proc").is_dir():  # the runtime processes are only findable on Linux
            with self.assertRaises(Blocked) as caught:
                self._endpoint().inject("sigstopRuntime", {"process": "webserver"})
            self.assertIn("webserver/app.py", str(caught.exception))
        with self.assertRaises(Blocked) as caught:
            self._endpoint().inject("sigstopRuntime", {"process": "postgres"})
        self.assertIn("is not a runtime process", str(caught.exception))

    @staticmethod
    def _stream(sent: List[str]) -> SocketIOSession:
        """A session whose transport only records what it was given."""

        class Stream(SocketIOSession):  # pylint: disable=super-init-not-called
            def __init__(self) -> None:
                self.seq = 0
                self.lock = threading.Lock()
                self.pending: dict = {}

            def _raw_frame(self, text: str) -> None:
                sent.append(text)

        return Stream()

    def test_a_raw_frame_is_a_message_document_the_gateway_parses_and_the_harness_never_sends(self) -> None:
        sent: List[str] = []
        stream = self._stream(sent)
        # A message the gateway cannot read is refused with no seq to correlate on
        # (gateway.py `_failure` with request_seq=None), so only that one answer is uncorrelated.
        stream.uncorrelated.put({"success": False, "error": {"code": "BAD_REQUEST"}})
        self.assertEqual(stream.inject_frame("rawFrame", {"frame": "truncated"})["error"]["code"],
                         "BAD_REQUEST")
        # `invalid` is a whole document the gateway reads the seq out of, so it refuses correlated
        # and the harness must wait for that seq rather than for any response at all.
        threading.Timer(0.2, lambda: stream.pending[2].put(
            {"success": False, "error": {"code": "BAD_REQUEST"}})).start()
        self.assertEqual(stream.inject_frame("rawFrame", {"frame": "invalid"})["error"]["code"],
                         "BAD_REQUEST")
        # The wire form is a Socket.IO event whose argument is the message document, so the fault
        # lands in the gateway's own parser and not in the framing above it.
        for text, needle in zip(sent, ('"command":', '"type":"notice"')):
            self.assertTrue(text.startswith("42/api/debug-session,["), text[:40])
            name, document = json.loads(text.split(",", 1)[1])
            self.assertEqual(name, "msg")
            self.assertIn(needle, document)

    def test_an_oversize_frame_waits_for_its_own_refusal_and_a_garbage_burst_for_nothing(self) -> None:
        sent: List[str] = []
        stream = self._stream(sent)
        # A message over P6 is refused correlated: the refusal carries the seq it refused, so the
        # harness waits for that seq rather than for any response at all.
        threading.Timer(0.2, lambda: stream.pending[1].put(
            {"success": False, "error": {"code": "LIMIT_EXCEEDED"}})).start()
        reply = stream.inject_frame("rawFrame", {"frame": "oversize", "bytes": 4096})
        self.assertEqual(reply["error"]["code"], "LIMIT_EXCEEDED")
        self.assertEqual(len(json.loads(json.loads(sent[0].split(",", 1)[1])[1]
                                        )["arguments"]["pad"]), 4096)
        # Frames that are not messages answer nothing, and the harness does not wait for one.
        self.assertEqual(stream.inject_frame("rawFrame", {"frame": "garbage", "count": 3, "bytes": 8}),
                         {"frames": 3, "bytes": 24})
        self.assertEqual(sent[-3:], ["!" * 8] * 3)
        # A burst states only its count: the frame size stays small rather than becoming the 2 MiB
        # an oversize message asks for, which is 2 GB of garbage at a thousand frames.
        stream.inject_frame("rawFrame", {"frame": "garbage", "count": 2})
        self.assertEqual([len(t) for t in sent[-2:]], [64, 64])

    def test_a_frame_it_does_not_know_is_refused_rather_than_approximated(self) -> None:
        with self.assertRaises(Blocked) as caught:
            self._stream([]).inject_frame("rawFrame", {"frame": "sideways"})
        self.assertIn("is not a frame this harness builds", str(caught.exception))


class RunnerTest(unittest.TestCase):
    def test_correct_fake_passes_real_corpus_scenarios(self) -> None:
        for scenario in PASSING:
            with self.subTest(scenario=scenario):
                result = _run_fake(scenario)
                self.assertEqual(result.verdict.status, "PASS", runner.format_trace(result))
                # Index -1 is the setup row (what the harness deployed), not an oracle step.
                self.assertEqual(
                    len([t for t in result.trace if t.index >= 0]),
                    len(json.loads(_oracle(scenario).read_text())["steps"]),
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
