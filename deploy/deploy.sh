#!/usr/bin/env bash
set -euo pipefail

APP_DIR=${APP_DIR:-/data/workspace/polyester}
REPO_ROOT=${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}

mkdir -p "$APP_DIR"

find "$APP_DIR" -maxdepth 1 -name '*.py' -delete
cp "$REPO_ROOT"/bot/*.py "$APP_DIR"/
chmod +x "$APP_DIR"/*.py

sudo install -m 644 "$REPO_ROOT/deploy/polyester-bot.service" /etc/systemd/system/polyester-bot.service
sudo install -m 644 "$REPO_ROOT/deploy/polyester-claim.service" /etc/systemd/system/polyester-claim.service
sudo install -m 644 "$REPO_ROOT/deploy/polyester-claim.timer" /etc/systemd/system/polyester-claim.timer
sudo systemctl daemon-reload
sudo systemctl enable --now polyester-claim.timer
sudo systemctl restart polyester-bot

sleep 5
if systemctl is-active --quiet polyester-bot; then
  echo "polyester-bot: active"
else
  echo "polyester-bot: FAILED to start" >&2
  journalctl -u polyester-bot -n 30 --no-pager >&2
  exit 1
fi
systemctl list-timers polyester-claim.timer --no-pager
journalctl -u polyester-bot -n 5 --no-pager
