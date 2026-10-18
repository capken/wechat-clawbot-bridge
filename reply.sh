#!/usr/bin/env bash
# Run reply.py with the right interpreter (.venv if present, else python3).
# Usage: bash reply.sh "text" | --image PATH | --file PATH [--caption text] | --dry-run ...
# Works from any current directory; all args are passed through unchanged.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$DIR/.venv/bin/python3" ]]; then PY="$DIR/.venv/bin/python3"; else PY="python3"; fi
exec "$PY" "$DIR/reply.py" "$@"
