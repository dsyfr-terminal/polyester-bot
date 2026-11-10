"""Guided setup: ask for the credentials, check them, write the .env.

Run it with `polyester-bot setup` (npm) or `python setup.py` (source tree).
It asks for the API key, the Ed25519 seed and the Telegram bot token plus the
chat id, derives the Polyester account id from the key, proves both channels
work and then writes the .env the bot reads.
"""

import base64
import getpass
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import nacl.signing

if __package__:
    from .paths import HOME_ENV, STATE_DIR, env_files
else:
    from paths import HOME_ENV, STATE_DIR, env_files

API = "https://api.testnet.polyester.com"
SITE = "https://testnet.polyester.com"
TELEGRAM_API = "https://api.telegram.org"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


class SetupError(Exception):
    pass


def normalize_seed(raw):
    """The Ed25519 seed as 64 hex characters, from hex or base64."""
    text = (raw or "").strip()
    if text[:2].lower() == "0x":
        text = text[2:]
    if len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text):
        return text.lower()
    padded = text + "=" * (-len(text) % 4)
    try:
        raw_bytes = base64.b64decode(padded, validate=True)
    except Exception:
        raw_bytes = b""
    if len(raw_bytes) != 32:
        raise ValueError("the seed must be 64 hex characters or base64 of 32 bytes")
    return raw_bytes.hex()


def merge_env(path, values):
    """Write values into the .env at path, keeping every other line."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    out = []
    seen = set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in values:
            if key in seen:
                continue
            out.append(key + "=" + values[key])
            seen.add(key)
        else:
            out.append(line)
    for key, value in values.items():
        if key not in seen:
            out.append(key + "=" + value)
    try:
        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(path, 0o600)
    except OSError as e:
        raise SetupError("cannot write " + path + ": " + str(e))


def signed_get(path, key_id, seed):
    """GET a signed endpoint with the given key id and hex seed."""
    ts = str(int(time.time() * 1000))
    canon = "\n".join([ts, "GET", path, "", hashlib.sha256(b"").hexdigest()])
    signature = nacl.signing.SigningKey(bytes.fromhex(seed)).sign(
        canon.encode()).signature.hex()
    req = urllib.request.Request(API + path, method="GET")
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    req.add_header("Origin", SITE)
    req.add_header("Referer", SITE + "/")
    req.add_header("X-API-KEY-ID", key_id)
    req.add_header("X-API-TIMESTAMP", ts)
    req.add_header("X-API-SIGNATURE", signature)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SetupError("the exchange refused " + path + ": " + body[:200])
    except OSError as e:
        raise SetupError("cannot reach the exchange: " + str(e))


def telegram(method, token, **params):
    """Call a Telegram bot method and return its result."""
    url = TELEGRAM_API + "/bot" + token + "/" + method
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            detail = json.loads(body).get("description", body[:200])
        except ValueError:
            detail = body[:200]
        raise SetupError("telegram " + method + " failed: " + str(detail))
    except OSError as e:
        raise SetupError("cannot reach telegram: " + str(e))
    if not data.get("ok"):
        raise SetupError("telegram " + method + " failed: "
                         + str(data.get("description", data)))
    return data.get("result", data)


def telegram_chat_ids(token):
    """Chat ids the bot has seen so far, oldest first."""
    updates = telegram("getUpdates", token, timeout=0) or []
    ids = []
    for update in updates:
        for field in ("message", "channel_post", "edited_message"):
            chat = (update.get(field) or {}).get("chat") or {}
            if chat.get("id") is not None and chat["id"] not in ids:
                ids.append(chat["id"])
    return ids


def ask(label, default=""):
    """A required line of text, with the current value offered as default."""
    hint = (" [" + default + "]") if default else ""
    while True:
        value = input(label + hint + ": ").strip()
        if value:
            return value
        if default:
            return default
        print("This one is needed.")


def ask_optional(label, default=""):
    """A line of text that may be left empty."""
    hint = (" [" + default + "]") if default else ""
    value = input(label + hint + ": ").strip()
    return value or default


def ask_secret(label, default=""):
    hint = " [kept]" if default else ""
    while True:
        value = getpass.getpass(label + hint + ": ").strip()
        if value:
            return value
        if default:
            return default
        print("This one is needed.")


def ask_optional_secret(label):
    """A secret that may be left empty."""
    value = getpass.getpass(label + " (optional, Enter to skip): ").strip()
    return value


def ask_chat_id(token, username):
    """The Telegram chat to alert: typed in, or found from a message to the bot."""
    while True:
        raw = input("Open @%s, press Start, then press Enter here "
                    "(or paste a chat id): " % username).strip()
        if raw and re.fullmatch(r"-?\d+", raw):
            return raw
        try:
            found = telegram_chat_ids(token)
        except SetupError as e:
            print(str(e))
            found = []
        if found:
            return str(found[-1])
        print("No chat id yet. Send /start to @%s and press Enter again, "
              "or paste the chat id." % username)


def defaults():
    """The values already on this machine, so the wizard can pre-fill them."""
    out = dict(os.environ)
    for path in env_files():
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        out.setdefault(k.strip(), v.strip())
        except OSError:
            pass
    return out


def targets():
    """Where the credentials go: the home copy, plus a .env already in the folder."""
    out = [HOME_ENV]
    local = os.path.join(STATE_DIR, ".env")
    if os.path.isfile(local):
        out.append(local)
    return out


def main():
    """Walk the user through the credentials and leave a working .env behind."""
    print("Polyester setup")
    print("Press Enter to keep the value in brackets. Testnet only.\n")

    have = defaults()
    key_id = ask("Polyester API key id (ak_...)",
                 have.get("POLYESTER_API_KEY_ID", ""))
    seed = normalize_seed(ask_secret(
        "Ed25519 seed (64 hex characters or base64)",
        have.get("POLYESTER_ED25519_SEED", "")))

    me = signed_get("/v1/auth/me", key_id, seed)
    account_id = str(me.get("accountId") or "")
    if not account_id:
        raise SetupError("the exchange returned no account id: "
                         + json.dumps(me)[:200])
    print("\nExchange ok: account %s (%s)"
          % (account_id, me.get("username", "unknown user")))

    token = ask_secret("Telegram bot token",
                       have.get("TELEGRAM_BOT_TOKEN", ""))
    bot = telegram("getMe", token)
    username = bot.get("username", "your bot")
    print("Telegram ok: @%s" % username)

    while True:
        chat_id = ask_chat_id(token, username)
        try:
            telegram("sendMessage", token, chat_id=chat_id,
                     text="Polyester setup: this chat gets the bot alerts.")
        except SetupError as e:
            print(str(e))
            print("Give another chat id.")
            continue
        break
    print("Telegram chat ok: %s" % chat_id)

    values = {
        "POLYESTER_API_KEY_ID": key_id,
        "POLYESTER_ED25519_SEED": seed,
        "POLYESTER_ACCOUNT_ID": account_id,
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_CHAT_ID": chat_id,
    }

    private_key = ask_optional_secret(
        "EOA private key (only for the daily paper-asset claim)")
    if private_key:
        values["POLYESTER_PRIVATE_KEY"] = private_key
        values["POLYESTER_SMART_ACCOUNT"] = ask_optional(
            "Smart account address (0x...)",
            str(me.get("rootSmartAccountAddress") or ""))

    for target in targets():
        merge_env(target, values)
    print("\nWrote %s" % " and ".join(targets()))
    print("Start the bot with: polyester-bot")


def cli():
    """Run the wizard and turn any failure into a one-line error."""
    try:
        main()
    except SetupError as e:
        print("SETUP FAILED: " + str(e), file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
