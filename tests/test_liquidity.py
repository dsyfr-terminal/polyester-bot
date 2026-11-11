"""The fallback prefers pairs where trades actually happen.

A wide dead spread fills at zero; the buy has to land. The candidate pair is
picked by recent trade flow, not by the widest displayed gap.

  1. the lively pair wins over the tighter/wider gap,
  2. zero trades lose to real trades,
  3. an unreadable tape does not break the pick,
  4. all-dead tape keeps the tightest gap (fb[0]).

Usage:  python tests/test_liquidity.py
Exit 0 = pass, Exit 1 = fail
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "bot"))

ok = True


def check(label, good, detail=""):
    global ok
    ok = ok and good
    print("%-6s %s%s" % ("PASS" if good else "FAIL", label,
                         (" -> " + detail) if detail else ""))


try:
    import monitor
except Exception as e:
    print("FAIL cannot import monitor:", repr(e))
    sys.exit(1)

ROW = lambda gap, sym, depth: (gap, sym, None, None, depth, depth)


def fake_md(counts, fail=()):
    class MD:
        async def get_trades(self, symbol=None, limit=100):
            if symbol in fail:
                raise RuntimeError("tape down")
            return type("R", (), {"trades": list(range(counts.get(symbol, 0)))})()
    return type("C", (), {"market_data": MD()})()


fb = [ROW("0.050", "DEAD-USDT", 2000), ROW("0.040", "LIVE-USDT", 2000),
      ROW("0.030", "QUIET-USDT", 2000), ROW("0.020", "APATHETIC-USDT", 2000)]

got = asyncio.run(monitor.pick_best_liquid(fake_md({"DEAD-USDT": 0, "LIVE-USDT": 14,
                                                    "QUIET-USDT": 3, "APATHETIC-USDT": 0}),
                                           fb))
check("the lively pair wins over the tighter gap",
      got[1] == "LIVE-USDT", got[1])

got = asyncio.run(monitor.pick_best_liquid(fake_md({"DEAD-USDT": 0, "LIVE-USDT": 0,
                                                    "QUIET-USDT": 8, "APATHETIC-USDT": 0}),
                                           fb))
check("zero trades lose to real trades", got[1] == "QUIET-USDT", got[1])

got = asyncio.run(monitor.pick_best_liquid(fake_md({}, fail=("DEAD-USDT",)), fb))
check("a broken tape does not break the pick", got[1] == "DEAD-USDT", got[1])

got = asyncio.run(monitor.pick_best_liquid(fake_md({}), fb))
check("all-time-dead tape keeps the tightest gap", got[1] == "DEAD-USDT", got[1])

print()
print("PASS: the fallback cycles the pairs that actually trade" if ok else "FAIL")
sys.exit(0 if ok else 1)
