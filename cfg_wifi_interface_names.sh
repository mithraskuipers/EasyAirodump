#!/bin/bash
# cfg_wifi_interface_names.sh - Assign a persistent name to a USB wifi adapter via udev
# Usage: sudo ./cfg_wifi_interface_names.sh

RULES_FILE="/etc/udev/rules.d/70-persistent-net.rules"

if [[ $EUID -ne 0 ]]; then
    echo "[!] Needs root to write udev rules. Re-running with sudo..."
    exec sudo "$0" "$@"
fi

echo "=== USB Wifi Adapter Namer ==="
echo

declare -a NAMES
declare -a MACS
declare -a DRIVERS

for path in /sys/class/net/*; do
    iface="$(basename "$path")"
    [[ "$iface" == "lo" ]] && continue
    [[ -d "$path/wireless" || -d "$path/phy80211" ]] || continue

    mac="$(cat "$path/address" 2>/dev/null || echo "unknown")"
    driver="unknown"
    if [[ -e "$path/device/driver" ]]; then
        driver="$(basename "$(readlink -f "$path/device/driver")")"
    fi

    NAMES+=("$iface")
    MACS+=("$mac")
    DRIVERS+=("$driver")
done

if [[ ${#NAMES[@]} -eq 0 ]]; then
    echo "[!] No wireless interfaces found."
    exit 1
fi

echo "Detected wireless interfaces:"
echo
for i in "${!NAMES[@]}"; do
    printf "  [%d] %-10s  MAC: %-17s  Driver: %s\n" "$i" "${NAMES[$i]}" "${MACS[$i]}" "${DRIVERS[$i]}"
done
echo

read -rp "Select interface number to name: " sel

if ! [[ "$sel" =~ ^[0-9]+$ ]] || (( sel < 0 || sel >= ${#NAMES[@]} )); then
    echo "[!] Invalid selection."
    exit 1
fi

mac="${MACS[$sel]}"
current="${NAMES[$sel]}"

if [[ "$mac" == "unknown" || -z "$mac" ]]; then
    echo "[!] Could not read MAC address for $current."
    exit 1
fi

echo
echo "Current interface: $current"
echo "MAC address:       $mac"
echo
read -rp "New persistent name for this adapter (e.g. rt5370): " newname

if [[ -z "$newname" ]]; then
    echo "[!] Name cannot be empty."
    exit 1
fi

if ! [[ "$newname" =~ ^[a-zA-Z0-9_]+$ ]]; then
    echo "[!] Name must contain only letters, numbers, or underscores."
    exit 1
fi

touch "$RULES_FILE"

if grep -qi "$mac" "$RULES_FILE" 2>/dev/null; then
    echo "[!] A rule for MAC $mac already exists:"
    grep -i "$mac" "$RULES_FILE"
    read -rp "Replace it? [y/N]: " replace
    if [[ "$replace" =~ ^[Yy]$ ]]; then
        sed -i "/$(echo "$mac" | sed 's/:/\\:/g')/d" "$RULES_FILE"
    else
        echo "[*] Aborted, no changes made."
        exit 0
    fi
fi

if grep -q "NAME=\"$newname\"" "$RULES_FILE" 2>/dev/null; then
    echo "[!] Name '$newname' is already used by an existing rule:"
    grep "NAME=\"$newname\"" "$RULES_FILE"
    exit 1
fi

echo "SUBSYSTEM==\"net\", ACTION==\"add\", ATTR{address}==\"$mac\", NAME=\"$newname\"" >> "$RULES_FILE"

echo
echo "[+] Rule added to $RULES_FILE:"
tail -n1 "$RULES_FILE"
echo
echo "[*] Reboot required for the new name to take effect:"
echo "      sudo reboot"
