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
# can miss on the first interactive logins. Where the cause can be determined
# read-only, it reports it specifically:
#   * time-sync timeout  — systemd-time-wait-sync.service is in the failed state
#     (bounded by the TimeoutStartSec=90s drop-in from build-image.sh step 5c-i)
#   * Docker install     — /opt/gateway/.docker-pending exists (first-boot.sh
#     withheld the sentinel because the get.docker.com install failed)
#
# Pure informational: must never fail, never set -e, and never touch files
# outside /etc/gateway-provisioned & /opt/gateway/.docker-pending.
#
# Strictly read-only. No locking, no writes, ignore every error.

if [ ! -e /etc/gateway-provisioned ]; then
    TIME_SYNC_FAILED=false
    if [ "$(systemctl is-failed systemd-time-wait-sync.service 2>/dev/null)" = "failed" ]; then
        TIME_SYNC_FAILED=true
    fi

    DOCKER_PENDING=false
    if [ -e /opt/gateway/.docker-pending ]; then
        DOCKER_PENDING=true
    fi

    echo ""
    echo "======================================================================"
    echo "  WARNING: First-boot provisioning has NOT completed."
    echo ""

    if [ "$TIME_SYNC_FAILED" = "true" ]; then
        echo "  Cause: the system clock did not synchronize in time."
        echo "  systemd-time-wait-sync.service timed out waiting for NTP, so the"
        echo "  TLS-dependent provisioning steps were held up. This is almost"
        echo "  always a network problem at boot (no internet, no DNS, or no"
        echo "  route for NTP). A fresh device has no RTC, so it must sync over"
        echo "  the network before git clone / Docker install can succeed."
        echo "    Check:  systemctl status systemd-time-wait-sync.service"
        echo "            timedatectl"
        echo ""
    fi

    if [ "$DOCKER_PENDING" = "true" ]; then
        echo "  Cause: Docker could not be installed."
        echo "  The get.docker.com installer failed after retries during first"
        echo "  boot, so the DePIN container modules cannot run. Usually caused"
        echo "  by network/DNS not being ready when the install was attempted."
        echo "    Check:  tail -n 60 /var/log/gateway-first-boot.log"
        echo "    Fix:    sudo curl -fsSL https://get.docker.com | sh"
        echo "            sudo systemctl restart gateway-platform.service"
        echo ""
    fi

    if [ "$TIME_SYNC_FAILED" != "true" ] && [ "$DOCKER_PENDING" != "true" ]; then
        echo "  This device was flashed but gateway provisioning failed or is"
        echo "  still in progress. Services that depend on it (web UI, token,"
        echo "  LoRa forwarder) may be incomplete or not installed."
        echo ""
    fi

    echo "  Logs:"
    echo "    sudo cat /var/log/gateway-first-boot.log"
    echo "    sudo cat /var/log/firstrun.log"
    echo ""
    echo "  Failed units:        systemctl --failed"
    echo "  Re-run provisioning: sudo systemctl restart gateway-platform.service"
    echo "  (also try, if the device never finished first-run:"
    echo "     sudo systemctl restart gateway-firstrun.service )"
    echo ""
    echo "    1. A README that was followed correctly should NOT hit this."
    echo "       If it did, confirm you started from a clean flash."
    echo "======================================================================"
    echo ""
fi
