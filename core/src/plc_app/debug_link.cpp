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
#include <cctype>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include "../lib/strucpp_abi.hpp"
#include "debug_agent.h"
#include "debug_force.h"
#include "debug_write_journal.h"
#include "image_tables.h"
#include "plc_output_hold.h"
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
/* ADR-005 D4.2: P2, the heartbeat timeout, when attach names none; and its allowed range. */
const double kP2DefaultS = 15, kP2MinS = 5, kP2MaxS = 600;

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
/* A cycle step in flight (ADR-001 D4.9). From probe P in scan k it runs in two legs: the rest of
 * scan k to a boundary hold, then one released scan with an AT_PROBE trap on P (a stop at P in
 * scan k+1, or a hold at the next boundary if that scan never reaches P). From a boundary it is
 * the second leg alone, ending in the hold. Each leg is one scan of the dispatcher's budget, so
 * virtual time moves exactly one interval (ADR-004 D4). */
struct Cycle
{
    bool     active    = false;
    bool     has_probe = false;
    bool     last_leg  = false; /* the released scan is running */
    uint32_t probe     = 0;
};
struct Session
{
    std::string user;
    bool        controller = false;
    /* ADR-005 D4.4: the core owns the P2 timer, so a gateway that dies still loses the session.
     * Any request naming the session (beatClient included) is a heartbeat. */
    int64_t     last_beat_ns = 0;
    int64_t     p2_ns        = 0;
};
/* One breakpoint as the link holds it: the id a client sees, the file whose setBreakpoints
 * owns it, and the kind it is. A probe (a line) may carry several, so the table is keyed by
 * probe and holds a list. */
struct Bp
{
    int id     = 0;
    int fileId = 0;
    int kind   = DBG_KIND_LINE;
};
std::mutex                     g_mu;
std::map<std::string, Session> g_sessions;
int                            g_next_session = 1;
std::map<uint32_t, std::vector<Bp>> g_bps; /* by probe */
/* The run-to-cursor's own breakpoint, while it waits for its stop: the probe it is at. It is
 * spent by the first stop, whatever caused it, so it leaves nothing behind. */
uint32_t g_cursor_probe = UINT32_MAX;
/* Where the PLC was when it safe-stopped, and who lost the session that held it. The core
 * holds the probe and the gateway resolves it to a file and a line (ADR-003 D1). */
struct SafeStop
{
    bool        valid   = false;
    std::string reason;
    std::string user;
    uint32_t    probe   = UINT32_MAX;
    long        task    = -1;
    int64_t     time_ms = 0;
};
SafeStop g_safe_stop;
int                            g_next_bp = 1;
long                           g_last_stop_task = -1; /* the task of the most recent stop */
bool        g_pausing = false; /* a pause or all-stop wants every task halted */
TaskView    g_view[DBG_MAX_TASKS];
Cycle       g_cycle[DBG_MAX_TASKS];
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
    /* F037's start-halted restart: parked before the first statement of the first scan. */
    case DBG_STOP_ENTRY: return "entry";
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

/* The build's identity. `hello` and the debug status body both report it (E45 ruling 1), from
 * one block built here: two copies would be free to disagree about which build is loaded, and
 * A217 reads this off the status API precisely to trust it. Under g_mu. */
void add_image(cJSON *b)
{
    cJSON *img = cJSON_CreateObject();
    cJSON_AddItemToObject(img, "md5", str_or_null(g_image_md5, g_image_loaded && !g_image_md5.empty()));
    cJSON_AddItemToObject(img, "buildId",
                          str_or_null(g_image_build_id, g_image_loaded && !g_image_build_id.empty()));
    cJSON_AddItemToObject(img, "buildTime", cJSON_CreateNull());
    cJSON_AddItemToObject(img, "sourceHash", cJSON_CreateNull());
    cJSON_AddItemToObject(b, "image", img);
}

/* The safe-stop, as the body carries it: why it happened, who lost the session that held it,
 * when, and where the program was. The core holds the probe and the gateway resolves it to a
 * file and a line (ADR-003 D1). */
cJSON *safe_stop_body(void)
{
    cJSON *ss = cJSON_CreateObject();
    cJSON_AddStringToObject(ss, "reason", g_safe_stop.reason.c_str());
    cJSON_AddStringToObject(ss, "user", g_safe_stop.user.c_str());
    cJSON_AddNumberToObject(ss, "time", (double)g_safe_stop.time_ms);
    if (g_safe_stop.probe != UINT32_MAX) cJSON_AddNumberToObject(ss, "probe", g_safe_stop.probe);
    if (g_safe_stop.task >= 0) cJSON_AddNumberToObject(ss, "taskId", g_safe_stop.task);
    return ss;
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
    cJSON_AddBoolToObject(caps, "cycleStep", true);
    /* ADR-005 D5: this core owns the force registry, so it serves force, unforce, unforceAll
     * and listForces, and gates them on the controller and the lock as the gateway does. */
    cJSON_AddBoolToObject(caps, "force", true);
    cJSON_AddBoolToObject(caps, "modify", true);
    /* F037: this core restarts the program and can arm the halt at its first statement. */
    cJSON_AddBoolToObject(caps, "startHalted", true);
    cJSON_AddItemToObject(b, "capabilities", caps);
    cJSON_AddNumberToObject(b, "probes", probes);
    add_image(b);
    /* The safe-stop, when there is one: the reason, who lost the session that held it, and
     * where the program was. The gateway resolves the probe to a file and a line. */
    if (g_safe_stop.valid) cJSON_AddItemToObject(b, "safeStop", safe_stop_body());
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
            /* A117 counts the overrun counter across twenty attaches and A217 watches it rise;
             * the figure is already tracked per task, so the status body carries it rather
             * than leaving the client to scrape STATS (E45 ruling 1). */
            const int64_t ov = plc_task_overruns(i);
            if (ov >= 0) cJSON_AddNumberToObject(t, "overruns", (double)ov);
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
    /* E45 ruling 1's `reason`, and the only one of the three that was already held. It is the
     * safe-stop reason plc_state_manager copied out of its g_safe_stop_reason when it entered
     * SAFE_STOPPED, read back through the getter rather than off that static: g_safe_stop_reason
     * is file-static behind state_mutex, which is not exported, while output_hold_reason()
     * snapshots the recorded string under the same PI mutex output_hold_set_reason() writes it
     * under. So a body carries the whole reason or the previous one, never half of one, and the
     * link holds no second copy of the fact. SAFE_STOPPED only: no other state has a safe stop to
     * explain, and the getter is cleared when the hold is released, so a stale reason cannot be
     * read as this stop's. Absent, not empty, when there is none. */
    if (s == PLC_STATE_SAFE_STOPPED)
    {
        char why[PLC_SAFE_STOP_REASON_LEN];
        output_hold_reason(why, sizeof why);
        if (why[0]) cJSON_AddStringToObject(b, "reason", why);
        /* Where the PLC was when it safe-stopped: the probe the halted task was parked at.
         * The gateway resolves it to a file and a line. */
        if (g_safe_stop.valid && g_safe_stop.probe != UINT32_MAX)
        {
            cJSON *loc = cJSON_CreateObject();
            cJSON_AddNumberToObject(loc, "probe", g_safe_stop.probe);
            if (g_safe_stop.task >= 0)
            {
                char names[DBG_MAX_TASKS][32];
                task_count(names);
                add_task(loc, (size_t)g_safe_stop.task, names[g_safe_stop.task]);
            }
            cJSON_AddItemToObject(b, "location", loc);
        }
        if (g_safe_stop.valid) cJSON_AddItemToObject(b, "safeStop", safe_stop_body());
    }
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
    /* The breakpoints the runtime holds, and how many of them are armed. A client reads this to
     * know what it set and what is still live: a one-shot that has fired is gone from the list,
     * and a disabled breakpoint is listed but not armed. The agent's table is the authority:
     * the list is what the runtime holds, not what was asked of it. */
    cJSON *bps = cJSON_CreateArray();
    for (const auto &kv : g_bps)
    {
        dbg_bp_info_t info[DBG_MAX_PER_PROBE];
        const size_t  n = debug_agent_breakpoint_at(kv.first, info, DBG_MAX_PER_PROBE);
        for (size_t i = 0; i < n; ++i)
        {
            cJSON *o = cJSON_CreateObject();
            cJSON_AddNumberToObject(o, "id", info[i].id);
            cJSON_AddNumberToObject(o, "probe", kv.first);
            /* The file the breakpoint belongs to: the link's bookkeeping, by the id. */
            for (const Bp &b : kv.second)
                if (b.id == info[i].id) { cJSON_AddNumberToObject(o, "fileId", b.fileId); break; }
            cJSON_AddBoolToObject(o, "enabled", info[i].enabled);
            cJSON_AddBoolToObject(o, "oneShot", info[i].one_shot);
            if (info[i].hit_target) cJSON_AddNumberToObject(o, "hitCount", info[i].hit_target);
            cJSON_AddNumberToObject(o, "hits", info[i].hits);
            cJSON_AddItemToArray(bps, o);
        }
    }
    cJSON_AddItemToObject(b, "breakpoints", bps);
    cJSON_AddNumberToObject(b, "armedProbes", (double)debug_agent_armed_count());
    /* A117's "the runtime PID/uptime are unchanged" across twenty attaches: the process id
     * is the one fact an attach must not move, and nothing else in the body says it (E45
     * ruling 1). The gateway reads it through the core, so it survives a status poll that
     * the webserver answers while plc_main is SIGSTOPped. */
    cJSON_AddNumberToObject(b, "pid", (double)getpid());
    /* A217 reads the build identity back off the status API to prove a refused apply changed
     * nothing; hello_locked() reports the same block, and this is that block. */
    add_image(b);
    /* The sessions the core holds, so a status read can tell a stop from a detach (A033). */
    cJSON *ids = cJSON_CreateArray();
    for (auto &sess : g_sessions) cJSON_AddItemToArray(ids, cJSON_CreateString(sess.first.c_str()));
    cJSON_AddItemToObject(b, "sessions", ids);
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

/* ADR-003 D4: every stop carries the task's virtual time, ms since the program started. */
void add_vt(cJSON *b, size_t task)
{
    const int64_t vt = plc_task_virtual_time_ns(task);
    if (vt >= 0) cJSON_AddNumberToObject(b, "vt", (double)vt / 1e6);
}

/* A held task's cycle step: start the released scan, or report its end. Under g_mu. */
bool cycle_on_hold_locked(size_t i, const char *name, int64_t since)
{
    Cycle &c = g_cycle[i];
    if (!c.active) return false;
    if (c.has_probe && !c.last_leg)
    {
        c.last_leg = true;
        debug_agent_set_trap(DBG_TRAP_AT_PROBE, (uint32_t)i, c.probe);
        if (!plc_run_control_run_scans(i, 1, 2000)) log_error("[debug] cycle step: the task did not resume");
        return true;
    }
    c = Cycle{};
    debug_agent_clear_trap();
    TaskView &v   = g_view[i];
    v.halted      = true;
    v.at_boundary = true;
    g_last_stop_task = (long)i;
    cJSON *b = cJSON_CreateObject();
    cJSON_AddStringToObject(b, "reason", "step");
    add_task(b, i, name);
    cJSON_AddBoolToObject(b, "heldAtBoundary", true);
    cJSON_AddNumberToObject(b, "since", wall_ms_of(since));
    add_vt(b, i);
    send_event("stopped", b);
    plc_run_control_halted(i);
    return true;
}

void expire_sessions_locked(void);

/* ---------------------------------------------------------------- forces
 * ADR-005 D5. The registry is debug_force.cpp's; what is here is the wire
 * shape and the rule that every change to it is reported as a `forces` event
 * (D5.3), so a client never has to poll to learn a force has gone. */

std::string hex_of(const uint8_t *bytes, uint16_t len)
{
    static const char kHex[] = "0123456789abcdef";
    std::string       s((size_t)len * 2u, '0');
    for (uint16_t i = 0; i < len; ++i)
    {
        s[2 * i]     = kHex[bytes[i] >> 4];
        s[2 * i + 1] = kHex[bytes[i] & 0xF];
    }
    return s;
}

cJSON *force_body(const dbgforce_t &f)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddNumberToObject(o, "leaf", (double)f.leaf);
    cJSON_AddStringToObject(o, "value", hex_of(f.value, f.len).c_str());
    cJSON_AddStringToObject(o, "owner", f.owner);
    cJSON_AddStringToObject(o, "reason", f.reason);
    /* Absent means no TTL: a legacy 0x42 force (ADR-005 D5.4) is the only kind
     * the runtime makes without an end. */
    if (f.expires_ms != 0) cJSON_AddNumberToObject(o, "expiresAt", (double)f.expires_ms);
    return o;
}

cJSON *forces_list(void)
{
    static dbgforce_t buf[256];
    cJSON           *list = cJSON_CreateArray();
    for (int at = 0; at < dbgforce_count();)
    {
        const int n = dbgforce_copy(&buf[0], (int)(sizeof buf / sizeof buf[0]));
        if (n <= 0) break;
        for (int i = 0; i < n; ++i) cJSON_AddItemToArray(list, force_body(buf[i]));
        at += n;
    }
    return list;
}

/* `send_event` takes the body (cJSON_AddItemToObject inside event_envelope owns it), so this
 * does not free it — the pattern every other event here follows. */
void push_forces(void)
{
    cJSON *b = cJSON_CreateObject();
    cJSON_AddItemToObject(b, "list", forces_list());
    send_event("forces", b);
}

/* ADR-005 D5.2: the TTL runs on the wall clock and an expired force is removed
 * within one dispatcher tick of expiry. The agent's tick is that window: it
 * runs whether or not any task is, including while every task is halted. */
void expire_forces_locked(void)
{
    if (dbgforce_expire(wall_ms_of(mono_ns())) > 0) push_forces();
}

/* One agent pass: P2 expiry, force TTLs, hello, stops, boundary holds, resumes, counters, state. */
void agent_pass(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    expire_sessions_locked();
    expire_forces_locked();
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
            g_cycle[i]    = Cycle{}; /* a cycle step ends at any stop: P, or a breakpoint first */
            /* The run-to-cursor's own breakpoint is spent by the first stop, whatever caused
             * it: a cursor that reached its line, or a breakpoint on the way there. */
            if (g_cursor_probe != UINT32_MAX)
            {
                debug_agent_clear_kind(g_cursor_probe, DBG_KIND_CURSOR);
                g_cursor_probe = UINT32_MAX;
            }
            cJSON *b      = cJSON_CreateObject();
            cJSON_AddStringToObject(b, "reason", reason_name(s.reason));
            add_task(b, i, names[i]);
            cJSON_AddNumberToObject(b, "probe", s.probe);
            cJSON_AddNumberToObject(b, "depth", s.depth);
            if (s.reason == DBG_STOP_BREAKPOINT)
            {
                /* pm ruling 7: breakpoint ids, as setBreakpoints returned them. The id of the
                 * breakpoint that stopped, not of every breakpoint on the line. */
                cJSON *ids = cJSON_CreateArray();
                if (s.bp_id) cJSON_AddItemToArray(ids, cJSON_CreateNumber(s.bp_id));
                cJSON_AddItemToObject(b, "breakpointIds", ids);
                cJSON_AddNumberToObject(b, "hits", s.hits);
            }
            cJSON_AddNumberToObject(b, "since", wall_ms_of(s.since_ns));
            add_vt(b, i);
            send_event("stopped", b);
            plc_run_control_halted(i);
            /* D4.11: a step that landed clears the trap, and so does a start-halted entry stop:
             * it is one shot, and every task has its own (the agent arms one per task). */
            if (s.reason == DBG_STOP_STEP || s.reason == DBG_STOP_ENTRY) debug_agent_clear_trap();
            if (s.reason == DBG_STOP_BREAKPOINT) all_stop_locked();
            continue;
        }
        int64_t    since = 0;
        const long st    = plc_task_run_state(i, &since);
        if (st == PLC_TASK_RUN_HELD && !v.halted && cycle_on_hold_locked(i, names[i], since)) continue;
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
            add_vt(b, i);
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
        for (auto &c : g_cycle) c = Cycle{};
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
    /* stop, terminate: answer once the program has stopped (outside g_mu: the unload takes it) */
    bool        wait_stopped = false;
};

/* A033: a stop's response means the program is stopped, so the status read right after it says
 * STOPPED. Bounded: a stop that has not landed in 3 s is answered anyway; the runState event
 * reports the landing. */
void wait_stopped(void)
{
    for (int i = 0; i < 300 && (plc_get_state() != PLC_STATE_STOPPED || plc_state_is_transitioning()); ++i)
        usleep(10000);
}

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

/* One setBreakpoints item: a probe ID, or {probe, condition?, hitCount?, oneShot?, enabled?}
 * (pm ruling 6). */
dbg_status_t set_one(cJSON *item, uint32_t *probe, dbg_bp_spec_t *spec, std::string *why)
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
    cJSON *o = cJSON_IsObject(item) ? cJSON_GetObjectItem(item, "oneShot") : nullptr;
    cJSON *e = cJSON_IsObject(item) ? cJSON_GetObjectItem(item, "enabled") : nullptr;
    cJSON *i = cJSON_IsObject(item) ? cJSON_GetObjectItem(item, "instance") : nullptr;
    spec->cond       = cond.data();
    spec->cond_len   = cond.size();
    spec->hit_target = is_int(h) && h->valuedouble > 0 ? (uint32_t)h->valuedouble : 0;
    spec->one_shot   = cJSON_IsTrue(o);
    spec->enabled    = !cJSON_IsFalse(e);
    /* The instance an instance breakpoint names: the flat leaf index of its first leaf. */
    spec->instance   = is_int(i) && i->valuedouble >= 0 ? (uint32_t)i->valuedouble : DBG_NO_INSTANCE;
    return DBG_OK;
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
    for (auto &c : g_cycle) c = Cycle{};
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

/* ADR-005 D4.6: a session lost past P2. A lost controller with a task halted safe-stops (outputs
 * held) instead of resuming; anything else ends as a detach. True if a safe-stop was requested. */
bool lose_session_locked(const std::string &id, bool running)
{
    auto it = g_sessions.find(id);
    if (it == g_sessions.end()) return false;
    bool halted = g_pausing;
    for (const auto &v : g_view) halted = halted || v.halted;
    const bool safe_stop = running && halted && it->second.controller;
    if (!safe_stop)
    {
        end_session_locked(id, running);
        return false;
    }
    const std::string lost_user = it->second.user;
    g_sessions.erase(it);
    if (g_sessions.empty()) debug_agent_set_attached(false);
    clear_breakpoints_locked();
    g_pausing = false;
    debug_agent_clear_trap();
    /* Where the PLC was when it safe-stopped: the probe the halted task was parked at. The
     * lost session's breakpoints are cleared above, so the stop is the only thing left that
     * says where the program was. */
    g_safe_stop = SafeStop{};
    g_safe_stop.valid   = true;
    g_safe_stop.reason  = "heartbeat_timeout";
    g_safe_stop.user    = lost_user;
    g_safe_stop.time_ms = wall_ms_of(mono_ns());
    for (size_t i = 0; i < DBG_MAX_TASKS; ++i)
    {
        const TaskView &v = g_view[i];
        if (v.halted && !v.at_boundary)
        {
            g_safe_stop.probe = v.stop.probe;
            g_safe_stop.task  = (long)i;
            break;
        }
    }
    const bool requested = plc_request_safe_stop("heartbeat_timeout");
    log_warn("Debug controller lost past P2 while halted: safe-stop %s",
             requested ? "requested" : "refused (no dispatcher)");
    return requested;
}

/* The core's P2 timer, on the agent's tick: whether or not a gateway is connected. */
void expire_sessions_locked(void)
{
    const int64_t now = mono_ns();
    std::vector<std::string> lost;
    for (const auto &s : g_sessions)
        if (now - s.second.last_beat_ns > s.second.p2_ns) lost.push_back(s.first);
    if (lost.empty()) return;
    const bool running = plc_get_state() == PLC_STATE_RUNNING;
    for (const auto &id : lost)
    {
        log_warn("[debug] session %s timed out after P2 on the core", id.c_str());
        lose_session_locked(id, running);
        cJSON *b = cJSON_CreateObject();
        cJSON_AddStringToObject(b, "session", id.c_str());
        cJSON_AddStringToObject(b, "reason", "heartbeat_timeout");
        send_event("sessionEnded", b);
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
/* ADR-004 D5.2: a located input's image slot, the value its next copy-in takes. While a task is
 * halted the program member stays at what its scan copied in (frozen); plugin writes land in the
 * image, latest wins, so the session shows both. False for anything but a located %I leaf: an
 * output or memory slot is not "pending" (the scan's copy-out decides what it becomes). */
bool read_pending_input(uint8_t arr, uint16_t elem, uint16_t width, std::string *hex)
{
    uint8_t  area = 0, size = 0, bit = 0;
    uint16_t idx  = 0;
    if (!ext_strucpp_debug_locate || !ext_strucpp_debug_locate(arr, elem, &area, &size, &idx, &bit) ||
        area != (uint8_t)strucpp::LocatedArea::Input || idx >= BUFFER_SIZE || bit > 7)
        return false;
    uint64_t v  = 0;
    bool     ok = true;
    image_lock();
    switch ((strucpp::LocatedSize)size)
    {
    case strucpp::LocatedSize::Bit: ok = bool_input[idx][bit]; if (ok) v = *bool_input[idx][bit] ? 1 : 0; break;
    case strucpp::LocatedSize::Byte: ok = byte_input[idx]; if (ok) v = *byte_input[idx]; break;
    case strucpp::LocatedSize::Word: ok = int_input[idx]; if (ok) v = *int_input[idx]; break;
    case strucpp::LocatedSize::DWord: ok = dint_input[idx]; if (ok) v = *dint_input[idx]; break;
    case strucpp::LocatedSize::LWord: ok = lint_input[idx]; if (ok) v = *lint_input[idx]; break;
    default: ok = false; break;
    }
    image_unlock();
    if (!ok) return false;
    /* The leaf's own width, little-endian, so the gateway decodes it with the leaf's type. */
    static const char kHex[] = "0123456789abcdef";
    hex->assign(width * 2u, '0');
    for (uint16_t i = 0; i < width && i < 8; ++i)
    {
        const uint8_t b = (uint8_t)(v >> (8 * i));
        (*hex)[2 * i]     = kHex[b >> 4];
        (*hex)[2 * i + 1] = kHex[b & 0xF];
    }
    return true;
}

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
    cJSON *values  = cJSON_CreateArray();
    cJSON *pending = cJSON_CreateArray();
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
            cJSON_AddItemToArray(pending, cJSON_CreateNull());
            continue;
        }
        std::string in_hex;
        cJSON_AddItemToArray(pending, read_pending_input((uint8_t)arr, (uint16_t)idx, got, &in_hex)
                                          ? cJSON_CreateString(in_hex.c_str())
                                          : cJSON_CreateNull());
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
    cJSON_AddItemToObject(b, "pending", pending);
    return success(b);
}

/* One frame's stack locals (ADR-002 D3 "Addressing", the frame half). A function's VAR_INPUT,
 * VAR_IN_OUT and VAR_TEMP, and an FB's or program's VAR_TEMP, live on the call's stack, not in the
 * program's static debug table, so readLeaves cannot name them: the frame carries its own
 * `__dbg_locals` table (void*[] in slot order, debug-info.json `pous[].frameLeaves`) and this reads
 * the leaves asked for by slot. The core does not know a table's length or a leaf's IEC type — both
 * are the gateway's, from the same build's debug info (ADR-002 D1: bound by buildId) — so each item
 * names the slot and the byte size to read. Valid only while the task stays parked: the tables live
 * on the halted task's stack. */
Reply read_frame_locals(size_t task, cJSON *frame, cJSON *list)
{
    const TaskView &v = g_view[task];
    if (plc_task_run_state(task, nullptr) == PLC_TASK_RUN_RUNNING || !v.halted)
        return fail("NOT_HALTED", "the task is not halted");
    if (!is_int(frame) || frame->valuedouble < 0)
        return fail("BAD_REQUEST", "readFrameLocals needs a non-negative frame index");
    if (!cJSON_IsArray(list) || cJSON_GetArraySize(list) > kMaxLeaves)
        return fail("BAD_REQUEST", "readFrameLocals needs a list of at most 1024 frame leaves");
    dbg_frame_t  frames[256];
    bool         truncated = false;
    const size_t n  = v.at_boundary ? 0 : debug_agent_frames(&v.stop, frames, 256, &truncated);
    const size_t idx = (size_t)frame->valuedouble;
    if (idx >= n) return fail("BAD_REQUEST", "there is no such frame in the halted task's stack");
    /* The table is void*[]; a frame with none (a POU with no frame variables) answers null for
     * every slot rather than dereferencing nothing. */
    void *const *tab = (void *const *)frames[idx].locals;
    cJSON       *out = cJSON_CreateArray();
    cJSON       *item;
    cJSON_ArrayForEach(item, list)
    {
        cJSON *slot = arg(item, "slot");
        cJSON *size = arg(item, "size");
        if (!is_int(slot) || !is_int(size))
        {
            cJSON_AddItemToArray(out, cJSON_CreateNull());
            continue;
        }
        const long s = (long)slot->valuedouble;
        const int  w = (int)size->valuedouble;
        if (s < 0 || w < 1 || w > kMaxLeafSize || !tab || !tab[s])
        {
            cJSON_AddItemToArray(out, cJSON_CreateNull());
            continue;
        }
        uint8_t buf[kMaxLeafSize];
        std::memcpy(buf, tab[s], (size_t)w);
        std::string hex((size_t)w * 2u, '0');
        static const char kHex[] = "0123456789abcdef";
        for (int i = 0; i < w; ++i)
        {
            hex[2 * i]     = kHex[buf[i] >> 4];
            hex[2 * i + 1] = kHex[buf[i] & 0xF];
        }
        cJSON_AddItemToArray(out, cJSON_CreateString(hex.c_str()));
    }
    cJSON *b = cJSON_CreateObject();
    cJSON_AddItemToObject(b, "values", out);
    return success(b);
}

/* ADR-005 D5.1: a force carries an owner (the gateway passes the session's
 * user, so the core never has to be told whose it is), a reason and a TTL.
 * `owner` is required here too: a force with no owner is exactly what R7
 * exists to prevent, and the legacy 0x42 path has its own `legacy` entry
 * point. `ttlS` absent means no end (a legacy force, re-registered as one);
 * a session force always carries one. */
Reply force_leaf(cJSON *args)
{
    cJSON *leaf = arg(args, "leaf"), *value = arg(args, "value"), *owner = arg(args, "owner"),
         *reason = arg(args, "reason"), *ttl = arg(args, "ttlS");
    if (!is_int(leaf) || leaf->valuedouble < 0) return fail("BAD_REQUEST", "force needs a leaf index");
    if (!cJSON_IsString(value)) return fail("BAD_REQUEST", "force needs the value as hex bytes");
    if (!cJSON_IsString(owner) || !owner->valuestring[0])
        return fail("BAD_REQUEST", "force needs the owner's name");
    if (!cJSON_IsString(reason)) return fail("BAD_REQUEST", "force needs a reason");

    uint8_t  bytes[DBGF_MAX_VALUE];
    const std::string hex = value->valuestring;
    if (hex.size() % 2 != 0 || hex.size() / 2 == 0 || hex.size() / 2 > sizeof bytes)
        return fail("BAD_REQUEST", "force needs a non-empty even-length hex value");
    for (size_t i = 0; i < hex.size() / 2; ++i)
    {
        const std::string pair = hex.substr(2 * i, 2);
        if (!std::isxdigit((unsigned char)pair[0]) || !std::isxdigit((unsigned char)pair[1]))
            return fail("BAD_REQUEST", "force's value is not hexadecimal");
        bytes[i] = (uint8_t)std::stoul(pair, nullptr, 16);
    }
    const uint16_t len = (uint16_t)(hex.size() / 2);

    /* A TTL past the maximum is refused before anything is pinned, naming the
     * constraint (DEBUG_PROTOCOL's RANGE_ERROR). */
    int64_t expires = 0;
    if (ttl)
    {
        if (!cJSON_IsNumber(ttl) || ttl->valuedouble <= 0)
            return fail("BAD_REQUEST", "force ttlS must be a positive number of seconds");
        expires = wall_ms_of(mono_ns()) + (int64_t)(ttl->valuedouble * 1000.0);
        if ((int64_t)(ttl->valuedouble * 1000.0) > DBGF_MAX_TTL_MS)
            return fail("RANGE_ERROR", "a force's TTL may not exceed 8 hours");
    }

    uint8_t  arr = 0;
    uint16_t elem = 0;
    if (dbgforce_arr_elem_of((uint32_t)leaf->valuedouble, &arr, &elem) != 0)
        return fail("INVALID_LOCATION", "there is no variable at that leaf in the running program");
    /* SAFE_STOPPED: no program is running, so there is nothing for a force to override. (D5.2's
     * safe-stopped rule is about a TTL expiring there, not about making one.) */
    if (plc_get_state() == PLC_STATE_SAFE_STOPPED)
        return fail("SAFE_STOPPED", "the runtime is safe-stopped, so there is no program to force");
    /* Enqueue first: the registry must not list a force the journal refused. */
    if (runtime_external_write(arr, elem, DBGW_OP_FORCE, bytes, len) != 0)
        return fail("LIMIT_EXCEEDED", "the external-write queue is full; try again shortly");
    if (dbgforce_set((uint32_t)leaf->valuedouble, owner->valuestring, reason->valuestring, bytes,
                     len, expires) != 0)
    {
        runtime_external_write(arr, elem, DBGW_OP_UNFORCE, nullptr, 0);
        return fail("LIMIT_EXCEEDED", "the force registry is full; try again shortly");
    }
    push_forces();
    return success();
}

/* One leaf's release. Absent is not an error: a force that is already gone is
 * the state the caller asked for. */
Reply unforce_leaf(cJSON *args)
{
    cJSON *leaf = arg(args, "leaf");
    if (!is_int(leaf) || leaf->valuedouble < 0) return fail("BAD_REQUEST", "unforce needs a leaf index");
    const int held = dbgforce_clear((uint32_t)leaf->valuedouble);
    uint8_t   arr = 0;
    uint16_t  elem = 0;
    if (dbgforce_arr_elem_of((uint32_t)leaf->valuedouble, &arr, &elem) != 0)
        return fail("INVALID_LOCATION", "there is no variable at that leaf in the running program");
    runtime_external_write(arr, elem, DBGW_OP_UNFORCE, nullptr, 0);
    if (held) push_forces();
    return success();
}

/* ADR-005 D5.5: one action clears every force, every owner's and legacy ones
 * included; the count is the audit log's number. */
/* ADR-005 D5.5: one action clears every force, every owner's and legacy ones included, and
 * answers how many went, which is the number the audit log records. `session` names the caller
 * and is deliberately not consulted: cancel-all is not scoped to the caller, and A188's
 * red-proof (clear only your own) is exactly what it must not do. */
Reply unforce_all(const std::string &)
{
    static dbgforce_t buf[256];
    for (int at = 0; at < dbgforce_count();)
    {
        const int n = dbgforce_copy(&buf[0], (int)(sizeof buf / sizeof buf[0]));
        if (n <= 0) break;
        for (int i = 0; i < n; ++i)
        {
            uint8_t  arr = 0;
            uint16_t elem = 0;
            if (dbgforce_arr_elem_of(buf[i].leaf, &arr, &elem) == 0)
                runtime_external_write(arr, elem, DBGW_OP_UNFORCE, nullptr, 0);
        }
        at += n;
    }
    const int removed = dbgforce_clear_all();
    push_forces();
    cJSON *b = cJSON_CreateObject();
    cJSON_AddNumberToObject(b, "removed", (double)removed);
    return success(b);
}

bool is_command(const char *c, std::initializer_list<const char *> names)
{
    for (const char *n : names)
        if (!std::strcmp(c, n)) return true;
    return false;
}

/* F037's restart, run with the link's lock dropped. The transition unloads the program, and the
 * unload calls debug_link_set_image, which takes this file's own g_mu on this same thread; it also
 * has to leave the agent thread free to turn the entry stop into a `stopped` event. Dropping the
 * lock around it is the only way both can happen: handle() owns it, and nothing else runs requests
 * meanwhile (the server thread serves one client at a time). */
int restart_program(bool cold, bool start_halted)
{
    g_mu.unlock();
    const int rc = plc_restart_program(cold, start_halted);
    g_mu.lock();
    return rc;
}

Reply handle(const char *command, cJSON *args)
{
    const bool running = plc_get_state() == PLC_STATE_RUNNING;
    std::lock_guard<std::mutex> g(g_mu);

    if (args && !cJSON_IsObject(args)) return fail("BAD_REQUEST", "arguments must be an object");
    if (!std::strcmp(command, "status")) return success(run_state_locked());
    if (!std::strcmp(command, "threads"))
    {
        /* A130: the tasks as threads, with the configuration and the scan statistics each
         * task keeps. The task's own state, so a client can tell a held task from a running
         * one without asking twice. */
        cJSON *list = cJSON_CreateArray();
        char   names[DBG_MAX_TASKS][32];
        size_t n = task_count(names);
        for (size_t i = 0; i < n; ++i)
        {
            plc_task_info_t info;
            if (plc_task_info(i, &info) != 0) continue;
            int64_t    since = 0;
            const long st    = plc_task_run_state(i, &since);
            cJSON     *t     = cJSON_CreateObject();
            cJSON_AddStringToObject(t, "name", info.name);
            cJSON_AddNumberToObject(t, "taskId", (double)i);
            cJSON_AddNumberToObject(t, "intervalMs", (double)(info.interval_ns / 1000000));
            cJSON_AddNumberToObject(t, "priority", info.priority);
            /* The task's state in the run-state vocabulary (F040): a task held at its boundary
             * is paused, and one parked at a breakpoint is halted at it. */
            const TaskView &v   = g_view[i];
            const bool      at_bp = v.halted && !v.at_boundary && v.stop.reason == DBG_STOP_BREAKPOINT;
            const char     *state = "RUNNING";
            if (st == PLC_TASK_RUN_HELD || st == PLC_TASK_RUN_ABANDONING) state = "PAUSED";
            else if (st == PLC_TASK_RUN_PARKED) state = at_bp ? "HALTED_AT_BP" : "PAUSED";
            cJSON_AddStringToObject(t, "state", state);
            cJSON *stats = cJSON_CreateObject();
            cJSON_AddNumberToObject(stats, "cycles", (double)info.cycles);
            cJSON_AddNumberToObject(stats, "overruns", (double)info.overruns);
            cJSON *scan = cJSON_CreateObject();
            cJSON_AddNumberToObject(scan, "current", (double)info.scan_ms_current);
            cJSON_AddNumberToObject(scan, "min", (double)info.scan_ms_min);
            cJSON_AddNumberToObject(scan, "max", (double)info.scan_ms_max);
            cJSON_AddNumberToObject(scan, "avg", (double)info.scan_ms_avg);
            cJSON_AddItemToObject(stats, "scanMs", scan);
            cJSON *cycle = cJSON_CreateObject();
            cJSON_AddNumberToObject(cycle, "current", (double)info.cycle_ms_current);
            cJSON_AddNumberToObject(cycle, "min", (double)info.cycle_ms_min);
            cJSON_AddNumberToObject(cycle, "max", (double)info.cycle_ms_max);
            cJSON_AddNumberToObject(cycle, "avg", (double)info.cycle_ms_avg);
            cJSON_AddItemToObject(stats, "cycleMs", cycle);
            cJSON_AddItemToObject(t, "stats", stats);
            cJSON_AddItemToArray(list, t);
        }
        cJSON *b = cJSON_CreateObject();
        cJSON_AddItemToObject(b, "threads", list);
        return success(b);
    }
    if (!std::strcmp(command, "attach"))
    {
        cJSON *user = arg(args, "user"), *role = arg(args, "role"), *p2 = arg(args, "p2");
        if (!cJSON_IsString(user) || !cJSON_IsString(role))
            return fail("BAD_REQUEST", "attach needs a user and a role");
        if (p2 && (!cJSON_IsNumber(p2) || p2->valuedouble < kP2MinS || p2->valuedouble > kP2MaxS))
            return fail("BAD_REQUEST", "attach p2 must be a number of seconds from 5 to 600");
        char id[24];
        std::snprintf(id, sizeof id, "s%d", g_next_session++);
        Session s;
        s.user       = user->valuestring;
        s.controller = !std::strcmp(role->valuestring, "controller");
        s.last_beat_ns = mono_ns();
        s.p2_ns        = (int64_t)((p2 ? p2->valuedouble : kP2DefaultS) * 1e9);
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
                                            "readLeaves", "cycleStep", "restart", "threads",
                                            "runToCursor",
                                            "readFrameLocals", "force", "unforce", "unforceAll",
                                            "listForces"});
    if (!known)
        return fail("UNSUPPORTED", std::string("this runtime core does not implement ") + command);

    /* pm ruling 5: every request but attach and status names a session the core holds. */
    cJSON *sid = arg(args, "session");
    if (!cJSON_IsString(sid)) return fail("BAD_REQUEST", "the request needs a session");
    if (!g_sessions.count(sid->valuestring)) return fail("SESSION_ENDED", "no such session on the core");
    const std::string session = sid->valuestring;
    g_sessions[session].last_beat_ns = mono_ns();

    if (!std::strcmp(command, "beatClient")) return success();
    if (!std::strcmp(command, "detach"))
    {
        /* ADR-005 D4.6: the gateway's P2 expiry. A lost controller with a task halted safe-stops
         * (outputs held) instead of resuming; otherwise it is a plain detach. */
        cJSON     *why  = arg(args, "reason");
        const bool lost = cJSON_IsString(why) && !std::strcmp(why->valuestring, "heartbeat_timeout");
        bool safe_stopped = false;
        if (lost)
            safe_stopped = lose_session_locked(session, running);
        else
            end_session_locked(session, running);
        /* ADR-005 D5.3: a force outlives the session, so detach reports what
         * is still active and a new session lists the same (A113). */
        cJSON *b = cJSON_CreateObject();
        cJSON_AddItemToObject(b, "forces", forces_list());
        cJSON_AddBoolToObject(b, "safeStopped", safe_stopped);
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
    if (!std::strcmp(command, "restart"))
    {
        /* F037. The program goes away and comes back, so the request answers only once the new
         * program is RUNNING: the stop and the start both run here, on the link's socket thread,
         * which is not a scan thread and may wait (as `stop` already does).
         * ADR-005 D5.3: forces die with the program, which plc_state_manager's unload
         * (dbgforce_reset) does; the registry is empty by the time the new one is RUNNING. */
        cJSON     *kind = arg(args, "kind"), *halt = arg(args, "startHalted");
        const bool named = kind && cJSON_IsString(kind);
        if (kind && (!named || (std::strcmp(kind->valuestring, "warm") &&
                                 std::strcmp(kind->valuestring, "cold") &&
                                 std::strcmp(kind->valuestring, "reset"))))
            return fail("BAD_REQUEST", "restart kind must be warm, cold or reset");
        if (named && !std::strcmp(kind->valuestring, "reset"))
            return fail("UNSUPPORTED",
                        "a reset-to-origin restart clears the retained store, which this core "
                        "cannot do; use warm or cold");
        if (halt && !cJSON_IsBool(halt) && !cJSON_IsNumber(halt))
            return fail("BAD_REQUEST", "startHalted must be a boolean");
        const bool start_halted = cJSON_IsTrue(halt);
        if (start_halted && running && debug_agent_probe_count() == 0)
            return fail("NO_PROBES", "the running program was built without probes");

        /* No stop, step or breakpoint survives the reload: the task indices and the probe space
         * belong to the program that is going away. */
        g_pausing = false;
        debug_agent_clear_trap();
        for (auto &v : g_view) v = TaskView{};
        for (auto &c : g_cycle) c = Cycle{};
        g_last_stop_task = -1;

        const bool cold = named && !std::strcmp(kind->valuestring, "cold");
        const int  rc   = restart_program(cold, start_halted);
        if (rc == PLC_RESTART_BUSY) return fail("BUSY", "a state change is in flight");
        if (rc == PLC_RESTART_REFUSED)
            return fail("SAFE_STOPPED",
                        "the PLC is safe-stopped, or its mode switch forbids running; a user with "
                        "control must start it");
        if (rc != PLC_RESTART_OK)
            return fail("INTERNAL", "the program did not restart; the PLC log has the reason");
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
        /* The kind of breakpoint this request sets: a line, a function's entry or one instance.
         * Each kind has its own set, so setting one never disturbs another. */
        uint8_t kind = DBG_KIND_LINE;
        cJSON  *k    = arg(args, "kind");
        if (k)
        {
            if (!cJSON_IsString(k)) return fail("BAD_REQUEST", "the breakpoint kind must be a string");
            const std::string ks = k->valuestring;
            if (ks == "line") kind = DBG_KIND_LINE;
            else if (ks == "function") kind = DBG_KIND_FUNCTION;
            else if (ks == "instance") kind = DBG_KIND_INSTANCE;
            else return fail("BAD_REQUEST", "the breakpoint kind must be line, function or instance");
        }
        /* The per-breakpoint request: one item is one breakpoint, and several items may name the
         * same probe (a conditional breakpoint and a logpoint on one line). The items are grouped
         * by probe, keeping their order, and the probe's list is published as one record. */
        struct Item
        {
            uint32_t     probe = 0;
            dbg_bp_spec_t spec{};
            bool         parsed = false;
            dbg_status_t status = DBG_OK;
            std::string  why;
        };
        std::vector<Item>        items;
        std::vector<uint32_t>    order;   /* the probes, in the order they first appear */
        cJSON                   *item = nullptr;
        cJSON_ArrayForEach(item, list)
        {
            Item it;
            dbg_bp_spec_t spec{};
            std::string   why;
            const dbg_status_t s = set_one(item, &it.probe, &spec, &why);
            it.spec   = spec;
            it.spec.kind = kind;
            it.parsed = s == DBG_OK;
            it.status = s;
            it.why    = why;
            if (it.parsed)
            {
                bool seen = false;
                for (uint32_t p : order) if (p == it.probe) { seen = true; break; }
                if (!seen) order.push_back(it.probe);
            }
            items.push_back(it);
        }
        /* Replace this kind's set for the file: disarm what the new list no longer names
         * (pm ruling 6). The other kinds at the same probe are untouched. */
        for (auto it = g_bps.begin(); it != g_bps.end();)
        {
            bool wanted = false;
            for (uint32_t p : order) if (p == it->first) { wanted = true; break; }
            std::vector<Bp> keep;
            bool             mine = false;
            for (const Bp &b : it->second)
            {
                if (b.kind == kind && b.fileId == fid) { mine = true; continue; }
                keep.push_back(b);
            }
            if (mine && !wanted)
            {
                /* The whole probe goes only when nothing of another kind is left on it. */
                if (keep.empty()) debug_agent_clear_breakpoint(it->first);
                else debug_agent_clear_kind(it->first, kind);
                it->second = keep;
                if (it->second.empty()) it = g_bps.erase(it);
                else ++it;
            }
            else
                ++it;
        }
        /* Publish each probe's list. */
        std::map<uint32_t, std::vector<Bp>> ids;
        std::vector<int>                     item_ids(items.size(), 0);
        for (uint32_t p : order)
        {
            dbg_bp_spec_t specs[DBG_MAX_PER_PROBE];
            size_t        n = 0;
            /* The id of a breakpoint travels with its position among the breakpoints of this
             * kind, so a replace keeps the client's id. */
            std::vector<Bp> old_same;
            auto             old = g_bps.find(p);
            if (old != g_bps.end())
                for (const Bp &b : old->second)
                    if (b.kind == kind) old_same.push_back(b);
            size_t i = 0;
            for (size_t k = 0; k < items.size(); ++k)
            {
                Item &it = items[k];
                if (!it.parsed || it.probe != p || n >= DBG_MAX_PER_PROBE) continue;
                Bp b;
                b.fileId = fid;
                b.kind   = kind;
                b.id     = i < old_same.size() ? old_same[i].id : g_next_bp++;
                it.spec.id = b.id;
                specs[n++] = it.spec;
                item_ids[k] = b.id;
                i++;
            }
            const dbg_status_t s = debug_agent_set_breakpoint(p, kind, specs, n);
            if (s != DBG_OK)
            {
                for (Item &it : items)
                    if (it.parsed && it.probe == p)
                    {
                        it.parsed = false;
                        it.status = s;
                        it.why    = s == DBG_E_LIMIT ? "the runtime's limit of 1024 breakpoints is reached"
                                  : s == DBG_E_INVALID_LOCATION ? "no such probe in the running program"
                                  : s == DBG_E_NO_PROBES ? "the running program has no probes"
                                  : s == DBG_E_CONDITION ? "the condition failed verification"
                                                        : "the breakpoint was refused";
                    }
                continue;
            }
            /* The probe's list: the kinds this request does not touch keep their place. */
            std::vector<Bp> list;
            if (old != g_bps.end())
                for (const Bp &b : old->second)
                    if (b.kind != kind) list.push_back(b);
            for (size_t k = 0; k < items.size(); ++k)
            {
                const Item &it = items[k];
                if (!it.parsed || it.probe != p) continue;
                Bp b;
                b.fileId = fid;
                b.kind   = kind;
                b.id     = it.spec.id;
                list.push_back(b);
            }
            ids[p] = list;
        }
        for (auto &kv : ids) g_bps[kv.first] = kv.second;
        cJSON *out = cJSON_CreateArray();
        for (size_t k = 0; k < items.size(); ++k)
        {
            const Item &it = items[k];
            cJSON       *o  = cJSON_CreateObject();
            if (it.parsed)
            {
                cJSON_AddNumberToObject(o, "id", item_ids[k]);
            }
            else
            {
                cJSON_AddItemToObject(o, "id", cJSON_CreateNull());
            }
            cJSON_AddNumberToObject(o, "probe", it.probe);
            cJSON_AddBoolToObject(o, "verified", it.parsed);
            cJSON_AddBoolToObject(o, "enabled", it.spec.enabled);
            cJSON_AddBoolToObject(o, "oneShot", it.spec.one_shot);
            if (it.spec.hit_target) cJSON_AddNumberToObject(o, "hitCount", it.spec.hit_target);
            if (!it.parsed)
            {
                cJSON *e = cJSON_CreateObject();
                cJSON_AddStringToObject(e, "code", status_code(it.status));
                cJSON_AddStringToObject(e, "message", it.why.c_str());
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

    if (!std::strcmp(command, "force")) return force_leaf(args);
    if (!std::strcmp(command, "unforce")) return unforce_leaf(args);
    if (!std::strcmp(command, "unforceAll")) return unforce_all(session);
    if (!std::strcmp(command, "listForces"))
    {
        cJSON *b = cJSON_CreateObject();
        cJSON_AddItemToObject(b, "forces", forces_list());
        return success(b);
    }

    if (!running) return fail("NOT_RUNNING", "no program is running");

    if (!std::strcmp(command, "stop"))
    {
        /* Stop the program, not the session (F005): the session stays attached. */
        g_pausing = false;
        debug_agent_clear_trap();
        if (!plc_begin_transition(PLC_STATE_STOPPED)) return fail("BUSY", "a state change is in flight");
        Reply r = success();
        r.wait_stopped = true;
        return r;
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
    if (!std::strcmp(command, "readFrameLocals"))
    {
        if (all)
        {
            if (g_last_stop_task < 0) return fail("NOT_HALTED", "no task is halted");
            task = (size_t)g_last_stop_task;
        }
        return read_frame_locals(task, arg(args, "frame"), arg(args, "leaves"));
    }
    /* pm ruling 8: continue is all-stop, whatever `task` says. */
    if (!std::strcmp(command, "continue")) return resume_all_locked();

    /* Run to cursor: a breakpoint of the runtime's own, at the cursor's probe, and the tasks
     * resume. It is one shot and it leaves nothing behind: the first stop spends it. */
    if (!std::strcmp(command, "runToCursor"))
    {
        cJSON *p = arg(args, "probe");
        if (!is_int(p) || p->valuedouble < 0)
            return fail("BAD_REQUEST", "runToCursor needs a probe");
        const uint32_t probe = (uint32_t)p->valuedouble;
        if (debug_agent_probe_count() == 0)
            return fail("NO_PROBES", "the running program has no probes");
        dbg_bp_spec_t spec{};
        spec.kind     = DBG_KIND_CURSOR;
        spec.enabled  = true;
        spec.one_shot = true;
        const dbg_status_t s = debug_agent_set_breakpoint(probe, DBG_KIND_CURSOR, &spec, 1);
        if (s != DBG_OK)
            return fail(status_code(s), "the run-to-cursor breakpoint was refused");
        g_cursor_probe = probe;
        return resume_all_locked();
    }

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
    if (!std::strcmp(command, "cycleStep"))
    {
        /* D4.9: from probe P, stop at P in the next scan; from a boundary, run one scan and hold.
         * The trap is off for the first leg so the task runs to its boundary. */
        Cycle c;
        c.active    = true;
        c.has_probe = !v.at_boundary;
        c.probe     = v.stop.probe;
        c.last_leg  = v.at_boundary;
        debug_agent_clear_trap();
        g_cycle[task] = c;
        g_view[task]  = TaskView{};
        if (!plc_run_control_run_scans(task, 1, 2000))
        {
            g_cycle[task] = Cycle{};
            return fail("INTERNAL", "the dispatcher did not resume the task");
        }
        char names[DBG_MAX_TASKS][32];
        task_count(names);
        cJSON *ev = cJSON_CreateObject();
        add_task(ev, task, names[task]);
        send_event("continued", ev);
        return success();
    }
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
    if (r.wait_stopped) wait_stopped();

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
