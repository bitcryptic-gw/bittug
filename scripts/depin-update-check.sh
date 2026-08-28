#!/bin/bash
# depin-update-check.sh — compare local Docker image digests against registry.
# Writes state to /var/lib/gateway-ui/depin-update-state.json.
# Run as root (via systemd timer). Safe to run repeatedly.
set -euo pipefail

STATE_FILE="/var/lib/gateway-ui/depin-update-state.json"
AUTO_FILE="/var/lib/gateway-ui/depin-auto-update.json"
NOTIFY_FILE="/var/lib/gateway-ui/depin-notify-pending"
LOG_TAG="depin-update-check"

DOCKER="/usr/bin/docker"

declare -A IMAGES=(
    ["honeygain"]="honeygain/honeygain:latest"
    ["urnetwork"]="bringyour/community-provider:g4-latest"
    ["myst"]="mysteriumnetwork/myst:latest"
    ["anyone"]="ghcr.io/anyone-protocol/ator-protocol:latest"
)

mkdir -p /var/lib/gateway-ui
chown root:gateway-ui /var/lib/gateway-ui 2>/dev/null || true
chmod 2775 /var/lib/gateway-ui 2>/dev/null || true

# ── Load prior state ─────────────────────────────────────────────────────────
# Emits one tab-delimited "project\tremote_digest\tupdate_available" line per
# project that has a known remote digest, so callers can preserve prior flags on
# transient failure. Tab-delimited (not ':'-delimited) because a remote digest
# itself contains ':'.
load_state() {
    if [ -f "$STATE_FILE" ]; then
        python3 -c "
import json, sys
try:
    d = json.load(open('$STATE_FILE'))
    for k, v in d.get('projects', {}).items():
        r = v.get('remote_digest', '')
        u = '1' if v.get('update_available', False) else '0'
        if r: print(f'{k}\t{r}\t{u}')
except: pass
" 2>/dev/null
    fi
}

load_auto() {
    if [ -f "$AUTO_FILE" ]; then
        python3 -c "
import json, sys
try:
    d = json.load(open('$AUTO_FILE'))
    for k, v in d.items():
        if v: print(k)
except: pass
" 2>/dev/null
    fi
}

log() {
    echo "$LOG_TAG: $*" >&2
}

# Parse a repo@pinned/sha256:... or bare sha256:... reference down to the digest.
norm_digest() {
    local ref="$1"
    ref="${ref##*@}"
    ref="${ref%%:*}":"${ref##*:}"
    printf '%s' "$ref"
}

# ── Local digest (what we have pulled) ───────────────────────────────────────
local_digest() {
    local image="$1" out
    out=$("$DOCKER" image inspect "$image" --format '{{index .RepoDigests 0}}' 2>/dev/null || true)
    [ -z "$out" ] && { printf ''; return; }
    norm_digest "$out"
}

# ── Image build/creation timestamp (when the local image was built) ───────────
image_created() {
    local image="$1" out
    out=$("$DOCKER" image inspect "$image" --format '{{.Created}}' 2>/dev/null || true)
    [ -z "$out" ] && { printf ''; return; }
    printf '%s' "$out"
}

# ── Latest remote index digest (read-only, no pull) ──────────────────────────
# Uses `docker buildx imagetools inspect`, which performs the registry token
# dance anonymously for both Docker Hub and ghcr.io (a bare curl / plain
# `docker manifest inspect` 401s or omits the index digest). Returns non-zero
# on any failure so the caller can treat it as "unknown", never as
# "no update available".
remote_digest() {
    local image="$1" out
    out=$("$DOCKER" buildx imagetools inspect --format '{{.Manifest.Digest}}' "$image" 2>/dev/null || true)
    [ -z "$out" ] && return 1
    norm_digest "$out"
}

# ── Helpers over the JSON state file ────────────────────────────────────────
prev_flag() {
    local project="$1"
    echo "${prev_state:-}" | awk -F'\t' -v p="$project" '$1==p{print $3}' || true
}

write_proj() {
    local project="$1" avail="$2" dig="$3" err="$4" lc="$5" locd="$6" created="$7"
    python3 -c "
import json, os
state = {'projects': {}}
try:
    if os.path.exists('$STATE_FILE'):
        state = json.load(open('$STATE_FILE'))
except: pass
state.setdefault('projects', {})
p = state['projects'].setdefault('$project', {})
p['remote_digest'] = '$dig'
p['local_digest'] = '$locd'
p['image_created'] = '$created'
p['update_available'] = ('$avail' == '1')
p['last_checked'] = '$lc'
if '$err':
    p['last_error'] = '$err'
else:
    p.pop('last_error', None)
json.dump(state, open('$STATE_FILE', 'w'), indent=2)
" 2>/dev/null
}

# ── Version capture on (re)start — logic is SHARED in scripts/depin-version-lib.sh ──
# capture_version_on_restart is the entry the auto-update path calls; the shared
# library (also sourced by the ExecStartPost boot hook) provides the actual capture
# per project (log-line patterns for ur/myst/anyone, digest->tag for honeygain).
source "$(dirname "$0")/depin-version-lib.sh"

capture_version_on_restart() {
    capture_version_for "$1"
}

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
prev_state=$(load_state)

# ── Check each project ───────────────────────────────────────────────────────
declare -A final_avail
declare -A new_remote
declare -A new_local
declare -A new_created
declare -A new_err

has_updates=0

for project in "${!IMAGES[@]}"; do
    image="${IMAGES[$project]}"

    loc=$(local_digest "$image")
    if [ -z "$loc" ]; then
        # Image not pulled — nothing to compare; record a clean no-update state.
        new_local[$project]=""
        new_created[$project]=""
        new_remote[$project]=""
        new_err[$project]=""
        continue
    fi
    new_local[$project]="$loc"
    new_created[$project]="$(image_created "$image")"

    rem=$(remote_digest "$image" || true)
    if [ -z "$rem" ]; then
        # Registry lookup failure. Log loudly and PRESERVE the prior update flag
        # rather than silently flipping this project to "no update available".
        log "manifest-inspect/buildx FAILED for ${image}; preserving prior update state"
        new_remote[$project]=""
        new_err[$project]="remote-inspect-failed:${image}"
        continue
    fi
    new_remote[$project]="$rem"

    if [ "$rem" != "$loc" ]; then
        final_avail[$project]=1
        has_updates=1
    fi
done

# ── Persist per-project state ────────────────────────────────────────────────
for project in "${!IMAGES[@]}"; do
    dig="${new_remote[$project]-}"
    if [ -z "$dig" ] && [ -z "${new_err[$project]-}" ]; then
        # Nothing pulled locally: no update possible, not an error.
        write_proj "$project" 0 "" "" "$NOW" "$(local_digest "${IMAGES[$project]}")" "${new_created[$project]-}"
        continue
    fi
    if [ -z "$dig" ]; then
        # Remote lookup failed: preserve prior flag, record the error.
        prior=$(prev_flag "$project")
        [ "$prior" = "1" ] && avail=1 || avail=0
        write_proj "$project" "$avail" "" "${new_err[$project]}" "$NOW" "$loc" "${new_created[$project]-}"
        if [ "$avail" = "1" ]; then
            # A preserved-available project is a real auto-update candidate for
            # this cycle: include it so it can be applied once its registry
            # lookup succeeds again, rather than leaving it flagged but idle.
            final_avail[$project]=1
            has_updates=1
        fi
        continue
    fi
    avail=0
    if [ -n "${final_avail[$project]-}" ]; then avail=1; fi
    write_proj "$project" "$avail" "$dig" "" "$NOW" "$loc" "${new_created[$project]-}"
done

# ── Apply auto-updates (only on confirmed flag) ──────────────────────────────
if [ "$has_updates" -eq 0 ]; then
    exit 0
fi

auto_projects=$(load_auto)

for project in "${!final_avail[@]}"; do
    if ! echo "$auto_projects" | grep -qw "$project"; then
        continue
    fi
    image="${IMAGES[$project]}"
    echo "$LOG_TAG: Auto-updating ${project} (${image})..."
    before="${new_local[$project]}"
    after=$("$DOCKER" pull "$image" 2>/dev/null && local_digest "$image") || true
    if [ -n "$after" ] && [ "$after" = "$before" ] || [ -z "$after" ]; then
        # Pull did not change the local image (failed, or pulled nothing new).
        # Do NOT clear update_available — it stays until a real apply happens.
        echo "$LOG_TAG: pull for ${project} did not change image (${after:-failed}); keeping update_available"
        continue
    fi
    "/bin/systemctl" restart "depin-${project}.service" 2>/dev/null || true
    # New container (re)started: capture the running version now, while its
    # startup version line is still in the fresh log window. Honeygain is a
    # no-op here (no version line). Aligned with the capture in main.py.
    capture_version_on_restart "$project"
    # Confirmed successful pull + applied: clear the flag to match the new state.
    # The pull + restart have already happened; a JSON write failure here must
    # degrade gracefully (log + no-op), not abort the end of the sequence.
    if ! python3 -c "
import json, os
try:
    p='$STATE_FILE'
    if os.path.exists(p):
        s=json.load(open(p))
        s.setdefault('projects', {}).setdefault('$project', {})['update_available'] = False
        json.dump(s, open(p, 'w'), indent=2)
except Exception:
    raise SystemExit(1)
" 2>/dev/null; then
        echo "$LOG_TAG: WARNING: failed to clear update_available for ${project} after apply (state write). Update was pulled and service restarted."
    else
        echo "$LOG_TAG: auto-updated ${project} and cleared update_available"
    fi
done

# Signal NTFY notifier that updates were found (picked up by gateway-ui's
# background notifier loop). Only leftover projects (manual ones) still flagged.
touch "$NOTIFY_FILE"
