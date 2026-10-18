#!/usr/bin/env bash
# Stop this install's bridge. Refuses (and removes nothing) if the pid in state/bridge.pid
# is a live process that does not belong to this install dir.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL="$(cd "$DIR" && pwd -P)"
PIDFILE="$DIR/state/bridge.pid"

# true if pid is a python process running bridge.py from this install dir (by script path or cwd)
belongs_here() {
  local pid="$1" arg cwd
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  while IFS= read -r -d '' arg; do
    [[ "$(basename -- "$arg")" == "bridge.py" ]] || continue
    if [[ "$arg" == /* ]]; then
      [[ "$(readlink -f "$arg" 2>/dev/null)" == "$REAL/bridge.py" ]] && return 0
    else
      [[ "$cwd" == "$REAL" ]] && return 0
    fi
  done < "/proc/$pid/cmdline"
  return 1
}

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  pid=$(cat "$PIDFILE")
  if ! belongs_here "$pid"; then
    echo "refusing to stop: pid $pid in $PIDFILE is not a bridge.py from this install dir ($REAL). Nothing was killed or removed." >&2
    echo "  its cwd: $(readlink -f "/proc/$pid/cwd" 2>/dev/null || echo unknown)" >&2
    exit 1
  fi
  kill "$pid"
  for _ in $(seq 1 45); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
  echo "bridge stopped (pid $pid)"
else
  echo "bridge not running"
fi
rm -f "$PIDFILE"
