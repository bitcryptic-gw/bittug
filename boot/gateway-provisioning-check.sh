#!/bin/bash
# gateway-provisioning-check.sh — provisioned-status notice for interactive logins.
#
# Installed by bootstrap.sh as /etc/profile.d/99-gateway-provisioning.sh so that
# every interactive login prints a visible banner if first-boot provisioning did
# not complete. A failed provisioning run leaves the sentinel file unwritten and
# the (surviving) systemd units in a 'failed' state that only a user who
# explicitly knows to check would notice — this turns that condition into a
# message nobody can miss.
#
# Pure informational: must never fail, never set -e, and never touch files
# outside /etc/gateway-provisioned & /etc/gateway-bootstrap-complete. Guarded so
# it is a complete no-op the moment provisioning is confirmed complete.
#
# Strictly read-only. No locking, no writes, ignore every error.

[ -e /etc/gateway-provisioned ] && exit 0

echo ""
echo "======================================================================"
echo "  WARNING: First-boot provisioning has NOT completed."
echo ""
echo "  This device was flashed but gateway provisioning failed part-way."
echo "  Services that depend on it (web UI, token, LoRa forwarder) are"
echo "  incomplete or not installed. Proceed as follows:"
echo ""
echo "    1. Inspect the log:   sudo cat /var/log/firstrun.log"
echo "    2. Check for failed units:  systemctl --failed"
echo "    3. Once the cause is fixed, re-run the first-boot provisioning:"
echo "           sudo systemctl restart gateway-firstrun.service"
echo ""
echo "    4. A README that was followed correctly should NOT hit this."
echo "       If it did, confirm you started from a clean flash."
echo "======================================================================"
echo ""
