#!/usr/bin/env python3
"""Install once on this host; preserve statusLine optional fields and original shell command."""
import argparse
import fcntl
import json
import shlex
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from claude_capture import atomic_json
from config import load_config, config_path


def backup(path):
    when = datetime.now()
    while True:
        dest = Path(str(path) + when.strftime('.bak-%Y%m%d-%H%M%S-statusline'))
        if not dest.exists():
            shutil.copy2(path, dest)
            return
        when += timedelta(seconds=1)


def install(settings):
    settings = Path(settings).expanduser()
    settings.parent.mkdir(parents=True, exist_ok=True)
    wrapper = Path(__file__).resolve().with_name('claude_statusline_hook.py')
    with settings.with_name(settings.name + '.pacer-install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(settings.read_text()) if settings.exists() else {}
        old = data.get('statusLine') or {}
        if not isinstance(old, dict) or old.get('type', 'command') != 'command':
            raise ValueError('statusLine must be a command; settings were not changed')
        original = old.get('command', ':')
        if not isinstance(original, str):
            raise ValueError('statusLine command must be a string')
        try:
            parts = shlex.split(original)
        except ValueError:
            parts = []
        cfg = load_config()
        prefix = 'env PACER_CONFIG=' + shlex.quote(str(config_path())) + ' PACER_STATE_DIR=' + shlex.quote(str(cfg.state_dir)) + ' '
        if '--pacer-wrapper-v1' in parts and str(wrapper) in parts and '--command' in parts:
            original = parts[parts.index('--command') + 1]
        # Preserve the existing direct-hook display when upgrading the old command.
        if parts == ['python3', str(wrapper)]:
            original = 'python3 ' + shlex.quote(str(wrapper.with_name('claude_statusline_legacy_display.py')))
        command = prefix + 'python3 ' + shlex.quote(str(wrapper)) + ' --pacer-wrapper-v1 --command ' + shlex.quote(original)
        if command == old.get('command'):
            return False
        data['statusLine'] = dict(old, type='command', command=command)
        if settings.exists():
            backup(settings)
        atomic_json(settings, data)
        return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', default=str(Path.home() / '.claude/settings.json'))
    args = parser.parse_args()
    print('statusLine installed' if install(args.settings) else 'statusLine already installed')
