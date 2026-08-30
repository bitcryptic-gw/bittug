#!/bin/bash
# helium-hardware-check.sh — detect whether Helium concentrator hardware is
# attached (ATECC608A secure element on I2C-1 @ 0x60).
#
# Two modes, selected by the first argument:
#
#   probe (default) — perform the real I2C probe and record the result in the
#     marker file. Used as ExecCondition= on pktfwd.service, which is ordered
#     first (gateway-rs.service has After=pktfwd.service + Requires=pktfwd.service).
#     Only this unit ever touches the I2C bus, so there is exactly ONE real
#     probe per boot — the race between two back-to-back probes that caused a
#     false "absent" on a hardware-present device is eliminated by construction.
#
#   check — read only the marker file, never touch the I2C bus. Used as
#     ExecCondition= on gateway-rs.service. Because gateway-rs.service is
#     ordered After=pktfwd.service, the marker is guaranteed to already exist
#     (or be absent) by the time gateway-rs's condition runs — the probe result
#     is shared, not re-derived.
#
# Retry behaviour (probe mode only): the ATECC608A is documented as requiring
# a wake/settle window and can transiently NACK a probe that lands too early
# after power-on or during another I2C transaction (see the open Ecc608(Timeout)
# investigation — same I2C fragility). A single one-shot probe is therefore not
# trustworthy: probe mode retries a few times with a short settle delay so a
# hardware-present device is not skipped on a single flaky read.
#
# Exit codes (both modes):
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

MODE="${1:-probe}"

I2C_BUS=1
ECC_ADDR=0x60
MARKER_DIR="/run/gateway"
MARKER="${MARKER_DIR}/helium-hardware-present"

PROBE_ATTEMPTS=5
PROBE_DELAY_SEC=0.3

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

# ── check mode: marker only, no I2C access ───────────────────────────────
if [ "$MODE" = "check" ]; then
    if [ -f "$MARKER" ]; then
        log "Helium hardware detected (marker present from pktfwd probe)"
        exit 0
    fi
    log "No Helium hardware detected (no marker — pktfwd probe found none) — skipping gateway-rs"
    exit 77
fi

# ── probe mode ───────────────────────────────────────────────────────────
if ! command -v i2cdetect >/dev/null 2>&1; then
    log "WARNING: i2cdetect not found (i2c-tools missing) — treating as no hardware"
    absent
fi

if [ ! -e "/dev/i2c-${I2C_BUS}" ]; then
    log "WARNING: /dev/i2c-${I2C_BUS} not present — treating as no hardware"
    absent
fi

attempt=0
while [ "$attempt" -lt "$PROBE_ATTEMPTS" ]; do
    attempt=$((attempt + 1))

    # Read-mode single-address probe. The ATECC608A answers read probes (a
    # quick-write probe can miss it), so -r is required. Checks the 0x60 row's
    # first data cell is "60" (present) rather than "--" (absent).
    if i2cdetect -y -r "${I2C_BUS}" "${ECC_ADDR}" "${ECC_ADDR}" 2>/dev/null \
        | awk '$1 == "60:" && $2 == "60" { found = 1 } END { exit !found }'; then
        present
    fi

    if [ "$attempt" -lt "$PROBE_ATTEMPTS" ]; then
        log "probe ${attempt}/${PROBE_ATTEMPTS}: no response at 0x60 — retrying in ${PROBE_DELAY_SEC}s"
        sleep "$PROBE_DELAY_SEC"
    fi
done

absent
