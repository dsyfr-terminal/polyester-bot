"""List ALL open orders on the account, cancel everything, verify empty."""

import hashlib
import json
import os
import time
import urllib.request
import urllib.error

import nacl.signing

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, ".env"), encoding="utf-8") as f:
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


def signed(method, path, query="", body=b""):
    ts = str(int(time.time() * 1000))
    canon = "\n".join([ts, method.upper(), path, query,
                       hashlib.sha256(body).hexdigest()])
    req = urllib.request.Request(API + path + (("?") + query if query else ""),
                                 data=body or None, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Origin", "https://testnet.polyester.com")
    if body:
        req.add_header("Content-Type", "application/json")
    req.add_header("X-API-KEY-ID", KEY)
    req.add_header("X-API-TIMESTAMP", ts)
    req.add_header("X-API-SIGNATURE", sk.sign(canon.encode()).signature.hex())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode(errors="replace")[:200]}


s, before = signed("GET", "/v1/orders/open")
orders = before.get("orders", []) if isinstance(before, dict) else []
print(f"open orders BEFORE: {len(orders)}")
for o in orders:
    print(" ", o.get("orderId"), o.get("symbol"), o.get("side"), o.get("origQty"),
          "@", o.get("price"), o.get("status"), o.get("clientOrderId", "")[:14])

if orders:
    import uuid
    body = json.dumps({"requestId": "manual-" + uuid.uuid4().hex[:12]}).encode()
    s, res = signed("POST", "/v1/orders/cancel-all", body=body)
    print("cancel-all:", s, str(res)[:200])
    time.sleep(2)
    s, after = signed("GET", "/v1/orders/open")
    left = after.get("orders", []) if isinstance(after, dict) else []
    print(f"open orders AFTER: {len(left)}")
    for o in left:
        print(" ", o.get("orderId"), o.get("symbol"), o.get("side"), o.get("status"))
else:
    print("nothing open — nothing to cancel")
