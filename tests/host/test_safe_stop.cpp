// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file test_safe_stop.cpp
 * @brief Host test: output hold, start gate and halt behaviour (ADR-004 D1, D2, D7.3).
 *
 * Runs the real state manager, dispatcher, image tables, journal and socket command
 * handler against an in-process fake program and a fake plant: a sampler thread that
 * reads the output image the way a threaded plugin does, while plugins run. Process
 * restarts are re-executions of this binary with --boot. Criteria: A049 A050 A059
 * A206 A207 A208.
 */

// NO-FOLD: runtime-core

#include "../../core/src/lib/strucpp_abi.hpp"
#include "../../core/src/plc_app/plc_state_manager.h"

extern "C" {
#include "../../core/src/drivers/plugin_driver.h"
#include "../../core/src/plc_app/plc_switch.h"
#include "../../core/src/plc_app/unix_socket.h"
void handle_unix_socket_commands(const char *command, char *response, size_t response_size);
}
#include "../../core/src/plc_app/image_tables.h"
#include "../../core/src/plc_app/journal_buffer.h"
#include "../../core/src/plc_app/plc_output_hold.h"
#include "../../core/src/plc_app/plc_state_manager.h"

#include <atomic>
#include <cerrno>
#include <csignal>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <semaphore.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

// ---------------------------------------------------------------------------
// Stubs for what the core links against in plc_main
// ---------------------------------------------------------------------------

static bool g_verbose = false;

static void vlog(const char *lvl, const char *fmt, va_list ap)
{
    if (!g_verbose) return;
    std::fprintf(stderr, "    [%s] ", lvl);
    std::vfprintf(stderr, fmt, ap);
    std::fprintf(stderr, "\n");
}

static std::atomic<int>  g_feeds{0};
static std::atomic<int>  g_cycle_start{0};
static std::atomic<int>  g_cycle_end{0};
static std::atomic<bool> g_plugins_running{false};
static std::atomic<bool> g_crash_in_cycle_start{false};
static std::atomic<int>  g_generation{0};
static plugin_driver_t   g_fake_driver;

static void take_sample(void);

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
int plugin_driver_start(plugin_driver_t *)
{
    g_plugins_running = true;
    take_sample();   /* the first frame a plugin sends */
    return 0;
}
int plugin_driver_stop(plugin_driver_t *)
{
    g_plugins_running = false;
    return 0;
}
void plugin_driver_cycle_start(plugin_driver_t *)
{
    g_cycle_start++;
    if (g_crash_in_cycle_start.exchange(false)) raise(SIGSEGV);
}
void plugin_driver_cycle_end(plugin_driver_t *) { g_cycle_end++; }
size_t plugin_driver_append_stats_json(plugin_driver_t *, char *, size_t) { return 0; }
size_t process_debug_data(uint8_t *, size_t) { return 0; }
int plugin_driver_execute_command(plugin_driver_t *, const char *, const char *, char *, size_t)
{
    return -1;
}

void watchdog_feed(void) { g_feeds++; }
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
// Fake program: one 10 ms task whose outputs follow input %IW0
// ---------------------------------------------------------------------------

static const int64_t kIntervalNs = 10000000;

static uint16_t m_qw0, m_qw1, m_iw0;
static uint8_t  m_qx0, m_qb1;
static uint32_t m_qd0;
static uint64_t m_ql0;

static std::atomic<long> g_scans{0};
static std::atomic<bool> g_block_scan{false};
static sem_t             g_block_sem;

/* Program value of %QW0 for input v in load generation g. Distinct per load, so a
 * restarted program's own output is never the held one. */
static uint16_t program_qw0(uint16_t v, int g) { return (uint16_t)(v + 100 * g); }

struct FakeProgram : strucpp::ProgramBase
{
    void run() override
    {
        g_scans++;
        while (g_block_scan.load())
        {
            if (sem_wait(&g_block_sem) == 0) break;
        }
        m_qw0 = program_qw0(m_iw0, g_generation.load());
        m_qw1 = (uint16_t)(m_qw0 + 1);
        m_qx0 = m_qw0 != 0;
        m_qb1 = (uint8_t)m_qw0;
        m_qd0 = (uint32_t)m_qw0 * 1000u;
        m_ql0 = (uint64_t)m_qw0 * 1000000ull;
    }
    void located_range(uint32_t *o, uint32_t *c) const override
    {
        *o = 0;
        *c = 7;
    }
};

static FakeProgram            g_prog;
static strucpp::ProgramBase  *g_progs[] = {&g_prog};
static strucpp::TaskInstance  g_tasks[] = {{"main", kIntervalNs, 1, g_progs, 1}};
static strucpp::ResourceInstance g_res[] = {{"res", "cpu", g_tasks, 1}};

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
    {A::Output, S::Word, 0, 0, {}, &m_qw0}, {A::Output, S::Word, 1, 0, {}, &m_qw1},
    {A::Output, S::Bit, 0, 0, {}, &m_qx0},  {A::Output, S::Byte, 1, 0, {}, &m_qb1},
    {A::Output, S::DWord, 0, 0, {}, &m_qd0}, {A::Output, S::LWord, 0, 0, {}, &m_ql0},
    {A::Input, S::Word, 0, 0, {}, &m_iw0},
};

static char g_md5[33] = "0123456789abcdef0123456789abcdef";

extern "C" {
static void fk_advance(uint64_t) {}
static void fk_set_time(int64_t) {}
static strucpp::ConfigurationInstance *fk_config(void) { return &g_cfg; }
static const strucpp::LocatedVar *fk_lv(void) { return g_lv; }
static uint32_t fk_lv_count(void) { return 7; }
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
    /* A load re-creates program storage at its initial values. */
    m_qw0 = m_qw1 = m_iw0 = 0;
    m_qx0 = m_qb1 = 0;
    m_qd0 = 0;
    m_ql0 = 0;
    g_generation++;
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
// Fake plant: samples of the output image taken while plugins run
// ---------------------------------------------------------------------------

struct Out
{
    uint16_t qw0, qw1;
    uint8_t  qx0, qb1;
    uint32_t qd0;
    uint64_t ql0;
    bool operator==(const Out &o) const
    {
        return qw0 == o.qw0 && qw1 == o.qw1 && qx0 == o.qx0 && qb1 == o.qb1 && qd0 == o.qd0 &&
               ql0 == o.ql0;
    }
    bool any_zero() const
    {
        return qw0 == 0 || qw1 == 0 || qx0 == 0 || qb1 == 0 || qd0 == 0 || ql0 == 0;
    }
    bool all_zero() const
    {
        return qw0 == 0 && qw1 == 0 && qx0 == 0 && qb1 == 0 && qd0 == 0 && ql0 == 0;
    }
};

static Out expected_for(uint16_t qw0)
{
    return Out{qw0, (uint16_t)(qw0 + 1), (uint8_t)(qw0 != 0), (uint8_t)qw0, (uint32_t)qw0 * 1000u,
               (uint64_t)qw0 * 1000000ull};
}

static std::mutex       g_log_mu;
static std::vector<Out> g_log;
static std::atomic<bool> g_sampler_run{true};

static bool read_outputs(Out *o)
{
    image_lock();
    bool bound = int_output[0] && int_output[1] && bool_output[0][0] && byte_output[1] &&
                 dint_output[0] && lint_output[0];
    if (bound)
        *o = Out{*int_output[0], *int_output[1], *bool_output[0][0], *byte_output[1],
                 *dint_output[0], *lint_output[0]};
    image_unlock();
    return bound;
}

static void take_sample(void)
{
    Out o{};
    if (!read_outputs(&o)) return;
    std::lock_guard<std::mutex> g(g_log_mu);
    g_log.push_back(o);
}

static void sampler(void)
{
    const timespec p = {0, 200000L};
    while (g_sampler_run)
    {
        if (g_plugins_running) take_sample();
        nanosleep(&p, nullptr);
    }
}

static size_t log_mark(void)
{
    std::lock_guard<std::mutex> g(g_log_mu);
    return g_log.size();
}

static std::vector<Out> log_since(size_t mark)
{
    std::lock_guard<std::mutex> g(g_log_mu);
    return std::vector<Out>(g_log.begin() + (long)mark, g_log.end());
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

/* One line per failing loop: the first offending sample, and how many there were. */
#define CHECK_EACH(samples, o, cond, ...)                                                          \
    do                                                                                             \
    {                                                                                              \
        size_t bad_ = 0;                                                                           \
        Out    first_{};                                                                           \
        for (const Out &o : (samples))                                                             \
            if (!(cond) && bad_++ == 0) first_ = o;                                                \
        if (bad_)                                                                                  \
        {                                                                                          \
            const Out &o = first_;                                                                 \
            (void)o;                                                                               \
            std::printf("  FAIL  [%s] %zu of %zu samples: ", g_case, bad_, (samples).size());    \
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
    for (int i = 0; i < timeout_ms; i += 5)
    {
        if (pred()) return true;
        sleep_ms(5);
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

static bool wait_state(PLCState s, int timeout_ms = 3000)
{
    return wait_for([&] { return plc_get_state() == s; }, timeout_ms);
}

static void set_input(uint16_t v)
{
    journal_write_int(JOURNAL_INT_INPUT, 0, v);
}

static bool outputs_reach(const Out &want, int timeout_ms = 2000)
{
    return wait_for([&] {
        Out o{};
        return read_outputs(&o) && o == want;
    }, timeout_ms);
}

static Out outputs_now(void)
{
    Out o{};
    read_outputs(&o);
    return o;
}

static bool file_exists(const char *p)
{
    struct stat st;
    return stat(p, &st) == 0;
}

static bool copy_file(const char *from, const char *to)
{
    FILE *a = std::fopen(from, "rb");
    if (!a) return false;
    FILE *b = std::fopen(to, "wb");
    if (!b)
    {
        std::fclose(a);
        return false;
    }
    char   buf[4096];
    size_t n;
    while ((n = std::fread(buf, 1, sizeof buf, a)) > 0) std::fwrite(buf, 1, n, b);
    std::fclose(a);
    std::fclose(b);
    return true;
}

static void write_policy(const char *json)
{
    mkdir("core", 0700);
    mkdir("core/generated", 0700);
    mkdir("core/generated/conf", 0700);
    FILE *f = std::fopen(PLC_HALT_POLICY_PATH, "w");
    if (f)
    {
        std::fputs(json, f);
        std::fclose(f);
    }
}

/* Run the program with input v and wait until the plant sees its outputs. */
static Out run_with_input(uint16_t v)
{
    set_input(v);
    Out want = expected_for(program_qw0(v, g_generation.load()));
    CHECK(outputs_reach(want), "outputs never reached the program's values for input %u", v);
    return want;
}

static void stop_and_land(void)
{
    if (plc_get_state() == PLC_STATE_RUNNING || plc_get_state() == PLC_STATE_SAFE_STOPPED)
        cmd("STOP");
    CHECK(wait_state(PLC_STATE_STOPPED, 5000), "STOP did not land STOPPED (state %d)",
          (int)plc_get_state());
}

static bool start_plain(void)
{
    std::string r = cmd("START");
    return r == "START:OK" && wait_state(PLC_STATE_RUNNING);
}

static bool start_granted(void)
{
    std::string r = cmd("START_AUTHORISED:alice");
    return r == "START:OK" && wait_state(PLC_STATE_RUNNING);
}

/* The safe-stop, as ADR-005's trigger will request it. */
static bool safe_stop(void)
{
    return plc_request_safe_stop("test") && wait_state(PLC_STATE_SAFE_STOPPED);
}

// ---------------------------------------------------------------------------
// Cases
// ---------------------------------------------------------------------------

/* A049 default policy: a halt holds every output and the program's frozen. */
static void case_halt_holds_outputs(void)
{
    g_case = "A049 hold";
    write_policy("{\"policy\":\"hold\"}");
    CHECK(start_plain(), "plain START from STOPPED did not run");
    Out held = run_with_input(1234);

    CHECK(plc_run_control_hold(PLC_ALL_TASKS, 1000), "hold was not applied");
    sleep_ms(50);
    const long   scans  = g_scans.load();
    const int    cycles = g_cycle_end.load();
    const size_t mark   = log_mark();
    set_input(999);
    sleep_ms(300);
    CHECK_EACH(log_since(mark), o, o == held, "plant saw qw0=%u qx0=%u while halted (want %u)",
               o.qw0, o.qx0, held.qw0);
    CHECK(g_scans.load() == scans, "program scanned while halted (%ld -> %ld)", scans,
          g_scans.load());
    CHECK(g_cycle_end.load() > cycles + 10, "plugins stopped exchanging while halted");
    CHECK(plc_get_state() == PLC_STATE_RUNNING, "a halt is not a stop (state %d)",
          (int)plc_get_state());

    CHECK(plc_run_control_resume(PLC_ALL_TASKS, 1000), "resume was not applied");
    CHECK(outputs_reach(expected_for(program_qw0(999, g_generation.load()))),
          "the input that arrived during the halt never reached the outputs after resume");
}

/* A049 safe policy: only the configured outputs change, and come back on resume. */
static void case_halt_safe_policy(void)
{
    g_case = "A049 safe";
    stop_and_land();
    write_policy("{\"policy\":\"safe\",\"safe\":[{\"location\":\"%QW1\",\"value\":7},"
                 "{\"location\":\"%QX0.0\",\"value\":0}]}");
    CHECK(start_plain(), "plain START did not run");
    const halt_policy_t *p = halt_policy_get();
    CHECK(p->kind == HALT_POLICY_SAFE && p->count == 2,
          "policy read-back: kind %d count %zu, want safe with 2 outputs", (int)p->kind, p->count);
    Out before = run_with_input(1234);

    CHECK(plc_run_control_hold(PLC_ALL_TASKS, 1000), "hold was not applied");
    Out safe = before;
    safe.qw1 = 7;
    safe.qx0 = 0;
    CHECK(outputs_reach(safe, 200), "configured outputs did not take their safe values");
    const size_t mark = log_mark();
    sleep_ms(200);
    CHECK_EACH(log_since(mark), o, o == safe,
               "while halted: qw0=%u qw1=%u qx0=%u qb1=%u, want %u/%u/%u/%u", o.qw0, o.qw1, o.qx0,
               o.qb1, safe.qw0, safe.qw1, safe.qx0, safe.qb1);

    CHECK(plc_run_control_resume(PLC_ALL_TASKS, 1000), "resume was not applied");
    CHECK(outputs_reach(before), "outputs did not return to the program's values after resume");
    stop_and_land();
    write_policy("{\"policy\":\"hold\"}");
}

/* A050: a long halt never trips the stuck-task check; an overrun after resume does. */
static void case_watchdog_stand_down(void)
{
    g_case = "A050";
    CHECK(start_plain() || start_granted(), "could not start");
    run_with_input(5);

    g_block_scan = true;   /* the next scan stays in its body: a halt mid-scan */
    CHECK(wait_for([] { return g_scans.load() > 0 && g_block_scan.load(); }, 100), "no scan");
    sleep_ms(30);
    CHECK(plc_run_control_hold(PLC_ALL_TASKS, 1000), "hold was not applied");
    sleep_ms(400);   /* 40 periods; the stuck limit is 10 */
    CHECK(plc_get_state() == PLC_STATE_RUNNING,
          "the stuck check tripped during the halt (state %d)", (int)plc_get_state());

    CHECK(plc_run_control_resume(PLC_ALL_TASKS, 1000), "resume was not applied");
    /* Still in the same scan after resume: an overrun the re-armed check must catch. */
    CHECK(wait_for([] { return plc_get_state() != PLC_STATE_RUNNING; }, 1500),
          "the overrun after resume was never caught");
    g_block_scan = false;
    sem_post(&g_block_sem);
    wait_for([] {
        PLCState s = plc_get_state();
        return s == PLC_STATE_ERROR || s == PLC_STATE_STOPPED;
    }, 5000);
}

/* A206: in SAFE_STOPPED the plant keeps receiving the held outputs. */
static Out case_safe_stop_holds(void)
{
    g_case = "A206 hold";
    CHECK(start_plain() || start_granted(), "could not start");
    Out held = run_with_input(1234);

    const size_t mark   = log_mark();
    CHECK(safe_stop(), "state never became SAFE_STOPPED");
    const long scans    = g_scans.load();
    const int  cycles_s = g_cycle_start.load();
    const int  cycles_e = g_cycle_end.load();
    const int  feeds    = g_feeds.load();
    sleep_ms(400);

    std::vector<Out> s = log_since(mark);
    CHECK(!s.empty(), "the plant received nothing");
    CHECK_EACH(s, o, o == held, "plant saw qw0=%u qx0=%u qb1=%u in SAFE_STOPPED (held %u)", o.qw0,
               o.qx0, o.qb1, held.qw0);
    CHECK(g_scans.load() == scans, "the program scanned in SAFE_STOPPED");
    CHECK(g_cycle_start.load() - cycles_s >= 20, "plugin cycle hooks stopped in SAFE_STOPPED");
    CHECK(std::abs((g_cycle_start.load() - cycles_s) - (g_cycle_end.load() - cycles_e)) <= 1,
          "cycle_start/cycle_end not paired");
    CHECK(g_feeds.load() - feeds >= 20, "the dispatcher stopped feeding the watchdog");
    CHECK(cmd("STATUS") == "STATUS:SAFE_STOPPED", "STATUS answered %s", cmd("STATUS").c_str());
    CHECK(plc_output_hold_is_set() && plc_start_gate_is_set(), "hold %d gate %d after safe-stop",
          plc_output_hold_is_set(), plc_start_gate_is_set());

    output_hold_file_flush();
    CHECK(copy_file(output_hold_file_path(), "boot_values.hold"), "no safe_stop.hold written");
    return held;
}

/* A059 / row l: every ungranted start is refused while the gate is set. */
static void case_ungranted_starts_refused(PLCState in_state)
{
    const long scans = g_scans.load();
    CHECK(cmd("START") == "START:ERROR_SAFE_STOPPED", "socket START answered %s",
          cmd("START").c_str());
    /* The plugin / VPP switch start and the boot auto-start: plc_begin_transition, no grant. */
    CHECK(!plc_begin_transition(PLC_STATE_RUNNING), "an ungranted start was claimed");
    sleep_ms(100);
    CHECK(plc_get_state() == in_state, "state moved to %d after refused starts",
          (int)plc_get_state());
    CHECK(g_scans.load() == scans, "the program scanned after a refused start");
}

/* A206 / A208: a granted start shows the held values until scan 1, never 0. */
static void case_granted_start(const Out &held, const char *label)
{
    g_case = label;
    const size_t mark = log_mark();
    CHECK(start_granted(), "START_AUTHORISED did not start the program");
    const Out own = expected_for(program_qw0(0, g_generation.load()));
    CHECK(outputs_reach(own), "the program's own outputs never reached the plant");
    std::vector<Out> s = log_since(mark);
    CHECK_EACH(s, o, !o.any_zero(), "plant saw a 0 output (qw0=%u qx0=%u) across the granted start",
               o.qw0, o.qx0);
    CHECK_EACH(s, o, o == held || o == own,
               "plant saw qw0=%u, neither held %u nor the program's %u", o.qw0, held.qw0, own.qw0);
    CHECK(!s.empty() && s.front() == held,
          "the first frame after the start did not carry the held values (qw0=%u, held %u)",
          s.empty() ? 0 : s.front().qw0, held.qw0);
    CHECK(wait_for([] { return !plc_start_gate_is_set() && !plc_output_hold_is_set(); }, 500),
          "hold %d gate %d not cleared after the first copy-out", plc_output_hold_is_set(),
          plc_start_gate_is_set());
    output_hold_file_flush();
    CHECK(!file_exists(output_hold_file_path()), "safe_stop.hold left behind after the run");
}

/* A206 row m / A059: STOP from SAFE_STOPPED zeroes, and the gate survives it. */
static void case_stop_from_safe_stop(void)
{
    g_case = "A206 stop";
    run_with_input(1234);
    CHECK(safe_stop(), "state never became SAFE_STOPPED");
    const size_t mark = log_mark();
    CHECK(cmd("STOP") == "STOP:OK", "STOP from SAFE_STOPPED answered %s", cmd("STOP").c_str());
    CHECK(wait_state(PLC_STATE_STOPPED, 5000), "STOP did not land STOPPED");
    std::vector<Out> s = log_since(mark);
    CHECK(!s.empty() && s.back().all_zero(), "the plant's last frame after STOP is not all 0");
    CHECK(plc_start_gate_is_set(), "the explicit STOP cleared the start gate");
    CHECK(!plc_output_hold_is_set(), "the explicit STOP kept the output hold");

    g_case = "A059 stop-then-start";
    case_ungranted_starts_refused(PLC_STATE_STOPPED);
    /* VPP switch RUN -> STOP -> RUN. */
    plc_set_switch_position(PLC_SWITCH_STOP);
    plc_set_switch_position(PLC_SWITCH_RUN);
    CHECK(!plc_begin_transition(PLC_STATE_RUNNING), "the switch RUN start was claimed");
    sleep_ms(50);
    CHECK(plc_get_state() == PLC_STATE_STOPPED, "switch RUN left STOPPED");

    output_hold_file_flush();
    CHECK(copy_file(output_hold_file_path(), "boot_gate.hold"), "no gate-only safe_stop.hold");
}

/* A207 / A208: a crash in SAFE_STOPPED lands ERROR with the hold; STOP and a grant both work. */
static void case_error_with_hold(void)
{
    g_case = "A207 crash";
    CHECK(start_granted(), "granted start from STOPPED with the gate set was refused");
    Out held = run_with_input(1234);
    CHECK(safe_stop(), "state never became SAFE_STOPPED");
    const size_t mark = log_mark();
    g_crash_in_cycle_start = true;
    CHECK(wait_state(PLC_STATE_ERROR), "the hook crash did not land ERROR");
    sleep_ms(100);
    CHECK_EACH(log_since(mark), o, o == held, "plant saw qw0=%u after the crash, held %u", o.qw0,
               held.qw0);
    CHECK(cmd("STATUS") == "STATUS:ERROR", "STATUS answered %s", cmd("STATUS").c_str());

    g_case = "A208 error";
    case_ungranted_starts_refused(PLC_STATE_ERROR);
    case_granted_start(held, "A208 error");

    g_case = "A207 stop";
    held = run_with_input(1234);
    CHECK(safe_stop(), "state never became SAFE_STOPPED");
    g_crash_in_cycle_start = true;
    CHECK(wait_state(PLC_STATE_ERROR), "the hook crash did not land ERROR");
    const size_t mark2 = log_mark();
    CHECK(cmd("STOP") == "STOP:OK", "STOP from ERROR with the hold answered %s",
          cmd("STOP").c_str());
    CHECK(wait_state(PLC_STATE_STOPPED, 5000), "STOP from ERROR did not land STOPPED (state %d)",
          (int)plc_get_state());
    std::vector<Out> s = log_since(mark2);
    CHECK(!s.empty() && s.back().all_zero(), "outputs not 0 after the STOP from ERROR");
    CHECK(plc_start_gate_is_set(), "the STOP from ERROR cleared the gate");
    plc_set_switch_position(PLC_SWITCH_STOP);
    plc_set_switch_position(PLC_SWITCH_RUN);
    CHECK(!plc_begin_transition(PLC_STATE_RUNNING), "the switch RUN after STOP was claimed");
}

/* Row n: a restart re-asserts the held values and never auto-starts. Runs in a fresh process. */
static int child_boot(const char *file, const char *mode, uint16_t held_qw0)
{
    output_hold_set_file_path(file);
    std::thread smp(sampler);
    const Out held = expected_for(held_qw0);

    if (std::strcmp(mode, "gate") == 0)
    {
        g_case = "A059 boot gate-only";
        CHECK(!plc_boot_from_hold_file(), "a gate-only file entered SAFE_STOPPED");
        CHECK(plc_start_gate_is_set(), "a gate-only file did not set the gate");
        CHECK(!plc_begin_transition(PLC_STATE_RUNNING), "the boot auto-start was claimed");
        CHECK(cmd("START") == "START:ERROR_SAFE_STOPPED", "socket START answered %s",
              cmd("START").c_str());
    }
    else
    {
        g_case = "A206 boot values";
        CHECK(plc_boot_from_hold_file(), "a values file did not enter SAFE_STOPPED");
        CHECK(plc_get_state() == PLC_STATE_SAFE_STOPPED, "state %d after boot", (int)plc_get_state());
        CHECK(plc_safe_stop_runtime_restarted(), "record does not say the runtime restarted");
        sleep_ms(200);
        CHECK(outputs_now() == held, "the first frames after the restart are not the held values");
        CHECK(g_cycle_end.load() >= 10, "no programless dispatcher exchanging the held image");
        CHECK(!plc_begin_transition(PLC_STATE_RUNNING), "the boot auto-start was claimed");
        if (std::strcmp(mode, "stop") == 0)
        {
            const size_t mark = log_mark();
            CHECK(cmd("STOP") == "STOP:OK", "STOP in programless SAFE_STOPPED answered %s",
                  cmd("STOP").c_str());
            CHECK(wait_state(PLC_STATE_STOPPED, 5000), "STOP did not land STOPPED");
            std::vector<Out> s = log_since(mark);
            CHECK(!s.empty() && s.back().all_zero(), "outputs not 0 after STOP");
            CHECK(plc_start_gate_is_set(), "STOP cleared the gate");
        }
        else
        {
            case_granted_start(held, "A206 boot granted start");
        }
    }
    if (plc_get_state() == PLC_STATE_RUNNING) stop_and_land();
    g_sampler_run = false;
    smp.join();
    return g_failures == 0 ? 0 : 1;
}

/* Each child gets its own copy: a granted start deletes the file it booted from. */
static void run_child(const char *src, const char *mode, uint16_t held_qw0)
{
    const char *file = "child.hold";
    unlink(file);
    if (!copy_file(src, file))
    {
        std::printf("  FAIL  [boot %s] no %s to boot from\n", mode, src);
        g_failures++;
        return;
    }
    char qw0[16];
    std::snprintf(qw0, sizeof qw0, "%u", held_qw0);
    std::fflush(stdout);
    pid_t pid = fork();
    if (pid == 0)
    {
        const char *v = g_verbose ? "-v" : "-q";
        execl("/proc/self/exe", "test_safe_stop", "--boot", file, mode, qw0, v, (char *)nullptr);
        _exit(127);
    }
    int st = 0;
    waitpid(pid, &st, 0);
    if (!WIFEXITED(st) || WEXITSTATUS(st) != 0)
    {
        std::printf("  FAIL  [boot %s] child exited %d\n", mode,
                    WIFEXITED(st) ? WEXITSTATUS(st) : -1);
        g_failures++;
    }
}

static void on_sigusr1(int) {}

int main(int argc, char **argv)
{
    sem_init(&g_block_sem, 0, 0);
    /* plc_main installs this wake handler; task threads are woken with SIGUSR1. */
    struct sigaction wake{};
    wake.sa_handler = on_sigusr1;
    sigemptyset(&wake.sa_mask);
    sigaction(SIGUSR1, &wake, nullptr);
    for (int i = 1; i < argc; ++i)
        if (std::strcmp(argv[i], "-v") == 0) g_verbose = true;
    if (argc >= 5 && std::strcmp(argv[1], "--boot") == 0)
        return child_boot(argv[2], argv[3], (uint16_t)std::atoi(argv[4]));

    std::printf("safe-stop: output hold, start gate, halt behaviour (ADR-004 D1 D2 D7.3)\n");
    char tmpl[] = "/tmp/safe_stop_XXXXXX";
    if (!mkdtemp(tmpl) || chdir(tmpl) != 0)
    {
        std::printf("  FAIL  no temp dir\n");
        return 1;
    }
    output_hold_set_file_path("safe_stop.hold");
    std::thread smp(sampler);

    case_halt_holds_outputs();
    case_halt_safe_policy();
    Out held = case_safe_stop_holds();
    g_case   = "A059 SAFE_STOPPED";
    case_ungranted_starts_refused(PLC_STATE_SAFE_STOPPED);
    case_granted_start(held, "A206 granted start");
    case_stop_from_safe_stop();
    case_error_with_hold();
    stop_and_land();
    case_watchdog_stand_down();

    g_sampler_run = false;
    smp.join();
    run_child("boot_values.hold", "start", held.qw0);
    run_child("boot_values.hold", "stop", held.qw0);
    run_child("boot_gate.hold", "gate", held.qw0);

    if (g_failures == 0) std::printf("all cases passed\n");
    else std::printf("%d check(s) failed\n", g_failures);
    std::fflush(stdout);
    _exit(g_failures == 0 ? 0 : 1);   /* detached runtime threads may still be parked */
}
