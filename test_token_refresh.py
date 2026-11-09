"""Offline tests for isolated Claude access-token renewal. The CLI is always mocked."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import claude_token_refresh as ctr
import config
from providers import claude

T = 1804543200.0
MOCKVAL_A, MOCKVAL_R = 'mock-access-MOCKVAL-a1', 'mock-refresh-MOCKVAL-r1'


class RefreshTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / 'state'
        self.profile = self.root / 'profile'
        self.profile.mkdir()
        self.logs = []
        self.now = [T]
        for target in ('subprocess.run', 'subprocess.Popen'):
            blocked = patch(target, side_effect=AssertionError('real subprocess forbidden'))
            blocked.start()
            self.addCleanup(blocked.stop)
        cli = patch('claude_token_refresh.cli_path', return_value='/fake/bin/claude')
        cli.start()
        self.addCleanup(cli.stop)

    def write_cred(self, expires, access=MOCKVAL_A):
        (self.profile / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
            'accessToken': access, 'refreshToken': MOCKVAL_R, 'expiresAt': int(expires * 1000),
            'scopes': ['user:profile']}}))

    def call(self, **kw):
        return ctr.maybe_refresh(self.state, self.profile, clock=lambda: self.now[0], log=self.logs.append, **kw)

    def renewing_cli(self, new_expiry, code=0):
        calls = []
        def run(argv, env, cwd, timeout):
            calls.append((argv, env, cwd, timeout))
            if new_expiry is not None:
                self.write_cred(new_expiry, access='mock-access-MOCKVAL-a2')
            return code
        return calls, patch('claude_token_refresh._run', side_effect=run)

    def stored(self):
        return json.loads((self.state / ctr.STATUS_NAME).read_text())

    def test_outside_margin_plans_without_running(self):
        self.write_cred(T + 6 * 3600)
        calls, mock = self.renewing_cli(T + 14 * 3600)
        with mock:
            view = self.call()
        self.assertEqual(calls, [])
        self.assertFalse(view['would_attempt_now'])
        self.assertEqual(view['next_refresh_at'], ctr.stamp(T + 5 * 3600))
        self.assertEqual(view['access_token_expires_in_seconds'], 6 * 3600)
        self.assertEqual(view['credential_state'], 'valid')
        self.assertFalse((self.state / ctr.STATUS_NAME).exists())

    def test_within_margin_runs_isolated_cli_and_records_success(self):
        self.write_cred(T + 1800)
        calls, mock = self.renewing_cli(T + 8 * 3600)
        env = {'ANTHROPIC_API_KEY': 'x', 'CLAUDE_CODE_OAUTH_TOKEN': 'x', 'CLAUDE_CONFIG_DIR': '/default/login',
               'ANTHROPIC_AUTH_TOKEN': 'x', 'CLAUDE_CODE_SETUP_TOKEN': 'x', 'PACER_CLAUDE_LOGIN_CODE': 'x',
               'HOME': str(self.root), 'PATH': '/usr/bin'}
        with mock, patch.dict(os.environ, env, clear=True):
            view = self.call()
        self.assertEqual(len(calls), 1)
        argv, child_env, cwd, timeout = calls[0]
        self.assertEqual(argv, ['/fake/bin/claude', 'auth', 'status'])
        self.assertEqual(child_env['CLAUDE_CONFIG_DIR'], str(self.profile))
        for leaked in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_AUTH_TOKEN',
                       'CLAUDE_CODE_SETUP_TOKEN', 'PACER_CLAUDE_LOGIN_CODE'):
            self.assertNotIn(leaked, child_env)
        self.assertEqual(timeout, ctr.TIMEOUT)
        self.assertEqual(view['last_result'], 'refreshed')
        self.assertTrue(view['attempted'])
        self.assertEqual(view['access_token_expires_at'], ctr.stamp(T + 8 * 3600))
        self.assertEqual(view['next_refresh_at'], ctr.stamp(T + 7 * 3600))
        st = self.stored()
        self.assertEqual((st['last_result'], st['consecutive_failures'], st['next_allowed_at']), ('refreshed', 0, T + 600))

    def test_rate_limited_between_attempts(self):
        self.write_cred(T + 1800)
        calls, mock = self.renewing_cli(None, code=1)
        with mock:
            self.call()
            self.now[0] = T + 599
            view = self.call()
            self.assertEqual(len(calls), 1)
            self.assertFalse(view['would_attempt_now'])
            self.now[0] = T + 600
            self.call()
        self.assertEqual(len(calls), 2)

    def test_failure_backoff_doubles_and_caps(self):
        self.write_cred(T - 10)  # already expired
        calls, mock = self.renewing_cli(None, code=1)
        gaps = []
        with mock:
            for _ in range(5):
                self.call()
                st = self.stored()
                gaps.append(st['next_allowed_at'] - self.now[0])
                self.now[0] = st['next_allowed_at']
        self.assertEqual(gaps, [600, 1200, 2400, 3600, 3600])
        self.assertEqual(self.stored()['last_result'], 'exit_1')
        self.assertEqual(self.stored()['consecutive_failures'], 5)
        calls2, mock2 = self.renewing_cli(T + 9 * 3600)
        with mock2:
            view = self.call()
        self.assertEqual(view['last_result'], 'refreshed')
        self.assertEqual(self.stored()['consecutive_failures'], 0)

    def test_no_change_retries_inside_cli_window(self):
        self.write_cred(T + 3500)
        calls, mock = self.renewing_cli(None, code=0)
        with mock:
            view = self.call()
        self.assertEqual(view['last_result'], 'no_change')
        self.assertEqual(self.stored()['next_allowed_at'], T + 3500 - ctr.CLI_WINDOW)
        self.assertEqual(self.stored()['consecutive_failures'], 0)
        self.now[0] = T + 3260
        with mock:
            self.call()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.stored()['next_allowed_at'], T + 3260 + 600)  # interval floor still applies

    def test_timeout_and_exec_errors_are_failures(self):
        self.write_cred(T + 60)
        for error, result in ((subprocess.TimeoutExpired('claude', 60), 'timeout'),
                              (FileNotFoundError(), 'exec_FileNotFoundError')):
            with self.subTest(result=result), patch('claude_token_refresh._run', side_effect=error):
                (self.state / ctr.STATUS_NAME).unlink(missing_ok=True)
                self.assertEqual(self.call()['last_result'], result)

    def test_missing_cli_or_login_never_runs(self):
        calls, mock = self.renewing_cli(T + 3600)
        with mock:
            self.assertEqual(self.call()['last_result'], 'no_credentials')
            (self.state / ctr.STATUS_NAME).unlink()
            self.write_cred(T + 60)
            with patch('claude_token_refresh.cli_path', return_value=None):
                self.assertEqual(self.call()['last_result'], 'cli_missing')
        self.assertEqual(calls, [])

    def test_default_and_linked_profiles_are_rejected(self):
        calls, mock = self.renewing_cli(T + 3600)
        other = self.root / 'other'
        other.mkdir()
        (other / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'expiresAt': int(T * 1000)}}))
        (self.profile / '.credentials.json').symlink_to(other / '.credentials.json')
        with mock:
            self.assertEqual(self.call()['credential_state'], 'rejected_linked_credential')
            with patch('claude_token_refresh.Path.home', return_value=self.root), patch.object(Path, 'resolve', autospec=True, side_effect=lambda p, *a, **k: Path(os.path.abspath(p))):
                default = self.root / '.claude'
                default.mkdir()
                view = ctr.maybe_refresh(self.state, default, clock=lambda: T)
            self.assertEqual(view['credential_state'], 'rejected_default_profile')
        self.assertEqual(calls, [])

    def test_no_secret_reaches_status_logs_or_view(self):
        self.write_cred(T + 100)
        calls, mock = self.renewing_cli(T + 8 * 3600)
        with mock:
            view = self.call()
        blob = json.dumps(view) + (self.state / ctr.STATUS_NAME).read_text() + '\n'.join(self.logs)
        for secret in (MOCKVAL_A, MOCKVAL_R, 'mock-access-MOCKVAL-a2', 'MOCKVAL'):
            self.assertNotIn(secret, blob)
        self.assertTrue(self.logs)

    def test_dry_run_and_oauth_gate_untouched(self):
        self.write_cred(T + 60)
        self.state.mkdir()
        gate = self.state / 'claude-oauth-cache.json'
        gate.write_text('{"next_due": 1, "backoff": true}')
        calls, mock = self.renewing_cli(T + 3600)
        with mock:
            view = self.call(dry_run=True)
            self.assertTrue(view['would_attempt_now'])
            self.assertEqual(calls, [])
            self.call()
        self.assertEqual(len(calls), 1)
        self.assertEqual(gate.read_text(), '{"next_due": 1, "backoff": true}')
        self.assertFalse((self.state / 'claude-oauth.lock').exists())


class IntegrationTest(unittest.TestCase):
    def test_collect_renews_only_when_enabled_and_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch('providers.claude.claude_statusline', side_effect=claude.Attempt('x')), \
                patch('providers.claude.claude_oauth_usage', side_effect=claude.Attempt('y')), \
                patch('claude_token_refresh.maybe_refresh', return_value={'last_result': 'not_due'}) as renew:
            ctx = {'state': Path(tmp)}
            claude.claude_collect(None, ctx)
            renew.assert_not_called()
            self.assertNotIn('claude_token_refresh', ctx)
            ctx['claude_auto_refresh'] = True
            claude.claude_collect(None, ctx)
            renew.assert_called_once()
            self.assertEqual(ctx['claude_token_refresh'], {'last_result': 'not_due'})
            renew.side_effect = RuntimeError('boom')
            _, attempts = claude.claude_collect(None, ctx)
            self.assertEqual(ctx['claude_token_refresh']['last_result'], 'error_RuntimeError')
            self.assertEqual([a['method'] for a in attempts], ['claude_statusline', 'claude_oauth_shared'])

    def test_config_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'c.toml'
            path.write_text('claude_auto_refresh = false\n')
            self.assertFalse(config.load_config(path).claude_auto_refresh)
            path.write_text('')
            self.assertTrue(config.load_config(path).claude_auto_refresh)
            path.write_text('claude_auto_refresh = "yes"\n')
            with self.assertRaises(ValueError):
                config.load_config(path)


if __name__ == '__main__':
    unittest.main()
