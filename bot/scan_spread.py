"""Rank every pair by bid-ask gap vs the 0.2% maker round-trip fee."""

import asyncio
import os
from decimal import Decimal

from polyester import POLYESTER_TESTNET_ENVIRONMENT, AsyncPolyester

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    try:
        with open(os.path.join(HERE, ".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


load_env()

RT_FEE_PCT = Decimal("0.20")  # maker 0.1% buy + 0.1% sell


def dec(v):
    return v.as_decimal() if hasattr(v, "as_decimal") else Decimal(str(v))


async def main():
    c = AsyncPolyester(environment=POLYESTER_TESTNET_ENVIRONMENT,
                       api_key_id=os.environ["POLYESTER_API_KEY_ID"],
                       api_private_key=os.environ["POLYESTER_ED25519_SEED"])
    try:
        await c.wait_for_catalogs()
        cfg = await c.market_data.get_spot_config()
        pairs = [p for p in cfg.raw["pairs"]
                 if p.get("status") in ("PAIR_STATUS_ENABLED", "PAIR_STATUS_POST_ONLY")]
        rows = []
        for p in pairs:
            sym = p["symbol"]
            try:
                book = await c.orderbook.get(symbol=sym, depth=1)
                if not book.bids or not book.asks:
                    continue
                bid, ask = dec(book.bids[0].price), dec(book.asks[0].price)
                if bid <= 0 or ask <= bid:
                    continue
                mid = (bid + ask) / 2
                spread_pct = (ask - bid) / mid * 100
                bid_qty, ask_qty = dec(book.bids[0].qty), dec(book.asks[0].qty)
                rows.append((sym, bid, ask, spread_pct, spread_pct - RT_FEE_PCT,
                             bid_qty * bid, ask_qty * ask))
            except Exception as e:
                print("skip", sym, repr(e), flush=True)

        rows.sort(key=lambda r: r[4], reverse=True)
        print(f"\n{'pair':14} {'bid':>12} {'ask':>12} {'gap%':>7} "
              f"{'net%':>7} {'bid$':>10} {'ask$':>10}  verdict")
        for sym, bid, ask, sp, net, bn, an in rows:
            verdict = ("PROFIT" if net > 0 and min(bn, an) >= 50 else
                       "profit*thin" if net > 0 else "fee-loss")
            print(f"{sym:14} {bid:>12} {ask:>12} {sp:>7.3f} "
                  f"{net:>7.3f} {float(bn):>10.1f} {float(an):>10.1f}  {verdict}")
    finally:
        await c.aclose()


asyncio.run(main())
