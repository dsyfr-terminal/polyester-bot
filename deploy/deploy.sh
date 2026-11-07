#!/usr/bin/env bash
# Sync bot code to the instabox sandbox and restart the service.
# Runs ON the target machine; the repo is expected at $REPO_ROOT.
set -euo pipefail

APP_DIR=${APP_DIR:-/data/workspace/polyester}
REPO_ROOT=${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}

mkdir -p "$APP_DIR"

# Code only. .env, .session and volume_state.json live on the VM and are
# never overwritten by a deploy.
rsync -a \
  --exclude '.env' --exclude '.env.example' \
  --exclude '.session' --exclude 'volume_state.json' --exclude 'hot_pairs.json' \
  --exclude '__pycache__' --exclude '*.bak.*' \
  "$REPO_ROOT/bot/" "$APP_DIR/"

sudo install -m 644 "$REPO_ROOT/deploy/polyester-bot.service" /etc/systemd/system/polyester-bot.service
sudo systemctl daemon-reload
sudo systemctl restart polyester-bot

sleep 5
if systemctl is-active --quiet polyester-bot; then
  echo "polyester-bot: active"
else
  echo "polyester-bot: FAILED to start" >&2
  journalctl -u polyester-bot -n 30 --no-pager >&2
  exit 1
fi
journalctl -u polyester-bot -n 5 --no-pager
