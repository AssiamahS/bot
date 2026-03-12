#!/bin/bash
# VPS Deployment Script for Hyperliquid Market Maker
#
# Usage:
#   1. Get a VPS (Hetzner CX22 ~$4/mo, or DigitalOcean $6/mo - pick closest to AWS us-east-1 for low latency)
#   2. SSH into it: ssh root@YOUR_VPS_IP
#   3. Copy this repo: scp -r ~/hyperliquid-sol root@YOUR_VPS_IP:~/hyperliquid-sol
#   4. Run this script: bash ~/hyperliquid-sol/deploy_vps.sh
#
# The bot runs as a systemd service with auto-restart.
# Control it via Telegram commands (see /help).

set -e

echo "=== Hyperliquid Bot VPS Setup ==="

# System packages
apt-get update
apt-get install -y python3 python3-pip python3-venv git

# Create venv
cd /home/ubuntu/hyperliquid-sol
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install eth-account hyperliquid-python-sdk

# Create systemd service
cat > /etc/systemd/system/hl-bot.service << 'UNIT'
[Unit]
Description=Hyperliquid Market Maker Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/hyperliquid-sol
ExecStart=/home/ubuntu/hyperliquid-sol/.venv/bin/python3 trader.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

# Log to file + journal
StandardOutput=append:/home/ubuntu/hyperliquid-sol/trader.log
StandardError=append:/home/ubuntu/hyperliquid-sol/trader.log

# Graceful shutdown
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
UNIT

# Enable and start
systemctl daemon-reload
systemctl enable hl-bot
systemctl start hl-bot

echo ""
echo "=== Done! ==="
echo ""
echo "Commands:"
echo "  systemctl status hl-bot     # check status"
echo "  journalctl -u hl-bot -f     # live logs"
echo "  tail -f ~/hyperliquid-sol/trader.log  # log file"
echo "  systemctl restart hl-bot    # restart"
echo "  systemctl stop hl-bot       # stop"
echo ""
echo "Telegram commands: /help /status /set /params /pause /resume /stop"
