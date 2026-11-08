#!/bin/bash
# MCP stdio entrypoint. Ensures the daemon is up (quietly, stderr only), then execs the server.
# Resolve with Bash builtins: MCP hosts may supply a minimal PATH or CDPATH.
SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
[[ "$SCRIPT_DIR" != "${BASH_SOURCE[0]}" ]] || SCRIPT_DIR=.
LIB="$(CDPATH= cd -- "$SCRIPT_DIR" && pwd)" || exit 1
"$LIB/start-daemon.sh" >&2 || true
exec "$LIB/venv/bin/python" "$LIB/mcp_server.py"
