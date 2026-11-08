"""One-shot: place a full-balance BUY one tick above best bid on BTC-USDT."""

import asyncio
import os
import uuid
from decimal import Decimal, ROUND_FLOOR

from polyester import POLYESTER_TESTNET_ENVIRONMENT, AsyncPolyester

if __package__:
    from .paths import env_file
else:
    from paths import env_file


def load_env():
    try:
        with open(env_file(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


load_env()

SYMBOL = "BTC-USDT"
STEP = Decimal("0.00000001")
FEE_BUFFER = Decimal("1.0015")

CID = "man1-" + uuid.uuid4().hex[:16]


async def main():
    c = AsyncPolyester(environment=POLYESTER_TESTNET_ENVIRONMENT,
                       api_key_id=os.environ["POLYESTER_API_KEY_ID"],
                       api_private_key=os.environ["POLYESTER_ED25519_SEED"],
                       default_account_id=os.environ["POLYESTER_ACCOUNT_ID"])
    try:
        await c.wait_for_catalogs()
        b = await c.balances.list()
        usdt = next(a for a in b.balances if a.asset_id == 1)
        avail = Decimal(usdt.available) / Decimal(10 ** 18)

        book = await c.orderbook.get(symbol=SYMBOL, depth=1)
        bid = book.bids[0].price.as_decimal()
        price = bid + Decimal("0.01")

        qty = (avail / (price * FEE_BUFFER)).quantize(STEP, rounding=ROUND_FLOOR)
        notional = qty * price

        print(f"USDT available : {avail}")
        print(f"best bid       : {bid}")
        print(f"order price    : {price}  (bid + 1 tick)")
        print(f"order qty      : {qty} BTC")
        print(f"notional       : ~{notional.quantize(Decimal('0.01'))} USDT")
        print(f"maker fee 0.1% : ~{(notional * Decimal('0.001')).quantize(Decimal('0.01'))} USDT on fill")

        res = await c.orders.batch_create(symbol=SYMBOL, items=[{
            "symbol": SYMBOL, "side": "buy", "order_type": "limit", "tif": "gtc",
            "qty": format(qty, "f"), "price": format(price, "f"),
            "client_order_id": CID,
        }], request_id="man1-" + uuid.uuid4().hex[:16])
        for item in res.results:
            print("RESULT:", item.status, getattr(item, "code", "") or "",
                  getattr(item, "order_id", ""))
    finally:
        await c.aclose()


asyncio.run(main())
