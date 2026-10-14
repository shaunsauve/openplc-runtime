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
struct BpRecord
{
    uint32_t probe;
    uint32_t hit_target;
    uint16_t slot;     /* the breakpoint's side slot, shared by its successive records */
    uint16_t cond_len; /* 0: unconditional */
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
    int32_t  slot; /* -1: the slot lives on in the replacing record */
    uint64_t snap[DBG_MAX_TASKS];
};

struct Desired
{
    std::vector<uint8_t> cond;
    uint32_t             hit_target;
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

/* Trap word (D6.3): kind | task << 8 | depth bound << 16 | generation << 32. */
std::atomic<uint64_t> g_trap{0};
std::atomic<uint32_t> g_attach_gen{0};

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

bool trap_matches(uint64_t trap, size_t task)
{
    const uint32_t kind = (uint32_t)(trap & 0xFF);
    if (kind == DBG_TRAP_NONE) return false;
    const uint32_t t = (uint32_t)((trap >> 8) & 0xFF);
    if (t != DBG_TASK_ANY && t != task) return false;
    const uint32_t bound = (uint32_t)((trap >> 16) & 0xFFFF);
    switch (kind)
    {
    case DBG_TRAP_PAUSE:
    case DBG_TRAP_STEP_IN: return true;
    case DBG_TRAP_STEP_OVER: return tl_depth <= bound;
    case DBG_TRAP_STEP_OUT: return tl_depth < bound;
    default: return false;
    }
}

// ---- Writer helpers (g_mu held) ----

/* QSBR retirement (D6.2): the unpublishing store has happened; fence ④, then snapshot every
 * task's qs. The record may be reused once each snapshot is even or has moved. */
void retire_locked(uint16_t rec, int32_t slot)
{
    std::atomic_thread_fence(std::memory_order_seq_cst);
    Retired r;
    r.rec  = rec;
    r.slot = slot;
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
            if (r.slot >= 0) g_free_slot.push_back((uint16_t)r.slot);
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

dbg_status_t set_locked(uint32_t probe, const uint8_t *cond, size_t cond_len, uint32_t hit_target)
{
    if (!g_bp) return DBG_E_NO_PROBES;
    if (probe >= g_count) return DBG_E_INVALID_LOCATION;
    if (cond_len && !cond_verify(cond, cond_len)) return DBG_E_CONDITION;
    const int32_t cur = g_cur[probe];
    if (cur < 0 && g_nbps >= DBG_MAX_BREAKPOINTS)
    {
        g_limit_refusals++;
        return DBG_E_LIMIT;
    }
    if (g_free_rec.empty() || (cur < 0 && g_free_slot.empty())) reclaim_locked();
    if (g_free_rec.empty() || (cur < 0 && g_free_slot.empty()))
    {
        /* Never wait on the scan side: the request is refused instead (D6.2). */
        g_limit_refusals++;
        return DBG_E_LIMIT;
    }
    const uint16_t rec = g_free_rec.back();
    g_free_rec.pop_back();
    uint16_t slot;
    if (cur >= 0)
    {
        slot = g_pool[cur].slot;
    }
    else
    {
        slot = g_free_slot.back();
        g_free_slot.pop_back();
        g_slots[slot].hits.store(0, std::memory_order_relaxed);
        g_slots[slot].oneshot_taken.store(0, std::memory_order_relaxed);
    }
    BpRecord &r  = g_pool[rec];
    r.probe      = probe;
    r.hit_target = hit_target;
    r.slot       = slot;
    r.cond_len   = (uint16_t)cond_len;
    if (cond_len) std::memcpy(r.cond, cond, cond_len);

    g_bp[probe].store(&r, std::memory_order_release); /* ① */
    flag_store(probe, (uint8_t)(flag_load(probe) | DBG_FLAG_ARMED));
    if (cur >= 0)
        retire_locked((uint16_t)cur, -1);
    else
        g_nbps++;
    g_cur[probe] = rec;
    /* D4.13: every probe reached after the response sees the flag. */
    std::atomic_thread_fence(std::memory_order_seq_cst);
    return DBG_OK;
}

bool clear_locked(uint32_t probe)
{
    if (!g_bp || probe >= g_count || g_cur[probe] < 0) return false;
    const int32_t cur = g_cur[probe];
    flag_store(probe, (uint8_t)(flag_load(probe) & ~DBG_FLAG_ARMED));
    g_bp[probe].store(nullptr, std::memory_order_release);
    retire_locked((uint16_t)cur, g_pool[cur].slot);
    g_cur[probe] = -1;
    g_nbps--;
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

    const bool trap_wants = trap_matches(g_trap.load(std::memory_order_relaxed), tl_idx);

    bool     bp_wants = false;
    uint32_t hits     = 0;
    const BpRecord *r = g_bp[id].load(std::memory_order_acquire);
    if (r)
    {
        const int c = r->cond_len ? cond_eval(r->cond, r->cond_len) : 1;
        if (c < 0)
        {
            g_condition_errors.fetch_add(1, std::memory_order_relaxed);
        }
        else if (c > 0)
        {
            hits     = g_slots[r->slot].hits.fetch_add(1, std::memory_order_relaxed) + 1;
            bp_wants = r->hit_target <= 1 || hits >= r->hit_target;
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

    timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    t->stop.reason   = bp_wants ? DBG_STOP_BREAKPOINT
                     : ((g_trap.load(std::memory_order_relaxed) & 0xFF) == DBG_TRAP_PAUSE ? DBG_STOP_PAUSE
                                                                                          : DBG_STOP_STEP);
    t->stop.probe    = id;
    t->stop.hits     = hits;
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
    for (uint32_t p = 0; p < probe_count; ++p) flag_store(p, 0);

    /* Breakpoints kept across a stop: re-armed only for the same probe space. */
    if (probe_count != g_last_count) g_desired.clear();
    g_last_count = probe_count;
    for (auto &d : g_desired)
        set_locked(d.first, d.second.cond.data(), d.second.cond.size(), d.second.hit_target);
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

extern "C" dbg_status_t debug_agent_set_breakpoint(uint32_t probe, const uint8_t *cond,
                                                   size_t cond_len, uint32_t hit_target)
{
    std::lock_guard<std::mutex> g(g_mu);
    const dbg_status_t s = set_locked(probe, cond, cond_len, hit_target);
    if (s == DBG_OK) g_desired[probe] = Desired{std::vector<uint8_t>(cond, cond + cond_len), hit_target};
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

extern "C" void debug_agent_reclaim(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    reclaim_locked();
}

extern "C" void debug_agent_set_trap(dbg_trap_kind_t kind, uint32_t task, uint32_t depth_bound)
{
    std::lock_guard<std::mutex> g(g_mu);
    const uint64_t pred = (uint64_t)(kind & 0xFF) | (uint64_t)(task & 0xFF) << 8 |
                          (uint64_t)(depth_bound & 0xFFFF) << 16 | (uint64_t)(++g_trap_gen) << 32;
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
        out[n].pou  = f->pou;
        out[n].at   = n == 0 ? stop->probe : f->at;
        out[n].self = f->self;
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
