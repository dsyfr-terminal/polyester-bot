"""One-shot check: list the bot's open BTC-USDT quotes."""
import hashlib
import json
import os
import time
import urllib.request
import urllib.error

import nacl.signing

if __package__:
    from .paths import env_file
else:
    from paths import env_file

with open(env_file(), encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

KEY = os.environ["POLYESTER_API_KEY_ID"]
sk = nacl.signing.SigningKey(bytes.fromhex(os.environ["POLYESTER_ED25519_SEED"]))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
API = "https://api.testnet.polyester.com"

path, query = "/v1/orders/open", "symbol=BTC-USDT"
ts = str(int(time.time() * 1000))
canon = "\n".join([ts, "GET", path, query, hashlib.sha256(b"").hexdigest()])
req = urllib.request.Request(f"{API}{path}?{query}", method="GET")
req.add_header("User-Agent", UA)
req.add_header("Accept", "application/json")
req.add_header("Origin", "https://testnet.polyester.com")
req.add_header("X-API-KEY-ID", KEY)
req.add_header("X-API-TIMESTAMP", ts)
req.add_header("X-API-SIGNATURE", sk.sign(canon.encode()).signature.hex())
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    for o in data.get("orders", []):
        print(o["side"], o["origQty"], "@", o["price"], o["status"], o["clientOrderId"][:12])
    print(len(data.get("orders", [])), "open orders")
except urllib.error.HTTPError as e:
    print("ERR", e.code, e.read().decode(errors="replace")[:300])
