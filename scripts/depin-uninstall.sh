#!/bin/bash
# depin-uninstall.sh — destructive removal of a DePIN Docker service.
# Stops + disables systemd unit, removes data volumes, removes Docker image.
# Run as root (via sudo from gateway-ui backend).
# Usage: depin-uninstall.sh <project>
#   <project> must be one of: honeygain, urnetwork, myst, anyone
set -euo pipefail

readonly PROJECT="$1"

case "$PROJECT" in
    honeygain|urnetwork|myst|anyone) ;;
    *)
        echo "ERROR: unknown project '$PROJECT' — must be one of: honeygain, urnetwork, myst, anyone" >&2
        exit 1
        ;;
esac

UNIT="depin-${PROJECT}.service"
IMAGE=""
DATA_DIRS=()
VOLUME=""

case "$PROJECT" in
    honeygain)
        IMAGE="honeygain/honeygain:latest"
        ;;
    urnetwork)
        IMAGE="bringyour/community-provider:g4-latest"
        DATA_DIRS=("/var/lib/gateway-ui/urnetwork")
        ;;
    myst)
        IMAGE="mysteriumnetwork/myst:latest"
        VOLUME="myst-data"
        ;;
    anyone)
        IMAGE="ghcr.io/anyone-protocol/ator-protocol:latest"
        DATA_DIRS=(
            "/var/lib/gateway-ui/anyone/var"
            "/var/lib/gateway-ui/anyone/run"
        )
        ;;
esac

echo "=== Uninstalling DePIN: $PROJECT ==="

# Stop and disable the service
echo "Stopping and disabling $UNIT..."
systemctl stop "$UNIT" 2>/dev/null || true
systemctl disable "$UNIT" 2>/dev/null || true

# Remove any leftover container
/usr/bin/docker rm -f "$PROJECT" 2>/dev/null || true

# Remove bind-mounted data directories' contents
for d in "${DATA_DIRS[@]}"; do
    if [ -d "$d" ]; then
        echo "Removing contents of $d..."
        rm -rf "${d:?}"/*
    fi
done

# Remove named Docker volume (Myst only)
if [ -n "$VOLUME" ]; then
    echo "Removing Docker volume $VOLUME..."
    /usr/bin/docker volume rm "$VOLUME" 2>/dev/null || true
fi

# Remove Docker image
if [ -n "$IMAGE" ]; then
    echo "Removing Docker image $IMAGE..."
    /usr/bin/docker rmi "$IMAGE" 2>/dev/null || true
fi

echo "=== Uninstall complete for $PROJECT ==="
