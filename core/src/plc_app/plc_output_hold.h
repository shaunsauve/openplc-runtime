// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file plc_output_hold.h
 * @brief Output values held across a safe-stop, the file that carries them across a
 * process restart, and the per-project halt policy (hold-last or safe values).
 *
 * The hold and start-gate flags live in plc_state_manager.cpp, under its
 * state_mutex; this module owns only values and files.
 *
 * See also:
 *   core/src/plc_app/plc_state_manager.cpp - safe-stop entry, hold flag, start gate.
 *   core/src/plc_app/image_tables.cpp - the output pointer tables the values come from.
 *   docs/HALTED_SEMANTICS.md - the behaviour this implements, statement by statement.
 */

#ifndef PLC_OUTPUT_HOLD_H
#define PLC_OUTPUT_HOLD_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C"
{
#endif

/* Default location of the hold file. Under /var/lib so it survives a reboot as well as a restart. */
#define PLC_SAFE_STOP_HOLD_PATH "/var/lib/openplc-runtime/safe_stop.hold"

/* Per-project halt policy, shipped in program.zip and extracted with the sources. */
#define PLC_HALT_POLICY_PATH "./core/generated/conf/halt_policy.json"

/* Most outputs a safe-state policy may name. */
#define PLC_HALT_POLICY_MAX_OUTPUTS 64

/* Longest safe-stop reason kept in the record. */
#define PLC_SAFE_STOP_REASON_LEN 64

/**
 * @brief Copy the value behind every bound output pointer into the held snapshot.
 *
 * Values, never pointers: the pointer tables are rebound on every load and the
 * backing buffers are zeroed by image_tables_fill_null_pointers(). Caller holds
 * the image-tables mutex.
 */
void output_hold_capture(void);

/**
 * @brief Write the held snapshot through the CURRENT output pointers.
 *
 * Caller holds the image-tables mutex. No-op when the snapshot is not valid.
 */
void output_hold_restore(void);

/** @brief True when the snapshot holds values (captured, or read from the hold file). */
bool output_hold_valid(void);

/** @brief Drop the snapshot. */
void output_hold_invalidate(void);

/**
 * @brief Override the hold file path (tests). NULL restores PLC_SAFE_STOP_HOLD_PATH.
 * @param path string with static storage duration, or NULL
 */
void output_hold_set_file_path(const char *path);

/** @brief Path of the hold file in use. */
const char *output_hold_file_path(void);

/**
 * @brief What the hold file carries.
 *
 * GATE_ONLY: an explicit STOP dropped the values but the start gate stays.
 * VALUES: the gate plus held output values to re-assert at boot.
 */
typedef enum
{
    OUTPUT_HOLD_FILE_NONE = 0,
    OUTPUT_HOLD_FILE_GATE_ONLY,
    OUTPUT_HOLD_FILE_VALUES,
} output_hold_file_kind_t;

/**
 * @brief Write the hold file now, on the calling thread (write, fsync, rename).
 *
 * Never call from the dispatcher; use output_hold_file_request() there.
 *
 * @param kind      GATE_ONLY or VALUES (VALUES writes the current snapshot)
 * @param period_ns dispatcher period to run the programless hold at after a restart
 * @return 0 on success, -1 on any I/O error (logged)
 */
int output_hold_file_write(output_hold_file_kind_t kind, int64_t period_ns);

/** @brief Delete the hold file now, on the calling thread. Missing file is success. */
int output_hold_file_delete(void);

/**
 * @brief Read the hold file. VALUES loads the snapshot and makes it valid.
 *
 * A file that cannot be parsed counts as GATE_ONLY: the start stays gated, and
 * nothing unverified is written to an output.
 *
 * @param period_ns out: the stored dispatcher period (0 when absent); may be NULL
 * @return the kind found; NONE when there is no file
 */
output_hold_file_kind_t output_hold_file_read(int64_t *period_ns);

/**
 * @brief Ask the hold-file thread to write or delete the file. Async-safe, non-blocking.
 *
 * For the dispatcher, which must not do file I/O. NONE means delete.
 */
void output_hold_file_request(output_hold_file_kind_t kind, int64_t period_ns);

/** @brief Block until every request made so far has been carried out (tests, shutdown). */
void output_hold_file_flush(void);

/** @brief Record why the safe-stop happened. Kept until the hold is cleared. */
void output_hold_set_reason(const char *reason);

/** @brief The safe-stop reason, or "" when none. */
const char *output_hold_reason(void);

/** @brief Note that a task had to be aborted during the safe-stop entry. */
void output_hold_note_task_fault(void);

/** @brief True when a task was aborted during the safe-stop entry. */
bool output_hold_task_faulted(void);

/* ---- Halt policy ---- */

typedef enum
{
    HALT_POLICY_HOLD = 0,
    HALT_POLICY_SAFE = 1,
} halt_policy_kind_t;

typedef enum
{
    HALT_OUT_BIT = 0,
    HALT_OUT_BYTE,
    HALT_OUT_WORD,
    HALT_OUT_DWORD,
    HALT_OUT_LWORD,
} halt_out_size_t;

typedef struct
{
    halt_out_size_t size;
    uint16_t        byte_index;
    uint8_t         bit_index;
    uint64_t        safe_value;
    uint64_t        saved_value; /* image value before the halt applied safe_value */
    bool            applied;     /* safe_value journaled, saved_value owed back */
} halt_policy_output_t;

typedef struct
{
    halt_policy_kind_t   kind;
    size_t               count;
    halt_policy_output_t outputs[PLC_HALT_POLICY_MAX_OUTPUTS];
} halt_policy_t;

/**
 * @brief Load the policy from path. A missing file is the default, hold.
 *
 * A file that is present but invalid also falls back to hold, and is logged:
 * hold never invents an output value, safe would.
 *
 * @return 0 when the file was absent or valid, -1 when it was invalid
 */
int halt_policy_load(const char *path);

/** @brief The loaded policy (read-only view, for the session read-back). */
const halt_policy_t *halt_policy_get(void);

/** @brief Reset to the default hold policy with no outputs. */
void halt_policy_reset(void);

/**
 * @brief Parse "%QX0.1", "%QB3", "%QW2", "%QD1", "%QL0". Only %Q is accepted.
 * @return true when loc names an output
 */
bool halt_policy_parse_location(const char *loc, halt_out_size_t *size, uint16_t *byte_index,
                                uint8_t *bit_index);

/**
 * @brief Decides which configured outputs a halt affects. NULL means all of them.
 * @return true when the output belongs to the halted scope
 */
typedef bool (*halt_policy_scope_fn)(const halt_policy_output_t *out, void *ctx);

/**
 * @brief Save the image value of each in-scope configured output and journal its safe value.
 *
 * Safe values go through the journal so a forced slot keeps its forced value
 * (force > policy). No-op under the hold policy. Caller must NOT hold the image
 * mutex; this takes it to read the current values.
 */
void halt_policy_apply_safe(halt_policy_scope_fn in_scope, void *ctx);

/**
 * @brief Journal the saved values of every applied output back, so no safe value outlives the halt.
 */
void halt_policy_restore_saved(void);

/**
 * @brief Journal the safe value of every configured output (safe-stop entry, all-stop scope).
 * No-op under the hold policy.
 */
void halt_policy_journal_safe_values(void);

#ifdef __cplusplus
}
#endif

#endif /* PLC_OUTPUT_HOLD_H */
