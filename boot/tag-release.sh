#!/usr/bin/env bash
# tag-release.sh — create and push a date-based release tag
# Usage: ./boot/tag-release.sh [date]
# If no date provided, uses today's date (YYYY.MM.DD)
set -euo pipefail

DATE="${1:-$(date +%Y.%m.%d)}"

# Validate date format
if ! echo "$DATE" | grep -qE '^[0-9]{4}\.[0-9]{2}\.[0-9]{2}$'; then
    echo "ERROR: Invalid date format. Expected YYYY.MM.DD, got: ${DATE}" >&2
    exit 1
fi

# Build tag base
TAG="v${DATE}"

# Check if tag already exists locally or remotely, append .N if needed
SUFFIX=""
while git rev-parse "${TAG}${SUFFIX}" &>/dev/null || \
      git ls-remote --tags origin "${TAG}${SUFFIX}" | grep -q .; do
    if [ -z "$SUFFIX" ]; then
        SUFFIX=".1"
    else
        N="${SUFFIX#.}"
        SUFFIX=".$((N + 1))"
    fi
    echo "[tag] Tag ${TAG}${SUFFIX%.*} already exists — trying ${TAG}${SUFFIX}"
done

FINAL_TAG="${TAG}${SUFFIX}"

echo "=== Tag Release ==="
echo "Date:  ${DATE}"
echo "Tag:   ${FINAL_TAG}"

git tag -a "$FINAL_TAG" -m "Release ${DATE}"
echo "[tag] Created annotated tag: ${FINAL_TAG}"

echo "[tag] Pushing tag to origin..."
git push origin "$FINAL_TAG"

echo ""
echo "Release triggered!"
echo "Watch the build at:"
echo "  https://github.com/bitcryptic-gw/bittug/actions"
echo ""
echo "To download the image once built:"
echo "  https://github.com/bitcryptic-gw/bittug/releases/tag/${FINAL_TAG}"
