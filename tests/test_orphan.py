"""The orphan-clear loop stops hammering every poll.

Real inventory, unsizable sell (available=0), held above min notional: the
code used to cancel_all and alert hourly-spam every 2 seconds.

  1. the first failed attempt cancels once and alerts once,
  2. a poll seconds later does nothing,
  3. an hour later it tries again.

Usage:  python tests/test_orphan.py
Exit 0 = pass, Exit 1 = fail
"""

import asyncio
import os
import sys
import types
import time
from decimal import Decimal, ROUND_FLOOR

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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

META = {"ARB-USDT": {"tick": Decimal("0.0001"), "step": Decimal("0.1"),
                     "min_notional": Decimal("5"), "min_qty": Decimal("1"), "scale": 8}}
NOW = [1000.0]
alerts = []
cancel_alls = []


class FakeTime:
    def time(self):
        return NOW[0]

    def monotonic(self):
        return 500.0 + NOW[0]

    def strftime(self, *args, **kwargs):
        return time.strftime(*args, **kwargs)

    def gmtime(self, *args, **kwargs):
        return time.gmtime(*args, **kwargs)


class FakeOrders:
    async def cancel_all(self, symbol=None, request_id=None):
        cancel_alls.append(symbol)


class FakeC:
    def __init__(self):
        self.orders = FakeOrders()


class FakeL:
    def __init__(self, p, q):
        self.price, self.qty = Decimal(p), Decimal(q)


def fake_book(c, sym, depth=10, max_age=15.0):
    async def _get():
        return types.SimpleNamespace(bids=[FakeL("0.1670", "5000")],
                                     asks=[FakeL("0.1690", "5000")],
                                     available=True)
    return _get()


monitor.time = FakeTime()
monitor.book = fake_book
monitor.rest_balances = lambda: [{"symbol": "ARB",
                                  "trading": "50000", "available": "0"}]
monitor.notify = lambda emoji, action, *lines: alerts.append(action)
monitor.wallet = lambda base=None: "USDT 100"

pos = {"symbol": "ARB-USDT", "buy_px": Decimal("0.1673"), "qty": Decimal("50000")}

NOW[0] = 1000.0
cancel_alls.clear()
alerts.clear()
r = asyncio.run(monitor.place_sell(FakeC(), pos, META))
check("the first failed attempt cancels once and alerts once",
      r is False and cancel_alls == ["ARB-USDT"] and "ORPHAN CLEARED" in alerts,
      f"r={r} cancels={cancel_alls} alerts={alerts}")

NOW[0] += 2
before = list(cancel_alls)
before_alerts = list(alerts)
r = asyncio.run(monitor.place_sell(FakeC(), pos, META))
check("a poll seconds later does nothing",
      r is False and cancel_alls == before and alerts == before_alerts,
      f"r={r} cancels={cancel_alls} alerts={alerts}")

NOW[0] += 3600
r = asyncio.run(monitor.place_sell(FakeC(), pos, META))
check("an hour later it tries again",
      cancel_alls == ["ARB-USDT", "ARB-USDT"] and alerts.count("ORPHAN CLEARED") == 2,
      f"cancels={cancel_alls} alerts={alerts}")

print()
print("PASS: the stuck loop runs once an hour, not once a poll" if ok else "FAIL")
sys.exit(0 if ok else 1)
