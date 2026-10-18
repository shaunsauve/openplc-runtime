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
from interfaces import Blocked, PlantSample  # noqa: E402
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
    ("stop_at_wrong_line_first", "plant-io/bp-before-statement", 3, "stopped.location.line"),
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

            def _send_frame(self, text: str) -> str:
                sent.append(text)
                return ""

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

    def test_a_burst_that_takes_the_transport_with_it_is_named_at_the_burst(self) -> None:
        """A burst answers nothing by design, so the injection is what says whether the link
        survived it: A087's evidence wants a valid request afterwards to work."""
        sent: List[str] = []
        stream = self._stream(sent)
        gone: List[str] = []

        def transport_gone(timeout_s: float) -> str:
            del timeout_s
            return gone[0] if gone else ""

        stream._transport_gone = transport_gone  # type: ignore[method-assign]
        self.assertEqual(stream.inject_frame("rawFrame", {"frame": "garbage", "count": 2})["frames"], 2)
        gone.append("Connection closed: 1002")
        with self.assertRaises(Blocked) as caught:
            stream.inject_frame("rawFrame", {"frame": "garbage", "count": 1000})
        message = str(caught.exception)
        self.assertIn("1000 frames of 64 bytes", message)
        self.assertIn("A087's burst is not refused, it ends the stream", message)
        # The burst can also take the transport down between two of its own frames, which used
        # to escape as an OSError out of the send rather than as an injection that did something.
        gone.clear()
        stream._send_frame = lambda text: "OSError: [Errno 9] Bad file descriptor"  # type: ignore[method-assign]
        with self.assertRaises(Blocked) as caught:
            stream.inject_frame("rawFrame", {"frame": "garbage", "count": 1000})
        self.assertIn("frame 1 of 1000", str(caught.exception))


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


# ---------------------------------------------------------------- the identifier fold


# (the oracle's spelling, the runtime's, do the two name the same type?). A fold may only erase a
# difference the specification declares insignificant: case is declared (IEC 61131-3 6.1.2, pm
# ruling 13) and the whitespace between two tokens is nothing anyone can name as significant, so
# those fold. Nothing else does -- least of all a bound, which is why this table exists: the
# tokeniser the fold used to run on could not see a leading digit, so every width and every array
# bound tokenised away and `STRING` matched `STRING[80]`. A112 is a refusal to write past a bounded
# STRING's width, so an oracle that cannot tell STRING[8] from STRING[80] cannot falsify it.
TYPE_FOLDS: List[Tuple[str, str, bool]] = [
    ("Seg", "SEG", True),
    ("Mode", "mode", True),
    ("STRING[8]", "STRING[8]", True),
    ("ARRAY[0..1] OF Seg", "array[0..1] of SEG", True),
    ("ARRAY[1..4] OF INT", "array[1..4] of int", True),
    ("ARRAY[0..1, 0..2] OF Point", "ARRAY[0..1,0..2] of POINT", True),
    # -- and the differences nothing declares insignificant:
    ("INT", "DINT", False),
    ("Seg", "Point", False),
    ("STRING", "STRING[8]", False),
    ("STRING[8]", "STRING", False),
    ("STRING[8]", "STRING[80]", False),
    ("STRING[80]", "STRING[8]", False),
    ("STRING", "STRING[80]", False),
    ("ARRAY[0..1] OF Seg", "ARRAY[0..9] OF Seg", False),
    ("ARRAY[0..9] OF Seg", "ARRAY[0..1] OF Seg", False),
    ("ARRAY[1..4] OF INT", "ARRAY[1..40] OF INT", False),
    ("ARRAY[1..4] OF INT", "ARRAY[1..4] OF DINT", False),
    ("ARRAY[0..1, 0..2] OF Point", "ARRAY[0..1, 0..9] OF Point", False),
]


def _type_judged(want: str, got: str) -> bool:
    """What the runner does with a `type`: fold the runtime's spelling, then compare exactly.

    The same two calls `_check_response` and `_check_event` make, so a fold that erases too much is
    caught here rather than only by whichever oracle happens to be wrong about a width.
    """
    from matchers import fold_identifiers, match  # noqa: PLC0415

    folded = fold_identifiers({"type": want}, {"type": got})
    return match({"type": want}, folded, {}) is None


class TypeFoldTest(unittest.TestCase):
    """What the `type` fold may and may not erase (matchers.py `_fold_type`)."""

    def test_a_type_folds_only_on_case_and_the_whitespace_between_tokens(self) -> None:
        for want, got, folds in TYPE_FOLDS:
            with self.subTest(oracle=want, runtime=got):
                self.assertEqual(_type_judged(want, got), folds)

    def test_the_refusal_says_what_it_saw(self) -> None:
        # A width that does not match is a mismatch at the path that carries it, so the ledger
        # says which type disagreed rather than only that some expectation did.
        from matchers import fold_identifiers, match  # noqa: PLC0415

        folded = fold_identifiers({"type": "STRING[8]"}, {"type": "STRING[80]"})
        found = match({"type": "STRING[8]"}, folded, {})
        self.assertIsNotNone(found)
        self.assertEqual(found.path, "$.type")  # type: ignore[union-attr]
        assert found is not None
        self.assertIn("STRING[80]", found.describe())

    def test_a_contains_over_records_folds_case_and_still_refuses_a_different_bound(self) -> None:
        # `~contains` folds each element itself, so a record inside a list is compared the way a
        # record at the top of an expectation is -- which is also how a bound would slip through it.
        from matchers import match  # noqa: PLC0415

        def contains(want: str, got: str) -> bool:
            return match({"~contains": {"type": want}}, [{"type": got}], {}) is None

        self.assertTrue(contains("ARRAY[0..1] OF Seg", "array[0..1] of SEG"))
        self.assertTrue(contains("Mode", "MODE"))
        self.assertFalse(contains("STRING[8]", "STRING[80]"))
        self.assertFalse(contains("ARRAY[0..1] OF Seg", "ARRAY[0..9] OF Seg"))
        self.assertFalse(contains("STRING", "STRING[8]"))

    def test_an_identifier_key_folds_case_and_nothing_else_either(self) -> None:
        # `pou`, `instance`, `name` and `path` are identifiers the specification folds for case and
        # for nothing else, so the same rule holds there: a name that differs by more than case is
        # a different name, including when the name carries a bound.
        from matchers import fold_identifiers, match  # noqa: PLC0415

        def judged(want: str, got: str) -> bool:
            folded = fold_identifiers({"path": want}, {"path": got})
            return match({"path": want}, folded, {}) is None

        self.assertTrue(judged("PROG0.segs[0]", "prog0.SEGS[0]"))
        self.assertFalse(judged("PROG0.segs[0]", "PROG0.segs[1]"))


# ---------------------------------------------------------------- the event stream


class _ScriptedSession:
    """A stream that replays `events` and answers nothing; no socket, no endpoint."""

    def __init__(self, events: List[dict]) -> None:
        self.queue = list(events)

    def events(self) -> List[dict]:
        out, self.queue = self.queue, []
        return out

    def request(self, command: str, args: dict) -> dict:
        return {"success": True, "body": {}}

    def close(self) -> None:
        pass

    def kill(self) -> None:
        pass


class _ScriptedEndpoint:
    description = "scripted"

    def __init__(self, events: List[dict]) -> None:
        self.events = list(events)

    def open_session(self):
        return _ScriptedSession(self.events)


class _VirtualClock:
    """Time passes only when the runner polls, so a `withinMs` bound costs no wall time."""

    def __init__(self) -> None:
        self.now = 0.0

    def now_ms(self) -> float:
        return self.now

    def sleep_ms(self, ms: float) -> None:
        self.now += max(ms, 0.0)


def _event(name: str, body: dict) -> dict:
    return {"event": name, "body": body}


def _at(when_ms: float, name: str, body: dict) -> dict:
    """An event the endpoint releases once its clock has reached `when_ms`."""
    return {"at": when_ms, "event": name, "body": body}


class _TimedSession:
    """A session whose events become available at the scheduled clock reading, so a test can put
    an event at 200 ms or at 15 s without spending 15 s."""

    def __init__(self, pending: List[dict], clock) -> None:
        self.pending = list(pending)
        self.clock = clock
        self.inbox: List[dict] = []

    def request(self, command: str, args: dict) -> dict:
        return {"success": True, "body": {}}

    def events(self) -> List[dict]:
        ready = [e for e in self.pending if e["at"] <= self.clock.now_ms()]
        self.pending = [e for e in self.pending if e["at"] > self.clock.now_ms()]
        self.inbox.extend(ready)
        out, self.inbox = self.inbox, []
        return out

    def close(self) -> None:
        pass

    def kill(self) -> None:
        pass


class _CountingSession(_TimedSession):
    """A timed session that counts the drains, so a row can measure what a wait COST rather than
    what it saw."""

    def __init__(self, clock) -> None:
        super().__init__([], clock)
        self.drains = 0

    def events(self) -> List[dict]:
        self.drains += 1
        return super().events()


class _TimedEndpoint:
    description = "timed"

    def __init__(self, events: List[dict], clock, session=None) -> None:
        self.events = list(events)
        self.clock = clock
        self.session = session

    def open_session(self):
        if self.session is not None:
            return self.session
        return _TimedSession(self.events, self.clock)


class _ScriptedPlant:
    """A plant that answers successive samples with the given virtual-time readings, in order,
    the last one repeated."""

    def __init__(self, vt_ms: List[float], tasks: Optional[List[str]] = None) -> None:
        self.vt = list(vt_ms)
        self.tasks = tasks or ["Main"]
        self.read = 0

    def prepare(self, plant_setup: dict) -> None:
        pass

    def set_inputs(self, values) -> None:
        pass

    def fail_exchanges(self, n: int) -> None:
        raise Blocked("scripted plant cannot fail exchanges")

    def sample(self):
        value = self.vt[min(self.read, len(self.vt) - 1)]
        self.read += 1
        return PlantSample(0.0, {t: 1 for t in self.tasks}, {t: value for t in self.tasks}, {})

    def history(self, since_ms: float):
        return []


class EventStreamTest(unittest.TestCase):
    """`_next_event`: an event the oracle did not ask for is not a divergence (where the step
    means a matching event), and a stream that never sends the one it asked for still fails.
    Nothing tested this, which is how two loosening commits and a fold that erased every bound
    landed in a row.

    Every call here states which reading the row is about, because `first` is no longer a default
    inside the runner: `_step_event` resolves a step's reading with `firstness.takes_first` and
    passes it down. `False` is the reading for every name except `stopped` (StopFirstnessTest
    covers the other one), and these rows are about a stream that queues several events of a name.
    """

    @staticmethod
    def _run(events: List[dict]):
        run = object.__new__(runner._Run)  # the plumbing only; no oracle, no endpoint, no socket
        run.doc = {}
        run.corpus = "plant-io"
        run.table = {}
        run.endpoint = _ScriptedEndpoint(events)  # type: ignore[assignment]
        run.plant = runner.UnavailablePlant()  # type: ignore[assignment]
        run.clock = _VirtualClock()  # type: ignore[assignment]
        run.build = runner.UnavailableBuild()  # type: ignore[assignment]
        run.setup = {}
        run.captures = {}
        run.streams = {}
        run.arrivals = 0
        run.last_witness = None
        run.last_plant_ms = 0.0
        run.last_vt = None
        run.step_end_ms = 0.0
        return run

    def test_an_earlier_event_that_does_not_match_is_skipped_not_failed(self) -> None:
        # ADR-005 D5.3: `forces` is sent on every change, so the force that was added is queued
        # before the one that expired. Taking the oldest made A116 read the wrong force.
        run = self._run([
            _event("forces", {"list": [{"name": "PW", "state": "added"}]}),
            _event("forces", {"list": [{"name": "PW", "state": "expired"}]}),
        ])
        got, problem, _at = run._next_event(
            "main", "forces", {"body": {"list": [{"name": "PW", "state": "expired"}]}}, 500.0, False
        )
        self.assertIsNone(problem)
        self.assertEqual(got["body"], {"list": [{"name": "PW", "state": "expired"}]})

    def test_an_event_of_another_name_is_never_consumed(self) -> None:
        run = self._run([_event("log", {"line": "x"}), _event("stopped", {"reason": "breakpoint"})])
        stream = run._stream("main")
        run._drain()
        self.assertEqual([e["event"] for _, e, _at in stream.inbox], ["log", "stopped"])
        got, problem, _at = run._next_event(
            "main", "stopped", {"reason": "breakpoint"}, 500.0, True
        )
        self.assertIsNone(problem)
        self.assertEqual(got["body"], {"reason": "breakpoint"})
        self.assertEqual([e["event"] for _, e, _at in stream.inbox], ["log"])

    def test_a_stream_that_never_sends_what_was_asked_for_still_fails(self) -> None:
        # The skip must not become a way to pass: with nothing matching, the earliest event comes
        # back with its own problem so the verdict names what actually arrived.
        run = self._run([_event("forces", {"list": [{"name": "PW", "state": "added"}]})])
        got, problem, _at = run._next_event(
            "main", "forces", {"body": {"list": [{"name": "PW", "state": "expired"}]}}, 50.0, False
        )
        self.assertEqual(got["body"], {"list": [{"name": "PW", "state": "added"}]})
        assert problem is not None
        self.assertIn("not equal", problem.reason)

        run = self._run([_event("forces", {"list": [{"name": "PW", "state": "added"}]})])
        with self.assertRaises(runner.Diverge) as caught:
            run._step_event({"event": "forces", "withinMs": 50,
                             "expect": {"body": {"list": [{"name": "PW", "state": "expired"}]}}})
        # Something did arrive, so the failure names what arrived rather than the timeout.
        self.assertIn('observed "added"', caught.exception.reason)

    def test_a_stream_with_no_event_at_all_is_blocked_by_the_step_not_by_a_hang(self) -> None:
        run = self._run([])
        with self.assertRaises(runner.Diverge) as caught:
            run._step_event({"event": "stopped", "withinMs": 50, "expect": {"reason": "breakpoint"}})
        self.assertIn("no 'stopped' event within 50 ms", caught.exception.reason)
        self.assertLessEqual(run.clock.now_ms(), 50.0)

    def test_an_event_step_with_no_expectation_takes_the_first_that_arrives(self) -> None:
        run = self._run([_event("heartbeat", {"n": 1})])
        got, problem, _at = run._next_event("main", "heartbeat", {}, 500.0, False)
        self.assertIsNone(problem)
        self.assertEqual(got["body"], {"n": 1})

    def test_a_count_step_is_not_failed_by_an_event_that_does_not_match(self) -> None:
        # `count` says "N of these arrive": a stream that sends none is a count of 0, checked.
        run = self._run([_event("log", {"line": "unrelated"})])
        detail = run._step_event({"event": "log", "count": 0, "withinMs": 50,
                                  "expect": {"body": {"line": "wanted"}}})
        self.assertEqual(detail, "log")
        run = self._run([_event("log", {"line": "unrelated"})])
        with self.assertRaises(runner.Diverge) as caught:
            run._step_event({"event": "log", "count": 1, "withinMs": 50,
                             "expect": {"body": {"line": "wanted"}}})
        self.assertIn("log.count", caught.exception.reason)


def _corpus_step(scenario: str, event: str) -> dict:
    """The real event step of that name, straight out of the corpus file, unmodified.

    Reading a default off a hand-written step proves the default, not the corpus: the row has to run
    the step the corpus actually ships, or what it guards is the test's own spelling.
    """
    doc = json.loads(_oracle(scenario).read_text())
    for step in doc["steps"]:
        if isinstance(step, dict) and step.get("event") == event:
            return step
    raise AssertionError(f"{scenario} has no {event!r} step")


class StopFirstnessTest(unittest.TestCase):
    """Which event of that name a step means (`oracle.schema.json` § first, `corpus/firstness.py`).

    `stopped` is the one name whose order and cardinality ARE the claim: one halt, one event, and
    A038 says a step landing on an armed breakpoint yields EXACTLY ONE stop. So a `stopped` step
    that says nothing means the first stop the stream still holds, and a runtime that stops at the
    wrong line three times and then at the right one fails instead of passing. Every other name
    pushes on every change (`forces` per force, `runState` per state change, ADR-005 D5.3) and
    keeps the loosening, because there the event an oracle wants is not the oldest one.

    The corpus's own steps are fed in unmodified, so each half is measured on what ships.
    """

    @staticmethod
    def _stops(lines: List[int]) -> List[dict]:
        return [_event("stopped", {"reason": "breakpoint",
                                   "location": {"file": "program.st", "line": n}})
                for n in lines]

    @staticmethod
    def _run(events: List[dict]):
        return StopFirstnessTest._run_on(events, lambda evs, _clock: _ScriptedEndpoint(evs))

    @staticmethod
    def _run_on(events: List[dict], make_endpoint):
        run = object.__new__(runner._Run)
        run.doc = {}
        run.corpus = "plant-io"
        run.table = {}
        run.clock = _VirtualClock()  # type: ignore[assignment]
        run.endpoint = make_endpoint(events, run.clock)  # type: ignore[assignment]
        run.plant = runner.UnavailablePlant()  # type: ignore[assignment]
        run.build = runner.UnavailableBuild()  # type: ignore[assignment]
        run.setup = {}
        run.captures = {}
        run.streams = {}
        run.arrivals = 0
        run.last_witness = None
        run.last_plant_ms = 0.0
        run.last_vt = None
        run.step_end_ms = 0.0
        return run

    # -- the hole closing

    def test_a_stopped_step_that_says_nothing_takes_the_first_stop(self) -> None:
        # The one this goal is about: the stream holds a stop at 3 and then the one the step asked
        # for at 2. Walking to the second one hands a PASS to a runtime that stopped at the wrong
        # line, which is what every one of the corpus's first-stop steps was willing to accept.
        run = self._run(self._stops([3, 2]))
        with self.assertRaises(runner.Diverge) as caught:
            run._step_event({"event": "stopped", "withinMs": 500,
                             "expect": {"reason": "breakpoint",
                                        "location": {"file": "program.st", "line": 2}}})
        self.assertIn("stopped.location.line", caught.exception.reason)
        self.assertIn('observed 3', caught.exception.reason)

    def test_the_first_stop_is_consumed_even_when_it_is_the_one_asked_for(self) -> None:
        # And the other way round, or "take the first" would be a rule with no working case: the
        # first stop matches and is read, and the SECOND is left there for the next step to mean.
        run = self._run(self._stops([2, 3]))
        run._step_event({"event": "stopped", "withinMs": 500,
                         "expect": {"reason": "breakpoint",
                                    "location": {"file": "program.st", "line": 2}}})
        run._step_event({"event": "stopped", "withinMs": 50,
                         "expect": {"reason": "breakpoint",
                                    "location": {"file": "program.st", "line": 3}}})
        stream = run._stream("main")
        self.assertEqual([e["body"]["location"]["line"] for _, e, _a in stream.inbox], [])

    def test_an_expectless_step_consumes_the_event_it_read(self) -> None:
        # 17 steps in the corpus name an event and no expectation ("a `continued` arrives within
        # 500 ms"), and `nested-fb-loop/control-responses` has two of them in a row. The second must
        # not be satisfied by the FIRST's event: it is about the next one. Reading an event without
        # consuming it left it in the stream for whoever asked next, which is the same hole as
        # walking past stops, one step smaller.
        run = self._run([_event("continued", {"task": "all"})])
        run._step_event({"event": "continued", "withinMs": 50})
        with self.assertRaises(runner.Diverge) as caught:
            run._step_event({"event": "continued", "withinMs": 50})
        self.assertIn("no 'continued' event within 50 ms", caught.exception.reason)

    def test_a_stopped_step_that_says_false_reads_the_later_stop(self) -> None:
        # The opt-out, and it is the ONLY way back to the loose reading: `validate.py
        # firstness_warnings` counts every one of these, so the surface is a number, not a habit.
        run = self._run(self._stops([3, 2]))
        detail = run._step_event({"event": "stopped", "first": False, "withinMs": 500,
                                  "expect": {"reason": "breakpoint",
                                             "location": {"file": "program.st", "line": 2}}})
        self.assertIn("stopped", detail)

    def test_the_gate_and_the_runner_read_the_same_default(self) -> None:
        # A default stated twice is how `first` became a field that was documented, validated and
        # never read: the runner had its own idea of what an event step meant. One resolver, and
        # both sides hold the same object.
        import firstness  # noqa: PLC0415 - the corpus module the runner and the gate both import

        self.assertIs(runner.firstness, firstness)
        self.assertIs(runner.firstness.takes_first, validate.takes_first)
        self.assertIs(validate.takes_first, firstness.takes_first)
        for step, want in [
            ({"event": "stopped"}, True),   # the default: one halt, one stop
            ({"event": "stopped", "first": True}, True),
            ({"event": "stopped", "first": False}, False),
            ({"event": "forces"}, False),   # the loosening, kept: a stream that pushes
            ({"event": "runState"}, False),
            ({"event": "forces", "first": True}, True),
            ({"event": "stopped", "first": "yes"}, True),  # a typo the schema refuses is not a mode
        ]:
            with self.subTest(step=step):
                self.assertIs(firstness.takes_first(step), want)

    # -- the loosening that must keep working, on the corpus's own steps

    def test_a_run_state_step_that_says_nothing_still_reads_the_state_it_names(self) -> None:
        # A047, unmodified: ADR-005 D5.3 pushes a `runState` on every state change, so the watcher's
        # stream holds the states that came before the one this step is about.
        # `run-state-visibility` steps[4] reads PAUSED off a stream carrying RUNNING and HALTED_AT_BP.
        step = _corpus_step("plant-io/run-state-visibility", "runState")
        self.assertNotIn("first", step, "the step under test must exercise the DEFAULT")
        self.assertEqual(step["client"], "watcher")
        run = self._run([
            _event("runState", {"state": "RUNNING", "tasks": ["Main"]}),
            _event("runState", {"state": "HALTED_AT_BP", "tasks": ["Main"]}),
            _event("runState", {"state": "PAUSED", "tasks": ["Main"]}),
        ])
        self.assertIn("runState", run._step_event(dict(step)))

    def test_the_force_that_expired_is_still_the_one_a116_reads(self) -> None:
        # A116, unmodified: a force emits a `forces` event, so the one that was ADDED is queued
        # before the one that EXPIRED. `force-ttl` steps[13] waits for the list to empty, and a
        # runner that took the oldest would read the force before it had expired.
        step = _corpus_step("plant-io/force-ttl", "forces")
        self.assertNotIn("first", step, "the step under test must exercise the DEFAULT")
        # The step carries notBeforeMs 9000 (the 10 s TTL less the 1 s the stopped step may take), so
        # the two `forces` are released on the clock: the force that was ADDED at 200 ms, and the one
        # that EXPIRED at 9500 ms. Reading the first would fail the floor and reading the oldest
        # would fail the emptiness; only the second event is this step's event.
        run = self._run_on([
            _at(200, "forces", {"list": [{"name": "out_dint", "value": 2222}]}),
            _at(9500, "forces", {"list": []}),
        ], _TimedEndpoint)
        self.assertIn("forces", run._step_event(dict(step)))

    def test_the_opt_out_is_the_one_the_gate_names(self) -> None:
        # The two halves of the mechanism agree on what an opt-out IS: what the runner walks past is
        # exactly what the gate's register reports. A step in one list and not the other is a default
        # stated twice.
        loose = {"event": "stopped", "first": False, "withinMs": 1000}
        run = self._run(self._stops([3, 2]))
        run._step_event(dict(loose, expect={"location": {"file": "program.st", "line": 2}}))
        self.assertIn("not the first stop", validate.steps_over_a_stop(loose))
        self.assertIsNone(validate.steps_over_a_stop({"event": "stopped", "withinMs": 1000}))


class TimingBoundTest(unittest.TestCase):
    """The two halves. A bound nobody has watched fail is not a bound, so each key is proved both
    ways here: green on the interval it is for, DIVERGE on the interval it must reject.

    `notBeforeMs` is the wall-clock floor and exists only for a must-not-fire-early claim, where
    contention lengthens the interval and so cannot make the floor flaky. `sinceVtMs` is PLC
    virtual time and exists for PLC-time claims, which are exact on that clock whatever the wall
    clock did. Neither is a general two-sided timer and the corpus README says so."""

    @staticmethod
    def _run(events: List[dict], plant=None, session=None):
        run = object.__new__(runner._Run)
        run.doc = {}
        run.corpus = "plant-io"
        run.table = {}
        run.endpoint = None  # type: ignore[assignment]
        run.clock = _VirtualClock()  # type: ignore[assignment]
        run.endpoint = _TimedEndpoint(events, run.clock, session)  # type: ignore[assignment]
        run.plant = plant or runner.UnavailablePlant()  # type: ignore[assignment]
        run.build = runner.UnavailableBuild()  # type: ignore[assignment]
        run.setup = {}
        run.captures = {}
        run.streams = {}
        run.arrivals = 0
        run.last_witness = None
        run.last_plant_ms = 0.0
        run.last_vt = None
        run.step_end_ms = 0.0
        return run

    # -- notBeforeMs

    def test_an_event_that_arrives_late_enough_passes_the_floor(self) -> None:
        run = self._run([_at(15000.0, "runState", {"state": "SAFE_STOPPED"})])
        run.step_end_ms = 0.0
        detail = run._step_event({"event": "runState", "withinMs": 16000, "notBeforeMs": 15000,
                                  "expect": {"body": {"state": "SAFE_STOPPED"}}})
        self.assertEqual(detail, "runState not before 15000 ms")

    def test_an_event_that_fires_early_is_a_divergence_naming_the_interval(self) -> None:
        # The A057 hole: safe-stopping at 200 ms instead of after P2 = 15 s. The ceiling alone
        # cannot see it, so this is the whole point of the key.
        run = self._run([_at(200.0, "runState", {"state": "SAFE_STOPPED"})])
        run.step_end_ms = 0.0
        with self.assertRaises(runner.Diverge) as caught:
            run._step_event({"event": "runState", "withinMs": 16000, "notBeforeMs": 15000,
                             "expect": {"body": {"state": "SAFE_STOPPED"}}})
        self.assertIn("fired early", caught.exception.reason)
        self.assertIn("200 ms", caught.exception.reason)

    def test_the_floor_is_measured_from_the_step_before_not_from_the_scenario_start(self) -> None:
        # An event 16 s into the scenario that arrives 20 ms after a step finished is early for
        # this step even though its absolute age is large; measuring from the start would let a
        # runtime that safe-stops at once pass by having taken its time getting there.
        run = self._run([_at(16000.0, "runState", {"state": "SAFE_STOPPED"})])
        run.clock.now = 15990.0
        run.step_end_ms = 15990.0
        with self.assertRaises(runner.Diverge) as caught:
            run._step_event({"event": "runState", "withinMs": 500, "notBeforeMs": 15000,
                             "expect": {"body": {"state": "SAFE_STOPPED"}}})
        self.assertIn("fired early", caught.exception.reason)

    def test_a_wait_does_not_hide_an_event_that_arrived_during_it(self) -> None:
        # Without draining in slices, `wait wallMs 16000` holds a 200 ms event undrained and stamps
        # it at 16000, and the floor passes a runtime that safe-stopped 15 s early.
        run = self._run([_at(200.0, "runState", {"state": "SAFE_STOPPED"})])
        run._stream("main")  # the controller is already attached when the kill lands
        run._step_wait({"wait": {"wallMs": 16000}})
        run.step_end_ms = 0.0  # the floor measures from the step before the wait: the kill
        with self.assertRaises(runner.Diverge) as caught:
            run._step_event({"event": "runState", "withinMs": 16000, "notBeforeMs": 15000,
                             "expect": {"body": {"state": "SAFE_STOPPED"}}})
        self.assertIn("fired early", caught.exception.reason)

    def test_a_step_with_no_floor_is_unaffected_by_the_key_elsewhere(self) -> None:
        run = self._run([_at(200.0, "stopped", {"reason": "breakpoint"})])
        run.step_end_ms = 0.0
        detail = run._step_event({"event": "stopped", "withinMs": 1000,
                                  "expect": {"reason": "breakpoint"}})
        self.assertEqual(detail, "stopped")

    # -- wait slicing: the drain (load-bearing) and its cost (not)

    def test_a_long_wait_drains_often_enough_to_stamp_an_early_arrival_low(self) -> None:
        """The drain AT THE CAP, not on the growth ramp.

        The growth is geometric, so before the cap is reached the slice in effect at t is about t/2
        and a cap change is invisible to anything that arrives early. The cap is what bounds an
        arrival in the LONG part of a wait -- past twice the cap -- and that is where this event is
        put: 250 s into a 299 s wait, at 250x the cap.

        The shape is A189's (plant-io/control-handover step 12): something that may not fire before
        299 s, observed firing at 250 s. Stamped within one cap of its arrival the floor still
        fires; stamped at the end of the wait it does not, and a runtime that acted 49 s early is
        handed a pass.

        Red under a cap of 200000, which is a real measurement and not a strawman: with a cap that
        wide the slice in effect at 250 s is 81920, the event is stamped at 331920, and the floor
        passes it. (A cap raised to 200000 while the event sat at 20 s changed nothing, which is why
        this row is at 250 s and that one was not.) MAX_SLEEP_SLICE_MS is therefore the number that
        decides this, and the 9000 ms floor in A116 is what buys room for it.
        """
        run = self._run([_at(250000.0, "forces", {"list": []})])
        run._stream("main")
        run._step_wait({"wait": {"wallMs": 299000}})
        stamps = [at for _, ev, at in run._stream("main").inbox if ev["event"] == "forces"]
        self.assertTrue(stamps, "the wait drained nothing, so the event is still in the session")
        self.assertGreater(stamps[0], 250000.0, "the event was stamped before it arrived")
        self.assertLessEqual(stamps[0], 250000.0 + runner.MAX_SLEEP_SLICE_MS)
        run.step_end_ms = 0.0
        with self.assertRaises(runner.Diverge) as caught:
            run._step_event({"event": "forces", "withinMs": 299000, "notBeforeMs": 299000,
                             "expect": {"body": {"list": {"~len": 0}}}})
        self.assertIn("fired early", caught.exception.reason)

    def test_a_long_wait_costs_logarithmically_many_drains_not_one_per_poll(self) -> None:
        """Why the slice grows. 45 of the corpus's oracles wait on wall time, and at one slice per
        POLL_MS a 299 s wait asked the stream 59,800 times and returned at 394-433 s on a real
        endpoint: `wait wallMs` overran by a third and A210 DIVERGEd on its own +-1 s tolerance.

        The bound is stated against the measured budget rather than as "fewer drains": A210's wait
        may cost at most 1000 ms of overrun, and the per-slice cost measured on that endpoint is
        1.6-2.3 ms, so 299000/1000 * 2.3 = 688 ms of drain is the whole allowance. Red under the
        old fixed slice, which asks 59,800 times.
        """
        session = _CountingSession(None)
        run = self._run([], session=session)
        run._stream("main")
        run._step_wait({"wait": {"wallMs": 299000}})
        allowed = int(299000 / runner.MAX_SLEEP_SLICE_MS * 2.3) + 32  # + the growth ramp
        self.assertLessEqual(session.drains, allowed)
        # and the wait is still the wait: it did not end early to save the drains
        self.assertEqual(run.clock.now_ms(), 299000.0)

    # -- sinceVtMs

    def test_virtual_time_advanced_by_the_claimed_amount_matches(self) -> None:
        run = self._run([], _ScriptedPlant([500.0]))
        run.last_vt = {"Main": 0.0}
        run._step_observe({"observe": "plant", "expect": {"sinceVtMs": {"Main": {"~ge": 500}}}})

    def test_virtual_time_that_advanced_further_than_claimed_is_a_divergence(self) -> None:
        # A halt that cost 5 scans of virtual time instead of 1: a ceiling on `vtMs` reads this as
        # fine, since 5000 > 500.
        run = self._run([], _ScriptedPlant([5000.0]))
        run.last_vt = {"Main": 0.0}
        with self.assertRaises(runner.Diverge) as caught:
            run._step_observe({"observe": "plant", "expect": {"sinceVtMs": {"Main": 500}}})
        self.assertIn("plant.sinceVtMs.Main", caught.exception.reason)

    def test_the_window_advances_so_a_second_observation_measures_from_the_first(self) -> None:
        run = self._run([], _ScriptedPlant([100.0, 100.0]))
        run.last_vt = {"Main": 0.0}
        run._step_observe({"observe": "plant", "expect": {"sinceVtMs": {"Main": 100}}})
        run._step_observe({"observe": "plant", "expect": {"sinceVtMs": {"Main": 0}}})

    def test_a_task_the_plant_has_never_reported_is_absent_not_zero(self) -> None:
        run = self._run([], _ScriptedPlant([100.0], tasks=["Fast"]))
        run.last_vt = {"Main": 0.0}
        with self.assertRaises(runner.Diverge) as caught:
            run._step_observe({"observe": "plant", "expect": {"sinceVtMs": {"Slow": 0}}})
        self.assertIn("absent", caught.exception.reason)


class DiagnosticLedgerTest(unittest.TestCase):
    """A diagnostic scenario's verdict is a bisect, and the ledger has to say so.

    It used to write no row at all, because `results_lines()` emitted one row per criterion a scenario
    listed and a diagnostic lists none. A bisect that leaves no trace cannot be told from a scenario
    nobody ran -- which is how a stale one goes unnoticed. Now it writes one `criterion: null` row.
    """

    ORACLE = "all-scopes-types/scopes-tail"

    def test_a_bisect_writes_one_null_criterion_row(self) -> None:
        oracle = _oracle(self.ORACLE)
        doc = json.loads(oracle.read_text())
        self.assertEqual(doc["kind"], "diagnostic")
        self.assertEqual(doc["criteria"], [])
        result = runner.RunResult(self.ORACLE, [], runner.Verdict("PASS", None, "all 9 steps matched"))
        result.diagnostic = True
        rows = runner.results_lines(result, ran_on="test", commit="0000000")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["criterion"])
        self.assertEqual(rows[0]["test"], f"tests/debug/harness/runner.py::{self.ORACLE}")
        # The same row set as any other run: a reader parses one shape whatever ran.
        self.assertEqual(
            set(rows[0]),
            {"criterion", "verdict", "test", "ran_on", "commit", "repo", "timestamp", "evidence"},
        )

    def test_a_scenario_that_claims_nothing_and_is_not_a_bisect_still_writes_nothing(self) -> None:
        # validate.py refuses that combination; the runner does not invent a row for it either, so a
        # scenario can never be counted as coverage by being empty.
        rows = runner.results_lines(
            runner.RunResult("x/y", [], runner.Verdict("PASS", None, "ok")), "test", "0000000"
        )
        self.assertEqual(rows, [])

    def test_a_scenario_is_flagged_as_a_bisect_when_it_is_loaded_as_one(self) -> None:
        result = _run_fake(self.ORACLE)
        self.assertEqual(result.scenario, self.ORACLE)
        self.assertTrue(result.diagnostic)
        self.assertEqual(result.criteria, [])
        self.assertEqual(len(runner.results_lines(result, "test", "0000000")), 1)


if __name__ == "__main__":
    unittest.main()
