#!/bin/bash
# start.sh - Launch the airodump-ng live web viewer
# Usage:  sudo ./start.sh

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$SCRIPT_DIR"

if [[ $EUID -ne 0 ]]; then
    echo "[!] Needs root for airodump-ng. Re-running with sudo..."
    exec sudo "$0"
fi

echo "[*] Starting web app..."
exec python3 "$SCRIPT_DIR/web_viewer.py"
