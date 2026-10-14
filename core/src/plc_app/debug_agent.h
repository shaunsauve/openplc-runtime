// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file debug_agent.h
 * @brief L2 core debug agent: the breakpoint table, the probe slow path and run control.
 *
 * Implements the host side of STruCpp's probe ABI (debug_probe.hpp, ABI 1):
 * strucpp_dbg_hit, strucpp_dbg_frame_link and strucpp_dbg_frame_unlink. plc_main exports them
 * (-rdynamic), so they interpose the program's weak no-op versions.
 *
 * Breakpoints are immutable records published by pointer swap and reclaimed after a QSBR grace
 * period (ADR-001 D6.2). A stop parks the scan thread through the host's park op, which the state
 * manager implements (ADR-001 D2.2, D11). Step and pause stop through the trap word (D6.3).
 *
 * Threads: the scan threads run strucpp_dbg_hit and the frame calls only. Every other function
 * here is the writer side and serialises on one agent mutex, so the agent is the single writer of
 * records, pointers, flag bytes and the trap word.
 *
 * See also:
 *   core/src/plc_app/debug_link.cpp - the core link: requests, events and the agent thread.
 *   core/src/plc_app/plc_state_manager.cpp - park, resume, abandon and the per-task run state.
 *   docs/DEBUG_PROTOCOL.md - the core-link messages built on this interface.
 */
#ifndef DEBUG_AGENT_H
#define DEBUG_AGENT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* P3, declared in the handshake (F027). The record and side-slot pools are twice this. */
#define DBG_MAX_BREAKPOINTS 1024
#define DBG_POOL_SIZE       (2 * DBG_MAX_BREAKPOINTS)
#define DBG_MAX_TASKS       32
/* ADR-007's bound on condition bytecode. */
#define DBG_COND_MAX 512

/* Flag byte bits, as debug_probe.hpp defines them. */
#define DBG_FLAG_ARMED 1u
#define DBG_FLAG_TRAP  2u

typedef enum
{
    DBG_OK = 0,
    DBG_E_NO_PROBES,        /* no program bound, or a probe-off build */
    DBG_E_LIMIT,            /* LIMIT_EXCEEDED */
    DBG_E_INVALID_LOCATION, /* no such probe */
    DBG_E_CONDITION,        /* the condition failed verification */
} dbg_status_t;

/* Trap kinds (ADR-001 D4). */
typedef enum
{
    DBG_TRAP_NONE = 0,
    DBG_TRAP_PAUSE,    /* any probe of the target task(s) */
    DBG_TRAP_STEP_IN,  /* the next probe of the task */
    DBG_TRAP_STEP_OVER,/* the next probe of the task at frame depth <= bound */
    DBG_TRAP_STEP_OUT, /* the next probe of the task at frame depth < bound */
} dbg_trap_kind_t;

#define DBG_TASK_ANY 0xFFu

typedef enum
{
    DBG_STOP_NONE = 0,
    DBG_STOP_BREAKPOINT,
    DBG_STOP_PAUSE,
    DBG_STOP_STEP,
} dbg_stop_reason_t;

/* What a parked task stopped on; written by the scan thread before park. */
typedef struct
{
    dbg_stop_reason_t reason;
    uint32_t          probe;
    uint32_t          hits;     /* the breakpoint's hit count, 0 for a trap stop */
    uint32_t          depth;    /* linked frames at the stop */
    int64_t           since_ns; /* CLOCK_MONOTONIC */
} dbg_stop_t;

/* park() return values (ADR-001 D2.2). */
#define DBG_PARK_RESUME  0
#define DBG_PARK_ABANDON 1

/* Host ops the state manager provides. park runs on the scan thread: it must mark the task
 * parked, give back its in-flight slot, call debug_agent_wake() and block until the dispatcher
 * resumes or abandons it. */
typedef struct
{
    int (*park)(size_t task_idx);
} dbg_host_t;

void debug_agent_set_host(const dbg_host_t *host);

/* ---- Program binding (state manager, around a program's lifetime) ---- */

/* Binds the loaded program's flag bytes and session byte. lock_depth may be NULL (non-threaded).
 * Breakpoints kept across a stop are re-armed when the probe count is unchanged. */
void debug_agent_bind(volatile uint8_t *flags, uint32_t probe_count, uint8_t *active,
                      int (*lock_depth)(void), size_t task_count);
/* Unpublishes everything; called once no task thread is left and before the .so is closed. */
void debug_agent_unbind(void);
uint32_t debug_agent_probe_count(void);

/* ---- Scan threads ---- */

/* On the task thread before its first scan; identifies it to the slow path and the grace period. */
void debug_agent_task_enter(size_t task_idx);
/* On every exit path of the task thread: leaves its read-side section even if it died inside one. */
void debug_agent_task_exit(void);

/* ---- Session ---- */

/* Sets strucpp_dbg_active (and bumps the attach generation) or clears it. */
void debug_agent_set_attached(bool attached);
bool debug_agent_attached(void);

/* ---- Breakpoint table (writer side) ---- */

/* Arms or replaces the breakpoint at a probe. cond may be NULL (unconditional). hit_target 0 or 1
 * stops on every hit; n stops from the n-th hit on. A replace keeps the hit count. */
dbg_status_t debug_agent_set_breakpoint(uint32_t probe, const uint8_t *cond, size_t cond_len,
                                        uint32_t hit_target);
dbg_status_t debug_agent_clear_breakpoint(uint32_t probe);
/* Returns the number removed. */
uint32_t debug_agent_clear_all_breakpoints(void);
uint32_t debug_agent_breakpoint_count(void);

/* Reuses retired records whose grace period has passed. The agent thread calls it every tick. */
void debug_agent_reclaim(void);

/* ---- Trap (pause and step) ---- */

void debug_agent_set_trap(dbg_trap_kind_t kind, uint32_t task, uint32_t depth_bound);
void debug_agent_clear_trap(void);

/* ---- Stops (agent thread) ---- */

/* Takes the stop record a task left at park, once. False when none is pending. */
bool debug_agent_take_stop(size_t task_idx, dbg_stop_t *out);
/* Wakes the agent thread (sem_post); called by park. */
void debug_agent_wake(void);
/* Waits up to timeout_ms for a wake. */
void debug_agent_wait(int timeout_ms);

typedef struct
{
    uint64_t condition_errors; /* a condition that could not be evaluated */
    uint64_t skipped_locked;   /* a breakpoint hit at lock depth > 0 (not stopped) */
    uint64_t records_reused;   /* records reclaimed after a grace period */
    uint64_t limit_refusals;
} dbg_counters_t;

void debug_agent_counters(dbg_counters_t *out);

#ifdef __cplusplus
}

namespace plc_dbg {
/* Thrown by the slow path when park returns ABANDON (ADR-001 D7.4); the task's worker loop catches
 * it before catch (...), so the abandoned scan unwinds its frames and never copies out. */
struct Abandon
{
};
}  // namespace plc_dbg
#endif

#endif  // DEBUG_AGENT_H
