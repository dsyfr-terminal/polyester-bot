"""Server-side take-profit triggers.

If the sandbox freezes (2am cooldown, machine asleep) with a position held,
the exchange itself sells it when price rallies TP_TRIGGER_PCT above entry. Covers:

  1. armed at cycle entry for the full posted quantity, side=sell,
     trigger_type=take_profit - the exchange sells without the bot,
  2. known basis -> trigger above that entry; unknown basis (claims) -> above
     the current ask,
  3. TP_TRIGGER_PCT <= 0 disarms the feature entirely,
  4. a failing triggers API never breaks the cycle: the bot falls back to
     managing the position locally,
  5. the close path disarms the trigger once the position is managed away.

Usage:  python tests/test_tp_trigger.py
Exit 0 = pass, Exit 1 = fail
"""

import asyncio
import os
import sys
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

M = {"tick": Decimal("0.0001"), "step": Decimal("0.1"),
     "min_notional": Decimal("5"), "min_qty": Decimal("1"), "scale": 8}


class FakePrice:
    def __init__(self, price):
        self.price = price


class FakeBook:
    def __init__(self, bids, asks):
        self.bids = [FakePrice(b) for b in bids]
        self.asks = [FakePrice(p) for p in asks]


class FakeTriggers:
    def __init__(self, fail=False):
        self.creates = []
        self.cancels = []
        self.fail = fail

    async def create(self, **kwargs):
        if self.fail:
            raise RuntimeError("triggers unavailable")
        self.creates.append(kwargs)
        return type("R", (), {"trigger_id": 4242, "status": "created"})()

    async def cancel(self, trigger_id=None):
        if self.fail:
            raise RuntimeError("triggers unavailable")
        self.cancels.append(trigger_id)


class FakeC:
    def __init__(self, ask=None, fail=False):
        self.triggers = FakeTriggers(fail)
        self._ask = [ask] if ask else []

    @property
    def orderbook(self):
        ask = self._ask[0]

        class Ob:
            async def get(self, symbol=None, depth=None):
                return FakeBook([], [ask])
        return Ob()


monitor.notify = lambda emoji, action, *lines: None
monitor.wallet = lambda base=None: "USDT 2.99"
NEW_PCT = monitor.TP_TRIGGER_PCT
new_pos = lambda px: {"symbol": "ARB-USDT", "buy_px": px, "qty": Decimal("1000"),
                      "tp_tid": None}

pos = new_pos(Decimal("0.1673"))
c = FakeC(ask="0.1690")
res = asyncio.run(monitor.place_tp_trigger(c, pos, M))
check("a TP trigger is armed for the held position",
      res is not None and len(c.triggers.creates) == 1)
cr = c.triggers.creates[0] if c.triggers.creates else {}
check("it sells the full posted qty as a take_profit",
      cr.get("side") == "sell" and cr.get("trigger_type") == "take_profit"
      and str(cr.get("qty")).startswith("1000"), str(cr))
check("a market child executes on the server", cr.get("order_type") == "market")
check("the trigger sits TP_TRIGGER_PCT above entry",
      cr.get("trigger_price") == str(Decimal("0.1673") * (1 + NEW_PCT / 100)),
      str(cr.get("trigger_price")))
check("the client id is ours", str(cr.get("client_trigger_id") or "").startswith("gap1-tp-"),
      str(cr.get("client_trigger_id")))
check("the trigger id is kept on the position", pos["tp_tid"] == 4242)

pos = new_pos(Decimal("0"))
c = FakeC(ask="0.1690")
asyncio.run(monitor.place_tp_trigger(c, pos, M))
cr = c.triggers.creates[0]
check("unknown basis (claims) anchors to the current ask",
      cr.get("trigger_price") == str(Decimal("0.1690") * (1 + NEW_PCT / 100)),
      str(cr.get("trigger_price")))

pos = new_pos(Decimal("0.1673"))
c = FakeC(fail=True)
res = asyncio.run(monitor.place_tp_trigger(c, pos, M))
check("a broken triggers API does not break the cycle",
      res is None and pos["tp_tid"] is None)

pos = new_pos(Decimal("0.1673"))
pos["tp_tid"] = 4242
c = FakeC(fail=True)
check("disarm tolerates a broken triggers API",
      asyncio.run(monitor.cancel_tp_trigger(c, pos)) is None
      and pos["tp_tid"] is None)

pos = new_pos(Decimal("0.1673"))
pos["tp_tid"] = None
check("disarm with no armed trigger is a no-op",
      asyncio.run(monitor.cancel_tp_trigger(FakeC(), pos)) is None)

old_pct = monitor.TP_TRIGGER_PCT
monitor.TP_TRIGGER_PCT = Decimal("0")
c = FakeC()
res = asyncio.run(monitor.place_tp_trigger(c, new_pos(Decimal("0.1673")), M))
monitor.TP_TRIGGER_PCT = old_pct
check("TP_TRIGGER_PCT=0 arms nothing", res is None and c.triggers.creates == [])

orders = [
    {"clientOrderId": "gap1-s-01", "symbol": "ARB-USDT"},
    {"clientOrderId": "gap1-b-02", "symbol": "ARB-USDT"},
    {"clientOrderId": "gap1-x-03", "symbol": "BTC-USDT"},
    {"clientOrderId": "other-c99", "symbol": "ARB-USDT"},
]


class SweepC:
    def __init__(self):
        self.cancelled = []
        self.orders = type("Orders", (), {})()

        async def cancel(key=None, symbol=None):
            self.cancelled.append(symbol)
        self.orders.cancel = cancel


sweep = SweepC()
monitor.rest_open_orders = lambda: {"orders": orders}
n = asyncio.run(monitor.cancel_stale_symbol_orders(sweep, "ARB-USDT"))
check("the ask a trigger beat is swept",
      n == 2 and sweep.cancelled == ["ARB-USDT", "ARB-USDT"],
      f"n={n} cancelled={sweep.cancelled}")


def boom():
    raise RuntimeError("orders unreadable")


monitor.rest_open_orders = boom
n = asyncio.run(monitor.cancel_stale_symbol_orders(SweepC(), "ARB-USDT"))
monitor.rest_open_orders = lambda: {"orders": []}
check("an unreadable orders list does not break cycle close", n == 0)

print()
print("PASS: the exchange disarms our position risk itself" if ok else "FAIL")
sys.exit(0 if ok else 1)
