#!/usr/bin/env bash
# Wrapper for the scenario driver, so you do not have to remember the uv
# invocation. Runs from anywhere; any arguments are passed straight through.
#
#   ./scripts/run_scenario.sh happy_path
#   ./scripts/run_scenario.sh human_approval --decision approve
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

exec uv run python scripts/run_scenario.py "$@"
