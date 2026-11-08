"""Detects a frozen (paused) sandbox from the clock skew.

A paused sandbox stops the guest kernel: CLOCK_MONOTONIC stops advancing while
the wall clock keeps going. The difference between the two deltas is how long
the VM was frozen. If the guest clock survives the pause, the wall-clock jump
alone (a stalled loop) still gives it away.

Everything below runs against the REAL source of bot/monitor.py:
  1. the helper frozen_seconds()
  2. the thresholds FREEZE_SECONDS / STALL_SECONDS
  3. the freeze guard in main()'s loop (resync + reset state on a jump)

Usage:  python tests/test_freeze_detect.py [path-to-monitor.py]
Exit 0 = pass, Exit 1 = fail
"""

import ast
import asyncio
import os
import sys
import textwrap
import types

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


def seg(nodes):
    return textwrap.dedent("\n".join(lines[nodes[0].lineno - 1:nodes[-1].end_lineno]))


def top_func(name):
    return next(n for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


# ---------------------------------------------------------------- part 1
fn = top_func("frozen_seconds")
ns = {}
exec(compile(seg([fn]), "<frozen_seconds>", "exec"), ns)
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
thresholds = {}
for name in ("FREEZE_SECONDS", "STALL_SECONDS"):
    node = next((a for a in tree.body
                 if isinstance(a, ast.Assign)
                 and any(getattr(t, "id", None) == name for t in a.targets)), None)
    if node is None:
        print("FAIL: monitor.py has no %s threshold" % name)
        sys.exit(1)
    ns_env = {"os": types.SimpleNamespace(environ={})}
    exec(compile("\n".join(lines[node.lineno - 1:node.end_lineno]), "<%s>" % name, "exec"),
         ns_env)
    thresholds[name] = ns_env[name]
FREEZE_SECONDS = thresholds["FREEZE_SECONDS"]
STALL_SECONDS = thresholds["STALL_SECONDS"]
check("thresholds come from monitor.py", FREEZE_SECONDS > 0 and STALL_SECONDS > FREEZE_SECONDS,
      "freeze=%.0fs stall=%.0fs" % (FREEZE_SECONDS, STALL_SECONDS))

# ---------------------------------------------------------------- part 3
main_node = top_func("main")
loop = next((n for n in ast.walk(main_node)
             if isinstance(n, ast.While) and isinstance(n.test, ast.Constant)
             and n.test.value is True), None)
if loop is None:
    print("FAIL: no main loop found in monitor.py")
    sys.exit(1)

try_node = next((s for s in loop.body if isinstance(s, ast.Try)), None)
if try_node is None:
    print("FAIL: the main loop has no try block")
    sys.exit(1)

# the guard is the clock bookkeeping plus the freeze/resync block; the state
# machine that follows lives in that block's else branch and is left out
resync_if_idx = None
for i, s in enumerate(try_node.body):
    if isinstance(s, ast.If) and s.orelse and "pending_resync" in ast.dump(s.test):
        resync_if_idx = i
        break
if resync_if_idx is None:
    print("FAIL: the main loop has no freeze guard")
    sys.exit(1)

guard_stmts = try_node.body[:resync_if_idx]
resync_if = try_node.body[resync_if_idx]
guard_text = "\n".join("\n".join(lines[s.lineno - 1:s.end_lineno]) for s in guard_stmts)
# the resync block without its else branch (the state machine)
guard_text += "\n" + "\n".join(lines[resync_if.lineno - 1:resync_if.body[-1].end_lineno])

guard_src = ("async def guard(now, last_wall, last_mono, pos, state, adopt_q,\n"
             "                 next_scan, pending_resync):\n")
guard_src += textwrap.indent(textwrap.dedent(guard_text), "    ")
guard_src += ("\n    return pos, state, adopt_q, next_scan, pending_resync, "
              "last_wall, last_mono\n")


class FakeTime:
    def __init__(self):
        self.wall = 1000.0
        self.mono = 500.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono


ft = FakeTime()
calls = []
alerts = []
beats = []
resync_result = ([("TEST-USDT", 5)], True)


async def fake_resync(tag):
    calls.append(tag)
    return list(resync_result[0]), resync_result[1]


gns = {"frozen_seconds": frozen_seconds, "time": ft, "alert":
       lambda text, priority=False: alerts.append((text, priority)),
       "resync": fake_resync, "beat": beats.append,
       "FREEZE_SECONDS": FREEZE_SECONDS, "STALL_SECONDS": STALL_SECONDS}
exec(compile(guard_src, "<freeze_guard>", "exec"), gns)
guard = gns["guard"]


async def run_guard(wall_delta, mono_delta, last=(1000.0, 500.0), pending=False):
    calls.clear()
    alerts.clear()
    beats.clear()
    ft.wall = 1000.0 + wall_delta
    ft.mono = 500.0 + mono_delta
    return await guard(now=ft.wall, last_wall=last[0], last_mono=last[1],
                       pos={"symbol": "OLD-USDT"}, state="selling",
                       adopt_q=[], next_scan=123.0, pending_resync=pending)


# 1. normal iteration: nothing happens, and the baseline moves forward
pos, state, adopt_q, next_scan, pending, lw, lm = asyncio.run(run_guard(2.0, 2.0))
check("normal poll does not resync", not calls and state == "selling"
      and pos == {"symbol": "OLD-USDT"} and next_scan == 123.0,
      "calls=%d" % len(calls))
check("normal poll is not an alert", not alerts, str(alerts)[:60])
check("normal poll writes the heartbeat", len(beats) == 1)
check("baseline moves forward", (lw, lm) == (1002.0, 502.0), "%s/%s" % (lw, lm))

# 2. the real event: 1h47m freeze, guest clock stopped
pos, state, adopt_q, next_scan, pending, lw, lm = asyncio.run(run_guard(6470.0, 2.0))
check("1h47m freeze triggers a resync", len(calls) == 1, "calls=%d" % len(calls))
check("freeze alerts the user", any("FREEZE" in t for t, _ in alerts),
      str([t for t, _ in alerts])[:80])
check("freeze resets the position", pos == {} and state == "idle")
check("freeze re-adopts the real balances", adopt_q == [("TEST-USDT", 5)], str(adopt_q))
check("freeze forces an immediate scan", next_scan == 0.0)
check("freeze clears the pending flag", pending is False)

# 3. guest clock survived the pause (TSC case): only the wall clock jumped
pos, state, adopt_q, next_scan, pending, lw, lm = asyncio.run(run_guard(6470.0, 6470.0))
check("stalled loop triggers a resync too", len(calls) == 1, "calls=%d" % len(calls))
check("stalled loop resets the position", pos == {} and state == "idle")

# 4. the threshold is the one in monitor.py, not a stale copy
pos, state, adopt_q, next_scan, pending, lw, lm = asyncio.run(
    run_guard(FREEZE_SECONDS + 2.0, 1.0))
check("skew just over the threshold triggers", len(calls) == 1, "calls=%d" % len(calls))
pos, state, adopt_q, next_scan, pending, lw, lm = asyncio.run(
    run_guard(FREEZE_SECONDS, 1.0))
check("skew just under the threshold does not", not calls, "calls=%d" % len(calls))

# 5. the exchange is unreachable: keep the position, retry next poll
resync_result = ([], False)
pos, state, adopt_q, next_scan, pending, lw, lm = asyncio.run(run_guard(6470.0, 2.0))
check("failed resync keeps the position", pos == {"symbol": "OLD-USDT"} and state == "selling")
check("failed resync stays pending", pending is True)
pos, state, adopt_q, next_scan, pending, lw, lm = asyncio.run(
    run_guard(2.0, 2.0, last=(1002.0, 502.0), pending=pending))
check("failed resync is retried", len(calls) == 1, "calls=%d" % len(calls))
check("still pending after the retry", pending is True)
resync_result = ([("TEST-USDT", 5)], True)

print()
if ok:
    print("PASS: freeze detected from wall-vs-monotonic skew, state re-synced")
    sys.exit(0)
print("FAIL: freeze detection is wrong")
sys.exit(1)
