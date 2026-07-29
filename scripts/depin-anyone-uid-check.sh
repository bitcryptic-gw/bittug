#!/bin/bash
# depin-anyone-uid-check.sh — verify anond UID/GID matches host bind-mount ownership.
# Runs as ExecStartPre= in depin-anyone.service. Idempotent, safe for every startup.
# Uses --pull=never so no network hit on restart — image is already local from
# the main ExecStart= docker run (or from the first successful start).
#
# sync-provisioning.sh sets initial defaults (100:101) for first-boot provisioning;
# this script self-heals any drift after image updates, without needing a full
# provisioning re-run.
set -euo pipefail

IMAGE="ghcr.io/anyone-protocol/ator-protocol:latest"
DIRS="/var/lib/gateway-ui/anyone/var /var/lib/gateway-ui/anyone/run"

if ! docker image inspect "$IMAGE" &>/dev/null; then
    echo "Anyone image not yet pulled — skipping UID drift check (sync-provisioning.sh defaults apply)"
    exit 0
fi

ANOND_INFO=$(docker run --rm --pull=never --entrypoint id "$IMAGE" anond 2>&1) || {
    echo "ERROR: Failed to determine anond UID/GID from image: $ANOND_INFO" >&2
    echo "Ownership of Anyone bind-mount directories may be stale — check manually." >&2
    exit 0
}

ANOND_UID=$(echo "$ANOND_INFO" | sed -n 's/.*uid=\([0-9]*\).*/\1/p')
ANOND_GID=$(echo "$ANOND_INFO" | sed -n 's/.*gid=\([0-9]*\).*/\1/p')

if [ -z "$ANOND_UID" ] || [ -z "$ANOND_GID" ]; then
    echo "ERROR: Could not parse anond UID/GID from output: $ANOND_INFO" >&2
    exit 0
fi

fixed=0
for dir in $DIRS; do
    if [ ! -d "$dir" ]; then
        echo "ERROR: $dir does not exist — run sync-provisioning.sh first" >&2
        exit 0
    fi
    CUR_UID=$(stat -c '%u' "$dir")
    CUR_GID=$(stat -c '%g' "$dir")
    if [ "$CUR_UID" != "$ANOND_UID" ] || [ "$CUR_GID" != "$ANOND_GID" ]; then
        chown "${ANOND_UID}:${ANOND_GID}" "$dir" 2>/dev/null || {
            echo "WARNING: Failed to chown $dir to ${ANOND_UID}:${ANOND_GID} (was ${CUR_UID}:${CUR_GID})" >&2
            continue
        }
        chmod 750 "$dir" 2>/dev/null
        echo "Corrected $dir ownership to ${ANOND_UID}:${ANOND_GID} (was ${CUR_UID}:${CUR_GID})"
        fixed=1
    fi
done

if [ "$fixed" -eq 0 ]; then
    echo "anond UID/GID match: ${ANOND_UID}:${ANOND_GID}"
fi
