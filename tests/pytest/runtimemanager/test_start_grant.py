# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""A REST start resends as a grant when the safe-stop start gate refuses it (ADR-005 D4.7, the
webserver half of A208). The core half is tests/host/test_safe_stop.cpp.

NO-FOLD: contract (the command-socket lines the webserver sends; no runtime surface of its own)."""

from typing import List, Optional

import pytest

from webserver import runtimemanager as rm


class _Socket:
    def __init__(self, replies: List[str]) -> None:
        self.replies = replies
        self.sent: List[str] = []

    def send_and_receive(self, command: str) -> str:
        self.sent.append(command)
        return self.replies.pop(0)


@pytest.mark.parametrize(
    "replies,user,sent",
    [
        (
            ["START:ERROR_SAFE_STOPPED\n", "START:OK\n"],
            "tech",
            ["START\n", "START_AUTHORISED:tech\n"],
        ),
        (["START:OK\n"], "tech", ["START\n"]),
        (["START:ERROR\n"], "tech", ["START\n"]),
        (["START:ERROR_SAFE_STOPPED\n"], None, ["START\n"]),
        (["START:ERROR_SAFE_STOPPED\n"], "a b\nSTOP", ["START\n"]),
    ],
)
def test_a_refused_start_is_resent_as_a_grant_only_for_a_named_user(
    replies: List[str], user: Optional[str], sent: List[str]
) -> None:
    mgr = rm.RuntimeManager("/bin/false", "/tmp/x.sock", "/tmp/y.sock")
    sock = _Socket(list(replies))
    mgr.runtime_socket = sock  # type: ignore[assignment]
    reply = mgr.start_plc(granting_user=user)
    assert sock.sent == sent
    assert reply == replies[len(sent) - 1]
