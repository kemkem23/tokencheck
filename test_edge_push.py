"""Offline tests for the optional edge snapshot pusher. HTTP is always mocked."""
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import collectors
import config
import daemon
import edge_push

NOW = time.time()
MOCK_SECRET = 'mock-ingest-MOCKVAL-1234567890'


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace('+00:00', 'Z')


def write_state(state, five_reset=True):
    state.mkdir(parents=True, exist_ok=True)
    reset = iso(NOW + 86400)
    rows = []
    for k in range(0, 24 * 60, 5):
        t = NOW - k * 60
        claude_w = [{'l': 'ALL WEEK', 'u': 40 + k / 1000, 'p': 50, 'started': True, 'r': reset, 'h': 168},
                    {'l': 'FABLE WEEK', 'u': 10, 'p': 50, 'started': True, 'r': reset, 'h': 168}]
        if five_reset is not None:
            claude_w.append({'l': '5H', 'u': 5, 'p': 20, 'started': True, 'r': reset if five_reset else None, 'h': 5})
        rows.append({'v': 1, 't': iso(t), 'providers': [
            {'n': 'claude', 'w': claude_w},
            {'n': 'codex', 'w': [{'l': 'WEEK', 'u': 44, 'p': 35, 'started': True, 'r': reset, 'h': 168}]},
            {'n': 'grok', 'w': [{'l': 'WEEK', 'u': 3, 'p': 71, 'started': True, 'r': reset, 'h': 168}]},
            {'n': 'agy', 'w': [{'l': 'ARGON WEEK', 'u': 0.53, 'p': 35, 'started': True, 'r': reset, 'h': 168},
                               {'l': 'GEM 5H', 'u': 0, 'p': None, 'started': False, 'r': reset, 'h': 5}]}]})
    rows.reverse()
    (state / 'history.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    view = {'overall': {'action': 'caution', 'quota_pressure': 'ahead_of_pace'}, 'providers': [
        {'provider': 'grok', 'plan': 'SuperGrok Heavy', 'health': {'provider_stale': False, 'active_source': '/home/x/secret/path'},
         'windows': [{'label': 'WEEK', 'used_percent': 3.0, 'pace_percent': 71.04, 'resets_at': reset, 'window_hours': 168,
                      'started': True, 'age_seconds': 25.0, 'pacing_assessment': 'safe', 'source': 'grok_session'}]},
        {'provider': 'codex', 'health': {'provider_stale': True},
         'windows': [{'label': 'WEEK', 'used_percent': 44.0, 'pace_percent': 35.3, 'resets_at': reset, 'window_hours': 168,
                      'started': True, 'age_seconds': 25.0, 'pacing_assessment': 'ahead_of_pace'}]}]}
    (state / 'snapshot.json').write_text(json.dumps({'computed_at': iso(NOW), 'results': {'all': view}}))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / 'state'

    def test_order_membership_and_compact_history(self):
        write_state(self.state, five_reset=False)
        snap = edge_push.build_snapshot(self.state, NOW)
        keys = [(p['p'], p['l']) for p in snap['panels']]
        self.assertEqual(keys, [('grok', 'WEEK'), ('claude', 'ALL WEEK'), ('codex', 'WEEK'), ('claude', 'FABLE WEEK'),
                                ('agy', 'ARGON WEEK'), ('agy', 'GEM 5H')])  # no Claude 5H without a real reset
        self.assertEqual(snap['panels'][1]['hd'], ['CLAUDE', 'WEEK'])
        self.assertEqual(snap['panels'][3]['hd'], ['FABLE', 'WEEK'])
        grok = snap['panels'][0]
        self.assertEqual((grok['u'], grok['pc'], grok['age']), (3.0, 71.04, 25.0))
        self.assertTrue(snap['panels'][2]['stale'])
        self.assertFalse(snap['panels'][-1]['st'])
        self.assertIsNone(snap['panels'][-1]['pc'])
        self.assertEqual(len(grok['hu']), snap['n'])
        self.assertTrue(140 <= snap['n'] <= 146)
        self.assertEqual(snap['step'], 600)
        body = json.dumps(snap)
        self.assertLess(len(body), 30000)
        for forbidden in ('/home/', 'secret', '@', 'source', 'plan', 'SuperGrok'):
            self.assertNotIn(forbidden, body)

    def test_claude_5h_with_real_reset_is_kept(self):
        write_state(self.state, five_reset=True)
        keys = [(p['p'], p['l']) for p in edge_push.build_snapshot(self.state, NOW)['panels']]
        self.assertIn(('claude', '5H'), keys)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class PusherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.secret = self.root / 'secret'
        self.secret.write_text(MOCK_SECRET + '\n')
        self.calls, self.logs = [], []
        self.clock = FakeClock()
        blocked = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        blocked.start()
        self.addCleanup(blocked.stop)

    def pusher(self, poster, **kw):
        return edge_push.EdgePusher('https://edge.example/ingest', self.secret, self.root, clock=self.clock,
                                    log=self.logs.append, poster=poster, builder=lambda s, now: {'v': 1, 't': 'x', 'panels': []},
                                    settle=0, **kw)

    def run_once(self, p):
        self.assertTrue(p.kick())
        p.thread.join(5)

    def test_success_posts_bearer_and_rate_limits(self):
        def poster(url, secret, body, timeout):
            self.calls.append((url, secret, json.loads(body), timeout))
            return 200
        p = self.pusher(poster)
        self.run_once(p)
        self.assertEqual(self.calls, [('https://edge.example/ingest', MOCK_SECRET, {'v': 1, 't': 'x', 'panels': []}, 10)])
        self.assertIsNone(p.status['last_error'])
        self.assertEqual(p.status['pushes_ok'], 1)
        self.clock.t += 54
        self.assertFalse(p.kick())
        self.clock.t += 1  # 55 s: a slightly early 60 s poll still pushes
        self.run_once(p)
        self.assertEqual(len(self.calls), 2)

    def test_kick_never_blocks_polling(self):
        gate = threading.Event()
        def poster(url, secret, body, timeout):
            gate.wait(5)
            return 200
        p = self.pusher(poster, min_interval=0)
        started = time.monotonic()
        self.assertTrue(p.kick())
        self.assertFalse(p.kick())  # still running: no second thread
        self.assertLess(time.monotonic() - started, 0.5)
        gate.set()
        p.thread.join(5)

    def test_backoff_on_failure_and_recovery(self):
        codes = [500, 500, 500, 500, 500, 500, 200]
        p = self.pusher(lambda *a: codes.pop(0))
        gaps = []
        for _ in range(6):
            self.run_once(p)
            nxt = datetime.fromisoformat(p.status['next_allowed_at'].replace('Z', '+00:00')).timestamp()
            gaps.append(round(nxt - self.clock.t))
            self.clock.t = nxt - 6
            self.assertFalse(p.kick())
            self.clock.t = nxt
        self.assertEqual(gaps, [60, 120, 240, 480, 900, 900])
        self.assertEqual(p.status['last_error'], 'http_500')
        self.assertEqual(p.status['consecutive_failures'], 6)
        self.run_once(p)
        self.assertEqual((p.status['last_error'], p.status['consecutive_failures']), (None, 0))

    def test_exceptions_and_missing_secret_are_recorded_without_values(self):
        def boom(url, secret, body, timeout):
            raise OSError('connection refused to ' + secret)
        p = self.pusher(boom)
        self.run_once(p)
        self.assertEqual(p.status['last_error'], 'OSError')
        self.secret.write_text('')
        self.clock.t += 1000
        p2 = self.pusher(lambda *a: 200)
        self.run_once(p2)
        self.assertEqual(p2.status['last_error'], 'secret_missing')
        blob = json.dumps(p.status) + json.dumps(p2.status) + '\n'.join(self.logs)
        self.assertNotIn('MOCKVAL', blob)

    def test_post_uses_no_proxy_no_redirect_and_bearer(self):
        seen = {}
        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        class Opener:
            def open(self, req, timeout):
                seen.update(url=req.full_url, auth=req.get_header('Authorization'), method=req.get_method(), timeout=timeout)
                return Resp()
        with patch('urllib.request.build_opener', return_value=Opener()) as build:
            self.assertEqual(edge_push._post('https://edge.example/ingest', MOCK_SECRET, b'{}', 10), 200)
        handlers = build.call_args.args
        self.assertTrue(any(isinstance(h, edge_push._NoRedirect) for h in handlers))
        self.assertEqual(seen, {'url': 'https://edge.example/ingest', 'auth': 'Bearer ' + MOCK_SECRET, 'method': 'POST', 'timeout': 10})


class ConfigAndCollectorTests(unittest.TestCase):
    def test_config_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'c.toml'
            path.write_text('')
            cfg = config.load_config(path)
            self.assertFalse(cfg.edge_push_enabled)
            self.assertIsNone(edge_push.EdgePusher.from_config(cfg, Path(tmp)))
            path.write_text('edge_push_enabled = true\nedge_push_url = "https://e.example/ingest"\nedge_push_secret_file = "~/s"\n')
            cfg = config.load_config(path)
            self.assertTrue(cfg.edge_push_enabled)
            self.assertTrue(cfg.edge_push_secret_file.endswith('/s') and not cfg.edge_push_secret_file.startswith('~'))
            self.assertIsNotNone(edge_push.EdgePusher.from_config(cfg, Path(tmp)))
            for bad in ('edge_push_enabled = "yes"\n', 'edge_push_enabled = true\nedge_push_url = "http://e"\nedge_push_secret_file = "s"\n',
                        'edge_push_enabled = true\nedge_push_url = "https://e"\n'):
                path.write_text(bad)
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    config.load_config(path)

    def test_collector_exposes_status_and_kicks_after_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'c.toml'
            state = Path(tmp) / 'state'
            path.write_text(f'state_dir = "{state}"\nenabled_providers = ["codex"]\n')
            cfg = config.load_config(path)
            state.mkdir()
            now = datetime.now(timezone.utc)
            provider = {'provider': 'codex', 'source': 'mock', 'measured_at': now.isoformat(), 'windows': [
                {'label': 'WEEK', 'used_percent': 1, 'window_hours': 168, 'resets_at': (now + timedelta(hours=4)).isoformat()}]}
            with patch('collectors.run_chain', return_value=(provider, [{'method': 'mock', 'ok': True}])):
                col = collectors.Collector(state, daemon, cfg)
                self.assertIsNone(col.edge)
                self.assertEqual(col.poll()['edge_push'], {'enabled': False})
                kicked = []
                col.edge = SimpleNamespace(status={'enabled': True, 'last_ok_at': None}, kick=lambda: kicked.append(1))
                status = col.poll()
            self.assertEqual(kicked, [1])
            self.assertEqual(status['edge_push']['enabled'], True)


if __name__ == '__main__':
    unittest.main()
