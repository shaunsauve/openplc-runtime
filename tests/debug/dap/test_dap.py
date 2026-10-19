#!/usr/bin/env python3
"""Unit guards for the DAP adapter's own code: the framing, and the promise `initialize` makes.

No runtime here. These are the checks that do not need one — the framing has to be right before
anything can be measured through it, and "every advertised capability has a handler" is a property
of the source that a run against a real plc_main only checks indirectly.

  python3 tests/debug/dap/test_dap.py        (or: python3 -m unittest discover tests/debug/dap)
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import adapter  # noqa: E402
import dap_harness  # noqa: E402


def frames(*messages):
    out = io.BytesIO()
    for m in messages:
        raw = json.dumps(m).encode("utf-8")
        out.write(b"Content-Length: %d\r\n\r\n" % len(raw))
        out.write(raw)
    return out.getvalue()


def split(raw: bytes):
    """Bytes -> the JSON objects they carry, so a test can read what an adapter wrote."""
    got, rest = [], raw
    while b"Content-Length:" in rest:
        _, rest = rest.split(b"Content-Length:", 1)
        head, rest = rest.split(b"\r\n\r\n", 1)
        length = int(head.split(b"\r\n")[0])
        got.append(json.loads(rest[:length].decode("utf-8")))
        rest = rest[length:]
    return got


class Framing(unittest.TestCase):
    def test_a_frame_round_trips(self):
        raw = frames({"seq": 1, "type": "request", "command": "initialize"})
        self.assertEqual(split(raw)[0]["command"], "initialize")

    def test_extra_headers_are_ignored_and_the_body_is_read_by_length(self):
        body = b'{"seq":1,"type":"request","command":"pause"}'
        raw = b"Content-Length: %d\r\nContent-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n%s" % (
            len(body), body)
        self.assertEqual(split(raw)[0]["command"], "pause")

    def test_a_body_containing_a_blank_line_is_read_whole(self):
        # The framing is a LENGTH, not a delimiter: a message that contains "\r\n\r\n" must not be
        # cut in half, and a reader that split on the blank line would report a short body.
        body = b'{"seq":1,"type":"request","command":"setBreakpoints","arguments":{"x":"a\\r\\n\\r\\nb"}}'
        raw = b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
        self.assertEqual(split(raw)[0]["arguments"]["x"], "a\r\n\r\nb")

    def test_a_header_without_content_length_is_refused(self):
        with self.assertRaises(adapter.DapError):
            adapter.read_message(io.BytesIO(b"Content-Type: x\r\n\r\n{}"))

    def test_a_length_that_cannot_be_a_message_is_refused(self):
        with self.assertRaises(adapter.DapError):
            adapter.read_message(io.BytesIO(b"Content-Length: 99999999999\r\n\r\n{}"))

    def test_end_of_stream_is_none_not_an_error(self):
        self.assertIsNone(adapter.read_message(io.BytesIO(b"")))


class Capabilities(unittest.TestCase):
    """The promise `initialize` makes, read from the outside."""

    def _initialize(self):
        out = subprocess.run([sys.executable, os.path.join(HERE, "adapter.py")],
                             input=frames({"seq": 1, "type": "request", "command": "initialize",
                                           "arguments": {}}),
                             capture_output=True, timeout=60)
        return split(out.stdout)[0]

    def test_initialize_answers_a_capability_block(self):
        resp = self._initialize()
        self.assertTrue(resp["success"], resp)
        self.assertIsInstance(resp["body"], dict)

    def test_every_capability_advertised_true_has_a_handler(self):
        # A DAP capability name is not a request name (`supportsConfigurationDoneRequest` ->
        # `configurationDone`), so the relation to check runs through the harness's discharge
        # table: each advertised capability names a request, and that request must exist.
        caps = self._initialize()["body"]
        for name, value in caps.items():
            if value is True:
                requests = dap_harness.CAPABILITY_DISCHARGED_BY.get(name)
                self.assertIsNotNone(requests, "initialize advertises %s and nothing names it" % name)
                for request in requests:
                    self.assertIn(request, adapter.SUPPORTED_REQUESTS,
                                  "initialize advertises %s but no %s handler exists"
                                  % (name, request))

    def test_every_advertised_capability_has_a_known_discharge(self):
        # The harness table, not the adapter's: an adapter that grows a capability without telling
        # the harness how to exercise it would make the next run silently incomplete.
        caps = self._initialize()["body"]
        for name, value in caps.items():
            if value is True:
                self.assertIn(name, dap_harness.CAPABILITY_DISCHARGED_BY,
                              "no request discharges %s, so nothing can observe it" % name)

    def test_a_seeded_capability_with_no_handler_is_visible_from_outside(self):
        # A073's Red-proof, at the level that needs no runtime: `--seed steplie` advertises
        # supportsStepBack, and the harness's discharge table does not know it. That is the
        # divergence the real run then shows against a plc_main.
        out = subprocess.run([sys.executable, os.path.join(HERE, "adapter.py"), "--seed", "steplie"],
                             input=frames({"seq": 1, "type": "request", "command": "initialize",
                                           "arguments": {}}),
                             capture_output=True, timeout=60)
        caps = split(out.stdout)[0]["body"]
        self.assertIs(caps.get("supportsStepBack"), True)
        self.assertNotIn("supportsStepBack", dap_harness.CAPABILITY_DISCHARGED_BY)


class Refusals(unittest.TestCase):
    """The requests the runtime's session protocol does not serve. These need no runtime, because
    the adapter is the layer that refuses them."""

    def _round_trip(self, request, arguments=None):
        out = subprocess.run(
            [sys.executable, os.path.join(HERE, "adapter.py")],
            input=frames({"seq": 1, "type": "request", "command": request,
                          "arguments": arguments or {}}),
            capture_output=True, timeout=60)
        return split(out.stdout)[0]

    def test_set_function_breakpoints_is_refused_by_name(self):
        resp = self._round_trip("setFunctionBreakpoints", {"breakpoints": []})
        self.assertFalse(resp["success"])
        self.assertIn("setFunctionBreakpoints", resp["message"])
        self.assertEqual(resp["body"]["error"]["code"], "UNSUPPORTED")

    def test_evaluate_is_refused_by_name(self):
        resp = self._round_trip("evaluate", {"expression": "1"})
        self.assertFalse(resp["success"])
        self.assertIn("evaluate", resp["message"])

    def test_set_variable_is_refused_by_name(self):
        resp = self._round_trip("setVariable", {"variablesReference": 1, "name": "x", "value": 1})
        self.assertFalse(resp["success"])
        self.assertIn("setVariable", resp["message"])

    def test_an_unrecognised_request_names_the_ones_that_exist(self):
        resp = self._round_trip("nonsense", {})
        self.assertFalse(resp["success"])
        self.assertIn("unrecognized request", resp["message"])
        self.assertIn("stackTrace", resp["message"])

    def test_a_request_before_attach_says_so(self):
        resp = self._round_trip("threads", {})
        self.assertFalse(resp["success"])
        self.assertIn("attach", resp["message"])

    def test_a_thread_id_the_adapter_never_issued_is_refused(self):
        resp = self._round_trip("stackTrace", {"threadId": 7})
        self.assertFalse(resp["success"])
        self.assertIn("no thread 7", resp["message"])

    def test_a_malformed_message_is_refused_without_killing_the_stream(self):
        out = subprocess.run(
            [sys.executable, os.path.join(HERE, "adapter.py")],
            input=(b"Content-Length: 5\r\n\r\n{bad}" +
                   frames({"seq": 1, "type": "request", "command": "initialize", "arguments": {}})),
            capture_output=True, timeout=60)
        got = split(out.stdout)
        self.assertEqual(len(got), 2, out.stdout)
        self.assertFalse(got[0]["success"])
        self.assertIsNone(got[0]["request_seq"])
        self.assertTrue(got[1]["success"])


class Markers(unittest.TestCase):
    def test_a_marker_becomes_the_line_it_is_written_on(self):
        import re
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "program.st"
            src.write_text("one\ntwo (*@TWO*)\nthree\n", encoding="utf-8")
            self.assertEqual(dap_harness.markers(src), {"@TWO": 2})


if __name__ == "__main__":
    unittest.main(verbosity=2)