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
import sys
import time
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, ROUND_FLOOR

import nacl.signing
from polyester import POLYESTER_TESTNET_ENVIRONMENT, AsyncPolyester
from polyester.models import ClientOrderId

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

if __package__:
    from .paths import HERE, env_files, state_file
else:
    from paths import HERE, env_files, state_file


def load_env():
    for path in env_files():
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip())
        except OSError:
            pass


load_env()

API = "https://api.testnet.polyester.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
KEY = os.environ.get("POLYESTER_API_KEY_ID", "")
_SK = nacl.signing.SigningKey(bytes.fromhex(os.environ.get("POLYESTER_ED25519_SEED", "0" * 64)))
ACCOUNT = os.environ.get("POLYESTER_ACCOUNT_ID", "")

QUOTE = "USDT"
RT_FEE_PCT = Decimal("0.20")
GAP_MIN_PCT = Decimal(os.environ.get("GAP_MIN_PCT", "0.21"))
DEPTH_MIN_USD = Decimal(os.environ.get("DEPTH_MIN_USD", "50"))
FALLBACK_SYMBOL = os.environ.get("FALLBACK_SYMBOL", "").strip()
FALLBACK_MIN_GAP_PCT = Decimal(os.environ.get("FALLBACK_MIN_GAP_PCT", "0.05"))
FALLBACK_DEPTH_MIN_USD = Decimal(os.environ.get("FALLBACK_DEPTH_MIN_USD", "1000"))
FALLBACK_COOLDOWN = float(os.environ.get("FALLBACK_COOLDOWN", "60"))
FALLBACK_PENNY = os.environ.get("FALLBACK_PENNY", "0") == "1"
SCAN_SECONDS = float(os.environ.get("SCAN_SECONDS", "10"))
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "2"))
PERSIST_SECONDS = float(os.environ.get("PERSIST_SECONDS", "60"))
BETTER_ASKS_LIMIT = int(os.environ.get("BETTER_ASKS_LIMIT", "2"))
BUY_TIMEOUT_SECONDS = float(os.environ.get("BUY_TIMEOUT_SECONDS", "180"))
FEE_BUFFER = Decimal("1.0015")
FREEZE_SECONDS = float(os.environ.get("FREEZE_SECONDS", "300"))
STALL_SECONDS = float(os.environ.get("STALL_SECONDS", "900"))
BALANCE_GRACE = float(os.environ.get("BALANCE_GRACE_SECONDS", "120"))
HEARTBEAT = "/var/tmp/polyester.heartbeat"


def frozen_seconds(wall, mono, last_wall, last_mono):
    """Seconds the VM spent frozen: wall clock advanced but the guest did not run.

    A paused sandbox stops the guest kernel, so CLOCK_MONOTONIC freezes while
    the wall clock keeps going. The difference is the freeze duration.
    """
    return (wall - last_wall) - (mono - last_mono)

TERMINAL = {"filled", "canceled", "rejected", "expired"}

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


def alert(text):
    print(text, flush=True)
    import threading

    def send():
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


def money(x):
    return f"{Decimal(x):,.2f}"


def num(x):
    s = f"{Decimal(x):,.8f}".rstrip("0").rstrip(".")
    return s or "0"


def wallet(base=None):
    """The balances that matter right now: 'USDT 3,094.10 | ARB 8,946.35'."""
    try:
        rows = {r.get("symbol"): r for r in rest_balances()}
    except Exception as e:
        print("wallet read failed:", repr(e), flush=True)
        return "balances unavailable"
    parts = [f"{QUOTE} {money(rows.get(QUOTE, {}).get('trading') or 0)}"]
    if base and base in rows:
        parts.append(f"{base} {num(rows[base].get('trading') or 0)}")
    return " | ".join(parts)


def notify(emoji, action, *lines):
    """Telegram layout: emoji + action, then one line per fact."""
    alert("\n".join([f"{emoji} {action}"] + [l for l in lines if l]))


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


def rest_me():
    """GET /v1/auth/me via signed REST (the account this key belongs to)."""
    ts = str(int(time.time() * 1000))
    path = "/v1/auth/me"
    canon = "\n".join([ts, "GET", path, "", hashlib.sha256(b"").hexdigest()])
    req = urllib.request.Request(API + path, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Origin", "https://testnet.polyester.com")
    req.add_header("X-API-KEY-ID", KEY)
    req.add_header("X-API-TIMESTAMP", ts)
    req.add_header("X-API-SIGNATURE", _SK.sign(canon.encode()).signature.hex())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


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
    base = sym.split("-")[0]
    tick, step, min_not = meta[sym]["tick"], meta[sym]["step"], meta[sym]["min_notional"]
    book = await c.orderbook.get(symbol=sym, depth=10)
    if not book.asks:
        return False
    best_ask = pdec(book.asks[0].price)
    best_bid = pdec(book.bids[0].price) if book.bids else Decimal(0)
    spread = ((best_ask - best_bid) / ((best_ask + best_bid) / 2) * 100
              if best_bid else Decimal(0))
    basis = pos.get("buy_px") or Decimal(0)
    if pos.get("bail"):
        target = best_ask - tick
    elif pos.get("join_ask"):
        target = max(best_ask, basis) if basis > 0 else best_ask
    elif basis > 0:
        target = max(best_ask - tick, basis)
    else:
        target = best_ask - tick
    q = floor_step(pos["qty"], step)
    held = None
    try:
        for row in rest_balances():
            if row.get("symbol") == base:
                held = floor_step(Decimal(row.get("trading") or 0), step)
                q = min(q, floor_step(Decimal(row.get("available") or 0), step))
                break
    except Exception:
        held = None
    if q <= 0 or q * best_ask < min_not:
        if held and held * best_ask >= min_not:
            try:
                await c.orders.cancel_all(symbol=sym,
                                          request_id="gap1-ca-" + uuid.uuid4().hex[:10])
                pos["skip_noted"] = False
                notify("⚠️", "ORPHAN CLEARED",
                       f"{sym} | {num(held)} reserved by an orphan order | retrying")
            except Exception as e:
                print("orphan clear failed:", repr(e), flush=True)
        elif not pos.get("skip_noted"):
            pos["too_small"] = True
            pos["skip_noted"] = True
            notify("⚠️", "SELL SKIPPED",
                   f"{sym} | below pair minimums | qty {num(q)}")
        return False
    po = not pos.get("no_postonly")
    for price, post in ((target, po), (best_ask, False)):
        if price <= 0:
            continue
        cid, err = await place(c, sym, "sell", q, price, post_only=post)
        if cid:
            pos["sell_cid"], pos["ask_px"] = cid, price
            pos["better"], pos["ask_at"] = {}, time.time()
            pnl_txt = (f"expected {(price - basis) * q * Decimal('0.998'):+.2f} USDT"
                       if basis > 0 else "expected P&L unknown (no basis)")
            notify("🔵", "ASK",
                   f"{sym} | gap {spread:.3f}% | {num(q)} @ {num(price)} | "
                   f"bought @ {num(basis) if basis else '?'}",
                   f"{pnl_txt} | {wallet(base)}")
            return True
    if err and "insufficient" in err.lower():
        try:
            await c.orders.cancel_all(symbol=sym,
                                      request_id="gap1-ca-" + uuid.uuid4().hex[:10])
            pos["skip_noted"] = False
            notify("⚠️", "INSUFFICIENT FUNDS",
                   f"{sym} | orphaned orders cleared | retrying")
        except Exception as e:
            print("orphan clear failed:", repr(e), flush=True)
    elif pos.get("last_err") != err:
        pos["last_err"] = err
        notify("⚠️", "ASK REJECTED", f"{sym} | {err}")
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
    global ACCOUNT
    last_wall, last_mono = time.time(), time.monotonic()
    c = None
    for attempt in range(12):
        try:
            if not ACCOUNT:
                ACCOUNT = str((rest_me() or {}).get("accountId") or "")
                if not ACCOUNT:
                    raise RuntimeError("the exchange returned no account id")
                print("account:", ACCOUNT, flush=True)
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

        async def resync(tag):
            """Cancel our stale orders, re-read balances.

            Returns (held, ok). ok=False means the exchange was unreachable, so
            the caller must keep its position instead of forgetting it.
            """
            held, ok = [], False
            for attempt in range(24):
                try:
                    held = []
                    for row in rest_balances():
                        sym = row.get("symbol", "")
                        if sym != QUOTE and Decimal(row.get("trading") or 0) > 0 \
                                and (sym + "-" + QUOTE) in pairs:
                            held.append((sym + "-" + QUOTE, Decimal(row["trading"])))
                    ok = True
                    break
                except Exception as e:
                    print(f"balances read retry {attempt + 1}/24: {repr(e)}", flush=True)
                    await asyncio.sleep(5)
            if not ok:
                notify("⚠️", "RESYNC FAILED",
                       "balances unreadable | keeping the position")
                return [], False

            left = []
            for attempt in range(4):
                try:
                    ours = [o for o in rest_open_orders().get("orders", [])
                            if str(o.get("clientOrderId", "")).startswith("gap1-")]
                    for o in ours:
                        await c.orders.cancel(key=ClientOrderId(o["clientOrderId"]),
                                              symbol=o.get("symbol"))
                    await asyncio.sleep(3)
                    left = [o for o in rest_open_orders().get("orders", [])
                            if str(o.get("clientOrderId", "")).startswith("gap1-")]
                    if not left:
                        break
                except Exception as e:
                    print(f"sweep retry {attempt + 1}/4:", repr(e), flush=True)
                    await asyncio.sleep(3)
            if left:
                notify("⚠️", "ORDERS STILL OPEN",
                       f"{len(left)} of our orders survived the sweep")
            return held, True

        adopt, boot_ok = await resync("gap1-boot")

        notify("🤖", "GAP HUNTER ONLINE",
               f"gap ≥ {GAP_MIN_PCT}% | depth ≥ {money(DEPTH_MIN_USD)} USDT | "
               + (", ".join(f"{s} {num(q)}" for s, q in adopt) or "nothing to adopt"),
               wallet())

        state = "idle"
        adopt_q = list(adopt)
        pos = {}
        carry = {}
        dust = {}
        pending_resync = not boot_ok
        next_scan = 0.0
        next_fallback = 0.0
        next_block_alert = 0.0

        vol, next_10k = None, None
        try:
            with open(state_file("volume_state.json"), encoding="utf-8") as f:
                s = json.load(f)
            vol = Decimal(s["total"])
            next_10k = int(s["next_milestone"])
            notify("📊", "VOLUME TRACKER RESUMED",
                   f"{money(vol)} USDT | next milestone {next_10k:,}")
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
            notify("⚠️", "VOLUME BASELINE",
                   "scorecard unavailable | starting from 50,000")
        if next_10k is None:
            next_10k = (int(vol // 10000) + 1) * 10000
            notify("📊", "VOLUME TRACKER ONLINE",
                   f"season baseline {money(vol)} USDT | next milestone {next_10k:,}")

        def save_vol():
            try:
                with open(state_file("volume_state.json"), "w",
                          encoding="utf-8") as f:
                    json.dump({"total": str(vol), "next_milestone": next_10k}, f)
            except OSError:
                pass

        def beat(now):
            """Heartbeat for the external keepalive: a stale file means we are wedged."""
            try:
                with open(HEARTBEAT, "w", encoding="utf-8") as f:
                    f.write(str(int(now)))
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
            if sell_px > 0:
                net_txt = f"net {net:+.2f} USDT"
            else:
                net_txt = "P&L unknown (fill price not observed)"
            notify("✅", "CYCLE DONE",
                   f"{sym} | sold {num(sold)} @ "
                   f"{num(sell_px) if sell_px else '?'} | fee {money(fees)} USDT",
                   f"{net_txt} | {wallet(base_sym)}")
            buy_side = basis if basis > 0 else sell_px
            vol += sold * (buy_side + sell_px)
            while vol >= next_10k:
                notify("🏆", "MILESTONE",
                       f"{next_10k:,} USDT season volume | tracker {money(vol)} USDT",
                       f"{money(vol - next_10k)} USDT over the line | {wallet()}")
                next_10k += 10000
            save_vol()
            pos, state = {}, "idle"

        while True:
            now = time.time()
            try:
                mono = time.monotonic()
                gap = frozen_seconds(now, mono, last_wall, last_mono)
                stall = now - last_wall
                last_wall, last_mono = now, mono
                beat(now)
                if (gap > FREEZE_SECONDS or stall > STALL_SECONDS) and not pending_resync:
                    pending_resync = True
                    notify("❄️", "FREEZE DETECTED",
                           f"sandbox paused {max(gap, stall) / 60:.1f} min "
                           f"| re-syncing orders and balances",
                           wallet())
                if pending_resync:
                    held, ok = await resync(f"gap1-freeze-{int(now)}")
                    if ok:
                        pending_resync = False
                        adopt_q = list(held)
                        pos, state = {}, "idle"
                        next_scan = 0.0
                    now = time.time()
                elif state == "idle" and adopt_q:
                    sym, qty = adopt_q.pop(0)
                    basis, qty0 = carry.get(sym, (None, None))
                    pos = {"symbol": sym, "qty": qty, "qty0": qty0 or qty,
                           "buy_px": basis if basis is not None
                           else COST_BASIS.get(sym, Decimal(0)),
                           "sell_cid": None, "better": {}, "since": now}
                    state = "selling"
                    carry[sym] = (pos["buy_px"], pos["qty0"])
                    notify("♻️", "ADOPT",
                           f"{sym} {num(qty)} | basis "
                           f"{num(pos['buy_px']) if pos['buy_px'] else 'unknown'}",
                           wallet(sym.split("-")[0]))

                elif state == "idle":
                    if now >= next_scan:
                        next_scan = now + SCAN_SECONDS
                        rows = await scan_books(c, pairs)
                        ours = [o for o in rest_open_orders().get("orders", [])
                                if str(o.get("clientOrderId", "")).startswith("gap1-")]
                        held = []
                        for r in rest_balances():
                            psym = r.get("symbol", "") + "-" + QUOTE
                            qty = Decimal(r.get("trading") or 0)
                            if r.get("symbol") == QUOTE or qty <= 0:
                                continue
                            if psym not in pairs or dust.get(psym) == qty:
                                continue
                            held.append(r)
                        if held and not adopt_q:
                            adopt_q.extend((r["symbol"] + "-" + QUOTE,
                                            Decimal(r["trading"])) for r in held)
                            notify("♻️", "ADOPT QUEUED",
                                   f"{len(held)} position(s) to sell back | "
                                   + ", ".join(f"{r['symbol']} {num(r['trading'])}"
                                               for r in held),
                                   wallet())
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
                                    notify("⚠️", "ONE-POSITION",
                                           f"{sym} | {len(ours)} open order(s) | "
                                           f"{len(held)} held position(s) | 24h {chg:.1f}%",
                                           f"not starting a new trade | {wallet()}")
                                picked = None
                            elif hot and (ours or held):
                                try:
                                    await c.orders.cancel_all(
                                        request_id="gap1-hot-" + uuid.uuid4().hex[:10])
                                except Exception:
                                    pass
                                adopt_q.extend((r["symbol"] + "-" + QUOTE,
                                                Decimal(r["trading"])) for r in held)
                                notify("🔥", "HOT OVERRIDE",
                                       f"{sym} | 24h {chg:.1f}% > 20% | "
                                       f"{len(ours)} order(s) | {len(held)} held",
                                       "starting anyway | queued for adoption")
                                picked = None
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
                                        notify("🟢", "BUY",
                                               f"{sym} | gap {gap:.2f}% "
                                               f"(net +{gap - RT_FEE_PCT:.2f}%) | "
                                               f"{num(q)} @ {num(price)} | "
                                               f"depth {money(bid_usd)}/{money(ask_usd)}",
                                               f"expected +{q * price * (gap - RT_FEE_PCT) / 100:.2f}"
                                               f" USDT | {wallet(sym.split('-')[0])}")
                                    else:
                                        loss = q * price * (RT_FEE_PCT - gap) / 100
                                        notify("🟡", "VOLUME BUY",
                                               f"{sym} | gap {gap:.3f}% "
                                               f"(fee {RT_FEE_PCT}%) | "
                                               f"{num(q)} @ {num(price)} | "
                                               f"volume {money(q * price * 2)} USDT",
                                               f"expected -{loss:.2f} USDT "
                                               f"| {wallet(sym.split('-')[0])}")
                            else:
                                notify("⚠️", "SKIPPED",
                                       f"{sym} | {tag} | below pair minimums")

                elif state == "buying":
                    sym = pos["symbol"]
                    m = meta[sym]
                    st, cum, avg = await order_state(c, sym, pos["buy_cid"],
                                                     m["scale"])
                    if st in TERMINAL:
                        held_qty = floor_step(await base_balance(c, sym, m), m["step"])
                        qty = held_qty or floor_step(cum, m["step"])
                        if st == "filled" or cum > 0:
                            pos["qty"] = qty
                            pos["qty0"] = qty
                            pos["buy_px"] = avg if avg > 0 else pos["buy_px"]
                            pos["since"] = now
                            carry[sym] = (pos["buy_px"], pos["qty0"])
                            notify("✅", "BUY FILLED",
                                   f"{sym} | {num(pos['qty'])} @ {num(pos['buy_px'])}",
                                   wallet(sym.split("-")[0]))
                            state = "selling"
                        else:
                            pos["qty"] = held_qty
                            notify("⚠️", "BUY UNFILLED",
                                   f"{sym} | order ended {st}",
                                   wallet(sym.split("-")[0]))
                            state = "idle"
                    elif now - pos["buy_at"] > BUY_TIMEOUT_SECONDS:
                        try:
                            await c.orders.cancel(key=ClientOrderId(pos["buy_cid"]),
                                                  symbol=sym)
                        except Exception:
                            pass
                        st, cum, avg = await order_state(c, sym, pos["buy_cid"],
                                                         m["scale"])
                        notify("⚠️", "BUY TIMEOUT",
                               f"{sym} | no fill after {BUY_TIMEOUT_SECONDS:.0f}s "
                               f"| cancelled",
                               wallet(sym.split("-")[0]))
                        pos["qty"] = floor_step(await base_balance(c, sym, m), m["step"])
                        if pos["qty"] > 0 or cum > 0:
                            pos["qty"] = pos["qty"] or floor_step(cum, m["step"])
                            pos["qty0"] = pos["qty"]
                            pos["buy_px"] = avg if avg > 0 else pos["buy_px"]
                            pos["since"] = now
                            carry[sym] = (pos["buy_px"], pos["qty0"])
                            notify("✅", "BUY FILLED",
                                   f"{sym} | {num(pos['qty'])} @ {num(pos['buy_px'])}",
                                   wallet(sym.split("-")[0]))
                            state = "selling"
                        else:
                            state = "idle"

                elif state == "selling":
                    sym = pos["symbol"]
                    m = meta[sym]
                    held, avail = rest_asset_balance(sym.split("-")[0])
                    price_est = pos.get("last_avg") or pos.get("ask_px") or Decimal(0)
                    if held > 0:
                        pos["seen"] = True
                    if held <= 0 and not pos.get("seen"):
                        if now - pos.get("since", 0) < BALANCE_GRACE:
                            pass
                        else:
                            cycle_done(sym, price_est)
                    elif held < m["min_qty"] or (price_est > 0 and
                                               held * price_est < m["min_notional"]):
                        if held > 0:
                            dust[sym] = held
                        cycle_done(sym, price_est)
                    elif pos["sell_cid"] is None:
                        pos["qty"] = min(pos.get("qty") or held, held)
                        placed = await place_sell(c, pos, meta)
                        if placed:
                            pos["too_small"] = False
                        elif pos.get("too_small"):
                            dust[sym] = held
                            notify("⚠️", "DUST",
                                   f"{sym} | {num(held)} left | too small to sell",
                                   wallet(sym.split("-")[0]))
                            pos, state = {}, "idle"
                    else:
                        st, cum, avg = await order_state(c, sym, pos["sell_cid"], m["scale"])
                        if avg and avg > 0:
                            pos["last_avg"] = avg
                        if st in TERMINAL:
                            if st == "rejected" and cum <= 0:
                                pos["no_postonly"] = True
                            pos["sell_cid"] = None
                            pos["canceling"] = False
                        elif pos.get("canceling"):
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
                                notify("⚠️", "ASK BAIL-OUT",
                                       f"{sym} | {aged} better asks held "
                                       f"{PERSIST_SECONDS:.0f}s | re-asking under them",
                                       wallet(sym.split("-")[0]))
                                pos["canceling"] = True
                                pos["bail"] = True
            except Exception as e:
                print("loop error:", repr(e), flush=True)
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await c.aclose()


def cli():
    if sys.argv[1:2] == ["setup"]:
        if __package__:
            from .setup import cli as setup_cli
        else:
            from setup import cli as setup_cli
        setup_cli()
        return
    missing = [k for k in ("POLYESTER_API_KEY_ID", "POLYESTER_ED25519_SEED")
               if not os.environ.get(k, "").strip()]
    if missing:
        print("missing credentials: " + ", ".join(missing)
              + " (put them in .env next to the bot or in the environment)")
        sys.exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
