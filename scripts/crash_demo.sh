#!/usr/bin/env bash
# Wrapper for the durability demo. Kills the worker mid fan out and shows that
# the specialists that already finished are not re-run.
#
# Run this with no worker of your own running: the script starts and kills its
# own, and a second worker would absorb the crash and make the demo prove
# nothing. It checks for other pollers and refuses rather than mislead.
#
#   ./scripts/cleanup.sh --all    # stop any worker first
#   ./scripts/crash_demo.sh
#   ./scripts/crash_demo.sh --force
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

exec uv run python -u scripts/crash_demo.py "$@"
