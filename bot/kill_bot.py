"""Kill the gap hunter, its supervisor, and any leftover bot processes."""
import subprocess
import sys

ps = r"""
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'monitor|hunter_forever' -and $_.ProcessId -ne $PID } |
  ForEach-Object { Write-Output $_.ProcessId; Stop-Process -Id $_.ProcessId -Force }
"""
subprocess.run(["powershell", "-NoProfile", "-Command", ps])
print("done")
