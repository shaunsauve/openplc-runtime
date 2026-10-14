// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file debug_link.cpp
 * @brief The core link and the agent thread (ADR-003 D1.3, D9; ADR-001 D9 L2).
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
 *   docs/DEBUG_PROTOCOL.md - the message list (§ Core link).
 */

#include "debug_link.h"

#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
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
#include "plc_state_manager.h"

extern "C" {
#include "../drivers/plugins/native/cjson/cJSON.h"
#include "unix_socket.h"
#include "utils/log.h"
}

namespace {

const char *kProtocol = "1.0";
const int   kTickMs   = 10;
const int   kBeatMs   = 250;

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
std::mutex  g_mu;
std::string g_by;
bool        g_pausing = false; /* a pause or all-stop wants every task halted */
TaskView    g_view[DBG_MAX_TASKS];
std::string g_last_run_state;
dbg_counters_t g_last_counters{};

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

/* Sends one envelope to the connected gateway; dropped when none is connected. */
void send_frame(cJSON *msg)
{
    char *text = cJSON_PrintUnformatted(msg);
    if (!text) return;
    const size_t n  = std::strlen(text);
    uint32_t     be = htonl((uint32_t)n);
    {
        std::lock_guard<std::mutex> g(g_send_mu);
        if (g_client_fd >= 0)
            (void)(write_all(g_client_fd, (const char *)&be, 4) && write_all(g_client_fd, text, n));
    }
    cJSON_free(text);
}

void send_event(const char *name, cJSON *body)
{
    cJSON *e = cJSON_CreateObject();
    cJSON_AddStringToObject(e, "type", "event");
    cJSON_AddNumberToObject(e, "seq", ++g_out_seq);
    cJSON_AddStringToObject(e, "event", name);
    cJSON_AddItemToObject(e, "body", body ? body : cJSON_CreateObject());
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
    if (s == PLC_STATE_RUNNING)
    {
        char   names[DBG_MAX_TASKS][32];
        size_t n = task_count(names);
        for (size_t i = 0; i < n; ++i)
        {
            int64_t   t_since = 0;
            const long st     = plc_task_run_state(i, &t_since);
            cJSON     *t      = cJSON_CreateObject();
            cJSON_AddNumberToObject(t, "task", (double)i);
            cJSON_AddStringToObject(t, "name", names[i]);
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
        cJSON_AddStringToObject(b, "by", g_by.c_str());
        cJSON *loc = cJSON_CreateObject();
        cJSON_AddNumberToObject(loc, "task", loc_task);
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

/* One agent pass: stops, boundary holds, resumes, counters, run state. */
void agent_pass(void)
{
    std::lock_guard<std::mutex> g(g_mu);
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
            cJSON *b      = cJSON_CreateObject();
            cJSON_AddStringToObject(b, "reason", reason_name(s.reason));
            cJSON_AddNumberToObject(b, "task", (double)i);
            cJSON_AddStringToObject(b, "taskName", names[i]);
            cJSON_AddNumberToObject(b, "probe", s.probe);
            cJSON_AddNumberToObject(b, "depth", s.depth);
            if (s.reason == DBG_STOP_BREAKPOINT)
            {
                cJSON *ids = cJSON_CreateArray();
                cJSON_AddItemToArray(ids, cJSON_CreateNumber(s.probe));
                cJSON_AddItemToObject(b, "hitBreakpointIds", ids);
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
            cJSON *b      = cJSON_CreateObject();
            cJSON_AddStringToObject(b, "reason", "pause");
            cJSON_AddNumberToObject(b, "task", (double)i);
            cJSON_AddStringToObject(b, "taskName", names[i]);
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
// Requests
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
    default: return nullptr;
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

int arg_int(cJSON *args, const char *key, int dflt)
{
    cJSON *v = args ? cJSON_GetObjectItem(args, key) : nullptr;
    return v && cJSON_IsNumber(v) ? v->valueint : dflt;
}

dbg_status_t set_one(cJSON *bp, std::string *why)
{
    cJSON *p = cJSON_GetObjectItem(bp, "probe");
    if (!p || !cJSON_IsNumber(p) || p->valuedouble < 0)
    {
        *why = "probe must be a non-negative integer";
        return DBG_E_INVALID_LOCATION;
    }
    std::vector<uint8_t> cond;
    cJSON *c = cJSON_GetObjectItem(bp, "condition");
    if (c && (!cJSON_IsString(c) || !b64_decode(c->valuestring, &cond)))
    {
        *why = "condition must be base64 bytecode of at most 512 bytes";
        return DBG_E_CONDITION;
    }
    const dbg_status_t s = debug_agent_set_breakpoint((uint32_t)p->valuedouble, cond.data(), cond.size(),
                                                      (uint32_t)arg_int(bp, "hitCount", 0));
    if (s == DBG_E_LIMIT) *why = "the runtime's limit of 1024 breakpoints is reached";
    else if (s == DBG_E_INVALID_LOCATION) *why = "no such probe in the running program";
    else if (s == DBG_E_NO_PROBES) *why = "the running program has no probes";
    else if (s == DBG_E_CONDITION) *why = "the condition failed verification";
    return s;
}

/* Task argument: absent means every task. */
bool task_arg(cJSON *args, size_t *task, bool *all)
{
    cJSON *t = args ? cJSON_GetObjectItem(args, "task") : nullptr;
    *all     = !t;
    *task    = t && cJSON_IsNumber(t) ? (size_t)t->valueint : PLC_ALL_TASKS;
    char names[DBG_MAX_TASKS][32];
    return *all || (cJSON_IsNumber(t) && *task < task_count(names));
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

Reply handle(const char *command, cJSON *args)
{
    const bool running = plc_get_state() == PLC_STATE_RUNNING;
    std::lock_guard<std::mutex> g(g_mu);

    if (!std::strcmp(command, "initialize"))
    {
        cJSON *b = cJSON_CreateObject();
        cJSON_AddStringToObject(b, "protocol", kProtocol);
        cJSON_AddNumberToObject(b, "engineAbi", STRUCPP_DBG_ABI_VERSION_SUPPORTED);
        cJSON_AddNumberToObject(b, "probes", debug_agent_probe_count());
        cJSON_AddNumberToObject(b, "maxBreakpoints", DBG_MAX_BREAKPOINTS);
        cJSON *caps = cJSON_CreateObject();
        cJSON_AddBoolToObject(caps, "probes", debug_agent_probe_count() > 0);
        cJSON_AddNumberToObject(caps, "maxBreakpoints", DBG_MAX_BREAKPOINTS);
        cJSON_AddBoolToObject(caps, "hitCount", true);
        cJSON_AddBoolToObject(caps, "conditional", false);
        cJSON_AddBoolToObject(caps, "control", true);
        cJSON_AddItemToObject(b, "capabilities", caps);
        return success(b);
    }
    if (!std::strcmp(command, "attach"))
    {
        cJSON *by = args ? cJSON_GetObjectItem(args, "by") : nullptr;
        g_by      = by && cJSON_IsString(by) ? by->valuestring : "";
        debug_agent_set_attached(true);
        return success();
    }
    if (!std::strcmp(command, "detach"))
    {
        debug_agent_clear_all_breakpoints();
        debug_agent_set_attached(false);
        if (running) resume_all_locked();
        g_by.clear();
        return success();
    }
    if (!std::strcmp(command, "status")) return success(run_state_locked());

    const bool breakpoints = !std::strcmp(command, "setBreakpoint") ||
                             !std::strcmp(command, "setBreakpoints") ||
                             !std::strcmp(command, "clearBreakpoint") ||
                             !std::strcmp(command, "clearAllBreakpoints");
    if (breakpoints && debug_agent_probe_count() == 0)
        return fail("NO_PROBES", "the running program was built without probes");
    if (!std::strcmp(command, "setBreakpoint"))
    {
        std::string        why;
        const dbg_status_t s = set_one(args, &why);
        if (s != DBG_OK) return fail(status_code(s), why);
        cJSON *b = cJSON_CreateObject();
        cJSON_AddNumberToObject(b, "probe", cJSON_GetObjectItem(args, "probe")->valuedouble);
        cJSON_AddBoolToObject(b, "verified", true);
        return success(b);
    }
    if (!std::strcmp(command, "setBreakpoints"))
    {
        cJSON *list = args ? cJSON_GetObjectItem(args, "breakpoints") : nullptr;
        if (!cJSON_IsArray(list)) return fail("BAD_REQUEST", "breakpoints must be an array");
        cJSON *out = cJSON_CreateArray();
        cJSON *bp;
        cJSON_ArrayForEach(bp, list)
        {
            std::string        why;
            const dbg_status_t s = set_one(bp, &why);
            cJSON             *o = cJSON_CreateObject();
            cJSON             *p = cJSON_GetObjectItem(bp, "probe");
            if (p) cJSON_AddItemToObject(o, "probe", cJSON_Duplicate(p, 1));
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
    if (!std::strcmp(command, "clearBreakpoint"))
    {
        const dbg_status_t s = debug_agent_clear_breakpoint((uint32_t)arg_int(args, "probe", -1));
        return s == DBG_OK ? success() : fail(status_code(s), "no such probe");
    }
    if (!std::strcmp(command, "clearAllBreakpoints"))
    {
        cJSON *b = cJSON_CreateObject();
        cJSON_AddNumberToObject(b, "removed", debug_agent_clear_all_breakpoints());
        return success(b);
    }

    const bool control = !std::strcmp(command, "pause") || !std::strcmp(command, "continue") ||
                         !std::strcmp(command, "next") || !std::strcmp(command, "stepIn") ||
                         !std::strcmp(command, "stepOut") || !std::strcmp(command, "stop");
    if (!control) return fail("BAD_REQUEST", std::string("unknown command ") + command);
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
    if (!debug_agent_attached()) return fail("BAD_REQUEST", "no session is attached");

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
    if (!std::strcmp(command, "continue")) return resume_all_locked();

    /* Steps: the task must be halted; only it resumes (DAP singleThread). */
    if (all) return fail("BAD_REQUEST", "a step needs a task");
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
    cJSON *b = cJSON_CreateObject();
    cJSON_AddNumberToObject(b, "task", (double)task);
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
        /* One gateway at a time; a new connection replaces the old one. */
        {
            std::lock_guard<std::mutex> g(g_send_mu);
            g_client_fd = fd;
        }
        log_info("[debug] core link connected");
        {
            std::lock_guard<std::mutex> g(g_mu);
            g_last_run_state.clear(); /* the new peer gets the current run state at once */
        }
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

extern "C" void debug_link_handle_request(const char *json, char *out, size_t out_size)
{
    cJSON *resp = respond(json);
    if (!cJSON_PrintPreallocated(resp, out, (int)out_size, 0) && out_size)
        std::snprintf(out, out_size, "{\"type\":\"response\",\"success\":false,"
                                     "\"error\":{\"code\":\"INTERNAL\",\"message\":\"response too large\"}}");
    cJSON_Delete(resp);
}
