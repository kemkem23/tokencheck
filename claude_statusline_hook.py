#!/usr/bin/env python3
"""Transparent statusLine wrapper: capture allowlisted values, run original shell command."""
import argparse
import json
import os
import signal
import shlex
import subprocess
import sys
from pathlib import Path
from claude_capture import capture, stamp
from config import load_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pacer-wrapper-v1', action='store_true')
    parser.add_argument('--command')
    args = parser.parse_args()
    payload = sys.stdin.buffer.read()
    measured_at = stamp()  # Render/input receipt time, never delayed by lock contention.
    try:
        capture(json.loads(payload), load_config().state_dir, measured_at)
    except Exception:
        pass  # Capture errors must never affect the user's statusline.
    command = args.command
    if command is None:
        # Keep the existing direct local settings command functional until installation.
        command = shlex.quote(sys.executable) + ' ' + shlex.quote(str(Path(__file__).with_name('claude_statusline_legacy_display.py')))
    process = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE)
    try:
        process.communicate(payload)
    except BrokenPipeError:
        process.wait()
    if process.returncode < 0:
        sig = -process.returncode
        signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)
    return process.returncode


if __name__ == '__main__':
    sys.exit(main())
