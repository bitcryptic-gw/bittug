#!/bin/bash
# firstrun.sh — SenseCap M1 Gateway first-boot provisioning
#
# HOW THIS WORKS (for transparency):
#
# This script is invoked by gateway-firstrun.service, a oneshot systemd
# unit that is installed and enabled at image-build time. The unit is
# ordered after network-online.target (Requires= + After=), so it only
# fires once NetworkManager has completed DHCP on the wired interface.
#
# One-shot guard: the unit has ConditionPathExists=!/etc/gateway-provisioned.
# This script touches that sentinel file as its last provisioning action,
# so the unit is skipped on every subsequent boot. If provisioning fails
# (exit non-zero), the sentinel is never written and the unit retries on
# the next boot.
#
# After this script completes, it reboots the device so all newly-enabled
# services start cleanly.
#
# This script does minimal work itself — its sole job is to clone the
# gateway repo and invoke boot/bootstrap.sh, which handles full provisioning.
# See boot/bootstrap.sh for details of what provisioning does.
#
# Raspberry Pi Imager's Customisation step can optionally be used to set the
# operator's username, password, SSH key, and hostname before flashing — but
# this is not available in all Imager flows (confirmed unavailable in Imager
# v2.0.10 when flashing via "Use custom" with a custom .img.xz). If no such
# user exists at boot, this script creates a fallback account
# (sensecap/sensecap, forced password change on first login) before falling
# through to the primary-user derivation logic. See the "Fallback default
# account" block below.
set -euo pipefail

LOG="/var/log/firstrun.log"
REPO_URL="https://github.com/bitcryptic-gw/sensecap-m1-gateway"
REPO_DIR="/opt/gateway"

# Tee all output to log file and console
exec > >(tee -a "$LOG") 2>&1

# Find the first UID-1000+ account with a real login shell (i.e. one
# listed in /etc/shells).  Service accounts with nologin/false shells
# are deliberately excluded — they are not interactive users.
first_interactive_user() {
    getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 {print $1":"$7}' | while IFS=: read -r user shell; do
        if grep -qxF "$shell" /etc/shells; then
            echo "$user"
            break
        fi
    done
}

echo "=== SenseCap M1 Gateway First-Run ==="
echo "Started: $(date)"

# --- Install git if needed ---
echo "[firstrun] $(date '+%H:%M:%S') Starting: git install check"
if ! command -v git &>/dev/null; then
    echo "[firstrun] Installing git..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq git
    echo "[firstrun] git installed"
fi
echo "[firstrun] $(date '+%H:%M:%S') Completed: git install check"

# --- Fallback default account ---
# If Raspberry Pi Imager did not provision a user — this happens when
# flashing via "Use custom" in some Imager versions — create a fallback
# account 'sensecap' with a static published password and force a change
# on first login. Idempotent: does nothing if sensecap already exists or
# any UID 1000+ user is already present.
echo "[firstrun] $(date '+%H:%M:%S') Starting: fallback account check"
if ! id sensecap &>/dev/null; then
    EXISTING_USER=$(first_interactive_user)
    if [ -z "$EXISTING_USER" ]; then
        echo "[firstrun] No Imager-provisioned user found — creating fallback account 'sensecap'"

        groupadd -f sudo

        useradd --create-home --shell /bin/bash --groups sudo sensecap

        echo "sensecap:sensecap" | chpasswd

        chage -d 0 sensecap

        echo "[firstrun] Fallback account 'sensecap' created. Default password is published in README.md. Password change will be required on first login."
    fi
fi
echo "[firstrun] $(date '+%H:%M:%S') Completed: fallback account check"

# --- Enable SSH ---
# Runs unconditionally on every first boot regardless of which
# user-creation path was taken (Imager-provisioned user, sensecap
# fallback, or neither). The marker file ensures sshswitch.service
# also enables SSH on subsequent boots.
echo "[firstrun] $(date '+%H:%M:%S') Starting: enable SSH"
systemctl enable --now ssh || echo "[firstrun] WARNING: Failed to enable SSH" >&2
touch /boot/firmware/ssh
echo "[firstrun] $(date '+%H:%M:%S') Completed: enable SSH"

# --- Derive primary user ---
echo "[firstrun] $(date '+%H:%M:%S') Starting: primary user derivation"
PRIMARY_USER=$(first_interactive_user)
if [ -z "$PRIMARY_USER" ]; then
    echo "[firstrun] ERROR: No primary non-root user found (UID 1000–65533)."
    echo "[firstrun] Did you forget to set a username in Raspberry Pi Imager?"
    exit 1
fi
echo "[firstrun] Primary user: ${PRIMARY_USER}"
echo "[firstrun] $(date '+%H:%M:%S') Completed: primary user derivation"

# --- Clone the repo ---
echo "[firstrun] $(date '+%H:%M:%S') Starting: repo clone"
if [ -d "$REPO_DIR" ]; then
    if git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        echo "[firstrun] ${REPO_DIR} already contains a valid git repo — skipping clone"
    else
        echo "[firstrun] ${REPO_DIR} exists but is not a valid git repo (likely an interrupted previous run) — removing and re-cloning"
        rm -rf "$REPO_DIR"
        echo "[firstrun] ${REPO_DIR} removed"
    fi
fi
if [ ! -d "$REPO_DIR" ]; then
    echo "[firstrun] Cloning gateway repo..."
    mkdir -p "$REPO_DIR"
    chown "${PRIMARY_USER}:${PRIMARY_USER}" "$REPO_DIR"
    # The clone runs over HTTPS and can transiently fail against a stale boot
    # clock (TLS cert "not before" check) or a still-settling network, even
    # with time-sync/network-online gating. Retry briefly with backoff instead
    # of failing the whole provisioning run on the first attempt.
    CLONE_OK=false
    for attempt in 1 2 3 4 5; do
        if sudo -u "$PRIMARY_USER" git clone "$REPO_URL" "$REPO_DIR"; then
            CLONE_OK=true
            break
        fi
        echo "[firstrun] git clone attempt ${attempt}/5 failed — waiting and retrying..." >&2
        sleep 10
    done
    if [ "$CLONE_OK" != true ]; then
        echo "[firstrun] ERROR: git clone failed after 5 attempts." >&2
        echo "[firstrun] Check network connectivity and that the clock is correct (date)." >&2
        echo "[firstrun] NOTE: if this is a clock issue, a successful NTP sync + reboot usually fixes it." >&2
        exit 1
    fi
    echo "[firstrun] Repo cloned to ${REPO_DIR}"
fi
echo "[firstrun] $(date '+%H:%M:%S') Completed: repo clone"

# Grant gateway-ui write access to .git for OTA (2026-07-29)
if id gateway-ui &>/dev/null; then
    usermod -aG "$PRIMARY_USER" gateway-ui
fi
chmod -R g+rwX,g+s "${REPO_DIR}/.git"
echo "[firstrun] $(date '+%H:%M:%S') Completed: gateway-ui .git write access"

# --- Run bootstrap.sh ---
echo "[firstrun] $(date '+%H:%M:%S') Starting: bootstrap.sh"
echo "[firstrun] Running bootstrap.sh..."
bash "${REPO_DIR}/boot/bootstrap.sh"
echo "[firstrun] $(date '+%H:%M:%S') Completed: bootstrap.sh"

# --- Write provisioning sentinel ---
# gateway-firstrun.service gates on this file — it must only be
# touched after ALL provisioning steps have completed successfully.
# set -euo pipefail (line 28) ensures a failure in bootstrap.sh or
# anywhere above exits before reaching this point.
echo "[firstrun] $(date '+%H:%M:%S') Starting: write sentinel"
echo "[firstrun] Provisioning complete — writing sentinel"
touch /etc/gateway-provisioned
echo "[firstrun] $(date '+%H:%M:%S') Completed: write sentinel"

# --- Remove any legacy systemd.run from cmdline.txt ---
# gateway-firstrun.service is now the trigger mechanism, but
# older image builds injected systemd.run= into cmdline.txt.
# Clean it up idempotently so that an OTA upgrade on a device
# that never completed first-boot doesn't run this script twice.
CMDLINE="/boot/firmware/cmdline.txt"
if [ -f "$CMDLINE" ] && grep -q 'systemd\.run=' "$CMDLINE" 2>/dev/null; then
    echo "[firstrun] Removing legacy systemd.run from ${CMDLINE}..."
    sed -i 's/\s*systemd\.run=[^ ]*//g' "$CMDLINE"
    echo "[firstrun] cmdline.txt cleaned"
fi

# --- Enable cgroup memory controller for DePIN container limits ---
# Raspberry Pi firmware injects cgroup_disable=memory by default, which
# silently drops --memory= limits on Docker containers. Re-enable it so
# the DePIN unit files' --memory=<X> values are actually enforced.
# Takes effect on reboot — first-boot already reboots at the end of this
# script, so no extra reboot is needed for fresh devices.
if [ -f "$CMDLINE" ]; then
    if grep -qE '(^|[[:space:]])cgroup_memory=1([[:space:]]|$)' "$CMDLINE" 2>/dev/null; then
        echo "[firstrun] cgroup_memory=1 already present in cmdline.txt"
    else
        echo "[firstrun] Enabling cgroup memory controller (cgroup_memory=1)"
        sed -i 's/[[:space:]]*$/ cgroup_memory=1/' "$CMDLINE"
        echo "[firstrun] cmdline.txt: $(cat "$CMDLINE")"
    fi
fi

echo "[firstrun] First-run complete. Rebooting in 5s..."
echo "=== First-Run Complete: $(date) ==="
sleep 5
reboot
