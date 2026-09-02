#!/usr/bin/env bash
# Wrapper for the Temporal worker, so you do not have to remember the uv
# invocation. Runs from anywhere; any arguments are passed straight through.
#
#   ./scripts/worker.sh
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

exec uv run python worker.py "$@"
