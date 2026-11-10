"""The setup wizard: seed formats, .env handling, no baked-in account.

Covers:
  1. normalize_seed() accepts hex and base64 Ed25519 secrets, rejects junk
  2. merge_env() replaces the keys it owns and keeps everything else
  3. env_file() finds the home copy when the working directory has none
  4. monitor.py carries no account id of its own and derives one at runtime

Usage:  python tests/test_setup.py
Exit 0 = pass, Exit 1 = fail
"""

import base64
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.abspath(os.path.join(HERE, "..", "bot"))
sys.path.insert(0, BOT)

ok = True


def check(label, good, detail=""):
    global ok
    ok = ok and good
    print("%-6s %s%s" % ("PASS" if good else "FAIL", label,
                         (" -> " + detail) if detail else ""))


import paths  # noqa: E402

try:
    import setup  # noqa: E402
except ImportError:
    setup = None

check("the setup wizard exists", setup is not None, "bot/setup.py")

if setup is not None:
    seed_hex = "ab" * 32
    seed_b64 = base64.b64encode(bytes.fromhex(seed_hex)).decode()

    check("a hex seed stays hex",
          setup.normalize_seed(seed_hex) == seed_hex,
          setup.normalize_seed(seed_hex))
    check("a 0x prefix is dropped",
          setup.normalize_seed("0x" + seed_hex) == seed_hex)
    check("a base64 seed becomes hex",
          setup.normalize_seed(seed_b64) == seed_hex,
          setup.normalize_seed(seed_b64))
    try:
        setup.normalize_seed("nonsense")
        check("a short secret is rejected", False, "no error raised")
    except ValueError as e:
        check("a short secret is rejected", True, str(e))

    with tempfile.TemporaryDirectory() as tmp:
        env = os.path.join(tmp, ".env")
        with open(env, "w", encoding="utf-8") as f:
            f.write("# my tuning\nGAP_MIN_PCT=0.30\nPOLYESTER_API_KEY_ID=old\n")
        setup.merge_env(env, {"POLYESTER_API_KEY_ID": "ak_new",
                              "TELEGRAM_CHAT_ID": "42"})
        body = open(env, encoding="utf-8").read()
        check("the tuning survives", "GAP_MIN_PCT=0.30" in body)
        check("the comment survives", "# my tuning" in body)
        check("the owned key is replaced",
              "POLYESTER_API_KEY_ID=ak_new" in body
              and "POLYESTER_API_KEY_ID=old" not in body)
        check("a new key is added", "TELEGRAM_CHAT_ID=42" in body)
        check("the owned key appears once",
              body.count("POLYESTER_API_KEY_ID=") == 1,
              str(body.count("POLYESTER_API_KEY_ID=")))

with tempfile.TemporaryDirectory() as tmp:
    home = os.path.join(tmp, "home.env")
    work = os.path.join(tmp, "work")
    os.makedirs(work)
    old_home = getattr(paths, "HOME_ENV", None)
    paths.HOME_ENV = home
    paths.STATE_DIR = work

    with open(home, "w", encoding="utf-8") as f:
        f.write("POLYESTER_API_KEY_ID=ak_home\n")
    check("the home copy is found when the directory has none",
          paths.env_file() == home, paths.env_file())

    local = os.path.join(work, ".env")
    with open(local, "w", encoding="utf-8") as f:
        f.write("POLYESTER_API_KEY_ID=ak_local\n")
    check("the working directory wins",
          paths.env_file() == local, paths.env_file())

    paths.HOME_ENV = old_home
    paths.STATE_DIR = os.path.join(tmp, "work")

src = open(os.path.join(BOT, "monitor.py"), encoding="utf-8").read()
check("no account id is baked into the bot",
      "MUYiAxF1Kkv" not in src)
check("the bot asks the exchange for its account id",
      "def rest_me(" in src and "rest_me()" in src)

print()
print("PASS: the setup wizard is ready" if ok else "FAIL")
sys.exit(0 if ok else 1)
