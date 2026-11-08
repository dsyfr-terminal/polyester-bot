"""One-shot: show the bot's open quotes and the current hot-pairs signal."""
import json
import os
import sys

if __package__:
    from . import check_orders  # noqa: F401
    from .paths import state_file
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import check_orders  # noqa: F401
    from paths import state_file

try:
    with open(state_file("hot_pairs.json"), encoding="utf-8") as f:
        hot = json.load(f)
    if hot:
        print("hot pairs:", json.dumps(hot))
    else:
        print("hot pairs: none (all quiet)")
except OSError:
    print("hot pairs: no scan yet")
