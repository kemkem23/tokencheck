"""Optional push of a compact, non-secret usage snapshot to an edge dashboard. No inference.

After each poll the collector calls `EdgePusher.kick()`. Pushing happens on a
daemon thread (never blocks polling), at most once per 60 s poll (>= 55 s apart),
with a 10 s timeout and exponential backoff on failure. The snapshot holds
only percentages, pace, reset times, observation ages and a downsampled 24 h
history: no tokens, emails, account IDs, hostnames or file paths. The ingest
secret is read from `edge_push_secret_file` at push time and never logged.
"""
import json
import math
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MIN_INTERVAL = 60
TIMEOUT = 10
MAX_BACKOFF = 900
JITTER = 5          # 60 s polls drift slightly; never push more than once per poll
SETTLE = 6          # let the 5 s pace engine publish the poll's results first
HOURS = 24
STEP = 600          # 10-minute history buckets
MAX_BYTES = 200 * 1024


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _num(v, nd=2):
    return round(float(v), nd) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _ts(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, ValueError, TypeError):
        return None


def _history(samples, now, hours=HOURS, step=STEP):
    """Ordered current panels (PNG membership rules) with bucketed usage/pace arrays."""
    import plot_png
    cutoff = now - hours * 3600
    recent = [s for s in samples if isinstance(s, dict) and (t := _ts(s.get('t'))) is not None and cutoff <= t <= now + 5]
    panels = sorted(plot_png._current_panels(recent, 'all'), key=plot_png._panel_order_key)
    t0 = (int(cutoff) // step) * step
    n = (int(now) - t0) // step + 1
    out = []
    for name, label, pts in panels:
        hu, hp = [None] * n, [None] * n
        for t, used, pace, _w in pts:
            i = int((t.timestamp() - t0) // step)
            if 0 <= i < n:  # latest real value in each bucket; absent samples never erase one
                if _num(used) is not None:
                    hu[i] = _num(used)
                if _num(pace) is not None:
                    hp[i] = _num(pace)
        out.append({'p': name, 'l': label, 'hd': list(plot_png._display_header(name, label)), 'hu': hu, 'hp': hp,
                    'last': pts[-1][3] or {}})
    return {'t0': _iso(t0), 'step': step, 'n': n, 'panels': out}


def build_snapshot(state, now=None):
    """Compact snapshot from the published view plus cached 24 h history."""
    from history import history_path
    from history_cache import cache_for
    now = time.time() if now is None else now
    snap = json.loads((Path(state) / 'snapshot.json').read_text())
    view = (snap.get('results') or {}).get('all') or {}
    computed = snap.get('computed_at') or _iso(now)
    current = {}
    for provider in view.get('providers') or []:
        for w in provider.get('windows') or []:
            if isinstance(w, dict) and isinstance(w.get('label'), str):
                current[(provider.get('provider'), w['label'])] = (provider, w)
    path = history_path(state)
    hist = cache_for(path).get(path, ('edge', HOURS, STEP, int(now // STEP)), lambda samples: _history(samples, now))
    panels = []
    for item in hist['panels']:
        provider, w = current.get((item['p'], item['l']), ({}, None))
        last = item['last']
        if w is None:  # fall back to the latest history point
            w = {'used_percent': last.get('u'), 'pace_percent': last.get('p'), 'resets_at': last.get('r'),
                 'window_hours': last.get('h'), 'started': last.get('started'), 'pacing_assessment': last.get('a')}
        health = provider.get('health') or {}
        panels.append({'p': item['p'], 'l': item['l'], 'hd': item['hd'], 'u': _num(w.get('used_percent')),
                       'pc': _num(w.get('pace_percent')) if w.get('started') is not False else None,
                       'r': w.get('resets_at'), 'h': _num(w.get('window_hours'), 1), 'st': w.get('started') is not False,
                       'a': w.get('pacing_assessment'), 'age': _num(w.get('age_seconds'), 0),
                       'stale': bool(health.get('provider_stale')),
                       # Not-started rows are drawn as small rows without a chart.
                       'hu': item['hu'] if w.get('started') is not False else [],
                       'hp': item['hp'] if w.get('started') is not False else []})
    overall = view.get('overall') or {}
    return {'v': 1, 't': computed, 'pushed_at': _iso(now), 't0': hist['t0'], 'step': hist['step'], 'n': hist['n'],
            'action': overall.get('action'), 'pressure': overall.get('quota_pressure'), 'panels': panels}


def _post(url, secret, body, timeout):
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json', 'User-Agent': 'pacer-tokencheck/1'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never resend the bearer elsewhere
        return None


class EdgePusher:
    def __init__(self, url, secret_file, state, clock=time.time, log=lambda m: None, poster=_post,
                 builder=build_snapshot, min_interval=MIN_INTERVAL, timeout=TIMEOUT, settle=SETTLE):
        self.url, self.secret_file, self.state = url, Path(secret_file).expanduser(), Path(state)
        self.clock, self.log, self.poster, self.builder = clock, log, poster, builder
        self.min_interval, self.timeout, self.settle = min_interval, timeout, settle
        self.thread = None
        self.lock = threading.Lock()
        # Fixed key set: updated in place so concurrent JSON readers never see a resize.
        self.status = {'enabled': True, 'last_attempt_at': None, 'last_ok_at': None, 'last_error': None,
                       'last_http_status': None, 'last_bytes': None, 'last_ms': None,
                       'consecutive_failures': 0, 'next_allowed_at': None, 'pushes_ok': 0}
        self._next = 0.0

    @classmethod
    def from_config(cls, config, state, log=lambda m: None):
        if not getattr(config, 'edge_push_enabled', False):
            return None
        return cls(config.edge_push_url, config.edge_push_secret_file, state, log=log)

    def kick(self):
        """Non-blocking; returns True when a push thread was started."""
        with self.lock:
            now = self.clock()
            if (self.thread is not None and self.thread.is_alive()) or now < self._next - JITTER:
                return False
            self._next = now + self.min_interval
            self.thread = threading.Thread(target=self._run, name='edge-push', daemon=True)
            self.thread.start()
            return True

    def _run(self):
        if self.settle:
            time.sleep(self.settle)
        started = self.clock()
        t0 = time.monotonic()
        code, error, size = None, None, None
        try:
            body = json.dumps(self.builder(self.state, started), separators=(',', ':'), allow_nan=False,
                              ensure_ascii=False).encode()
            size = len(body)
            if size > MAX_BYTES:
                raise ValueError('snapshot_too_large')
            secret = self.secret_file.read_text().strip()
            if not secret:
                raise ValueError('secret_missing')
            code = self.poster(self.url, secret, body, self.timeout)
            del secret
            if code != 200:
                error = f'http_{code}'
        except Exception as exc:  # never crash; never include values in status
            error = exc.args[0] if isinstance(exc, ValueError) and exc.args and str(exc.args[0]).isidentifier() else exc.__class__.__name__
        done = self.clock()
        with self.lock:
            failures = 0 if error is None else self.status['consecutive_failures'] + 1
            wait = self.min_interval if error is None else min(MAX_BACKOFF, self.min_interval * 2 ** (failures - 1))
            if error:
                self._next = max(self._next, done + wait)
            self.status.update(last_attempt_at=_iso(started), last_error=error, last_http_status=code, last_bytes=size,
                               last_ms=round((time.monotonic() - t0) * 1000), consecutive_failures=failures,
                               next_allowed_at=_iso(self._next))
            if error is None:
                self.status.update(last_ok_at=_iso(done), pushes_ok=self.status['pushes_ok'] + 1)
        if error:
            self.log(f'edge_push failed error={error} failures={failures} next={_iso(self._next)}')
