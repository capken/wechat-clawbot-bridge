#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/state/bridge.pid"
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "bridge: running (pid $(cat "$PIDFILE"))"
else
  echo "bridge: stopped"
fi
[[ -f "$DIR/state/credentials.json" ]] && echo "credentials: present" || echo "credentials: missing (run login.py)"
[[ -f "$DIR/state/expired" ]] && echo "EXPIRED: ERRCODE -14 -> rerun login.py and rescan"
[[ -f "$DIR/state/context.json" ]] && echo "context_token: present (saved $(python3 -c "import json;print(json.load(open('$DIR/state/context.json')).get('saved_at',''))"))" || echo "context_token: none yet (send a WeChat message first)"
[[ -f "$DIR/login_status.json" ]] && echo "login: $(python3 -c "import json;print(json.load(open('$DIR/login_status.json')).get('status'))")"
[[ -s "$DIR/state/failed_webhooks.jsonl" ]] && echo "failed webhooks: $(wc -l < "$DIR/state/failed_webhooks.jsonl")"
echo "--- last log lines ---"; tail -n 5 "$DIR/logs/bridge.log" 2>/dev/null
