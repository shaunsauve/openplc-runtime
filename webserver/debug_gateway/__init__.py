# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

"""The debug session gateway (ADR-003 L3): /api/debug-session over the internal core link.

See also:
  webserver/debug_gateway/gateway.py - the Socket.IO namespace and dispatch.
  webserver/debug_gateway/core_link.py - the internal link to the core agent.
  webserver/app.py - wires it beside the legacy /api/debug namespace.
"""

from webserver.debug_gateway.core_link import CoreLink, CoreReply, CoreUnavailable, UnixCoreLink
from webserver.debug_gateway.gateway import DebugGateway, init_debug_gateway

__all__ = [
    "CoreLink",
    "CoreReply",
    "CoreUnavailable",
    "DebugGateway",
    "UnixCoreLink",
    "init_debug_gateway",
]
