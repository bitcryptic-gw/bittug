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
cat > /etc/sudoers.d/10-gateway-ui << 'SUDOERS'
gateway-ui ALL=(root) NOPASSWD: /bin/systemctl restart gateway-ui
gateway-ui ALL=(root) NOPASSWD: /bin/systemctl restart pktfwd
gateway-ui ALL=(root) NOPASSWD: /bin/systemctl restart gateway-rs
gateway-ui ALL=(root) NOPASSWD: /opt/gateway/scripts/apply-band.sh
gateway-ui ALL=(root) NOPASSWD: /opt/gateway/scripts/apply-timezone.sh
gateway-ui ALL=(root) NOPASSWD: /opt/gateway/scripts/apply-hostname.sh
SUDOERS
chmod 0440 /etc/sudoers.d/10-gateway-ui
if visudo -c -f /etc/sudoers.d/10-gateway-ui; then
    log "Sudoers entries installed and validated"
else
    log "ERROR: sudoers validation failed — removing /etc/sudoers.d/10-gateway-ui"
    rm -f /etc/sudoers.d/10-gateway-ui
    exit 1
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

log "sync-provisioning complete"
