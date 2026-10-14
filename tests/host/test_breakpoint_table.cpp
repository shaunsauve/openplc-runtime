// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file test_breakpoint_table.cpp
 * @brief Host test: the breakpoint table racing the scan (ADR-001 D6.2-D6.3). Criteria: A205, A032.
 *
 * Links the debug agent alone. Scan threads call strucpp_dbg_hit exactly as a generated probe
 * does (a relaxed load of the flag byte, the slow path when non-zero); park is a stub that counts
 * stops. run.sh builds it twice, plain and under ThreadSanitizer: the TSan build is A205's race
 * check, and any report fails the run.
 *
 * See also:
 *   core/src/plc_app/debug_agent.cpp - the table, the slow path and QSBR reclamation.
 *   tests/host/redproof_breakpoint_table.sh - the naive tables this test must catch.
 *   tests/host/test_run_control.cpp - the same agent driven through the dispatcher and core link.
 */

// NO-FOLD: runtime-core

#include "../../core/src/plc_app/debug_agent.h"

#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <thread>
#include <vector>

extern "C" void strucpp_dbg_hit(uint32_t probe);

extern "C" {
void log_info(const char *, ...) {}
void log_warn(const char *, ...) {}
void log_error(const char *, ...) {}
void log_debug(const char *, ...) {}
}

static const uint32_t kProbes = 4096;
static volatile uint8_t g_flags[kProbes];
static uint8_t          g_active = 0;

static thread_local int tl_lock_depth = 0;
static int fake_lock_depth(void) { return tl_lock_depth; }

static std::atomic<long> g_parks{0};
static int fake_park(size_t task)
{
    dbg_stop_t s;
    g_parks.fetch_add(1);
    (void)debug_agent_take_stop(task, &s);
    return DBG_PARK_RESUME;
}

static int         g_failures = 0;
static const char *g_case     = "";

#define CHECK(cond, ...)                                                                           \
    do                                                                                             \
    {                                                                                              \
        if (!(cond))                                                                               \
        {                                                                                          \
            std::printf("  FAIL  [%s] ", g_case);                                                  \
            std::printf(__VA_ARGS__);                                                              \
            std::printf("\n");                                                                     \
            g_failures++;                                                                          \
        }                                                                                          \
    } while (0)

/* What STRUCPP_PROBE(id) compiles to under STRUCPP_THREADED. */
static inline void probe(uint32_t id)
{
    if (__builtin_expect(__atomic_load_n(&g_flags[id], __ATOMIC_RELAXED) != 0, 0))
        strucpp_dbg_hit(id);
}

// ---- Condition programs (the record's bytecode; ADR-007 stand-in, see debug_agent.cpp) ----

static const uint8_t OP_END = 0x00, OP_PUSH = 0x01, OP_LT = 0x02, OP_AND = 0x05;

static void push(std::vector<uint8_t> &b, int32_t v)
{
    b.push_back(OP_PUSH);
    for (int i = 0; i < 4; ++i) b.push_back((uint8_t)((uint32_t)v >> (8 * i)));
}

/* `0 < -1`: the short always-false condition. */
static std::vector<uint8_t> cond_short()
{
    std::vector<uint8_t> b;
    push(b, 0);
    push(b, -1);
    b.push_back(OP_LT);
    b.push_back(OP_END);
    return b;
}

/* A >= 400 B conjunction of always-false terms. */
static std::vector<uint8_t> cond_long()
{
    std::vector<uint8_t> b;
    push(b, 0);
    push(b, -1);
    b.push_back(OP_LT);
    for (int i = 1; b.size() < 400; ++i)
    {
        push(b, i);
        push(b, -i);
        b.push_back(OP_LT);
        b.push_back(OP_AND);
    }
    b.push_back(OP_END);
    return b;
}

static void bind_fresh(void)
{
    /* A clean table: breakpoints kept across an unbind would otherwise be re-armed. */
    debug_agent_clear_all_breakpoints();
    debug_agent_unbind();
    std::memset((void *)g_flags, 0, sizeof g_flags);
    debug_agent_bind(g_flags, kProbes, &g_active, fake_lock_depth, 2);
    debug_agent_set_attached(true);
}

// ---------------------------------------------------------------------------

/* A032: the declared limit is honoured, the one past it refused, and nothing displaced. */
static void case_limit(void)
{
    g_case = "A032 limit";
    bind_fresh();
    g_parks = 0;
    int ok = 0;
    for (uint32_t p = 0; p < DBG_MAX_BREAKPOINTS; ++p)
        ok += debug_agent_set_breakpoint(p, nullptr, 0, 0) == DBG_OK;
    CHECK(ok == DBG_MAX_BREAKPOINTS, "only %d of %d breakpoints armed", ok, DBG_MAX_BREAKPOINTS);
    CHECK(debug_agent_set_breakpoint(DBG_MAX_BREAKPOINTS, nullptr, 0, 0) == DBG_E_LIMIT,
          "breakpoint %d past the limit was not refused LIMIT_EXCEEDED", DBG_MAX_BREAKPOINTS + 1);
    CHECK((g_flags[DBG_MAX_BREAKPOINTS] & DBG_FLAG_ARMED) == 0, "the refused probe's flag is armed");
    CHECK(debug_agent_breakpoint_count() == DBG_MAX_BREAKPOINTS, "count %u after the refusal",
          debug_agent_breakpoint_count());
    int armed = 0;
    for (uint32_t p = 0; p < DBG_MAX_BREAKPOINTS; ++p) armed += (g_flags[p] & DBG_FLAG_ARMED) != 0;
    CHECK(armed == DBG_MAX_BREAKPOINTS, "the refusal disarmed %d breakpoints",
          DBG_MAX_BREAKPOINTS - armed);
    /* A stop still occurs at a previously set one, and at a sample of 10 across the range. */
    std::thread t([] {
        debug_agent_task_enter(0);
        for (uint32_t k = 0; k < 10; ++k) probe(k * (DBG_MAX_BREAKPOINTS / 10));
        probe(DBG_MAX_BREAKPOINTS);
        debug_agent_task_exit();
    });
    t.join();
    CHECK(g_parks == 10, "%ld stops for 10 armed probes and one refused", g_parks.load());
    CHECK(debug_agent_clear_all_breakpoints() == DBG_MAX_BREAKPOINTS, "clear-all count wrong");
    CHECK(debug_agent_set_breakpoint(kProbes, nullptr, 0, 0) == DBG_E_INVALID_LOCATION,
          "a probe past the program's count was accepted");
}

/* D4.13: a breakpoint set while the task runs is armed for the very next probe it reaches. */
static void case_visibility(void)
{
    g_case = "A031 visibility";
    bind_fresh();
    g_parks = 0;
    std::atomic<uint64_t> iter{0};
    std::atomic<uint64_t> stop_iter{0};
    std::atomic<bool>     done{false};
    std::thread t([&] {
        debug_agent_task_enter(0);
        while (!done)
        {
            const long before = g_parks.load();
            probe(77);
            if (g_parks.load() != before)
            {
                stop_iter = iter.load();
                break;
            }
            iter.fetch_add(1);
        }
        debug_agent_task_exit();
    });
    while (iter.load() < 1000) std::this_thread::yield();
    debug_agent_set_breakpoint(77, nullptr, 0, 0);
    const uint64_t at_response = iter.load();
    for (int i = 0; i < 2000 && g_parks == 0; ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    done = true;
    t.join();
    CHECK(g_parks == 1, "no stop after the set response (%ld)", g_parks.load());
    CHECK(stop_iter <= at_response + 1, "stopped %llu probes after the response",
          (unsigned long long)(stop_iter - at_response));
}

/* D1.3(2): at lock depth > 0 a breakpoint never parks; it is counted as skipped. */
static void case_lock_depth(void)
{
    g_case = "D1.3 lock depth";
    bind_fresh();
    g_parks = 0;
    dbg_counters_t c0, c1;
    debug_agent_counters(&c0);
    debug_agent_set_breakpoint(5, nullptr, 0, 0);
    std::thread t([] {
        debug_agent_task_enter(1);
        tl_lock_depth = 1;
        probe(5);
        tl_lock_depth = 0;
        probe(5);
        debug_agent_task_exit();
    });
    t.join();
    debug_agent_counters(&c1);
    CHECK(g_parks == 1, "%ld stops: want only the depth-0 hit", g_parks.load());
    CHECK(c1.skipped_locked == c0.skipped_locked + 1, "the locked hit was not counted as skipped");
}

/* A205: 10^5 condition replacements, disarm/re-arm and clear-all of the full table while two
 * tasks hammer the probe. No stop, no condition error, and the pool cycles many times. */
static void case_race(void)
{
    g_case = "A205 race";
    bind_fresh();
    g_parks = 0;
    const uint32_t P = 3000;
    const auto     a = cond_short(), b = cond_long();
    dbg_counters_t c0, c1;
    debug_agent_counters(&c0);
    CHECK(debug_agent_set_breakpoint(P, a.data(), a.size(), 0) == DBG_OK, "arm with a condition");

    std::atomic<bool>     done{false};
    std::atomic<uint64_t> hits{0};
    auto scan = [&](size_t task) {
        debug_agent_task_enter(task);
        while (!done)
        {
            probe(P);
            probe((uint32_t)(hits.fetch_add(1, std::memory_order_relaxed) % DBG_MAX_BREAKPOINTS));
        }
        debug_agent_task_exit();
    };
    std::thread t0(scan, 0), t1(scan, 1);

    const int kReplacements = 100000;
    int       limit_refused = 0, clear_all_ok = 0;
    for (int i = 0; i < kReplacements; ++i)
    {
        const auto &c = (i & 1) ? b : a;
        if (debug_agent_set_breakpoint(P, c.data(), c.size(), 0) != DBG_OK)
        {
            CHECK(false, "replacement %d refused", i);
            break;
        }
        if (i % 1000 == 500)
        {
            debug_agent_clear_breakpoint(P);
            debug_agent_set_breakpoint(P, c.data(), c.size(), 0);
        }
        if (i % 20000 == 10000)
        {
            /* Fill the table with always-false breakpoints, arm one past it, clear all. */
            for (uint32_t p = 0; debug_agent_breakpoint_count() < DBG_MAX_BREAKPOINTS; ++p)
                if (p != P) debug_agent_set_breakpoint(p, a.data(), a.size(), 0);
            limit_refused += debug_agent_set_breakpoint(2000, a.data(), a.size(), 0) == DBG_E_LIMIT;
            clear_all_ok += debug_agent_clear_all_breakpoints() == DBG_MAX_BREAKPOINTS;
            debug_agent_set_breakpoint(P, c.data(), c.size(), 0);
        }
        if ((i & 63) == 0) debug_agent_reclaim(); /* the agent's 10 ms tick */
    }
    done = true;
    t0.join();
    t1.join();
    debug_agent_counters(&c1);
    CHECK(g_parks == 0, "%ld stops on always-false conditions", g_parks.load());
    CHECK(c1.condition_errors == c0.condition_errors, "%llu condition errors",
          (unsigned long long)(c1.condition_errors - c0.condition_errors));
    CHECK(limit_refused == 5, "the arm past the limit was refused %d of 5 times", limit_refused);
    CHECK(clear_all_ok == 5, "clear-all removed the full table %d of 5 times", clear_all_ok);
    CHECK(c1.records_reused - c0.records_reused >= (uint64_t)kReplacements - DBG_POOL_SIZE,
          "only %llu records reused: the pool did not cycle",
          (unsigned long long)(c1.records_reused - c0.records_reused));
    CHECK(hits > 100000, "the scan threads made only %llu probe calls",
          (unsigned long long)hits.load());
}

int main()
{
    static const dbg_host_t host = {fake_park};
    debug_agent_set_host(&host);
    case_limit();
    case_visibility();
    case_lock_depth();
    case_race();
    debug_agent_unbind();
    if (g_failures == 0)
    {
        std::printf("  all cases passed\n");
        return 0;
    }
    std::printf("  %d check(s) failed\n", g_failures);
    return 1;
}
