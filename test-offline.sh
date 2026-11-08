#!/bin/bash
# Offline fixtures only; never inherit provider/config/auth environment values.
set -euo pipefail
SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
[[ "$SCRIPT_DIR" != "${BASH_SOURCE[0]}" ]] || SCRIPT_DIR=.
LIB="$(CDPATH= cd -- "$SCRIPT_DIR" && pwd)"
PYTHON="$LIB/venv/bin/python"
# An explicit interpreter is useful when reviewing an export without a bundled venv.
[[ $# == 0 ]] || PYTHON="$1"
TEST_ROOT="$(/usr/bin/mktemp -d)"
trap '/bin/rm -rf -- "$TEST_ROOT"' EXIT
/bin/mkdir "$TEST_ROOT/home"
printf 'enabled_providers = []\n' > "$TEST_ROOT/config.toml"
cd -- "$LIB"
/usr/bin/env -i HOME="$TEST_ROOT/home" PATH=/usr/local/bin:/usr/bin:/bin LC_ALL=C.UTF-8 \
  PACER_CONFIG="$TEST_ROOT/config.toml" PYTHONDONTWRITEBYTECODE=1 \
  MPLCONFIGDIR="$TEST_ROOT/matplotlib" "$PYTHON" -B -m unittest discover -v
