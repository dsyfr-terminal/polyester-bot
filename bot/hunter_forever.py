"""Supervisor: restart the gap hunter whenever it exits (crash, WiFi drop)."""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
print("[supervisor] gap hunter supervisor started", flush=True)

while True:
    p = subprocess.run([sys.executable, os.path.join(HERE, "monitor.py")], cwd=HERE)
    print(f"[supervisor] hunter exited ({p.returncode}); restarting in 10s", flush=True)
    time.sleep(10)
