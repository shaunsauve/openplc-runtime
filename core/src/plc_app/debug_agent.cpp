// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file debug_agent.cpp
 * @brief The probe slow path, the frame chain and the breakpoint table (ADR-001 D2.1, D3, D6).
 *
 * Scan-thread code (strucpp_dbg_hit, the frame calls) takes no lock, allocates nothing, makes no
 * syscall and logs nothing (N002); the only syscalls on a stop are the host's park futexes.
 * Everything else is the writer side, serialised on g_mu.
 *
 * See also:
 *   core/src/plc_app/debug_agent.h - the interface and its threading contract.
 *   core/src/plc_app/plc_state_manager.cpp - the host's park and the dispatcher's resume.
 *   core/src/plc_app/debug_link.cpp - the agent thread that consumes stops and reclaims records.
 */

#include "debug_agent.h"

#include <atomic>
#include <cerrno>
#include <cstring>
#include <ctime>
#include <map>
#include <mutex>
#include <vector>

#include <semaphore.h>

#include "../lib/strucpp_abi.hpp"

namespace {

/* Immutable once published: the agent fills it, publishes it by a release store of its pointer,
 * and never writes it again until a grace period after it was unpublished. */
struct BpSpec
{
    uint32_t hit_target; /* 0 or 1: every hit; n: from the n-th hit on */
    uint32_t instance;   /* the instance's leaf index, or DBG_NO_INSTANCE for any */
    int32_t  id;         /* the id the client knows this breakpoint by */
    uint16_t slot;       /* the breakpoint's side slot: its hit count and its one-shot flag */
    uint16_t cond_off;   /* offset into the record's condition buffer */
    uint16_t cond_len;   /* 0: unconditional */
    uint8_t  one_shot;   /* clears itself on its first hit */
    uint8_t  enabled;    /* 0: listed, not armed */
    uint8_t  kind;       /* DBG_KIND_* */
};

struct BpRecord
{
    uint32_t probe;
    uint8_t  nspec;
    BpSpec  spec[DBG_MAX_PER_PROBE];
    uint8_t  cond[DBG_COND_MAX];
};

/* Mutable per-breakpoint state. Scan threads only fetch_add / exchange it, inside a section. */
struct SideSlot
{
    std::atomic<uint32_t> hits{0};
    std::atomic<uint8_t>  oneshot_taken{0};
};

struct TaskSlot
{
    /* Odd while the task's thread is inside a read-side section. One writer: that thread. */
    std::atomic<uint64_t> qs{0};
    dbg_stop_t            stop{};
    std::atomic<bool>     stop_pending{false};
};

struct Retired
{
    uint16_t rec;
    /* The side slots this record owned that no replacing record took over. -1: none. */
    int32_t  slots[DBG_MAX_PER_PROBE];
    uint8_t  nslots;
    uint64_t snap[DBG_MAX_TASKS];
};

struct DesiredSpec
{
    std::vector<uint8_t> cond;
    uint32_t             hit_target;
    uint32_t             instance;
    uint8_t              kind;
    bool                 one_shot;
    bool                 enabled;
};

struct Desired
{
    std::vector<DesiredSpec> specs;
};

dbg_host_t g_host = {nullptr};

/* Bound program. Written by bind/unbind with no task thread alive, read by the slow path. */
volatile uint8_t                    *g_flags      = nullptr;
uint32_t                             g_count      = 0;
uint8_t                             *g_active     = nullptr;
int                                (*g_lock_depth)(void) = nullptr;
size_t                               g_task_count = 0;
std::atomic<const BpRecord *>       *g_bp         = nullptr;

BpRecord g_pool[DBG_POOL_SIZE];
SideSlot g_slots[DBG_POOL_SIZE];
TaskSlot g_tasks[DBG_MAX_TASKS];

/* Trap word (D6.3): kind | task << 8 | generation << 16 | bound << 32. The bound is a frame depth,
 * or a probe ID for AT_PROBE, so it takes the 32-bit half. */
std::atomic<uint64_t> g_trap{0};
std::atomic<uint32_t> g_attach_gen{0};
/* F037's start-halted restart: one entry stop armed per task, cleared by the task that takes it.
 * A separate flag per task, not one trap, so a sibling task cannot spend another's entry stop. */
std::atomic<uint8_t> g_entry[DBG_MAX_TASKS];

std::atomic<uint64_t> g_condition_errors{0};
std::atomic<uint64_t> g_skipped_locked{0};

thread_local TaskSlot          *tl_task  = nullptr;
thread_local size_t             tl_idx   = 0;
thread_local strucpp_dbg_frame *tl_head  = nullptr;
thread_local uint32_t           tl_depth = 0;

/* ---- Writer side, under g_mu ---- */
std::mutex                  g_mu;
std::vector<uint16_t>       g_free_rec, g_free_slot;
std::vector<Retired>        g_retired;
std::vector<int32_t>        g_cur; /* per probe: published record index, or -1 */
uint32_t                    g_nbps       = 0;
uint32_t                    g_trap_gen   = 0;
bool                        g_attached   = false;
uint32_t                    g_last_count = 0;
std::map<uint32_t, Desired> g_desired; /* kept across a stop, re-armed at the next bind */
uint64_t                    g_records_reused = 0, g_limit_refusals = 0;

sem_t g_wake;
struct WakeInit
{
    WakeInit() { sem_init(&g_wake, 0, 0); }
} g_wake_init;

inline uint8_t flag_load(uint32_t p) { return __atomic_load_n(&g_flags[p], __ATOMIC_RELAXED); }
inline void    flag_store(uint32_t p, uint8_t v) { __atomic_store_n(&g_flags[p], v, __ATOMIC_RELAXED); }

// ---- Condition bytecode ----
// TODO: ADR-007's VM replaces this stand-in. It has no variable loads: it exists so a record
// carries a variable-length program the scan thread evaluates, which is what D6.2 protects.
enum : uint8_t { OP_END = 0x00, OP_PUSH = 0x01, OP_LT = 0x02, OP_GT = 0x03, OP_EQ = 0x04,
                 OP_AND = 0x05, OP_OR = 0x06, OP_NOT = 0x07 };
const int kCondStack = 16;

/* 1 true, 0 false, -1 not evaluable. Bounds-checked: a torn program must not crash the task. */
int cond_eval(const uint8_t *c, size_t n)
{
    int64_t st[kCondStack];
    int     sp = 0;
    size_t  pc = 0;
    while (pc < n)
    {
        const uint8_t op = c[pc++];
        switch (op)
        {
        case OP_END:
            return (sp == 1 && pc == n) ? (st[0] != 0) : -1;
        case OP_PUSH:
        {
            if (pc + 4 > n || sp >= kCondStack) return -1;
            uint32_t v = (uint32_t)c[pc] | (uint32_t)c[pc + 1] << 8 | (uint32_t)c[pc + 2] << 16 |
                         (uint32_t)c[pc + 3] << 24;
            st[sp++] = (int32_t)v;
            pc += 4;
            break;
        }
        case OP_NOT:
            if (sp < 1) return -1;
            st[sp - 1] = !st[sp - 1];
            break;
        case OP_LT: case OP_GT: case OP_EQ: case OP_AND: case OP_OR:
        {
            if (sp < 2) return -1;
            const int64_t b = st[--sp], a = st[sp - 1];
            st[sp - 1] = op == OP_LT ? a < b : op == OP_GT ? a > b : op == OP_EQ ? a == b
                       : op == OP_AND ? (a && b) : (a || b);
            break;
        }
        default:
            return -1;
        }
    }
    return -1;
}

/* The verifier the agent runs before publishing (ADR-007's role): a stack-depth simulation. */
bool cond_verify(const uint8_t *c, size_t n)
{
    if (n > DBG_COND_MAX) return false;
    int    sp = 0;
    size_t pc = 0;
    while (pc < n)
    {
        const uint8_t op = c[pc++];
        if (op == OP_END) return sp == 1 && pc == n;
        if (op == OP_PUSH) { if (pc + 4 > n || ++sp > kCondStack) return false; pc += 4; }
        else if (op == OP_NOT) { if (sp < 1) return false; }
        else if (op >= OP_LT && op <= OP_OR) { if (sp < 2) return false; --sp; }
        else return false;
    }
    return false;
}

bool trap_matches(uint64_t trap, size_t task, uint32_t probe)
{
    const uint32_t kind = (uint32_t)(trap & 0xFF);
    if (kind == DBG_TRAP_NONE) return false;
    const uint32_t t = (uint32_t)((trap >> 8) & 0xFF);
    if (t != DBG_TASK_ANY && t != task) return false;
    const uint32_t bound = (uint32_t)(trap >> 32);
    switch (kind)
    {
    case DBG_TRAP_PAUSE:
    case DBG_TRAP_STEP_IN: return true;
    case DBG_TRAP_STEP_OVER: return tl_depth <= bound;
    case DBG_TRAP_STEP_OUT: return tl_depth < bound;
    case DBG_TRAP_AT_PROBE: return probe == bound;
    /* One shot per task: the task that took its entry stop returns at once until the agent
     * clears the trap, which it does as soon as the first entry stop lands (D4.11). */
    case DBG_TRAP_ENTRY: return task < DBG_MAX_TASKS && g_entry[task].load(std::memory_order_relaxed);
    default: return false;
    }
}

// ---- Writer helpers (g_mu held) ----

/* QSBR retirement (D6.2): the unpublishing store has happened; fence ④, then snapshot every
 * task's qs. The record may be reused once each snapshot is even or has moved. The side slots
 * the record owned and no replacing record took over are freed with it. */
void retire_locked(uint16_t rec, const int32_t *slots, int nslots)
{
    std::atomic_thread_fence(std::memory_order_seq_cst);
    Retired r;
    r.rec    = rec;
    r.nslots = (uint8_t)nslots;
    for (int i = 0; i < nslots && i < DBG_MAX_PER_PROBE; ++i) r.slots[i] = slots[i];
    for (size_t t = 0; t < DBG_MAX_TASKS; ++t)
        r.snap[t] = t < g_task_count ? g_tasks[t].qs.load(std::memory_order_acquire) : 0;
    g_retired.push_back(r);
}

void reclaim_locked(void)
{
    size_t keep = 0;
    for (size_t i = 0; i < g_retired.size(); ++i)
    {
        const Retired &r      = g_retired[i];
        bool           passed = true;
        for (size_t t = 0; t < g_task_count && passed; ++t)
            passed = (r.snap[t] & 1) == 0 ||
                     g_tasks[t].qs.load(std::memory_order_acquire) != r.snap[t];
        if (passed)
        {
            g_free_rec.push_back(r.rec);
            for (uint8_t s = 0; s < r.nslots; ++s)
                if (r.slots[s] >= 0) g_free_slot.push_back((uint16_t)r.slots[s]);
            g_records_reused++;
        }
        else
        {
            g_retired[keep++] = r;
        }
    }
    g_retired.resize(keep);
}

void reset_pools_locked(void)
{
    g_free_rec.clear();
    g_free_slot.clear();
    g_retired.clear();
    for (int i = DBG_POOL_SIZE - 1; i >= 0; --i)
    {
        g_free_rec.push_back((uint16_t)i);
        g_free_slot.push_back((uint16_t)i);
    }
    g_nbps = 0;
}

/* Publishes the breakpoints at a probe. `slots[i]` is the side slot the breakpoint keeps
 * (0xFFFF: take a free one). The record is immutable once published: the agent fills it and
 * publishes it by a release store of its pointer (①). */
dbg_status_t publish_locked(uint32_t probe, const dbg_bp_spec_t *specs, size_t count,
                            const uint16_t *slots)
{
    if (!g_bp) return DBG_E_NO_PROBES;
    if (probe >= g_count) return DBG_E_INVALID_LOCATION;
    if (count > DBG_MAX_PER_PROBE) return DBG_E_LIMIT;
    size_t need = 0;
    for (size_t i = 0; i < count; ++i)
    {
        if (specs[i].cond_len && !cond_verify(specs[i].cond, specs[i].cond_len)) return DBG_E_CONDITION;
        need += specs[i].cond_len;
    }
    /* The condition bytes of one probe share the record's buffer. */
    if (need > DBG_COND_MAX) return DBG_E_CONDITION;

    const int32_t   cur = g_cur[probe];
    const BpRecord *old = cur >= 0 ? &g_pool[cur] : nullptr;
    const size_t    old_n = old ? old->nspec : 0;
    /* The count the probe holds after this call: what it holds now, minus what it holds and
     * loses, plus what it gains. The limit is on the number of breakpoints, not of probes. */
    const int32_t want = (int32_t)g_nbps - (int32_t)old_n + (int32_t)count;
    if (want > DBG_MAX_BREAKPOINTS)
    {
        g_limit_refusals++;
        return DBG_E_LIMIT;
    }
    size_t fresh = 0;
    for (size_t i = 0; i < count; ++i) if (slots[i] == 0xFFFF) fresh++;
    if (g_free_rec.empty() || g_free_slot.size() < fresh) reclaim_locked();
    if (g_free_rec.empty() || g_free_slot.size() < fresh)
    {
        /* Never wait on the scan side: the request is refused instead (D6.2). */
        g_limit_refusals++;
        return DBG_E_LIMIT;
    }
    const uint16_t rec = g_free_rec.back();
    g_free_rec.pop_back();
    BpRecord &r  = g_pool[rec];
    r.probe      = probe;
    r.nspec      = (uint8_t)count;
    size_t       coff = 0;
    for (size_t i = 0; i < count; ++i)
    {
        BpSpec &s    = r.spec[i];
        s.hit_target = specs[i].hit_target;
        s.instance   = specs[i].instance;
        s.id         = specs[i].id;
        s.cond_off   = (uint16_t)coff;
        s.cond_len   = (uint16_t)specs[i].cond_len;
        s.one_shot   = specs[i].one_shot ? 1 : 0;
        s.enabled    = specs[i].enabled ? 1 : 0;
        s.kind       = specs[i].kind;
        if (slots[i] != 0xFFFF)
        {
            s.slot = slots[i];
        }
        else
        {
            s.slot = g_free_slot.back();
            g_free_slot.pop_back();
            g_slots[s.slot].hits.store(0, std::memory_order_relaxed);
            g_slots[s.slot].oneshot_taken.store(0, std::memory_order_relaxed);
        }
        if (specs[i].cond_len) std::memcpy(r.cond + coff, specs[i].cond, specs[i].cond_len);
        coff += specs[i].cond_len;
    }

    g_bp[probe].store(&r, std::memory_order_release); /* ① */
    /* The probe is armed when at least one of its breakpoints is. */
    bool armed = false;
    for (size_t i = 0; i < count; ++i) armed |= specs[i].enabled;
    if (armed) flag_store(probe, (uint8_t)(flag_load(probe) | DBG_FLAG_ARMED));
    else        flag_store(probe, (uint8_t)(flag_load(probe) & ~DBG_FLAG_ARMED));
    if (old)
    {
        /* The slots the new list did not take over travel with the retired record. */
        int32_t freed[DBG_MAX_PER_PROBE];
        int    nfreed = 0;
        for (size_t i = count; i < old_n && nfreed < DBG_MAX_PER_PROBE; ++i)
            freed[nfreed++] = old->spec[i].slot;
        retire_locked((uint16_t)cur, freed, nfreed);
    }
    else
    {
        g_nbps += (uint32_t)count;
    }
    g_cur[probe] = rec;
    /* D4.13: every probe reached after the response sees the flag. */
    std::atomic_thread_fence(std::memory_order_seq_cst);
    return DBG_OK;
}

/* The old specs of one kind (or of every other kind), in order, with their slots. */
size_t old_of_kind(const BpRecord *old, uint8_t kind, bool same, dbg_bp_spec_t *out,
                   uint16_t *slots, size_t max)
{
    size_t n = 0;
    if (!old) return 0;
    for (uint8_t i = 0; i < old->nspec && n < max; ++i)
    {
        const BpSpec &s = old->spec[i];
        if (same ? s.kind != kind : s.kind == kind) continue;
        out[n].cond       = old->cond + s.cond_off;
        out[n].cond_len   = s.cond_len;
        out[n].hit_target = s.hit_target;
        out[n].instance   = s.instance;
        out[n].id         = s.id;
        out[n].kind       = s.kind;
        out[n].one_shot   = s.one_shot != 0;
        out[n].enabled    = s.enabled != 0;
        slots[n]          = s.slot;
        n++;
    }
    return n;
}

/* Arms or replaces the breakpoints of one kind at a probe. A replace keeps the hit count of
 * every breakpoint it keeps, matched by its position among the breakpoints of that kind: the
 * side slot travels with the position. The slots the old record held and the new list does
 * not take over are freed with it. */
dbg_status_t set_locked(uint32_t probe, uint8_t kind, const dbg_bp_spec_t *specs, size_t count)
{
    if (!g_bp) return DBG_E_NO_PROBES;
    if (probe >= g_count) return DBG_E_INVALID_LOCATION;
    if (count > DBG_MAX_PER_PROBE) return DBG_E_LIMIT;
    const int32_t   cur = g_cur[probe];
    const BpRecord *old = cur >= 0 ? &g_pool[cur] : nullptr;

    dbg_bp_spec_t merged[DBG_MAX_PER_PROBE];
    uint16_t      slots[DBG_MAX_PER_PROBE];
    size_t        n = 0;
    /* The kinds this call does not touch keep their place. */
    if (kind != DBG_KIND_ANY)
    {
        dbg_bp_spec_t other[DBG_MAX_PER_PROBE];
        uint16_t      other_slots[DBG_MAX_PER_PROBE];
        const size_t  n_other = old_of_kind(old, kind, false, other, other_slots, DBG_MAX_PER_PROBE);
        for (size_t i = 0; i < n_other && n < DBG_MAX_PER_PROBE; ++i)
        {
            merged[n] = other[i];
            slots[n]  = other_slots[i];
            n++;
        }
    }
    /* The breakpoints of this kind that the old record held: their slots are the hit counts. */
    dbg_bp_spec_t same[DBG_MAX_PER_PROBE];
    uint16_t      same_slots[DBG_MAX_PER_PROBE];
    const size_t  n_same = old_of_kind(old, kind, true, same, same_slots, DBG_MAX_PER_PROBE);
    for (size_t i = 0; i < count; ++i)
    {
        if (n >= DBG_MAX_PER_PROBE) return DBG_E_LIMIT;
        merged[n] = specs[i];
        slots[n]  = i < n_same ? same_slots[i] : 0xFFFF;
        n++;
    }
    return publish_locked(probe, merged, n, slots);
}

/* A one-shot that has fired leaves the breakpoint list (ADR-003, the per-breakpoint request):
 * the writer side drops it, so the list a client reads no longer names it. The scan thread
 * never writes the table; it only marks the slot, and this sweep does the rest. */
void sweep_oneshots_locked(void)
{
    for (uint32_t p = 0; g_bp && p < g_count; ++p)
    {
        const int32_t cur = g_cur[p];
        if (cur < 0) continue;
        const BpRecord &r = g_pool[cur];
        bool spent = false;
        for (uint8_t i = 0; i < r.nspec; ++i)
            if (r.spec[i].one_shot &&
                g_slots[r.spec[i].slot].oneshot_taken.load(std::memory_order_relaxed))
                spent = true;
        if (!spent) continue;
        dbg_bp_spec_t specs[DBG_MAX_PER_PROBE];
        uint16_t      slots[DBG_MAX_PER_PROBE];
        size_t        n = 0;
        for (uint8_t i = 0; i < r.nspec && n < DBG_MAX_PER_PROBE; ++i)
        {
            const BpSpec &s = r.spec[i];
            if (s.one_shot && g_slots[s.slot].oneshot_taken.load(std::memory_order_relaxed)) continue;
            specs[n].cond       = r.cond + s.cond_off;
            specs[n].cond_len   = s.cond_len;
            specs[n].hit_target = s.hit_target;
            specs[n].one_shot   = s.one_shot != 0;
            specs[n].enabled    = s.enabled != 0;
            slots[n]            = s.slot;
            n++;
        }
        publish_locked(p, specs, n, slots);
    }
}

/* Trap publication (D6.3). The predicate first, then TRAP in every flag byte: a probe that sees
 * TRAP reaches the slow path, whose acquire fence pairs with the release fence here. Under g_mu. */
void set_trap_locked(dbg_trap_kind_t kind, uint32_t task, uint32_t bound)
{
    const uint64_t pred = (uint64_t)(kind & 0xFF) | (uint64_t)(task & 0xFF) << 8 |
                          (uint64_t)(++g_trap_gen & 0xFFFF) << 16 | (uint64_t)bound << 32;
    g_trap.store(pred, std::memory_order_relaxed);
    /* Pairs with the slow path's fence through the flag byte: a probe that sees TRAP reads pred. */
    std::atomic_thread_fence(std::memory_order_release);
    for (uint32_t p = 0; g_flags && p < g_count; ++p)
    {
        const uint8_t v = flag_load(p);
        if (!(v & DBG_FLAG_TRAP)) flag_store(p, (uint8_t)(v | DBG_FLAG_TRAP));
    }
    std::atomic_thread_fence(std::memory_order_seq_cst);
}

bool clear_locked(uint32_t probe)
{
    if (!g_bp || probe >= g_count || g_cur[probe] < 0) return false;
    const int32_t cur = g_cur[probe];
    const BpRecord &r = g_pool[cur];
    flag_store(probe, (uint8_t)(flag_load(probe) & ~DBG_FLAG_ARMED));
    g_bp[probe].store(nullptr, std::memory_order_release);
    int32_t freed[DBG_MAX_PER_PROBE];
    for (uint8_t i = 0; i < r.nspec; ++i) freed[i] = r.spec[i].slot;
    retire_locked((uint16_t)cur, freed, r.nspec);
    g_cur[probe] = -1;
    g_nbps -= r.nspec;
    return true;
}

}  // namespace

// ---------------------------------------------------------------------------
// Scan-thread side: the probe ABI
// ---------------------------------------------------------------------------

extern "C" void strucpp_dbg_hit(uint32_t id)
{
    TaskSlot *t = tl_task;
    if (!t || id >= g_count) return;

    /* Enter (D6.2): an RMW, so each odd value heads into the previous leave's release sequence. */
    const uint64_t q = t->qs.fetch_add(1, std::memory_order_relaxed);
    std::atomic_thread_fence(std::memory_order_seq_cst); /* ③; also D2.1's acquire */

    const uint64_t trap        = g_trap.load(std::memory_order_relaxed);
    const bool     trap_wants  = trap_matches(trap, tl_idx, id);
    const bool     entry_wants = trap_wants && (uint32_t)(trap & 0xFF) == DBG_TRAP_ENTRY;

    bool     bp_wants = false;
    uint32_t hits     = 0;
    int32_t  bp_id    = 0;
    const BpRecord *r = g_bp[id].load(std::memory_order_acquire);
    if (r)
    {
        /* The probe carries a list of breakpoints (the per-breakpoint request). The first one
         * that wants the stop takes it; a disabled one is listed but never stops, and a one-shot
         * that has already fired is spent. */
        for (uint8_t i = 0; i < r->nspec; ++i)
        {
            const BpSpec &s = r->spec[i];
            if (!s.enabled) continue;
            if (s.one_shot && g_slots[s.slot].oneshot_taken.load(std::memory_order_relaxed)) continue;
            /* An instance breakpoint stops only in the instance it names: the innermost frame's
             * `this` is mapped to its first leaf, and the leaf is what the gateway resolved. */
            if (s.instance != DBG_NO_INSTANCE)
            {
                const void *self = tl_head ? tl_head->self : nullptr;
                if (!self || (uint32_t)debug_agent_leaf_of(self) != s.instance) continue;
            }
            const int c = s.cond_len ? cond_eval(r->cond + s.cond_off, s.cond_len) : 1;
            if (c < 0)
            {
                g_condition_errors.fetch_add(1, std::memory_order_relaxed);
                continue;
            }
            if (c == 0) continue;
            const uint32_t h = g_slots[s.slot].hits.fetch_add(1, std::memory_order_relaxed) + 1;
            /* A hit count stops on the n-th hit (ADR-007 D5: "stop on the Nth hit"), not from
             * the n-th on: the breakpoint fires once, on that hit. */
            if (s.hit_target <= 1 || h == s.hit_target)
            {
                bp_wants = true;
                hits     = h;
                bp_id    = s.id;
                if (s.one_shot) g_slots[s.slot].oneshot_taken.store(1, std::memory_order_relaxed);
                break;
            }
        }
    }

    t->qs.store(q + 2, std::memory_order_release); /* leave ②: before any park or throw */

    if (!bp_wants && !trap_wants) return;
    /* D1.3(2): never park holding a shared-global lock. */
    if (g_lock_depth && g_lock_depth() > 0)
    {
        if (bp_wants) g_skipped_locked.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    if (!g_host.park) return;
    /* The entry stop belongs to this task alone and is spent here, before the park: a task
     * resumed from it finishes that scan and every scan after without stopping at entry again. */
    if (entry_wants) g_entry[tl_idx].store(0, std::memory_order_relaxed);

    timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    t->stop.reason   = bp_wants ? DBG_STOP_BREAKPOINT
                     : entry_wants ? DBG_STOP_ENTRY
                     : ((g_trap.load(std::memory_order_relaxed) & 0xFF) == DBG_TRAP_PAUSE ? DBG_STOP_PAUSE
                                                                                          : DBG_STOP_STEP);
    t->stop.probe    = id;
    t->stop.hits     = hits;
    t->stop.bp_id    = bp_id;
    t->stop.depth    = tl_depth;
    t->stop.since_ns = (int64_t)now.tv_sec * 1000000000LL + now.tv_nsec;
    t->stop.head     = tl_head;
    t->stop_pending.store(true, std::memory_order_release);
    if (g_host.park(tl_idx) == DBG_PARK_ABANDON) throw plc_dbg::Abandon{};
}

extern "C" void strucpp_dbg_frame_link(strucpp_dbg_frame *f)
{
    f->prev   = tl_head;
    f->gen    = g_attach_gen.load(std::memory_order_relaxed);
    f->linked = 1;
    tl_head   = f;
    tl_depth++;
}

extern "C" void strucpp_dbg_frame_unlink(strucpp_dbg_frame *f)
{
    /* Frames are LIFO on one thread, so the one unlinking is the head. */
    tl_head   = f->prev;
    f->linked = 0;
    if (tl_depth) tl_depth--;
}

extern "C" void debug_agent_task_enter(size_t task_idx)
{
    if (task_idx >= DBG_MAX_TASKS) return;
    tl_idx   = task_idx;
    tl_task  = &g_tasks[task_idx];
    tl_head  = nullptr;
    tl_depth = 0;
}

extern "C" void debug_agent_task_exit(void)
{
    TaskSlot *t = tl_task;
    if (!t) return;
    /* A thread that died inside a section (a signal in the slow path) must not stall reclamation. */
    const uint64_t q = t->qs.load(std::memory_order_relaxed);
    if (q & 1) t->qs.store(q + 1, std::memory_order_release);
    tl_task  = nullptr;
    tl_head  = nullptr;
    tl_depth = 0;
}

// ---------------------------------------------------------------------------
// Writer side
// ---------------------------------------------------------------------------

extern "C" void debug_agent_set_host(const dbg_host_t *host)
{
    std::lock_guard<std::mutex> g(g_mu);
    g_host = host ? *host : dbg_host_t{nullptr};
}

extern "C" void debug_agent_bind(volatile uint8_t *flags, uint32_t probe_count, uint8_t *active,
                                 int (*lock_depth)(void), size_t task_count)
{
    std::lock_guard<std::mutex> g(g_mu);
    if (!flags || probe_count == 0) return;
    g_flags      = flags;
    g_count      = probe_count;
    g_active     = active;
    g_lock_depth = lock_depth;
    g_task_count = task_count < DBG_MAX_TASKS ? task_count : DBG_MAX_TASKS;
    g_bp         = new std::atomic<const BpRecord *>[probe_count];
    for (uint32_t p = 0; p < probe_count; ++p) g_bp[p].store(nullptr, std::memory_order_relaxed);
    g_cur.assign(probe_count, -1);
    reset_pools_locked();
    for (size_t t = 0; t < DBG_MAX_TASKS; ++t) g_tasks[t].stop_pending.store(false);
    g_trap.store(0, std::memory_order_relaxed);
    for (size_t t = 0; t < DBG_MAX_TASKS; ++t) g_entry[t].store(0, std::memory_order_relaxed);
    for (uint32_t p = 0; p < probe_count; ++p) flag_store(p, 0);

    /* Breakpoints kept across a stop: re-armed only for the same probe space. */
    if (probe_count != g_last_count) g_desired.clear();
    g_last_count = probe_count;
    for (auto &d : g_desired)
    {
        dbg_bp_spec_t specs[DBG_MAX_PER_PROBE];
        for (size_t i = 0; i < d.second.specs.size() && i < DBG_MAX_PER_PROBE; ++i)
        {
            const DesiredSpec &ds = d.second.specs[i];
            specs[i].cond       = ds.cond.data();
            specs[i].cond_len   = ds.cond.size();
            specs[i].hit_target = ds.hit_target;
            specs[i].instance   = ds.instance;
            specs[i].kind       = ds.kind;
            specs[i].one_shot   = ds.one_shot;
            specs[i].enabled    = ds.enabled;
        }
        set_locked(d.first, DBG_KIND_ANY, specs, d.second.specs.size());
    }
    if (g_active) __atomic_store_n(g_active, (uint8_t)(g_attached ? 1 : 0), __ATOMIC_RELAXED);
}

extern "C" void debug_agent_unbind(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    if (!g_bp) return;
    /* No task thread is left, so nothing can be inside a section: drop everything at once. */
    delete[] g_bp;
    g_bp         = nullptr;
    g_flags      = nullptr;
    g_active     = nullptr;
    g_lock_depth = nullptr;
    g_count      = 0;
    g_cur.clear();
    reset_pools_locked();
    g_trap.store(0, std::memory_order_relaxed);
    for (size_t t = 0; t < DBG_MAX_TASKS; ++t) g_entry[t].store(0, std::memory_order_relaxed);
}

extern "C" uint32_t debug_agent_probe_count(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    return g_count;
}

extern "C" void debug_agent_set_attached(bool attached)
{
    std::lock_guard<std::mutex> g(g_mu);
    g_attached = attached;
    /* N4: every frame linked after this attach carries a newer generation than any before it. */
    if (attached) g_attach_gen.fetch_add(1, std::memory_order_relaxed);
    if (g_active) __atomic_store_n(g_active, (uint8_t)(attached ? 1 : 0), __ATOMIC_RELAXED);
}

extern "C" bool debug_agent_attached(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    return g_attached;
}

/* Remembers what a probe holds, so a program that is loaded again re-arms the same
 * breakpoints (the hit counts travel with the side slots). */
void remember_desired_locked(uint32_t probe)
{
    const int32_t cur = g_cur[probe];
    if (cur < 0)
    {
        g_desired.erase(probe);
        return;
    }
    const BpRecord &r = g_pool[cur];
    Desired        d;
    for (uint8_t i = 0; i < r.nspec; ++i)
    {
        DesiredSpec ds;
        ds.cond       = std::vector<uint8_t>(r.cond + r.spec[i].cond_off,
                                              r.cond + r.spec[i].cond_off + r.spec[i].cond_len);
        ds.hit_target = r.spec[i].hit_target;
        ds.instance   = r.spec[i].instance;
        ds.kind       = r.spec[i].kind;
        ds.one_shot   = r.spec[i].one_shot != 0;
        ds.enabled    = r.spec[i].enabled != 0;
        d.specs.push_back(ds);
    }
    g_desired[probe] = d;
}

extern "C" dbg_status_t debug_agent_set_breakpoint(uint32_t probe, uint8_t kind,
                                                   const dbg_bp_spec_t *specs, size_t count)
{
    std::lock_guard<std::mutex> g(g_mu);
    const dbg_status_t s = set_locked(probe, kind, specs, count);
    if (s == DBG_OK) remember_desired_locked(probe);
    return s;
}

extern "C" dbg_status_t debug_agent_clear_kind(uint32_t probe, uint8_t kind)
{
    std::lock_guard<std::mutex> g(g_mu);
    if (!g_bp) return DBG_E_NO_PROBES;
    if (probe >= g_count) return DBG_E_INVALID_LOCATION;
    const dbg_status_t s = set_locked(probe, kind, nullptr, 0);
    if (s == DBG_OK) remember_desired_locked(probe);
    return s;
}

extern "C" dbg_status_t debug_agent_clear_breakpoint(uint32_t probe)
{
    std::lock_guard<std::mutex> g(g_mu);
    if (!g_bp) return DBG_E_NO_PROBES;
    if (probe >= g_count) return DBG_E_INVALID_LOCATION;
    clear_locked(probe);
    g_desired.erase(probe);
    return DBG_OK;
}

extern "C" uint32_t debug_agent_clear_all_breakpoints(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    uint32_t n = 0;
    for (uint32_t p = 0; g_bp && p < g_count && g_nbps > 0; ++p) n += clear_locked(p);
    g_desired.clear();
    return n;
}

extern "C" uint32_t debug_agent_breakpoint_count(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    return g_nbps;
}

extern "C" size_t debug_agent_breakpoint_at(uint32_t probe, dbg_bp_info_t *out, size_t max)
{
    std::lock_guard<std::mutex> g(g_mu);
    if (!g_bp || probe >= g_count || g_cur[probe] < 0) return 0;
    const BpRecord &r = g_pool[g_cur[probe]];
    size_t          n = r.nspec < max ? r.nspec : max;
    for (size_t i = 0; i < n; ++i)
    {
        out[i].hit_target = r.spec[i].hit_target;
        out[i].hits       = g_slots[r.spec[i].slot].hits.load(std::memory_order_relaxed);
        out[i].id         = r.spec[i].id;
        out[i].kind       = r.spec[i].kind;
        out[i].one_shot   = r.spec[i].one_shot != 0;
        out[i].enabled    = r.spec[i].enabled != 0;
    }
    return n;
}

extern "C" uint32_t debug_agent_armed_count(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    uint32_t n = 0;
    for (uint32_t p = 0; g_bp && p < g_count; ++p)
    {
        const int32_t cur = g_cur[p];
        if (cur < 0) continue;
        const BpRecord &r = g_pool[cur];
        for (uint8_t i = 0; i < r.nspec; ++i)
            if (r.spec[i].enabled) n++;
    }
    return n;
}

extern "C" void debug_agent_reclaim(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    sweep_oneshots_locked();
    reclaim_locked();
}

extern "C" void debug_agent_set_trap(dbg_trap_kind_t kind, uint32_t task, uint32_t bound)
{
    std::lock_guard<std::mutex> g(g_mu);
    set_trap_locked(kind, task, bound);
}

extern "C" void debug_agent_clear_trap(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    /* Predicate first: a probe that still sees a stale TRAP bit reads 0 and returns. */
    g_trap.store(0, std::memory_order_relaxed);
    for (uint32_t p = 0; g_flags && p < g_count; ++p)
    {
        const uint8_t v = flag_load(p);
        if (v & DBG_FLAG_TRAP) flag_store(p, (uint8_t)(v & ~DBG_FLAG_TRAP));
    }
    for (size_t t = 0; t < DBG_MAX_TASKS; ++t) g_entry[t].store(0, std::memory_order_relaxed);
}

extern "C" bool debug_agent_arm_entry(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    if (!g_bp || g_count == 0) return false;
    for (size_t t = 0; t < DBG_MAX_TASKS; ++t)
        g_entry[t].store(t < g_task_count ? 1 : 0, std::memory_order_relaxed);
    set_trap_locked(DBG_TRAP_ENTRY, DBG_TASK_ANY, 0);
    return true;
}

extern "C" size_t debug_agent_frames(const dbg_stop_t *stop, dbg_frame_t *out, size_t max,
                                     bool *truncated)
{
    *truncated = false;
    const strucpp_dbg_frame *f = stop ? stop->head : nullptr;
    if (!f) return 0;
    const uint32_t gen = f->gen;
    size_t         n   = 0;
    for (; f && n < max; f = f->prev)
    {
        /* N4: frames of an earlier attach may have holes between them and the newer ones. */
        if (f->gen != gen)
        {
            *truncated = true;
            break;
        }
        out[n].pou    = f->pou;
        out[n].at     = n == 0 ? stop->probe : f->at;
        out[n].self   = f->self;
        out[n].locals = f->locals;
        n++;
    }
    if (f && n == max) *truncated = true;
    return n;
}

namespace {
std::mutex               g_leaf_mu;
std::vector<const void *> g_leaf_addrs;
}  // namespace

extern "C" void debug_agent_set_leaves(const void *const *addrs, size_t count)
{
    std::lock_guard<std::mutex> g(g_leaf_mu);
    g_leaf_addrs.assign(addrs, addrs + (addrs ? count : 0));
}

extern "C" long debug_agent_leaf_of(const void *self)
{
    std::lock_guard<std::mutex> g(g_leaf_mu);
    if (!self) return -1;
    long        best = -1;
    uintptr_t   best_addr = UINTPTR_MAX;
    const auto  s = (uintptr_t)self;
    for (size_t i = 0; i < g_leaf_addrs.size(); ++i)
    {
        const auto a = (uintptr_t)g_leaf_addrs[i];
        if (a >= s && a < best_addr)
        {
            best      = (long)i;
            best_addr = a;
        }
    }
    return best;
}

extern "C" bool debug_agent_take_stop(size_t task_idx, dbg_stop_t *out)
{
    if (task_idx >= DBG_MAX_TASKS) return false;
    TaskSlot &t = g_tasks[task_idx];
    if (!t.stop_pending.exchange(false, std::memory_order_acq_rel)) return false;
    *out = t.stop;
    return true;
}

extern "C" void debug_agent_wake(void)
{
    sem_post(&g_wake);
}

extern "C" void debug_agent_wait(int timeout_ms)
{
    timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_sec += timeout_ms / 1000;
    ts.tv_nsec += (long)(timeout_ms % 1000) * 1000000L;
    if (ts.tv_nsec >= 1000000000L)
    {
        ts.tv_nsec -= 1000000000L;
        ts.tv_sec++;
    }
    while (sem_timedwait(&g_wake, &ts) != 0 && errno == EINTR) {}
    /* Collapse a burst of wakes into this one pass. */
    while (sem_trywait(&g_wake) == 0) {}
}

extern "C" void debug_agent_counters(dbg_counters_t *out)
{
    std::lock_guard<std::mutex> g(g_mu);
    out->condition_errors = g_condition_errors.load(std::memory_order_relaxed);
    out->skipped_locked   = g_skipped_locked.load(std::memory_order_relaxed);
    out->records_reused   = g_records_reused;
    out->limit_refusals   = g_limit_refusals;
}
