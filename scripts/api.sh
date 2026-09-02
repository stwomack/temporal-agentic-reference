#!/usr/bin/env bash
# Wrapper for the API and UI server, so you do not have to remember the uv
# invocation. Runs from anywhere.
#
#   ./scripts/api.sh
#   ./scripts/api.sh --no-access-log        # extra uvicorn flags pass through
#
# Host and port come from API_HOST and API_PORT, read through the project's
# settings so a value in .env is honored the same way the rest of the app
# honors it. Anything you pass on the command line is appended after the
# defaults, and uvicorn lets the later flag win, so "--port 9000" still works.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

read -r host port <<<"$(uv run python -c \
  'from common.config import get_settings as g; s = g(); print(s.api_host, s.api_port)')"

echo "Serving the UI on http://${host}:${port}"
exec uv run uvicorn api.main:app --reload --host "$host" --port "$port" "$@"
