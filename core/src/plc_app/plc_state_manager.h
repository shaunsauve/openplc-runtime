// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

#ifndef PLC_STATE_MANAGER_H
#define PLC_STATE_MANAGER_H

#include "plcapp_manager.h"
#include "scan_cycle_manager.h"
#include <pthread.h>
#include <semaphore.h>
#include <setjmp.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>

/* Dual-language atomic types: C uses <stdatomic.h>'s _Atomic-typedef
 * forms; C++ uses std::atomic<T>. Both have the same memory layout, so
 * a struct that contains plc_atomic_long_t compiles in both languages
 * and the linker treats it as the same storage. */
#ifdef __cplusplus
#include <atomic>
typedef std::atomic<long>               plc_atomic_long_t;
typedef std::atomic<uint_least64_t>     plc_atomic_u64_t;
typedef std::atomic<int_least64_t>      plc_atomic_i64_t;
extern "C" {
#else
#include <stdatomic.h>
typedef atomic_long                     plc_atomic_long_t;
typedef atomic_uint_least64_t           plc_atomic_u64_t;
typedef atomic_int_least64_t            plc_atomic_i64_t;
#endif

/* Runtime states. TRANSITIONING values APPEND, never insert: the
 * first five are wire-visible (FC 0x46). RUNNING is published at
 * first-scan release, not at request. Both TRANSITIONING != RUNNING. */
typedef enum
{
    PLC_STATE_INIT,
    PLC_STATE_RUNNING,
    PLC_STATE_STOPPED,
    PLC_STATE_ERROR,
    PLC_STATE_EMPTY,
    PLC_STATE_TRANSITIONING_TO_RUN,
    PLC_STATE_TRANSITIONING_TO_STOP,
    /* No program executes; the dispatcher runs without tasks and plugins exchange
     * the held outputs. Left only by an explicit STOP or a granted start. */
    PLC_STATE_SAFE_STOPPED
} PLCState;

/* Per-task run state, owned by the dispatcher. HELD: not released, and excluded
 * from overrun, stuck and first-scan accounting. */
#define PLC_TASK_RUN_RUNNING 0
#define PLC_TASK_RUN_HELD    1

/* Pending run-control request in PlcTaskCtx::run_request. */
#define PLC_TASK_REQ_NONE   0
#define PLC_TASK_REQ_HOLD   1
#define PLC_TASK_REQ_RESUME 2

/* Task index meaning "every task" for the run-control calls. */
#define PLC_ALL_TASKS ((size_t)-1)

typedef struct PlcTaskCtx
{
    size_t                idx;                /* index into plc_tasks[] */
    int64_t               interval_ns;
    int                   priority;           /* IEC TASK priority, mapped to SCHED_FIFO */
    uint64_t              cpu_affinity_mask;  /* 0 = no pinning, kernel decides */
    bool                  is_fastest_task;    /* retained for STATS; housekeeping is on the dispatcher */
    void                 *task_handle;        /* opaque strucpp::TaskInstance* */
    pthread_t             thread;
    char                  name[32];

    
    sem_t                 go;
    uint64_t              divisor;
    int64_t               time_at_dispatch;
    plc_atomic_long_t     alive;
    plc_atomic_long_t     released;
    plc_atomic_long_t     completed;
    plc_atomic_long_t     overrun_count;
    plc_atomic_long_t     stuck_ticks;     /* consecutive due ticks found still in one scan */
    plc_atomic_long_t     exited;          /* 1 once the thread function has returned */
    plc_atomic_i64_t      release_ns;      /* CLOCK_MONOTONIC time of the last release */

    sigjmp_buf            crash_jmp;
    volatile sig_atomic_t crash_sig;
    volatile sig_atomic_t holding_mutex;   /* image-tables mutex held (crash unlock) */
    volatile sig_atomic_t in_body;         /* inside IEC program code, where the abort may jump out */

    plc_atomic_u64_t      local_tick;

    plc_atomic_long_t     run_state;       /* PLC_TASK_RUN_*, written by the dispatcher only */
    plc_atomic_long_t     run_request;     /* PLC_TASK_REQ_*, written by run control */
    plc_atomic_i64_t      held_since_ns;   /* CLOCK_MONOTONIC time the hold was applied */

    /* Per-task scan/cycle/latency tracker. The task thread updates it
     * around its scan body; STATS walks all trackers for per-task entries. */
    scan_cycle_tracker_t  tracker;

    /* Virtual clock (ADR-004 D4), written by the dispatcher only. vt_ns is the time
     * of the task's last running due tick; vt_next_ns the next one's. Neither moves
     * while the task is held. */
    plc_atomic_i64_t      vt_ns;
    int64_t               vt_next_ns;
} PlcTaskCtx;

extern PlcTaskCtx *plc_tasks;
extern size_t      plc_task_count;

/* Lifecycle lock for plc_tasks / plc_task_count. plc_cycle_thread owns
 * the array; without this lock a plugin STOP could free plc_tasks
 * while STATS iterates. Readers hold it for the walk; writer holds
 * it around alloc, publish and free. */
void plc_tasks_reader_lock(void);
void plc_tasks_reader_unlock(void);

/**
 * @brief Get the current PLC state.
 * @return PLCState The current PLC state
 */
PLCState plc_get_state(void);

/**
 * @brief Body of a transition that has ALREADY been claimed. Not a setter.
 *
 * PRECONDITION: plc_claim_transition(new_state) returned true, so a TRANSITIONING
 * state is current. Calling this without a claim tears the program down with no
 * TRANSITIONING_TO_STOP for the scan loops to observe and then publishes a final
 * state nobody claimed, corrupting the interlock the TRANSITIONING states are.
 *
 * Writes no state of its own: the landing is published by whoever knows the work
 * finished -- plc_cycle_thread for RUNNING, unload_plc_program for STOPPED -- with
 * the failure paths here publishing ERROR or EMPTY.
 *
 * Go through plc_begin_transition() instead. It is the only intended entry point
 * for a state change and it does the claiming for you.
 *
 * @param new_state PLC_STATE_RUNNING or PLC_STATE_STOPPED
 * @return true if the transition body completed without error
 */
bool plc_set_state(PLCState new_state);

/**
 * @brief Claim the right to transition, publishing the matching TRANSITIONING state.
 *
 * The single arbiter for "may this change start". Under the state lock: a request
 * arriving while a TRANSITIONING state is current is DROPPED -- you cannot change
 * state in the middle of changing state -- and so is a request for the state the
 * runtime is already in. Otherwise the direction is published and the caller owns
 * the transition until it publishes a final state.
 *
 * @param target PLC_STATE_RUNNING or PLC_STATE_STOPPED
 * @return true when the transition is claimed and the caller must complete it
 */
bool plc_claim_transition(PLCState target);

/**
 * @brief Publish the state a transition landed on: RUNNING, STOPPED, ERROR or EMPTY.
 *
 * Ends the transition. ERROR is sticky against STOPPED: a task that crashed while
 * a stop was tearing down has already recorded the more important fact, and the
 * teardown finishing must not paper over it.
 */
void plc_publish_final_state(PLCState final_state);

/**
 * @brief Publish RUNNING, but only if the start being completed is still in flight.
 *
 * Same landing as plc_publish_final_state(PLC_STATE_RUNNING), refused when the
 * current state is not TRANSITIONING_TO_RUN. Ending a transition is only the
 * caller's to do while it owns it: a watchdog-forced ERROR or a shutdown that
 * published TRANSITIONING_TO_STOP has taken ownership away, and overwriting either
 * with RUNNING erases a landing (or, on the shutdown path, hangs the join waiting
 * for a state that is never written again).
 *
 * @return true when RUNNING was published and the caller may start scanning
 */
bool plc_publish_running_if_claimed(void);

/**
 * @brief Longest a stop may take before the watchdog exits the process.
 *
 * PLC_TASK_STUCK_PERIODS times the longest task interval of the loaded program,
 * plus the outputs-off settle time and PLC_STOP_TEARDOWN_ALLOWANCE_MS.
 */
int64_t plc_stop_budget_ms(void);

/**
 * @brief Drive every output to 0 with no program loaded.
 *
 * Used on the safe-mode boot after a watchdog exit: loads and starts the configured
 * plugins (including VPP board plugins), runs the same outputs-off sequence as a
 * stop, then stops the plugins again. The caller must hold a claimed stop.
 *
 * @return true when the plugins were brought up and the zeroed outputs were pushed
 */
bool plc_outputs_off_without_program(void);

/**
 * @brief CLOCK_MONOTONIC release time (ns) of the oldest first scan still running, or 0.
 *
 * Read by the watchdog to bound first scans, which the dispatcher does not count.
 */
int64_t plc_first_scan_pending_since_ns(void);

/**
 * @brief Ask the dispatcher to trip on the oldest first scan still running.
 *
 * Called by the watchdog when that scan exceeds PLC_FIRST_SCAN_TIMEOUT_MS. The
 * dispatcher then stops the PLC exactly as for a stuck task.
 */
void plc_request_first_scan_trip(void);

/** @brief True while a transition is in flight (either direction). */
bool plc_state_is_transitioning(void);

/* transition_worker stops waiting for a landing at PLC_TRANSITION_LANDING_TIMEOUT_MS.
 * The watchdog forces ERROR on a start stuck past PLC_TRANSITION_STUCK_TIMEOUT_MS; a
 * stuck stop is bounded by plc_stop_budget_ms() and ends in watchdog_fatal_exit(). */
#define PLC_TRANSITION_LANDING_TIMEOUT_MS 90000
#define PLC_TRANSITION_STUCK_TIMEOUT_MS   (PLC_TRANSITION_LANDING_TIMEOUT_MS + 30000)

/**
 * @brief Claim a start that carries a one-shot grant from an authorised user.
 *
 * Same as plc_claim_transition(PLC_STATE_RUNNING), except that the start gate does
 * not refuse it. The grant is consumed by this call whether or not the claim
 * succeeds, so it can never authorise a later, different start.
 *
 * @param user name of the user who holds control; must be non-empty
 * @return true when the start is claimed and the caller must complete it
 */
bool plc_claim_granted_start(const char *user);

/** @brief True while the outputs are held (from a safe-stop until a granted run's first copy-out, or an explicit STOP). */
bool plc_output_hold_is_set(void);

/** @brief True while every start needs a grant (from a safe-stop until a granted run's first copy-out). */
bool plc_start_gate_is_set(void);

/**
 * @brief Ask the dispatcher to safe-stop the PLC: abandon the scan, hold the outputs.
 *
 * Asynchronous. The dispatcher applies it at its next tick; a transition already
 * in flight (a user STOP) wins and the request is dropped. Behaviour:
 * docs/HALTED_SEMANTICS.md.
 *
 * @param reason short text kept in the safe-stop record, e.g. "heartbeat_timeout"
 * @return false when no dispatcher is running to apply it
 */
bool plc_request_safe_stop(const char *reason);

/**
 * @brief Hold a task (or PLC_ALL_TASKS) at its scan boundary.
 *
 * The dispatcher stops releasing it and stops counting its overruns and stuck
 * ticks. Under a safe halt policy the configured outputs in the held scope take
 * their safe values once the held tasks are idle. Called by run control.
 *
 * @param task_idx  index into plc_tasks, or PLC_ALL_TASKS
 * @param timeout_ms how long to wait for the dispatcher to apply the hold
 * @return true when every targeted task is held
 */
bool plc_run_control_hold(size_t task_idx, int timeout_ms);

/**
 * @brief Resume a held task (or PLC_ALL_TASKS).
 *
 * Saved values of outputs the safe policy changed are journaled back first; the
 * dispatcher then re-arms the stuck and overrun checks (stuck count 0, release
 * time moved forward by the hold duration) and releases at the next due tick.
 *
 * @return true when every targeted task is running again
 */
bool plc_run_control_resume(size_t task_idx, int timeout_ms);

/**
 * @brief Virtual time of one task, in ns since the program started (ADR-004 D4).
 *
 * The time its last released scan read through TIME() and the timer FBs, or the
 * due tick it skipped as an overrun. Frozen while the task is held; a halt advances
 * it by at most one interval. Equal to the wall schedule when nothing ever halts.
 *
 * @return the task's virtual time, or -1 when no program is running or the index names no task
 */
int64_t plc_task_virtual_time_ns(size_t task_idx);

/** @brief Virtual time of the fastest task (the plugins' default clock), or -1 with no program. */
int64_t plc_virtual_time_ns(void);

/**
 * @brief Apply a safe_stop.hold file found at boot.
 *
 * GATE_ONLY sets the start gate and otherwise boots as usual. VALUES also sets the
 * hold and enters programless SAFE_STOPPED: plugins started with the held values,
 * and a dispatcher without tasks keeping them exchanging.
 *
 * @return true when the runtime is now SAFE_STOPPED and must not auto-start or drive outputs off
 */
bool plc_boot_from_hold_file(void);

/** @brief True when the current safe-stop survived a process restart. */
bool plc_safe_stop_runtime_restarted(void);

/**
 * @brief Cleanup the PLC state manager and unloads the plugin manager.
 * @return void
 */
void plc_state_manager_cleanup(void);

/**
 * @brief Force the PLC into ERROR state from any thread.
 *
 * This is intended for the watchdog thread to transition the PLC to ERROR
 * state without triggering program load/unload side effects (unlike plc_set_state).
 */
void plc_force_error_state(void);

/**
 * @brief Get the signal number that caused the last PLC crash.
 * @return The signal number (e.g. SIGFPE, SIGSEGV), or 0 if no crash occurred
 */
int plc_get_crash_signal(void);

#ifdef __cplusplus
}
#endif

#endif // PLC_STATE_MANAGER_H
