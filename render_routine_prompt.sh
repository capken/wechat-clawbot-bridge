#!/usr/bin/env bash
# Print the routine prompt, ready to paste into UpdateRoutine: strips the header above the first
# "---" line of the template and replaces <INSTALL_DIR> with this script's real absolute directory.
# Exits nonzero if any <INSTALL_DIR> or <PY> placeholder is left.
# Usage: bash render_routine_prompt.sh [path/to/routine-prompt.md]
#   Default template: ../routine-prompt.md (when run inside the skill folder's bridge/), else ./routine-prompt.md.
#   When run from an installed copy, pass the skill's template: bash render_routine_prompt.sh "<skill dir>/routine-prompt.md"
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ -n "${1:-}" ]]; then TEMPLATE="$1"
elif [[ -f "$DIR/../routine-prompt.md" ]]; then TEMPLATE="$DIR/../routine-prompt.md"
else TEMPLATE="$DIR/routine-prompt.md"; fi
[[ -f "$TEMPLATE" ]] || { echo "render: template not found: $TEMPLATE (pass its path as the first argument)" >&2; exit 2; }

body="$(awk 'found { print; next } /^---[[:space:]]*$/ { found = 1 }' "$TEMPLATE" | sed '/./,$!d')"
[[ -n "$body" ]] || { echo "render: no '---' separator line (or empty body) in $TEMPLATE" >&2; exit 2; }

out="${body//<INSTALL_DIR>/$DIR}"
if [[ "$out" == *"<INSTALL_DIR>"* || "$out" == *"<PY>"* ]]; then
  echo "render: unreplaced placeholder (<INSTALL_DIR> or <PY>) remains; fix the template" >&2; exit 1
fi
printf '%s\n' "$out"
