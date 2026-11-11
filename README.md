<div align="center">

<img src="polyester-bot.png" alt="Polyester" width="120" height="120" style="border-radius:50%; object-fit:cover;" />

# Polyester Gap Hunter

**Testnet market-making bot · one position at a time · Telegram alerts · GitHub Actions deploy**

![Python](https://img.shields.io/badge/Python-3.13-blue?logo=python)
![asyncio](https://img.shields.io/badge/asyncio-trading-2b5f8a?logo=asyncio)
![systemd](https://img.shields.io/badge/systemd-service-81dafb?logo=systemd)
![GitHub Actions](https://img.shields.io/badge/deploy-GitHub_Actions-2088ff?logo=github-actions)

</div>

## How it works

```
scan pairs → find gap → BUY at the bid → ASK under the offer → CYCLE DONE
```

- **Gap mode** — buys the bid, asks one tick under the offer (maker on both legs).
  Fees are read from the exchange (VIP tier) every 30 min; the gap floor follows
  them (`2 x maker + GAP_MARGIN_PCT`, `0.11%` at VIP3). An explicit `GAP_MIN_PCT`
  pins it instead.
- **Volume fallback** — when nothing clears the fee bar, the deepest tightest pair is
  cycled anyway to keep season volume up. The candidate with real trade flow wins
  over the widest dead spread. A saturation ladder cuts the waiting:
  after `SAT_PENNY_AFTER` consecutive unfilled fallback buys it pennies the bid,
  after `SAT_TAKER_AFTER` it buys at the ask (taker fee), and the buy timeout
  shrinks to `FALLBACK_BUY_TIMEOUT`.
- **Chase rule (both sides)** — if `BETTER_ASKS_LIMIT` better asks (sell) or
  `BETTER_BIDS_LIMIT` better bids (buy) beat our price for `PERSIST_SECONDS`,
  the resting order is **amended** one tick more aggressive than them — one
  atomic call, no unfilled window. A cancel happens only when the amend cannot.
- **Dead-man switch** — `orders.cancel_all_after` is re-armed every 60 s; if the
  bot dies or freezes without re-arming, the exchange cancels all our orders itself.
- **Pre-flight check** — before a buy is placed, `orders.preview_order` asks the
  exchange whether it would be admissible; only a clear rejection blocks
  (throttled, never an outage blocking trades).
- **Websocket books** — every pair's orderbook is streamed live (`orderbook.subscribe`);
  readers prefer the fresh cache and fall back to REST when it goes quiet.
- **One position** — never stacks trades; the exception is a `>20%` 24h mover.
- **Volume tracker** — every cycle adds to `volume_state.json`, milestone alert
  every 10,000 USDT.
- **Daily equity line** — once a day one `EQUITY` alert: total in USDT (balances ×
  fresh mids), per-position breakdown, 24 h delta vs yesterday's snapshot
  (`equity_state.json`).
- **TP triggers** — `TP_TRIGGER_PCT` arms a full-size server-side take-profit on a held
  position; the trigger reserves the whole inventory while armed, so the bot cannot
  sell alongside it. Off by default.

## Install

**npm** — the wrapper builds its own Python environment on first run:

```bash
npm install -g polyester-bot
polyester-bot setup
```

**pip** — straight from the release tag:

```bash
pip install "polyester-bot @ git+https://github.com/natanim-kemal/polyester-bot.git@v1.1.0"
polyester-bot setup
```

**source**:

```bash
git clone https://github.com/natanim-kemal/polyester-bot.git
cd polyester-bot
pip install -e .
polyester-bot setup
```

Python 3.11 or newer.

## Setup

One command walks you through the credentials and leaves a working `.env` behind:

```bash
polyester-bot setup
```

It asks for:

| Question | Where to get it |
| --- | --- |
| Polyester API key id (`ak_...`) | Polyester dashboard → API keys |
| Ed25519 seed (64 hex chars or base64) | the same API key, shown once when you create it |
| Telegram bot token | [@BotFather](https://t.me/BotFather) → `/newbot` |
| Telegram chat id | send `/start` to your bot; the wizard finds it, or paste the id |
| EOA private key | optional, only for the daily paper-asset claim |

The account id is **not** asked for: the wizard signs a `GET /v1/auth/me` with
your key and derives it. The exchange and Telegram are both checked before
anything is written, and the wizard sends one test message so you know the
alerts will arrive.

Credentials land in `~/.polyester-bot/.env` (and in a `.env` next to the bot if
one is already there), never in the git repository. The bot reads the working
directory first, then the home copy, so it works from anywhere.

Run the bot the same way:

```bash
polyester-bot
```

`POLYESTER_STATE_DIR` moves the working directory somewhere else. The bot trades
**testnet** funds only.

## Project Structure

```
├── bot/
│   ├── monitor.py            # the bot: scanner, executor, chase rule
│   ├── setup.py              # guided setup: ask, check, write the .env
│   ├── polyester_farm.py     # season volume, daily paper-asset claim
│   ├── paths.py              # where .env and the state files live
│   ├── check_orders.py       # print open orders
│   ├── cancel_all.py         # cancel every open order
│   ├── status_all.py         # open orders + hot pairs
│   ├── scan_spread.py        # spread of every pair
│   ├── big_buy.py            # one-off market buy
│   ├── hunter_forever.py     # run monitor.py in a loop
│   ├── kill_bot.py           # stop the bot
│   └── .env.example          # every setting, no secrets
├── bin/                      # npm wrapper: polyester-bot, polyester-farm
├── deploy/
│   ├── polyester-bot.service    # systemd unit for the bot
│   ├── polyester-claim.service  # daily paper-asset claim
│   ├── polyester-claim.timer    # 00:10 UTC, persistent
│   └── deploy.sh                # install code + units, restart
├── tests/                    # 14 suites, all run in CI before any deploy:
│   ├── test_sell_loop.py        # the re-ask/cancel machine, end to end
│   ├── test_freeze_detect.py    # detects a paused sandbox from clock skew
│   ├── test_paths.py            # .env and state file resolution
│   ├── test_stuck.py            # a filled buy survives a lagging balance
│   ├── test_setup.py            # the setup wizard and account derivation
│   ├── test_fees.py             # fee math, gap floor and entry ladder
│   ├── test_chase.py            # both sides chase; sell trigger on one better ask
│   ├── test_amend.py            # chase amends in place, cancel only as fallback
│   ├── test_tp_trigger.py       # server-side take-profit triggers (opt-in)
│   ├── test_ws.py               # websocket orderbook feed with REST fallback
│   ├── test_liquidity.py        # fallback prefers a live tape over a wide dead book
│   ├── test_equity.py           # daily equity report from balances x mids
│   ├── test_preflight.py        # admissibility check before a buy is placed
│   └── test_orphan.py           # orphan-clear runs hourly, not every poll
├── .github/workflows/
│   ├── deploy.yml               # push to main → deploy to the sandbox
│   └── keepalive.yml            # every 10 min: keep the sandbox awake
├── pyproject.toml            # the Python package (pip install .)
├── package.json              # the npm package (npm install -g polyester-bot)
└── polyester-bot.png
```

## Configuration

`bot/.env` holds the credentials and tuning; `bot/.env.example` lists every key.
The real file stays on the machine and is gitignored — a deploy never overwrites it.

| Key | Default | Meaning |
| --- | --- | --- |
| `GAP_MIN_PCT` | fee-driven (`0.11`) | minimum spread to take in gap mode |
| `GAP_MARGIN_PCT` | `0.01` | profit margin added on top of the round-trip fee |
| `FEES_REFRESH_SECONDS` | `1800` | how often per-pair fees are re-read from the exchange |
| `DEPTH_MIN_USD` | `50` | minimum depth on both sides |
| `SCAN_SECONDS` | `10` | how often to scan for a gap |
| `POLL_SECONDS` | `2` | how often to manage the open position |
| `PERSIST_SECONDS` | `60` | how long better prices must persist before a chase fires |
| `BETTER_ASKS_LIMIT` | `1` | better asks under our ask that trigger the sell chase |
| `BETTER_BIDS_LIMIT` | `1` | better bids over our bid that trigger the buy chase |
| `BUY_TIMEOUT_SECONDS` | `180` | cancel an unfilled buy after this long |
| `SAT_PENNY_AFTER` | `2` | consecutive fallback buy timeouts before penny bids |
| `SAT_TAKER_AFTER` | `4` | consecutive fallback buy timeouts before buying at the ask |
| `FALLBACK_BUY_TIMEOUT` | `60` | fallback buy timeout once the ladder is engaged |
| `DEADMAN_TIMEOUT` | `300` | auto-cancel window re-armed every 60s; `0` disables |
| `TP_TRIGGER_PCT` | `0` | arm server-side take-profit on sell entry; `0` = never arm |
| `FREEZE_SECONDS` | `300` | wall-vs-monotonic clock skew that counts as a paused sandbox |
| `STALL_SECONDS` | `900` | how long the loop may hang before it counts as a pause |
| `FALLBACK_*` | see example | volume fallback mode |
| `VOLUME_BASELINE` | `57000` | season volume to start from with no state file |

Alerts go to **Telegram** (every alert) and **Discord** if `DISCORD_WEBHOOK_URL`
is set. Everything is also printed to the journal.

Every Telegram alert is three lines — the action, the market with its spread,
then the money:

```
🟢 BUY
BTC-USDT | gap 0.85% (net +0.65%) | 0.0123 @ 41,250.00 | depth 12,450.00/12,556.00
expected +8.20 USDT | USDT 3,094.10 | BTC 0.0123
```

```
🔵 ASK
ARB-USDT | gap 0.118% | 8,946.0 @ 0.167 | bought @ 0.167
expected +1.21 USDT | USDT 2,983.94 | ARB 8,946.35
```

`🟡 VOLUME BUY`, `✅ BUY FILLED`, `✅ CYCLE DONE`, `🏆 MILESTONE`, `❄️ FREEZE
DETECTED` and `⚠️` warnings use the same shape.

## Deploy

Push to `main` → GitHub Actions → SSH → `deploy/deploy.sh` → systemd restart → verify.

| Name | Kind | Value |
| --- | --- | --- |
| `INSTABOX_SSH_KEY` | secret | private key with access to the sandbox |
| `INSTABOX_HOST` | variable | `ssh.instabox.sh` |
| `INSTABOX_PORT` | variable | `2222` |
| `INSTABOX_USER` | variable | sandbox user |

Secrets, `.session` and `volume_state.json` are never touched by a deploy.

`keepalive.yml` runs every 10 minutes and opens an SSH session: the sandbox pauses
itself after 30 idle minutes, and SSH traffic counts as activity, so it stays up.
It also restarts the bot when its heartbeat file (`/var/tmp/polyester.heartbeat`)
goes stale, which catches a bot that is alive but wedged.

The bot defends itself as well. A paused sandbox stops the guest clock, so the
wall clock jumps while the monotonic clock does not; when the bot sees that
skew (or a loop that hung for minutes), it cancels its **own** stale orders,
re-reads the balances and re-adopts the real position. If the exchange is
unreachable it keeps the position it has and retries on the next poll instead
of trading on stale state.

## Daily claim

`polyester-claim.timer` claims the daily paper asset at **00:10 UTC** with
`Persistent=true`, so a run missed while the machine was down happens on next boot.
Whatever it pays out arrives as extra balances, which the bot adopts and sells back
into USDT before looking for the next gap.

```bash
systemctl list-timers polyester-claim.timer
journalctl -u polyester-claim.service
```

## Tests

All 14 suites run in CI on every push, before anything is deployed, together with a
build of the Python package and a smoke test of its entry point.
`python tests/test_<name>.py` runs any one locally.

```bash
for t in tests/test_*.py; do python "$t" || exit 1; done
```

Exits non-zero on the first failing suite. Coverage map:

`test_sell_loop.py` replays the sell state machine against a fake exchange — the
bug where the bot cancelled the ask it had just placed.
`test_freeze_detect.py` drives the freeze guard — the real 1h47m pause triggers a
re-sync, an unreachable exchange keeps the position.
`test_stuck.py` replays the bug where a filled buy was invisible because the
balance lagged behind the order.
`test_setup.py` checks the setup wizard and that the account id is derived, never
baked in.
`test_paths.py` checks `.env` and state file resolution.
`test_fees.py` pins the fee math: the gap floor, the entry ladder, cycle fees.
`test_chase.py` checks both sides chase one tick past the competitor, and the
flicking-competitor guard.
`test_amend.py` checks orders amend atomically and cancel only when unavoidable.
`test_tp_trigger.py` arms a server-side take-profit when enabled (off by default).
`test_ws.py` checks the websocket book feed populates the cache and REST falls back.
`test_liquidity.py` checks the fallback prefers a live tape.
`test_equity.py` checks the daily equity report and its snapshot file.
`test_preflight.py` checks the admissibility check blocks a clear rejection only.
`test_orphan.py` checks the orphan-clear runs hourly, not every poll.

## Ops

| Script | What it does |
| --- | --- |
| `check_orders.py` | print all open orders with client ids |
| `cancel_all.py` | cancel every open order |
| `status_all.py` | open orders plus hot pairs |
| `scan_spread.py` | print the spread of every pair |
| `big_buy.py` | buy a fixed notional at market |
| `hunter_forever.py` | keep `monitor.py` running |
| `kill_bot.py` | stop the bot processes |

## Remote / Links

- GitHub: [@natanim-kemal](https://github.com/natanim-kemal)
- Telegram: [@phnatanim](https://t.me/phnatanim)
- Email: reachnatanim@gmail.com

---

<div align="center">

Built for the <a href="https://polyester.com">Polyester</a> testnet by <a href="https://github.com/natanim-kemal">@natanim-kemal</a>

</div>
