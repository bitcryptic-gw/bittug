#!/bin/bash
# gateway-provisioning-check.sh — provisioned-status notice for interactive logins.
#
# Installed by bootstrap.sh as /etc/profile.d/99-gateway-provisioning.sh and
# SOURCED into every interactive login shell by /etc/profile (or bash's own
# profile-sourcing loop) — NOT executed as a subprocess. Scripts under
# /etc/profile.d/ run in the caller's shell, so `exit` would terminate the
# entire login session before the user ever gets a prompt. This file therefore
# contains NO `exit`/`return`; the whole banner is wrapped in a negated
# condition so a provisioned device (sentinel exists) just falls through with no
# output and control returns to the shell normally.
#
# Purpose: a failed provisioning run leaves the sentinel file unwritten and the
# (surviving) systemd units in a 'failed' state that only a user who explicitly
# knows to check would notice — this turns that condition into a message nobody
# can miss on the first interactive logins.
#
# Pure informational: must never fail, never set -e, and never touch files
# outside /etc/gateway-provisioned & /etc/gateway-bootstrap-complete.
#
# Strictly read-only. No locking, no writes, ignore every error.

if [ ! -e /etc/gateway-provisioned ]; then
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
fi
