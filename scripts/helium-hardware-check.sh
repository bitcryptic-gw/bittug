#!/bin/bash
# helium-hardware-check.sh — detect whether Helium concentrator hardware is
# attached (ATECC608A secure element on I2C-1 @ 0x60).
#
# Used as ExecCondition= in pktfwd.service and gateway-rs.service:
#   exit 0  → hardware present, proceed with the unit start
#   exit 77 → hardware absent, skip the unit (systemd: skipped, not failed,
#             and a skipped start does not count toward the rate limit)
#
# Also maintains a marker file (/run/gateway/helium-hardware-present) that
# gateway-ui reads so a hardware-absent device renders "not configured"
# instead of a spurious Helium "fault" / "Helium Offline" alert.
#
# Run as root (both units have no User=, so ExecCondition runs as root).
set -euo pipefail

I2C_BUS=1
ECC_ADDR=0x60
MARKER_DIR="/run/gateway"
MARKER="${MARKER_DIR}/helium-hardware-present"

log() { echo "[helium-hw-check] $*"; }

absent() {
    rm -f "$MARKER"
    log "No Helium hardware detected (no ATECC608A at i2c-${I2C_BUS}:${ECC_ADDR}) — skipping pktfwd/gateway-rs"
    exit 77
}

present() {
    mkdir -p "$MARKER_DIR"
    : > "$MARKER"
    chmod 644 "$MARKER"
    log "Helium hardware detected (ATECC608A at i2c-${I2C_BUS}:${ECC_ADDR})"
    exit 0
}

if ! command -v i2cdetect >/dev/null 2>&1; then
    log "WARNING: i2cdetect not found (i2c-tools missing) — treating as no hardware"
    absent
fi

if [ ! -e "/dev/i2c-${I2C_BUS}" ]; then
    log "WARNING: /dev/i2c-${I2C_BUS} not present — treating as no hardware"
    absent
fi

# Read-mode single-address probe. The ATECC608A answers read probes (a
# quick-write probe can miss it), so -r is required. Checks the 0x60 row's
# first data cell is "60" (present) rather than "--" (absent).
if i2cdetect -y -r "${I2C_BUS}" "${ECC_ADDR}" "${ECC_ADDR}" 2>/dev/null \
    | awk '$1 == "60:" && $2 == "60" { found = 1 } END { exit !found }'; then
    present
fi

absent
