#!/bin/bash
# start_scan.sh - Launch airodump-ng writing CSV every 1 second
# Usage: sudo ./start_scan.sh [interface]
# Default interface: rt5370

IFACE="${1:-rt5370}"
PREFIX="scan"
OUTDIR="$(dirname "$(readlink -f "$0")")"
cd "$OUTDIR" || exit 1

# Clean previous run files (optional)
rm -f "${PREFIX}"-*.csv "${PREFIX}"-*.cap "${PREFIX}"-*.kismet.csv "${PREFIX}"-*.kismet.netxml 2>/dev/null

echo "[*] Starting airodump-ng on interface: $IFACE"
echo "[*] Writing CSV every 1 second → ${PREFIX}-01.csv"
echo "[*] Press Ctrl+C to stop"
echo

# --output-format csv   → only CSV (no huge pcap)
# --write-interval 1    → update file every second (realtime)
# -w PREFIX             → creates PREFIX-01.csv
exec airodump-ng \
    --output-format csv \
    --write-interval 1 \
    -w "$PREFIX" \
    "$IFACE"
