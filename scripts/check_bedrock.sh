#!/usr/bin/env bash
# Wrapper for the Bedrock preflight check, so you do not have to remember the
# uv invocation. Runs from anywhere; any arguments are passed straight through.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

exec uv run python scripts/check_bedrock.py "$@"
