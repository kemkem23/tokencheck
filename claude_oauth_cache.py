"""One persistent OAuth budget for the local usage transport.

Only normalized usage windows are stored. The lock remains held for the entire
request; an atomic reservation is durable before any network activity begins.
"""
import fcntl
import math
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from claude_capture import atomic_json, epoch, read_json, stamp

MIN_INTERVAL = 600


def retry_due(value, now):
    try:
        seconds = int(value)
        return now + max(MIN_INTERVAL, seconds)
    except (ValueError, TypeError):
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(now + MIN_INTERVAL, when.timestamp())
        except (ValueError, TypeError, OverflowError, AttributeError):
            return now + 3600


def merge_windows(providers, now):
    """Newest observation for each window wins; passive statusline wins exact ties."""
    winners = {}
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        windows = provider.get('windows')
        if not isinstance(windows, list):
            continue
        for window in windows:
            if not isinstance(window, dict) or not isinstance(window.get('label'), str):
                continue
            used = window.get('used_percent')
            if not isinstance(used, (int, float)) or isinstance(used, bool) or not math.isfinite(used) or not 0 <= used <= 100 or epoch(window.get('resets_at')) is None:
                continue
            measured_at = window.get('measured_at') or provider.get('measured_at')
            measured = epoch(measured_at)
            if measured is None or measured > now + 5:
                continue  # Clock skew must not pin a future observation over real data.
            source = window.get('source') or provider.get('source') or 'unknown'
            # Fable is only accepted with parser provenance from scoped OAuth limits[].
            if window.get('label') == 'FABLE WEEK' and (not source.startswith('claude_oauth_') or window.get('mapping') != 'weekly_scoped_model_fable'):
                continue
            priority = (measured, int(source.startswith('claude_statusline')))
            key = window.get('label')
            if key not in winners or priority > winners[key][0]:
                winners[key] = (priority, dict(window, source=source, measured_at=measured_at,
                                              age_seconds=max(0, round(now - measured, 3))))
    return [v[1] for _, v in sorted(winners.items())]


class OAuthCache:
    def __init__(self, state, clock=time.time, interval=MIN_INTERVAL):
        self.state = Path(state)
        self.clock = clock
        self.interval = max(MIN_INTERVAL, interval)

    def get(self, select_request, parse, log=lambda message: None):
        """select_request runs only when due; returns (safe source name, request).

        request returns status, response-body, Retry-After. No fallback endpoint is
        attempted after request failure. Failure and crash reservations survive restart.
        """
        self.state.mkdir(parents=True, exist_ok=True)
        path = self.state / 'claude-oauth-cache.json'
        with (self.state / 'claude-oauth.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            now = self.clock()
            cache = read_json(path)
            valid_schedule = all(isinstance(cache.get(key, 0), (int, float)) and not isinstance(cache.get(key, 0), bool) and math.isfinite(cache.get(key, 0)) and cache.get(key, 0) >= 0 for key in ('next_due', 'last_attempt_at'))
            if path.exists() and (not cache or not valid_schedule):
                cache = {'next_due': now + 3600, 'last_error': 'cache_invalid'}
                atomic_json(path, cache)
            due = max(float(cache.get('next_due') or 0), float(cache.get('last_attempt_at') or 0) + self.interval if cache.get('last_attempt_at') is not None else 0)

            def event(kind, source=None):
                log(f'{stamp(self.clock())} claude_oauth event={kind} source={source or cache.get("source", "none")} next_due={stamp(cache.get("next_due", due))}')

            if now < due:
                event('backoff' if cache.get('backoff') else 'cooldown')
                if cache.get('windows'):
                    event('cached')
            else:
                cache.update(last_attempt_at=now, next_due=now + self.interval, backoff=False, source='selecting')
                atomic_json(path, cache)  # Commit before credentials/transport selection too.
                try:
                    source, request = select_request()
                    cache.update(source=source, last_transport=source, last_attempt_at=self.clock(), next_due=self.clock() + self.interval)
                    atomic_json(path, cache)
                    event('request', source)
                    code, body, retry_after = request()
                    received = self.clock()
                    cache['next_due'] = max(cache['next_due'], received + self.interval)
                    if code == 429:
                        cache.update(next_due=max(received + self.interval, retry_due(retry_after, received)), backoff=True, last_error='http_429')
                    elif code == 200 and isinstance(body, dict):
                        provider = parse(body, received, source)
                        cache['windows'] = merge_windows([{'windows': cache.get('windows', [])}, provider], received)
                        cache['last_success_at'] = stamp(received)
                        cache.pop('last_error', None)
                    else:
                        cache['last_error'] = 'http_' + str(int(code))
                except Exception:
                    cache['next_due'] = max(cache['next_due'], self.clock() + self.interval)
                    cache['last_error'] = 'request_or_parse_failed'
                atomic_json(path, cache)
                if cache.get('backoff'):
                    event('backoff')
                else:
                    event('cooldown')
            windows = merge_windows([{'windows': cache.get('windows', [])}], self.clock())
            if not windows:
                return None
            return {'provider': 'claude', 'plan': 'Max', 'source': 'claude_oauth_cache',
                    'measured_at': max((w['measured_at'] for w in windows), key=epoch), 'windows': windows}
