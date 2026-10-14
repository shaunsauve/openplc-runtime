// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Interface only: the probe ABI symbols and the table API, with no behaviour yet.

#include "debug_agent.h"

#include "../lib/strucpp_abi.hpp"

extern "C" {
void strucpp_dbg_hit(uint32_t) {}
void strucpp_dbg_frame_link(strucpp_dbg_frame *) {}
void strucpp_dbg_frame_unlink(strucpp_dbg_frame *) {}

void debug_agent_set_host(const dbg_host_t *) {}
void debug_agent_bind(volatile uint8_t *, uint32_t, uint8_t *, int (*)(void), size_t) {}
void debug_agent_unbind(void) {}
uint32_t debug_agent_probe_count(void) { return 0; }
void debug_agent_task_enter(size_t) {}
void debug_agent_task_exit(void) {}
void debug_agent_set_attached(bool) {}
bool debug_agent_attached(void) { return false; }
dbg_status_t debug_agent_set_breakpoint(uint32_t, const uint8_t *, size_t, uint32_t)
{
    return DBG_E_NO_PROBES;
}
dbg_status_t debug_agent_clear_breakpoint(uint32_t) { return DBG_E_NO_PROBES; }
uint32_t debug_agent_clear_all_breakpoints(void) { return 0; }
uint32_t debug_agent_breakpoint_count(void) { return 0; }
void debug_agent_reclaim(void) {}
void debug_agent_set_trap(dbg_trap_kind_t, uint32_t, uint32_t) {}
void debug_agent_clear_trap(void) {}
bool debug_agent_take_stop(size_t, dbg_stop_t *) { return false; }
void debug_agent_wake(void) {}
void debug_agent_wait(int) {}
void debug_agent_counters(dbg_counters_t *out) { *out = dbg_counters_t{}; }
}
