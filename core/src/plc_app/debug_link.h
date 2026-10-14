// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Autonomy®

/**
 * @file debug_link.h
 * @brief The core link (ADR-003 D1.3): the debug gateway's connection to the core agent.
 *
 * A full-duplex unix socket carrying frames of `u32 BE length + UTF-8 JSON` in the public
 * envelope. The core answers requests and pushes events (stopped, continued, runState, beat,
 * output) unsolicited. The same requests are also accepted one per line on the legacy command
 * socket as `DEBUG_LINK:<json>`, answered `DEBUG_LINK:<json>`, with no events.
 *
 * See also:
 *   core/src/plc_app/debug_agent.h - the table, slow path and trap the requests drive.
 *   docs/DEBUG_PROTOCOL.md - every core-link message, field and error code.
 */
#ifndef DEBUG_LINK_H
#define DEBUG_LINK_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DEBUG_LINK_SOCKET_PATH "/run/runtime/plc_debug.socket"
/* Largest frame accepted; a larger one is BAD_REQUEST and closes the connection. */
#define DEBUG_LINK_MAX_FRAME (1024 * 1024)

/* Starts the agent thread and the socket server at path. Returns 0 on success. */
int debug_link_start(const char *path);

/* Handles one request (a JSON envelope) and writes the response envelope to out.
 * Used by the legacy socket's DEBUG_LINK: command. */
void debug_link_handle_request(const char *json, char *out, size_t out_size);

#ifdef __cplusplus
}
#endif

#endif  // DEBUG_LINK_H
