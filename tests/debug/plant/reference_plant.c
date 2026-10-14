// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file reference_plant.c
 * @brief The reference plant (F150): a simulated I/O plugin on PLC virtual time.
 *
 * A native plugin the debug harness drives instead of real I/O. At every cycle_end it samples
 * the output image, each task's scan witness and virtual clock, and applies the scripted input
 * schedule on the plant clock: PLC virtual time (get_plc_virtual_time_ns), so the schedule and
 * the samples freeze with the PLC (ADR-004 D4.3, R8xR9). `clock=wall` drives the schedule from
 * CLOCK_MONOTONIC instead: the red-proof of A152, never a mode for real tests.
 *
 * The harness talks to it over a Unix stream socket, one line per command:
 *   SAMPLES <seq>          every sample after seq (JSON lines), then END
 *   SET <addr> <value>     write an input through the journal (%IX0.0, %IW0, %ID0, ...)
 *   RESET                  clear the schedule; its times count from the plant clock now (logged)
 *   SCHEDULE <ms> <addr> <value>   set the input when the plant clock reaches origin + ms
 *   HOLD <task|all>, RESUME <task|all>   run control through plc_run_control_hold/resume
 *   MARK <text>            a line in the plant log
 * Every sample is also appended to the plant log as one JSON line. The socket stays up across a
 * PLC stop and start (stop_loop only stops sampling); cleanup closes it.
 *
 * Config (plugin config path, key=value lines): socket, log, clock (virtual|wall), one
 * `witness=<task> <task index> <%Q address>` line per task and one `output=<%Q address>` line per
 * output the plant observes.
 *
 * See also:
 *   tests/debug/harness/reference_plant.py - the harness's PlantAdapter over this socket.
 *   tests/debug/plant/e2e.sh - builds this plugin, a runtime and a corpus program, runs scenarios.
 *   core/src/drivers/plugin_types.h - plugin_runtime_args_t and the hook contract.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "plugin_types.h"

#define MAX_TASKS     8
#define MAX_OUTS      64
#define MAX_SCHEDULE  64
#define RING          1024        /* hook -> server hand-off, power of two */
#define HISTORY       16384       /* samples kept for SAMPLES queries */
#define JOURNAL_BOOL_INPUT 0
#define JOURNAL_BYTE_INPUT 3
#define JOURNAL_INT_INPUT  5
#define JOURNAL_DINT_INPUT 8
#define JOURNAL_LINT_INPUT 11

typedef struct
{
    char     area;   /* 'I' or 'Q' */
    char     size;   /* 'X', 'B', 'W', 'D', 'L' */
    int      index;
    int      bit;
} addr_t;

typedef struct
{
    addr_t   addr;
    uint64_t value;
} io_value_t;

typedef struct
{
    uint64_t   seq;
    int64_t    wall_ns;
    int64_t    vt_ns[MAX_TASKS];
    int64_t    witness[MAX_TASKS];
    int        n_out;
    io_value_t out[MAX_OUTS];
} sample_t;

typedef struct
{
    char   name[32];
    int    task_idx;
    addr_t addr;
} witness_t;

typedef struct
{
    int64_t    at_ns;
    io_value_t set;
    bool       done;
} schedule_t;

static plugin_runtime_args_t g_args;
static char       g_socket_path[108] = "/tmp/reference_plant.sock";
static char       g_log_path[256]    = "/tmp/reference_plant.log";
static bool       g_wall_clock       = false;
static witness_t  g_witness[MAX_TASKS];
static int        g_n_witness = 0;
static addr_t     g_outputs[MAX_OUTS];
static int        g_n_outputs = 0;

/* Single producer (the dispatcher's cycle_end) and single consumer (the server thread). */
static sample_t        g_ring[RING];
static atomic_uint_fast64_t g_head = 0, g_tail = 0;
static uint64_t        g_seq = 0;

static sample_t        g_history[HISTORY];
static uint64_t        g_history_count = 0;   /* server thread only */

/* Schedule: written by the server under g_sched_mu, applied by the hook with a trylock so the
 * dispatcher never blocks on the harness. */
static pthread_mutex_t g_sched_mu = PTHREAD_MUTEX_INITIALIZER;
static schedule_t      g_schedule[MAX_SCHEDULE];
static int             g_n_schedule = 0;
static int64_t         g_origin_ns  = 0;

static atomic_bool     g_running = false;  /* the socket server runs: from start_loop to cleanup */
static atomic_bool     g_sampling = false; /* cycle_end samples: from start_loop to stop_loop */
static pthread_t       g_server;
static bool            g_server_up = false;
static int             g_listen_fd = -1;
static FILE           *g_log = NULL;

typedef bool (*run_control_fn)(size_t, int);

static int64_t mono_ns(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000000000LL + t.tv_nsec;
}

static int64_t plant_clock_ns(void)
{
    if (g_wall_clock) return mono_ns();
    return g_args.get_plc_virtual_time_ns ? g_args.get_plc_virtual_time_ns() : -1;
}

static bool parse_addr(const char *s, addr_t *a)
{
    if (s[0] != '%' || (s[1] != 'I' && s[1] != 'Q')) return false;
    a->area = s[1];
    a->size = s[2];
    a->bit  = 0;
    const char *n = s + 3;
    if (a->size >= '0' && a->size <= '9')
    {
        a->size = 'X';
        n       = s + 2;
    }
    if (strchr("XBWDL", a->size) == NULL) return false;
    char *end = NULL;
    a->index  = (int)strtol(n, &end, 10);
    if (end == n || a->index < 0 || a->index >= g_args.buffer_size) return false;
    if (a->size == 'X')
    {
        if (*end != '.') return false;
        a->bit = (int)strtol(end + 1, &end, 10);
        if (a->bit < 0 || a->bit > 7) return false;
    }
    return *end == '\0';
}

static int format_addr(char *buf, size_t n, const addr_t *a)
{
    if (a->size == 'X') return snprintf(buf, n, "%%%cX%d.%d", a->area, a->index, a->bit);
    return snprintf(buf, n, "%%%c%c%d", a->area, a->size, a->index);
}

/* Caller holds image_lock. False when the slot is not bound to a program variable. */
static bool read_slot(const addr_t *a, uint64_t *v)
{
    const bool q = a->area == 'Q';
    switch (a->size)
    {
    case 'X': {
        IEC_BOOL *p =
            q ? g_args.bool_output[a->index][a->bit] : g_args.bool_input[a->index][a->bit];
        if (!p) return false;
        *v = *p ? 1 : 0;
        return true;
    }
    case 'B': {
        IEC_BYTE *p = q ? g_args.byte_output[a->index] : g_args.byte_input[a->index];
        if (!p) return false;
        *v = *p;
        return true;
    }
    case 'W': {
        IEC_UINT *p = q ? g_args.int_output[a->index] : g_args.int_input[a->index];
        if (!p) return false;
        *v = *p;
        return true;
    }
    case 'D': {
        IEC_UDINT *p = q ? g_args.dint_output[a->index] : g_args.dint_input[a->index];
        if (!p) return false;
        *v = *p;
        return true;
    }
    case 'L': {
        IEC_ULINT *p = q ? g_args.lint_output[a->index] : g_args.lint_input[a->index];
        if (!p) return false;
        *v = *p;
        return true;
    }
    }
    return false;
}

static int write_input(const addr_t *a, uint64_t v)
{
    if (a->area != 'I') return -1;
    switch (a->size)
    {
    case 'X': return g_args.journal_write_bool(JOURNAL_BOOL_INPUT, a->index, a->bit, v != 0);
    case 'B': return g_args.journal_write_byte(JOURNAL_BYTE_INPUT, a->index, (int)v);
    case 'W': return g_args.journal_write_int(JOURNAL_INT_INPUT, a->index, (int)v);
    case 'D': return g_args.journal_write_dint(JOURNAL_DINT_INPUT, a->index, (unsigned int)v);
    case 'L': return g_args.journal_write_lint(JOURNAL_LINT_INPUT, a->index, v);
    }
    return -1;
}

/* The configured outputs. Every image slot has storage (unbound ones point at filler), so the
 * program's own outputs come from the config. Caller holds image_lock. */
static int collect_outputs(io_value_t *out)
{
    int n = 0;
    for (int i = 0; i < g_n_outputs; ++i)
    {
        uint64_t v;
        if (read_slot(&g_outputs[i], &v)) out[n++] = (io_value_t){g_outputs[i], v};
    }
    return n;
}

static void apply_schedule(void)
{
    if (pthread_mutex_trylock(&g_sched_mu) != 0) return;   /* next tick */
    const int64_t now = plant_clock_ns();
    for (int i = 0; i < g_n_schedule; ++i)
    {
        schedule_t *s = &g_schedule[i];
        if (!s->done && now >= 0 && now - g_origin_ns >= s->at_ns)
        {
            write_input(&s->set.addr, s->set.value);
            s->done = true;
        }
    }
    pthread_mutex_unlock(&g_sched_mu);
}

// ---------------------------------------------------------------------------
// Plugin hooks
// ---------------------------------------------------------------------------

static void load_config(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f)
    {
        if (g_args.log_warn) g_args.log_warn("[PLANT] no config at %s: defaults", path);
        return;
    }
    char line[256];
    while (fgets(line, sizeof line, f))
    {
        line[strcspn(line, "\r\n")] = '\0';
        char *eq = strchr(line, '=');
        if (!eq) continue;
        *eq             = '\0';
        const char *val = eq + 1;
        if (strcmp(line, "socket") == 0)
            snprintf(g_socket_path, sizeof g_socket_path, "%s", val);
        else if (strcmp(line, "log") == 0)
            snprintf(g_log_path, sizeof g_log_path, "%s", val);
        else if (strcmp(line, "clock") == 0)
            g_wall_clock = strcmp(val, "wall") == 0;
        else if (strcmp(line, "output") == 0 && g_n_outputs < MAX_OUTS)
        {
            if (parse_addr(val, &g_outputs[g_n_outputs]) && g_outputs[g_n_outputs].area == 'Q')
                g_n_outputs++;
            else if (g_args.log_warn)
                g_args.log_warn("[PLANT] bad output line: %s", val);
        }
        else if (strcmp(line, "witness") == 0 && g_n_witness < MAX_TASKS)
        {
            witness_t *w = &g_witness[g_n_witness];
            char       addr[32];
            if (sscanf(val, "%31s %d %31s", w->name, &w->task_idx, addr) == 3 &&
                parse_addr(addr, &w->addr) && w->addr.area == 'Q')
                g_n_witness++;
            else if (g_args.log_warn)
                g_args.log_warn("[PLANT] bad witness line: %s", val);
        }
    }
    fclose(f);
}

int init(void *args)
{
    if (!args) return -1;
    /* The runtime frees args when init returns: keep a copy. */
    memcpy(&g_args, args, sizeof g_args);
    if (!g_args.get_plc_virtual_time_ns || !g_args.get_task_virtual_time_ns)
    {
        if (g_args.log_error) g_args.log_error("[PLANT] runtime has no virtual-time API");
        return -1;
    }
    load_config(g_args.plugin_specific_config_file_path);
    return 0;
}

void cycle_start(void) {}

/* Dispatcher thread, every frame: sample and apply the schedule. No allocation, no IO. */
void cycle_end(void)
{
    if (!atomic_load(&g_sampling)) return;
    apply_schedule();

    const uint64_t head = atomic_load_explicit(&g_head, memory_order_relaxed);
    const uint64_t tail = atomic_load_explicit(&g_tail, memory_order_acquire);
    if (head - tail >= RING) return;   /* the server fell behind: drop, seq shows the gap */
    sample_t *s = &g_ring[head & (RING - 1)];
    s->seq      = ++g_seq;
    s->wall_ns  = mono_ns();
    g_args.image_lock();
    for (int i = 0; i < g_n_witness; ++i)
    {
        uint64_t v = 0;
        s->witness[i] = read_slot(&g_witness[i].addr, &v) ? (int64_t)(int32_t)v : -1;
    }
    s->n_out = collect_outputs(s->out);
    g_args.image_unlock();
    for (int i = 0; i < g_n_witness; ++i)
        s->vt_ns[i] = g_args.get_task_virtual_time_ns(g_witness[i].task_idx);
    atomic_store_explicit(&g_head, head + 1, memory_order_release);
}

// ---------------------------------------------------------------------------
// Server thread
// ---------------------------------------------------------------------------

static int format_sample(char *buf, size_t n, const sample_t *s)
{
    int k = snprintf(buf, n, "{\"seq\":%llu,\"t\":%.3f,\"vt\":{", (unsigned long long)s->seq,
                     (double)s->wall_ns / 1e6);
    for (int i = 0; i < g_n_witness && k < (int)n; ++i)
        k += snprintf(buf + k, n - k, "%s\"%s\":%.3f", i ? "," : "", g_witness[i].name,
                      (double)s->vt_ns[i] / 1e6);
    k += snprintf(buf + k, n - k, "},\"witness\":{");
    for (int i = 0; i < g_n_witness && k < (int)n; ++i)
        k += snprintf(buf + k, n - k, "%s\"%s\":%lld", i ? "," : "", g_witness[i].name,
                      (long long)s->witness[i]);
    k += snprintf(buf + k, n - k, "},\"outputs\":{");
    for (int i = 0; i < s->n_out && k < (int)n; ++i)
    {
        char a[24];
        format_addr(a, sizeof a, &s->out[i].addr);
        if (s->out[i].addr.size == 'X')
            k += snprintf(buf + k, n - k, "%s\"%s\":%s", i ? "," : "", a,
                          s->out[i].value ? "true" : "false");
        else
            k += snprintf(buf + k, n - k, "%s\"%s\":%llu", i ? "," : "", a,
                          (unsigned long long)s->out[i].value);
    }
    k += snprintf(buf + k, n - k, "}}\n");
    return k < (int)n ? k : (int)n - 1;
}

/* Move hook samples into the history and the plant log. */
static void drain(void)
{
    char line[4096];
    for (;;)
    {
        const uint64_t tail = atomic_load_explicit(&g_tail, memory_order_relaxed);
        if (tail == atomic_load_explicit(&g_head, memory_order_acquire)) break;
        const sample_t *s = &g_ring[tail & (RING - 1)];
        g_history[g_history_count % HISTORY] = *s;
        g_history_count++;
        if (g_log)
        {
            format_sample(line, sizeof line, s);
            fputs(line, g_log);
        }
        atomic_store_explicit(&g_tail, tail + 1, memory_order_release);
    }
    if (g_log) fflush(g_log);
}

static void send_all(int fd, const char *buf, size_t n)
{
    while (n > 0)
    {
        ssize_t w = send(fd, buf, n, MSG_NOSIGNAL);
        if (w <= 0)
        {
            if (w < 0 && errno == EINTR) continue;
            return;
        }
        buf += w;
        n -= (size_t)w;
    }
}

static void reply(int fd, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
static void reply(int fd, const char *fmt, ...)
{
    char    buf[256];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    if (n > 0) send_all(fd, buf, (size_t)(n < (int)sizeof buf ? n : (int)sizeof buf - 1));
}

static bool parse_value(const char *s, uint64_t *v)
{
    if (strcmp(s, "true") == 0 || strcmp(s, "TRUE") == 0) return (*v = 1), true;
    if (strcmp(s, "false") == 0 || strcmp(s, "FALSE") == 0) return (*v = 0), true;
    char *end = NULL;
    long long x = strtoll(s, &end, 0);
    if (end == s || *end) return false;
    *v = (uint64_t)x;
    return true;
}

static size_t task_arg(const char *s)
{
    if (strcmp(s, "all") == 0) return (size_t)-1;
    for (int i = 0; i < g_n_witness; ++i)
        if (strcmp(g_witness[i].name, s) == 0) return (size_t)g_witness[i].task_idx;
    return (size_t)strtoul(s, NULL, 10);
}

static void command(int fd, char *cmd)
{
    char *arg = strchr(cmd, ' ');
    if (arg) *arg++ = '\0';
    else arg = cmd + strlen(cmd);

    if (strcmp(cmd, "SAMPLES") == 0)
    {
        drain();
        const uint64_t after = strtoull(arg, NULL, 10);
        const uint64_t first = g_history_count > HISTORY ? g_history_count - HISTORY : 0;
        char           line[4096];
        for (uint64_t i = first; i < g_history_count; ++i)
        {
            const sample_t *s = &g_history[i % HISTORY];
            if (s->seq > after) send_all(fd, line, (size_t)format_sample(line, sizeof line, s));
        }
        reply(fd, "END\n");
    }
    else if (strcmp(cmd, "SET") == 0)
    {
        char     a[32], v[32];
        addr_t   addr;
        uint64_t val;
        if (sscanf(arg, "%31s %31s", a, v) == 2 && parse_addr(a, &addr) && parse_value(v, &val) &&
            write_input(&addr, val) == 0)
            reply(fd, "OK\n");
        else
            reply(fd, "ERR SET %s\n", arg);
    }
    else if (strcmp(cmd, "RESET") == 0)
    {
        pthread_mutex_lock(&g_sched_mu);
        g_n_schedule = 0;
        g_origin_ns  = plant_clock_ns();
        pthread_mutex_unlock(&g_sched_mu);
        drain();
        if (g_log)
        {
            const int64_t vt = g_args.get_plc_virtual_time_ns();
            fprintf(g_log, "{\"mark\":\"reset\",\"origin\":%.3f,\"clock\":\"%s\",\"vt\":%.3f}\n",
                    (double)g_origin_ns / 1e6, g_wall_clock ? "wall" : "virtual", (double)vt / 1e6);
            fflush(g_log);
        }
        reply(fd, "OK\n");
    }
    else if (strcmp(cmd, "SCHEDULE") == 0)
    {
        double   at;
        char     a[32], v[32];
        addr_t   addr;
        uint64_t val;
        if (sscanf(arg, "%lf %31s %31s", &at, a, v) == 3 && parse_addr(a, &addr) &&
            addr.area == 'I' && parse_value(v, &val) && g_n_schedule < MAX_SCHEDULE)
        {
            pthread_mutex_lock(&g_sched_mu);
            g_schedule[g_n_schedule++] = (schedule_t){(int64_t)(at * 1e6), {addr, val}, false};
            pthread_mutex_unlock(&g_sched_mu);
            reply(fd, "OK\n");
        }
        else
            reply(fd, "ERR SCHEDULE %s\n", arg);
    }
    else if (strcmp(cmd, "HOLD") == 0 || strcmp(cmd, "RESUME") == 0)
    {
        /* The run-control test hook in plc_main (exported by -rdynamic); no probes exist yet. */
        const char    *sym = cmd[0] == 'H' ? "plc_run_control_hold" : "plc_run_control_resume";
        run_control_fn fn  = (run_control_fn)dlsym(RTLD_DEFAULT, sym);
        if (!fn) reply(fd, "ERR %s not exported\n", sym);
        else if (fn(task_arg(arg), 2000)) reply(fd, "OK\n");
        else reply(fd, "ERR %s refused\n", sym);
    }
    else if (strcmp(cmd, "MARK") == 0)
    {
        drain();
        if (g_log)
        {
            fprintf(g_log, "{\"mark\":\"%s\",\"t\":%.3f}\n", arg, (double)mono_ns() / 1e6);
            fflush(g_log);
        }
        reply(fd, "OK\n");
    }
    else
        reply(fd, "ERR unknown %s\n", cmd);
}

static void *server(void *unused)
{
    (void)unused;
    while (atomic_load(&g_running))
    {
        drain();
        struct timeval tv = {0, 5000};
        fd_set         rd;
        FD_ZERO(&rd);
        FD_SET(g_listen_fd, &rd);
        if (select(g_listen_fd + 1, &rd, NULL, NULL, &tv) <= 0) continue;
        int fd = accept(g_listen_fd, NULL, NULL);
        if (fd < 0) continue;
        /* One client at a time: the harness keeps its connection and polls. */
        char   buf[1024];
        size_t len = 0;
        while (atomic_load(&g_running))
        {
            drain();
            struct timeval t2 = {0, 5000};
            FD_ZERO(&rd);
            FD_SET(fd, &rd);
            if (select(fd + 1, &rd, NULL, NULL, &t2) <= 0) continue;
            ssize_t r = recv(fd, buf + len, sizeof buf - 1 - len, 0);
            if (r <= 0) break;
            len += (size_t)r;
            buf[len] = '\0';
            char *nl;
            while ((nl = strchr(buf, '\n')) != NULL)
            {
                *nl = '\0';
                command(fd, buf);
                len -= (size_t)(nl + 1 - buf);
                memmove(buf, nl + 1, len + 1);
            }
            if (len >= sizeof buf - 1) len = 0;   /* a line too long for any command */
        }
        close(fd);
    }
    return NULL;
}

/* The server outlives a PLC stop (stop_loop): a harness reads the plant after STOP (A033's
 * "plant count frozen" after stop), and the last samples are what it reads. */
int start_loop(void)
{
    atomic_store(&g_sampling, true);
    if (g_server_up) return 0;
    g_log = fopen(g_log_path, "a");
    struct sockaddr_un sa = {0};
    sa.sun_family         = AF_UNIX;
    snprintf(sa.sun_path, sizeof sa.sun_path, "%s", g_socket_path);
    unlink(g_socket_path);
    g_listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (g_listen_fd < 0 || bind(g_listen_fd, (struct sockaddr *)&sa, sizeof sa) != 0 ||
        listen(g_listen_fd, 1) != 0)
    {
        if (g_args.log_error) g_args.log_error("[PLANT] cannot listen on %s", g_socket_path);
        if (g_listen_fd >= 0) close(g_listen_fd);
        g_listen_fd = -1;
        return -1;
    }
    atomic_store(&g_running, true);
    g_server_up = pthread_create(&g_server, NULL, server, NULL) == 0;
    if (g_args.log_info)
        g_args.log_info("[PLANT] reference plant on %s, clock %s, %d witness(es)", g_socket_path,
                        g_wall_clock ? "wall (red-proof)" : "virtual", g_n_witness);
    return g_server_up ? 0 : -1;
}

void stop_loop(void)
{
    atomic_store(&g_sampling, false);
}

void cleanup(void)
{
    atomic_store(&g_sampling, false);
    atomic_store(&g_running, false);
    if (g_server_up) pthread_join(g_server, NULL);
    g_server_up = false;
    if (g_listen_fd >= 0) close(g_listen_fd);
    g_listen_fd = -1;
    unlink(g_socket_path);
    if (g_log) fclose(g_log);
    g_log = NULL;
}
