#!/bin/bash
# depin-update-check.sh — compare local Docker image digests against registry.
# Writes state to /var/lib/gateway-ui/depin-update-state.json.
# Run as root (via systemd timer). Safe to run repeatedly.
set -euo pipefail

STATE_FILE="/var/lib/gateway-ui/depin-update-state.json"
AUTO_FILE="/var/lib/gateway-ui/depin-auto-update.json"
NOTIFY_FILE="/var/lib/gateway-ui/depin-notify-pending"

declare -A IMAGES=(
    ["honeygain"]="honeygain/honeygain:latest"
    ["urnetwork"]="bringyour/community-provider:g4-latest"
    ["myst"]="mysteriumnetwork/myst:latest"
    ["anyone"]="ghcr.io/anyone-protocol/ator-protocol:latest"
)

mkdir -p /var/lib/gateway-ui

# ── Load current state ───────────────────────────────────────────────────────
prev_state=$(load_state)

load_auto() {
    if [ -f "$STATE_FILE" ]; then
        python3 -c "
import json, sys
try:
    d = json.load(open('$STATE_FILE'))
    for k, v in d.get('projects', {}).items():
        r = v.get('remote_digest', '')
        if r: print(f'{k}:{r}')
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

# ── Check each project ───────────────────────────────────────────────────────
declare -A new_remote
has_updates=0

for project in "${!IMAGES[@]}"; do
    image="${IMAGES[$project]}"

    # Get local digest (if image is pulled)
    loc=$(docker image inspect "$image" --format '{{index .RepoDigests 0}}' 2>/dev/null || true)
    if [ -z "$loc" ]; then
        # Image not pulled — nothing to compare
        continue
    fi

    # Get remote manifest digest (lightweight, no full pull)
    # docker manifest inspect returns the manifest list digest; for single-arch
    # images, compare against the manifest digest directly.
    rem=$(docker manifest inspect "$image" 2>/dev/null | python3 -c "
import json, sys
try:
    m = json.load(sys.stdin)
    if 'manifests' in m:
        # Multi-arch — use the arm64 variant digest
        for entry in m['manifests']:
            if entry.get('platform', {}).get('architecture') == 'arm64':
                print(entry['digest'])
                break
    if 'config' in m:
        print(m['config'].get('digest', m.get('digest', '')))
except: pass
" 2>/dev/null || true)

    if [ -z "$rem" ]; then
        continue
    fi

    new_remote[$project]="$rem"

    # Compare against previously-known remote digest
    prev=$(echo "${prev_state:-}" | grep "^${project}:" | cut -d: -f2- || true)
    if [ "$prev" != "$rem" ]; then
        available[$project]=1
        has_updates=1
    fi
done

# ── Build state JSON ─────────────────────────────────────────────────────────
# Build per-project entries as JSON fragments
for project in "${!IMAGES[@]}"; do
    avail=0
    dig="${new_remote[$project]-}"
    if [ -n "${available[$project]-}" ]; then avail=1; fi
    python3 -c "
import json, os
state = {'projects': {}}
try:
    if os.path.exists('$STATE_FILE'):
        state = json.load(open('$STATE_FILE'))
except: pass
state.setdefault('projects', {})['$project'] = {
    'remote_digest': '$dig',
    'update_available': bool($avail)
}
json.dump(state, open('$STATE_FILE', 'w'), indent=2)
"
done

# ── Handle updates ───────────────────────────────────────────────────────────
if [ "$has_updates" -eq 0 ]; then
    exit 0
fi

# Check auto-update flags
auto_projects=$(load_auto)
for project in "${!available[@]}"; do
    if echo "$auto_projects" | grep -qw "$project"; then
        echo "Auto-updating $project..."
        image="${IMAGES[$project]}"
        /usr/bin/docker pull "$image" && \
            /bin/systemctl restart "depin-${project}.service" || true
    fi
done

# Signal NTFY notifier that updates were found (picked up by gateway-ui's
# background notifier loop)
touch "$NOTIFY_FILE"
