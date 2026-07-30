#!/bin/bash
# sync-provisioning.sh — idempotent provisioning-sync steps.
# Safe to re-run at any time: first-boot, OTA updates, manual recovery.
# Must run as root.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run as root (sudo)." >&2
    exit 1
fi

log() {
    echo "  [sync] $*"
}

# --- gateway-ui user creation (idempotent) ---
if ! id -u gateway-ui &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin \
        --groups systemd-journal,i2c gateway-ui
    log "Created system user: gateway-ui (groups: systemd-journal, i2c)"
else
    log "User gateway-ui already exists — skipping"
fi

# --- Tailscale operator ---
if command -v tailscale &>/dev/null; then
    if tailscale set --operator=gateway-ui 2>/dev/null; then
        log "Tailscale operator set to gateway-ui"
    else
        log "WARNING: could not set Tailscale operator (Tailscale not authenticated yet?)"
    fi
fi

# --- File ownership fixes ---
for f in \
    /var/log/gateway-ota.log \
    /etc/gateway-ui/ntfy.json; do
    if [ -f "$f" ]; then
        chown gateway-ui:gateway-ui "$f" && \
            log "Fixed ownership of ${f} to gateway-ui:gateway-ui" || \
            log "WARNING: Failed to chown ${f}"
    fi
done

# github-token is a read-only secret — root owns it, service reads via group
if [ -f /etc/gateway-ui/github-token ]; then
    cur_owner=$(stat -c '%U:%G' /etc/gateway-ui/github-token 2>/dev/null || true)
    if [ "$cur_owner" != "root:gateway-ui" ]; then
        chown root:gateway-ui /etc/gateway-ui/github-token && \
            chmod 640 /etc/gateway-ui/github-token && \
            log "Corrected github-token ownership to root:gateway-ui (was ${cur_owner})" || \
            log "WARNING: Failed to chown /etc/gateway-ui/github-token"
    fi
fi

# --- sudoers deployment ---
# Writes to a temp file first, validates with visudo, and only replaces
# the live file if validation passes. On failure, preserves the previous
# file and warns non-fatally — the rest of provisioning continues.
SUDOERS_TMP=$(mktemp /tmp/10-gateway-ui.XXXXXX)
cat > "$SUDOERS_TMP" << 'SUDOERS'
gateway-ui ALL=(root) NOPASSWD: /bin/systemctl restart gateway-ui
gateway-ui ALL=(root) NOPASSWD: /bin/systemctl restart pktfwd
gateway-ui ALL=(root) NOPASSWD: /bin/systemctl restart gateway-rs
gateway-ui ALL=(root) NOPASSWD: /opt/gateway/scripts/apply-band.sh
gateway-ui ALL=(root) NOPASSWD: /opt/gateway/scripts/apply-timezone.sh
gateway-ui ALL=(root) NOPASSWD: /opt/gateway/scripts/apply-hostname.sh
gateway-ui ALL=(root) NOPASSWD: /opt/gateway/scripts/depin-uninstall.sh
gateway-ui ALL=(root) NOPASSWD: /bin/systemctl enable depin-*.service
gateway-ui ALL=(root) NOPASSWD: /bin/systemctl disable depin-*.service
gateway-ui ALL=(root) NOPASSWD: /bin/systemctl start depin-*.service
gateway-ui ALL=(root) NOPASSWD: /bin/systemctl stop depin-*.service
gateway-ui ALL=(root) NOPASSWD: /bin/systemctl restart depin-*.service
gateway-ui ALL=(root) NOPASSWD: /usr/bin/docker pull honeygain/honeygain\:latest
gateway-ui ALL=(root) NOPASSWD: /usr/bin/docker pull bringyour/community-provider\:g4-latest
gateway-ui ALL=(root) NOPASSWD: /usr/bin/docker pull mysteriumnetwork/myst\:latest
gateway-ui ALL=(root) NOPASSWD: /usr/bin/docker pull ghcr.io/anyone-protocol/ator-protocol\:latest
SUDOERS
chmod 0440 "$SUDOERS_TMP"
if visudo -c -f "$SUDOERS_TMP"; then
    mv "$SUDOERS_TMP" /etc/sudoers.d/10-gateway-ui
    log "Sudoers entries installed and validated"
else
    log "WARNING: sudoers validation failed — preserving previous file untouched"
    rm -f "$SUDOERS_TMP"
fi

# --- gateway-rs settings.toml ---
# Sync updated config to /etc/helium_gateway/ on already-provisioned devices.
# Idempotent: only copies when content differs (hash compare, not mtime).
# Restarts gateway-rs.service only if the file actually changed.
SETTINGS_SRC="/opt/gateway/config/settings.toml"
SETTINGS_DST="/etc/helium_gateway/settings.toml"
if [ -f "$SETTINGS_SRC" ]; then
    src_hash=$(sha256sum "$SETTINGS_SRC" | awk '{print $1}')
    dst_hash=""
    if [ -f "$SETTINGS_DST" ]; then
        dst_hash=$(sha256sum "$SETTINGS_DST" | awk '{print $1}')
    fi
    if [ "$src_hash" = "$dst_hash" ]; then
        log "settings.toml unchanged — skipping"
    else
        log "Updating settings.toml (content changed)"
        mkdir -p /etc/helium_gateway
        cp "$SETTINGS_SRC" "$SETTINGS_DST"
        chmod 644 "$SETTINGS_DST"
        chown root:root "$SETTINGS_DST"
        if /usr/bin/systemctl list-unit-files gateway-rs.service --no-legend | grep -q .; then
            log "Restarting gateway-rs.service to pick up updated settings.toml"
            /usr/bin/systemctl restart gateway-rs.service
        else
            log "WARNING: gateway-rs.service not installed — settings.toml synced, restart skipped"
        fi
    fi
else
    log "WARNING: settings.toml not found at ${SETTINGS_SRC} — skipping"
fi

# --- gateway-ui .git write-access durability check (2026-07-29) ---
# Ensures gateway-ui retains write access to /opt/gateway/.git even if
# something (git gc, re-clone, etc.) ever resets group ownership or the
# setgid bit. Two independent conditions, both required.
PRIMARY_USER=$(getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 {print $1; exit}')
REPO_GIT_DIR="/opt/gateway/.git"
if [ -d "$REPO_GIT_DIR" ]; then
    if id -nG gateway-ui | grep -qw "$PRIMARY_USER"; then
        log "gateway-ui already in ${PRIMARY_USER} group — skipping"
    else
        log "Adding gateway-ui to ${PRIMARY_USER} group"
        usermod -aG "$PRIMARY_USER" gateway-ui
    fi

    if stat -c '%A' "$REPO_GIT_DIR" | grep -q '^drwxrws'; then
        log "${REPO_GIT_DIR} setgid bit already set — skipping"
    else
        log "Setting setgid bit on ${REPO_GIT_DIR}"
        chmod -R g+rwX,g+s "$REPO_GIT_DIR"
    fi
else
    log "WARNING: ${REPO_GIT_DIR} not found — skipping .git write-access check"
fi

# --- DePIN directories and credential durability ---
# Creates the directory tree so EnvironmentFile= references resolve cleanly
# at unit start time (fail-fast, not fail-later). Re-fixes ownership on
# every run — durable, not one-shot.
DEPIN_DIR="/etc/gateway-ui/depin"
if [ -d "$DEPIN_DIR" ]; then
    cur_mode=$(stat -c '%a' "$DEPIN_DIR" 2>/dev/null || true)
    cur_owner=$(stat -c '%U:%G' "$DEPIN_DIR" 2>/dev/null || true)
    if [ "$cur_owner" != "root:gateway-ui" ] || [ "$cur_mode" != "750" ]; then
        chown root:gateway-ui "$DEPIN_DIR" && \
            chmod 750 "$DEPIN_DIR" && \
            log "Corrected ${DEPIN_DIR} ownership to root:gateway-ui (was ${cur_owner}:${cur_mode})" || \
            log "WARNING: Failed to chown ${DEPIN_DIR}"
    fi
else
    mkdir -p "$DEPIN_DIR"
    chown root:gateway-ui "$DEPIN_DIR"
    chmod 750 "$DEPIN_DIR"
    log "Created ${DEPIN_DIR} (root:gateway-ui 750)"
fi

# Honeygain env file durability — fix ownership if file exists
if [ -f "${DEPIN_DIR}/honeygain.env" ]; then
    cur_owner=$(stat -c '%U:%G' "${DEPIN_DIR}/honeygain.env" 2>/dev/null || true)
    if [ "$cur_owner" != "root:gateway-ui" ]; then
        chown root:gateway-ui "${DEPIN_DIR}/honeygain.env" && \
            chmod 640 "${DEPIN_DIR}/honeygain.env" && \
            log "Corrected honeygain.env ownership to root:gateway-ui (was ${cur_owner})" || \
            log "WARNING: Failed to chown ${DEPIN_DIR}/honeygain.env"
    fi
fi

# URnetwork data directory
# Container runs as root (no USER in image config — verified via registry
# inspection of bringyour/community-provider:g4-latest arm64 manifest).
URNET_DIR="/var/lib/gateway-ui/urnetwork"
if [ -d "$URNET_DIR" ]; then
    cur_owner=$(stat -c '%U:%G' "$URNET_DIR" 2>/dev/null || true)
    if [ "$cur_owner" != "root:root" ]; then
        chown root:root "$URNET_DIR" && \
            chmod 755 "$URNET_DIR" && \
            log "Corrected ${URNET_DIR} ownership to root:root (was ${cur_owner})" || \
            log "WARNING: Failed to chown ${URNET_DIR}"
    fi
else
    mkdir -p "$URNET_DIR"
    chown root:root "$URNET_DIR"
    chmod 755 "$URNET_DIR"
    log "Created ${URNET_DIR} (root:root 755)"
fi

# Anyone data directories
# etc:  configs read by root at container start — root:root 755
# var:  data directory, writable by anond (UID 100) after privilege drop
# run:  runtime directory, writable by anond (UID 100) after privilege drop
# UID/GID sourced from ghcr.io/anyone-protocol/ator-protocol:latest image layer
# inspection (adduser --system creates anond with next-available system UID=100,
# GID=101 on Debian Bookworm base).
ANOND_UID=100
ANOND_GID=101

# etc directory (read-only for container)
if [ ! -d /var/lib/gateway-ui/anyone/etc ]; then
    mkdir -p /var/lib/gateway-ui/anyone/etc
    chown root:root /var/lib/gateway-ui/anyone/etc
    chmod 755 /var/lib/gateway-ui/anyone/etc
    log "Created /var/lib/gateway-ui/anyone/etc"
fi

# var directory (writable by anond — durability check)
if [ -d /var/lib/gateway-ui/anyone/var ]; then
    cur_owner=$(stat -c '%U:%G' /var/lib/gateway-ui/anyone/var 2>/dev/null || true)
    cur_mode=$(stat -c '%a' /var/lib/gateway-ui/anyone/var 2>/dev/null || true)
    if [ "$cur_owner" != "${ANOND_UID}:${ANOND_GID}" ] || [ "$cur_mode" != "750" ]; then
        chown ${ANOND_UID}:${ANOND_GID} /var/lib/gateway-ui/anyone/var && \
            chmod 750 /var/lib/gateway-ui/anyone/var && \
            log "Corrected /var/lib/gateway-ui/anyone/var ownership to ${ANOND_UID}:${ANOND_GID} 750 (was ${cur_owner}:${cur_mode})" || \
            log "WARNING: Failed to chown /var/lib/gateway-ui/anyone/var"
    fi
else
    mkdir -p /var/lib/gateway-ui/anyone/var
    chown ${ANOND_UID}:${ANOND_GID} /var/lib/gateway-ui/anyone/var
    chmod 750 /var/lib/gateway-ui/anyone/var
    log "Created /var/lib/gateway-ui/anyone/var (${ANOND_UID}:${ANOND_GID} 750)"
fi

# run directory (writable by anond — durability check)
if [ -d /var/lib/gateway-ui/anyone/run ]; then
    cur_owner=$(stat -c '%U:%G' /var/lib/gateway-ui/anyone/run 2>/dev/null || true)
    cur_mode=$(stat -c '%a' /var/lib/gateway-ui/anyone/run 2>/dev/null || true)
    if [ "$cur_owner" != "${ANOND_UID}:${ANOND_GID}" ] || [ "$cur_mode" != "750" ]; then
        chown ${ANOND_UID}:${ANOND_GID} /var/lib/gateway-ui/anyone/run && \
            chmod 750 /var/lib/gateway-ui/anyone/run && \
            log "Corrected /var/lib/gateway-ui/anyone/run ownership to ${ANOND_UID}:${ANOND_GID} 750 (was ${cur_owner}:${cur_mode})" || \
            log "WARNING: Failed to chown /var/lib/gateway-ui/anyone/run"
    fi
else
    mkdir -p /var/lib/gateway-ui/anyone/run
    chown ${ANOND_UID}:${ANOND_GID} /var/lib/gateway-ui/anyone/run
    chmod 750 /var/lib/gateway-ui/anyone/run
    log "Created /var/lib/gateway-ui/anyone/run (${ANOND_UID}:${ANOND_GID} 750)"
fi

# Anyone anonrc durability — fix ownership if file exists
if [ -f /var/lib/gateway-ui/anyone/etc/anonrc ]; then
    cur_owner=$(stat -c '%U:%G' /var/lib/gateway-ui/anyone/etc/anonrc 2>/dev/null || true)
    if [ "$cur_owner" != "root:root" ]; then
        chown root:root /var/lib/gateway-ui/anyone/etc/anonrc && \
            chmod 644 /var/lib/gateway-ui/anyone/etc/anonrc && \
            log "Corrected anonrc ownership to root:root (was ${cur_owner})" || \
            log "WARNING: Failed to chown /var/lib/gateway-ui/anyone/etc/anonrc"
    fi
fi

log "sync-provisioning complete"
