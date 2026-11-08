#!/usr/bin/env bash
# No provider installation, authentication, or automatic daemon start.
set -euo pipefail
LIB="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$LIB/install.py" "$@"
