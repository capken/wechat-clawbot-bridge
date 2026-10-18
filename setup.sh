#!/usr/bin/env bash
# One-time setup: create a private venv next to the scripts and install dependencies.
# Usage: bash setup.sh        (safe to re-run)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
if python3 -c "import requests, cryptography, qrcode, PIL" 2>/dev/null && [[ ! -d .venv ]]; then
  echo "dependencies already available in system python3; no venv needed"
else
  [[ -d .venv ]] || python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
  echo "venv ready: $DIR/.venv (start.sh / status.sh use it automatically)"
fi
mkdir -p state logs inbox; chmod 700 state inbox
echo "ok. Next: $(command -v python3) $DIR/login.py --background"
