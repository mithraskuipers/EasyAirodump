#!/bin/bash
# configure_iface.sh - Pick which wifi interface handles internet
#                       and which stays unmanaged for airodump-ng
# Usage: sudo ./configure_iface.sh

CONF_FILE="/etc/NetworkManager/conf.d/99-unmanaged-wifi.conf"

if [[ $EUID -ne 0 ]]; then
    echo "[!] Needs root to change NetworkManager config. Re-running with sudo..."
    exec sudo "$0" "$@"
fi

if ! command -v nmcli &>/dev/null; then
    echo "[!] nmcli not found. This script requires NetworkManager."
    exit 1
fi

echo "=== Wifi Interface Role Configurator ==="
echo

declare -a NAMES
declare -a MACS
declare -a STATES

for path in /sys/class/net/*; do
    iface="$(basename "$path")"
    [[ "$iface" == "lo" ]] && continue
    [[ -d "$path/wireless" || -d "$path/phy80211" ]] || continue

    mac="$(cat "$path/address" 2>/dev/null || echo "unknown")"
    state="$(nmcli -t -f DEVICE,STATE device status 2>/dev/null | awk -F: -v d="$iface" '$1==d {print $2}')"
    [[ -z "$state" ]] && state="unmanaged"

    NAMES+=("$iface")
    MACS+=("$mac")
    STATES+=("$state")
done

if [[ ${#NAMES[@]} -eq 0 ]]; then
    echo "[!] No wireless interfaces found."
    exit 1
fi

echo "Detected wireless interfaces:"
echo
for i in "${!NAMES[@]}"; do
    printf "  [%d] %-10s  MAC: %-17s  NM state: %s\n" "$i" "${NAMES[$i]}" "${MACS[$i]}" "${STATES[$i]}"
done
echo

read -rp "Select interface number for INTERNET connectivity (blank to skip): " inet_sel
read -rp "Select interface number to reserve UNMANAGED for airodump-ng (blank to skip): " scan_sel

manage_iface() {
    local idx="$1"
    local iface="${NAMES[$idx]}"
    local mac="${MACS[$idx]}"
    echo "[*] Setting $iface back to managed by NetworkManager..."
    if [[ -f "$CONF_FILE" ]]; then
        sed -i "s/;interface-name:$iface//g; s/interface-name:$iface;//g; s/^unmanaged-devices=interface-name:$iface\$//" "$CONF_FILE"
        sed -i "/^unmanaged-devices=\s*$/d" "$CONF_FILE"
    fi
    nmcli device set "$iface" managed yes 2>/dev/null
}

unmanage_iface() {
    local idx="$1"
    local iface="${NAMES[$idx]}"
    echo "[*] Reserving $iface as unmanaged for airodump-ng..."

    mkdir -p "$(dirname "$CONF_FILE")"
    touch "$CONF_FILE"

    if ! grep -q "^\[keyfile\]" "$CONF_FILE" 2>/dev/null; then
        printf '[keyfile]\n' >> "$CONF_FILE"
    fi

    if ! grep -q "interface-name:$iface" "$CONF_FILE" 2>/dev/null; then
        if grep -q "^unmanaged-devices=" "$CONF_FILE"; then
            sed -i "s/^unmanaged-devices=\(.*\)/unmanaged-devices=\1;interface-name:$iface/" "$CONF_FILE"
        else
            printf 'unmanaged-devices=interface-name:%s\n' "$iface" >> "$CONF_FILE"
        fi
    fi

    nmcli device set "$iface" managed no 2>/dev/null
}

if [[ "$inet_sel" =~ ^[0-9]+$ ]] && (( inet_sel >= 0 && inet_sel < ${#NAMES[@]} )); then
    manage_iface "$inet_sel"
fi

if [[ "$scan_sel" =~ ^[0-9]+$ ]] && (( scan_sel >= 0 && scan_sel < ${#NAMES[@]} )); then
    if [[ -n "$inet_sel" && "$scan_sel" == "$inet_sel" ]]; then
        echo "[!] Cannot use the same interface for both roles. Skipping unmanage step."
    else
        unmanage_iface "$scan_sel"
    fi
fi

echo
echo "[*] Reloading NetworkManager configuration..."
nmcli general reload conf 2>/dev/null

echo
echo "[+] Current state:"
nmcli device status

echo
echo "[*] If an interface still shows managed, restart NetworkManager:"
echo "      sudo systemctl restart NetworkManager"
