"""Where the bot reads .env and keeps its state (bot/paths.py).

An installed copy lives in a read-only site-packages, so the .env and the
state files belong to the directory the bot runs in.

Usage:  python tests/test_paths.py
Exit 0 = pass, Exit 1 = fail
"""

import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.abspath(os.path.join(HERE, "..", "bot"))
PATHS = os.path.join(BOT, "paths.py")

ok = True


def check(label, good, detail=""):
    global ok
    ok = ok and good
    print("%-6s %s%s" % ("PASS" if good else "FAIL", label,
                         (" -> " + detail) if detail else ""))


def load(state_dir):
    os.environ["POLYESTER_STATE_DIR"] = state_dir
    spec = importlib.util.spec_from_file_location("paths_under_test", PATHS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with tempfile.TemporaryDirectory() as tmp:
    work = os.path.join(tmp, "work")
    os.makedirs(work)
    mod = load(work)

    check("state dir is the override", mod.STATE_DIR == work, mod.STATE_DIR)

    env = os.path.join(work, ".env")
    with open(env, "w", encoding="utf-8") as f:
        f.write("POLYESTER_API_KEY_ID=demo\n")
    check("env file comes from the state dir", mod.env_file() == env, mod.env_file())

    state = os.path.join(work, "volume_state.json")
    with open(state, "w", encoding="utf-8") as f:
        f.write("{}")
    check("an existing state file stays where it is",
          mod.state_file("volume_state.json") == state,
          mod.state_file("volume_state.json"))

    fresh = mod.state_file("hot_pairs.json")
    check("a new state file lands in a writable dir",
          os.path.dirname(fresh) == work, fresh)

    os.remove(env)
    os.remove(state)
    fallback = os.path.join(BOT, ".env")
    want = fallback if os.path.isfile(fallback) else os.path.join(work, ".env")
    check("without a .env in the state dir it falls back",
          mod.env_file() == want, mod.env_file())

sys.path.insert(0, BOT)
import paths  # noqa: E402

check("the flat import works", paths.STATE_DIR == os.environ["POLYESTER_STATE_DIR"])

print()
print("PASS: env and state paths resolve to the working directory" if ok else "FAIL")
sys.exit(0 if ok else 1)
