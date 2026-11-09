"""Renew the isolated Claude OAuth access token before it expires. No inference.

The normal Claude CLI renews an expiring access token when `claude auth status`
runs against a profile. This module runs that one command for the dedicated
isolated profile only, with a minimal allowlisted environment (no inherited
CLAUDE_CONFIG_DIR, API keys, setup-tokens or environment OAuth tokens), no
shell, a timeout and discarded output. Only `expiresAt` is read from the
credential file; token values are never read into status, logs or exceptions.

Attempts are rate limited (default at least 600 seconds apart, exponential
backoff on failure) and persisted in `<state>/claude-token-refresh.json`.
The OAuth usage gate (`claude-oauth-cache.json` / `claude-oauth.lock`) is not
touched: a renewed token is simply picked up when the gate is next due.
"""
import fcntl
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

from claude_capture import atomic_json, read_json, stamp

MARGIN = 3600            # start renewing this long before expiry
MIN_INTERVAL = 600       # never run the CLI more often than this
MAX_BACKOFF = 3600       # failure backoff ceiling
CLI_WINDOW = 240         # land a retry inside the CLI's own near-expiry window
TIMEOUT = 60
STATUS_NAME = 'claude-token-refresh.json'
LOCK_NAME = 'claude-token-refresh.lock'
ENV_ALLOW = ('HOME', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'USER', 'LOGNAME', 'TMPDIR')
STATUS_KEYS = ('last_attempt_at', 'last_result', 'last_ok_at', 'last_exit_code', 'last_duration_ms',
               'consecutive_failures', 'next_allowed_at', 'expires_at_before', 'expires_at_after')


def credential_expiry(config_dir):
    """Return the isolated access token's expiresAt (epoch seconds) or a reason string."""
    config_dir = Path(config_dir)
    if config_dir.resolve() == (Path.home() / '.claude').resolve():
        return None, 'rejected_default_profile'
    credential = config_dir / '.credentials.json'
    try:
        if credential.resolve().parent != config_dir.resolve():
            return None, 'rejected_linked_credential'
        data = json.loads(credential.read_text())
    except (OSError, ValueError):
        return None, 'no_credentials'
    oauth = data.get('claudeAiOauth') if isinstance(data, dict) else None
    exp = oauth.get('expiresAt') if isinstance(oauth, dict) else None
    del data, oauth  # keep secret material out of any later frame/traceback
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or not math.isfinite(exp) or exp <= 0:
        return None, 'no_expiry'
    return exp / 1000 if exp > 1e12 else float(exp), None


def cli_path(env_path):
    found = shutil.which('claude', path=env_path)
    if found:
        return found
    fallback = Path.home() / '.local/bin/claude'
    return str(fallback) if fallback.is_file() and os.access(fallback, os.X_OK) else None


def refresh_env(config_dir):
    env = {k: os.environ[k] for k in ENV_ALLOW if os.environ.get(k)}
    env.setdefault('HOME', str(Path.home()))
    home = env['HOME']
    env['PATH'] = os.pathsep.join([home + '/.local/bin', env.get('PATH') or '/usr/local/bin:/usr/bin:/bin'])
    env['CLAUDE_CONFIG_DIR'] = str(config_dir)
    env['DISABLE_AUTOUPDATER'] = '1'
    return env


def _run(argv, env, cwd, timeout):
    """Isolated CLI call; output is discarded so account details never reach logs."""
    return subprocess.run(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=timeout, check=False,
                          start_new_session=True).returncode


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def plan(status, expires, now, margin=MARGIN):
    """Earliest time a renewal attempt is wanted and allowed."""
    allowed = _num(status.get('next_allowed_at')) or 0
    wanted = expires - margin if expires is not None else now
    return max(wanted, allowed)


def view(status, expires, reason, now, margin=MARGIN):
    """Non-secret status for the collector/health views."""
    out = {k: status.get(k) for k in STATUS_KEYS if status.get(k) is not None}
    for key in ('last_attempt_at', 'last_ok_at', 'next_allowed_at', 'expires_at_before', 'expires_at_after'):
        if _num(out.get(key)) is not None:
            out[key] = stamp(out[key])
    out.update(enabled=True, margin_seconds=margin,
               access_token_expires_at=stamp(expires) if expires is not None else None,
               access_token_expires_in_seconds=round(expires - now) if expires is not None else None,
               next_refresh_at=stamp(plan(status, expires, now, margin)) if expires is not None else None,
               credential_state=reason or ('expired' if expires is not None and expires <= now else 'valid'))
    return out


def maybe_refresh(state, config_dir, clock=time.time, log=lambda message: None, margin=MARGIN,
                  min_interval=MIN_INTERVAL, timeout=TIMEOUT, dry_run=False):
    """Renew when within `margin` of expiry (or expired) and the rate limit allows.

    Returns a non-secret status view. `dry_run` reports the plan without running the CLI.
    """
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    path = state / STATUS_NAME
    with (state / LOCK_NAME).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            now = clock()
            expires, reason = credential_expiry(config_dir)
            return dict(view(read_json(path), expires, reason, now, margin), busy=True)
        status = read_json(path)
        now = clock()
        expires, reason = credential_expiry(config_dir)
        if reason in ('rejected_default_profile', 'rejected_linked_credential'):
            return view(status, None, reason, now, margin)  # never act on another login
        due = plan(status, expires, now, margin)
        if dry_run or now < due:
            return dict(view(status, expires, reason, now, margin), would_attempt_now=now >= due)
        env = refresh_env(config_dir)
        cli = cli_path(env['PATH'])
        status.update(last_attempt_at=now, expires_at_before=expires)
        # Reserve before running so a crash mid-call still honours the interval.
        status['next_allowed_at'] = now + min_interval
        atomic_json(path, status)
        started = time.monotonic()
        code = None
        if cli is None:
            result = 'cli_missing'
        elif reason is not None:
            result = reason  # no isolated login to renew; re-login is manual
        else:
            try:
                code = _run([cli, 'auth', 'status'], env, str(config_dir), timeout)
                result = None
            except subprocess.TimeoutExpired:
                result = 'timeout'
            except OSError as error:
                result = 'exec_' + error.__class__.__name__
        after, after_reason = credential_expiry(config_dir)
        done = clock()
        if result is None:
            if after is not None and (expires is None or after > expires + 1) and after > done:
                result = 'refreshed'
            elif code != 0:
                result = f'exit_{code}'
            else:
                result = 'no_change'
        failures = int(_num(status.get('consecutive_failures')) or 0)
        if result == 'refreshed':
            failures = 0
            status['last_ok_at'] = done
            next_allowed = done + min_interval
        elif result == 'no_change' and after is not None and after > done:
            # CLI considered the token fresh enough; retry inside its own renewal window.
            next_allowed = max(done + min_interval, after - CLI_WINDOW)
        else:
            failures += 1
            next_allowed = done + min(MAX_BACKOFF, min_interval * 2 ** (failures - 1))
        status.update(last_result=result, last_exit_code=code, consecutive_failures=failures,
                      last_duration_ms=round((time.monotonic() - started) * 1000),
                      expires_at_after=after, next_allowed_at=next_allowed)
        atomic_json(path, status)
        log(f'{stamp(done)} claude_token_refresh result={result} expires_at={stamp(after) if after else after_reason} '
            f'next_allowed={stamp(next_allowed)}')
        return dict(view(status, after, after_reason, done, margin), attempted=True)


if __name__ == '__main__':
    import argparse
    from config import load_config
    parser = argparse.ArgumentParser(description='Show or run the isolated Claude token renewal (no secrets printed)')
    parser.add_argument('--run', action='store_true', help='renew now if due under the rate limit')
    args = parser.parse_args()
    cfg = load_config()
    print(json.dumps(maybe_refresh(cfg.state_dir, cfg.claude_config_dir, log=print, dry_run=not args.run), indent=1))
