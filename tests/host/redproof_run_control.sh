#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Autonomy®

# Red-proofs for test_breakpoint_table and test_run_control: injects each defect the criteria
# name into a scratch copy of the tree and requires the named test to fail with the expected
# text (an ERE). Linux with ThreadSanitizer (run.sh's requirements; in Docker add
# --security-opt seccomp=unconfined). Usage: tests/host/redproof_run_control.sh
#
# A034's red-proof is in STruCpp's probe emitter (outside this repo); the probe is moved after its
# statement in the test's fake program instead, which is the same observable.
# RP=<substring> runs only the red-proofs whose name contains it.

set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT=$(pwd)

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# A205 (3) is an inspection, not a run (TSan counts a same-thread store in the release sequence):
# the read-side section must be entered with an RMW on qs.
if grep -q 'const uint64_t q = t->qs.fetch_add(1, std::memory_order_relaxed);' core/src/plc_app/debug_agent.cpp; then
  echo "  ok    A205-3 inspection: the section enter is a fetch_add on qs"
else
  echo "  FAIL  A205-3 inspection: the section enter is not a fetch_add on qs"
  exit 1
fi

python3 - "$ROOT" "$WORK" <<'PY'
import os, re, subprocess, sys, shutil
root, work = sys.argv[1], sys.argv[2]
A = 'core/src/plc_app/debug_agent.cpp'
L = 'core/src/plc_app/debug_link.cpp'
T = 'tests/host/test_run_control.cpp'
# name, test filter, expected ERE, [(file, find, replace)]
M = [
 ('A031 arm at the next scan boundary', 'test_run_control', r'\[A031', [
   (A, '    flag_store(probe, (uint8_t)(flag_load(probe) | DBG_FLAG_ARMED));\n    if (cur >= 0)', '    if (cur >= 0)'),
   (A, '    tl_head   = f;\n    tl_depth++;', '    tl_head   = f;\n    if (tl_depth == 0)\n        for (uint32_t p = 0; p < g_count; ++p)\n            if (g_bp[p].load(std::memory_order_acquire)) flag_store(p, (uint8_t)(flag_load(p) | DBG_FLAG_ARMED));\n    tl_depth++;')]),
 ('A032 drop breakpoints past the maximum', 'test_breakpoint_table', r'past the limit was not refused', [
   (A, '    if (cur < 0 && g_nbps >= DBG_MAX_BREAKPOINTS)\n    {\n        g_limit_refusals++;\n        return DBG_E_LIMIT;\n    }', '    if (cur < 0 && g_nbps >= DBG_MAX_BREAKPOINTS) return DBG_OK;')]),
 ('A034 probe after its statement', 'test_run_control', r'\[A034', [
   (T, '        probe(P_K);\n        m_counter   = (uint16_t)(m_counter + 1);\n        g_counter_st = m_counter;\n', '        m_counter   = (uint16_t)(m_counter + 1);\n        g_counter_st = m_counter;\n        probe(P_K);\n')]),
 ('A033-1 stop as detach', 'test_run_control', r'no longer attached after stop', [
   (L, '        g_pausing = false;\n        debug_agent_clear_trap();\n        if (!plc_begin_transition', '        g_pausing = false;\n        debug_agent_clear_trap();\n        debug_agent_set_attached(false);\n        if (!plc_begin_transition')]),
 ('A033-2 pause deferred to the scan boundary', 'test_run_control', r'froze [0-9]+ ms after a pause', [
   (L, '    debug_agent_set_trap(DBG_TRAP_PAUSE, DBG_TASK_ANY, 0);\n    plc_run_control_pause(PLC_ALL_TASKS);', '    plc_run_control_pause(PLC_ALL_TASKS);')]),
 ('A205-1 rewrite the armed record in place (Option A)', 'test_breakpoint_table_tsan', r'ThreadSanitizer: data race', [
   (A, '    const int32_t cur = g_cur[probe];\n    if (cur < 0 && g_nbps', '    const int32_t cur = g_cur[probe];\n    if (cur >= 0)\n    {\n        BpRecord &ip = g_pool[cur];\n        ip.hit_target = hit_target;\n        ip.cond_len = (uint16_t)cond_len;\n        if (cond_len) std::memcpy(ip.cond, cond, cond_len);\n        return DBG_OK;\n    }\n    if (cur < 0 && g_nbps')]),
 ('A205-2 reuse a retired record without the grace period', 'test_breakpoint_table_tsan', r'ThreadSanitizer: data race', [
   (A, 'passed = (r.snap[t] & 1) == 0 ||', 'passed = true ||')]),
]
fails = 0
for name, only, expect, edits in [m for m in M if os.environ.get("RP", "") in m[0]]:
    tree = os.path.join(work, 'tree')
    shutil.rmtree(tree, ignore_errors=True)
    os.makedirs(tree)
    subprocess.run(f"cd '{root}' && tar cf - --exclude=build --exclude=venvs --exclude=.git core tests/host | (cd '{tree}' && tar xf -)", shell=True, check=True)
    ok = True
    for f, find, repl in edits:
        p = os.path.join(tree, f); s = open(p).read()
        if s.count(find) != 1:
            print(f"  FAIL  {name}: mutation target not found exactly once in {f}"); ok = False; break
        open(p, 'w').write(s.replace(find, repl))
    if not ok:
        fails += 1; continue
    r = subprocess.run(['bash', 'tests/host/run.sh'], cwd=tree, env={**os.environ, 'ONLY': only},
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    out = r.stdout + r.stderr
    if 'did not compile' in out:
        print(f"  FAIL  {name}: mutant did not compile"); fails += 1
    elif re.search(expect, out):
        print(f"  red   {name}")
    else:
        print(f"  FAIL  {name}: survived (expected /{expect}/)")
        print('\n'.join(l for l in out.splitlines() if 'FAIL' in l)[:800]); fails += 1
print("red-proofs: every injected defect was caught" if fails == 0 else f"red-proofs: {fails} not caught")
sys.exit(1 if fails else 0)
PY
