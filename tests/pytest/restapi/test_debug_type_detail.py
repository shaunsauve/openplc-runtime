"""The structural facts a client asks for with `variables {types: true}` (E45 ruling 2).

`type` is a display string; these are the same facts structured, so a client computes from
them instead of parsing the string. Every assertion here is a field a criterion's Evidence
names (A013: "nested struct layout, array bounds and enum members").

Run: python3 -m pytest tests/pytest/restapi/test_debug_type_detail.py

See also:
  webserver/debug_gateway/debug_info.py - DebugInfo.type_detail, next to type_name.
  tests/debug/corpus/all-scopes-types/scopes.oracle.json - steps 6-8, the real oracle.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _load(name: str) -> types.ModuleType:
    """Load one gateway module by path, without `webserver.debug_gateway.__init__`.

    That package pulls in the whole webserver (flask_sqlalchemy and the rest) to reach the
    gateway; a guard for a pure type renderer should not need the web stack to run.
    """
    pkg = name.rsplit(".", 1)[0]
    for step in ("webserver", "webserver.debug_gateway"):
        if step not in sys.modules:
            stub = types.ModuleType(step)
            stub.__path__ = [str(REPO / Path(*step.split(".")))]  # type: ignore[attr-defined]
            sys.modules[step] = stub
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO.joinpath(*name.split(".")).with_suffix(".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("webserver.debug_gateway.protocol")
_debug_info = _load("webserver.debug_gateway.debug_info")
DebugInfo = _debug_info.DebugInfo
protocol = _load("webserver.debug_gateway.protocol")


def _info() -> DebugInfo:
    """The three shapes the oracle asks about: an array of structs, an enum, an array of INT."""
    return DebugInfo(
        {
            "major": 1,
            "minor": 0,
            "types": [
                {"id": 1, "kind": "elementary", "name": "INT"},
                {"id": 2, "kind": "elementary", "name": "Mode"},
                {"id": 3, "kind": "struct", "name": "Point",
                 "members": [{"name": "x", "typeId": 1}, {"name": "y", "typeId": 1}]},
                {"id": 4, "kind": "enum", "name": "Mode",
                 "values": [["IDLE", 0], ["RUN", 1], ["FAULT", 2]]},
                {"id": 5, "kind": "struct", "name": "Seg",
                 "members": [{"name": "a", "typeId": 3}, {"name": "tag", "typeId": 4}]},
                # scopes.oracle.json step 6: ARRAY[0..1] OF Seg
                {"id": 6, "kind": "array", "name": None, "dims": [[0, 1]], "elem": 5},
                # step 8: ARRAY[1..4] OF INT
                {"id": 7, "kind": "array", "name": None, "dims": [[1, 4]], "elem": 1},
            ],
        }
    )


class TypeDetail(unittest.TestCase):
    def test_member_names_are_keys_not_positions(self):
        """A member is `{name, type}`, not `[name, type]`.

        IEC 61131-3 6.1.2 makes an identifier case-insensitive, and the matcher folds a name
        by KEY (`IDENTIFIER_KEYS`). A positional pair gives the fold nowhere to act: the oracle
        asks for `a`, the debug info spells it `A`, and the two can never be compared as the
        same identifier. Naming the member makes the comparison case-insensitive for free, and
        it is the form DAP's `Variable` uses.
        """
        detail = _info().type_detail(6)["elementType"]
        for member in detail["members"]:
            self.assertIsInstance(member, dict, "a member must name its keys")
            self.assertEqual(sorted(member), ["name", "type"])

    def test_an_array_names_its_bounds_and_its_element_types_structure(self):
        self.assertEqual(
            _info().type_detail(6),
            {"dims": [[0, 1]],
             "elementType": {"name": "Seg",
                             "members": [{"name": "a", "type": "Point"},
                                         {"name": "tag", "type": "Mode"}]}},
        )

    def test_an_enum_names_its_members(self):
        self.assertEqual(
            _info().type_detail(4),
            {"values": [["IDLE", 0], ["RUN", 1], ["FAULT", 2]]},
        )

    def test_an_array_of_elementary_names_only_its_bounds(self):
        self.assertEqual(_info().type_detail(7), {"dims": [[1, 4]]})

    def test_a_multidimensional_array_names_every_dimension(self):
        info = DebugInfo(
            {"major": 1, "minor": 0,
             "types": [{"id": 1, "kind": "elementary", "name": "INT"},
                       {"id": 2, "kind": "array", "name": None,
                        "dims": [[0, 1], [1, 3]], "elem": 1}]}
        )
        self.assertEqual(info.type_detail(2), {"dims": [[0, 1], [1, 3]]})

    def test_an_elementary_type_adds_no_keys(self):
        self.assertEqual(_info().type_detail(1), {})

    def test_an_unknown_type_adds_no_keys(self):
        self.assertEqual(_info().type_detail(999), {})
        self.assertEqual(_info().type_detail(None), {})


class TypesArgumentSurvivesCheckArgs(unittest.TestCase):
    """`check_args` keeps the DECLARED arguments and drops everything else.

    A renderer can be correct and still never run: `types: true` undeclared is dropped on the
    way in, so `type_detail` is never reached and the answer silently lacks every key it would
    have added. This is the guard for that, and it is why `types` is declared in protocol.py.
    """

    def test_types_true_reaches_the_handler(self):
        args = protocol.check_args(protocol.COMMANDS["variables"],
                                   {"path": "PROG0.segs", "types": True})
        self.assertEqual(args, {"path": "PROG0.segs", "types": True},
                         "check_args dropped types: true, so type_detail would never run")

    def test_omitting_types_leaves_it_out(self):
        self.assertEqual(protocol.check_args(protocol.COMMANDS["variables"],
                                             {"path": "PROG0.segs"}),
                         {"path": "PROG0.segs"})


if __name__ == "__main__":
    unittest.main()