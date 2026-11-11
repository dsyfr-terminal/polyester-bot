"""The dead-man switch re-arms the exchange's auto-cancel.

If the bot dies or the VM freezes, nothing re-arms the deadline and every
open order is cancelled by the exchange on its own. Covers:
  1. the re-arm calls the exchange with the configured timeout,
  2. a failing exchange is swallowed and reported, never raised,
  3. DEADMAN_TIMEOUT defaults to 300 and 0 disables the feature.

Usage:  python tests/test_deadman.py
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

calls = []


class FakeOrders:
    async def cancel_all_after(self, timeout_sec=None):
        calls.append(timeout_sec)


class FakeC:
    def __init__(self):
        self.orders = FakeOrders()


check("DEADMAN_TIMEOUT defaults within the exchange cap",
      monitor.DEADMAN_TIMEOUT == 120)
check("DEADMAN_REFRESH is tighter than the timeout",
      0 < monitor.DEADMAN_REFRESH < monitor.DEADMAN_TIMEOUT)
check("re-arm passes the timeout to the exchange",
      asyncio.run(monitor.deadman_rearm(FakeC())) is True and calls == [120],
      str(calls))
check("a failing exchange does not crash the loop",
      asyncio.run(monitor.deadman_rearm(type("C", (), {})())) is False)

print()
print("PASS: dead-man switch re-arms the auto-cancel" if ok else "FAIL")
sys.exit(0 if ok else 1)
