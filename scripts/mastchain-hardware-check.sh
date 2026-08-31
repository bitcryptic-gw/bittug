#!/bin/bash
# mastchain-hardware-check.sh — detect whether an RTL-SDR-class USB device is
# attached (MastChain's AIS-catcher needs one to receive AIS at ~162 MHz).
#
# Two modes, selected by the first argument:
#
#   (default, no args) — presence probe: exit 0 if any RTL-SDR-class device is
#     present, exit 77 if none. Used as ExecCondition= on depin-mastchain.service
#     so a device with no dongle skips the unit entirely (systemd: exit 77 =
#     skipped, not failed, and a skipped start does not count toward the rate
#     limit) instead of crash-looping the container on "No devices available"
#     the way upstream's mastradar.service did.
#
#   --count — print the number of matching RTL-SDR devices and exit 0. Used by
#     gateway-ui at status-poll time for the live "no hardware" badge AND the
#     one-dongle-one-spectrum warning (which only fires when exactly ONE dongle
#     is present and readsb is already holding it).
#
# Probe method: read sysfs (idVendor + idProduct under /sys/bus/usb/devices/*).
# Dependency-free — no lsusb/usbutils needed on minimal Debian — and the reads
# are world-readable, so the same script runs as root (ExecCondition) and as the
# unprivileged gateway-ui user (UI probe) with identical results. Unlike the
# Helium ATECC608A I2C probe, there is no flakiness/settle window, so no retry
# logic and no shared marker file (single consumer).
#
# Device IDs: the same RTL-SDR vendor:product pairs the 99-rtlsdr.rules udev
# rule matches (Realtek RTL2832U / RTL2838 / RTL2840).
#
# Exit codes:
#   exit 0  → hardware present (probe mode), or --count printed
#   exit 77 → hardware absent (probe mode only — skip the unit)
set -euo pipefail

VENDOR="0bda"
PRODUCTS="2832 2838 2840"

log() { echo "[mastchain-hw-check] $*"; }

# Count matching RTL-SDR devices. Iterates the enumerated device children of
# /sys/bus/usb/devices/; a device matches when its idVendor == 0bda and its
# idProduct is one of the RTL-SDR family.
rtlsdr_count() {
    local count=0 v p dev
    for dev in /sys/bus/usb/devices/*/idVendor; do
        [ -f "$dev" ] || continue
        v=$(cat "$dev" 2>/dev/null || true)
        [ "$v" = "$VENDOR" ] || continue
        p=$(cat "${dev%/idVendor}/idProduct" 2>/dev/null || true)
        case " $PRODUCTS " in
            *" $p "*) count=$((count + 1)) ;;
        esac
    done
    printf '%s' "$count"
}

count=$(rtlsdr_count)

if [ "${1:-}" = "--count" ]; then
    echo "$count"
    exit 0
fi

if [ "$count" -gt 0 ]; then
    log "RTL-SDR hardware detected (${count} device(s))"
    exit 0
fi
log "No RTL-SDR hardware detected (none of $VENDOR:$PRODUCTS under /sys/bus/usb/devices) — skipping depin-mastchain"
exit 77
