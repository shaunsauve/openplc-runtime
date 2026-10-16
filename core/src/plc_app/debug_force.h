// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/* The force registry (ADR-005 D5.3): the core agent's own record of which
 * leaves are forced, by whom, why, and until when. It lives in the core, not
 * in a client, so a force outlives a reload, a reconnect and the gateway
 * process itself, and it is gone when the program is unloaded or the runtime
 * restarts. Nothing here is persisted.
 *
 * A registry entry never drives the pin itself: `runtime_external_write`
 * does, at the dispatcher's quiescent window, exactly as a legacy 0x42 force
 * does. This is the bookkeeping around it — the leaf -> (arr, elem) mapping
 * lives here because the link speaks flat leaf indexes (as readLeaves does)
 * while the pin is addressed by array and element.
 *
 * Thread-safe: any thread may add or clear; the link's agent thread expires. */
#ifndef DEBUG_FORCE_H
#define DEBUG_FORCE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ADR-005 D5.1: P5's default and its maximum. P5's default is the gateway's
 * (it reads debug.conf); the maximum is this core's backstop, so a TTL past
 * it is refused even if a gateway forwards one. */
#define DBGF_MAX_TTL_MS 28800000LL /* 8 h */
#define DBGF_MAX_VALUE   512      /* the widest leaf strucpp_debug_read writes */
#define DBGF_MAX_OWNER   64
#define DBGF_MAX_REASON  256

/* One registry entry. `expires_ms` 0 means no TTL (a legacy 0x42 force, and
 * any force whose TTL the runtime has not given an end to). */
typedef struct
{
    uint32_t leaf;
    char     owner[DBGF_MAX_OWNER];
    char     reason[DBGF_MAX_REASON];
    uint8_t  value[DBGF_MAX_VALUE];
    uint16_t len;
    int64_t  expires_ms;
} dbgforce_t;

/* The flat leaf index of (arr, elem), and its inverse. Both need the loaded
 * program's debug-table shape, so neither is meaningful across an unload. */
int  dbgforce_leaf_of(uint8_t arr, uint16_t elem, uint32_t *leaf);
int  dbgforce_arr_elem_of(uint32_t leaf, uint8_t *arr, uint16_t *elem);

/* Register a force on `leaf`, replacing any force already there. `expires_ms`
 * is an absolute wall-clock instant (CLOCK_REALTIME ms) or 0 for none.
 * Returns 0, or -1 when the entry would not fit. */
int dbgforce_set(uint32_t leaf, const char *owner, const char *reason,
                 const uint8_t *bytes, uint16_t len, int64_t expires_ms);

/* Remove one force, or every force (unforceAll). Returns how many went. */
int dbgforce_clear(uint32_t leaf);
int dbgforce_clear_all(void);

/* Drop every expired force, enqueueing the matching unforce for each so the
 * pin is released at the next dispatcher drain. Returns how many went. */
int dbgforce_expire(int64_t now_ms);

/* Copy out the registry, and one entry by leaf. Counts returned; copy in
 * chunks of the count, the registry is not bounded small. */
int dbgforce_count(void);
int dbgforce_copy(dbgforce_t *out, int max);
int dbgforce_get(uint32_t leaf, dbgforce_t *out);

/* Empty the registry without enqueueing anything: the program is going away,
 * so its leaves and their pins are going with it (ADR-005 D5.3). */
void dbgforce_reset(void);

/* The legacy 0x42 hook (ADR-003 D4.4, ADR-005 D5.4): a force made through
 * the PDU is listed with owner `legacy` and no TTL. Called from the 0x42
 * handler only when its write journal entry was accepted. */
void dbgforce_legacy_set(uint8_t arr, uint16_t elem, const uint8_t *bytes, uint16_t len);
void dbgforce_legacy_clear(uint8_t arr, uint16_t elem);

#ifdef __cplusplus
}
#endif

#endif /* DEBUG_FORCE_H */