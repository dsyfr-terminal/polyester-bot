"""Fee math: the bot pays the exchange's real fees, not an assumed 0.20%.

Covers:
  1. the round trip is twice the per-side maker fee (VIP3: 0.05 -> 0.10)
  2. the gap floor is fee-driven: 2 x maker + margin, unless GAP_MIN_PCT is set
  3. cycle fees use the real per-side rates, half of the old assumption
  4. entry price: gap buys always penny; fallback escalates bid -> penny -> taker
  5. the fallback timeout shrinks once the ladder is engaged

Usage:  python tests/test_fees.py
Exit 0 = pass, Exit 1 = fail
"""

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

from decimal import Decimal

FEES = {"ARB-USDT": (Decimal("0.05"), Decimal("0.105")),
        "SHIB-USDT": (Decimal("0.05"), Decimal("0.105"))}

check("maker fee comes from the exchange",
      monitor.maker_fee_pct(FEES, "ARB-USDT") == Decimal("0.05"),
      str(monitor.maker_fee_pct(FEES, "ARB-USDT")))
check("an unknown pair falls back to the observed rate",
      monitor.maker_fee_pct({}, "XYZ-USDT") == monitor.DEFAULT_MAKER_PCT,
      str(monitor.DEFAULT_MAKER_PCT))
check("round trip is twice the maker fee",
      monitor.rt_fee_pct(FEES, "ARB-USDT") == Decimal("0.10"),
      str(monitor.rt_fee_pct(FEES, "ARB-USDT")))
check("the gap floor is fee driven",
      monitor.gap_min_pct(None, FEES, "ARB-USDT") == Decimal("0.11"),
      str(monitor.gap_min_pct(None, FEES, "ARB-USDT")))
check("an explicit GAP_MIN_PCT still wins",
      monitor.gap_min_pct(Decimal("0.21"), FEES, "ARB-USDT") == Decimal("0.21"),
      str(monitor.gap_min_pct(Decimal("0.21"), FEES, "ARB-USDT")))

fees = monitor.round_trip_fees(Decimal("1000"), Decimal("1000"), Decimal("0.05"))
check("cycle fees are the real rate, half the old assumption",
      fees == Decimal("1.0"), str(fees))

META = {"tick": Decimal("0.0001")}
bid, ask = Decimal("0.1670"), Decimal("0.1690")

check("a gap buy always pennies the bid",
      monitor.entry_price("GAP", bid, ask, META, False, False)
      == Decimal("0.1671"))
check("a fallback buy rests at the bid while calm",
      monitor.entry_price("VOLUME", bid, ask, META, False, False)
      == Decimal("0.1670"))
check("the ladder pennies the fallback bid",
      monitor.entry_price("VOLUME", bid, ask, META, True, False)
      == Decimal("0.1671"))
check("the ladder crosses the spread when the book stays cold",
      monitor.entry_price("VOLUME", bid, ask, META, True, True) == ask,
      str(monitor.entry_price("VOLUME", bid, ask, META, True, True)))
check("a penny never crosses into the ask",
      monitor.entry_price("GAP", Decimal("0.1689"), Decimal("0.1690"), META,
                          False, False) == Decimal("0.1689"),
      str(monitor.entry_price("GAP", Decimal("0.1689"), Decimal("0.1690"),
                              META, False, False)))

check("the fallback timeout shrinks on the ladder",
      monitor.buy_timeout_for("VOLUME", 2) == monitor.FALLBACK_BUY_TIMEOUT,
      str(monitor.buy_timeout_for("VOLUME", 2)))
check("a calm fallback keeps the long timeout",
      monitor.buy_timeout_for("VOLUME", 0) == monitor.BUY_TIMEOUT_SECONDS,
      str(monitor.buy_timeout_for("VOLUME", 0)))
check("gap buys always keep the long timeout",
      monitor.buy_timeout_for("GAP", 9) == monitor.BUY_TIMEOUT_SECONDS,
      str(monitor.buy_timeout_for("GAP", 9)))

print()
print("PASS: the bot pays the exchange's real fees" if ok else "FAIL")
sys.exit(0 if ok else 1)
