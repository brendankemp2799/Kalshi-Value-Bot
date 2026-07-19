#!/usr/bin/env bash
# Server setup script for the Arbitrage Betting Bot.
# Run once on a fresh Ubuntu 22.04 droplet as the ubuntu user.
# Idempotent — safe to re-run after updates.
set -euo pipefail

REPO_DIR="/opt/arbitrage-bot"
BOT_DIR="$REPO_DIR/arbitrage_betting_bot"

echo "==> Installing Python 3.8"
sudo apt-get update -q
sudo apt-get install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get update -q
sudo apt-get install -y python3.8 python3.8-distutils python3.8-venv curl

# Install pip for Python 3.8 if not present
if ! python3.8 -m pip --version &>/dev/null; then
    curl -sS https://bootstrap.pypa.io/get-pip.py | python3.8
fi

echo "==> Installing Python dependencies"
python3.8 -m pip install -r "$REPO_DIR/requirements.txt"

echo "==> Installing systemd service units"
sudo cp "$REPO_DIR/deploy/arbitrage-bot.service"       /etc/systemd/system/
sudo cp "$REPO_DIR/deploy/arbitrage-dashboard.service" /etc/systemd/system/

echo "==> Enabling services"
sudo systemctl daemon-reload
sudo systemctl enable arbitrage-bot arbitrage-dashboard

echo ""
echo "Setup complete. Next steps:"
echo "  1. Ensure $BOT_DIR/.env is in place (copy from your Mac)"
echo "  2. Ensure ~/.kalshi/private_key.pem is in place (chmod 600)"
echo "  3. sudo systemctl start arbitrage-bot arbitrage-dashboard"
echo "  4. sudo systemctl status arbitrage-bot"
echo "  5. sudo journalctl -u arbitrage-bot -f    (live logs)"
