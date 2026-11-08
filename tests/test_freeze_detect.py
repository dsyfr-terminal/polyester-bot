"""Detects a frozen (paused) sandbox from the clock skew.

A paused sandbox stops the guest kernel: CLOCK_MONOTONIC stops advancing while
the wall clock keeps going. The difference between the two deltas is how long
the VM was frozen.

Two parts, both against the REAL source of bot/monitor.py:
  1. the helper frozen_seconds()
  2. the freeze guard in main()'s loop (resync + reset state on a jump)

Usage:  python tests/test_freeze_detect.py [path-to-monitor.py]
Exit 0 = pass, Exit 1 = fail
"""

import ast
import asyncio
import os
import sys
import textwrap
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "bot", "monitor.py")
src = open(SRC, encoding="utf-8").read()
lines = src.splitlines()
tree = ast.parse(src)

ok = True


def check(label, good, detail=""):
    global ok
    ok = ok and good
    print("%-6s %s%s" % ("PASS" if good else "FAIL", label,
                         (" -> " + detail) if detail else ""))


# ---------------------------------------------------------------- part 1
fn = next((n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name == "frozen_seconds"), None)
if fn is None:
    print("FAIL: monitor.py has no frozen_seconds() helper")
    sys.exit(1)

ns = {}
exec(compile("\n".join(lines[fn.lineno - 1:fn.end_lineno]), "<frozen_seconds>", "exec"), ns)
frozen_seconds = ns["frozen_seconds"]

cases = [
    # (wall_delta, mono_delta, expected_frozen, label)
    (2.0, 2.0, 0.0, "normal poll"),
    (2.4, 2.4, 0.0, "normal poll with jitter"),
    (6470.0, 2.0, 6468.0, "1h47m freeze (the real event)"),
    (30.0, 0.0, 30.0, "30s freeze"),
    (3600.0, 3600.0, 0.0, "slow request, guest still running"),
]
for wall_d, mono_d, expected, label in cases:
    got = frozen_seconds(100.0 + wall_d, 50.0 + mono_d, 100.0, 50.0)
    check(label, abs(got - expected) < 0.001,
          "expected=%.1f got=%.1f" % (expected, got))

# ---------------------------------------------------------------- part 2
main_node = next(n for n in tree.body
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
loop = next((n for n in ast.walk(main_node)
             if isinstance(n, ast.While) and isinstance(n.test, ast.Constant)
             and n.test.value is True), None)
if loop is None:
    print("FAIL: no main loop found in monitor.py")
    sys.exit(1)

body = loop.body
try_idx = next((i for i, s in enumerate(body) if isinstance(s, ast.Try)), None)
frozen_idx = next((i for i, s in enumerate(body)
                   if isinstance(s, ast.Assign)
                   and any(getattr(t, "id", None) == "frozen" for t in s.targets)), None)
if try_idx is None or frozen_idx is None:
    print("FAIL: the main loop has no freeze guard (no frozen_seconds call)")
    sys.exit(1)

guard_src = "async def guard(now, mono, last_wall, last_mono, pos, state, adopt_q, next_scan):\n"
guard_src += textwrap.indent(textwrap.dedent(
    "\n".join("\n".join(lines[s.lineno - 1:s.end_lineno])
              for s in body[frozen_idx:try_idx])), "    ")
guard_src += "\n    return pos, state, adopt_q, next_scan, frozen\n"

calls = []
alerts = []


async def fake_resync(tag):
    calls.append(tag)
    return [("TEST-USDT", 5)]


gns = {"frozen_seconds": frozen_seconds, "FREEZE_SECONDS": 60.0, "time": time,
       "alert": lambda text, priority=False: alerts.append((text, priority)),
       "resync": fake_resync}
exec(compile(guard_src, "<freeze_guard>", "exec"), gns)
guard = gns["guard"]


async def run_guard(wall_delta, mono_delta):
    calls.clear()
    alerts.clear()
    return await guard(now=1000.0 + wall_delta, mono=500.0 + mono_delta,
                       last_wall=1000.0, last_mono=500.0,
                       pos={"symbol": "OLD-USDT"}, state="selling",
                       adopt_q=[], next_scan=123.0)


# normal iteration: nothing happens
pos, state, adopt_q, next_scan, frozen = asyncio.run(run_guard(2.0, 2.0))
check("normal poll does not resync", not calls and state == "selling"
      and pos == {"symbol": "OLD-USDT"} and next_scan == 123.0,
      "frozen=%.1f calls=%d" % (frozen, len(calls)))

# the real event: 1h47m freeze
pos, state, adopt_q, next_scan, frozen = asyncio.run(run_guard(6470.0, 2.0))
check("1h47m freeze triggers a resync", len(calls) == 1, "calls=%d" % len(calls))
check("freeze alerts the user", any("FREEZE" in t for t, _ in alerts),
      str([t for t, _ in alerts])[:80])
check("freeze resets the position", pos == {} and state == "idle")
check("freeze re-adopts the real balances", adopt_q == [("TEST-USDT", 5)],
      str(adopt_q))
check("freeze forces an immediate scan", next_scan == 0.0)

print()
if ok:
    print("PASS: freeze detected from wall-vs-monotonic skew, state re-synced")
    sys.exit(0)
print("FAIL: freeze detection is wrong")
sys.exit(1)
