#!/usr/bin/env bash
# Stop this repo's worker and API processes.
#
#   ./scripts/cleanup.sh              stop only orphaned ones (the default)
#   ./scripts/cleanup.sh --all        stop every one, including ones you started
#                                     in a terminal
#   ./scripts/cleanup.sh --dry-run    list what would be stopped, kill nothing
#
# "Orphaned" means reparented to PID 1 with no controlling terminal, which is
# what a process started detached in the background looks like once its shell
# is gone. Those are the ones that quietly hold port 8000 and are awkward to
# find by hand. A worker you started yourself in a terminal has a tty and is
# left alone unless you pass --all.
#
# The Temporal server is never touched. Run and stop that separately.
#
# Scoped to this checkout: a candidate has to have its working directory set to
# this repo root, so an unrelated worker.py elsewhere on the machine is safe.

set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

all=false
dry_run=false
for arg in "$@"; do
  case "$arg" in
    --all) all=true ;;
    --dry-run|-n) dry_run=true ;;
    -h|--help)
      # Print the comment header, stopping at the first line of real code, so
      # this stays correct as the header grows or shrinks.
      awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' \
        "${BASH_SOURCE[0]}"
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Command line fragments identifying our two long running processes. Note that
# "temporal server" matches neither, and is filtered again below for safety.
PATTERNS=('worker\.py' 'api\.main:app')

# PIDs to never kill: this script, its shell, and their ancestors.
protected=""
pid=$$
while [ -n "$pid" ] && [ "$pid" != "0" ] && [ "$pid" != "1" ]; do
  protected="$protected $pid"
  pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
done

is_protected() {
  case " $protected " in *" $1 "*) return 0 ;; esac
  return 1
}

process_cwd() {
  # macOS has no /proc, so ask lsof for the cwd of this one pid.
  lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1
}

descendants() {
  local parent="$1" child
  for child in $(pgrep -P "$parent" 2>/dev/null); do
    echo "$child"
    descendants "$child"
  done
}

roots=""
for pattern in "${PATTERNS[@]}"; do
  for candidate in $(pgrep -f "$pattern" 2>/dev/null); do
    is_protected "$candidate" && continue

    command_line="$(ps -o command= -p "$candidate" 2>/dev/null)"
    [ -z "$command_line" ] && continue

    # Belt and braces: never the Temporal server, whatever the pattern matched.
    case "$command_line" in *"temporal server"*) continue ;; esac
    # Skip children; they are picked up by the descendant walk instead.
    case "$command_line" in *"multiprocessing-fork"*) continue ;; esac

    [ "$(process_cwd "$candidate")" = "$repo_root" ] || continue

    tty_name="$(ps -o tty= -p "$candidate" 2>/dev/null | tr -d ' ')"
    if [ "$all" = false ] && [ "$tty_name" != "??" ] && [ -n "$tty_name" ]; then
      echo "skipping pid $candidate (tty $tty_name, started from a terminal). Use --all to include it."
      continue
    fi

    case " $roots " in *" $candidate "*) continue ;; esac
    roots="$roots $candidate"
  done
done

# shellcheck disable=SC2086
roots="$(echo $roots | tr ' ' '\n' | sed '/^$/d' | sort -u)"

if [ -z "$roots" ]; then
  echo "Nothing to stop. No orphaned worker or API process from this checkout is running."
  exit 0
fi

targets=""
for root in $roots; do
  targets="$targets $root $(descendants "$root" | tr '\n' ' ')"
done
# shellcheck disable=SC2086
targets="$(echo $targets | tr ' ' '\n' | sed '/^$/d' | sort -u -n)"

echo "Will stop:"
for target in $targets; do
  printf '  %-7s %s\n' "$target" "$(ps -o command= -p "$target" 2>/dev/null | cut -c1-95)"
done

if [ "$dry_run" = true ]; then
  echo "(dry run, nothing killed)"
  exit 0
fi

# Ask nicely, then insist. Children first so a supervisor does not respawn them.
# shellcheck disable=SC2086
reversed="$(echo $targets | tr ' ' '\n' | sort -u -rn)"
for target in $reversed; do
  kill -TERM "$target" 2>/dev/null
done

for _ in 1 2 3 4 5 6 7 8 9 10; do
  still_running=""
  for target in $targets; do
    kill -0 "$target" 2>/dev/null && still_running="$still_running $target"
  done
  [ -z "$still_running" ] && break
  sleep 0.5
done

# shellcheck disable=SC2086
if [ -n "${still_running// /}" ]; then
  echo "Force killing:$still_running"
  for target in $still_running; do
    kill -KILL "$target" 2>/dev/null
  done
  sleep 1
fi

remaining=""
for target in $targets; do
  kill -0 "$target" 2>/dev/null && remaining="$remaining $target"
done

if [ -n "${remaining// /}" ]; then
  echo "Still running after SIGKILL:$remaining" >&2
  exit 1
fi

echo "Stopped. The Temporal server, if you are running one, was not touched."
