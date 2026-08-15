#!/bin/bash
# tailscale-reauth-watchdog.sh — fallback recovery for web-UI user reauth.
#
# WHY THIS EXISTS: the gateway-ui web UI can trigger an interactive/browser
# reauthentication (`tailscale up --force-reauth`, no authkey) so a
# tag-authenticated device can be moved to user authentication. That call can
# sever the tailnet interface — and with it the very gateway-ui session that
# triggered it. This watchdog runs as its own systemd oneshot, independent of
# the UI's request/response cycle, and guarantees the device recovers:
#
#   * It waits out the configurable window (deadline written by
#     tailscale-wrapper reauth into /var/lib/gateway-ui/tailscale-reauth.json).
#   * It then checks whether a new USER-AUTHENTICATED connection has actually
#     been established: BackendState == Running, Self.Online == true, and
#     Self.Tags empty (a user-owned node carries no ACL tags).
#   * If the window elapses without that, it restores the known-working tagged
#     connection by re-running `tailscale up --auth-key=file:/etc/gateway/
#     tailscale.key`, preserving all existing prefs.
#
# This mirrors the real-world case (Unraid servers) where force-reauth was
# done manually with a browser login; the watchdog is the safety net for the
# case where the human never completes the login, or where the known upstream
# `tailscale up --force-reauth` hang bug strikes (login completed in the
# browser but the CLI never returns).
#
# State transitions written back to tailscale-reauth.json:
#   pending   -> success   (user-auth connection confirmed)
#   pending   -> fallback  (tagged auth key restored)
#   pending   -> fallback-failed  (no usable key, or tailscale up failed)
set -euo pipefail

STATE_FILE="/var/lib/gateway-ui/tailscale-reauth.json"
PID_FILE="/var/lib/gateway-ui/tailscale-reauth.pid"
LOG_FILE="/var/log/gateway-tailscale-reauth.log"
KEY_FILE="/etc/gateway/tailscale.key"
TAILSCALE_BIN="/usr/bin/tailscale"
SYSTEMCTL_BIN="/usr/bin/systemctl"
SLEEP_STEP=10

log() { echo "[tailscale-reauth-watchdog] $*" | tee -a "$LOG_FILE"; }

# ── helpers ──────────────────────────────────────────────────────────────────

read_state_field() {
    local key="$1"
    local default="${2:-}"
    [ -r "$STATE_FILE" ] || { echo "$default"; return; }
    jq -r --arg k "$key" --arg d "$default" '.[$k] // $d' "$STATE_FILE" 2>/dev/null || echo "$default"
}

write_status() {
    local new_status="$1"
    local started window deadline
    started=$(read_state_field triggered_at "0")
    window=$(read_state_field window "480")
    deadline=$(read_state_field deadline "0")
    cat > "$STATE_FILE" <<EOF
{"status":"${new_status}","triggered_at":${started},"window":${window},"deadline":${deadline}}
EOF
    chmod 0644 "$STATE_FILE"
}

kill_pending_reauth() {
    # Kill the lingering `tailscale up --force-reauth` CLI if it is still
    # running (the known hang bug leaves it stuck after browser login, or it
    # is simply still waiting for a login that will now be replaced by the
    # tagged-key up below).
    local pid
    if [ -r "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE" 2>/dev/null || true)
        if [[ "$pid" =~ ^[0-9]+$ ]] && [ "$pid" -gt 1 ] && kill -0 "$pid" 2>/dev/null; then
            log "killing lingering reauth process (pid ${pid})"
            kill "$pid" 2>/dev/null || true
            sleep 2
        fi
        rm -f "$PID_FILE"
    fi
}

# Preserve current prefs explicitly (same contract as the wrappers: never
# --reset, which silently wipes prefs). Populates globals: run_ssh, routes,
# hostname_pref.
load_prefs() {
    local prefs
    prefs=$("$TAILSCALE_BIN" debug prefs 2>/dev/null) || prefs='{}'
    run_ssh=$(jq -r 'if .RunSSH == true then "true" else "false" end' <<<"$prefs" 2>/dev/null) || run_ssh="false"
    routes=$(jq -r '(.AdvertiseRoutes // []) | join(",")' <<<"$prefs" 2>/dev/null) || routes=""
    hostname_pref=$(jq -r '.Hostname // ""' <<<"$prefs" 2>/dev/null) || hostname_pref=""
}

# Returns 0 if a user-authenticated connection is confirmed.
user_auth_confirmed() {
    local json backend online tags
    json=$("$TAILSCALE_BIN" status --json 2>/dev/null) || return 1
    backend=$(jq -r '.BackendState // ""' <<<"$json" 2>/dev/null || true)
    online=$(jq -r '.Self.Online // false' <<<"$json" 2>/dev/null || true)
    tags=$(jq -r '(.Self.Tags // []) | length' <<<"$json" 2>/dev/null || echo 1)
    [ "$backend" = "Running" ] && [ "$online" = "true" ] && [ "$tags" -eq 0 ]
}

# ── main ─────────────────────────────────────────────────────────────────────

# No pending reauth state → nothing to supervise.
if [ ! -r "$STATE_FILE" ]; then
    exit 0
fi
status=$(read_state_field status "idle")
if [ "$status" != "pending" ]; then
    exit 0
fi

deadline=$(read_state_field deadline "0")
[[ "$deadline" =~ ^[0-9]+$ ]] || deadline=0
now=$(date +%s)

# Wait out the remaining window. Re-check the state file each sleep step so an
# externally-cancelled reauth (state file removed / status changed) stops the
# wait immediately.
if [ "$now" -lt "$deadline" ]; then
    remaining=$((deadline - now))
    log "window open — waiting ${remaining}s for interactive user login to complete"
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if [ ! -r "$STATE_FILE" ]; then
            log "state file removed — cancelling watch"
            exit 0
        fi
        if [ "$(read_state_field status "pending")" != "pending" ]; then
            exit 0
        fi
        sleep "$SLEEP_STEP"
    done
fi

log "window elapsed — checking for user-authenticated connection"

if user_auth_confirmed; then
    log "SUCCESS: user-authenticated connection established (Running, online, no ACL tags)"
    kill_pending_reauth
    write_status "success"
    exit 0
fi

# ── Fallback: restore the known-working tagged connection ────────────────────

log "user-auth connection NOT confirmed — falling back to saved tagged auth key"
kill_pending_reauth

if [ ! -r "$KEY_FILE" ] || [ ! -s "$KEY_FILE" ]; then
    log "ERROR: no usable saved auth key at ${KEY_FILE} — manual intervention required"
    write_status "fallback-failed"
    exit 1
fi

load_prefs

args=( up "--auth-key=file:${KEY_FILE}" "--operator=gateway-ui" "--ssh=${run_ssh}" "--timeout=90s" )
if [ -n "$routes" ]; then
    args+=( "--advertise-routes=${routes}" )
fi
if [ -n "$hostname_pref" ]; then
    args+=( "--hostname=${hostname_pref}" )
fi

log "restoring tagged connection: tailscale ${args[*]}"
if "$TAILSCALE_BIN" "${args[@]}"; then
    log "FALLBACK OK: tagged connection restored with saved auth key"
    write_status "fallback"
    exit 0
else
    rc=$?
    log "ERROR: fallback re-auth failed (exit ${rc}) — the saved key may be revoked/expired"
    write_status "fallback-failed"
    exit "$rc"
fi
