"""Daily equity report from real balances and fresh book mids.

Total = USDT + sum(held * mid). A snapshot is written for tomorrow's delta.

  1. equity is the sum of quote + positions marked at mid,
  2. an unreadable book skips the position, not the report,
  3. a missing snapshot is "no prior snapshot", not a crash,
  4. a broken balances read never escapes the report.

Usage:  python tests/test_equity.py
Exit 0 = pass, Exit 1 = fail
"""

import asyncio
import os
import sys
import types
import tempfile
from decimal import Decimal

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

WALLETS = [
    {"symbol": "USDT", "trading": "1000.00", "available": "1000.00"},
    {"symbol": "HYPE", "trading": "10", "available": "10"},
    {"symbol": "DOGE", "trading": "0", "available": "0"},
]
alerts = []
monitor.notify = lambda emoji, action, *lines: alerts.append((action, lines))
monitor.wallet = lambda base=None: "USDT 1000.00"
TMPDIR = tempfile.mkdtemp(prefix="polyester-test-")
STATE = os.path.join(TMPDIR, "equity_state.json")
monitor.state_file = lambda name: STATE


def fake_book_factory(mid_map, fail=()):
    async def fake_book(c, sym, depth=1):
        if sym in fail:
            raise RuntimeError("book down")
        mid = Decimal(str(mid_map[sym]))
        bid, ask = mid - Decimal("2"), mid + Decimal("2")

        class L:
            def __init__(self, p, q):
                self.price, self.qty = Decimal(p), Decimal(q)
        return types.SimpleNamespace(bids=[L(str(bid), "10")],
                                     asks=[L(str(ask), "10")],
                                     available=True)
    return fake_book


def run(mid_map, wallets=None, fail=()):
    alerts.clear()
    monitor.book = fake_book_factory(mid_map, fail)
    monitor.rest_balances = lambda: (wallets if wallets is not None else WALLETS)
    if os.path.exists(STATE):
        os.remove(STATE)
    asyncio.run(monitor.equity_report(None))
    return alerts


notes = run({"HYPE-USDT": 220})
check("equity is quote plus positions marked at mid",
      notes[0][0] == "EQUITY" and "total 3,200.00 USDT" in notes[0][1][0],
      str(notes[0][1]))
check("a missing snapshot is reported, not fatal",
      "no prior snapshot" in notes[0][1][0], str(notes[0][1]))
check("the snapshot is written for tomorrow",
      os.path.exists(STATE) and open(STATE).read().count("3200") == 1,
      open(STATE).read() if os.path.exists(STATE) else "missing")

alerts.clear()
monitor.rest_balances = lambda: WALLETS
monitor.book = fake_book_factory({"HYPE-USDT": 220})
asyncio.run(monitor.equity_report(None))
notes = alerts
check("tomorrow's report carries the 24h delta",
      "24h +0.00 USDT" in notes[0][1][0], str(notes[0][1]))

notes = run({"HYPE-USDT": 220}, fail=("HYPE-USDT",))
check("an unreadable book skips the position, not the report",
      notes and "total 1,000.00 USDT" in notes[0][1][0], str(notes[0][1]))

def broken():
    raise RuntimeError("balances down")

alerts.clear()
monitor.rest_balances = broken
try:
    asyncio.run(monitor.equity_report(None))
    check("a broken balances read never escapes the report", True)
except RuntimeError:
    check("a broken balances read never escapes the report", False)

print()
print("PASS: the daily equity line tells the truth" if ok else "FAIL")
sys.exit(0 if ok else 1)
