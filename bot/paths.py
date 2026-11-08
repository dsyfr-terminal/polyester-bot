"""Where the bot finds its .env and keeps its runtime state.

An installed copy lives in site-packages, which is read-only, so the .env and
the state files live next to the project that runs the bot. STATE_DIR is the
working directory unless POLYESTER_STATE_DIR says otherwise.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("POLYESTER_STATE_DIR") or os.getcwd()


def env_file():
    """The .env to read: the state dir first, the script dir as fallback."""
    for d in (STATE_DIR, HERE):
        p = os.path.join(d, ".env")
        if os.path.isfile(p):
            return p
    return os.path.join(STATE_DIR, ".env")


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
