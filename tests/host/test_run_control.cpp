// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file test_run_control.cpp
 * @brief Host test: breakpoints, pause, resume and stop over the core link (ADR-001 D2, D4, D7,
 *        D11; ADR-003 D1.3, D9). Criteria: A031 A032 A033 A034 A047 (the core's part).
 *
 * Runs the real state manager, dispatcher, image tables, debug agent and core link against a fake
 * 20 ms program whose statements carry probes exactly as STruCpp emits them (a relaxed flag-byte
 * load, strucpp_dbg_hit when non-zero, a frame linked while a session is attached). The test is a
 * client of the core link's unix socket, speaking length-prefixed JSON, and reads the output image
 * (what a plugin sees) as the plant.
 *
 * See also:
 *   tests/host/test_breakpoint_table.cpp - the table alone, under ThreadSanitizer.
 *   core/src/plc_app/debug_link.cpp - the core link this test drives.
 *   tests/debug/plant/runctl_e2e.py - the same scenarios against a real plc_main and a probed corpus program.
 */

// NO-FOLD: runtime-core

#include "../../core/src/lib/strucpp_abi.hpp"
#include "../../core/src/plc_app/debug_agent.h"
#include "../../core/src/plc_app/debug_link.h"
#include "../../core/src/plc_app/plc_state_manager.h"

extern "C" {
#include "../../core/src/drivers/plugin_driver.h"
#include "../../core/src/drivers/plugins/native/cjson/cJSON.h"
#include "../../core/src/plc_app/unix_socket.h"
void handle_unix_socket_commands(const char *command, char *response, size_t response_size);
}
#include "../../core/src/plc_app/image_tables.h"

#include <algorithm>
#include <atomic>
#include <csignal>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <functional>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

// ---------------------------------------------------------------------------
// Stubs for what the core links against in plc_main
// ---------------------------------------------------------------------------

static bool            g_verbose = getenv("VERBOSE") != nullptr;
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
void plugin_driver_cycle_end(plugin_driver_t *);
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
// Fake probed program: one 20 ms task
// ---------------------------------------------------------------------------

static const int64_t kMs       = 1000000;
static const int64_t kTaskNs   = 20 * kMs;
static const int64_t kDwellNs  = 150 * kMs;  /* under the 10-period stuck limit */
/* Probes the program reaches; the flag array is larger so the table can be filled (A032). */
static const uint32_t kProbes  = 2048;
enum : uint32_t { P_WITNESS = 0, P_K = 1, P_OUT = 2, P_DWELL = 3, P_L = 4, P_SUB = 5 };
/* POU IDs (a POU's entry probe in debug-info.json): the program and the FB it calls. */
enum : uint32_t { POU_PROG = 0, POU_SUB = 5 };

static volatile uint8_t g_flags[kProbes];
static uint8_t          g_active = 0;

extern "C" {
static uint8_t *fk_active_addr(void) { return &g_active; }
static uint32_t fk_probe_count(void) { return kProbes; }
static uint32_t fk_abi_version(void) { return 1; }
static int      fk_lock_depth(void) { return 0; }
void            strucpp_dbg_hit(uint32_t probe);
void            strucpp_dbg_frame_link(strucpp_dbg_frame *f);
void            strucpp_dbg_frame_unlink(strucpp_dbg_frame *f);
}

static int64_t mono_ns(void)
{
    timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000000000LL + t.tv_nsec;
}

/* STRUCPP_PROBE(id) under STRUCPP_THREADED. */
static inline void probe(uint32_t id)
{
    if (__builtin_expect(__atomic_load_n(&g_flags[id], __ATOMIC_RELAXED) != 0, 0))
        strucpp_dbg_hit(id);
}

/* strucpp::dbg::Frame. */
struct Frame : strucpp_dbg_frame
{
    Frame(uint32_t pou_id, const void *s)
    {
        linked = 0;
        at     = 0;
        if (*static_cast<volatile uint8_t *>(&g_active) & 1u)
        {
            pou    = pou_id;
            self   = s;
            locals = nullptr;
            strucpp_dbg_frame_link(this);
        }
    }
    ~Frame()
    {
        if (linked) strucpp_dbg_frame_unlink(this);
    }
};

static uint16_t m_witness, m_counter;

/* Mirrors of program storage the test reads while the task is parked or between scans. */
static std::atomic<int> g_witness_st{0}, g_counter_st{0};
static std::atomic<long> g_scans{0};
static std::atomic<bool> g_dwell{false}, g_in_dwell{false};

/* An FB instance the program calls after L: a second frame for the stack walk and stepping. */
struct Sub
{
    uint16_t speed = 0;
    void     run()
    {
        Frame f(POU_SUB, this);
        probe(P_SUB);
        speed = (uint16_t)(speed + 1);
    }
};

struct Prog : strucpp::ProgramBase
{
    Sub sub;
    void run() override
    {
        Frame f(POU_PROG, this);
        probe(P_WITNESS);
        m_witness   = (uint16_t)(m_witness + 1);
        g_witness_st = m_witness;
        probe(P_K);
        m_counter   = (uint16_t)(m_counter + 1);
        g_counter_st = m_counter;
        probe(P_OUT);
        if (g_dwell.load())
        {
            const int64_t t0 = mono_ns();
            g_in_dwell       = true;
            while ((probe(P_DWELL), mono_ns() - t0 < kDwellNs)) {}
            g_in_dwell = false;
        }
        probe(P_L);
        f.at = P_L; /* STRUCPP_AT: the statement making the call */
        sub.run();
        g_scans++;
    }
    void located_range(uint32_t *o, uint32_t *c) const override
    {
        *o = 0;
        *c = 2;
    }
};

static Prog                  g_prog;
static strucpp::ProgramBase *g_progs[] = {&g_prog};
static strucpp::TaskInstance g_tasks[] = {{"Main", kTaskNs, 1, g_progs, 1}};
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
    {A::Output, S::Word, 0, 0, {}, &m_witness},
    {A::Output, S::Word, 1, 0, {}, &m_counter},
};

static char g_md5[33] = "0123456789abcdef0123456789abcdef";

extern "C" {
static void fk_advance(uint64_t) {}
static void fk_set_time(int64_t) {}
static strucpp::ConfigurationInstance *fk_config(void) { return &g_cfg; }
static const strucpp::LocatedVar *fk_lv(void) { return g_lv; }
static uint32_t fk_lv_count(void) { return 2; }
/* The program's debug table (generated_debug.cpp): leaves 0 witness, 1 counter, 2 sub.speed. */
static strucpp_debug_entry g_leaf_arr[3] = {
    {&m_witness, 3, 0}, {&m_counter, 3, 0}, {&g_prog.sub.speed, 3, 0}};
static const strucpp_debug_entry *g_leaf_arrays[1] = {g_leaf_arr};
static uint8_t  fk_arr_count(void) { return 1; }
static uint16_t fk_elem_count(uint8_t a) { return a == 0 ? 3 : 0; }
static uint16_t fk_size(uint8_t a, uint16_t e) { return a == 0 && e < 3 ? 2 : 0; }
static uint8_t  fk_set(uint8_t, uint16_t, bool, const uint8_t *, uint16_t) { return 0; }
static uint16_t fk_read(uint8_t a, uint16_t e, uint8_t *dest)
{
    if (a != 0 || e >= 3) return 0;
    std::memcpy(dest, g_leaf_arr[e].ptr, 2);
    return 2;
}
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
void *plugin_manager_try_get_symbol(PluginManager *, const char *n)
{
    struct { const char *name; void *fn; } t[] = {
        {"strucpp_dbg_flags", (void *)g_flags},
        {"strucpp_dbg_active_addr", (void *)fk_active_addr},
        {"strucpp_dbg_probe_count", (void *)fk_probe_count},
        {"strucpp_dbg_abi_version", (void *)fk_abi_version},
        {"strucpp_dbg_lock_depth", (void *)fk_lock_depth},
        {STRUCPP_DEBUG_ARRAYS_SYMBOL, (void *)g_leaf_arrays},
    };
    for (auto &e : t)
        if (std::strcmp(e.name, n) == 0) return e.fn;
    return nullptr;
}
}

// ---------------------------------------------------------------------------
// The plant: samples the output image at every cycle_end, as the reference plant does
// ---------------------------------------------------------------------------

static std::mutex       g_plant_mu;
static std::vector<int> g_plant_witness;
static std::vector<int64_t> g_plant_t;

extern "C" void plugin_driver_cycle_end(plugin_driver_t *)
{
    image_lock();
    const int w = int_output[0] ? *int_output[0] : -1;
    image_unlock();
    std::lock_guard<std::mutex> g(g_plant_mu);
    g_plant_witness.push_back(w);
    g_plant_t.push_back(mono_ns());
}

/* When the plant's witness took the value it has now, if that was after `from`; else `from`. */
static int64_t plant_frozen_since(int64_t from)
{
    std::lock_guard<std::mutex> g(g_plant_mu);
    size_t i = g_plant_witness.size();
    if (i == 0) return from;
    const int last = g_plant_witness[i - 1];
    while (i > 0 && g_plant_witness[i - 1] == last && g_plant_t[i - 1] >= from) --i;
    return i < g_plant_witness.size() && g_plant_t[i] > from ? g_plant_t[i] : from;
}

static size_t plant_mark(void)
{
    std::lock_guard<std::mutex> g(g_plant_mu);
    return g_plant_witness.size();
}

static std::vector<int> plant_since(size_t from)
{
    std::lock_guard<std::mutex> g(g_plant_mu);
    return std::vector<int>(g_plant_witness.begin() + (long)from, g_plant_witness.end());
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
    for (int i = 0; i < timeout_ms; i += 1)
    {
        if (pred()) return true;
        sleep_ms(1);
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

/* The plant's view: the output image, under the image lock as a plugin reads it. */
static int plant_word(int idx)
{
    image_lock();
    int v = int_output[idx] ? *int_output[idx] : -1;
    image_unlock();
    return v;
}

// ---- Core-link client: u32 BE length + JSON ----

struct Msg
{
    int64_t t_ns;
    cJSON  *j;
};

struct Link
{
    int               fd = -1;
    std::thread       rd;
    std::mutex        mu;
    std::vector<Msg>  msgs;
    std::atomic<bool> closed{false};
    int               seq = 0;
    std::string       session; /* from attach; added to every request that names none */

    bool open(const char *path)
    {
        fd = socket(AF_UNIX, SOCK_STREAM, 0);
        sockaddr_un a{};
        a.sun_family = AF_UNIX;
        std::snprintf(a.sun_path, sizeof a.sun_path, "%s", path);
        for (int i = 0; i < 200; ++i)
        {
            if (connect(fd, (sockaddr *)&a, sizeof a) == 0)
            {
                rd = std::thread([this] { reader(); });
                return true;
            }
            sleep_ms(10);
        }
        return false;
    }
    void reader()
    {
        for (;;)
        {
            uint32_t be;
            if (!read_all(&be, 4)) break;
            std::string body(ntohl(be), '\0');
            if (!read_all(&body[0], body.size())) break;
            cJSON *j = cJSON_Parse(body.c_str());
            std::lock_guard<std::mutex> g(mu);
            msgs.push_back({mono_ns(), j});
        }
        closed = true;
    }
    bool read_all(void *p, size_t n)
    {
        char *c = static_cast<char *>(p);
        while (n)
        {
            ssize_t r = ::read(fd, c, n);
            if (r <= 0) return false;
            c += r;
            n -= (size_t)r;
        }
        return true;
    }
    int send(const char *command, const char *args_json)
    {
        std::string args = args_json ? args_json : "{}";
        if (!session.empty() && args.find("\"session\"") == std::string::npos)
            args.insert(1, "\"session\":\"" + session + "\"" + (args == "{}" ? "" : ","));
        std::string buf(args.size() + 256, '\0');
        int  s = ++seq;
        int  n = std::snprintf(&buf[0], buf.size(),
                               "{\"type\":\"request\",\"seq\":%d,\"command\":\"%s\",\"arguments\":%s}", s,
                               command, args.c_str());
        uint32_t be = htonl((uint32_t)n);
        (void)!::write(fd, &be, 4);
        (void)!::write(fd, buf.data(), (size_t)n);
        return s;
    }
    /* Waits for the response to a request; nullptr on timeout. */
    cJSON *response(int s, int timeout_ms = 2000)
    {
        cJSON *r = nullptr;
        wait_for([&] {
            std::lock_guard<std::mutex> g(mu);
            for (auto &m : msgs)
            {
                cJSON *rs = cJSON_GetObjectItem(m.j, "request_seq");
                if (rs && rs->valueint == s) { r = m.j; return true; }
            }
            return false;
        }, timeout_ms);
        return r;
    }
    cJSON *call(const char *command, const char *args_json = nullptr, int timeout_ms = 2000)
    {
        return response(send(command, args_json), timeout_ms);
    }
    size_t mark()
    {
        std::lock_guard<std::mutex> g(mu);
        return msgs.size();
    }
    /* First event named `name` received at or after index `from` that pred accepts. */
    bool event(size_t from, const char *name, Msg *out, int timeout_ms,
               const std::function<bool(cJSON *)> &pred = nullptr)
    {
        return wait_for([&] {
            std::lock_guard<std::mutex> g(mu);
            for (size_t i = from; i < msgs.size(); ++i)
            {
                cJSON *e = cJSON_GetObjectItem(msgs[i].j, "event");
                if (!e || !cJSON_IsString(e) || std::strcmp(e->valuestring, name) != 0) continue;
                cJSON *b = cJSON_GetObjectItem(msgs[i].j, "body");
                if (pred && !pred(b)) continue;
                if (out) *out = msgs[i];
                return true;
            }
            return false;
        }, timeout_ms);
    }
    int count(size_t from, const char *name)
    {
        std::lock_guard<std::mutex> g(mu);
        int n = 0;
        for (size_t i = from; i < msgs.size(); ++i)
        {
            cJSON *e = cJSON_GetObjectItem(msgs[i].j, "event");
            if (e && cJSON_IsString(e) && std::strcmp(e->valuestring, name) == 0) n++;
        }
        return n;
    }
};

static bool ok(cJSON *r)
{
    cJSON *s = r ? cJSON_GetObjectItem(r, "success") : nullptr;
    return s && cJSON_IsTrue(s);
}

static const char *err_code(cJSON *r)
{
    cJSON *e = r ? cJSON_GetObjectItem(r, "error") : nullptr;
    cJSON *c = e ? cJSON_GetObjectItem(e, "code") : nullptr;
    return c && cJSON_IsString(c) ? c->valuestring : "";
}

static int body_int(cJSON *m, const char *key)
{
    cJSON *b = m ? cJSON_GetObjectItem(m, "body") : nullptr;
    cJSON *v = b ? cJSON_GetObjectItem(b, key) : nullptr;
    return v && cJSON_IsNumber(v) ? v->valueint : -1;
}

static std::string body_str(cJSON *m, const char *key)
{
    cJSON *b = m ? cJSON_GetObjectItem(m, "body") : nullptr;
    cJSON *v = b ? cJSON_GetObjectItem(b, key) : nullptr;
    return v && cJSON_IsString(v) ? v->valuestring : "";
}

static int frame_field(cJSON *frames, int i, const char *key)
{
    cJSON *f = frames ? cJSON_GetArrayItem(frames, i) : nullptr;
    cJSON *v = f ? cJSON_GetObjectItem(f, key) : nullptr;
    return v && cJSON_IsNumber(v) ? v->valueint : -1;
}

static std::function<bool(cJSON *)> stopped_at(const char *reason, int probe_id)
{
    return [=](cJSON *b) {
        cJSON *r = cJSON_GetObjectItem(b, "reason");
        cJSON *p = cJSON_GetObjectItem(b, "probe");
        return r && std::strcmp(r->valuestring, reason) == 0 &&
               (probe_id < 0 || (p && p->valueint == probe_id));
    };
}

static std::function<bool(cJSON *)> state_is(const char *s)
{
    return [=](cJSON *b) {
        cJSON *v = cJSON_GetObjectItem(b, "state");
        return v && cJSON_IsString(v) && std::strcmp(v->valuestring, s) == 0;
    };
}

static char g_sock[108];
static Link g_link;

static bool start_plc(void)
{
    const long n = g_scans.load();
    return cmd("START") == "START:OK" &&
           wait_for([] { return plc_get_state() == PLC_STATE_RUNNING; }, 3000) &&
           wait_for([&] { return g_scans.load() > n + 3; }, 2000);
}

static void cont(void)
{
    cJSON *r = g_link.call("continue");
    CHECK(ok(r), "continue refused: %s", err_code(r));
}

// ---------------------------------------------------------------------------
// Cases
// ---------------------------------------------------------------------------

/* docs/DEBUG_CORE_LINK.md: hello is the connection's first frame and declares the link version,
 * the probes, the breakpoint limit and the image; attach mints a session; initialize is not a link
 * request and an unknown session is refused; a beat arrives every 250 ms. */
static void case_handshake(void)
{
    g_case = "handshake";
    Msg hello{};
    CHECK(g_link.event(0, "hello", &hello, 1000), "no hello from the core");
    {
        std::lock_guard<std::mutex> g(g_link.mu);
        cJSON *first = g_link.msgs.empty() ? nullptr : cJSON_GetObjectItem(g_link.msgs[0].j, "event");
        CHECK(first && cJSON_IsString(first) && !std::strcmp(first->valuestring, "hello"),
              "hello is not the first frame");
    }
    cJSON *hb   = cJSON_GetObjectItem(hello.j, "body");
    cJSON *caps = cJSON_GetObjectItem(hb, "capabilities");
    cJSON *img  = cJSON_GetObjectItem(hb, "image");
    CHECK(body_str(hello.j, "link") == "1.0", "hello link '%s'", body_str(hello.j, "link").c_str());
    CHECK(body_int(hello.j, "probes") == (int)kProbes, "hello probes %d", body_int(hello.j, "probes"));
    CHECK(caps && cJSON_GetObjectItem(caps, "maxBreakpoints") &&
              cJSON_GetObjectItem(caps, "maxBreakpoints")->valueint == DBG_MAX_BREAKPOINTS,
          "hello does not declare maxBreakpoints %d", DBG_MAX_BREAKPOINTS);
    cJSON *md5 = img ? cJSON_GetObjectItem(img, "md5") : nullptr;
    CHECK(md5 && cJSON_IsString(md5) && strlen(md5->valuestring) == 32, "hello image has no md5");
    cJSON *r = g_link.call("initialize", "{\"protocol\":\"1.0\"}");
    CHECK(!ok(r) && !std::strcmp(err_code(r), "UNSUPPORTED"), "initialize answered '%s', want UNSUPPORTED",
          err_code(r));
    r = g_link.call("pause", "{\"session\":\"s999\"}");
    CHECK(!ok(r) && !std::strcmp(err_code(r), "SESSION_ENDED"), "an unknown session answered '%s'", err_code(r));
    r = g_link.call("attach", "{\"user\":\"tester\",\"role\":\"controller\"}");
    CHECK(ok(r) && !body_str(r, "session").empty(), "attach refused: %s", err_code(r));
    g_link.session = body_str(r, "session");
    const size_t m = g_link.mark();
    sleep_ms(600);
    CHECK(g_link.count(m, "beat") >= 2, "%d beats in 600 ms", g_link.count(m, "beat"));
}

/* A034, A031(a), A047: stop before K with no side effect, step, a breakpoint set while halted
 * later in the same scan, and the run state pushed and readable. The park outlasts the stuck
 * limit (10 periods), so the watchdog stand-down is exercised too. */
static void case_before_statement(void)
{
    g_case       = "A034 before statement";
    size_t m     = g_link.mark();
    const int64_t t_set = mono_ns();
    /* Another file's breakpoint first, on a probe the program never reaches, so K's breakpoint
     * id (2) differs from its probe ID (1) and the stopped event's breakpointIds can tell them apart. */
    cJSON *r     = g_link.call("setBreakpoints", "{\"fileId\":1,\"probes\":[2000]}");
    CHECK(ok(r), "setBreakpoints file 1 refused: %s", err_code(r));
    r            = g_link.call("setBreakpoints", "{\"fileId\":0,\"probes\":[1]}");
    CHECK(ok(r), "setBreakpoints refused: %s", err_code(r));
    cJSON *bp0 = cJSON_GetArrayItem(cJSON_GetObjectItem(cJSON_GetObjectItem(r, "body"), "breakpoints"), 0);
    const int bp_id = bp0 && cJSON_GetObjectItem(bp0, "id") ? cJSON_GetObjectItem(bp0, "id")->valueint : -1;
    Msg st{};
    CHECK(g_link.event(m, "stopped", &st, 1000, stopped_at("breakpoint", P_K)),
          "no breakpoint stop at K");
    cJSON *ids = cJSON_GetObjectItem(cJSON_GetObjectItem(st.j, "body"), "breakpointIds");
    CHECK(body_str(st.j, "task") == "Main" && body_int(st.j, "taskId") == 0 && cJSON_GetArraySize(ids) == 1 &&
              cJSON_GetArrayItem(ids, 0)->valueint == bp_id,
          "stopped does not name task Main, taskId 0 and breakpoint id %d", bp_id);
    Msg rs{};
    CHECK(g_link.event(m, "runState", &rs, 500, state_is("HALTED_AT_BP")),
          "no HALTED_AT_BP runState within 500 ms");
    CHECK(rs.t_ns - st.t_ns <= 500 * kMs, "runState %.0f ms after the stop",
          (double)(rs.t_ns - st.t_ns) / 1e6);
    (void)t_set;

    const int c0 = g_counter_st.load(), w_st = g_witness_st.load();
    const int plant_c = plant_word(1), plant_w = plant_word(0);
    const int64_t vt0 = plc_task_virtual_time_ns(0);
    sleep_ms(300);
    CHECK(g_counter_st.load() == c0, "counter moved while halted before K (%d -> %d)", c0,
          g_counter_st.load());
    CHECK(plant_word(1) == plant_c && plant_word(0) == plant_w,
          "the plant saw an output change during the halt");
    CHECK(plant_c == c0, "the plant's counter %d is not the last complete scan's %d", plant_c, c0);
    CHECK(plc_task_virtual_time_ns(0) == vt0, "the task's virtual clock moved while parked");

    cJSON *s = g_link.call("status");
    CHECK(body_str(s, "state") == "HALTED_AT_BP", "status state '%s'", body_str(s, "state").c_str());
    cJSON *tasks = cJSON_GetObjectItem(cJSON_GetObjectItem(s, "body"), "tasks");
    cJSON *t0    = tasks ? cJSON_GetArrayItem(tasks, 0) : nullptr;
    CHECK(t0 && cJSON_GetObjectItem(t0, "probe") && cJSON_GetObjectItem(t0, "probe")->valueint == 1 &&
              cJSON_GetObjectItem(t0, "since") && cJSON_IsString(cJSON_GetObjectItem(t0, "task")) &&
              cJSON_GetObjectItem(t0, "taskId"),
          "status does not carry the halted task's name, probe and since");
    CHECK(body_str(s, "by") == "tester", "status 'by' is '%s'", body_str(s, "by").c_str());

    /* Step over K: the counter changes by one; the plant sees nothing until the scan ends. */
    g_case = "A034 step";
    m      = g_link.mark();
    r      = g_link.call("next"); /* no task: the task of the most recent stop */
    CHECK(ok(r), "next refused: %s", err_code(r));
    CHECK(g_link.event(m, "stopped", nullptr, 1000, stopped_at("step", P_OUT)),
          "the step did not land on the next statement");
    CHECK(g_counter_st.load() == c0 + 1, "counter %d after the step, want %d", g_counter_st.load(),
          c0 + 1);
    CHECK(plant_word(1) == plant_c, "the plant saw the step's write before the scan ended");

    /* A031(a): set L while halted, continue: the stop is at L in the same scan. */
    g_case = "A031 same scan";
    r      = g_link.call("setBreakpoints", "{\"fileId\":0,\"probes\":[1,4]}");
    CHECK(ok(r), "setBreakpoints K, L refused: %s", err_code(r));
    m = g_link.mark();
    cont();
    CHECK(g_link.event(m, "stopped", nullptr, 1000, stopped_at("breakpoint", P_L)), "no stop at L");
    CHECK(g_witness_st.load() == w_st, "witness %d at L, want %d: not the same scan",
          g_witness_st.load(), w_st);
    CHECK(plant_word(0) == plant_w, "the plant's witness advanced between K and L");
    CHECK(plc_get_state() == PLC_STATE_RUNNING, "PLC state %d after a park past the stuck limit",
          (int)plc_get_state());

    /* A034, A088 (the core's part): the halted task's frames, innermost first, each with its POU,
     * its location probe and its instance's first leaf; and leaf values read while halted. */
    g_case = "A088 stack";
    r      = g_link.call("stackTrace", "{\"task\":\"Main\"}");
    cJSON *fr = cJSON_GetObjectItem(cJSON_GetObjectItem(r, "body"), "frames");
    CHECK(ok(r) && cJSON_GetArraySize(fr) == 1 &&
              frame_field(fr, 0, "pou") == (int)POU_PROG && frame_field(fr, 0, "at") == (int)P_L,
          "stackTrace at L: %s", r ? cJSON_PrintUnformatted(r) : "no response");
    m = g_link.mark();
    r = g_link.call("stepIn");
    CHECK(ok(r), "stepIn refused: %s", err_code(r));
    Msg in{};
    CHECK(g_link.event(m, "stopped", &in, 1000, stopped_at("step", P_SUB)), "stepIn did not land in the FB");
    CHECK(body_int(in.j, "depth") == 2, "depth %d in the FB, want 2", body_int(in.j, "depth"));
    r  = g_link.call("stackTrace");
    fr = cJSON_GetObjectItem(cJSON_GetObjectItem(r, "body"), "frames");
    cJSON *trunc = cJSON_GetObjectItem(cJSON_GetObjectItem(r, "body"), "truncated");
    CHECK(ok(r) && cJSON_GetArraySize(fr) == 2 && frame_field(fr, 0, "pou") == (int)POU_SUB &&
              frame_field(fr, 0, "at") == (int)P_SUB && frame_field(fr, 0, "leaf") == 2 &&
              frame_field(fr, 1, "pou") == (int)POU_PROG && frame_field(fr, 1, "at") == (int)P_L &&
              cJSON_IsFalse(trunc),
          "stackTrace in the FB: %s", r ? cJSON_PrintUnformatted(r) : "no response");
    g_case = "A034 read";
    r = g_link.call("readLeaves", "{\"leaves\":[1,2,99]}");
    cJSON *vals = cJSON_GetObjectItem(cJSON_GetObjectItem(r, "body"), "values");
    char want_c[8], want_s[8];
    const uint16_t c_now = (uint16_t)g_counter_st.load(), s_now = g_prog.sub.speed;
    std::snprintf(want_c, sizeof want_c, "%02x%02x", c_now & 0xFF, c_now >> 8);
    std::snprintf(want_s, sizeof want_s, "%02x%02x", s_now & 0xFF, s_now >> 8);
    CHECK(ok(r) && cJSON_GetArraySize(vals) == 3 &&
              !std::strcmp(cJSON_GetArrayItem(vals, 0)->valuestring ? cJSON_GetArrayItem(vals, 0)->valuestring : "", want_c) &&
              !std::strcmp(cJSON_GetArrayItem(vals, 1)->valuestring ? cJSON_GetArrayItem(vals, 1)->valuestring : "", want_s) &&
              cJSON_IsNull(cJSON_GetArrayItem(vals, 2)),
          "readLeaves %s, want [%s, %s, null]", r ? cJSON_PrintUnformatted(r) : "none", want_c, want_s);
    g_case = "A035 step out";
    m = g_link.mark();
    r = g_link.call("stepOut");
    CHECK(ok(r), "stepOut refused: %s", err_code(r));
    CHECK(g_link.event(m, "stopped", nullptr, 1000, stopped_at("step", P_WITNESS)),
          "stepOut of the FB, the program's last call, did not land on the next scan's first statement");

    /* A038: a step landing on an armed breakpoint (K) is one stop, reason breakpoint. The scan
     * the step crossed completed on the way: the stop at K sees its outputs in the plant. */
    g_case = "A038 step onto breakpoint";
    m = g_link.mark();
    r = g_link.call("next");
    CHECK(ok(r), "next refused: %s", err_code(r));
    CHECK(g_link.event(m, "stopped", nullptr, 1000, stopped_at("breakpoint", P_K)), "no stop at K");
    sleep_ms(200);
    CHECK(g_link.count(m, "stopped") == 1, "%d stopped events for one step onto a breakpoint",
          g_link.count(m, "stopped"));
    CHECK(plant_word(1) == c0 + 1, "plant counter %d at the next scan, want %d", plant_word(1), c0 + 1);
    CHECK(plant_word(0) == w_st, "plant witness %d, want %d", plant_word(0), w_st);

    r = g_link.call("clearAllBreakpoints");
    CHECK(body_int(r, "removed") == 3, "clearAllBreakpoints removed %d", body_int(r, "removed"));
    m = g_link.mark();
    cont();
    CHECK(g_link.event(m, "runState", nullptr, 500, state_is("RUNNING")), "no RUNNING runState");
    g_case = "A088 running";
    r = g_link.call("stackTrace", "{\"task\":0}");
    CHECK(!ok(r) && !std::strcmp(err_code(r), "NOT_HALTED"), "stackTrace of a running task answered '%s'",
          err_code(r));
}

/* A031(b): set L while the scan is in the dwell before it; the stop is in that scan, judged by
 * the plant's witness. Then clear it: no stop in 100 scans. */
static void case_armed_in_dwell(void)
{
    g_case = "A031 armed in dwell";
    g_dwell = true;
    CHECK(wait_for([] { return g_in_dwell.load(); }, 1000), "the program never entered its dwell");
    size_t m   = g_link.mark();
    cJSON *r   = g_link.call("setBreakpoints", "{\"fileId\":0,\"probes\":[4]}");
    const int w = plant_word(0);
    const size_t pm = plant_mark();
    CHECK(ok(r), "setBreakpoints refused: %s", err_code(r));
    CHECK(g_in_dwell.load(), "the response arrived after the dwell (test timing)");
    g_dwell = false;
    CHECK(g_link.event(m, "stopped", nullptr, 1000, stopped_at("breakpoint", P_L)), "no stop at L");
    sleep_ms(300);
    std::vector<int> halt = plant_since(pm);
    CHECK(halt.size() >= 10, "only %zu plant frames during the halt", halt.size());
    CHECK(std::all_of(halt.begin(), halt.end(), [&](int v) { return v == w; }),
          "the plant's witness left %d during the halt: the stop came a scan late", w);
    CHECK(g_witness_st.load() == w + 1, "the session read witness %d, want %d", g_witness_st.load(),
          w + 1);

    r = g_link.call("setBreakpoints", "{\"fileId\":0,\"probes\":[]}");
    CHECK(ok(r), "clearing file 0's breakpoints refused: %s", err_code(r));
    m = g_link.mark();
    const size_t pr = plant_mark();
    cont();
    const long n0 = g_scans.load();
    wait_for([&] { return g_scans.load() >= n0 + 100; }, 5000);
    std::vector<int> seen{w};
    for (int v : plant_since(pr))
        if (seen.back() != v) seen.push_back(v);
    CHECK(g_scans.load() >= n0 + 100, "only %ld scans after the clear", g_scans.load() - n0);
    CHECK(g_link.count(m, "stopped") == 0, "%d stops after the clear", g_link.count(m, "stopped"));
    CHECK(seen.size() >= 2 && seen[1] == w + 1, "after the resume the plant saw %d, want %d",
          seen.size() >= 2 ? seen[1] : -1, w + 1);
    int gaps = 0;
    for (size_t i = 1; i < seen.size(); ++i) gaps += seen[i] != seen[i - 1] + 1;
    CHECK(gaps == 0, "%d gaps in the plant's witness after the resume", gaps);
}

/* A032 over the link: the limit is honoured, the one past it refused, and nothing displaced. */
static void case_limit(void)
{
    g_case = "A032 limit";
    std::string args = "{\"fileId\":0,\"probes\":[{\"probe\":1}";
    for (uint32_t p = 100; p < 100 + DBG_MAX_BREAKPOINTS - 1; ++p)
        args += "," + std::to_string(p);
    args += "]}";
    /* Marked before the arming call: K is armed by it, and the stop may come before the
     * refusal below is answered. */
    size_t m  = g_link.mark();
    cJSON *r  = g_link.call("setBreakpoints", args.c_str());
    cJSON *bs = cJSON_GetObjectItem(cJSON_GetObjectItem(r, "body"), "breakpoints");
    int verified = 0;
    for (int i = 0; bs && i < cJSON_GetArraySize(bs); ++i)
        verified += cJSON_IsTrue(cJSON_GetObjectItem(cJSON_GetArrayItem(bs, i), "verified"));
    CHECK(verified == DBG_MAX_BREAKPOINTS, "%d of %d verified", verified, DBG_MAX_BREAKPOINTS);
    r = g_link.call("setBreakpoints", "{\"fileId\":1,\"probes\":[1500]}");
    cJSON *past = cJSON_GetArrayItem(cJSON_GetObjectItem(cJSON_GetObjectItem(r, "body"), "breakpoints"), 0);
    cJSON *perr = past ? cJSON_GetObjectItem(past, "error") : nullptr;
    CHECK(ok(r) && past && cJSON_IsFalse(cJSON_GetObjectItem(past, "verified")) && perr &&
              !std::strcmp(cJSON_GetObjectItem(perr, "code")->valuestring, "LIMIT_EXCEEDED"),
          "the breakpoint past the limit was not refused per item with LIMIT_EXCEEDED");
    CHECK(g_link.event(m, "stopped", nullptr, 1000, stopped_at("breakpoint", P_K)),
          "no stop at a previously set breakpoint after the refusal");
    r = g_link.call("clearAllBreakpoints");
    CHECK(body_int(r, "removed") == DBG_MAX_BREAKPOINTS, "clearAllBreakpoints removed %d",
          body_int(r, "removed"));
    cont();
}

/* A033: pause halts within one interval + 100 ms, judged by when the plant's witness froze;
 * at random points, idle (a boundary hold) and inside a long scan (a park at the next probe). */
static void case_pause(void)
{
    g_case = "A033 pause";
    std::mt19937 rng(7);
    int64_t      worst = 0;
    for (int i = 0; i < 25; ++i)
    {
        g_dwell = i >= 10; /* scans of 150 ms: a pause deferred to a boundary misses the bound */
        sleep_ms((int)(rng() % (i >= 10 ? 150 : 40)));
        size_t        m  = g_link.mark();
        const int64_t t0 = mono_ns();
        cJSON        *r  = g_link.call("pause");
        CHECK(ok(r), "pause refused: %s", err_code(r));
        CHECK(g_link.event(m, "stopped", nullptr, 1000, stopped_at("pause", -1)), "no pause stop");
        if (i == 0)
            CHECK(g_link.event(m, "runState", nullptr, 500, state_is("PAUSED")), "no PAUSED runState");
        sleep_ms(250);
        worst = std::max(worst, plant_frozen_since(t0) - t0);
        g_dwell        = false;
        const long n   = g_scans.load();
        cont();
        CHECK(wait_for([&] { return g_scans.load() > n + 1; }, 1000), "no scans after continue");
    }
    CHECK(worst <= kTaskNs + 100 * kMs, "the plant froze %.0f ms after a pause request (bound %lld)",
          (double)worst / 1e6, (long long)((kTaskNs + 100 * kMs) / kMs));
}

static double body_num(cJSON *m, const char *key)
{
    cJSON *b = m ? cJSON_GetObjectItem(m, "body") : nullptr;
    cJSON *v = b ? cJSON_GetObjectItem(b, key) : nullptr;
    return v && cJSON_IsNumber(v) ? v->valuedouble : -1.0;
}

/* A036, A037 (the core's part): a cycle step from a probe runs exactly one scan and stops at the
 * same probe in the next scan, with the task's virtual time one interval on and the plant one
 * scan on; statement steps leave virtual time alone; a cycle step from a boundary hold runs one
 * scan and holds again. Every stop reports the task's virtual time as `vt` (ms). */
static void case_cycle_step(void)
{
    g_case   = "A036 cycle step";
    size_t m = g_link.mark();
    cJSON *r = g_link.call("setBreakpoints", "{\"fileId\":0,\"probes\":[1]}");
    CHECK(ok(r), "setBreakpoints refused: %s", err_code(r));
    Msg st{};
    CHECK(g_link.event(m, "stopped", &st, 1000, stopped_at("breakpoint", P_K)), "no stop at K");
    g_link.call("setBreakpoints", "{\"fileId\":0,\"probes\":[]}");
    const int64_t vt0 = plc_task_virtual_time_ns(0);
    const int     w0  = g_witness_st.load(), pw0 = plant_word(0);
    CHECK(body_num(st.j, "vt") == (double)vt0 / 1e6, "stopped vt %.3f, the task's virtual time %.3f ms",
          body_num(st.j, "vt"), (double)vt0 / 1e6);
    for (int i = 1; i <= 3; ++i)
    {
        m = g_link.mark();
        r = g_link.call("cycleStep", "{\"task\":\"Main\"}");
        CHECK(ok(r), "cycleStep refused: %s", err_code(r));
        Msg cs{};
        CHECK(g_link.event(m, "stopped", &cs, 1000, stopped_at("step", P_K)),
              "cycle step %d did not stop at K in the next scan", i);
        CHECK(plc_task_virtual_time_ns(0) == vt0 + i * kTaskNs, "virtual time +%.1f ms after %d cycle steps",
              (double)(plc_task_virtual_time_ns(0) - vt0) / 1e6, i);
        CHECK(body_num(cs.j, "vt") == (double)(vt0 + i * kTaskNs) / 1e6, "stopped vt %.3f after %d cycle steps",
              body_num(cs.j, "vt"), i);
        CHECK(g_witness_st.load() == w0 + i && plant_word(0) == pw0 + i,
              "after %d cycle steps the program's witness is +%d and the plant's +%d", i,
              g_witness_st.load() - w0, plant_word(0) - pw0);
    }
    g_case = "A036 statement steps";
    for (int i = 0; i < 2; ++i)
    {
        m = g_link.mark();
        CHECK(ok(g_link.call("next")), "next refused");
        CHECK(g_link.event(m, "stopped", nullptr, 1000, stopped_at("step", -1)), "no step stop");
    }
    CHECK(plc_task_virtual_time_ns(0) == vt0 + 3 * kTaskNs, "statement steps moved virtual time");

    g_case = "A036 from a boundary";
    cont();
    bool boundary = false;
    for (int i = 0; i < 40 && !boundary; ++i)
    {
        m = g_link.mark();
        g_link.call("pause");
        Msg ps{};
        CHECK(g_link.event(m, "stopped", &ps, 1000, stopped_at("pause", -1)), "no pause stop");
        cJSON *hb = cJSON_GetObjectItem(cJSON_GetObjectItem(ps.j, "body"), "heldAtBoundary");
        boundary  = hb && cJSON_IsTrue(hb);
        if (!boundary) cont();
    }
    CHECK(boundary, "never paused at a scan boundary in 40 tries");
    const int64_t vt1 = plc_task_virtual_time_ns(0);
    const int     w1  = g_witness_st.load();
    m = g_link.mark();
    r = g_link.call("cycleStep");
    CHECK(ok(r), "cycleStep from a boundary refused: %s", err_code(r));
    Msg bs{};
    CHECK(g_link.event(m, "stopped", &bs, 1000, [](cJSON *b) {
              cJSON *rs = cJSON_GetObjectItem(b, "reason"), *hb = cJSON_GetObjectItem(b, "heldAtBoundary");
              return rs && !std::strcmp(rs->valuestring, "step") && hb && cJSON_IsTrue(hb);
          }),
          "a cycle step from a boundary did not hold at the next boundary");
    CHECK(g_witness_st.load() == w1 + 1 && plc_task_virtual_time_ns(0) == vt1 + kTaskNs,
          "a cycle step from a boundary ran %d scans and moved virtual time %.1f ms",
          g_witness_st.load() - w1, (double)(plc_task_virtual_time_ns(0) - vt1) / 1e6);
    sleep_ms(100);
    CHECK(g_witness_st.load() == w1 + 1, "the task ran on after the cycle step's hold");
    cont();
}

/* A033 stop: the program stops, the session stays; a stop while parked lands STOPPED, not ERROR. */
static void case_stop(void)
{
    g_case   = "A033 stop";
    size_t m = g_link.mark();
    cJSON *r = g_link.call("stop");
    CHECK(ok(r), "stop refused: %s", err_code(r));
    CHECK(wait_for([] { return plc_get_state() == PLC_STATE_STOPPED; }, 3000), "state %d after stop",
          (int)plc_get_state());
    CHECK(g_link.event(m, "runState", nullptr, 1000, state_is("STOPPED")), "no STOPPED runState");
    r = g_link.call("status");
    cJSON *att = cJSON_GetObjectItem(cJSON_GetObjectItem(r, "body"), "attached");
    CHECK(att && cJSON_IsTrue(att), "the session is no longer attached after stop");
    CHECK(g_link.count(m, "sessionEnded") == 0 && !g_link.closed, "stop ended the session");

    g_case = "D7.4 stop while parked";
    CHECK(start_plc(), "restart failed");
    m = g_link.mark();
    r = g_link.call("setBreakpoints", "{\"fileId\":0,\"probes\":[0]}");
    CHECK(ok(r), "setBreakpoints refused after a restart: %s", err_code(r));
    CHECK(g_link.event(m, "stopped", nullptr, 1000, stopped_at("breakpoint", P_WITNESS)),
          "no stop after the restart");
    r = g_link.call("stop");
    CHECK(wait_for([] { return plc_get_state() == PLC_STATE_STOPPED || plc_get_state() == PLC_STATE_ERROR; }, 3000) &&
              plc_get_state() == PLC_STATE_STOPPED,
          "a stop while parked landed %d, not STOPPED", (int)plc_get_state());
}

static void on_wake(int) {}

int main()
{
    setvbuf(stdout, nullptr, _IONBF, 0);
    /* plc_main installs this wake handler; task threads are woken with SIGUSR1. */
    struct sigaction wake;
    std::memset(&wake, 0, sizeof wake);
    wake.sa_handler = on_wake;
    sigaction(SIGUSR1, &wake, nullptr);
    std::snprintf(g_sock, sizeof g_sock, "/tmp/plc_debug_test_%d.sock", (int)getpid());
    if (debug_link_start(g_sock) != 0)
    {
        std::printf("  FAIL  the core link did not start on %s\n", g_sock);
        return 1;
    }
    if (!start_plc())
    {
        std::printf("  FAIL  the PLC did not start\n");
        return 1;
    }
    if (!g_link.open(g_sock))
    {
        std::printf("  FAIL  cannot connect to the core link\n");
        return 1;
    }
    case_handshake();
    case_before_statement();
    case_armed_in_dwell();
    case_limit();
    case_pause();
    case_cycle_step();
    case_stop();
    if (plc_get_state() == PLC_STATE_RUNNING) cmd("STOP");
    wait_for([] { return !plc_state_is_transitioning(); }, 3000);
    unlink(g_sock);
    if (g_failures == 0)
    {
        std::printf("  all cases passed\n");
        std::fflush(stdout);
        _exit(0);
    }
    std::printf("  %d check(s) failed\n", g_failures);
    std::fflush(stdout);
    _exit(1);
}
