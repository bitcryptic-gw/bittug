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

# ── Version capture on (re)start — NOT on the cycle timer ────────────────────
# The version line for urnetwork/mysterium/anyone lives on a STARTUP line,
# which scrolls out of any short tail window once the container has been
# running a while. So we only try to capture it immediately after a real
# restart (auto-update here, or the manual Update / enable paths in main.py).
# On a miss (slow start, or the line never appears), log loudly and PRESERVE
# the previously captured version — never overwrite it with blank.
# Honeygain has no version line at all; always a digest+date fallback.
# NOTE: the version-pattern list here mirrors _depin_capture_version_on_restart
# in gateway-ui/main.py — keep the two in sync.
JOURNALCTL="/bin/journalctl"
CAP_SINCE="25s"          # bounded re-read window after the restart
CAP_RETRIES=12           # ~12 x 1s of bounded readiness waiting

set_captured_version() {
    local project="$1" ver="$2"
    python3 -c "
import json, os
state = {'projects': {}}
try:
    if os.path.exists('$STATE_FILE'):
        state = json.load(open('$STATE_FILE'))
except: pass
state.setdefault('projects', {}).setdefault('$project', {})['captured_version'] = '$ver'
json.dump(state, open('$STATE_FILE', 'w'), indent=2)
" 2>/dev/null
}

# ── Honeygain digest→version resolution (shell / root context) ──────────────
# Honeygain's logs carry no version line, but its Docker Hub repo publishes
# versioned tags whose index digest can match the current :latest. Resolve the
# locally pulled digest to a published tag, cached for 24h so a manual Restart
# (a frequent capture trigger) does not hit Docker Hub every time. The cache is
# written to state['projects']['honeygain']['honeygain_cache'] and is SEPARATE
# from write_proj's per-cycle keys: write_proj loads the existing project dict
# and only sets its known keys, so this cache survives every 10-min cycle write
# (same deliberate separation captured_version already has). COVERAGE PARITY
# NOTE: this shell path indexes every known tag via imagetools inspect, and the
# Python resolver in main.py (gateway-ui cannot run docker) maps the same full
# tag set via the hub REST API + a per-tag /images backfill for old single-arch
# tags — the two now have equal coverage, so a given local digest resolves
# identically whether the trigger was auto-update (shell) or Restart/Update
# (Python). Keep the resolvers' tag coverage in sync.
HG_TTL_SECONDS=$((24 * 3600))
HG_TAGS_API="https://registry.hub.docker.com/v2/repositories/honeygain/honeygain/tags?page_size=100"

set_honeygain_cache() {
    # $1 = "tag=digest\ntag=digest..." (updated map), $2 = now epoch (unix)
    printf '%b' "$1" | python3 -c "
import json, os, sys
raw = sys.stdin.read()
now = sys.argv[1]; state_file = sys.argv[2]; project = 'honeygain'
tags = {}
for line in raw.splitlines():
    line = line.strip()
    if not line: continue
    t, _, d = line.partition('=')
    if t and d: tags[t] = d
s = {'projects': {}}
try:
    if os.path.exists(state_file):
        s = json.load(open(state_file))
except: pass
p = s.setdefault('projects', {}).setdefault(project, {})
p['honeygain_cache'] = {'resolved_at': int(now), 'tags': tags}
json.dump(s, open(state_file, 'w'), indent=2)
" "$2" "$STATE_FILE" 2>/dev/null
}

hg_cache_state() {
    # Print "state,tag" (no spaces) or "none" — how the current local digest
    # resolves from an existing cache. Comma-separated so it parses cleanly
    # with `IFS=, read state tag`.
    local wanted="$1"
    python3 -c "
import json, os, time, sys
wanted = sys.argv[1]; state_file = sys.argv[2]
try:
    if not os.path.exists(state_file): print('none'); sys.exit(0)
    s = json.load(open(state_file))
    c = s.get('projects', {}).get('honeygain', {}).get('honeygain_cache')
    if not isinstance(c, dict): print('none'); sys.exit(0)
    tags = c.get('tags', {}) or {}
    ts = c.get('resolved_at', 0)
    if wanted in tags.values():
        fresh = (time.time() - int(ts)) < $HG_TTL_SECONDS
        tag = [k for k, v in tags.items() if v == wanted][0]
        print(('fresh' if fresh else 'stale') + ',' + tag)
    else:
        print('none')
except Exception:
    print('none')
" "$wanted" "$STATE_FILE" 2>/dev/null
}

resolve_honeygain_version() {
    # Resolve the locally pulled :latest digest to a published version tag,
    # cache-first. Sets captured_version on a match; leaves it unset (no-op,
    # fall back to digest+date display) on no match. Never retries in a storm.
    local loc now map tag digest final_tag state
    loc="$(local_digest 'honeygain/honeygain:latest')"
    [ -z "$loc" ] && return 0   # image not pulled — nothing local to resolve

    IFS=',' read state tag <<< "$(hg_cache_state "$loc")"
    if [ "$state" = "fresh" ]; then
        set_captured_version honeygain "$tag"
        echo "$LOG_TAG: captured honeygain version: ${tag} (cached)"
        return 0
    fi

    # Cache miss or stale: full re-walk of published versioned tags.
    now="$(date -u +%s)"
    map=""
    for tag in $(curl -fsSL "$HG_TAGS_API" 2>/dev/null \
                    | python3 -c "import json,sys;d=json.load(sys.stdin);print(' '.join(t['name'] for t in (d.get('results') or []) if t['name']!='latest'))"); do
        digest="$("$DOCKER" buildx imagetools inspect --format '{{.Manifest.Digest}}' "honeygain/honeygain:${tag}" 2>/dev/null)"
        if [ -n "$digest" ]; then
            map="${map}${tag}=${digest}\n"
            if [ "$digest" = "$loc" ]; then final_tag="$tag"; fi
        fi
    done
    set_honeygain_cache "$map" "$now"   # always refresh cache from the re-walk
    if [ -n "${final_tag:-}" ]; then
        set_captured_version honeygain "$final_tag"
        echo "$LOG_TAG: captured honeygain version: ${final_tag} (matched digest ${loc})"
    else
        echo "$LOG_TAG: WARNING: no Honeygain tag matched local digest (${loc}); keeping prior captured_version"
    fi
}


capture_version_on_restart() {
    local project="$1" unit pat strip ver tries i
    case "$project" in
        honeygain) resolve_honeygain_version ; return 0 ;;
        urnetwork) pat='Provider [0-9][^ ]* started';  strip='s/^Provider //; s/ started$//' ;;
        myst)      pat='Starting Mysterium Node [0-9]+(\.[0-9]+)+';  strip='s/^Starting Mysterium Node //' ;;
        anyone)    pat='Anon version [0-9][^ ]*';  strip='s/^Anon version //' ;;
        *) return 0 ;;
    esac
    unit="depin-${project}.service"

    # Bounded readiness wait: retry reading the fresh startup window until the
    # version line appears or we time out. Don't grep once-and-give-up on a
    # slow start — but never block indefinitely.
    tries=0
    ver=""
    while [ "$tries" -lt "$CAP_RETRIES" ]; do
        ver=$("$JOURNALCTL" "-u" "$unit" "--since" "$CAP_SINCE" "-o" "cat" "--no-pager" 2>/dev/null \
                | grep -oE "$pat" | head -1 | sed "$strip" || true)
        [ -n "$ver" ] && break
        tries=$((tries + 1))
        sleep 1
    done

    if [ -z "$ver" ]; then
        echo "$LOG_TAG: WARNING: no version line captured for ${project} after restart (pattern '${pat}'); preserving prior captured_version"
        return 0
    fi
    set_captured_version "$project" "$ver"
    echo "$LOG_TAG: captured ${project} version: ${ver}"
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
