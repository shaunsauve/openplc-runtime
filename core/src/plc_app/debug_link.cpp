// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

// Interface only: the core link is declared with no behaviour yet.

#include "debug_link.h"

#include <cstdio>

extern "C" int debug_link_start(const char *) { return 0; }

extern "C" void debug_link_handle_request(const char *, char *out, size_t out_size)
{
    std::snprintf(out, out_size, "{\"type\":\"response\",\"success\":false}");
}
