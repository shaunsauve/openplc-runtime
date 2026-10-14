// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file debug_link.cpp
 * @brief The core link and the agent thread (ADR-003 D1.3, D9; ADR-001 D9 L2).
 *
 * The core end of docs/DEBUG_CORE_LINK.md: `hello` first on every connection and again when the
 * loaded program changes, then requests and events in the shapes of that file's linkspec block.
 *
 * The agent thread wakes on every park (debug_agent_wake) and every 10 ms. It turns stop records
 * into `stopped` events, applies the halt policy, reclaims retired breakpoint records, reports
 * condition errors and lock-skipped hits as `output` events, pushes `runState` on every change
 * and a `beat` every 250 ms. Requests arrive on the socket thread (or the legacy command socket)
 * and run synchronously there; neither thread is a scan thread, so JSON and allocation are fine.
 *
 * See also:
 *   core/src/plc_app/debug_agent.cpp - the table, slow path and trap these requests drive.
 *   core/src/plc_app/plc_state_manager.cpp - pause, resume and the per-task run state.
 *   docs/DEBUG_CORE_LINK.md - the message list.
 */

#include "debug_link.h"

#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
#include <strings.h>
#include <ctime>
#include <map>
#include <mutex>
#include <string>
#include <vector>

#include <arpa/inet.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include "../lib/strucpp_abi.hpp"
#include "debug_agent.h"
#include "image_tables.h"
#include "plc_state_manager.h"

extern "C" {
#include "../drivers/plugins/native/cjson/cJSON.h"
#include "unix_socket.h"
#include "utils/log.h"
}

namespace {

const char *kLink   = "1.0";
/* readLeaves: leaves per request, and the widest leaf (a WSTRING window is 253 bytes). */
const int   kMaxLeaves   = 1024;
const int   kMaxLeafSize = 512;
const int   kTickMs = 10;
const int   kBeatMs = 250;

std::mutex       g_send_mu;
int              g_client_fd = -1;
std::atomic<int> g_out_seq{0};
int              g_listen_fd = -1;

/* Session and halt view; under g_mu. */
struct TaskView
{
    bool       halted = false; /* a stopped event was sent for the current halt */
    bool       at_boundary = false;
    dbg_stop_t stop{};
};
struct Session
{
    std::string user;
    bool        controller = false;
};
/* A breakpoint the link armed: its DAP id and the file whose setBreakpoints owns it. */
struct Bp
{
    int id     = 0;
    int fileId = 0;
};
std::mutex                     g_mu;
std::map<std::string, Session> g_sessions;
int                            g_next_session = 1;
std::map<uint32_t, Bp>         g_bps; /* by probe */
int                            g_next_bp = 1;
long                           g_last_stop_task = -1; /* the task of the most recent stop */
bool        g_pausing = false; /* a pause or all-stop wants every task halted */
TaskView    g_view[DBG_MAX_TASKS];
std::string g_last_run_state;
dbg_counters_t g_last_counters{};
/* The loaded program's identity (debug_link_set_image) and the last hello pushed. */
bool        g_image_loaded = false;
std::string g_image_md5, g_image_build_id;
std::string g_last_hello;

int64_t mono_ns(void)
{
    timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000000000LL + t.tv_nsec;
}

/* A CLOCK_MONOTONIC instant as wall-clock epoch ms, for `since`. */
double wall_ms_of(int64_t mono)
{
    timespec w;
    clock_gettime(CLOCK_REALTIME, &w);
    const int64_t wall = (int64_t)w.tv_sec * 1000000000LL + w.tv_nsec;
    return (double)(wall - (mono_ns() - mono)) / 1e6;
}

bool write_all(int fd, const char *p, size_t n)
{
    while (n)
    {
        ssize_t w = send(fd, p, n, MSG_NOSIGNAL);
        if (w < 0 && errno == EINTR) continue;
        if (w <= 0) return false;
        p += w;
        n -= (size_t)w;
    }
    return true;
}

/* Writes one envelope to fd; the caller holds g_send_mu. */
void write_frame_locked(int fd, cJSON *msg)
{
    char *text = cJSON_PrintUnformatted(msg);
    if (!text) return;
    const size_t n  = std::strlen(text);
    uint32_t     be = htonl((uint32_t)n);
    if (fd >= 0) (void)(write_all(fd, (const char *)&be, 4) && write_all(fd, text, n));
    cJSON_free(text);
}

/* Sends one envelope to the connected gateway; dropped when none is connected. */
void send_frame(cJSON *msg)
{
    std::lock_guard<std::mutex> g(g_send_mu);
    write_frame_locked(g_client_fd, msg);
}

cJSON *event_envelope(const char *name, cJSON *body)
{
    cJSON *e = cJSON_CreateObject();
    cJSON_AddStringToObject(e, "type", "event");
    cJSON_AddNumberToObject(e, "seq", ++g_out_seq);
    cJSON_AddStringToObject(e, "event", name);
    cJSON_AddItemToObject(e, "body", body ? body : cJSON_CreateObject());
    return e;
}

void send_event(const char *name, cJSON *body)
{
    cJSON *e = event_envelope(name, body);
    send_frame(e);
    cJSON_Delete(e);
}

const char *reason_name(dbg_stop_reason_t r)
{
    switch (r)
    {
    case DBG_STOP_BREAKPOINT: return "breakpoint";
    case DBG_STOP_PAUSE: return "pause";
    case DBG_STOP_STEP: return "step";
    default: return "pause";
    }
}

const char *task_state_name(long st)
{
    switch (st)
    {
    case PLC_TASK_RUN_RUNNING: return "running";
    case PLC_TASK_RUN_HELD: return "held";
    case PLC_TASK_RUN_PARKED: return "parked";
    case PLC_TASK_RUN_ABANDONING: return "abandoning";
    default: return "unknown";
    }
}

size_t task_count(char names[][32])
{
    size_t n = plc_task_names(names, DBG_MAX_TASKS);
    return n < DBG_MAX_TASKS ? n : DBG_MAX_TASKS;
}

/* pm ruling 1: a task is carried as its name (`task`) and its index (`taskId`). */
void add_task(cJSON *o, size_t i, const char *name)
{
    cJSON_AddStringToObject(o, "task", name);
    cJSON_AddNumberToObject(o, "taskId", (double)i);
}

/* The controller's user, shown as `by` in halted states. Under g_mu. */
std::string by_locked(void)
{
    for (auto &s : g_sessions)
        if (s.second.controller) return s.second.user;
    return g_sessions.empty() ? std::string() : g_sessions.begin()->second.user;
}

cJSON *str_or_null(const std::string &s, bool present)
{
    return present ? cJSON_CreateString(s.c_str()) : cJSON_CreateNull();
}

/* The `hello` body (pm ruling 2). Under g_mu. */
cJSON *hello_locked(void)
{
    const uint32_t probes = debug_agent_probe_count();
    cJSON         *b      = cJSON_CreateObject();
    cJSON_AddStringToObject(b, "link", kLink);
    cJSON_AddNumberToObject(b, "engineAbi", STRUCPP_DBG_ABI_VERSION_SUPPORTED);
    cJSON *caps = cJSON_CreateObject();
    cJSON_AddBoolToObject(caps, "probes", probes > 0);
    cJSON_AddNumberToObject(caps, "maxBreakpoints", DBG_MAX_BREAKPOINTS);
    cJSON_AddBoolToObject(caps, "hitCount", true);
    cJSON_AddBoolToObject(caps, "conditional", false);
    cJSON_AddBoolToObject(caps, "control", true);
    cJSON_AddItemToObject(b, "capabilities", caps);
    cJSON_AddNumberToObject(b, "probes", probes);
    cJSON *img = cJSON_CreateObject();
    cJSON_AddItemToObject(img, "md5", str_or_null(g_image_md5, g_image_loaded && !g_image_md5.empty()));
    cJSON_AddItemToObject(img, "buildId",
                          str_or_null(g_image_build_id, g_image_loaded && !g_image_build_id.empty()));
    cJSON_AddItemToObject(img, "buildTime", cJSON_CreateNull());
    cJSON_AddItemToObject(img, "sourceHash", cJSON_CreateNull());
    cJSON_AddItemToObject(b, "image", img);
    return b;
}

/* Pushes hello when the image or probe count changed since the last one. Under g_mu. */
void push_hello_locked(void)
{
    cJSON *b    = hello_locked();
    char  *text = cJSON_PrintUnformatted(b);
    if (text && g_last_hello != text)
    {
        g_last_hello = text;
        send_event("hello", b);
        b = nullptr;
    }
    cJSON_free(text);
    cJSON_Delete(b);
}

/* The run state F040 names: the PLC state, refined by the tasks' halts. Under g_mu. */
cJSON *run_state_locked(void)
{
    cJSON         *b = cJSON_CreateObject();
    const PLCState s = plc_get_state();
    const char    *state;
    switch (s)
    {
    case PLC_STATE_RUNNING: state = "RUNNING"; break;
    case PLC_STATE_STOPPED: state = "STOPPED"; break;
    case PLC_STATE_ERROR: state = "ERROR"; break;
    case PLC_STATE_EMPTY: state = "EMPTY"; break;
    case PLC_STATE_INIT: state = "INIT"; break;
    case PLC_STATE_SAFE_STOPPED: state = "SAFE_STOPPED"; break;
    default: state = "TRANSITIONING"; break;
    }
    cJSON *tasks = cJSON_CreateArray();
    bool   any_bp = false, any_halt = false;
    int64_t since = 0;
    int     loc_task = -1;
    char    names[DBG_MAX_TASKS][32];
    if (s == PLC_STATE_RUNNING)
    {
        size_t n = task_count(names);
        for (size_t i = 0; i < n; ++i)
        {
            int64_t   t_since = 0;
            const long st     = plc_task_run_state(i, &t_since);
            cJSON     *t      = cJSON_CreateObject();
            add_task(t, i, names[i]);
            cJSON_AddStringToObject(t, "state", task_state_name(st));
            if (st != PLC_TASK_RUN_RUNNING)
            {
                any_halt = true;
                const TaskView &v = g_view[i];
                const bool bp = v.halted && !v.at_boundary && v.stop.reason == DBG_STOP_BREAKPOINT;
                any_bp |= bp;
                cJSON_AddStringToObject(t, "reason", v.halted && !v.at_boundary ? reason_name(v.stop.reason) : "pause");
                if (v.halted && !v.at_boundary)
                    cJSON_AddNumberToObject(t, "probe", v.stop.probe);
                else
                    cJSON_AddBoolToObject(t, "heldAtBoundary", true);
                cJSON_AddNumberToObject(t, "since", wall_ms_of(t_since));
                if (since == 0 || t_since < since) since = t_since;
                if (loc_task < 0 || bp) loc_task = (int)i;
            }
            cJSON_AddItemToArray(tasks, t);
        }
        if (any_bp) state = "HALTED_AT_BP";
        else if (any_halt) state = "PAUSED";
    }
    cJSON_AddStringToObject(b, "state", state);
    cJSON_AddItemToObject(b, "tasks", tasks);
    if (any_halt)
    {
        cJSON_AddStringToObject(b, "by", by_locked().c_str());
        cJSON *loc = cJSON_CreateObject();
        add_task(loc, (size_t)loc_task, names[loc_task]);
        const TaskView &v = g_view[loc_task];
        if (v.halted && !v.at_boundary) cJSON_AddNumberToObject(loc, "probe", v.stop.probe);
        cJSON_AddItemToObject(b, "location", loc);
        cJSON_AddNumberToObject(b, "since", wall_ms_of(since));
    }
    cJSON_AddBoolToObject(b, "attached", debug_agent_attached());
    return b;
}

/* Pushes runState when it differs from the last one pushed. Under g_mu. */
void push_run_state_locked(void)
{
    cJSON *b    = run_state_locked();
    char  *text = cJSON_PrintUnformatted(b);
    if (text && g_last_run_state != text)
    {
        g_last_run_state = text;
        send_event("runState", b);
        b = nullptr;
    }
    cJSON_free(text);
    cJSON_Delete(b);
}

/* Starts an all-stop: every other task halts at its next probe or boundary. Under g_mu. */
void all_stop_locked(void)
{
    if (g_pausing) return;
    g_pausing = true;
    debug_agent_set_trap(DBG_TRAP_PAUSE, DBG_TASK_ANY, 0);
    plc_run_control_pause(PLC_ALL_TASKS);
}

/* One agent pass: hello, stops, boundary holds, resumes, counters, run state. */
void agent_pass(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    push_hello_locked();
    char   names[DBG_MAX_TASKS][32];
    size_t n = plc_get_state() == PLC_STATE_RUNNING ? task_count(names) : 0;
    for (size_t i = 0; i < n; ++i)
    {
        TaskView  &v = g_view[i];
        dbg_stop_t s;
        if (debug_agent_take_stop(i, &s))
        {
            v.halted      = true;
            v.at_boundary = false;
            v.stop        = s;
            g_last_stop_task = (long)i;
            cJSON *b      = cJSON_CreateObject();
            cJSON_AddStringToObject(b, "reason", reason_name(s.reason));
            add_task(b, i, names[i]);
            cJSON_AddNumberToObject(b, "probe", s.probe);
            cJSON_AddNumberToObject(b, "depth", s.depth);
            if (s.reason == DBG_STOP_BREAKPOINT)
            {
                /* pm ruling 7: breakpoint ids, as setBreakpoints returned them. */
                cJSON *ids = cJSON_CreateArray();
                auto   it  = g_bps.find(s.probe);
                if (it != g_bps.end()) cJSON_AddItemToArray(ids, cJSON_CreateNumber(it->second.id));
                cJSON_AddItemToObject(b, "breakpointIds", ids);
                cJSON_AddNumberToObject(b, "hits", s.hits);
            }
            cJSON_AddNumberToObject(b, "since", wall_ms_of(s.since_ns));
            send_event("stopped", b);
            plc_run_control_halted(i);
            /* D4.11: a step that landed clears the trap; a breakpoint starts the all-stop. */
            if (s.reason == DBG_STOP_STEP) debug_agent_clear_trap();
            if (s.reason == DBG_STOP_BREAKPOINT) all_stop_locked();
            continue;
        }
        int64_t    since = 0;
        const long st    = plc_task_run_state(i, &since);
        if (st == PLC_TASK_RUN_HELD && !v.halted && g_pausing)
        {
            v.halted      = true;
            v.at_boundary = true;
            if (g_last_stop_task < 0) g_last_stop_task = (long)i;
            cJSON *b      = cJSON_CreateObject();
            cJSON_AddStringToObject(b, "reason", "pause");
            add_task(b, i, names[i]);
            cJSON_AddBoolToObject(b, "heldAtBoundary", true);
            cJSON_AddNumberToObject(b, "since", wall_ms_of(since));
            send_event("stopped", b);
            plc_run_control_halted(i);
        }
        else if (st == PLC_TASK_RUN_RUNNING && v.halted)
        {
            v = TaskView{};
        }
    }
    if (n == 0)
    {
        for (auto &v : g_view) v = TaskView{};
        g_pausing = false;
        g_last_stop_task = -1;
    }

    debug_agent_reclaim();
    dbg_counters_t c;
    debug_agent_counters(&c);
    if (c.condition_errors != g_last_counters.condition_errors)
    {
        cJSON *b = cJSON_CreateObject();
        cJSON_AddStringToObject(b, "category", "runtime");
        cJSON_AddStringToObject(b, "kind", "conditionError");
        cJSON_AddNumberToObject(b, "count", (double)(c.condition_errors - g_last_counters.condition_errors));
        send_event("output", b);
    }
    if (c.skipped_locked != g_last_counters.skipped_locked)
    {
        cJSON *b = cJSON_CreateObject();
        cJSON_AddStringToObject(b, "category", "runtime");
        cJSON_AddStringToObject(b, "kind", "breakpointSkippedLocked");
        cJSON_AddNumberToObject(b, "count", (double)(c.skipped_locked - g_last_counters.skipped_locked));
        send_event("output", b);
    }
    g_last_counters = c;
    push_run_state_locked();
}

void *agent_thread(void *)
{
    pthread_setname_np(pthread_self(), "plc_dbg_agent");
    int64_t next_beat = mono_ns();
    for (;;)
    {
        debug_agent_wait(kTickMs);
        agent_pass();
        const int64_t now = mono_ns();
        if (now >= next_beat)
        {
            cJSON *b = cJSON_CreateObject();
            cJSON_AddNumberToObject(b, "t", wall_ms_of(now));
            send_event("beat", b);
            next_beat = now + (int64_t)kBeatMs * 1000000LL;
        }
    }
    return nullptr;
}

// ---------------------------------------------------------------------------
// Requests (docs/DEBUG_CORE_LINK.md, "Messages")
// ---------------------------------------------------------------------------

struct Reply
{
    cJSON      *body  = nullptr;
    const char *code  = nullptr; /* error code, or null on success */
    std::string message;
};

Reply fail(const char *code, const std::string &msg)
{
    Reply r;
    r.code    = code;
    r.message = msg;
    return r;
}

Reply success(cJSON *body = nullptr)
{
    Reply r;
    r.body = body ? body : cJSON_CreateObject();
    return r;
}

const char *status_code(dbg_status_t s)
{
    switch (s)
    {
    case DBG_E_NO_PROBES: return "NO_PROBES";
    case DBG_E_LIMIT: return "LIMIT_EXCEEDED";
    case DBG_E_INVALID_LOCATION: return "INVALID_LOCATION";
    case DBG_E_CONDITION: return "BAD_REQUEST";
    default: return "INTERNAL";
    }
}

bool b64_decode(const char *in, std::vector<uint8_t> *out)
{
    static const std::string k = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    uint32_t acc = 0;
    int      bits = 0;
    for (const char *p = in; *p; ++p)
    {
        if (*p == '=') break;
        const size_t v = k.find(*p);
        if (v == std::string::npos) return false;
        acc = acc << 6 | (uint32_t)v;
        bits += 6;
        if (bits >= 8)
        {
            bits -= 8;
            out->push_back((uint8_t)(acc >> bits));
        }
    }
    return out->size() <= DBG_COND_MAX;
}

cJSON *arg(cJSON *args, const char *key)
{
    return args && cJSON_IsObject(args) ? cJSON_GetObjectItem(args, key) : nullptr;
}

bool is_int(cJSON *v)
{
    return v && cJSON_IsNumber(v) && v->valuedouble == (double)(int64_t)v->valuedouble;
}

/* One setBreakpoints item: a probe ID, or {probe, condition?, hitCount?} (pm ruling 6). */
dbg_status_t set_one(cJSON *item, uint32_t *probe, std::string *why)
{
    cJSON *p = cJSON_IsObject(item) ? cJSON_GetObjectItem(item, "probe") : item;
    if (!is_int(p) || p->valuedouble < 0)
    {
        *why = "a breakpoint's probe must be a non-negative integer";
        *probe = 0;
        return DBG_E_INVALID_LOCATION;
    }
    *probe = (uint32_t)p->valuedouble;
    std::vector<uint8_t> cond;
    cJSON *c = cJSON_IsObject(item) ? cJSON_GetObjectItem(item, "condition") : nullptr;
    if (c && (!cJSON_IsString(c) || !b64_decode(c->valuestring, &cond)))
    {
        *why = "condition must be base64 bytecode of at most 512 bytes";
        return DBG_E_CONDITION;
    }
    cJSON *h = cJSON_IsObject(item) ? cJSON_GetObjectItem(item, "hitCount") : nullptr;
    const dbg_status_t s = debug_agent_set_breakpoint(*probe, cond.data(), cond.size(),
                                                      is_int(h) && h->valuedouble > 0 ? (uint32_t)h->valuedouble : 0);
    if (s == DBG_E_LIMIT) *why = "the runtime's limit of 1024 breakpoints is reached";
    else if (s == DBG_E_INVALID_LOCATION) *why = "no such probe in the running program";
    else if (s == DBG_E_NO_PROBES) *why = "the running program has no probes";
    else if (s == DBG_E_CONDITION) *why = "the condition failed verification";
    return s;
}

/* The `task` argument: a name or an index (pm ruling 1). Absent: *all. */
bool task_arg(cJSON *args, size_t *task, bool *all)
{
    cJSON *t = arg(args, "task");
    *all     = !t;
    *task    = PLC_ALL_TASKS;
    if (!t) return true;
    char         names[DBG_MAX_TASKS][32];
    const size_t n = task_count(names);
    if (is_int(t) && t->valuedouble >= 0 && (size_t)t->valuedouble < n)
    {
        *task = (size_t)t->valuedouble;
        return true;
    }
    if (cJSON_IsString(t))
        for (size_t i = 0; i < n; ++i)
            if (strcasecmp(names[i], t->valuestring) == 0)
            {
                *task = i;
                return true;
            }
    return false;
}

Reply resume_all_locked(void)
{
    g_pausing = false;
    debug_agent_clear_trap();
    const bool ok = plc_run_control_resume(PLC_ALL_TASKS, 2000);
    for (auto &v : g_view) v = TaskView{};
    cJSON *b = cJSON_CreateObject();
    cJSON_AddBoolToObject(b, "allThreadsContinued", true);
    send_event("continued", cJSON_Duplicate(b, 1));
    return ok ? success(b) : (cJSON_Delete(b), fail("INTERNAL", "the dispatcher did not resume every task"));
}

void clear_breakpoints_locked(void)
{
    debug_agent_clear_all_breakpoints();
    g_bps.clear();
}

/* Ends one session (detach, terminate). Under g_mu. */
void end_session_locked(const std::string &id, bool running)
{
    auto it = g_sessions.find(id);
    if (it == g_sessions.end()) return;
    const bool controller = it->second.controller;
    g_sessions.erase(it);
    if (g_sessions.empty()) debug_agent_set_attached(false);
    if (controller || g_sessions.empty())
    {
        clear_breakpoints_locked();
        if (running)
        {
            Reply r = resume_all_locked();
            cJSON_Delete(r.body);
        }
    }
}

/* The parked task's frames (ADR-001 D3), innermost first. A task held at its scan boundary has
 * reached no probe: an empty stack (D2.5). Under g_mu, so no step or continue can resume the task
 * during the walk. */
Reply stack_trace_locked(size_t task)
{
    const TaskView &v = g_view[task];
    if (plc_task_run_state(task, nullptr) == PLC_TASK_RUN_RUNNING || !v.halted)
        return fail("NOT_HALTED", "the task is not halted");
    dbg_frame_t frames[256];
    bool        truncated = false;
    const size_t n = v.at_boundary ? 0 : debug_agent_frames(&v.stop, frames, 256, &truncated);
    char names[DBG_MAX_TASKS][32];
    task_count(names);
    cJSON *b = cJSON_CreateObject();
    add_task(b, task, names[task]);
    cJSON *list = cJSON_CreateArray();
    for (size_t i = 0; i < n; ++i)
    {
        cJSON *f = cJSON_CreateObject();
        cJSON_AddNumberToObject(f, "pou", frames[i].pou);
        cJSON_AddNumberToObject(f, "at", frames[i].at);
        const long leaf = debug_agent_leaf_of(frames[i].self);
        if (leaf >= 0)
            cJSON_AddNumberToObject(f, "leaf", (double)leaf);
        else
            cJSON_AddItemToObject(f, "leaf", cJSON_CreateNull());
        cJSON_AddItemToArray(list, f);
    }
    cJSON_AddItemToObject(b, "frames", list);
    cJSON_AddBoolToObject(b, "truncated", truncated);
    return success(b);
}

/* Leaf values by flat leaf index (ADR-002 D3 "Addressing"), as the hex of the bytes
 * strucpp_debug_read writes; null for an index the program does not have. The gateway decodes
 * them by the leaf's IEC type. Read without stopping the program, as the legacy 0x44 read is: a
 * halted task's values are stable, a running task's are a snapshot. */
Reply read_leaves(cJSON *list)
{
    if (!cJSON_IsArray(list) || cJSON_GetArraySize(list) > kMaxLeaves)
        return fail("BAD_REQUEST", "readLeaves needs a list of at most 1024 leaf indexes");
    cJSON *item;
    cJSON_ArrayForEach(item, list)
    {
        if (!is_int(item) || item->valuedouble < 0)
            return fail("BAD_REQUEST", "a leaf index must be a non-negative integer");
    }
    std::vector<uint16_t> counts;
    if (ext_strucpp_debug_array_count && ext_strucpp_debug_elem_count && ext_strucpp_debug_read &&
        ext_strucpp_debug_size)
        for (uint8_t a = 0, n = ext_strucpp_debug_array_count(); a < n; ++a)
            counts.push_back(ext_strucpp_debug_elem_count(a));
    cJSON *values = cJSON_CreateArray();
    cJSON_ArrayForEach(item, list)
    {
        uint64_t idx = (uint64_t)item->valuedouble;
        int      arr = -1;
        for (size_t a = 0; a < counts.size(); ++a)
        {
            if (idx < counts[a])
            {
                arr = (int)a;
                break;
            }
            idx -= counts[a];
        }
        uint8_t        buf[kMaxLeafSize];
        const uint16_t size = arr < 0 ? 0 : ext_strucpp_debug_size((uint8_t)arr, (uint16_t)idx);
        const uint16_t got  = size == 0 || size > kMaxLeafSize
                                  ? 0
                                  : ext_strucpp_debug_read((uint8_t)arr, (uint16_t)idx, buf);
        if (got == 0)
        {
            cJSON_AddItemToArray(values, cJSON_CreateNull());
            continue;
        }
        std::string hex(got * 2u, '0');
        static const char kHex[] = "0123456789abcdef";
        for (uint16_t i = 0; i < got; ++i)
        {
            hex[2 * i]     = kHex[buf[i] >> 4];
            hex[2 * i + 1] = kHex[buf[i] & 0xF];
        }
        cJSON_AddItemToArray(values, cJSON_CreateString(hex.c_str()));
    }
    cJSON *b = cJSON_CreateObject();
    cJSON_AddItemToObject(b, "values", values);
    return success(b);
}

bool is_command(const char *c, std::initializer_list<const char *> names)
{
    for (const char *n : names)
        if (!std::strcmp(c, n)) return true;
    return false;
}

Reply handle(const char *command, cJSON *args)
{
    const bool running = plc_get_state() == PLC_STATE_RUNNING;
    std::lock_guard<std::mutex> g(g_mu);

    if (args && !cJSON_IsObject(args)) return fail("BAD_REQUEST", "arguments must be an object");
    if (!std::strcmp(command, "status")) return success(run_state_locked());
    if (!std::strcmp(command, "attach"))
    {
        cJSON *user = arg(args, "user"), *role = arg(args, "role");
        if (!cJSON_IsString(user) || !cJSON_IsString(role))
            return fail("BAD_REQUEST", "attach needs a user and a role");
        char id[24];
        std::snprintf(id, sizeof id, "s%d", g_next_session++);
        Session s;
        s.user       = user->valuestring;
        s.controller = !std::strcmp(role->valuestring, "controller");
        g_sessions[id] = s;
        debug_agent_set_attached(true);
        cJSON *b = cJSON_CreateObject();
        cJSON_AddStringToObject(b, "session", id);
        return success(b);
    }

    /* pm ruling 3: a command outside the link, or one this core has not built, is UNSUPPORTED. */
    const bool known = is_command(command, {"detach", "terminate", "beatClient", "setBreakpoints",
                                            "clearAllBreakpoints", "continue", "pause", "next",
                                            "stepIn", "stepOut", "stop", "stackTrace",
                                            "readLeaves"});
    if (!known)
        return fail("UNSUPPORTED", std::string("this runtime core does not implement ") + command);

    /* pm ruling 5: every request but attach and status names a session the core holds. */
    cJSON *sid = arg(args, "session");
    if (!cJSON_IsString(sid)) return fail("BAD_REQUEST", "the request needs a session");
    if (!g_sessions.count(sid->valuestring)) return fail("SESSION_ENDED", "no such session on the core");
    const std::string session = sid->valuestring;

    if (!std::strcmp(command, "beatClient")) return success();
    if (!std::strcmp(command, "detach"))
    {
        end_session_locked(session, running);
        cJSON *b = cJSON_CreateObject();
        cJSON_AddItemToObject(b, "forces", cJSON_CreateArray());
        return success(b);
    }
    if (!std::strcmp(command, "terminate"))
    {
        end_session_locked(session, running);
        if (running)
        {
            g_pausing = false;
            debug_agent_clear_trap();
            if (!plc_begin_transition(PLC_STATE_STOPPED)) return fail("BUSY", "a state change is in flight");
        }
        return success();
    }

    if (is_command(command, {"setBreakpoints", "clearAllBreakpoints"}) && debug_agent_probe_count() == 0)
        return fail("NO_PROBES", "the running program was built without probes");
    if (!std::strcmp(command, "setBreakpoints"))
    {
        cJSON *file = arg(args, "fileId"), *list = arg(args, "probes");
        if (!is_int(file) || !cJSON_IsArray(list))
            return fail("BAD_REQUEST", "setBreakpoints needs an integer fileId and a probes list");
        const int fid = (int)file->valuedouble;
        /* Replace the file's set: disarm what the new list no longer names (pm ruling 6). */
        std::map<uint32_t, bool> wanted;
        cJSON *item;
        cJSON_ArrayForEach(item, list)
        {
            cJSON *p = cJSON_IsObject(item) ? cJSON_GetObjectItem(item, "probe") : item;
            if (is_int(p) && p->valuedouble >= 0) wanted[(uint32_t)p->valuedouble] = true;
        }
        for (auto it = g_bps.begin(); it != g_bps.end();)
        {
            if (it->second.fileId == fid && !wanted.count(it->first))
            {
                debug_agent_clear_breakpoint(it->first);
                it = g_bps.erase(it);
            }
            else
                ++it;
        }
        cJSON *out = cJSON_CreateArray();
        cJSON_ArrayForEach(item, list)
        {
            std::string        why;
            uint32_t           probe;
            const dbg_status_t s = set_one(item, &probe, &why);
            cJSON             *o = cJSON_CreateObject();
            if (s == DBG_OK)
            {
                auto it = g_bps.find(probe);
                if (it == g_bps.end()) it = g_bps.emplace(probe, Bp{g_next_bp++, fid}).first;
                it->second.fileId = fid;
                cJSON_AddNumberToObject(o, "id", it->second.id);
            }
            else
            {
                cJSON_AddItemToObject(o, "id", cJSON_CreateNull());
            }
            cJSON_AddNumberToObject(o, "probe", probe);
            cJSON_AddBoolToObject(o, "verified", s == DBG_OK);
            if (s != DBG_OK)
            {
                cJSON *e = cJSON_CreateObject();
                cJSON_AddStringToObject(e, "code", status_code(s));
                cJSON_AddStringToObject(e, "message", why.c_str());
                cJSON_AddItemToObject(o, "error", e);
            }
            cJSON_AddItemToArray(out, o);
        }
        cJSON *b = cJSON_CreateObject();
        cJSON_AddItemToObject(b, "breakpoints", out);
        return success(b);
    }
    if (!std::strcmp(command, "clearAllBreakpoints"))
    {
        cJSON *b = cJSON_CreateObject();
        cJSON_AddNumberToObject(b, "removed", debug_agent_clear_all_breakpoints());
        g_bps.clear();
        return success(b);
    }

    if (!std::strcmp(command, "readLeaves")) return read_leaves(arg(args, "leaves"));

    if (!running) return fail("NOT_RUNNING", "no program is running");

    if (!std::strcmp(command, "stop"))
    {
        /* Stop the program, not the session (F005): the session stays attached. */
        g_pausing = false;
        debug_agent_clear_trap();
        if (!plc_begin_transition(PLC_STATE_STOPPED)) return fail("BUSY", "a state change is in flight");
        return success();
    }

    size_t task;
    bool   all;
    if (!task_arg(args, &task, &all)) return fail("BAD_REQUEST", "no such task");

    if (!std::strcmp(command, "pause"))
    {
        if (all)
        {
            all_stop_locked();
        }
        else
        {
            debug_agent_set_trap(DBG_TRAP_PAUSE, (uint32_t)task, 0);
            plc_run_control_pause(task);
        }
        return success();
    }
    if (!std::strcmp(command, "stackTrace"))
    {
        if (all)
        {
            if (g_last_stop_task < 0) return fail("NOT_HALTED", "no task is halted");
            task = (size_t)g_last_stop_task;
        }
        return stack_trace_locked(task);
    }
    /* pm ruling 8: continue is all-stop, whatever `task` says. */
    if (!std::strcmp(command, "continue")) return resume_all_locked();

    /* Steps: the task must be halted; only it resumes (DAP singleThread). Without a task, the
     * task of the most recent stop (pm ruling 8). */
    if (all)
    {
        if (g_last_stop_task < 0) return fail("NOT_HALTED", "no task is halted");
        task = (size_t)g_last_stop_task;
    }
    const TaskView &v = g_view[task];
    if (plc_task_run_state(task, nullptr) == PLC_TASK_RUN_RUNNING || !v.halted)
        return fail("NOT_HALTED", "the task is not halted");
    const uint32_t depth = v.at_boundary ? 0 : v.stop.depth;
    dbg_trap_kind_t kind = DBG_TRAP_STEP_IN;
    uint32_t        bound = 0;
    if (!std::strcmp(command, "next") && !v.at_boundary)
    {
        kind  = DBG_TRAP_STEP_OVER;
        bound = depth;
    }
    else if (!std::strcmp(command, "stepOut") && !v.at_boundary && depth > 1)
    {
        kind  = DBG_TRAP_STEP_OUT;
        bound = depth;
    }
    /* Stepping out of the outermost frame lands on the next scan's first probe (D4.5). */
    debug_agent_set_trap(kind, (uint32_t)task, bound);
    g_view[task] = TaskView{};
    if (!plc_run_control_resume(task, 2000)) return fail("INTERNAL", "the dispatcher did not resume the task");
    char names[DBG_MAX_TASKS][32];
    task_count(names);
    cJSON *b = cJSON_CreateObject();
    add_task(b, task, names[task]);
    send_event("continued", cJSON_Duplicate(b, 1));
    return success(b);
}

/* Parses one request envelope and builds its response envelope. */
cJSON *respond(const char *json)
{
    cJSON      *req  = cJSON_Parse(json);
    cJSON      *seq  = req ? cJSON_GetObjectItem(req, "seq") : nullptr;
    cJSON      *cmd  = req ? cJSON_GetObjectItem(req, "command") : nullptr;
    cJSON      *type = req ? cJSON_GetObjectItem(req, "type") : nullptr;
    Reply       r;
    if (!req || !cJSON_IsString(cmd) || !cJSON_IsString(type) || std::strcmp(type->valuestring, "request"))
        r = fail("BAD_REQUEST", "not a request envelope");
    else
        r = handle(cmd->valuestring, cJSON_GetObjectItem(req, "arguments"));

    cJSON *resp = cJSON_CreateObject();
    cJSON_AddStringToObject(resp, "type", "response");
    cJSON_AddNumberToObject(resp, "seq", ++g_out_seq);
    cJSON_AddNumberToObject(resp, "request_seq", seq && cJSON_IsNumber(seq) ? seq->valuedouble : 0);
    if (cJSON_IsString(cmd)) cJSON_AddStringToObject(resp, "command", cmd->valuestring);
    cJSON_AddBoolToObject(resp, "success", r.code == nullptr);
    if (r.code)
    {
        cJSON *e = cJSON_CreateObject();
        cJSON_AddStringToObject(e, "code", r.code);
        cJSON_AddStringToObject(e, "message", r.message.c_str());
        cJSON_AddItemToObject(resp, "error", e);
    }
    else
    {
        cJSON_AddItemToObject(resp, "body", r.body);
    }
    cJSON_Delete(req);
    return resp;
}

bool read_all(int fd, void *p, size_t n)
{
    char *c = static_cast<char *>(p);
    while (n)
    {
        ssize_t r = read(fd, c, n);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) return false;
        c += r;
        n -= (size_t)r;
    }
    return true;
}

void serve_client(int fd)
{
    for (;;)
    {
        uint32_t be;
        if (!read_all(fd, &be, 4)) return;
        const uint32_t n = ntohl(be);
        if (n > DEBUG_LINK_MAX_FRAME)
        {
            log_error("[debug] core-link frame of %u bytes refused", n);
            return;
        }
        std::string body(n, '\0');
        if (n && !read_all(fd, &body[0], n)) return;
        cJSON *resp = respond(body.c_str());
        send_frame(resp);
        cJSON_Delete(resp);
    }
}

void *server_thread(void *)
{
    pthread_setname_np(pthread_self(), "plc_dbg_link");
    for (;;)
    {
        int fd = accept(g_listen_fd, nullptr, nullptr);
        if (fd < 0)
        {
            if (errno != EINTR) sleep(1);
            continue;
        }
        /* One gateway at a time; a new connection replaces the old one. `hello` is its first
         * frame: it is written under g_send_mu in the same section that publishes the fd, so no
         * agent-thread event can precede it. */
        {
            std::lock_guard<std::mutex> g(g_mu);
            cJSON *hello = hello_locked();
            char  *text  = cJSON_PrintUnformatted(hello);
            g_last_hello = text ? text : "";
            cJSON_free(text);
            cJSON *e = event_envelope("hello", hello);
            {
                std::lock_guard<std::mutex> s(g_send_mu);
                g_client_fd = fd;
                write_frame_locked(fd, e);
            }
            cJSON_Delete(e);
            g_last_run_state.clear(); /* the new peer gets the current run state at once */
        }
        log_info("[debug] core link connected");
        debug_agent_wake();
        serve_client(fd);
        {
            std::lock_guard<std::mutex> g(g_send_mu);
            if (g_client_fd == fd) g_client_fd = -1;
        }
        close(fd);
        log_info("[debug] core link disconnected");
    }
    return nullptr;
}

}  // namespace

extern "C" int debug_link_start(const char *path)
{
    unlink(path);
    g_listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (g_listen_fd < 0) return -1;
    sockaddr_un a{};
    a.sun_family = AF_UNIX;
    std::snprintf(a.sun_path, sizeof a.sun_path, "%s", path);
    if (bind(g_listen_fd, (sockaddr *)&a, sizeof a) != 0 || listen(g_listen_fd, 1) != 0)
    {
        log_error("[debug] core link socket %s: %s", path, strerror(errno));
        close(g_listen_fd);
        g_listen_fd = -1;
        return -1;
    }
    pthread_t t;
    if (pthread_create(&t, nullptr, agent_thread, nullptr) != 0) return -1;
    pthread_detach(t);
    if (pthread_create(&t, nullptr, server_thread, nullptr) != 0) return -1;
    pthread_detach(t);
    log_info("[debug] core link listening at %s", path);
    return 0;
}

extern "C" void debug_link_set_image(const char *md5, const char *build_id)
{
    {
        std::lock_guard<std::mutex> g(g_mu);
        g_image_loaded = md5 != nullptr;
        /* strucpp_program_md5 is 32 hex characters with no terminator. */
        g_image_md5.assign(md5 ? md5 : "", md5 ? strnlen(md5, 32) : 0);
        g_image_build_id = build_id ? build_id : "";
    }
    debug_agent_wake();
}

extern "C" void debug_link_handle_request(const char *json, char *out, size_t out_size)
{
    cJSON *resp = respond(json);
    if (!cJSON_PrintPreallocated(resp, out, (int)out_size, 0) && out_size)
        std::snprintf(out, out_size, "{\"type\":\"response\",\"success\":false,"
                                     "\"error\":{\"code\":\"INTERNAL\",\"message\":\"response too large\"}}");
    cJSON_Delete(resp);
}
