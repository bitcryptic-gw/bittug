#!/bin/bash
# depin-version-lib.sh — SHARED version-capture machinery.
# Sourced by BOTH:
#   - scripts/depin-update-check.sh  (auto-update restart path)
#   - scripts/depin-version-boot.sh  (ExecStartPost= boot hook on depin-*.service)
# This is the SINGLE source of the per-project version patterns + extraction,
# so the two call sites never silently drift apart (this project was burned by
# duplicated version patterns drifting once before). Keep every capture-related
# constant, helper and pattern in THIS file only.
#
# Two capture sources, dispatched by project:
#   - urnetwork / myst / anyone : a startup LOg line (this file's patterns).
#   - honeygain                  : a registry digest->version tag resolution.
# Both write the SAME captured_version field via set_captured_version().

# ── Paths / constants ─────────────────────────────────────────────────────────
# Each honours an existing value (set by depin-update-check.sh before sourcing,
# or a test harness) so sourcing this library never clobbers the caller.
STATE_FILE="${STATE_FILE:-/var/lib/gateway-ui/depin-update-state.json}"
LOG_TAG="${LOG_TAG:-depin-version}"
JOURNALCTL="${JOURNALCTL:-/bin/journalctl}"
CAP_SINCE="${CAP_SINCE:-25s}"   # bounded re-read window after the (re)start
CAP_RETRIES="${CAP_RETRIES:-12}"   # ~12 x 1s of bounded readiness waiting
DOCKER="${DOCKER:-/usr/bin/docker}"   # allow the caller override (tests)

# Honeygain digest->version (see resolve_honeygain_version).
HG_TTL_SECONDS=${HG_TTL_SECONDS:-$((24 * 3600))}
HG_TAGS_API="https://registry.hub.docker.com/v2/repositories/honeygain/honeygain/tags?page_size=100"

# ── State writer ──────────────────────────────────────────────────────────────
set_captured_version() {
    # $1 = project, $2 = version string. Only touches captured_version; the
    # regular check-cycle writer (write_proj) never clears it.
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
    return 0   # best-effort; never abort the caller under set -e
}

# ── Digest helpers (used by the Honeygain resolver) ──────────────────────────
norm_digest() {
    # Parse a repo@pinned/sha256:... or bare sha256:... reference to the digest.
    local ref="$1"
    ref="${ref##*@}"
    ref="${ref%%:*}":"${ref##*:}"
    printf '%s' "$ref"
}

local_digest() {
    # The locally pulled image's RepoDigest (index digest), or empty if absent.
    local image="$1" out
    out=$("$DOCKER" image inspect "$image" --format '{{index .RepoDigests 0}}' 2>/dev/null || true)
    [ -z "$out" ] && { printf ''; return; }
    norm_digest "$out"
}

# ── Honeygain digest→version resolution ──────────────────────────────────────
# Honeygain's logs carry no version; its Docker Hub repo publishes versioned
# tags whose index digest can match the current :latest. Cache-first (24h TTL)
# so a frequent trigger (manual Restart / auto-update / boot hook) does not hit
# Docker Hub on every capture. On no match the digested is left unset (digest+
# date fallback). COVERAGE PARITY with main.py's Python resolver (hub REST API
# + /images backfill); keep in sync.

set_honeygain_cache() {
    # $1 = "tag=digest\ntag=digest...", $2 = now epoch
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
" "$2" "$STATE_FILE" 2>/dev/null || true
    return 0   # best-effort; never abort the caller under set -e
}

hg_cache_state() {
    # Print "state,tag" (fresh/stale) or "none" for the cached resolution.
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
    # Boundedly waits for the image to become inspectable: at boot the honeygain
    # image may not be registered with dockerd yet when the ExecStartPost hook
    # fires, so an immediate empty local_digest must not be treated as "not
    # pulled" (a silent no-op that blanks the card the way a reboot did).
    local loc now map tag digest final_tag state tries tags_list
    loc=""
    tries=0
    while [ -z "$loc" ] && [ "$tries" -lt 30 ]; do
        loc="$(local_digest 'honeygain/honeygain:latest')"
        [ -n "$loc" ] && break
        tries=$((tries + 1))
        sleep 2
    done
    [ -n "$loc" ] || return 0   # image truly not inspectable after waiting

    IFS=',' read state tag <<< "$(hg_cache_state "$loc")"
    if [ "$state" = "fresh" ]; then
        set_captured_version honeygain "$tag"
        echo "$LOG_TAG: captured honeygain version: ${tag} (cached)"
        return 0
    fi

    now="$(date -u +%s)"
    map=""
    # Fetch the live tag list into a variable FIRST so a transient curl/network
    # failure (plausible at boot even after network-online) yields an empty list
    # -> "no match, preserve prior" — it must NEVER abort the whole capture
    # under set -euo pipefail (which is how a briefly-unreachable hub silently
    # left honeygain un-captured on the device).
    tags_list="$(curl -fsSL "$HG_TAGS_API" 2>/dev/null \
                    | python3 -c "import json,sys;d=json.load(sys.stdin);print(' '.join(t['name'] for t in (d.get('results') or []) if t['name']!='latest'))" 2>/dev/null || true)"
    for tag in $tags_list; do
        digest="$("$DOCKER" buildx imagetools inspect --format '{{.Manifest.Digest}}' "honeygain/honeygain:${tag}" 2>/dev/null || true)"
        if [ -n "$digest" ]; then
            map="${map}${tag}=${digest}\n"
            if [ "$digest" = "$loc" ]; then final_tag="$tag"; fi
        fi
    done
    set_honeygain_cache "$map" "$now"
    if [ -n "${final_tag:-}" ]; then
        set_captured_version honeygain "$final_tag"
        echo "$LOG_TAG: captured honeygain version: ${final_tag} (matched digest ${loc})"
    else
        echo "$LOG_TAG: WARNING: no Honeygain tag matched local digest (${loc}); keeping prior captured_version"
    fi
}

# ── Log-line capture (urnetwork / myst / anyone) ─────────────────────────────
# Returns the version by grepping the project's unit journal in a bounded
# window. The log patterns live HERE — the SINGLE copy. On a miss, log loudly
# and PRESERVE the prior captured_version (never blank it).

version_pattern_for() {
    # Echo "pat<TAB>strip-seed" for a log-pattern project, or empty for none.
    # mastchain: the AIS-catcher startup banner is
    #   "AIS-catcher (build <date>) v<version>"
    # — e.g. "AIS-catcher (build Aug 19 2026) v0.00-1-unknown" (observed on the
    # actual consumed image, Mac-side no-dongle run 2026-08-31). The build date
    # varies per image rebuild, so the pattern must bridge it; the version token
    # is the trailing "v<version>" (the fork reports a rolling "0.00-1-unknown"
    # style version, matching recon's finding that the fork is unversioned).
    case "$1" in
        urnetwork) printf 'Provider [0-9][^ ]* started\ts/^Provider //; s/ started$//';;
        myst)      printf 'Starting Mysterium Node [0-9]+(\\.[0-9]+)+\ts/^Starting Mysterium Node //';;
        anyone)    printf 'Anon version [0-9][^ ]*\ts/^Anon version //';;
        mastchain) printf 'AIS-catcher .* v[0-9][^ ]*\ts/^AIS-catcher .* v//';;
    esac
}

capture_version_from_log() {
    # $1 = project, $2 (opt) = journalctl "since" arg (default "--since $CAP_SINCE"),
    # $3 (opt) = max retries (default $CAP_RETRIES).
    #   - manual-restart / auto-update: "--since 25s" (fresh window, banner has
    #     just appeared).
    #   - boot hook: "-b" (current boot). At boot the container starts for the
    #     first time this boot, so the FIRST banner in the current-boot journal
    #     IS the current one — this is race-free against the large, variable
    #     delay between boot and when the ExecStartPost hook actually fires.
    local project="$1" unit pair pat strip ver tries
    local since_arg="${2:---since $CAP_SINCE}" retries="${3:-$CAP_RETRIES}"
    pair="$(version_pattern_for "$project")"
    [ -n "$pair" ] || return 0   # no log pattern (e.g. honeygain / unknown)
    pat="${pair%%$'\t'*}"
    strip="${pair#*$'\t'}"
    unit="depin-${project}.service"

    # Bounded readiness wait: the container may not have printed its banner yet.
    # shellcheck disable=SC2086  # since_arg is intended to word-split
    tries=0
    ver=""
    while [ "$tries" -lt "$retries" ]; do
        ver=$("$JOURNALCTL" "-u" "$unit" $since_arg "-o" "cat" "--no-pager" 2>/dev/null \
                | grep -oE "$pat" | head -1 | sed "$strip" || true)
        [ -n "$ver" ] && break
        tries=$((tries + 1))
        sleep 1
    done

    if [ -z "$ver" ]; then
        echo "$LOG_TAG: WARNING: no version line captured for ${project} after (re)start (pattern '${pat}'); preserving prior captured_version"
        return 0
    fi
    set_captured_version "$project" "$ver"
    echo "$LOG_TAG: captured ${project} version: ${ver}"
}

# ── Dispatcher ───────────────────────────────────────────────────────────────
capture_version_for() {
    # $1 = project, $2 (opt) = journalctl since arg for log projects, $3 (opt) = retries.
    # Routes to the right capture source; the since-arg/retries are forwarded so
    # a boot-time caller (ExecStartPost, which fires at a variable delay after
    # boot) can pass `-b` (whole current boot) instead of a tight `--since`.
    case "$1" in
        honeygain) resolve_honeygain_version ;;
        urnetwork|myst|anyone|mastchain) capture_version_from_log "$1" "$2" "$3" ;;
    esac
}
