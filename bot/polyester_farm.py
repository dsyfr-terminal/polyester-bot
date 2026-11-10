#!/usr/bin/env python3
"""Polyester testnet farming helper: daily claim + award poller + checkpoint reader.

Usage (env vars):
  POLYESTER_SESSION_JWT      use an existing session token (skips wallet login), or
  POLYESTER_PRIVATE_KEY      EOA private key that controls your smart account, and
  POLYESTER_SMART_ACCOUNT    your Polyester smart-account address (0x...)
  POLYESTER_SIGNER_ADDRESS   optional; defaults to the address derived from the key

Commands:
  python polyester_farm.py selftest           # verify endpoint reachability (no creds needed)
  python polyester_farm.py status             # today's daily-claim status
  python polyester_farm.py claim              # claim today's reward, poll until settled
  python polyester_farm.py awards             # list campaign awards (real-asset payouts)
  python polyester_farm.py set-destination --award-id ID --address 0x.. --confirm
  python polyester_farm.py checkpoints        # season recap stats (airdrop scorecard)
  python polyester_farm.py daily              # status + claim + awards in one run (cron)
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

if __package__:
    from .paths import env_files, state_file
else:
    from paths import env_files, state_file

API = "https://api.testnet.polyester.com"
SITE = "https://testnet.polyester.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def load_env():
    """Load KEY=VALUE pairs from every .env the bot reads (no override)."""
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


def api_key_headers(method, path, body_bytes):
    """Ed25519-signed headers: X-API-KEY-ID / TIMESTAMP / SIGNATURE."""
    seed = os.environ.get("POLYESTER_ED25519_SEED", "")
    key_id = os.environ.get("POLYESTER_API_KEY_ID", "")
    if not (seed and key_id):
        die("set POLYESTER_ED25519_SEED and POLYESTER_API_KEY_ID in .env "
            "for API-key calls")
    try:
        import nacl.signing
    except ImportError:
        die("pip install pynacl  (needed for API-key signing)")
    ts = str(int(time.time() * 1000))
    canon = "\n".join([ts, method.upper(), path, "",
                       hashlib.sha256(body_bytes).hexdigest()])
    sk = nacl.signing.SigningKey(bytes.fromhex(seed))
    return {"X-API-KEY-ID": key_id, "X-API-TIMESTAMP": ts,
            "X-API-SIGNATURE": sk.sign(canon.encode()).signature.hex()}


class ApiError(Exception):
    def __init__(self, status, code, payload):
        super().__init__(f"HTTP {status} {code}: {json.dumps(payload)[:300]}")
        self.status, self.code, self.payload = status, code, payload


def http(method, path, body=None, token=None, connect=False, api_key=False,
         retries=2):
    if not path.startswith("/"):
        path = "/" + path
    body_b = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=body_b, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Origin", SITE)
    req.add_header("Referer", SITE + "/")
    req.add_header("Accept-Language", "en-US,en;q=0.9")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    elif api_key:
        for k, v in api_key_headers(method, path, body_b or b"").items():
            req.add_header(k, v)
    if connect:
        req.add_header("Connect-Protocol-Version", "1")
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            payload = e.read().decode(errors="replace")
            try:
                parsed = json.loads(payload)
            except ValueError:
                if ("Just a moment" in payload or "cf_chl" in payload
                        or "Attention Required" in payload
                        or "<title>Cloudflare" in payload):
                    raise ApiError(e.code, "CLOUDFLARE_CHALLENGE",
                                   {"hint": "retry shortly or from another network"})
                parsed = {"raw": payload[:300]}
            code = parsed.get("code", "")
            last = ApiError(e.code, code, parsed)
            if e.code in (429, 503) or code in ("RATE_LIMIT_EXCEEDED", "UNAVAILABLE"):
                time.sleep(2 * (attempt + 1))
                continue
            raise last
        except (urllib.error.URLError, OSError) as e:
            last = ApiError(0, "NETWORK", {"error": str(e)})
            time.sleep(2 * (attempt + 1))
            continue
    raise last or ApiError(0, "NETWORK", {})


def die(msg, code=1):
    print("ERROR: " + msg, file=sys.stderr)
    hints = {
        "UNAUTHENTICATED": "log in first (set POLYESTER_SESSION_JWT or wallet env vars)",
        "AUTH_SESSION_KIND_NOT_ALLOWED": "API keys can't call this; need a session JWT",
        "SOCIAL_VERIFICATION_REQUIRED": "link X or Discord once in the web app, then retry",
        "CLAIM_UNAVAILABLE": "no reward available today; see 'status' for resetAt",
        "CLAIM_TEMPORARILY_UNAVAILABLE": "payout paused; claim is recorded, auto-retried later",
        "CLAIM_PAYOUT_UNAVAILABLE": "payout paused; claim is recorded, auto-retried later",
        "CLOUDFLARE_CHALLENGE": "Cloudflare rate-limited us; wait a minute and retry",
    }
    if msg in hints:
        print("HINT: " + hints[msg], file=sys.stderr)
    sys.exit(code)



def get_token():
    jwt = os.environ.get("POLYESTER_SESSION_JWT")
    if jwt:
        return jwt.strip()
    cache = state_file(".session")
    try:
        with open(cache, encoding="utf-8") as f:
            saved = json.load(f)
        exp = time.mktime(time.strptime(saved["expiresAt"], "%Y-%m-%dT%H:%M:%SZ")) - 300
        if saved.get("accessToken") and time.time() < exp:
            return saved["accessToken"]
    except (OSError, ValueError, KeyError):
        pass
    key = os.environ.get("POLYESTER_PRIVATE_KEY")
    smart = os.environ.get("POLYESTER_SMART_ACCOUNT")
    if not key or not smart:
        die("set POLYESTER_SESSION_JWT, or both POLYESTER_PRIVATE_KEY and "
            "POLYESTER_SMART_ACCOUNT")
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError:
        die("pip install eth-account  (needed for wallet login)")
    signer = os.environ.get("POLYESTER_SIGNER_ADDRESS") or Account.from_key(key).address
    ch = http("POST", "/v1/auth/wallet-challenge", {
        "smartAccountAddress": smart, "signerAddress": signer,
        "uri": "https://testnet.polyester.com", "purpose": "LOGIN"})
    message = ch.get("message")
    if not message:
        die("wallet-challenge returned no message: " + json.dumps(ch))
    sig = "0x" + Account.sign_message(encode_defunct(text=message), key).signature.hex()
    res = http("POST", "/v1/auth/login/wallet", {
        "smartAccountAddress": smart, "message": message, "signature": sig})
    token = res.get("accessToken")
    if not token:
        die("login failed: " + json.dumps(res))
    with open(cache, "w", encoding="utf-8") as f:
        json.dump({"accessToken": token,
                   "expiresAt": res.get("expiresAt", "")}, f)
    return token



def get_status(token):
    return http("GET", "/v1/trading/daily-claim", token=token)


def find_state(node):
    """Best-effort navigation of the claim state across shape variants."""
    if not isinstance(node, dict):
        return None
    for k in ("state", "status", "claimState"):
        if isinstance(node.get(k), str):
            return node[k]
    claim = node.get("claim")
    if isinstance(claim, dict):
        return find_state(claim)
    return None


def print_status(st):
    print(json.dumps(st, indent=2))
    reset = st.get("resetAt") or (st.get("campaign") or {}).get("resetAt")
    state = find_state(st)
    print(f"\nclaim state: {state or 'unknown'}   next reset: {reset or 'unknown'}")


def do_claim(token, subaccount=None):
    st = get_status(token)
    state = find_state(st)
    if state == "CLAIMED":
        print("already claimed today")
        print_status(st)
        return st
    body = {"subaccountId": subaccount} if subaccount else {}
    try:
        res = http("POST", "/v1/trading/daily-claim/claim", body, token=token)
    except ApiError as e:
        if e.code in ("CLAIM_UNAVAILABLE", "CONFLICT", "RATE_LIMIT_EXCEEDED"):
            print(f"claim not submitted ({e.code}); current status:")
            print_status(get_status(token))
            return None
        die(e.code or str(e))
    print("claim submitted:")
    print(json.dumps(res, indent=2))
    for _ in range(12):
        time.sleep(5)
        st = get_status(token)
        state = find_state(st)
        if state in ("CLAIMED", "PAYOUT_DELAYED", "UNAVAILABLE"):
            break
    print_status(st)
    return st



def list_awards(token):
    awards, page = [], None
    while True:
        body = {"limit": 100}
        if page:
            body["pageToken"] = page
        res = http("POST", "rewards.v1.RewardCampaignService/ListMyRewardAwards",
                   body, token=token, connect=True)
        awards += res.get("awards", [])
        page = res.get("nextPageToken")
        if not page:
            return awards, res.get("awardsMayChange")


def do_awards(token):
    awards, may_change = list_awards(token)
    if not awards:
        print("no campaign awards yet" + (
            "" if may_change is None else f" (more may come: {may_change})"))
        return awards
    for a in awards:
        need = "  <-- NEEDS DESTINATION" if a.get("fulfillmentState") == "AWAITING_DESTINATION" else ""
        print(f"[{a.get('fulfillmentState')}] {a.get('campaignName')} "
              f"{a.get('amountBaseUnits')} assetId={a.get('assetId')} "
              f"network={a.get('network')} awardId={a.get('awardId')}"
              + (f" dest={a.get('destinationAddress')}" if a.get("destinationAddress") else "")
              + (f" tx={a.get('transactionId')}" if a.get("transactionId") else "") + need)
    return awards


def do_set_destination(token, award_id, address, confirm):
    if not confirm:
        die("destination is WRITE-ONCE and cannot be changed; re-run with --confirm")
    awards, _ = list_awards(token)
    match = next((a for a in awards if a.get("awardId") == award_id), None)
    if not match:
        die(f"award {award_id} not found; run 'awards' to list ids")
    if match.get("destinationAddress"):
        die("award already has a destination: " + match["destinationAddress"])
    res = http("POST", "rewards.v1.RewardCampaignService/SetMyRewardDestination", {
        "awardId": award_id,
        "destinationAddress": address,
        "expectedRevision": str(match.get("fulfillmentRevision") or "1"),
        "requestId": str(uuid.uuid4()),
    }, token=token, connect=True)
    print("destination recorded:")
    print(json.dumps(res, indent=2))



def do_checkpoints(token, subaccount=None):
    body = {"subaccountId": subaccount} if subaccount else {}
    res = http("POST", "checkpoints.v1.CheckpointService/ListMyCheckpoints",
               body, token=token, connect=True)
    cps = res.get("checkpoints", [])
    if not cps:
        print("no checkpoints yet (no recorded season activity)")
        return
    for c in cps:
        print(f"season #{c.get('number')} {c.get('seasonStart')} -> {c.get('seasonEnd')} "
              f"(taken {c.get('takenAt')})")
        print(f"  volume=${c.get('volumeUsd')} trades={c.get('trades')} "
              f"orders={c.get('orders')} fillRate={c.get('fillRate')}")
        print(f"  dailyClaims={c.get('dailyClaims')} longestStreak={c.get('longestStreak')} "
              f"daysActive={c.get('daysActive')} markets={c.get('marketsTraded')}")
        print(f"  maker={c.get('makerTrades')} taker={c.get('takerTrades')} "
              f"cancelRatio={c.get('cancelRatio')} selfTrades={c.get('selfTrades')}")
        badges = ", ".join(b.get("badge", "?") for b in c.get("badges", [])) or "-"
        print(f"  badges: {badges}  incomplete: {', '.join(c.get('incomplete', [])) or '-'}")
        tops = c.get("topPairs", [])[:5]
        if tops:
            print("  top pairs: " + ", ".join(
                f"{t.get('symbol')}(${t.get('volumeUsd')})" for t in tops))



def do_selftest():
    """Assert unauthenticated reachability + wallet-challenge shape (no creds)."""
    checks = []

    def check(name, fn, expect):
        try:
            got = fn()
            ok = got in expect
            checks.append(ok)
            print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got}")
        except ApiError as e:
            ok = e.status in expect
            checks.append(ok)
            print(f"{'PASS' if ok else 'FAIL'}  {name}: HTTP {e.status} {e.code}")

    check("GET /v1/trading/daily-claim",
          lambda: http("GET", "/v1/trading/daily-claim") and 200, [401])
    check("POST ListMyRewardAwards",
          lambda: http("POST", "rewards.v1.RewardCampaignService/ListMyRewardAwards",
                       {}, connect=True) and 200, [401])
    check("POST ListMyCheckpoints",
          lambda: http("POST", "checkpoints.v1.CheckpointService/ListMyCheckpoints",
                       {}, connect=True) and 200, [401])
    check("POST wallet-challenge (dummy)",
          lambda: http("POST", "/v1/auth/wallet-challenge", {
              "smartAccountAddress": "0x1111111111111111111111111111111111111111",
              "signerAddress": "0x2222222222222222222222222222222222222222",
              "uri": "https://testnet.polyester.com", "purpose": "LOGIN"}) and 200,
          [200])
    if all(checks):
        print("selftest: all checks passed")
        return 0
    print("selftest: FAILED", file=sys.stderr)
    return 1


def do_me():
    print(json.dumps(http("GET", "/v1/auth/me", api_key=True), indent=2))


def do_balances():
    rows = http("GET", "/v1/balances", api_key=True).get("balances", [])
    for b in rows:
        try:
            total = sum(float(b.get(k) or 0) for k in
                        ("trading", "reserved", "funding"))
        except ValueError:
            total = 1
        if total:
            print("{:8} trading={:>24}  reserved={:>20}  funding={}".format(
                b.get("symbol", "?"), b.get("trading", "0"),
                b.get("reserved", "0"), b.get("funding", "0")))
    if not rows:
        print("(no balance rows returned)")



def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    c = sub.add_parser("claim")
    c.add_argument("--subaccount", help="public ID of an owned subaccount")
    sub.add_parser("awards")
    d = sub.add_parser("set-destination")
    d.add_argument("--award-id", required=True)
    d.add_argument("--address", required=True, help="MAINNET payout address, correct chain")
    d.add_argument("--confirm", action="store_true",
                   help="acknowledge the destination is permanent")
    k = sub.add_parser("checkpoints")
    k.add_argument("--subaccount")
    sub.add_parser("daily")
    sub.add_parser("selftest")
    sub.add_parser("me")
    sub.add_parser("balances")
    args = p.parse_args()

    if args.cmd == "selftest":
        sys.exit(do_selftest())
    if args.cmd == "me":
        do_me()
        return
    if args.cmd == "balances":
        do_balances()
        return

    token = get_token()
    try:
        if args.cmd == "status":
            print_status(get_status(token))
        elif args.cmd == "claim":
            do_claim(token, args.subaccount)
        elif args.cmd == "awards":
            do_awards(token)
        elif args.cmd == "set-destination":
            do_set_destination(token, args.award_id, args.address, args.confirm)
        elif args.cmd == "checkpoints":
            do_checkpoints(token, args.subaccount)
        elif args.cmd == "daily":
            st = get_status(token)
            state = find_state(st)
            if state in (None, "AVAILABLE", "PROCESSING"):
                do_claim(token)
            else:
                print(f"claim state today: {state}")
            print()
            do_awards(token)
    except ApiError as e:
        die(e.code or str(e))


if __name__ == "__main__":
    main()
