#!/bin/bash
# tailscale-autoconnect.sh — automatic Tailscale re-authentication.
#
# WHY THIS EXISTS (Perth incident, 2026-07-18): deleting a device's machine
# record from the Tailscale admin console permanently invalidates its node
# key. For remotely-deployed units with no physical access this previously
# meant someone on the LAN had to re-enter an auth key by hand in the web
# UI. This script runs from tailscale-autoconnect.service (triggered at
# boot and every 10 minutes by tailscale-autoconnect.timer) and recovers
# automatically using the persisted key at /etc/gateway/tailscale.key
# (written by first-boot.sh or by tailscale-wrapper on every successful UI
# auth).
#
# TWO DISTINCT TRIGGER CONDITIONS (validated on hardware 2026-07-18):
#
#  1. BackendState == "NeedsLogin"  → act immediately.
#     Unambiguous: the control server has explicitly rejected or logged
#     out this node (happens at boot / tailscaled restart after the
#     machine record was deleted). Plain `tailscale up --auth-key=...`
#     re-registers in ~5s. Proven repeatedly on hardware.
#
#  2. BackendState == "Running" but Self.Online == false, observed on
#     DEGRADED_THRESHOLD consecutive runs (no healthy run in between)
#     → restart tailscaled, then handle the resulting state.
#     Deleting the machine record while the node is up does NOT move
#     tailscaled to NeedsLogin: the map poll just dies (health:
#     not-in-map-poll) and BackendState sits at "Running" indefinitely
#     (57+ min observed), while peers have already dropped the node.
#     Locally this is indistinguishable from a control-plane/WAN outage,
#     hence the threshold + probe gate below.
#
#     THE ACTION IS A DAEMON RESTART, NOT `up --force-reauth`. An earlier
#     revision used `tailscale up --auth-key=... --force-reauth` here and
#     it worked (re-registered in ~8s) but then raced tailscaled's own
#     deferred teardown of the wedged old control client: 29s after the
#     successful login an orphaned "control: client.Shutdown" fired and
#     killed the NEW session's control machinery, leaving a zombie —
#     BackendState "Running", Self.Online stale-true, node unreachable
#     for ~60 min until a human restarted tailscaled (observed 11:42:18,
#     2026-07-18; the stale Online=true also blinded this script's own
#     degraded detection). A daemon restart destroys the wedged client by
#     construction and lands in a clean state:
#       - record deleted  → fresh login is rejected → NeedsLogin in ~3-8s
#                           → trigger-1 path re-auths with the saved key
#                           (restart→NeedsLogin: 3s measured at 10:39:35;
#                           up→Running: 5s measured at 10:46:50)
#       - record intact   → fresh login succeeds with the EXISTING node
#         (false positive)   key, no auth key consumed, Running in ~5s
#                           (measured 12:42:11→12:42:16) — i.e. a false
#                           positive self-corrects at zero cost beyond a
#                           brief bounce
#
#     Probe gate (measured 2026-07-18, control IPs firewalled): recovery
#     attempts against unreachable control block for their full timeout
#     and, worse, queue deferred work inside tailscaled. A 10s HTTPS
#     probe of the control server gates the restart: any HTTP response
#     (even 4xx/redirect) proves reachability; timeout/refusal/DNS
#     failure skips the cycle. The counter stays at threshold, so we
#     re-probe every cycle and recover within one cycle (<=10 min) of
#     control returning — which is also why no retry backoff is needed:
#     the expensive path is structurally unreachable while control is
#     down, and a wider interval would only delay recovery.
#
#     Threshold rationale: 2 consecutive degraded observations at the
#     10-min cadence (~11–21 min depending on timer phase relative to
#     fault onset, average ~16 min). Benign transients — router reboots,
#     ISP re-leases, typical control incidents — resolve well inside that
#     window and never trigger a restart of a healthy daemon.
#
# The consecutive-observation counter lives in /run (tmpfs): it resets on
# reboot (correct — the boot path is condition 1) and on every healthy or
# operator-stopped observation.
#
# Pref preservation: `tailscale up` refuses to run if any non-default pref
# is not re-specified on the command line, and `--reset` (the old
# wrapper's workaround) silently wipes them instead. We therefore read the
# live prefs and re-specify --ssh / --advertise-routes / --hostname
# explicitly, plus the project-invariant --operator=gateway-ui. Never add
# --reset (wipes prefs) or --force-reauth (races the wedged-client
# teardown, see above) here.
#
# Deliberate non-actions:
#   - BackendState "Stopped" (operator ran `tailscale down`) is respected.
#   - Missing/empty key file: exit 0 quietly — manual re-auth via the web
#     UI remains the fallback path. Remove the key file to disable auto
#     re-auth.
#
# Worst-case runtime budget, single worst path as the code executes
# (degraded → restart → NeedsLogin → up), matching TimeoutStartSec=420
# in the unit:
#   initial settle_state 12       <= 60s  (12 x 5s sleeps)
#   control reachability probe    <= 10s  (curl --max-time 10)
#   systemctl restart tailscaled  <= 92s  (TimeoutStopSec=90 default + start;
#                                          15s observed with wedged client)
#   post-restart settle_state 12  <= 60s
#   tailscale up --timeout=90s    <= 90s
#   CLI/jq call overheads         ~= 10s
#   total                         ~= 322s → TimeoutStartSec=420 (~30% margin,
#                                          still < the 600s timer cadence)
# (The Running-after-restart sub-branch replaces the final 90s `up` with a
#  <=30s online-wait loop: 60+10+92+60+30 = 252s — inside the same cap.)
set -euo pipefail

KEY_FILE="/etc/gateway/tailscale.key"
TAILSCALE_BIN="/usr/bin/tailscale"
SYSTEMCTL_BIN="/usr/bin/systemctl"
COUNT_FILE="/run/tailscale-autoconnect.count"
ALERT_STATE_FILE="/run/tailscale-autoconnect.alert-state"
LAST_ALERT_FILE="/run/tailscale-autoconnect.last-alert"
LAST_RESTART_FILE="/run/tailscale-autoconnect.last-restart"
NTFY_CONFIG="/etc/gateway-ui/ntfy.json"
DEGRADED_THRESHOLD=2
LAST_RESTART_COOLDOWN=$(( 2 * 60 * 60 ))   # 2 hours between restart attempts
REPEAT_ALERT_INTERVAL=$(( 60 * 60 ))       # 1 hour between repeat alerts

log() { echo "[tailscale-autoconnect] $*"; }

reset_count() { rm -f "$COUNT_FILE"; }

read_count() {
    local c
    c=$(cat "$COUNT_FILE" 2>/dev/null) || c=0
    [[ "$c" =~ ^[0-9]+$ ]] || c=0
    echo "$c"
}

# Poll backend state until it settles (Running/NeedsLogin/Stopped) or the
# attempt budget runs out. Populates globals: state, online.
settle_state() {
    local tries="$1"
    state=""
    online="false"
    local status_json=""
    for _ in $(seq 1 "$tries"); do
        status_json=$("$TAILSCALE_BIN" status --json 2>/dev/null) || status_json=""
        state=$(jq -r '.BackendState // empty' <<<"$status_json" 2>/dev/null) || state=""
        online=$(jq -r '.Self.Online // false' <<<"$status_json" 2>/dev/null) || online="false"
        case "$state" in
            Running|NeedsLogin|Stopped) return 0 ;;
            *) sleep 5 ;;
        esac
    done
    return 0
}

# ── NTFY alerting ──────────────────────────────────────────────────────────

# Read NTFY config from the shared gateway-ui config file.
# Populates globals: ntfyserver, ntfytopic, ntfytoken.
# Returns 0 if config is usable, 1 otherwise.
ntfy_server=""; ntfy_topic=""; ntfy_token=""
load_ntfy_config() {
    ntfy_server=""; ntfy_topic=""; ntfy_token=""
    if [ ! -r "$NTFY_CONFIG" ]; then
        log "NTFY config not found or not readable at ${NTFY_CONFIG} — alerting disabled"
        return 1
    fi
    ntfy_server=$(jq -r '.server // empty' "$NTFY_CONFIG" 2>/dev/null)   || ntfy_server=""
    ntfy_topic=$(jq -r '.topic // empty' "$NTFY_CONFIG" 2>/dev/null)     || ntfy_topic=""
    ntfy_token=$(jq -r '.token // empty' "$NTFY_CONFIG" 2>/dev/null)     || ntfy_token=""
    if [ -z "$ntfy_server" ] || [ -z "$ntfy_topic" ]; then
        log "NTFY server or topic not configured — alerting disabled"
        return 1
    fi
    return 0
}

# Send an NTFY notification. Non-fatal: failure is logged, never aborts recovery.
# Usage: ntfy_send <title> <message> [priority] [tags]
ntfy_send() {
    local title="$1" message="$2" priority="${3:-default}" tags="${4:-}"
    local url="${ntfy_server%/}/${ntfy_topic}"
    local args=(-s -X POST --max-time 5 "$url"
        -H "Title: ${title}"
        -H "Priority: ${priority}"
        -H "Tags: ${tags}"
        -H "Content-Type: text/plain")
    if [ -n "$ntfy_token" ]; then
        args+=(-H "Authorization: Bearer ${ntfy_token}")
    fi
    args+=(-d "${message}")
    if curl "${args[@]}" >/dev/null 2>&1; then
        log "NTFY alert sent: ${title}"
    else
        log "WARNING: NTFY send failed (${title}) — continuing without alert"
    fi
    return 0  # best-effort: never abort the caller
}

# ── Alert state tracking (in /run — survives daemon restarts, not reboots) ─

read_alert_state() {
    cat "$ALERT_STATE_FILE" 2>/dev/null || echo "healthy"
}

write_alert_state() {
    echo "$1" > "$ALERT_STATE_FILE"
}

# Returns 0 if enough time has elapsed since the last repeat alert.
should_repeat_alert() {
    local now last
    now=$(date +%s)
    last=$(cat "$LAST_ALERT_FILE" 2>/dev/null) || last=0
    [[ "$last" =~ ^[0-9]+$ ]] || last=0
    [ $(( now - last )) -ge "$REPEAT_ALERT_INTERVAL" ]
}

mark_alert_time() {
    date +%s > "$LAST_ALERT_FILE"
}

# ── Restart rate-limit (prevent cycling during extended outages) ────────────

can_restart() {
    local now last
    now=$(date +%s)
    last=$(cat "$LAST_RESTART_FILE" 2>/dev/null) || last=0
    [[ "$last" =~ ^[0-9]+$ ]] || last=0
    [ $(( now - last )) -ge "$LAST_RESTART_COOLDOWN" ]
}

mark_restart_attempt() {
    date +%s > "$LAST_RESTART_FILE"
}

if [ ! -x "$TAILSCALE_BIN" ]; then
    log "tailscale not installed — nothing to do"
    exit 0
fi
if ! command -v jq >/dev/null 2>&1; then
    log "WARNING: jq not available — cannot inspect backend state"
    exit 0
fi

if [ ! -f "$KEY_FILE" ]; then
    log "no saved auth key at ${KEY_FILE} — auto re-auth disabled (authenticate once via the web UI Network tab to enable it)"
    exit 0
fi
if [ ! -r "$KEY_FILE" ] || [ ! -s "$KEY_FILE" ]; then
    log "WARNING: ${KEY_FILE} exists but is empty or unreadable — refusing auto re-auth"
    exit 0
fi

# Load NTFY config (non-fatal: alerting is best-effort, never blocks recovery).
load_ntfy_config || log "NTFY alerting unavailable — continuing without alerts"

# Up to 60s for the backend to settle — shortly after boot or a tailscaled
# restart it may still be dialling the control server.
settle_state 12

case "$state" in
    Stopped)
        log "backend is Stopped (tailscale down) — not intervening"
        reset_count
        exit 0
        ;;
    NeedsLogin)
        : # trigger condition 1 — re-auth below
        ;;
    Running)
        if [ "$online" = "true" ]; then
            log "healthy (Running, online) — nothing to do"
            reset_count
            if [ "$(read_alert_state)" != "healthy" ]; then
                log "degraded condition resolved — sending recovery alert"
                ntfy_send "Tailscale Recovered" \
                    "Tailscale is back online on $(hostname) after automated recovery." \
                    "default" "white_check_mark,tailscale"
                write_alert_state "healthy"
            fi
            exit 0
        fi

        count=$(( $(read_count) + 1 ))
        echo "$count" > "$COUNT_FILE"
        log "DEGRADED: BackendState=Running but Self.Online=false (control unreachable) — observation ${count}/${DEGRADED_THRESHOLD} since last healthy run"
        if [ "$count" -lt "$DEGRADED_THRESHOLD" ]; then
            log "below threshold — waiting (a deleted machine record persists; transient outages clear on their own)"
            exit 0
        fi

        # Reachability probe (see header): never restart the daemon over a
        # genuine outage — pointless churn that would also drop any
        # surviving data-plane sessions. The probe result also determines
        # what alert (if any) should be fired — a successful probe means
        # the problem is specific to this node (= zombie), while a failed
        # probe means site-wide or infrastructure outage.
        probe_code="000"
        if command -v curl >/dev/null 2>&1; then
            probe_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://controlplane.tailscale.com/ 2>/dev/null) || probe_code="000"
        fi

        if [ "$probe_code" != "000" ]; then
            # ── Probe succeeded: network is fine, this node is a zombie ──
            log "control server reachable (probe HTTP ${probe_code}) but this node is offline — zombie condition confirmed"

            alert_state=$(read_alert_state)
            if [ "$alert_state" != "zombie" ] || should_repeat_alert; then
                ntfy_send "Tailscale Zombie Detected" \
                    "$(hostname) is running but reports as offline while the control server is reachable — attempting automatic daemon restart to recover." \
                    "high" "rotating_light,tailscale"
                write_alert_state "zombie"
                mark_alert_time
            fi

            if can_restart; then
                mark_restart_attempt
            else
                log "restart suppressed by cooldown (last attempt within ${LAST_RESTART_COOLDOWN}s) — will retry on next timer run"
                exit 0
            fi
        else
            # ── Probe failed: network or infrastructure outage ──
            log "control server unreachable (probe failed) — network or Tailscale infrastructure outage; skipping recovery this cycle"

            alert_state=$(read_alert_state)
            if [ "$alert_state" != "connectivity-down" ] || should_repeat_alert; then
                ntfy_send "Tailscale Connectivity Lost" \
                    "$(hostname) cannot reach the Tailscale control server — this may be a site-wide internet outage or a Tailscale infrastructure issue. Automated recovery is standing by until connectivity returns." \
                    "high" "warning,tailscale"
                write_alert_state "connectivity-down"
                mark_alert_time
            fi
            exit 0
        fi

        if ! "$SYSTEMCTL_BIN" restart tailscaled; then
            log "ERROR: systemctl restart tailscaled failed — will retry on the next timer run"
            exit 1
        fi

        # Fresh daemon: deleted record lands NeedsLogin (3-8s measured),
        # intact record lands Running with the existing identity (~5s
        # measured, no auth key consumed). Self.Online can lag Running by
        # a few seconds, so poll a little longer for it below.
        settle_state 12
        if [ "$state" = "Running" ]; then
            for _ in 1 2 3 4 5 6; do
                if [ "$online" = "true" ]; then
                    break
                fi
                sleep 5
                online=$("$TAILSCALE_BIN" status --json 2>/dev/null | jq -r '.Self.Online // false' 2>/dev/null) || online="false"
            done
            if [ "$online" = "true" ]; then
                log "recovered after restart with existing identity (machine record intact — degraded state was a false positive or transient); no auth key consumed"
                ntfy_send "Tailscale Recovered" \
                    "Tailscale is back online on $(hostname) after automated daemon restart." \
                    "default" "white_check_mark,tailscale"
                write_alert_state "healthy"
                reset_count
                exit 0
            fi
            log "ERROR: still Running-but-offline after restart — will retry on the next timer run"
            exit 1
        fi
        if [ "$state" != "NeedsLogin" ]; then
            log "ERROR: backend state '${state:-unknown}' after restart — will retry on the next timer run"
            exit 1
        fi
        log "restart landed in NeedsLogin (machine record gone) — proceeding to re-auth with saved key"
        ;;
    *)
        log "backend state '${state:-unknown}' after wait — nothing to do"
        exit 0
        ;;
esac

# ── Re-auth with the saved key (trigger 1, and trigger 2 after restart) ──

# Preserve the currently-set prefs explicitly (see header).
prefs=$("$TAILSCALE_BIN" debug prefs 2>/dev/null) || prefs='{}'
run_ssh=$(jq -r 'if .RunSSH == true then "true" else "false" end' <<<"$prefs" 2>/dev/null) || run_ssh="false"
routes=$(jq -r '(.AdvertiseRoutes // []) | join(",")' <<<"$prefs" 2>/dev/null) || routes=""
hostname_pref=$(jq -r '.Hostname // ""' <<<"$prefs" 2>/dev/null) || hostname_pref=""

args=( up "--auth-key=file:${KEY_FILE}" "--operator=gateway-ui" "--ssh=${run_ssh}" "--timeout=90s" )
if [ -n "$routes" ]; then
    args+=( "--advertise-routes=${routes}" )
fi
if [ -n "$hostname_pref" ]; then
    args+=( "--hostname=${hostname_pref}" )
fi

log "backend is NeedsLogin — attempting automatic re-auth (ssh=${run_ssh}${routes:+, routes=${routes}}${hostname_pref:+, hostname=${hostname_pref}})"

if "$TAILSCALE_BIN" "${args[@]}"; then
    log "automatic re-auth succeeded"
    ntfy_send "Tailscale Recovered" \
        "Tailscale re-authenticated successfully on $(hostname) after machine record re-registration." \
        "default" "white_check_mark,tailscale"
    write_alert_state "healthy"
    reset_count
else
    rc=$?
    # Counter is deliberately NOT reset on failure: once the degraded
    # threshold has been reached, recovery retries every cycle until it
    # succeeds or a healthy observation resets the counter.
    log "ERROR: automatic re-auth failed (exit ${rc}) — the saved key may be revoked/expired; will retry on the next timer run (manual fallback: web UI Network tab)"
    exit "$rc"
fi
