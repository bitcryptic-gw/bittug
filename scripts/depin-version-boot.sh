#!/bin/bash
# depin-version-boot.sh — version capture on a DePIN container (re)start at BOOT.
# Invoked as ExecStartPost= on each depin-<project>.service, so it fires right
# after the container comes up as part of the boot sequence (the 5th capture
# call site — reboot-boosted container starts). Runs as root (the depin units
# have no User=). Sourcing the shared library means the boot-time capture uses
# the exact same version patterns/extraction as the auto-update path — no
# second copy for these per-project log lines.
#
# Usage: depin-version-boot.sh <project>
# Depends on journald being up (confirmed: journald starts well before the
# depin units at boot) and, for honeygain, docker + outbound registry access.
set -euo pipefail

[ $# -eq 1 ] || { echo "usage: $0 <project>" >&2; exit 2; }
PROJECT="$1"

# ExecStartPost runs with a minimal environment; resolve our own directory.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/depin-version-lib.sh"

# Boot is slower and more variable than a manual restart: the ExecStartPost
# hook fires at an unpredictable delay after boot (observed 15s..>2min as the
# unit/container warms up), so a fixed --since window races the large, variable
# delay between boot and hook-fire. Instead grep the WHOLE current boot (`-b`):
# at boot the container starts for the first time this boot, so the first
# banner in the current-boot journal is the current one, whatever time the hook
# runs. Honeygain resolves via digest (no journal window needed).
# Bounded retries still cover the case where the container hasn't printed yet.
BOOT_RETRIES=30

capture_version_for "$PROJECT" "-b" "$BOOT_RETRIES"
exit 0
