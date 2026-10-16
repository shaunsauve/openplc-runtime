// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/* The force registry — see debug_force.h. */

#include "debug_force.h"

#include <algorithm>
#include <cstring>
#include <ctime>
#include <mutex>
#include <string>
#include <vector>

#include "debug_write_journal.h"
#include "image_tables.h" /* ext_strucpp_debug_array_count / _elem_count */

namespace {

/* Bounded like the write journal it sits beside: one entry per forced leaf,
 * which is already bounded by the debug table's own size. */
constexpr size_t kMaxForces = 4096;

struct Force
{
    uint32_t    leaf;
    std::string owner;
    std::string reason;
    std::vector<uint8_t> value;
    int64_t     expires_ms = 0;
};

std::mutex             g_mu;
std::vector<Force>     g_forces;

int64_t wall_ms(void)
{
    timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    return (int64_t)t.tv_sec * 1000LL + t.tv_nsec / 1000000LL;
}

/* The debug table's per-array element counts, in flat-leaf order: array 0's
 * elements, then array 1's, and so on (DEBUG_CORE_LINK.md, readLeaves). */
bool array_counts(std::vector<uint16_t> *counts)
{
    if (!ext_strucpp_debug_array_count || !ext_strucpp_debug_elem_count) return false;
    const uint8_t n = ext_strucpp_debug_array_count();
    counts->clear();
    for (uint8_t a = 0; a < n; ++a) counts->push_back(ext_strucpp_debug_elem_count(a));
    return true;
}

void copy_text(char *dst, size_t n, const std::string &src)
{
    const size_t k = src.size() < n - 1 ? src.size() : n - 1;
    std::memcpy(dst, src.data(), k);
    dst[k] = '\0';
}

}  // namespace

extern "C" int dbgforce_leaf_of(uint8_t arr, uint16_t elem, uint32_t *leaf)
{
    std::vector<uint16_t> counts;
    if (!leaf || !array_counts(&counts) || arr >= counts.size()) return -1;
    if (elem >= counts[arr]) return -1;
    uint64_t idx = elem;
    for (size_t a = 0; a < arr; ++a) idx += counts[a];
    *leaf = (uint32_t)idx;
    return 0;
}

extern "C" int dbgforce_arr_elem_of(uint32_t leaf, uint8_t *arr, uint16_t *elem)
{
    std::vector<uint16_t> counts;
    if (!arr || !elem || !array_counts(&counts)) return -1;
    uint64_t idx = leaf;
    for (size_t a = 0; a < counts.size(); ++a)
    {
        if (idx < counts[a])
        {
            *arr = (uint8_t)a;
            *elem = (uint16_t)idx;
            return 0;
        }
        idx -= counts[a];
    }
    return -1;
}

extern "C" int dbgforce_set(uint32_t leaf, const char *owner, const char *reason,
                            const uint8_t *bytes, uint16_t len, int64_t expires_ms)
{
    if (!bytes || len == 0 || len > DBGF_MAX_VALUE) return -1;
    if (expires_ms != 0 && expires_ms > wall_ms() + DBGF_MAX_TTL_MS) return -1;
    Force f;
    f.leaf       = leaf;
    f.owner      = owner ? owner : "";
    f.reason     = reason ? reason : "";
    f.value.assign(bytes, bytes + len);
    f.expires_ms = expires_ms;

    std::lock_guard<std::mutex> g(g_mu);
    auto                        it = std::find_if(
        g_forces.begin(), g_forces.end(), [&f](const Force &e) { return e.leaf == f.leaf; });
    if (it != g_forces.end())
        *it = std::move(f);
    else if (g_forces.size() < kMaxForces)
        g_forces.push_back(std::move(f));
    else
        return -1;
    return 0;
}

extern "C" int dbgforce_clear(uint32_t leaf)
{
    std::lock_guard<std::mutex> g(g_mu);
    const auto                   it =
        std::find_if(g_forces.begin(), g_forces.end(),
                     [leaf](const Force &e) { return e.leaf == leaf; });
    if (it == g_forces.end()) return 0;
    g_forces.erase(it);
    return 1;
}

extern "C" int dbgforce_clear_all(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    const int                   n = (int)g_forces.size();
    g_forces.clear();
    return n;
}

/* Enqueue the unforce that releases one leaf's pin. Outside the lock: the
 * journal has its own. */
static void release(uint32_t leaf)
{
    uint8_t  arr = 0;
    uint16_t elem = 0;
    if (dbgforce_arr_elem_of(leaf, &arr, &elem) != 0) return;
    runtime_external_write(arr, elem, DBGW_OP_UNFORCE, nullptr, 0);
}

extern "C" int dbgforce_expire(int64_t now_ms)
{
    std::vector<uint32_t> gone;
    {
        std::lock_guard<std::mutex> g(g_mu);
        for (const auto &f : g_forces)
            if (f.expires_ms != 0 && f.expires_ms <= now_ms) gone.push_back(f.leaf);
        if (gone.empty()) return 0;
        g_forces.erase(
            std::remove_if(g_forces.begin(), g_forces.end(),
                           [&gone](const Force &e) { return std::find(gone.begin(), gone.end(), e.leaf) != gone.end(); }),
            g_forces.end());
    }
    for (uint32_t leaf : gone) release(leaf);
    return (int)gone.size();
}

extern "C" int dbgforce_count(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    return (int)g_forces.size();
}

extern "C" int dbgforce_copy(dbgforce_t *out, int max)
{
    if (!out || max <= 0) return 0;
    std::lock_guard<std::mutex> g(g_mu);
    const int                   n = (int)g_forces.size() < max ? (int)g_forces.size() : max;
    for (int i = 0; i < n; ++i)
    {
        dbgforce_t &d  = out[i];
        const Force &f = g_forces[i];
        d.leaf         = f.leaf;
        d.expires_ms   = f.expires_ms;
        d.len          = (uint16_t)f.value.size();
        std::memcpy(d.value, f.value.data(), d.len);
        copy_text(d.owner, sizeof d.owner, f.owner);
        copy_text(d.reason, sizeof d.reason, f.reason);
    }
    return n;
}

extern "C" int dbgforce_get(uint32_t leaf, dbgforce_t *out)
{
    if (!out) return 0;
    std::lock_guard<std::mutex> g(g_mu);
    for (const auto &f : g_forces)
    {
        if (f.leaf != leaf) continue;
        out->leaf       = f.leaf;
        out->expires_ms = f.expires_ms;
        out->len        = (uint16_t)f.value.size();
        std::memcpy(out->value, f.value.data(), out->len);
        copy_text(out->owner, sizeof out->owner, f.owner);
        copy_text(out->reason, sizeof out->reason, f.reason);
        return 1;
    }
    return 0;
}

extern "C" void dbgforce_reset(void)
{
    std::lock_guard<std::mutex> g(g_mu);
    g_forces.clear();
}

extern "C" void dbgforce_legacy_set(uint8_t arr, uint16_t elem, const uint8_t *bytes, uint16_t len)
{
    uint32_t leaf = 0;
    if (dbgforce_leaf_of(arr, elem, &leaf) != 0) return;
    dbgforce_set(leaf, "legacy", "", bytes, len, 0);
}

extern "C" void dbgforce_legacy_clear(uint8_t arr, uint16_t elem)
{
    uint32_t leaf = 0;
    if (dbgforce_leaf_of(arr, elem, &leaf) != 0) return;
    dbgforce_clear(leaf);
}