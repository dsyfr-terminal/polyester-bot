"""One-shot: show the bot's open quotes and the current hot-pairs signal."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import check_orders  # noqa: F401  (prints open orders)

try:
    with open(os.path.join(HERE, "hot_pairs.json"), encoding="utf-8") as f:
        hot = json.load(f)
    if hot:
        print("hot pairs:", json.dumps(hot))
    else:
        print("hot pairs: none (all quiet)")
except OSError:
    print("hot pairs: no scan yet")
