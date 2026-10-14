// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file test_virtual_clock.cpp
 * @brief Host test: per-task virtual clock and halt-free scan statistics (ADR-004 D3, D4).
 *
 * Runs the real state manager, dispatcher, image tables and scan-cycle trackers against two
 * in-process fake tasks. Each fake program reads time the way STruCpp code does: the worker
 * stamps a thread-local through strucpp_set_current_time() and TIME() and the TON timer read
 * it. Halts are driven through plc_run_control_hold/resume. Criteria: A051 A052 A053.
 *
 * See also:
 *   tests/host/test_safe_stop.cpp - the same stubs, for the output hold and the start gate.
 *   core/src/plc_app/plc_state_manager.cpp - the dispatcher that advances each task's clock.
 *   core/src/plc_app/scan_cycle_manager.c - the per-task statistics a halt must not enter.
 */

// NO-FOLD: runtime-core

#include "../../core/src/lib/strucpp_abi.hpp"
#include "../../core/src/plc_app/plc_state_manager.h"

extern "C" {
#include "../../core/src/drivers/plugin_driver.h"
#include "../../core/src/plc_app/unix_socket.h"
void handle_unix_socket_commands(const char *command, char *response, size_t response_size);
}
#include "../../core/src/plc_app/image_tables.h"

#include <atomic>
#include <csignal>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <functional>
#include <mutex>
#include <string>
#include <vector>

#include <sys/stat.h>
#include <unistd.h>

// ---------------------------------------------------------------------------
// Stubs for what the core links against in plc_main
// ---------------------------------------------------------------------------

static bool            g_verbose = false;
static plugin_driver_t g_fake_driver;

static void vlog(const char *lvl, const char *fmt, va_list ap)
{
    if (!g_verbose) return;
    std::fprintf(stderr, "    [%s] ", lvl);
    std::vfprintf(stderr, fmt, ap);
    std::fprintf(stderr, "\n");
}

extern "C" {
void log_info(const char *f, ...) { va_list a; va_start(a, f); vlog("info", f, a); va_end(a); }
void log_warn(const char *f, ...) { va_list a; va_start(a, f); vlog("warn", f, a); va_end(a); }
void log_error(const char *f, ...) { va_list a; va_start(a, f); vlog("error", f, a); va_end(a); }
void log_debug(const char *, ...) {}

volatile sig_atomic_t keep_running  = 1;
plugin_driver_t      *plugin_driver = &g_fake_driver;

int plugin_driver_update_config(plugin_driver_t *, const char *) { return 0; }
int plugin_driver_append_config(plugin_driver_t *, const char *) { return 0; }
int plugin_driver_init(plugin_driver_t *) { return 0; }
int plugin_driver_cleanup_init(plugin_driver_t *) { return 0; }
int plugin_driver_start(plugin_driver_t *) { return 0; }
int plugin_driver_stop(plugin_driver_t *) { return 0; }
void plugin_driver_cycle_start(plugin_driver_t *) {}
void plugin_driver_cycle_end(plugin_driver_t *) {}
size_t plugin_driver_append_stats_json(plugin_driver_t *, char *, size_t) { return 0; }
size_t process_debug_data(uint8_t *, size_t) { return 0; }
int plugin_driver_execute_command(plugin_driver_t *, const char *, const char *, char *, size_t)
{
    return -1;
}

void watchdog_feed(void) {}
void watchdog_dispatcher_started(int64_t) {}
void watchdog_dispatcher_stopped(void) {}
void watchdog_set_fault_context(const char *) {}
void watchdog_fatal_exit(const char *reason)
{
    std::printf("  FAIL  watchdog_fatal_exit: %s\n", reason);
    std::fflush(stdout);
    _exit(98);
}

void plc_retain_init(void) {}
void plc_retain_read(void) {}
void plc_retain_save(void) {}
void plc_retain_flush(void) {}
void debug_write_journal_drain(void) {}
void debug_write_journal_reset(void) {}
}

// ---------------------------------------------------------------------------
// Fake program: a 10 ms task running a TON, and a 30 ms task
// ---------------------------------------------------------------------------

static const int64_t kMs        = 1000000;
static const int64_t kFastNs    = 10 * kMs;
static const int64_t kSlowNs    = 30 * kMs;
static const int64_t kTonPtNs   = 500 * kMs;

/* What strucpp_set_current_time() writes: TIME() of the scan on this worker. */
static thread_local int64_t tl_time_ns = 0;

static int64_t mono_ns(void)
{
    timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000000000LL + t.tv_nsec;
}

static uint16_t m_q0, m_q1;

/* The last scan of each task: the TIME() it read, and how many scans ran. */
static std::atomic<int64_t> g_fast_time{-1}, g_slow_time{-1};
static std::atomic<long>    g_fast_scans{0}, g_slow_scans{0};

/* Fast-task scan starts on the monotonic clock, for the catch-up check. */
static std::mutex           g_starts_mu;
static std::vector<int64_t> g_fast_starts;

/* TON (IEC 61131-3), as STruCpp's iec-standard-fb/timer.st computes it from TIME(). */
static std::atomic<bool>    g_ton_in{false};
static std::atomic<int64_t> g_ton_et{0};
static std::atomic<bool>    g_ton_q{false};
static std::atomic<int64_t> g_ton_start_vt{-1}, g_ton_q_vt{-1}, g_ton_q_wall{-1};
static int                  ton_state  = 0;
static bool                 ton_prev   = false;
static int64_t              ton_start  = 0;

static void ton_scan(int64_t now)
{
    const bool in = g_ton_in.load();
    if (ton_state == 0 && !ton_prev && in)
    {
        ton_state = 1;
        ton_start = now;
        g_ton_start_vt = now;
        g_ton_q        = false;
    }
    else if (!in)
    {
        ton_state = 0;
        g_ton_et  = 0;
        g_ton_q   = false;
    }
    else if (ton_state == 1)
    {
        if (ton_start + kTonPtNs <= now)
        {
            ton_state   = 2;
            g_ton_et    = kTonPtNs;
            g_ton_q     = true;
            g_ton_q_vt  = now;
            g_ton_q_wall = mono_ns();
        }
        else
        {
            g_ton_et = now - ton_start;
        }
    }
    ton_prev = in;
}

struct FastProgram : strucpp::ProgramBase
{
    void run() override
    {
        {
            std::lock_guard<std::mutex> g(g_starts_mu);
            g_fast_starts.push_back(mono_ns());
        }
        ton_scan(tl_time_ns);
        g_fast_time = tl_time_ns;
        g_fast_scans++;
        m_q0 = (uint16_t)g_fast_scans.load();
    }
    void located_range(uint32_t *o, uint32_t *c) const override
    {
        *o = 0;
        *c = 1;
    }
};

struct SlowProgram : strucpp::ProgramBase
{
    void run() override
    {
        g_slow_time = tl_time_ns;
        g_slow_scans++;
        m_q1 = (uint16_t)g_slow_scans.load();
    }
    void located_range(uint32_t *o, uint32_t *c) const override
    {
        *o = 1;
        *c = 1;
    }
};

static FastProgram           g_fast_prog;
static SlowProgram           g_slow_prog;
static strucpp::ProgramBase *g_fast_progs[] = {&g_fast_prog};
static strucpp::ProgramBase *g_slow_progs[] = {&g_slow_prog};
static strucpp::TaskInstance g_tasks[]      = {{"fast", kFastNs, 1, g_fast_progs, 1},
                                               {"slow", kSlowNs, 2, g_slow_progs, 1}};
static strucpp::ResourceInstance g_res[]    = {{"res", "cpu", g_tasks, 2}};

struct FakeConfig : strucpp::ConfigurationInstance
{
    const char *get_name() const override { return "cfg"; }
    strucpp::ResourceInstance *get_resources() override { return g_res; }
    size_t get_resource_count() const override { return 1; }
};
static FakeConfig g_cfg;

using A = strucpp::LocatedArea;
using S = strucpp::LocatedSize;
static strucpp::LocatedVar g_lv[] = {
    {A::Output, S::Word, 0, 0, {}, &m_q0},
    {A::Output, S::Word, 1, 0, {}, &m_q1},
};

static char g_md5[33] = "0123456789abcdef0123456789abcdef";

extern "C" {
static void fk_advance(uint64_t) {}
static void fk_set_time(int64_t ns) { tl_time_ns = ns; }
static strucpp::ConfigurationInstance *fk_config(void) { return &g_cfg; }
static const strucpp::LocatedVar *fk_lv(void) { return g_lv; }
static uint32_t fk_lv_count(void) { return 2; }
static uint8_t  fk_arr_count(void) { return 0; }
static uint16_t fk_elem_count(uint8_t) { return 0; }
static uint16_t fk_size(uint8_t, uint16_t) { return 0; }
static uint8_t  fk_set(uint8_t, uint16_t, bool, const uint8_t *, uint16_t) { return 0; }
static uint16_t fk_read(uint8_t, uint16_t, uint8_t *) { return 0; }
static uint8_t  fk_write(uint8_t, uint16_t, const uint8_t *, uint16_t) { return 0; }

static char g_pm_storage[16];

char *find_libplc_file(const char *) { return strdup("libplc_fake.so"); }
PluginManager *plugin_manager_create(const char *)
{
    return reinterpret_cast<PluginManager *>(g_pm_storage);
}
bool plugin_manager_load(PluginManager *) { return true; }
void plugin_manager_destroy(PluginManager *) {}
void *plugin_manager_get_symbol(PluginManager *, const char *n)
{
    struct { const char *name; void *fn; } t[] = {
        {"strucpp_advance_time", (void *)fk_advance},
        {"strucpp_set_current_time", (void *)fk_set_time},
        {"strucpp_get_config", (void *)fk_config},
        {"strucpp_get_located_vars", (void *)fk_lv},
        {"strucpp_get_located_var_count", (void *)fk_lv_count},
        {"strucpp_debug_array_count", (void *)fk_arr_count},
        {"strucpp_debug_elem_count", (void *)fk_elem_count},
        {"strucpp_debug_size", (void *)fk_size},
        {"strucpp_debug_set", (void *)fk_set},
        {"strucpp_debug_read", (void *)fk_read},
        {"strucpp_debug_write", (void *)fk_write},
        {"strucpp_program_md5", (void *)g_md5},
    };
    for (auto &e : t)
        if (std::strcmp(e.name, n) == 0) return e.fn;
    return nullptr;
}
void *plugin_manager_try_get_symbol(PluginManager *, const char *) { return nullptr; }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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

static void sleep_ms(int ms)
{
    timespec t = {ms / 1000, (long)(ms % 1000) * 1000000L};
    nanosleep(&t, nullptr);
}

static bool wait_for(const std::function<bool()> &pred, int timeout_ms)
{
    for (int i = 0; i < timeout_ms; i += 2)
    {
        if (pred()) return true;
        sleep_ms(2);
    }
    return pred();
}

static std::string cmd(const char *c)
{
    char buf[512] = {0};
    handle_unix_socket_commands(c, buf, sizeof buf);
    std::string s(buf);
    while (!s.empty() && s.back() == '\n') s.pop_back();
    return s;
}

static bool start_plc(void)
{
    return cmd("START") == "START:OK" &&
           wait_for([] { return plc_get_state() == PLC_STATE_RUNNING; }, 3000) &&
           wait_for([] { return g_slow_scans.load() > 3; }, 2000);
}

/* Wait for the next completed scan of a task; returns the TIME() it read. */
static int64_t next_scan_time(std::atomic<long> &scans, std::atomic<int64_t> &time)
{
    const long n = scans.load();
    wait_for([&] { return scans.load() > n; }, 2000);
    return time.load();
}

static plc_timing_stats_t fast_stats(void)
{
    plc_timing_stats_t s{};
    plc_tasks_reader_lock();
    if (plc_tasks && plc_task_count > 0) scan_cycle_tracker_snapshot(&plc_tasks[0].tracker, &s);
    plc_tasks_reader_unlock();
    return s;
}

static long fast_dispatch_overruns(void)
{
    long n = -1;
    plc_tasks_reader_lock();
    if (plc_tasks && plc_task_count > 0) n = plc_tasks[0].overrun_count.load();
    plc_tasks_reader_unlock();
    return n;
}

static double ms(int64_t ns) { return (double)ns / 1e6; }

// ---------------------------------------------------------------------------
// Cases
// ---------------------------------------------------------------------------

/* With no halt the clock is the wall schedule: one interval per scan, readers agree. */
static void case_no_halt(void)
{
    g_case       = "D4.1 no halt";
    int64_t prev = next_scan_time(g_fast_scans, g_fast_time);
    int     bad  = 0;
    for (int i = 0; i < 20; ++i)
    {
        int64_t t = next_scan_time(g_fast_scans, g_fast_time);
        if ((t - prev) % kFastNs != 0 || t <= prev) bad++;
        prev = t;
    }
    CHECK(bad == 0, "%d of 20 scans read a TIME() that is not a whole number of intervals later",
          bad);
    const int64_t prog   = g_fast_time.load();
    const int64_t plugin = plc_task_virtual_time_ns(0);
    CHECK(plugin >= prog && plugin - prog <= kFastNs,
          "plugin reader %.1f ms vs program reader %.1f ms: more than one interval apart",
          ms(plugin), ms(prog));
    CHECK(plc_virtual_time_ns() == plc_task_virtual_time_ns(0),
          "the plugins' default clock is not the fastest task's");
}

/* A052: an all-stop halt advances every task's clock by at most one interval. */
static void case_all_stop(int halt_ms)
{
    g_case = halt_ms < 2000 ? "A052 all-stop 1 s" : "A052 all-stop 3 s";
    CHECK(plc_run_control_hold(PLC_ALL_TASKS, 1000), "hold was not applied");
    sleep_ms(50);   /* a scan in flight at the hold completes */
    const int64_t fast_h = g_fast_time.load(), slow_h = g_slow_time.load();
    const int64_t plug_h = plc_task_virtual_time_ns(0), plug_slow_h = plc_task_virtual_time_ns(1);
    sleep_ms(halt_ms);
    CHECK(plc_task_virtual_time_ns(0) == plug_h && plc_task_virtual_time_ns(1) == plug_slow_h,
          "a clock moved while every task was held (fast %.1f -> %.1f ms)", ms(plug_h),
          ms(plc_task_virtual_time_ns(0)));
    CHECK(plc_run_control_resume(PLC_ALL_TASKS, 1000), "resume was not applied");
    const int64_t fast_r = next_scan_time(g_fast_scans, g_fast_time);
    const int64_t slow_r = next_scan_time(g_slow_scans, g_slow_time);
    CHECK(fast_r - fast_h <= kFastNs,
          "fast task's TIME() advanced %.1f ms across a %d ms halt (limit one interval, %.0f ms)",
          ms(fast_r - fast_h), halt_ms, ms(kFastNs));
    /* The slow reader waited for its own next scan, which the running fast clock does not
     * disturb: bound it by two of its intervals from the halted value. */
    CHECK(slow_r - slow_h <= 2 * kSlowNs,
          "slow task's TIME() advanced %.1f ms across a %d ms halt (limit %.0f ms)",
          ms(slow_r - slow_h), halt_ms, ms(2 * kSlowNs));
    const int64_t plug = plc_task_virtual_time_ns(0), prog = g_fast_time.load();
    CHECK(plug - prog <= kFastNs && prog - plug <= kFastNs,
          "after resume the plugin reader (%.1f ms) and the program (%.1f ms) disagree",
          ms(plug), ms(prog));
}

/* R4xR8: halting one task stops only that task's clock. */
static void case_halt_one(void)
{
    g_case = "R4xR8 halt-one";
    CHECK(plc_run_control_hold(0, 1000), "hold of the fast task was not applied");
    sleep_ms(50);
    const int64_t fast_h = g_fast_time.load();
    const int64_t slow_h = plc_task_virtual_time_ns(1);
    const int64_t wall_h = mono_ns();
    sleep_ms(1000);
    const int64_t slow_e = plc_task_virtual_time_ns(1);
    const int64_t wall_e = mono_ns();
    const long    slow_n = g_slow_scans.load();
    CHECK(plc_run_control_resume(0, 1000), "resume was not applied");
    const int64_t fast_r = next_scan_time(g_fast_scans, g_fast_time);
    CHECK(fast_r - fast_h <= kFastNs, "the halted task's clock advanced %.1f ms in 1 s",
          ms(fast_r - fast_h));
    const int64_t drift = (slow_e - slow_h) - (wall_e - wall_h);
    CHECK(drift <= 2 * kSlowNs && -drift <= 2 * kSlowNs,
          "the running task's clock advanced %.1f ms in %.1f ms of wall time", ms(slow_e - slow_h),
          ms(wall_e - wall_h));
    CHECK(slow_n > 25, "the running task scanned only %ld times", slow_n);
}

/* A051: halt time never enters scan statistics, and resume runs no catch-up burst. */
static void case_statistics(int halt_ms)
{
    g_case = "A051 statistics";
    sleep_ms(200);
    const plc_timing_stats_t before = fast_stats();
    const long overruns_before      = fast_dispatch_overruns();
    CHECK(plc_run_control_hold(PLC_ALL_TASKS, 1000), "hold was not applied");
    sleep_ms(halt_ms);
    const int64_t t_resume = mono_ns();
    CHECK(plc_run_control_resume(PLC_ALL_TASKS, 1000), "resume was not applied");
    sleep_ms(500);
    const plc_timing_stats_t after = fast_stats();

    size_t starts = 0;
    int64_t first = 0;
    {
        std::lock_guard<std::mutex> g(g_starts_mu);
        for (int64_t t : g_fast_starts)
            if (t >= t_resume && first == 0) first = t;
        for (int64_t t : g_fast_starts)
            if (first && t >= first && t < first + 2 * kFastNs) starts++;
    }
    CHECK(first != 0 && starts <= 2, "%zu scan starts in the first 2 intervals after resume",
          starts);
    CHECK(after.overruns == before.overruns,
          "STATS overruns went %lld -> %lld across a halt", (long long)before.overruns,
          (long long)after.overruns);
    CHECK(fast_dispatch_overruns() == overruns_before, "dispatcher overruns went %ld -> %ld",
          overruns_before, fast_dispatch_overruns());
    CHECK(after.scan_time_max < kFastNs / 1000, "max scan time %lld us is not below the interval",
          (long long)after.scan_time_max);
    CHECK(after.cycle_time_max < 5 * kFastNs / 1000,
          "max cycle time %lld us: the %d ms halt entered the statistics",
          (long long)after.cycle_time_max, halt_ms);
    CHECK(after.cycle_latency_max < 5 * kFastNs / 1000,
          "max latency %lld us: the %d ms halt entered the statistics",
          (long long)after.cycle_latency_max, halt_ms);
}

/* A053: a halted TON resumes where it stopped and reaches Q at PT of virtual time. */
static void case_ton(int halt_ms)
{
    g_case = "A053 TON";
    const int64_t wall_in = mono_ns();
    g_ton_in              = true;
    CHECK(wait_for([] { return g_ton_et.load() >= 200 * kMs; }, 2000), "TON never timed");
    CHECK(plc_run_control_hold(PLC_ALL_TASKS, 1000), "hold was not applied");
    sleep_ms(50);
    const int64_t et_h = g_ton_et.load();
    sleep_ms(halt_ms);
    CHECK(plc_run_control_resume(PLC_ALL_TASKS, 1000), "resume was not applied");
    next_scan_time(g_fast_scans, g_fast_time);
    const int64_t et_r = g_ton_et.load();
    CHECK(et_r - et_h <= kFastNs, "ET went %.1f -> %.1f ms across the halt", ms(et_h), ms(et_r));
    CHECK(wait_for([] { return g_ton_q.load(); }, 3000), "Q never rose");
    const int64_t q_vt = g_ton_q_vt.load() - g_ton_start_vt.load();
    CHECK(q_vt >= kTonPtNs && q_vt <= kTonPtNs + kFastNs,
          "Q rose %.1f ms of virtual time after IN (PT %.0f ms)", ms(q_vt), ms(kTonPtNs));
    const int64_t q_wall = g_ton_q_wall.load() - wall_in;
    const int64_t want   = kTonPtNs + (int64_t)halt_ms * kMs;
    CHECK(q_wall >= want - 50 * kMs && q_wall <= want + 150 * kMs,
          "Q rose %.1f ms of wall time after IN (want about %.0f ms)", ms(q_wall), ms(want));
    g_ton_in = false;
}

static void on_sigusr1(int) {}

int main(int argc, char **argv)
{
    struct sigaction wake{};
    wake.sa_handler = on_sigusr1;
    sigemptyset(&wake.sa_mask);
    sigaction(SIGUSR1, &wake, nullptr);
    for (int i = 1; i < argc; ++i)
        if (std::strcmp(argv[i], "-v") == 0) g_verbose = true;

    std::printf("virtual clock: per-task time, halt-free statistics (ADR-004 D3 D4)\n");
    char tmpl[] = "/tmp/virtual_clock_XXXXXX";
    if (!mkdtemp(tmpl) || chdir(tmpl) != 0)
    {
        std::printf("  FAIL  no temp dir\n");
        return 1;
    }

    g_case = "start";
    CHECK(start_plc(), "the PLC did not start");
    if (g_failures == 0)
    {
        case_no_halt();
        case_all_stop(1000);
        case_all_stop(3000);
        case_halt_one();
        case_statistics(2000);
        case_ton(2000);
    }
    cmd("STOP");
    wait_for([] { return plc_get_state() == PLC_STATE_STOPPED; }, 5000);

    if (g_failures == 0) std::printf("all cases passed\n");
    else std::printf("%d check(s) failed\n", g_failures);
    std::fflush(stdout);
    _exit(g_failures == 0 ? 0 : 1);
}
