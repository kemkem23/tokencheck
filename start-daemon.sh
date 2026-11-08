#!/bin/bash
# Compatibility entrypoint for cron and MCP. Config is resolved before startup.
set -euo pipefail
SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
[[ "$SCRIPT_DIR" != "${BASH_SOURCE[0]}" ]] || SCRIPT_DIR=.
LIB="$(CDPATH= cd -- "$SCRIPT_DIR" && pwd)"
# Repair PATH before config/state helpers; HOME may be absent in an MCP host.
export PATH="${HOME:+$HOME/.local/bin:$HOME/.grok/bin:}/usr/local/bin:/usr/bin:/bin${PATH:+:$PATH}"
PYTHON="$LIB/venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON=python3
STATE="$("$PYTHON" "$LIB/config.py" --field state_dir)"
mkdir -p "$STATE"
chmod 700 "$STATE"
PIDF="$STATE/daemon.pid"
if [[ -f "$PIDF" ]] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  echo "pacer-tokencheck already running pid=$(cat "$PIDF")"
  exit 0
fi
nohup setsid "$PYTHON" "$LIB/pacer_daemon.py" >>"$STATE/daemon.stderr" 2>&1 < /dev/null &
for i in $(seq 1 20); do
  sleep 0.5
  if "$PYTHON" "$LIB/tokencheck.py" --self-check >/dev/null 2>&1; then
    echo "pacer-tokencheck started pid=$(cat "$PIDF" 2>/dev/null)"
    exit 0
  fi
done
echo "pacer-tokencheck: /health not ready (see $STATE/collector.log)" >&2
exit 1
