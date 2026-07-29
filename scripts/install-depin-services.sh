#!/bin/bash
# install-depin-services.sh — deploy DePIN systemd unit files to /etc/systemd/system/.
# Safe to re-run. Does NOT enable or start services (user action via gateway-ui).
# Run as root.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run as root (sudo)." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT_SRC_DIR="${SCRIPT_DIR}/../systemd"
UNIT_DST_DIR="/etc/systemd/system"

echo "=== DePIN Services Install ==="

copied=0
for unit in "${UNIT_SRC_DIR}"/depin-*.service; do
    [ -f "$unit" ] || continue
    name="$(basename "$unit")"
    dst="${UNIT_DST_DIR}/${name}"
    cp "$unit" "$dst"
    echo "[OK] Installed ${name}"
    copied=$((copied + 1))
done

if [ "$copied" -eq 0 ]; then
    echo "[WARN] No depin-*.service unit files found in ${UNIT_SRC_DIR} — skipping"
    exit 0
fi

systemctl daemon-reload
echo "[OK] systemd daemon-reload complete"
echo "=== DePIN unit files deployed (${copied} total) ==="
