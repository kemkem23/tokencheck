"""Allowlisted passive Claude rate-limit capture, with per-window measurement times."""
import fcntl
import json
import math
import os
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def stamp(value=None):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z') if value is not None else datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def epoch(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def read_json(path):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(data, out, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def valid_window(window):
    if not isinstance(window, dict):
        return None
    used, reset = window.get('used_percentage'), window.get('resets_at')
    reset_epoch = epoch(reset)
    if isinstance(used, (int, float)) and not isinstance(used, bool) and math.isfinite(used) and 0 <= used <= 100 and reset_epoch is not None and reset_epoch > 0:
        return {'used_percentage': used, 'resets_at': reset}
    return None


def capture(payload, state, measured_at=None, host=None):
    """Missing/null/invalid windows never replace good values or advance their clocks."""
    rl = payload.get('rate_limits') if isinstance(payload, dict) else None
    if not isinstance(rl, dict):
        return
    measured_at = measured_at or stamp()
    measured = epoch(measured_at)
    if measured is None:
        return
    valid = {}
    for key in ('five_hour', 'seven_day'):
        window = valid_window(rl.get(key))
        if window is not None:
            valid[key] = window
    if not valid:
        return
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'claude-statusline.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = state / 'claude-statusline.json'
        previous = read_json(path)
        # Reconstruct only the allowlist, even when upgrading an older cache.
        result = {'rate_limits': {}, 'window_meta': {}}
        for key in ('five_hour', 'seven_day'):
            old_limits = previous.get('rate_limits')
            old = valid_window(old_limits.get(key)) if isinstance(old_limits, dict) else None
            old_meta = previous.get('window_meta')
            meta = old_meta.get(key) if isinstance(old_meta, dict) else None
            if not isinstance(meta, dict):
                meta = {'captured_at': previous.get('captured_at'), 'host': previous.get('host')}
            old_time = epoch(meta.get('captured_at')) if old is not None else None
            if old is not None and old_time is not None:
                result['rate_limits'][key] = old
                result['window_meta'][key] = {k: meta.get(k) for k in ('captured_at', 'host')}
            if key in valid and (old_time is None or measured >= old_time):
                result['rate_limits'][key] = valid[key]
                result['window_meta'][key] = {'captured_at': measured_at, 'host': host or socket.gethostname()}
        newest = max(result['window_meta'].values(), key=lambda m: epoch(m['captured_at']))
        result.update(newest)
        atomic_json(path, result)
