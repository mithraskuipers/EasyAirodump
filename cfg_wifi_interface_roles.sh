#!/bin/bash
# cfg_wifi_interface_roles.sh - Assign wifi interface roles: internet uplink,
#                       unmanaged for airodump-ng, or a standalone phone hotspot
# Usage: sudo ./cfg_wifi_interface_roles.sh

CONF_FILE="/etc/NetworkManager/conf.d/99-unmanaged-wifi.conf"

if [[ $EUID -ne 0 ]]; then
    echo "[!] Needs root to change NetworkManager config. Re-running with sudo..."
    exec sudo "$0" "$@"
fi

if ! command -v nmcli &>/dev/null; then
    echo "[!] nmcli not found. This script requires NetworkManager."
    exit 1
fi

echo "=== Wifi Interface Role Configurator (internet / airodump / hotspot) ==="
echo

declare -a NAMES
declare -a MACS
declare -a STATES
declare -a TYPES

for path in /sys/class/net/*; do
    iface="$(basename "$path")"
    [[ "$iface" == "lo" ]] && continue
    [[ -d "$path/wireless" || -d "$path/phy80211" ]] || continue

    mac="$(cat "$path/address" 2>/dev/null || echo "unknown")"
    state="$(nmcli -t -f DEVICE,STATE device status 2>/dev/null | awk -F: -v d="$iface" '$1==d {print $2}')"
    [[ -z "$state" ]] && state="unmanaged"

    devpath="$(readlink -f "$path/device" 2>/dev/null)"
    if [[ "$devpath" == *"/usb"* ]]; then
        type="usb"
    else
        type="onboard"
    fi

    NAMES+=("$iface")
    MACS+=("$mac")
    STATES+=("$state")
    TYPES+=("$type")
done

if [[ ${#NAMES[@]} -eq 0 ]]; then
    echo "[!] No wireless interfaces found."
    exit 1
fi

echo "Detected wireless interfaces:"
echo
for i in "${!NAMES[@]}"; do
    printf "  [%d] %-10s  MAC: %-17s  Type: %-7s  NM state: %s\n" "$i" "${NAMES[$i]}" "${MACS[$i]}" "${TYPES[$i]}" "${STATES[$i]}"
done
echo

read -rp "Select interface number for INTERNET connectivity (blank to skip): " inet_sel
read -rp "Select interface number to reserve UNMANAGED for airodump-ng (blank to skip): " scan_sel
read -rp "Select interface number to broadcast a phone HOTSPOT (blank to skip): " hs_sel

confirm_if_onboard() {
    local idx="$1"
    local role="$2"
    if [[ "${TYPES[$idx]}" == "onboard" ]]; then
        echo
        echo "[!] WARNING: ${NAMES[$idx]} looks like the Raspberry Pi's onboard wifi,"
        echo "    probably the one you're using for SSH or VNC right now."
        echo "    This setting is remembered permanently, so after this the Pi"
        echo "    will likely stop auto-connecting to your normal wifi, even"
        echo "    after a reboot. You may need a monitor and keyboard, or a"
        echo "    wired connection, to reach it again."
        echo
        read -rp "    Assign the $role role to ${NAMES[$idx]} anyway? [y/N]: " confirm
        if [[ "$confirm" =~ ^[Yy]$ ]]; then
            return 0
        fi
        echo "    [*] Skipped."
        return 1
    fi
    return 0
}

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

hotspot_iface() {
    local idx="$1"
    local iface="${NAMES[$idx]}"
    local con_name="walkabout-hotspot"

    echo
    read -rp "SSID for the hotspot [PiRange]: " ssid
    ssid="${ssid:-PiRange}"
    while true; do
        read -rp "Password for the hotspot, min 8 chars [walkabout1]: " pass
        pass="${pass:-walkabout1}"
        (( ${#pass} >= 8 )) && break
        echo "[!] Password must be at least 8 characters."
    done

    echo "[*] Making sure $iface is managed by NetworkManager..."
    if [[ -f "$CONF_FILE" ]]; then
        sed -i "s/;interface-name:$iface//g; s/interface-name:$iface;//g; s/^unmanaged-devices=interface-name:$iface\$//" "$CONF_FILE"
        sed -i "/^unmanaged-devices=\s*$/d" "$CONF_FILE"
    fi
    nmcli device set "$iface" managed yes 2>/dev/null

    echo "[*] Starting hotspot '$ssid' on $iface..."
    nmcli connection delete "$con_name" &>/dev/null
    nmcli device wifi hotspot ifname "$iface" con-name "$con_name" ssid "$ssid" password "$pass"

    local gw
    gw="$(nmcli -g IP4.GATEWAY connection show "$con_name" 2>/dev/null)"
    [[ -z "$gw" ]] && gw="10.42.0.1"

    echo
    echo "[+] Hotspot is live, no internet or router needed:"
    echo "      SSID:     $ssid"
    echo "      Password: $pass"
    echo "      Connect your phone to that network, then open:"
    echo "      http://$gw:8080"
    echo
    echo "[*] To stop it later:  sudo nmcli connection down $con_name"
    echo "[*] To bring it back:  sudo nmcli connection up $con_name"
}

if [[ "$inet_sel" =~ ^[0-9]+$ ]] && (( inet_sel >= 0 && inet_sel < ${#NAMES[@]} )); then
    manage_iface "$inet_sel"
fi

if [[ "$scan_sel" =~ ^[0-9]+$ ]] && (( scan_sel >= 0 && scan_sel < ${#NAMES[@]} )); then
    if [[ -n "$inet_sel" && "$scan_sel" == "$inet_sel" ]]; then
        echo "[!] Cannot use the same interface for both roles. Skipping unmanage step."
    elif confirm_if_onboard "$scan_sel" "airodump (unmanaged)"; then
        unmanage_iface "$scan_sel"
    fi
fi

if [[ "$hs_sel" =~ ^[0-9]+$ ]] && (( hs_sel >= 0 && hs_sel < ${#NAMES[@]} )); then
    if [[ ( -n "$inet_sel" && "$hs_sel" == "$inet_sel" ) || ( -n "$scan_sel" && "$hs_sel" == "$scan_sel" ) ]]; then
        echo "[!] Hotspot interface must be different from the other roles. Skipping."
    elif confirm_if_onboard "$hs_sel" "hotspot"; then
        hotspot_iface "$hs_sel"
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
