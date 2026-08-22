#!/bin/bash
# bootstrap.sh — One-time provisioning for a fresh Debian Trixie (ARM64) Pi.
# Usage: sudo ./boot/bootstrap.sh [--force]
set -euo pipefail

# ── Constants ──────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/bitcryptic-gw/sensecap-m1-gateway"
REPO_DIR="/opt/gateway"
CONFIG_TXT_SRC="${REPO_DIR}/boot/config.txt"
CONFIG_TXT_DST="/boot/firmware/config.txt"
ENV_FILE="${REPO_DIR}/config.env"
ENV_EXAMPLE="${REPO_DIR}/config.env.example"
SENTINEL="/etc/gateway-bootstrap-complete"

# ── Colour helpers ─────────────────────────────────────────────────────────────
green() { echo "  [OK] $*"; }
warn()  { echo "  [WARN] $*" >&2; }
info()  { echo "  [..] $*"; }

# ── Preflight ──────────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run as root (sudo)." >&2
    exit 1
fi

PRIMARY_USER=$(getent passwd | awk -F: '$3 >= 1000 && $3 < 65534 {print $1; exit}')
if [ -z "$PRIMARY_USER" ]; then
    echo "ERROR: No primary non-root user found (UID 1000–65533)." >&2
    exit 1
fi

echo "============================================"
echo "  SenseCap M1 Gateway Bootstrap"
echo "  Hostname:  $(hostname)"
echo "  User:      ${PRIMARY_USER}"
echo "  Date:      $(date)"
echo "============================================"
echo ""

FORCE=false
if [ "${1:-}" = "--force" ]; then
    FORCE=true
    info "Running in --force mode (re-run steps only)"
fi

# --- Already provisioned? ---
if [ -f "$SENTINEL" ] && [ "$FORCE" = false ]; then
    echo ""
    echo "This device appears to already be provisioned (${SENTINEL} exists)."
    echo "Re-run with --force to overwrite. This will not delete existing config or secrets."
    echo ""
    exit 0
fi

# ── 1. System packages ────────────────────────────────────────────────────────
#
# Docker is NOT installed here. Docker provisioning is handled exclusively by
# first-boot.sh via get.docker.com (docker-ce). Do not add docker.io, docker-ce,
# or any Docker package to this script — docker.io conflicts with docker-ce and
# re-running bootstrap.sh after first-boot.sh would silently remove the CLI.
# See fix: 2026-07-30 standardize on docker-ce, remove docker.io conflict.

echo "[firstrun] $(date '+%H:%M:%S') Starting: system packages"
echo "--- System Packages ---"
info "Updating package lists..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

info "Installing required packages..."
apt-get install -y -qq --no-install-recommends \
    git \
    python3 python3-pip python3-venv \
    i2c-tools \
    jq \
    curl \
    locales-all

green "System packages installed"
echo "[firstrun] $(date '+%H:%M:%S') Completed: system packages"

# ── 2. gateway-ui system user/group ───────────────────────────────────────────
#
# Every service that ships in this image (gateway-ui.service,
# gateway-platform.service via first-boot.sh, plus bootstrap.sh's own
# /var/lib/gateway-ui, /etc/gateway-ui, and OTA logic) assumes the gateway-ui
# system account exists. On a genuinely fresh device it does not — and if it is
# never created here, set -e makes every later `-g gateway-ui` / `chown
# gateway-ui` / `User=gateway-ui` hard-fail. That is exactly the fresh-boot
# provisioning failure this block fixes: previously the account was only ever
# created by sync-provisioning.sh, which runs via gateway-platform.service —
# which is itself installed later in this script and so never got the chance on
# a fresh flash.
#
# Runs AFTER system packages so the `i2c` group (created by i2c-tools) is
# guaranteed present before we grant it as a supplementary group. Idempotent
# and non-fatal-safe: if the user/group already exist we skip creation
# (mirroring the pre-existing `id gateway-ui && getent group` guards used
# elsewhere in this script). A second run, OTA re-run, or --force re-run
# must not clobber an existing account.

echo ""
echo "[firstrun] $(date '+%H:%M:%S') Starting: gateway-ui account"
echo "--- gateway-ui Account ---"

if ! getent group gateway-ui >/dev/null 2>&1; then
    if groupadd --system gateway-ui; then
        green "Created system group: gateway-ui"
    else
        warn "Failed to create group gateway-ui — continuing (further steps guarded)"
    fi
fi

# Supplementary groups, including only those that actually exist. systemd-journal
# always exists; i2c exists once i2c-tools is installed (it is, above). Filtering
# prevents a single missing group from failing the whole useradd.
SUPP_GROUPS=""
for g in systemd-journal i2c; do
    if getent group "$g" >/dev/null 2>&1; then
        SUPP_GROUPS="${SUPP_GROUPS}${SUPP_GROUPS:+,}${g}"
    else
        warn "Supplementary group '${g}' not present — omitting from gateway-ui"
    fi
done

if ! id -u gateway-ui >/dev/null 2>&1; then
    # System account: no login shell, no home (bootstrap.sh creates
    # /var/lib/gateway-ui later in this script).
    ARGS=(--system --no-create-home --shell /usr/sbin/nologin)
    if [ -n "$SUPP_GROUPS" ]; then
        ARGS+=(--groups "$SUPP_GROUPS")
    fi
    if useradd "${ARGS[@]}" gateway-ui; then
        green "Created system user: gateway-ui (groups: ${SUPP_GROUPS:-none})"
    else
        warn "Failed to create user gateway-ui — continuing (further steps guarded)"
    fi
fi

if ! id gateway-ui >/dev/null 2>&1 && ! getent group gateway-ui >/dev/null 2>&1; then
    echo "ERROR: gateway-ui account could not be created — refusing to continue." >&2
    echo "       The gateway-ui web service and most provisioning steps depend on it." >&2
    exit 1
fi
echo "[firstrun] $(date '+%H:%M:%S') Completed: gateway-ui account"

# ── 3. Repo clone ────────────────────────────────────────────────────────────

echo ""
echo "[firstrun] $(date '+%H:%M:%S') Starting: repo clone"
echo "--- Repo Clone ---"
if [ -d "${REPO_DIR}/.git" ]; then
    info "Repo already cloned at ${REPO_DIR}"
    # Ensure correct ownership in case of previous root-owned clone
    chown -R "${PRIMARY_USER}:${PRIMARY_USER}" "$REPO_DIR"
    green "Ownership verified: ${PRIMARY_USER}"
else
    info "Creating ${REPO_DIR}..."
    mkdir -p "$REPO_DIR"
    chown "${PRIMARY_USER}:${PRIMARY_USER}" "$REPO_DIR"
    info "Cloning repo as ${PRIMARY_USER}..."
    sudo -u "$PRIMARY_USER" git clone "$REPO_URL" "$REPO_DIR"
    green "Repo cloned at ${REPO_DIR}"
fi

# Grant gateway-ui write access to .git for OTA (2026-07-29)
if id gateway-ui &>/dev/null; then
    usermod -aG "$PRIMARY_USER" gateway-ui
fi
chmod -R g+rwX,g+s "${REPO_DIR}/.git"
green ".git write access granted for gateway-ui (OTA)"

# Mark repo as safe for all users (avoids dubious-ownership errors)
git config --system --add safe.directory /opt/gateway
green "Git safe.directory set for /opt/gateway"
echo "[firstrun] $(date '+%H:%M:%S') Completed: repo clone"

# Create /var/lib/gateway-ui/ with group-write for the gateway-ui service.
# setgid (2775) ensures files created by either root-context scripts or the
# gateway-ui Python process inherit the gateway-ui group.
# Non-recursive — anyone/ and urnetwork/ subdirectories get their own
# ownership from sync-provisioning.sh (anyone/) or Docker bind-mounts
# (urnetwork/) and must remain root:root.
if ! getent group gateway-ui >/dev/null 2>&1; then
    echo "ERROR: group 'gateway-ui' unavailable while creating /var/lib/gateway-ui." >&2
    echo "       This should have been created in the 'gateway-ui account' step above." >&2
    exit 1
fi
install -d -m 2775 -o root -g gateway-ui /var/lib/gateway-ui
green "Created /var/lib/gateway-ui (root:gateway-ui 2775)"

# ── 4. Timezone ──────────────────────────────────────────────────────────────

echo ""
echo "[firstrun] $(date '+%H:%M:%S') Starting: timezone"
echo "--- Timezone ---"
TIMEZONE="Etc/UTC"
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    TIMEZONE="${TIMEZONE:-Etc/UTC}"
fi
# Validate: the value must correspond to a real zoneinfo file.
if [ ! -f "/usr/share/zoneinfo/${TIMEZONE}" ]; then
    warn "Invalid TIMEZONE='${TIMEZONE}' — zoneinfo file not found, falling back to Etc/UTC"
    TIMEZONE="Etc/UTC"
fi
CURRENT_TZ=$(timedatectl show --property=Timezone --value 2>/dev/null || echo "")
if [ "$CURRENT_TZ" = "$TIMEZONE" ]; then
    green "Timezone already set to ${TIMEZONE}"
else
    timedatectl set-timezone "$TIMEZONE"
    green "Timezone set to ${TIMEZONE} (was: ${CURRENT_TZ:-none})"
fi
echo "[firstrun] $(date '+%H:%M:%S') Completed: timezone"

# ── 5. boot/config.txt ───────────────────────────────────────────────────────

echo ""
echo "[firstrun] $(date '+%H:%M:%S') Starting: boot config"
echo "--- Boot Config ---"
if [ "$FORCE" = true ] && [ -d "${REPO_DIR}/.git" ]; then
    info "Skipping boot config in --force mode"
elif [ -f "$CONFIG_TXT_DST" ]; then
    if cmp -s "$CONFIG_TXT_SRC" "$CONFIG_TXT_DST"; then
        green "Boot config already up-to-date"
    else
        BACKUP="${CONFIG_TXT_DST}.bak-$(date +%Y%m%d-%H%M%S)"
        cp "$CONFIG_TXT_DST" "$BACKUP"
        cp "$CONFIG_TXT_SRC" "$CONFIG_TXT_DST"
        green "Boot config updated (backup at ${BACKUP})"
    fi
else
    cp "$CONFIG_TXT_SRC" "$CONFIG_TXT_DST"
    green "Copied boot config to ${CONFIG_TXT_DST}"
fi
echo "[firstrun] $(date '+%H:%M:%S') Completed: boot config"

# ── 6. Systemd units ─────────────────────────────────────────────────────────

echo ""
echo "[firstrun] $(date '+%H:%M:%S') Starting: systemd units"
echo "--- Systemd Units ---"
for unit in "${REPO_DIR}"/systemd/*.service "${REPO_DIR}"/systemd/*.timer; do
    [ -e "$unit" ] || continue
    name=$(basename "$unit")
    if [[ "$name" == depin-* ]]; then
        info "Skipped ${name} (DePIN units are user-enabled via gateway-ui)"
        continue
    fi
    cp "$unit" "/etc/systemd/system/${name}"
    info "Copied ${name}"
    systemctl enable "$name" 2>/dev/null || \
        warn "Failed to enable ${name} (continuing)"
done
systemctl daemon-reload
green "Systemd units installed and enabled"
echo "[firstrun] $(date '+%H:%M:%S') Completed: systemd units"

# ── 7. Tailscale install ─────────────────────────────────────────────────────

echo ""
echo "[firstrun] $(date '+%H:%M:%S') Starting: tailscale"
echo "--- Tailscale ---"
# Root-only secrets directory (holds the persisted Tailscale auth key used
# by tailscale-autoconnect for unattended re-authentication).
install -d -m 0755 -o root -g root /etc/gateway
if [ -x "${REPO_DIR}/scripts/install-tailscale.sh" ]; then
    if "${REPO_DIR}/scripts/install-tailscale.sh"; then
        green "Tailscale installed"
    else
        warn "Tailscale install failed — re-run manually: sudo ${REPO_DIR}/scripts/install-tailscale.sh"
    fi
else
    warn "install-tailscale.sh not found or not executable — skipping"
fi
echo "[firstrun] $(date '+%H:%M:%S') Completed: tailscale"

# ── 8. Wingbits deps ─────────────────────────────────────────────────────────

echo ""
echo "[firstrun] $(date '+%H:%M:%S') Starting: wingbits deps"
echo "--- Wingbits Dependencies ---"
if [ -x "${REPO_DIR}/scripts/install-wingbits-deps.sh" ]; then
    "${REPO_DIR}/scripts/install-wingbits-deps.sh"
    green "Wingbits dependencies installed"
else
    warn "install-wingbits-deps.sh not found or not executable — skipping"
fi
echo "[firstrun] $(date '+%H:%M:%S') Completed: wingbits deps"

# ── 9. Helium gateway binary ───────────────────────────────────────────────

echo ""
echo "[firstrun] $(date '+%H:%M:%S') Starting: helium gateway"
echo "--- Helium Gateway ---"
if [ -x "${REPO_DIR}/scripts/install-helium-gateway.sh" ]; then
    "${REPO_DIR}/scripts/install-helium-gateway.sh"
    green "Helium gateway installed"
else
    warn "install-helium-gateway.sh not found or not executable — skipping"
fi
echo "[firstrun] $(date '+%H:%M:%S') Completed: helium gateway"

# ── 10. Gateway version ──────────────────────────────────────────────────────

echo ""
echo "[firstrun] $(date '+%H:%M:%S') Starting: gateway version"
echo "--- Gateway Version ---"
VERSION_TAG=$(git -C "${REPO_DIR}" describe --tags --always 2>/dev/null || echo "dev")
echo "${VERSION_TAG}" > /etc/gateway-version
chmod 644 /etc/gateway-version
green "Wrote /etc/gateway-version: ${VERSION_TAG}"
echo "[firstrun] $(date '+%H:%M:%S') Completed: gateway version"

# ── 11. Setuid wrappers (single source of truth) ────────────────────────────

echo ""
echo "[firstrun] $(date '+%H:%M:%S') Starting: setuid wrappers"
echo "--- Setuid Wrappers ---"
if command -v gcc &>/dev/null; then
    bash "${REPO_DIR}/scripts/install-wrappers.sh"
    green "All setuid wrappers installed"
else
    warn "gcc not found — setuid wrappers omitted (install build-essential and re-run)"
fi
echo "[firstrun] $(date '+%H:%M:%S') Completed: setuid wrappers"

# ── 12. Gateway UI config files ─────────────────────────────────────────────────────────

echo ""
echo "[firstrun] $(date '+%H:%M:%S') Starting: gateway UI config"
echo "--- OTA Log File ---"
touch /var/log/gateway-ota.log
if id gateway-ui &>/dev/null; then
    chown gateway-ui:gateway-ui /var/log/gateway-ota.log
elif getent group gateway-ui &>/dev/null; then
    chown root:gateway-ui /var/log/gateway-ota.log
else
    chown root:root /var/log/gateway-ota.log
fi
chmod 640 /var/log/gateway-ota.log
green "Created /var/log/gateway-ota.log"

echo ""
echo "--- NTFY Config ---"
NTFY_DIR="/etc/gateway-ui"
if [ ! -f "${NTFY_DIR}/ntfy.json" ]; then
    mkdir -p "${NTFY_DIR}"
    echo '{}' > "${NTFY_DIR}/ntfy.json"
    # Set ownership to root:gateway-ui (mode 640) so gateway-ui can read it
    if getent group gateway-ui &>/dev/null; then
        chown root:gateway-ui "${NTFY_DIR}/ntfy.json"
    else
        chown root:root "${NTFY_DIR}/ntfy.json"
    fi
    chmod 640 "${NTFY_DIR}/ntfy.json"
    green "Created /etc/gateway-ui/ntfy.json"
else
    green "ntfy.json already exists"
fi

echo ""
echo "--- GitHub Token ---"
if [ ! -f /etc/gateway-ui/github-token ]; then
    touch /etc/gateway-ui/github-token
    if getent group gateway-ui &>/dev/null; then
        chown root:gateway-ui /etc/gateway-ui/github-token
    else
        chown root:root /etc/gateway-ui/github-token
    fi
    chmod 640 /etc/gateway-ui/github-token
    green "Created /etc/gateway-ui/github-token (populate with GitHub PAT)"
else
    green "github-token already exists"
fi
echo "[firstrun] $(date '+%H:%M:%S') Completed: gateway UI config"

# ── 13. Python dependencies ───────────────────────────────────────────────────

echo ""
echo "[firstrun] $(date '+%H:%M:%S') Starting: python dependencies"
echo "--- Python Dependencies ---"
REQS="${REPO_DIR}/gateway-ui/requirements.txt"
if [ -f "$REQS" ]; then
    ALL_SATISFIED=true
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%%#*}"
        line="$(echo "$line" | xargs)"
        [ -z "$line" ] && continue
        pkg_name="${line%%==*}"
        pkg_name="${pkg_name%%\[*}"
        [ -z "$pkg_name" ] && continue
        req_ver="${line##*==}"
        inst_ver=$(pip3 show "$pkg_name" 2>/dev/null | awk '/^Version: / {print $2}')
        if [ -z "$inst_ver" ] || [ "$inst_ver" != "$req_ver" ]; then
            ALL_SATISFIED=false
            break
        fi
    done < "$REQS"
    if [ "$ALL_SATISFIED" = true ]; then
        green "Python dependencies already satisfied"
    else
        info "Installing Python dependencies from requirements.txt..."
        pip3 install --quiet --break-system-packages -r "$REQS"
        green "Python dependencies installed"
    fi
else
    warn "requirements.txt not found at ${REQS} — skipping pip install"
fi
echo "[firstrun] $(date '+%H:%M:%S') Completed: python dependencies"

# --- Install provisioning-status login notice ---
# Installs a /etc/profile.d/ script (copied from the repo at provisioning time)
# that prints a prominent banner on every interactive login while first-boot
# provisioning is incomplete (i.e. /etc/gateway-provisioned is missing). This
# surfaces a failed / interrupted run immediately instead of leaving a first-run
# user with a device that just quietly lacks its web UI, token and units.
# The script is a read-only, always-exit-0 checker — no failure mode here.
# Non-fatal: if it cannot be placed, the device still works; we just warn.
echo "[firstrun] $(date '+%H:%M:%S') Starting: provisioning status notice"
PROV_SRC="${REPO_DIR}/boot/gateway-provisioning-check.sh"
PROV_DST="/etc/profile.d/99-gateway-provisioning.sh"
if [ -f "$PROV_SRC" ]; then
    if cp "$PROV_SRC" "$PROV_DST" && chmod 644 "$PROV_DST"; then
        green "Installed provisioning-status login notice"
    else
        warn "Failed to install provisioning-status login notice (non-fatal)"
    fi
else
    warn "gateway-provisioning-check.sh not found in repo — skipping login notice"
fi
echo "[firstrun] $(date '+%H:%M:%S') Completed: provisioning status notice"

# --- Write provisioning sentinel ---
# Must be written only after ALL provisioning steps above have
# completed successfully.  set -e (line 4) guarantees a failure
# anywhere above exits before this point is reached.
echo "[firstrun] $(date '+%H:%M:%S') Starting: write sentinel"
touch "$SENTINEL"
echo "[firstrun] $(date '+%H:%M:%S') Completed: write sentinel"

# ── 14. Post-provisioning summary ─────────────────────────────────────────────

echo ""
echo "============================================"
echo "  Bootstrap complete."
echo "============================================"
echo ""
echo "Next steps:"
echo ""
echo "  1. Configure Helium:"
echo "       cp ${ENV_EXAMPLE} ${ENV_FILE}"
echo "       nano ${ENV_FILE}"
echo "       sudo systemctl start pktfwd gateway-rs"
echo ""
echo "  2. Start the web UI:"
echo "       sudo systemctl start gateway-ui"
echo "       # Access at http://$(hostname):8080"
echo ""
echo "  3. Set up Wingbits (if hardware is connected):"
echo "       sudo ${REPO_DIR}/scripts/wingbits-setup.sh \"<dashboard-url>\""
echo ""
echo "  4. Reboot to apply boot config changes:"
echo "       sudo reboot"
echo ""
