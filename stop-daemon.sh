#!/usr/bin/env bash
set -euo pipefail
LIB="$(cd "$(dirname "$0")" && pwd)"
STATE="$(python3 "$LIB/config.py" --field state_dir)"
PIDF="$STATE/daemon.pid"
if [[ -f "$PIDF" ]] && kill "$(cat "$PIDF")" 2>/dev/null; then
  echo stopped
else
  echo "not running"
fi
