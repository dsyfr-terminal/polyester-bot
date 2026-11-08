<div align="center">

<img src="polyester.jpg" alt="Polyester" width="120" height="120" style="border-radius:50%; object-fit:cover;" />

# ✦ Polyester Gap Hunter

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
  A gap must clear the `0.20%` round-trip fee, so `GAP_MIN_PCT` defaults to `0.21%`.
- **Volume fallback** — when nothing clears the fee bar, the deepest tightest pair is
  cycled anyway to keep season volume up.
- **Bail-out rule** — if more than `BETTER_ASKS_LIMIT` better asks sit under our
  price for `PERSIST_SECONDS`, the ask is cancelled and re-placed under them
  (maker exit, even at a loss). It then rests until the rule fires again.
- **One position** — never stacks trades; the exception is a `>20%` 24h mover.
- **Volume tracker** — every cycle adds to `volume_state.json`, milestone alert
  every 10,000 USDT.

## Project Structure

```
├── bot/
│   ├── monitor.py            # the bot: scanner, executor, bail-out rule
│   ├── polyester_farm.py     # season volume, daily paper-asset claim
│   ├── check_orders.py       # print open orders
│   ├── cancel_all.py         # cancel every open order
│   ├── status_all.py         # open orders + hot pairs
│   ├── scan_spread.py        # spread of every pair
│   ├── big_buy.py            # one-off market buy
│   ├── hunter_forever.py     # run monitor.py in a loop
│   ├── kill_bot.py           # stop the bot
│   └── .env.example          # every setting, no secrets
├── deploy/
│   ├── polyester-bot.service    # systemd unit for the bot
│   ├── polyester-claim.service  # daily paper-asset claim
│   ├── polyester-claim.timer    # 00:10 UTC, persistent
│   └── deploy.sh                # install code + units, restart
├── tests/
│   ├── test_sell_loop.py        # regression test for the cancel storm
│   └── test_freeze_detect.py    # detects a paused sandbox from clock skew
├── .github/workflows/
│   ├── deploy.yml               # push to main → deploy to the sandbox
│   └── keepalive.yml            # every 10 min: keep the sandbox awake
└── polyester.jpg
```

## Configuration

`bot/.env` holds the credentials and tuning; `bot/.env.example` lists every key.
The real file stays on the machine and is gitignored — a deploy never overwrites it.

| Key | Default | Meaning |
| --- | --- | --- |
| `GAP_MIN_PCT` | `0.21` | minimum spread to take in gap mode |
| `DEPTH_MIN_USD` | `50` | minimum depth on both sides |
| `SCAN_SECONDS` | `10` | how often to scan for a gap |
| `POLL_SECONDS` | `2` | how often to manage the open position |
| `PERSIST_SECONDS` | `60` | how long better asks must persist before the bail-out fires |
| `BETTER_ASKS_LIMIT` | `2` | how many better asks trigger the bail-out |
| `BUY_TIMEOUT_SECONDS` | `180` | cancel an unfilled buy after this long |
| `FREEZE_SECONDS` | `300` | wall-vs-monotonic clock skew that counts as a paused sandbox |
| `STALL_SECONDS` | `900` | how long the loop may hang before it counts as a pause |
| `FALLBACK_*` | see example | volume fallback mode |
| `VOLUME_BASELINE` | `57000` | season volume to start from with no state file |

Alerts go to **Telegram** (every alert), **ntfy** (routine alerts capped at one per
minute — the free tier rate-limits bursts) and **Discord** if `DISCORD_WEBHOOK_URL`
is set. Everything is also printed to the journal.

## Deploy

Push to `main` → GitHub Actions → SSH → `deploy/deploy.sh` → systemd restart → verify.

| Name | Kind | Value |
| --- | --- | --- |
| `INSTABOX_SSH_KEY` | secret | private key with access to the sandbox |
| `INSTABOX_HOST` | variable | `ssh.instabox.sh` |
| `INSTABOX_PORT` | variable | `2222` |
| `INSTABOX_USER` | variable | sandbox user |

Secrets, `.session` and `volume_state.json` are never touched by a deploy.

`keepalive.yml` runs every 20 minutes and opens an SSH session: the sandbox pauses
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

`tests/test_sell_loop.py` replays the sell state machine against a fake exchange.
It reproduces the bug where the bot cancelled the ask it had just placed — the
`canceling` flag was never cleared once the order reached a terminal state, so
every re-ask was cancelled again on the next poll.

```bash
python tests/test_sell_loop.py
```

Exits non-zero if the bot cancels more than once per bail-out.

`tests/test_freeze_detect.py` pulls the real freeze-detection code out of
`bot/monitor.py` and drives it: a normal poll does nothing, the real 1h47m
pause triggers a re-sync, a stalled loop triggers it too, and an unreachable
exchange keeps the position instead of forgetting it.

```bash
python tests/test_freeze_detect.py
```

Both run in CI on every push, before anything is deployed.

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
