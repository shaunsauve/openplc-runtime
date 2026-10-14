// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Held output values, the safe_stop.hold file and the halt policy. See plc_output_hold.h.

#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>

#include <fcntl.h>
#include <pthread.h>
#include <semaphore.h>
#include <unistd.h>

extern "C" {
#include "../drivers/plugins/native/cjson/cJSON.h"
}

#include "image_tables.h"
#include "journal_buffer.h"
#include "plc_output_hold.h"
#include "utils/log.h"
#include "utils/rt_mutex.h"
#include "utils/utils.h"

namespace {

struct HeldOutputs
{
    uint8_t  bool_q[BUFFER_SIZE][8];
    uint8_t  byte_q[BUFFER_SIZE];
    uint16_t int_q[BUFFER_SIZE];
    uint32_t dint_q[BUFFER_SIZE];
    uint64_t lint_q[BUFFER_SIZE];
};

HeldOutputs       g_held;
std::atomic<bool> g_held_valid{false};
/* Copied at capture: the file thread must not read the .so's pointer while an unload clears it. */
char              g_held_md5[36];

const char *g_hold_path = PLC_SAFE_STOP_HOLD_PATH;

/* The dispatcher writes the reason, the file thread reads it: PI mutex, held for a copy only. */
char            g_reason[PLC_SAFE_STOP_REASON_LEN];
pthread_mutex_t g_reason_mu = PTHREAD_MUTEX_INITIALIZER;

__attribute__((constructor)) void reason_mutex_init_pi(void)
{
    rt_mutex_upgrade_static(&g_reason_mu, "hold_reason_mutex");
}
std::atomic<bool> g_task_faulted{false};

halt_policy_t g_policy;

/* File layout: header, the HeldOutputs block when kind is VALUES, then an FNV-1a of all of it. */
constexpr char     kMagic[8] = {'O', 'P', 'L', 'C', 'H', 'O', 'L', 'D'};
constexpr uint32_t kVersion  = 1;

struct FileHeader
{
    char     magic[8];
    uint32_t version;
    uint32_t kind;
    int64_t  period_ns;
    uint32_t task_faulted;
    char     reason[PLC_SAFE_STOP_REASON_LEN];
    char     program_md5[36];
};

uint64_t fnv1a(uint64_t h, const void *data, size_t len)
{
    const uint8_t *p = static_cast<const uint8_t *>(data);
    for (size_t i = 0; i < len; ++i)
    {
        h ^= p[i];
        h *= 1099511628211ULL;
    }
    return h;
}

constexpr uint64_t kFnvBasis = 14695981039346656037ULL;

bool write_all(int fd, const void *data, size_t len)
{
    const uint8_t *p = static_cast<const uint8_t *>(data);
    while (len > 0)
    {
        ssize_t n = write(fd, p, len);
        if (n < 0)
        {
            if (errno == EINTR) continue;
            return false;
        }
        p += n;
        len -= (size_t)n;
    }
    return true;
}

bool read_all(int fd, void *data, size_t len)
{
    uint8_t *p = static_cast<uint8_t *>(data);
    while (len > 0)
    {
        ssize_t n = read(fd, p, len);
        if (n < 0)
        {
            if (errno == EINTR) continue;
            return false;
        }
        if (n == 0) return false;
        p += n;
        len -= (size_t)n;
    }
    return true;
}

/* Hold-file thread: the dispatcher posts, this thread does the I/O. Latest request wins. */
sem_t                  g_file_sem;
pthread_once_t         g_file_once = PTHREAD_ONCE_INIT;
std::atomic<int>       g_file_req_kind{OUTPUT_HOLD_FILE_NONE};
std::atomic<int64_t>   g_file_req_period{0};
std::atomic<uint64_t>  g_file_req_seq{0};
std::atomic<uint64_t>  g_file_done_seq{0};

void *file_thread(void *)
{
    for (;;)
    {
        while (sem_wait(&g_file_sem) != 0 && errno == EINTR)
        {
        }
        const uint64_t seq = g_file_req_seq.load(std::memory_order_acquire);
        const auto kind = (output_hold_file_kind_t)g_file_req_kind.load(std::memory_order_acquire);
        const int64_t period = g_file_req_period.load(std::memory_order_acquire);
        if (kind == OUTPUT_HOLD_FILE_NONE)
            output_hold_file_delete();
        else
            output_hold_file_write(kind, period);
        g_file_done_seq.store(seq, std::memory_order_release);
    }
    return nullptr;
}

void file_thread_start(void)
{
    if (sem_init(&g_file_sem, 0, 0) != 0)
    {
        log_error("[hold] hold-file semaphore init failed: %s", strerror(errno));
        return;
    }
    /* SCHED_OTHER explicitly: the first request may come from the FIFO dispatcher. */
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    sched_param sp{};
    sp.sched_priority = 0;
    pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
    pthread_attr_setschedpolicy(&attr, SCHED_OTHER);
    pthread_attr_setschedparam(&attr, &sp);
    pthread_attr_setdetachstate(&attr, PTHREAD_CREATE_DETACHED);
    pthread_t tid;
    int       rc = pthread_create(&tid, &attr, file_thread, nullptr);
    pthread_attr_destroy(&attr);
    if (rc != 0) log_error("[hold] hold-file thread could not start: %s", strerror(rc));
}

void journal_value(const halt_policy_output_t &o, uint64_t v)
{
    switch (o.size)
    {
    case HALT_OUT_BIT:
        journal_write_bool(JOURNAL_BOOL_OUTPUT, o.byte_index, o.bit_index, v != 0);
        break;
    case HALT_OUT_BYTE:
        journal_write_byte(JOURNAL_BYTE_OUTPUT, o.byte_index, (uint8_t)v);
        break;
    case HALT_OUT_WORD:
        journal_write_int(JOURNAL_INT_OUTPUT, o.byte_index, (uint16_t)v);
        break;
    case HALT_OUT_DWORD:
        journal_write_dint(JOURNAL_DINT_OUTPUT, o.byte_index, (uint32_t)v);
        break;
    case HALT_OUT_LWORD:
        journal_write_lint(JOURNAL_LINT_OUTPUT, o.byte_index, v);
        break;
    }
}

uint64_t image_value(const halt_policy_output_t &o)
{
    const uint16_t i = o.byte_index;
    switch (o.size)
    {
    case HALT_OUT_BIT:   return bool_output[i][o.bit_index] ? *bool_output[i][o.bit_index] : 0;
    case HALT_OUT_BYTE:  return byte_output[i] ? *byte_output[i] : 0;
    case HALT_OUT_WORD:  return int_output[i] ? *int_output[i] : 0;
    case HALT_OUT_DWORD: return dint_output[i] ? *dint_output[i] : 0;
    case HALT_OUT_LWORD: return lint_output[i] ? *lint_output[i] : 0;
    }
    return 0;
}

}  // namespace

extern "C" void output_hold_capture(void)
{
    for (int i = 0; i < BUFFER_SIZE; ++i)
    {
        for (int b = 0; b < 8; ++b)
            g_held.bool_q[i][b] = bool_output[i][b] ? *bool_output[i][b] : 0;
        g_held.byte_q[i] = byte_output[i] ? *byte_output[i] : 0;
        g_held.int_q[i]  = int_output[i] ? *int_output[i] : 0;
        g_held.dint_q[i] = dint_output[i] ? *dint_output[i] : 0;
        g_held.lint_q[i] = lint_output[i] ? *lint_output[i] : 0;
    }
    std::snprintf(g_held_md5, sizeof g_held_md5, "%s",
                  ext_strucpp_program_md5 ? ext_strucpp_program_md5 : "");
    g_held_valid.store(true, std::memory_order_release);
}

extern "C" void output_hold_restore(void)
{
    if (!g_held_valid.load(std::memory_order_acquire)) return;
    for (int i = 0; i < BUFFER_SIZE; ++i)
    {
        for (int b = 0; b < 8; ++b)
            if (bool_output[i][b]) *bool_output[i][b] = g_held.bool_q[i][b];
        if (byte_output[i]) *byte_output[i] = g_held.byte_q[i];
        if (int_output[i]) *int_output[i] = g_held.int_q[i];
        if (dint_output[i]) *dint_output[i] = g_held.dint_q[i];
        if (lint_output[i]) *lint_output[i] = g_held.lint_q[i];
    }
}

extern "C" bool output_hold_valid(void)
{
    return g_held_valid.load(std::memory_order_acquire);
}

extern "C" void output_hold_invalidate(void)
{
    g_held_valid.store(false, std::memory_order_release);
}

extern "C" void output_hold_set_file_path(const char *path)
{
    g_hold_path = path ? path : PLC_SAFE_STOP_HOLD_PATH;
}

extern "C" const char *output_hold_file_path(void)
{
    return g_hold_path;
}

extern "C" void output_hold_set_reason(const char *reason)
{
    pthread_mutex_lock(&g_reason_mu);
    std::snprintf(g_reason, sizeof g_reason, "%s", reason ? reason : "");
    pthread_mutex_unlock(&g_reason_mu);
}

extern "C" void output_hold_reason(char *out, size_t len)
{
    if (!out || len == 0) return;
    pthread_mutex_lock(&g_reason_mu);
    std::snprintf(out, len, "%s", g_reason);
    pthread_mutex_unlock(&g_reason_mu);
}

extern "C" void output_hold_note_task_fault(void)
{
    g_task_faulted.store(true, std::memory_order_release);
}

extern "C" bool output_hold_task_faulted(void)
{
    return g_task_faulted.load(std::memory_order_acquire);
}

extern "C" int output_hold_file_write(output_hold_file_kind_t kind, int64_t period_ns)
{
    const bool with_values = (kind == OUTPUT_HOLD_FILE_VALUES) && output_hold_valid();

    FileHeader hdr{};
    std::memcpy(hdr.magic, kMagic, sizeof kMagic);
    hdr.version      = kVersion;
    hdr.kind         = with_values ? OUTPUT_HOLD_FILE_VALUES : OUTPUT_HOLD_FILE_GATE_ONLY;
    hdr.period_ns    = period_ns;
    hdr.task_faulted = output_hold_task_faulted() ? 1 : 0;
    output_hold_reason(hdr.reason, sizeof hdr.reason);
    std::memcpy(hdr.program_md5, g_held_md5, sizeof hdr.program_md5);

    uint64_t sum = fnv1a(kFnvBasis, &hdr, sizeof hdr);
    if (with_values) sum = fnv1a(sum, &g_held, sizeof g_held);

    char tmp[512];
    std::snprintf(tmp, sizeof tmp, "%s.tmp", g_hold_path);
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0)
    {
        log_error("[hold] cannot write %s: %s", tmp, strerror(errno));
        return -1;
    }
    bool ok = write_all(fd, &hdr, sizeof hdr) &&
              (!with_values || write_all(fd, &g_held, sizeof g_held)) &&
              write_all(fd, &sum, sizeof sum) && fsync(fd) == 0;
    if (close(fd) != 0) ok = false;
    if (!ok || rename(tmp, g_hold_path) != 0)
    {
        log_error("[hold] writing %s failed: %s", g_hold_path, strerror(errno));
        unlink(tmp);
        return -1;
    }
    log_info("[hold] %s written (%s)", g_hold_path, with_values ? "held values" : "start gate only");
    return 0;
}

extern "C" int output_hold_file_delete(void)
{
    if (unlink(g_hold_path) != 0 && errno != ENOENT)
    {
        log_error("[hold] cannot delete %s: %s", g_hold_path, strerror(errno));
        return -1;
    }
    return 0;
}

extern "C" output_hold_file_kind_t output_hold_file_read(int64_t *period_ns)
{
    if (period_ns) *period_ns = 0;
    int fd = open(g_hold_path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
    {
        if (errno != ENOENT) log_error("[hold] cannot read %s: %s", g_hold_path, strerror(errno));
        return errno == ENOENT ? OUTPUT_HOLD_FILE_NONE : OUTPUT_HOLD_FILE_GATE_ONLY;
    }

    FileHeader hdr{};
    uint64_t   sum = 0;
    bool       ok  = read_all(fd, &hdr, sizeof hdr) && std::memcmp(hdr.magic, kMagic, 8) == 0 &&
              hdr.version == kVersion;
    const bool values = ok && hdr.kind == OUTPUT_HOLD_FILE_VALUES;
    /* Read into a scratch copy so a corrupt file never reaches the live snapshot. */
    static HeldOutputs scratch;
    if (ok && values) ok = read_all(fd, &scratch, sizeof scratch);
    if (ok) ok = read_all(fd, &sum, sizeof sum);
    close(fd);
    if (ok)
    {
        uint64_t want = fnv1a(kFnvBasis, &hdr, sizeof hdr);
        if (values) want = fnv1a(want, &scratch, sizeof scratch);
        ok = (want == sum);
    }
    if (!ok)
    {
        log_error("[hold] %s is unreadable: keeping the start gate, holding no values", g_hold_path);
        return OUTPUT_HOLD_FILE_GATE_ONLY;
    }

    hdr.reason[sizeof hdr.reason - 1] = '\0';
    hdr.program_md5[sizeof hdr.program_md5 - 1] = '\0';
    output_hold_set_reason(hdr.reason);
    std::memcpy(g_held_md5, hdr.program_md5, sizeof g_held_md5);
    if (hdr.task_faulted) output_hold_note_task_fault();
    if (period_ns) *period_ns = hdr.period_ns;
    if (!values) return OUTPUT_HOLD_FILE_GATE_ONLY;
    std::memcpy(&g_held, &scratch, sizeof g_held);
    g_held_valid.store(true, std::memory_order_release);
    return OUTPUT_HOLD_FILE_VALUES;
}

extern "C" void output_hold_file_request(output_hold_file_kind_t kind, int64_t period_ns)
{
    pthread_once(&g_file_once, file_thread_start);
    g_file_req_kind.store(kind, std::memory_order_release);
    g_file_req_period.store(period_ns, std::memory_order_release);
    g_file_req_seq.fetch_add(1, std::memory_order_acq_rel);
    sem_post(&g_file_sem);
}

extern "C" void output_hold_file_flush(void)
{
    const uint64_t want = g_file_req_seq.load(std::memory_order_acquire);
    const timespec poll = {0, 1000000L};
    for (int i = 0; i < 5000 && g_file_done_seq.load(std::memory_order_acquire) < want; ++i)
        nanosleep(&poll, nullptr);
}

/* ---- Halt policy ---- */

extern "C" void halt_policy_reset(void)
{
    std::memset(&g_policy, 0, sizeof g_policy);
    g_policy.kind = HALT_POLICY_HOLD;
}

extern "C" const halt_policy_t *halt_policy_get(void)
{
    return &g_policy;
}

extern "C" bool halt_policy_parse_location(const char *loc, halt_out_size_t *size,
                                           uint16_t *byte_index, uint8_t *bit_index)
{
    if (!loc || loc[0] != '%' || (loc[1] != 'Q' && loc[1] != 'q')) return false;
    halt_out_size_t s;
    switch (loc[2])
    {
    case 'X': case 'x': s = HALT_OUT_BIT; break;
    case 'B': case 'b': s = HALT_OUT_BYTE; break;
    case 'W': case 'w': s = HALT_OUT_WORD; break;
    case 'D': case 'd': s = HALT_OUT_DWORD; break;
    case 'L': case 'l': s = HALT_OUT_LWORD; break;
    default: return false;
    }
    char         *end = nullptr;
    unsigned long idx = std::strtoul(loc + 3, &end, 10);
    if (end == loc + 3 || idx >= BUFFER_SIZE) return false;
    unsigned long bit = 0;
    if (s == HALT_OUT_BIT)
    {
        if (*end != '.') return false;
        const char *b = end + 1;
        bit           = std::strtoul(b, &end, 10);
        if (end == b || bit > 7) return false;
    }
    if (*end != '\0') return false;
    *size       = s;
    *byte_index = (uint16_t)idx;
    *bit_index  = (uint8_t)bit;
    return true;
}

extern "C" int halt_policy_load(const char *path)
{
    halt_policy_reset();
    FILE *f = std::fopen(path, "rb");
    if (!f) return 0;

    char   buf[16384];
    size_t n = std::fread(buf, 1, sizeof buf - 1, f);
    std::fclose(f);
    buf[n] = '\0';

    cJSON *root = cJSON_Parse(buf);
    int    rc   = -1;
    do
    {
        if (!root) break;
        const cJSON *pol = cJSON_GetObjectItemCaseSensitive(root, "policy");
        if (!cJSON_IsString(pol)) break;
        halt_policy_t p{};
        if (std::strcmp(pol->valuestring, "safe") == 0)
            p.kind = HALT_POLICY_SAFE;
        else if (std::strcmp(pol->valuestring, "hold") != 0)
            break;
        /* The list is kept under hold too, for the read-back; only safe applies it. */
        const cJSON *list = cJSON_GetObjectItemCaseSensitive(root, "safe");
        if (list &&
            (!cJSON_IsArray(list) || cJSON_GetArraySize(list) > PLC_HALT_POLICY_MAX_OUTPUTS))
            break;
        bool         valid = true;
        const cJSON *e;
        cJSON_ArrayForEach(e, list)
        {
            const cJSON *loc = cJSON_GetObjectItemCaseSensitive(e, "location");
            const cJSON *val = cJSON_GetObjectItemCaseSensitive(e, "value");
            halt_policy_output_t &o = p.outputs[p.count];
            if (!cJSON_IsString(loc) || !cJSON_IsNumber(val) || val->valuedouble < 0 ||
                !halt_policy_parse_location(loc->valuestring, &o.size, &o.byte_index,
                                            &o.bit_index))
            {
                valid = false;
                break;
            }
            o.safe_value = (uint64_t)val->valuedouble;
            ++p.count;
        }
        if (!valid) break;
        g_policy = p;
        rc       = 0;
    } while (false);
    cJSON_Delete(root);

    if (rc != 0)
    {
        halt_policy_reset();
        log_error("[halt] %s is invalid: falling back to the hold policy", path);
    }
    else
    {
        log_info("[halt] halt policy: %s (%zu configured output(s))",
                 g_policy.kind == HALT_POLICY_SAFE ? "safe" : "hold", g_policy.count);
    }
    return rc;
}

extern "C" void halt_policy_apply_safe(halt_policy_scope_fn in_scope, void *ctx)
{
    if (g_policy.kind != HALT_POLICY_SAFE) return;
    image_lock();
    for (size_t i = 0; i < g_policy.count; ++i)
    {
        halt_policy_output_t &o = g_policy.outputs[i];
        if (o.applied || (in_scope && !in_scope(&o, ctx))) continue;
        o.saved_value = image_value(o);
        o.applied     = true;
    }
    image_unlock();
    for (size_t i = 0; i < g_policy.count; ++i)
        if (g_policy.outputs[i].applied) journal_value(g_policy.outputs[i], g_policy.outputs[i].safe_value);
}

extern "C" void halt_policy_restore_saved(halt_policy_scope_fn in_scope, void *ctx)
{
    for (size_t i = 0; i < g_policy.count; ++i)
    {
        halt_policy_output_t &o = g_policy.outputs[i];
        if (!o.applied || (in_scope && !in_scope(&o, ctx))) continue;
        journal_value(o, o.saved_value);
        o.applied = false;
    }
}

extern "C" void halt_policy_journal_safe_values(void)
{
    if (g_policy.kind != HALT_POLICY_SAFE) return;
    for (size_t i = 0; i < g_policy.count; ++i)
        journal_value(g_policy.outputs[i], g_policy.outputs[i].safe_value);
}
