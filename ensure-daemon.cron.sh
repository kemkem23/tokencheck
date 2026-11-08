#!/usr/bin/env bash
# Watchdog: run from cron / a routine every few minutes to restart after box reboot.
exec "$(cd "$(dirname "$0")" && pwd)/start-daemon.sh" >/dev/null 2>&1
