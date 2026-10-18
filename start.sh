#!/usr/bin/env bash
# Start bridge.py in the background (idempotent). Usage: bash start.sh [--dry-run]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${WECHAT_PYTHON:-$( [[ -x "$DIR/.venv/bin/python3" ]] && echo "$DIR/.venv/bin/python3" || echo python3 )}"
PIDFILE="$DIR/state/bridge.pid"
mkdir -p "$DIR/state" "$DIR/logs"; chmod 700 "$DIR/state"

# Optional .env (chmod 600). Variables already in the process env win.
if [[ -f "$DIR/.env" ]]; then
  perm=$(stat -c %a "$DIR/.env")
  [[ "$perm" == "600" || "$perm" == "400" ]] || { echo "refusing: $DIR/.env must be chmod 600 (is $perm)" >&2; exit 2; }
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    line="${line#export }"
    key="${line%%=*}"; val="${line#*=}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
    if [[ -z "${!key:-}" ]]; then export "$key=$val"; fi
  done < "$DIR/.env"
fi

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null \
   && grep -q bridge.py "/proc/$(cat "$PIDFILE")/cmdline" 2>/dev/null; then
  echo "bridge already running (pid $(cat "$PIDFILE"))"; exit 0
fi
# Only one poller per WeChat bot channel: refuse if a bridge.py from ANOTHER install dir is running.
REAL="$(cd "$DIR" && pwd -P)"
for cmdf in /proc/[0-9]*/cmdline; do
  pid="${cmdf#/proc/}"; pid="${pid%/cmdline}"
  [[ "$pid" == "$$" ]] && continue
  [[ -r "$cmdf" ]] || continue
  first=1; is_py=0; script=""
  while IFS= read -r -d '' arg; do
    if (( first )); then first=0; [[ "$(basename -- "$arg")" == python* ]] && is_py=1; continue; fi
    if (( is_py )) && [[ "$(basename -- "$arg")" == "bridge.py" ]]; then script="$arg"; break; fi
  done < "$cmdf" 2>/dev/null || true
  [[ -n "$script" ]] || continue
  if [[ "$script" == /* ]]; then other="$(dirname "$(readlink -f "$script" 2>/dev/null || echo "$script")")"
  else other="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || echo unknown)"; fi
  if [[ "$other" != "$REAL" ]]; then
    echo "refusing: another bridge.py is already running from a different install dir: $other (pid $pid)." >&2
    echo "Only one poller can use a WeChat bot channel at a time. Stop the old bridge first (bash $other/stop.sh), then run start.sh again." >&2
    exit 3
  fi
done
if [[ -f "$DIR/state/expired" ]]; then
  echo "凭证已过期 (ERRCODE -14)：先运行 $PY $DIR/login.py 重新扫码，再 start.sh" >&2; exit 14
fi
if [[ ! -f "$DIR/state/credentials.json" ]]; then
  echo "还没绑定：先运行 $PY $DIR/login.py" >&2; exit 2
fi
if [[ "${1:-}" != "--dry-run" ]] && { [[ -z "${WECHAT_ROUTINE_WEBHOOK_URL:-}" ]] || [[ -z "${WECHAT_ROUTINE_WEBHOOK_KEY:-}" ]]; }; then
  echo "缺少 WECHAT_ROUTINE_WEBHOOK_URL / WECHAT_ROUTINE_WEBHOOK_KEY（环境变量或 $DIR/.env）" >&2; exit 2
fi

cd "$DIR"
setsid nohup "$PY" "$DIR/bridge.py" "$@" >>"$DIR/logs/bridge.out" 2>&1 < /dev/null &
echo $! > "$PIDFILE"
sleep 2
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "bridge started (pid $(cat "$PIDFILE")); log: $DIR/logs/bridge.log"
else
  echo "bridge exited immediately; see $DIR/logs/bridge.log and bridge.out" >&2; rm -f "$PIDFILE"; exit 1
fi
