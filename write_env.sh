#!/usr/bin/env bash
# Write .env (chmod 600) next to this script from two environment variables:
#   WECHAT_ROUTINE_WEBHOOK_URL   routine webhook "POST to" URL (must start with https://)
#   WECHAT_ROUTINE_WEBHOOK_KEY   routine webhook key (a leading "Bearer " is stripped)
# Usage: WECHAT_ROUTINE_WEBHOOK_URL=... WECHAT_ROUTINE_WEBHOOK_KEY=... bash write_env.sh [--check]
#   --check  after writing, POST one harmless {"source":"setup-check"} request to the webhook (the routine ignores it)
#            and report only the HTTP status: 2xx ok; 401/403/404 = wrong key/URL or paused routine (exit 4).
# A variable that is not set keeps its current value from an existing .env (so you can fix just one value).
# Never prints the values. On validation failure prints a short error and writes nothing.
set -euo pipefail
CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

url="${WECHAT_ROUTINE_WEBHOOK_URL:-}"
key="${WECHAT_ROUTINE_WEBHOOK_KEY:-}"

# Re-requesting only one wrong value: a variable that is not set keeps its value from the existing .env.
old_val() { [[ -f "$DIR/.env" ]] || return 0; local l; l="$(grep -m1 "^$1=" "$DIR/.env" || true)"; printf '%s' "${l#*=}"; }
[[ -n "$url" ]] || url="$(old_val WECHAT_ROUTINE_WEBHOOK_URL)"
[[ -n "$key" ]] || key="$(old_val WECHAT_ROUTINE_WEBHOOK_KEY)"

fail() { echo "write_env: $1 (nothing written)" >&2; exit 2; }

# trim surrounding whitespace / CR / LF
trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; s="${s%"${s##*[![:space:]]}"}"; printf '%s' "$s"; }
url="$(trim "$url")"
key="$(trim "$key")"

# tolerate a pasted "Authorization: Bearer xxx" / "Bearer xxx" key
shopt -s nocasematch
if [[ "$key" == authorization:* ]]; then key="$(trim "${key#*:}")"; fi
if [[ "$key" == bearer[[:space:]]* ]]; then key="$(trim "${key#*[[:space:]]}")"; fi
shopt -u nocasematch

[[ -n "$url" ]] || fail "WECHAT_ROUTINE_WEBHOOK_URL is empty or not set"
[[ -n "$key" ]] || fail "WECHAT_ROUTINE_WEBHOOK_KEY is empty or not set"
[[ "$url" == https://* ]] || fail "webhook URL must start with https://"
[[ "$url" =~ ^https://[^[:space:]\"\'\\]+$ ]] || fail "webhook URL has whitespace/quotes or is malformed"
(( ${#url} <= 2048 )) || fail "webhook URL is too long"
[[ "$key" =~ ^[^[:space:]\"\'\\]+$ ]] || fail "webhook key contains whitespace/quotes (paste only the key)"
(( ${#key} >= 8 )) || fail "webhook key is too short (${#key} chars); probably pasted wrong"
(( ${#key} <= 1024 )) || fail "webhook key is too long (${#key} chars); probably pasted wrong"

umask 077
tmp="$(mktemp "$DIR/.env.tmp.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
{
  printf 'WECHAT_ROUTINE_WEBHOOK_URL=%s\n' "$url"
  printf 'WECHAT_ROUTINE_WEBHOOK_KEY=%s\n' "$key"
} > "$tmp"
chmod 600 "$tmp"
mv -f "$tmp" "$DIR/.env"
trap - EXIT
echo "ok: wrote $DIR/.env (chmod 600; key length ${#key})"

if (( CHECK )); then
  command -v curl >/dev/null 2>&1 || { echo "write_env: curl not found; skipped --check" >&2; exit 0; }
  # secrets go to curl via stdin config, never via argv
  code="$(printf 'url = "%s"\nheader = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\n' "$url" "$key" \
    | curl -sS -o /dev/null -w '%{http_code}' --max-time 20 -K - -X POST \
        --data '{"source":"setup-check","text":"credential check, ignore"}' 2>/dev/null || true)"
  case "$code" in
    2??) echo "check: webhook accepted the request (http $code)" ;;
    401|403|404) echo "check: webhook returned http $code: wrong key/URL or routine paused; re-request the wrong value and do not continue" >&2; exit 4 ;;
    *) echo "check: inconclusive (http ${code:-000}); network problem or rate limit, retry later" >&2; exit 5 ;;
  esac
fi
