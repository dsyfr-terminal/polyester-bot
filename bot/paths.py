"""Where the bot finds its .env and keeps its runtime state.

An installed copy lives in site-packages, which is read-only, so the .env and
the state files live next to the project that runs the bot. STATE_DIR is the
working directory unless POLYESTER_STATE_DIR says otherwise. HOME_ENV is the
copy the setup wizard writes, so the bot finds its credentials from any
directory.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("POLYESTER_STATE_DIR") or os.getcwd()
HOME_ENV = os.path.join(os.path.expanduser("~"), ".polyester-bot", ".env")


def env_file():
    """The .env to read: the state dir first, the home copy, then the script dir."""
    for p in (os.path.join(STATE_DIR, ".env"), HOME_ENV, os.path.join(HERE, ".env")):
        if os.path.isfile(p):
            return p
    return os.path.join(STATE_DIR, ".env")


def env_files():
    """Every .env to read, most specific first: the state dir, home, script dir."""
    out = []
    for p in (os.path.join(STATE_DIR, ".env"), HOME_ENV, os.path.join(HERE, ".env")):
        if os.path.isfile(p) and p not in out:
            out.append(p)
    return out


def state_file(name):
    """A writable path for a state file, keeping an existing one where it is."""
    for d in (STATE_DIR, HERE):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    for d in (STATE_DIR, HERE):
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return os.path.join(d, name)
    return os.path.join(STATE_DIR, name)
