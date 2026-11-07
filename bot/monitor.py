"""Gap hunter + executor.

Scans every USDT-quoted pair for bid-ask gaps that beat the 0.2% maker
round-trip fee, strikes fast (buy at bid, ask just under the offer), manages
the ask with the bail-out rule, and Telegram-alerts every step.

Bail-out rule (user spec): while our ask rests, if more than 2 better
(lower) ask levels sit above... i.e. below our price for >60s, cancel and
re-ask one tick under the current best ask (maker exit, even at a loss).

Adopted at startup: any asset with a Trading balance that has a USDT pair
(e.g. the BTC from the filled manual order, plus XRP).
"""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, ROUND_FLOOR

import nacl.signing
from polyester import POLYESTER_TESTNET_ENVIRONMENT, AsyncPolyester
from polyester.models import ClientOrderId

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

import sys  # noqa: E402
try:  # Windows consoles default to cp1252; arrows/emoji in alerts would crash
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API = "https://api.testnet.polyester.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
KEY = os.environ["POLYESTER_API_KEY_ID"]
_SK = nacl.signing.SigningKey(bytes.fromhex(os.environ["POLYESTER_ED25519_SEED"]))
ACCOUNT = os.environ.get("POLYESTER_ACCOUNT_ID", "MUYiAxF1Kkv")

QUOTE = "USDT"
RT_FEE_PCT = Decimal("0.20")          # maker 0.1% x2
GAP_MIN_PCT = Decimal(os.environ.get("GAP_MIN_PCT", "0.21"))
DEPTH_MIN_USD = Decimal(os.environ.get("DEPTH_MIN_USD", "50"))
# Volume-fallback mode: when no pair clears the fee bar, cycle the deepest
# tightest pair anyway, accepting a small per-cycle loss to keep volume up.
FALLBACK_SYMBOL = os.environ.get("FALLBACK_SYMBOL", "").strip()  # empty = auto-pick
FALLBACK_MIN_GAP_PCT = Decimal(os.environ.get("FALLBACK_MIN_GAP_PCT", "0.05"))
FALLBACK_DEPTH_MIN_USD = Decimal(os.environ.get("FALLBACK_DEPTH_MIN_USD", "1000"))
FALLBACK_COOLDOWN = float(os.environ.get("FALLBACK_COOLDOWN", "60"))
FALLBACK_PENNY = os.environ.get("FALLBACK_PENNY", "0") == "1"  # 1 = fill faster, lose more
SCAN_SECONDS = float(os.environ.get("SCAN_SECONDS", "10"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "2"))
PERSIST_SECONDS = float(os.environ.get("PERSIST_SECONDS", "60"))
BETTER_ASKS_LIMIT = int(os.environ.get("BETTER_ASKS_LIMIT", "2"))
BUY_TIMEOUT_SECONDS = float(os.environ.get("BUY_TIMEOUT_SECONDS", "180"))
FEE_BUFFER = Decimal("1.0015")        # fee reserve headroom when sizing buys

TERMINAL = {"filled", "canceled", "rejected", "expired"}

# Known cost basis for adopted holdings (from the filled manual buy).
COST_BASIS = {"BTC-USDT": Decimal("76227.38"), "ARB-USDT": Decimal("0.1684")}


def pdec(v):
    if v is None or v == "":
        return Decimal(0)
    return v.as_decimal() if hasattr(v, "as_decimal") else Decimal(str(v))


def qdec(v, scale):
    if v is None or v == "":
        return Decimal(0)
    return v.as_decimal(scale) if hasattr(v, "as_decimal") else Decimal(str(v))


def floor_step(x, step):
    step = Decimal(str(step))
    if step <= 0:
        return x
    return (x / step).to_integral_value(rounding=ROUND_FLOOR) * step


_last_ntfy = [0.0]


def alert(text, priority=False):
    print(text, flush=True)
    # never block the trading loop on alert I/O
    import threading

    def send():
        topic = os.environ.get("NTFY_TOPIC", "").strip()
        # ntfy's free tier 429s on bursts: routine alerts max ~1/min, priority always
        if topic and (priority or time.time() - _last_ntfy[0] >= 60):
            _last_ntfy[0] = time.time()
            try:
                req = urllib.request.Request(
                    "https://ntfy.sh/" + topic, data=text.encode(),
                    headers={"Title": "Polyester Hunter", "Tags": "robot"})
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                print("ntfy failed:", repr(e), flush=True)
        # telegram always gets every alert; only ntfy is rate-limited
        tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if tok and chat:
            try:
                req = urllib.request.Request(
                    "https://api.telegram.org/bot" + tok + "/sendMessage",
                    data=urllib.parse.urlencode({"chat_id": chat, "text": text}).encode())
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                print("telegram failed:", repr(e), flush=True)
        hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        if hook:
            try:
                req = urllib.request.Request(
                    hook, data=json.dumps({"content": text}).encode(),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                print("discord failed:", repr(e), flush=True)

    threading.Thread(target=send, daemon=True).start()


def rest_open_orders():
    """All open orders via signed REST (carries clientOrderId + symbol)."""
    ts = str(int(time.time() * 1000))
    path = "/v1/orders/open"
    canon = "\n".join([ts, "GET", path, "", hashlib.sha256(b"").hexdigest()])
    req = urllib.request.Request(API + path, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Origin", "https://testnet.polyester.com")
    req.add_header("X-API-KEY-ID", KEY)
    req.add_header("X-API-TIMESTAMP", ts)
    req.add_header("X-API-SIGNATURE", _SK.sign(canon.encode()).signature.hex())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b'{"orders": []}')


def rest_balances():
    """GET /v1/balances via signed REST (rows carry asset symbols)."""
    ts = str(int(time.time() * 1000))
    path = "/v1/balances"
    canon = "\n".join([ts, "GET", path, "", hashlib.sha256(b"").hexdigest()])
    req = urllib.request.Request(API + path, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Origin", "https://testnet.polyester.com")
    req.add_header("X-API-KEY-ID", KEY)
    req.add_header("X-API-TIMESTAMP", ts)
    req.add_header("X-API-SIGNATURE", _SK.sign(canon.encode()).signature.hex())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["balances"]


async def place(c, sym, side, qty, price, post_only=False):
    cid = "gap1-" + side[0] + "-" + uuid.uuid4().hex[:12]
    item = {"symbol": sym, "side": side, "order_type": "limit", "tif": "gtc",
            "qty": format(qty, "f"), "price": format(price, "f"),
            "client_order_id": cid}
    if post_only:
        item["post_only"] = True
    res = await c.orders.batch_create(symbol=sym, items=[item],
                                      request_id="gap1-" + uuid.uuid4().hex[:12])
    r = res.results[0]
    if r.status == "rejected":
        return None, str(getattr(r, "code", "") or "REJECTED")
    return cid, None


async def order_state(c, sym, cid, scale):
    """(status_lower, cum_qty, avg_px) for one of our orders."""
    res = await c.orders.get(key=ClientOrderId(cid))
    o = getattr(res, "order", None)
    if o is None:
        return "pending", Decimal(0), Decimal(0)
    return (str(o.status).lower(), qdec(o.cum_qty, scale), pdec(o.avg_px))


async def scan_books(c, pairs):
    """Every USDT pair with a two-sided book: (gap_pct, sym, bid, ask, bid$, ask$)."""
    rows = []
    for sym in pairs:
        try:
            book = await c.orderbook.get(symbol=sym, depth=1)
            if not book.bids or not book.asks:
                continue
            bid, ask = pdec(book.bids[0].price), pdec(book.asks[0].price)
            if bid <= 0 or ask <= bid:
                continue
            gap = (ask - bid) / ((ask + bid) / 2) * 100
            bid_usd = pdec(book.bids[0].qty) * bid
            ask_usd = pdec(book.asks[0].qty) * ask
            rows.append((gap, sym, bid, ask, bid_usd, ask_usd))
        except Exception as e:
            print("scan skip", sym, repr(e), flush=True)
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


async def place_sell(c, pos, meta):
    sym = pos["symbol"]
    tick, step, min_not = meta[sym]["tick"], meta[sym]["step"], meta[sym]["min_notional"]
    book = await c.orderbook.get(symbol=sym, depth=10)
    if not book.asks:
        return False
    best_ask = pdec(book.asks[0].price)
    basis = pos.get("buy_px") or Decimal(0)
    if pos.get("bail"):
        target = best_ask - tick                      # emergency exit: undercut
    elif pos.get("join_ask"):
        target = max(best_ask, basis) if basis > 0 else best_ask  # min-loss: join touch
    elif basis > 0:
        target = max(best_ask - tick, basis)          # never ask below known cost
    else:
        target = best_ask - tick
    q = floor_step(pos["qty"], step)
    held = None
    try:  # never advertise more than we actually hold
        base = sym.split("-")[0]
        for row in rest_balances():
            if row.get("symbol") == base:
                held = floor_step(Decimal(row.get("trading") or 0), step)
                q = min(q, floor_step(Decimal(row.get("available") or 0), step))
                break
    except Exception:
        held = None
    if q <= 0 or q * best_ask < min_not:
        # held-but-unavailable means one of our own orders is squatting on it
        if held and held * best_ask >= min_not:
            try:
                await c.orders.cancel_all(symbol=sym,
                                          request_id="gap1-ca-" + uuid.uuid4().hex[:10])
                pos["skip_noted"] = False
                alert(f"{sym}: {held} held but reserved by an orphan order -> "
                      f"clearing it, will retry")
            except Exception as e:
                print("orphan clear failed:", repr(e), flush=True)
        elif not pos.get("skip_noted"):
            pos["skip_noted"] = True  # alert once per position, never flood
            alert(f"{sym}: sell skipped, below pair minimums (qty {q})")
        return False
    # prefer undercutting the touch (maker, first in queue); fall back to joining
    po = not pos.get("no_postonly")
    for price, post in ((target, po), (best_ask, False)):
        if price <= 0:
            continue
        cid, err = await place(c, sym, "sell", q, price, post_only=po)
        if cid:
            pos["sell_cid"], pos["ask_px"] = cid, price
            pos["better"], pos["ask_at"] = {}, time.time()
            alert(f"{sym}: ASK {q} @ {price} (bought @ {pos.get('buy_px', '?')})")
            return True
    if err and "insufficient" in err.lower():
        # an orphaned live order is holding our funds — clear the symbol, retry next poll
        try:
            await c.orders.cancel_all(symbol=sym,
                                      request_id="gap1-ca-" + uuid.uuid4().hex[:10])
            pos["skip_noted"] = False
            alert(f"{sym}: insufficient funds -> cleared orphaned orders, will retry")
        except Exception as e:
            print("orphan clear failed:", repr(e), flush=True)
    elif pos.get("last_err") != err:  # one alert per distinct error, never a flood
        pos["last_err"] = err
        alert(f"{sym}: ask placement rejected: {err}")
    return False


def season_volume():
    """Season volume from the checkpoint scorecard, or None if unavailable."""
    try:
        out = subprocess.run(
            [sys.executable, os.path.join(HERE, "polyester_farm.py"), "checkpoints"],
            capture_output=True, text=True, timeout=180, cwd=HERE)
        m = re.search(r"volume=\$([\d.]+)", out.stdout or "")
        if m:
            return Decimal(m.group(1))
        print("volume parse failed:", (out.stderr or out.stdout)[-200:], flush=True)
    except Exception as e:
        print("volume baseline fetch failed:", repr(e), flush=True)
    return None


def rest_asset_balance(base):
    """(trading, available) for one asset via signed REST. Raises on failure."""
    for row in rest_balances():
        if row.get("symbol") == base:
            return (Decimal(row.get("trading") or 0),
                    Decimal(row.get("available") or 0))
    return Decimal(0), Decimal(0)


async def base_balance(c, sym, m):
    """Total base held (trading) via REST. Raises on failure."""
    base = sym.split("-")[0]
    held, _ = rest_asset_balance(base)
    return held


async def main():
    # tolerate WiFi blips at boot: retry client + catalog hydration
    c = None
    for attempt in range(12):
        try:
            c = AsyncPolyester(environment=POLYESTER_TESTNET_ENVIRONMENT,
                               api_key_id=KEY,
                               api_private_key=os.environ["POLYESTER_ED25519_SEED"],
                               default_account_id=ACCOUNT)
            await c.wait_for_catalogs()
            break
        except Exception as e:
            print(f"startup retry {attempt + 1}/12: {repr(e)}", flush=True)
            if c is not None:
                try:
                    await c.aclose()
                except Exception:
                    pass
                c = None
            await asyncio.sleep(5)
    if c is None:
        raise SystemExit("API unreachable after 12 retries; supervisor will restart us")
    try:
        await c.wait_for_catalogs()
        cfg = (await c.market_data.get_spot_config()).raw["pairs"]
        pairs = {p["symbol"]: p for p in cfg
                 if p.get("quoteAsset") == QUOTE
                 and p.get("status") in ("PAIR_STATUS_ENABLED", "PAIR_STATUS_POST_ONLY")}
        meta = {s: {"tick": Decimal(p["tickSize"]), "step": Decimal(p["stepSize"]),
                    "min_notional": Decimal(p.get("minNotionalQuote") or "0"),
                    "min_qty": Decimal(p.get("minQtyBase") or "0"),
                    "scale": int(p.get("baseQuantityScale") or 8)}
                for s, p in pairs.items()}

        # never carry orphaned orders across runs: sweep first, then adopt
        for sweep in range(2):
            try:
                await c.orders.cancel_all(request_id=f"gap1-boot-{sweep}-"
                                          + uuid.uuid4().hex[:10])
                await asyncio.sleep(4)
                if not rest_open_orders().get("orders", []):
                    break
                if sweep == 1:
                    alert(f"WARNING: {len(rest_open_orders().get('orders', []))} "
                          f"order(s) still open after startup sweep")
            except Exception as e:
                print("startup sweep failed:", repr(e), flush=True)

        adopt = []
        for attempt in range(24):  # up to ~2 min: WiFi may still be coming back
            try:
                for row in rest_balances():
                    sym = row.get("symbol", "")
                    if sym != QUOTE and Decimal(row.get("trading") or 0) > 0 \
                            and (sym + "-" + QUOTE) in pairs:
                        adopt.append((sym + "-" + QUOTE, Decimal(row["trading"])))
                break
            except Exception as e:
                print(f"balances read retry {attempt + 1}/24: {repr(e)}", flush=True)
                await asyncio.sleep(5)

        alert("Gap hunter online. Adopting: "
              + (", ".join(f"{s} {q}" for s, q in adopt) or "nothing")
              + f" | gap>={GAP_MIN_PCT}% depth>={DEPTH_MIN_USD}$")

        state = "idle"
        adopt_q = list(adopt)
        pos = {}
        next_scan = 0.0
        next_fallback = 0.0
        next_block_alert = 0.0

        # volume: disk state -> .env baseline -> API scorecard -> 50k
        vol, next_10k = None, None
        try:
            with open(os.path.join(HERE, "volume_state.json"), encoding="utf-8") as f:
                s = json.load(f)
            vol = Decimal(s["total"])
            next_10k = int(s["next_milestone"])
            alert(f"volume tracker resumed: {vol:,.0f} USDT; "
                  f"next milestone {next_10k:,}")
        except (OSError, ValueError, KeyError):
            pass
        if vol is None:
            env_base = os.environ.get("VOLUME_BASELINE", "").strip()
            if env_base:
                try:
                    vol = Decimal(env_base)
                except Exception:
                    vol = None
        if vol is None:
            vol = season_volume()
        if vol is None:
            vol = Decimal(50000)
            alert("volume baseline unavailable -> starting from 50,000")
        if next_10k is None:
            next_10k = (int(vol // 10000) + 1) * 10000
            alert(f"volume tracker online: season baseline {vol:,.0f} USDT; "
                  f"next milestone {next_10k:,}")

        def save_vol():
            try:
                with open(os.path.join(HERE, "volume_state.json"), "w",
                          encoding="utf-8") as f:
                    json.dump({"total": str(vol), "next_milestone": next_10k}, f)
            except OSError:
                pass

        def cycle_done(sym, sell_px):
            """Close a cycle from balance truth; alert with balances + volume."""
            nonlocal vol, next_10k, pos, state
            base_sym = sym.split("-")[0]
            base_held, _ = rest_asset_balance(base_sym)
            qty0 = pos.get("qty0") or pos.get("qty") or Decimal(0)
            sold = max(Decimal(0), qty0 - base_held)
            sell_px = sell_px or pos.get("last_avg") or pos.get("ask_px") or Decimal(0)
            basis = pos.get("buy_px") or Decimal(0)
            sell_usd = sell_px * sold
            buy_usd = basis * sold
            fees = (sell_usd + buy_usd) * Decimal("0.001")
            net = sell_usd - buy_usd - fees
            usdt_held, _ = rest_asset_balance(QUOTE)
            if sell_px > 0:
                net = sell_usd - buy_usd - fees
                net_txt = f"net {'+' if net >= 0 else ''}{net:.2f} USDT (fees ~{fees:.2f})"
            else:
                net_txt = "P&L unknown (fill price not observed)"
            alert(f"{sym}: CYCLE DONE. sold ~{sold} @ ~{sell_px or '?'} | {net_txt}\n"
                  f"balances: USDT {usdt_held:,.2f} | {base_sym} {base_held}")
            # the site counts BOTH sides of every round (each fill's notional)
            buy_side = basis if basis > 0 else sell_px  # adopted: assume buy ~ sell
            vol += sold * (buy_side + sell_px)
            while vol >= next_10k:
                alert(f"MILESTONE: {next_10k:,} USDT season volume reached "
                      f"(tracker total {vol:,.0f})", priority=True)
                next_10k += 10000
            save_vol()
            pos, state = {}, "idle"

        while True:
            now = time.time()
            try:
                if state == "idle" and adopt_q:
                    sym, qty = adopt_q.pop(0)
                    pos = {"symbol": sym, "qty": qty, "qty0": qty,
                           "buy_px": COST_BASIS.get(sym, Decimal(0)),
                           "sell_cid": None, "better": {}}
                    state = "selling"
                    alert(f"{sym}: adopting {qty} (basis {pos['buy_px'] or 'unknown'})")

                elif state == "idle":
                    if now >= next_scan:
                        next_scan = now + SCAN_SECONDS
                        rows = await scan_books(c, pairs)
                        picked, tag = None, ""
                        gaps = [r for r in rows
                                if r[0] >= GAP_MIN_PCT
                                and min(r[4], r[5]) >= DEPTH_MIN_USD]
                        if gaps:
                            picked, tag = gaps[0], "GAP"
                        elif now >= next_fallback:
                            fb = [r for r in rows
                                  if r[0] >= FALLBACK_MIN_GAP_PCT
                                  and min(r[4], r[5]) >= FALLBACK_DEPTH_MIN_USD]
                            if FALLBACK_SYMBOL:
                                fb = [r for r in fb if r[1] == FALLBACK_SYMBOL]
                            if fb:
                                picked, tag = fb[0], "VOLUME"
                                next_fallback = now + FALLBACK_COOLDOWN
                        if picked:
                            gap, sym, bid, ask, bid_usd, ask_usd = picked
                            # one-position rule: never stack trades unless the candidate
                            # is a >20% 24h mover (hot override)
                            ours = [o for o in rest_open_orders().get("orders", [])
                                    if str(o.get("clientOrderId", "")).startswith("gap1-")]
                            held = [r for r in rest_balances()
                                    if r.get("symbol") != QUOTE
                                    and Decimal(r.get("trading") or 0) > 0
                                    and (r.get("symbol") + "-" + QUOTE) in pairs]
                            hot, chg = False, Decimal(0)
                            if ours or held:
                                try:
                                    cr = await c.market_data.get_candles(
                                        symbol=sym, timeframe="1h", limit=25)
                                    cs = list(cr.candles)
                                    if len(cs) >= 2 and Decimal(cs[0].close) > 0:
                                        chg = abs((Decimal(cs[-1].close)
                                                   / Decimal(cs[0].close) - 1) * 100)
                                        hot = chg > 20
                                except Exception as e:
                                    print("hot check failed:", repr(e), flush=True)
                            if (ours or held) and not hot:
                                if now >= next_block_alert:
                                    next_block_alert = now + 600
                                    alert(f"one-position rule: {len(ours)} open order(s), "
                                          f"{len(held)} held position(s) -> not starting "
                                          f"{sym} ({tag}, 24h {chg:.1f}%)")
                                picked = None
                            elif hot and (ours or held):
                                try:
                                    await c.orders.cancel_all(
                                        request_id="gap1-hot-" + uuid.uuid4().hex[:10])
                                except Exception:
                                    pass
                                adopt_q.extend((r["symbol"] + "-" + QUOTE,
                                                Decimal(r["trading"])) for r in held)
                                alert(f"HOT override: {sym} 24h {chg:.1f}% > 20% -> "
                                      f"starting despite {len(ours)} order(s) / "
                                      f"{len(held)} held (queued for adoption)")
                                picked = None  # this scan starts clean next pass
                        if picked:
                            gap, sym, bid, ask, bid_usd, ask_usd = picked
                            m = meta[sym]
                            price = bid if (tag == "VOLUME" and not FALLBACK_PENNY) \
                                else min(bid + m["tick"], ask - m["tick"])
                            usdt = next((Decimal(r["available"]) for r in rest_balances()
                                         if r.get("symbol") == QUOTE), Decimal(0))
                            q = floor_step(usdt / (price * FEE_BUFFER), m["step"])
                            if q > 0 and q >= m["min_qty"] and q * price >= m["min_notional"]:
                                cid, err = await place(c, sym, "buy", q, price)
                                if cid:
                                    pos = {"symbol": sym, "buy_cid": cid, "buy_px": price,
                                           "buy_at": now, "qty": None,
                                           "sell_cid": None, "better": {},
                                           "join_ask": tag == "VOLUME"}
                                    state = "buying"
                                    if tag == "GAP":
                                        alert(f"GAP {sym}: {gap:.2f}% "
                                              f"(net +{gap - RT_FEE_PCT:.2f}%) | "
                                              f"bid ${bid_usd:.0f} ask ${ask_usd:.0f} "
                                              f"-> BUY {q} @ {price}", priority=True)
                                    else:
                                        loss = q * price * (RT_FEE_PCT - gap) / 100
                                        alert(f"VOLUME {sym}: gap {gap:.3f}% (<0.20% fee) -> "
                                              f"expect ~{loss:.2f} USDT loss per "
                                              f"~{q * price * 2:.0f} USDT volume cycle | "
                                              f"BUY {q} @ {price}", priority=True)
                            else:
                                alert(f"{tag} {sym} spotted but below pair minimums")

                elif state == "buying":
                    sym = pos["symbol"]
                    m = meta[sym]
                    st, cum, avg = await order_state(c, sym, pos["buy_cid"],
                                                     m["scale"])
                    if st in TERMINAL:
                        pos["qty"] = floor_step(await base_balance(c, sym, m), m["step"])
                        if pos["qty"] > 0:
                            pos["qty0"] = pos["qty"]
                            pos["buy_px"] = avg if avg > 0 else pos["buy_px"]
                            alert(f"{sym}: BUY filled {pos['qty']} @ {pos['buy_px']}")
                            state = "selling"
                        else:
                            alert(f"{sym}: buy ended unfilled ({st})")
                            state = "idle"
                    elif now - pos["buy_at"] > BUY_TIMEOUT_SECONDS:
                        try:
                            await c.orders.cancel(key=ClientOrderId(pos["buy_cid"]),
                                                  symbol=sym)
                        except Exception:
                            pass
                        st, cum, avg = await order_state(c, sym, pos["buy_cid"],
                                                         m["scale"])
                        alert(f"{sym}: buy timeout after {BUY_TIMEOUT_SECONDS:.0f}s")
                        pos["qty"] = floor_step(await base_balance(c, sym, m), m["step"])
                        if pos["qty"] > 0:
                            pos["qty0"] = pos["qty"]
                            pos["buy_px"] = avg if avg > 0 else pos["buy_px"]
                            alert(f"{sym}: BUY filled {pos['qty']} @ {pos['buy_px']}")
                            state = "selling"
                        else:
                            state = "idle"

                elif state == "selling":
                    sym = pos["symbol"]
                    m = meta[sym]
                    held, avail = rest_asset_balance(sym.split("-")[0])
                    price_est = pos.get("last_avg") or pos.get("ask_px") or Decimal(0)
                    if held < m["min_qty"] or (price_est > 0 and
                                               held * price_est < m["min_notional"]):
                        # balances are the truth: nothing sellable held -> cycle over
                        cycle_done(sym, price_est)
                    elif pos["sell_cid"] is None:
                        pos["qty"] = min(pos.get("qty") or held, held)
                        await place_sell(c, pos, meta)
                    else:
                        st, cum, avg = await order_state(c, sym, pos["sell_cid"], m["scale"])
                        if avg and avg > 0:
                            pos["last_avg"] = avg
                        if st in TERMINAL:
                            if st == "rejected" and cum <= 0:
                                pos["no_postonly"] = True  # post-only kept getting crossed
                            pos["sell_cid"] = None
                            pos["canceling"] = False  # order is gone: nothing left to cancel
                        elif pos.get("canceling"):
                            # re-send the cancel until the order is really gone
                            if any(o.get("clientOrderId") == pos["sell_cid"]
                                   for o in rest_open_orders().get("orders", [])):
                                try:
                                    await c.orders.cancel(key=ClientOrderId(pos["sell_cid"]),
                                                          symbol=sym)
                                except Exception:
                                    pass
                            else:
                                pos["sell_cid"] = None
                                pos["canceling"] = False
                        else:
                            book = await c.orderbook.get(symbol=sym, depth=10)
                            better = {pdec(a.price) for a in (book.asks or [])
                                      if pdec(a.price) < pos["ask_px"]}
                            track = pos["better"]
                            for p in list(track):
                                if p not in better:
                                    del track[p]
                            for p in better:
                                track.setdefault(p, now)
                            aged = sum(1 for p, t0 in track.items()
                                       if now - t0 >= PERSIST_SECONDS)
                            if aged > BETTER_ASKS_LIMIT:
                                try:
                                    await c.orders.cancel(key=ClientOrderId(pos["sell_cid"]),
                                                          symbol=sym)
                                except Exception:
                                    pass
                                alert(f"{sym}: {aged} better asks held >{PERSIST_SECONDS:.0f}s "
                                      f"-> bail-out: re-asking under them", priority=True)
                                pos["canceling"] = True
                                pos["bail"] = True
            except Exception as e:
                print("loop error:", repr(e), flush=True)
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await c.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
