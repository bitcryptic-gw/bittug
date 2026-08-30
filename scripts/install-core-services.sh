#!/bin/bash
# install-core-services.sh — deploy the gateway's core systemd unit files to
# /etc/systemd/system/.
# Safe to re-run. Does NOT enable or start services.
# Run as root.
#
# Covers the repo-owned units that the OTA restart loop restarts:
#   - pktfwd.service, gateway-rs.service, gateway-ui.service
#   - readsb.service drop-in override (readsb.service itself is upstream-managed
#     by the Wingbits installer; the repo owns only the override that makes it
#     tolerate absent SDR hardware)
# Without this re-copy, a git pull during OTA that changes one of these files
# would leave the /etc/systemd/system/ copy stale, and systemctl restart would
# silently keep running the old unit definition.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run as root (sudo)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT_SRC_DIR="${SCRIPT_DIR}/../systemd"
UNIT_DST_DIR="/etc/systemd/system"

echo "=== Core Services Install ==="

deployed=0
for unit in pktfwd.service gateway-rs.service gateway-ui.service; do
    src="${UNIT_SRC_DIR}/${unit}"
    if [ -f "$src" ]; then
        dst="${UNIT_DST_DIR}/${unit}"
        cp "$src" "$dst"
        echo "[OK] Installed ${unit}"
        deployed=$((deployed + 1))
    else
        echo "[WARN] ${unit} not found in ${UNIT_SRC_DIR} — skipping"
    fi
done

OVERRIDE_SRC="${UNIT_SRC_DIR}/readsb-override.conf"
OVERRIDE_DIR="${UNIT_DST_DIR}/readsb.service.d"
OVERRIDE_DST="${OVERRIDE_DIR}/override.conf"
if [ -f "$OVERRIDE_SRC" ]; then
    mkdir -p "$OVERRIDE_DIR"
    cp "$OVERRIDE_SRC" "$OVERRIDE_DST"
    echo "[OK] Installed readsb.service drop-in override"
    deployed=$((deployed + 1))
else
    echo "[WARN] readsb-override.conf not found in ${UNIT_SRC_DIR} — skipping"
fi

if [ "$deployed" -eq 0 ]; then
    echo "[WARN] No core unit files found in ${UNIT_SRC_DIR} — skipping"
    exit 0
fi

systemctl daemon-reload
echo "[OK] systemd daemon-reload complete"
echo "=== Core unit files deployed (${deployed} total) ==="
