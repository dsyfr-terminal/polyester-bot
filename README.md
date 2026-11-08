<div align="center">
  <img src="polyester.jpg" alt="Polyester" width="180">
  <h1>Polyester Gap Hunter</h1>
  <p>Market-making bot for the <a href="https://polyester.com">Polyester</a> testnet.<br>
  Buys the bid, asks under the offer, manages the ask with a bail-out rule,
  and ships every step to Telegram, ntfy and the journal.</p>
</div>

---

## How it works

The bot runs one position at a time and runs it as a complete cycle:

```
scan every USDT pair  ->  find a gap  ->  BUY at the bid  ->  ASK under the offer  ->  CYCLE DONE
```

### Gap mode

Every `SCAN_SECONDS` the bot pulls the top-of-book for all USDT pairs and picks
the widest spread whose depth clears `DEPTH_MIN_USD`. It buys at the bid and
asks one tick under the offer, so both legs are maker fills. The round trip
costs `0.20%` in fees, so a gap has to clear `GAP_MIN_PCT` (default `0.21%`)
before the bot takes it.

### Volume fallback mode

When no pair clears the fee bar, the bot still cycles the deepest, tightest
liquid pair. It loses roughly `1-3 USDT` per cycle and earns about `4,900 USDT`
of season volume per cycle - intentional, the season leaderboard counts volume,
not profit. `VOLUME_BASELINE` in `.env` seeds the tracker when there is no
`volume_state.json` yet.

### The bail-out rule

While our ask rests, if more than `BETTER_ASKS_LIMIT` better (lower) ask levels
sit under our price for more than `PERSIST_SECONDS`, the bot cancels and
re-asks one tick under the current best ask - a maker exit, even at a loss.
The re-ask then rests until the rule fires again.

### One-position rule

The bot never stacks trades. The exception is a hot override: if the candidate
pair moved more than `20%` in 24h, it cancels what it holds and takes the new
position anyway.

### Season volume tracker

Every completed cycle adds its volume to `volume_state.json` and posts a
milestone alert every `10,000 USDT`.

## Repository layout

```
.
├── bot/                 everything that runs on the machine (same folder = same .env)
│   ├── monitor.py           the bot: scanner, executor, bail-out rule
│   ├── polyester_farm.py    season volume / checkpoint helper
│   ├── big_buy.py           one-off market buy
│   ├── cancel_all.py        cancel every open order
│   ├── check_orders.py      print open orders
│   ├── hunter_forever.py    run monitor.py in a loop
│   ├── kill_bot.py          stop the bot
│   ├── scan_spread.py       print spreads across pairs
│   ├── status_all.py        open orders + hot pairs
│   └── .env.example         every setting, no secrets
├── deploy/
│   ├── polyester-bot.service systemd unit
│   └── deploy.sh            rsync code to the VM and restart the service
├── tests/
│   └── test_sell_loop.py    regression test for the ask/cancel storm
├── .github/workflows/deploy.yml
└── polyester.jpg
```

## Configuration

`bot/.env` holds the credentials and the tuning. `bot/.env.example` lists every
key with its default. The real file stays on the target machine and is
gitignored - a deploy never overwrites it.

| Key | Default | Meaning |
| --- | --- | --- |
| `GAP_MIN_PCT` | `0.21` | minimum spread to take in gap mode |
| `DEPTH_MIN_USD` | `50` | minimum depth on both sides |
| `SCAN_SECONDS` | `10` | how often to scan for a gap |
| `POLL_SECONDS` | `2` | how often to manage the open position |
| `PERSIST_SECONDS` | `60` | how long better asks must persist before the bail-out fires |
| `BETTER_ASKS_LIMIT` | `2` | how many better asks trigger the bail-out |
| `BUY_TIMEOUT_SECONDS` | `180` | cancel an unfilled buy after this long |
| `FALLBACK_*` | see example | volume fallback mode |
| `VOLUME_BASELINE` | `57000` | season volume to start from when no state file exists |

Alerts go out over Telegram (every alert), ntfy (routine alerts are capped at
one per minute because the free tier rate-limits bursts) and Discord if
`DISCORD_WEBHOOK_URL` is set. Everything is also printed to the journal.

## Running it

On the target machine:

```bash
cd /data/workspace/polyester
.venv/bin/python monitor.py
```

As a service:

```bash
sudo systemctl start polyester-bot
journalctl -u polyester-bot -f
```

## Daily claim

`deploy/polyester-claim.timer` claims the daily paper asset at **00:10 UTC**.
`Persistent=true`, so a run missed while the machine was stopped happens on
the next boot. Whatever the claim pays out arrives as extra balances, which
the bot adopts and sells back into USDT before it looks for the next gap.

```bash
systemctl list-timers polyester-claim.timer
journalctl -u polyester-claim.service
```

## Deploying

`main` is deployed to the instabox sandbox by GitHub Actions: the workflow
pushes `bot/` and `deploy/` over SSH, runs `deploy/deploy.sh` on the machine
(which rsyncs the code, reinstalls the systemd unit and restarts the service)
and then verifies the service is active.

Repository secrets and variables:

| Name | Kind | Value |
| --- | --- | --- |
| `INSTABOX_SSH_KEY` | secret | private key with access to the sandbox |
| `INSTABOX_HOST` | variable | `ssh.instabox.sh` |
| `INSTABOX_PORT` | variable | `2222` |
| `INSTABOX_USER` | variable | sandbox user |

Secrets, `.session` and `volume_state.json` are excluded from the rsync, so a
deploy never disturbs live credentials or the season volume counter.

To deploy by hand from this machine:

```bash
./deploy/deploy.sh          # on the VM, with the repo checked out
```

## Tests

`tests/test_sell_loop.py` replays the sell state machine against a fake
exchange. It reproduces the bug where the bot cancelled the ask it had just
placed - the `canceling` flag was never cleared once the order reached a
terminal state, so every re-ask was cancelled again on the next poll.

```bash
python tests/test_sell_loop.py
```

It exits non-zero if the bot cancels more than once per bail-out.

## Ops scripts

| Script | What it does |
| --- | --- |
| `check_orders.py` | print all open orders with client ids |
| `cancel_all.py` | cancel every open order |
| `status_all.py` | open orders plus hot pairs |
| `scan_spread.py` | print the spread of every pair |
| `big_buy.py` | buy a fixed notional at market |
| `hunter_forever.py` | keep `monitor.py` running |
| `kill_bot.py` | stop the bot processes |

## Notes

- The bot trades **testnet** funds (`api.testnet.polyester.com`).
- The sandbox network is flaky. The bot retries on boot, cancels orphaned
  orders at startup and treats balances as the source of truth for what it
  still holds.
- `polyester_farm.py` talks to the Polyester website behind Cloudflare, so it
  sends browser-like headers and reuses the session in `.session`.
