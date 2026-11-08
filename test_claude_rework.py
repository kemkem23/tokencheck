"""Offline regression suite. Every credential lookup/SSH/network path is blocked or mocked."""
import contextlib
import io
import json
import multiprocessing
import os
import shlex
import socket
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from unittest.mock import patch

import claude_capture as capture
import claude_oauth_cache as oauth
import collectors
import core
import daemon
import dashboard
import history
import install_claude_statusline as installer
from providers import claude, codex, grok, agy, common

SUBPROCESS_RUNNER = common.run
PROFILE_TOKEN_READER = claude._claude_profile_token
ROOT = Path(__file__).resolve().parent
T = 1804543200.0
RESET = T + 167 * 3600


def dt(t=T):
    return datetime.fromtimestamp(t, timezone.utc)


def usage(used=43, fable=True):
    limits = [{'kind': 'weekly_all', 'percent': used, 'resets_at': RESET}]
    if fable:
        limits.append({'kind': 'weekly_scoped', 'scope': {'model': {'display_name': 'Fable'}},
                       'percent': 12, 'resets_at': RESET, 'is_active': False})
    return {'five_hour': None, 'limits': limits}


def parse(body, received, source):
    return dict(claude.claude_from_rate_limits(body, dt(received), source), measured_at=capture.stamp(received))


def provider(used, measured, source='claude_oauth_usage', label='ALL WEEK'):
    window = {'label': label, 'used_percent': used, 'resets_at': capture.stamp(RESET), 'window_hours': 168}
    if label == 'FABLE WEEK':
        window['mapping'] = 'weekly_scoped_model_fable'
    return {'source': source, 'measured_at': capture.stamp(measured), 'windows': [window]}


def concurrent_worker(state, gate, attempts):
    gate.wait(5)
    def request():
        with open(attempts, 'a') as out:
            out.write(str(T) + '\n')
        return 200, usage(), None
    oauth.OAuthCache(state, clock=lambda: T).get(lambda: ('claude_oauth_legacy_host', request), parse)


def capture_worker(state, gate, offset):
    gate.wait(5)
    capture.capture({'rate_limits':{'seven_day':{'used_percentage':offset,'resets_at':RESET}}}, state, capture.stamp(T+offset), 'test-host')




class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        # An accidental live path fails the test before reading any actual credentials.
        for target in ('providers.claude._claude_profile_token', 'providers.common.run',
                       'urllib.request.urlopen', 'urllib.request.OpenerDirector.open', 'socket.create_connection'):
            mocked = patch(target, side_effect=AssertionError('live path forbidden in offline test'))
            mocked.start()
            self.addCleanup(mocked.stop)


class IsolatedLoginTests(OfflineTest):
    def test_only_dedicated_or_configured_credentials_are_read(self):
        for configured in (None,'/isolated/test-login'):
            with self.subTest(configured=configured):
                env={'HOME':str(claude.HOME),'CLAUDE_CODE_OAUTH_TOKEN':'ignored-env-token','CLAUDE_CONFIG_DIR':'/ignored-config'}
                if configured is not None:
                    env['PACER_CLAUDE_CONFIG_DIR']=configured
                expected=Path(configured) if configured else claude.HOME/'.local/share/pacer-tokencheck/claude-usage-config'
                reads=[]
                def read(path,*args,**kwargs):
                    reads.append(path)
                    return json.dumps({'claudeAiOauth':{'accessToken':'mock-isolated-token','scopes':['user:profile'],'expiresAt':(T+3600)*1000}})
                with patch.dict(os.environ,env,clear=True), patch('pathlib.Path.read_text',autospec=True,side_effect=read), patch('providers.claude.time.time',return_value=T):
                    self.assertEqual(PROFILE_TOKEN_READER(),'mock-isolated-token')
                self.assertEqual(reads,[expected/'.credentials.json'])

    def test_no_default_login_or_env_fallback_for_invalid_isolated_login(self):
        cases=[OSError('missing'),'{invalid','[]',json.dumps({'claudeAiOauth':{'accessToken':'mock','scopes':[]}}),json.dumps({'claudeAiOauth':{'accessToken':'mock','scopes':['user:profile'],'expiresAt':(T-1)*1000}})]
        for value in cases:
            with self.subTest(value=value), patch.dict(os.environ,{'PACER_CLAUDE_CONFIG_DIR':'/isolated/test-login','CLAUDE_CODE_OAUTH_TOKEN':'ignored-env-token'}), patch('pathlib.Path.read_text',autospec=True) as read, patch('providers.claude.time.time',return_value=T):
                if isinstance(value,Exception): read.side_effect=value
                else: read.return_value=value
                self.assertIsNone(PROFILE_TOKEN_READER())
                self.assertEqual(read.call_args.args[0],Path('/isolated/test-login/.credentials.json'))
                self.assertEqual(read.call_count,1)
        with patch.dict(os.environ,{'PACER_CLAUDE_CONFIG_DIR':str(claude.HOME/'.claude')}), patch('pathlib.Path.read_text') as read:
            with self.assertRaises(ValueError): PROFILE_TOKEN_READER()
            read.assert_not_called()


class CaptureTests(OfflineTest):
    def payload(self, five=8, seven=42):
        return {'rate_limits': {'five_hour': {'used_percentage': five, 'resets_at': T+18000},
                                'seven_day': {'used_percentage': seven, 'resets_at': RESET},
                                'seven_day_fable': {'used_percentage': 99, 'resets_at': RESET}},
                'unrelated': 'must-not-be-captured'}

    def test_allowlist_null_missing_and_per_window_clocks(self):
        capture.capture(self.payload(), self.state, capture.stamp(T), 'test-host')
        capture.capture(self.payload(five=None, seven=43), self.state, capture.stamp(T+60), 'second-host')
        cache = capture.read_json(self.state/'claude-statusline.json')
        self.assertEqual(set(cache), {'captured_at', 'host', 'rate_limits', 'window_meta'})
        self.assertEqual(set(cache['rate_limits']), {'five_hour', 'seven_day'})
        self.assertEqual(cache['rate_limits']['five_hour']['used_percentage'], 8)
        self.assertEqual(cache['window_meta']['five_hour']['captured_at'], capture.stamp(T))
        self.assertEqual(cache['window_meta']['seven_day']['captured_at'], capture.stamp(T+60))
        before = (self.state/'claude-statusline.json').read_bytes()
        capture.capture({'rate_limits': {'five_hour': None}}, self.state, capture.stamp(T+120))
        self.assertEqual(before, (self.state/'claude-statusline.json').read_bytes())

    def test_delayed_old_session_cannot_clobber_newer_capture(self):
        capture.capture(self.payload(seven=43), self.state, capture.stamp(T+60))
        capture.capture(self.payload(seven=42), self.state, capture.stamp(T))
        self.assertEqual(capture.read_json(self.state/'claude-statusline.json')['rate_limits']['seven_day']['used_percentage'], 43)

    def test_concurrent_sessions_keep_newest_capture(self):
        ctx=multiprocessing.get_context('fork')
        gate=ctx.Event()
        procs=[ctx.Process(target=capture_worker,args=(str(self.state),gate,offset)) for offset in (50,10,70,30,60,20)]
        for proc in procs:proc.start()
        gate.set()
        for proc in procs:
            proc.join(10)
            self.assertEqual(proc.exitcode,0)
        cache=capture.read_json(self.state/'claude-statusline.json')
        self.assertEqual(cache['rate_limits']['seven_day']['used_percentage'],70)
        self.assertEqual(cache['captured_at'],capture.stamp(T+70))

    def test_malformed_previous_cache_recovers_from_real_render(self):
        capture.atomic_json(self.state/'claude-statusline.json',{'rate_limits':[], 'window_meta':'invalid'})
        capture.capture(self.payload(),self.state,capture.stamp(T))
        self.assertEqual(capture.read_json(self.state/'claude-statusline.json')['rate_limits']['seven_day']['used_percentage'],42)

    def test_invalid_fields_and_real_zero(self):
        for value in (None, True, -1, 101, float('nan'), float('inf'), '42'):
            capture.capture(self.payload(value, value), self.state, capture.stamp(T))
        self.assertFalse((self.state/'claude-statusline.json').exists())
        capture.capture(self.payload(0, 0), self.state, capture.stamp(T))
        cache = capture.read_json(self.state/'claude-statusline.json')
        self.assertEqual(cache['rate_limits']['five_hour']['used_percentage'], 0)
        capture.capture({'rate_limits': {'five_hour': {'used_percentage': 88, 'resets_at': None}}}, self.state)
        self.assertEqual(capture.read_json(self.state/'claude-statusline.json'), cache)

    def test_wrapper_preserves_binary_stdin_stdout_stderr_and_exit(self):
        command_file = self.state/'display.py'
        command_file.write_text('import sys\nb=sys.stdin.buffer.read()\nsys.stdout.buffer.write(b"out:"+b)\nsys.stderr.buffer.write(b"err:exact\\n")\nsys.exit(7)\n')
        command = 'python3 ' + shlex.quote(str(command_file))
        payload = json.dumps(self.payload()).encode()+b'\n'
        env = dict(os.environ, PACER_STATE_DIR=str(self.state))
        original = subprocess.run(command, shell=True, input=payload, capture_output=True, env=env)
        wrapped = subprocess.run(['python3', str(ROOT/'claude_statusline_hook.py'), '--pacer-wrapper-v1', '--command', command], input=payload, capture_output=True, env=env)
        self.assertEqual((wrapped.returncode, wrapped.stdout, wrapped.stderr), (original.returncode, original.stdout, original.stderr))
        self.assertTrue((self.state/'claude-statusline.json').exists())
        invalid = subprocess.run(['python3', str(ROOT/'claude_statusline_hook.py'), '--command', command], input=b'\xff\x00not-json', capture_output=True, env=env)
        self.assertEqual(invalid.stdout, b'out:\xff\x00not-json')
        self.assertEqual(invalid.returncode, 7)

    def test_direct_existing_local_settings_command_preserves_display(self):
        result = subprocess.run(['python3', str(ROOT/'claude_statusline_hook.py')], input=b'{"model":{"display_name":"Claude"},"rate_limits":{"seven_day":{"used_percentage":42}}}', capture_output=True, env=dict(os.environ, PACER_STATE_DIR=str(self.state)))
        self.assertEqual((result.stdout,result.stderr,result.returncode),('Claude · wk 42%\n'.encode(),b'',0))

    def test_wrapper_capture_failure_is_silent(self):
        blocked = self.state/'not-a-directory'
        blocked.write_text('x')
        env = dict(os.environ, PACER_STATE_DIR=str(blocked))
        wrapped = subprocess.run(['python3', str(ROOT/'claude_statusline_hook.py'), '--command', 'printf visible; printf error >&2; exit 3'], input=json.dumps(self.payload()).encode(), capture_output=True, env=env)
        self.assertEqual((wrapped.stdout, wrapped.stderr, wrapped.returncode), (b'visible', b'error', 3))

    def test_installer_preserves_original_command_fields_and_idempotence(self):
        settings = self.state/'settings.json'
        old = {'statusLine': {'type': 'command', 'command': 'bash /example/user/.claude/statusline-command.sh', 'padding': 0, 'refreshInterval': 123}, 'unrelated': {'x': 3}}
        settings.write_text(json.dumps(old))
        self.assertTrue(installer.install(settings))
        new = capture.read_json(settings)
        self.assertEqual(new['unrelated'], old['unrelated'])
        self.assertEqual(new['statusLine']['padding'], 0)
        self.assertEqual(new['statusLine']['refreshInterval'], 123)
        self.assertEqual(shlex.split(new['statusLine']['command'])[-1], old['statusLine']['command'])
        backups = list(self.state.glob('settings.json.bak-*-statusline'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), old)
        before = settings.read_bytes()
        self.assertFalse(installer.install(settings))
        self.assertEqual(settings.read_bytes(), before)
        self.assertEqual(list(self.state.glob('settings.json.bak-*-statusline')), backups)

    def test_installer_local_upgrade_avoids_recursion_and_preserves_output(self):
        settings = self.state/'settings.json'
        settings.write_text(json.dumps({'statusLine': {'command': 'python3 '+str(ROOT/'claude_statusline_hook.py'), 'type': 'command'}}))
        installer.install(settings)
        command = shlex.split(capture.read_json(settings)['statusLine']['command'])[-1]
        self.assertIn('claude_statusline_legacy_display.py', command)
        result = subprocess.run(command, shell=True, input=b'{"model":{"display_name":"Claude"},"rate_limits":{"seven_day":{"used_percentage":42}}}', capture_output=True)
        self.assertEqual(result.stdout, 'Claude · wk 42%\n'.encode())


class ArbitrationTests(OfflineTest):
    def test_newest_wins_both_directions_and_passive_tie(self):
        passive = provider(42, T, 'claude_statusline')
        newer = provider(43, T+60)
        self.assertEqual(oauth.merge_windows([passive, newer], T+90)[0]['used_percent'], 43)
        passive = provider(44, T+120, 'claude_statusline')
        self.assertEqual(oauth.merge_windows([passive, newer], T+130)[0]['used_percent'], 44)
        tied = provider(99, T+120)
        for ordering in ([passive, tied], [tied, passive]):
            self.assertEqual(oauth.merge_windows(ordering, T+130)[0]['used_percent'], 44)

    def test_local_oauth_merge_per_window_and_future_clock_rejection(self):
        a = provider(42, T, 'claude_statusline')
        a['windows'].append(dict(provider(10, T-50, label='FABLE WEEK')['windows'][0], source='claude_oauth_legacy_host', measured_at=capture.stamp(T-50)))
        b = provider(43, T+60, 'claude_oauth_legacy_host')
        merged = oauth.merge_windows([a, b, provider(90, T+10000)], T+120)
        by = {w['label']:w for w in merged}
        self.assertEqual(by['ALL WEEK']['used_percent'], 43)
        self.assertEqual(by['ALL WEEK']['age_seconds'], 60)
        self.assertEqual(by['FABLE WEEK']['age_seconds'], 170)

    def test_mapping_fable_strict_no_synthetic_5h(self):
        p = claude.claude_from_rate_limits(usage(), dt(), 'claude_oauth_usage')
        self.assertEqual({w['label'] for w in p['windows']}, {'ALL WEEK', 'FABLE WEEK'})
        p = claude.claude_from_rate_limits({'seven_day_fable': {'utilization': 99, 'resets_at': RESET}, 'seven_day': {'utilization': 42, 'resets_at': RESET}, 'five_hour': None}, dt(), 'claude_oauth_usage')
        self.assertEqual([w['label'] for w in p['windows']], ['ALL WEEK'])
        forged = {'kind': 'something_else', 'group': 'weekly', 'percent': 99, 'resets_at': RESET, 'scope': {'model': {'display_name': 'Fable'}}}
        with self.assertRaises(collectors.Attempt):
            claude.claude_from_rate_limits({'limits': [forged]}, dt(), 'claude_oauth_usage')
        self.assertEqual(oauth.merge_windows([provider(10,T,'claude_statusline', 'FABLE WEEK')],T), [])

    def test_statusline_epoch_reset_and_microsecond_order(self):
        raw = {'captured_at': capture.stamp(T+.0001), 'host': 'remote', 'rate_limits': {'seven_day': {'used_percentage': 42, 'resets_at': RESET}, 'five_hour': {'used_percentage': None, 'resets_at': T+18000}}}
        p = claude.claude_statusline_data(raw, dt(T+1), 'claude_statusline')
        self.assertEqual(len(p['windows']), 1)
        merged = oauth.merge_windows([p, provider(43, T+.0002)], T+1)
        self.assertEqual(merged[0]['used_percent'], 43)

    def test_dynamic_oauth_buckets_and_legacy_fallback(self):
        body = usage()
        body['limits'].append({'kind':'daily_extra','group':'daily','percent':2,'resets_at':T+86400})
        body['five_hour'] = {'utilization': 7, 'resets_at': T+18000}
        p = claude.claude_from_rate_limits(body, dt(), 'claude_oauth_usage')
        by = {w['label']: w for w in p['windows']}
        self.assertEqual(by['DAILY EXTRA']['window_hours'], 24)
        self.assertEqual(by['5H']['used_percent'], 7)

    def test_core_view_recomputes_independent_ages_without_changing_measurements(self):
        windows = oauth.merge_windows([provider(43,T), provider(12,T-3600,label='FABLE WEEK')], T+60)
        p = {'provider':'claude', 'source':'claude_merged','plan':'Max','measured_at':capture.stamp(T),'windows': windows}
        view, _ = core.analyze_snapshot({'providers':[p]}, 'claude', dt(T+120))
        by = {w['label']:w for w in view['providers'][0]['windows']}
        self.assertEqual(by['ALL WEEK']['age_seconds'], 120)
        self.assertEqual(by['FABLE WEEK']['age_seconds'], 3720)
        self.assertEqual(by['FABLE WEEK']['source'], 'claude_oauth_usage')


class ThrottleTests(OfflineTest):
    def test_restart_failover_and_errors_share_600_second_budget(self):
        clock = [T]
        calls = []
        def select(source, status=200):
            def request():
                calls.append((clock[0],source))
                return status, usage(), None
            return lambda:(source, request)
        def get(selector):
            return oauth.OAuthCache(self.state, lambda:clock[0]).get(selector,parse)
        first = get(select('claude_oauth_usage', 500))
        self.assertIsNone(first)
        for offset in (1,60,120,599):
            clock[0]=T+offset
            get(select('claude_oauth_legacy_host'))
        self.assertEqual(len(calls),1)
        clock[0]=T+600
        result=get(select('claude_oauth_legacy_host'))
        self.assertEqual(len(calls),2)
        self.assertGreaterEqual(calls[1][0]-calls[0][0],600)
        original=[w['measured_at'] for w in result['windows']]
        clock[0]=T+900
        cached=get(select('claude_oauth_usage'))
        self.assertEqual([w['measured_at'] for w in cached['windows']],original)
        self.assertEqual(cached['windows'][0]['age_seconds'],300)

    def test_slow_requests_wait_600_seconds_after_completion(self):
        clock=[T]; calls=[]
        def request():
            calls.append(clock[0]);clock[0]+=30
            return 500,None,None
        select=lambda:('claude_oauth_usage',request)
        oauth.OAuthCache(self.state,lambda:clock[0]).get(select,parse)
        clock[0]=T+629
        oauth.OAuthCache(self.state,lambda:clock[0]).get(select,parse)
        self.assertEqual(calls,[T])
        clock[0]=T+630
        oauth.OAuthCache(self.state,lambda:clock[0]).get(select,parse)
        self.assertEqual(calls,[T,T+630])

    def test_429_retry_after_seconds_http_date_and_default(self):
        for header, delay in [('1200',1200), (formatdate(T+1800,usegmt=True),1800), (None,3600), ('bad',3600), ('20',600)]:
            with self.subTest(header=header), tempfile.TemporaryDirectory() as state:
                clock=[T]; calls=[]; logs=[]
                def request():
                    calls.append(clock[0]);return 429,None,header
                select=lambda:('claude_oauth_usage',request)
                cache=lambda:oauth.OAuthCache(state,lambda:clock[0])
                cache().get(select,parse,logs.append)
                clock[0]=T+delay-1
                cache().get(select,parse,logs.append)
                self.assertEqual(calls,[T])
                clock[0]=T+delay
                cache().get(select,parse,logs.append)
                self.assertEqual(calls,[T,T+delay])
                self.assertTrue(all('source=' in entry and 'next_due=' in entry and 'Z ' in entry for entry in logs))
                self.assertTrue(any('event=backoff' in entry for entry in logs))

    def test_last_good_fable_retained_after_omission_429_and_parse_failure(self):
        clock=[T]
        def get(code=200,body=None):
            return oauth.OAuthCache(self.state,lambda:clock[0]).get(lambda:('claude_oauth_usage',lambda:(code,body,None)),parse)
        get(body=usage())
        clock[0]=T+600
        newer=get(body=usage(44,False))
        fable=next(w for w in newer['windows'] if w['label']=='FABLE WEEK')
        self.assertEqual(fable['measured_at'],capture.stamp(T))
        self.assertEqual(fable['age_seconds'],600)
        clock[0]=T+1200
        limited=get(429)
        self.assertEqual(next(w for w in limited['windows'] if w['label']=='FABLE WEEK')['age_seconds'],1200)
        clock[0]=T+4800
        failed=get(body={})
        self.assertEqual(next(w for w in failed['windows'] if w['label']=='FABLE WEEK')['measured_at'],capture.stamp(T))

    def test_concurrent_processes_make_one_request_and_persist_budget(self):
        ctx=multiprocessing.get_context('fork')
        gate=ctx.Event(); attempts=str(self.state/'attempts')
        procs=[ctx.Process(target=concurrent_worker,args=(str(self.state),gate,attempts)) for _ in range(6)]
        for proc in procs: proc.start()
        gate.set()
        for proc in procs:
            proc.join(10)
            self.assertEqual(proc.exitcode,0)
        self.assertEqual(Path(attempts).read_text().splitlines(),[str(T)])
        cache=capture.read_json(self.state/'claude-oauth-cache.json')
        self.assertEqual(cache['next_due'],T+600)

    def test_crash_reservation_suppresses_second_endpoint(self):
        capture.atomic_json(self.state/'claude-oauth-cache.json',{'last_attempt_at':T,'next_due':T+600,'source':'claude_oauth_usage'})
        selector=lambda: self.fail('reserved attempt must prevent source selection')
        self.assertIsNone(oauth.OAuthCache(self.state,lambda:T+599).get(selector,parse))

    def test_corrupt_cache_fails_closed(self):
        (self.state/'claude-oauth-cache.json').write_text('truncated')
        selector=lambda:self.fail('corrupt cache must back off')
        oauth.OAuthCache(self.state,lambda:T).get(selector,parse)
        self.assertEqual(capture.read_json(self.state/'claude-oauth-cache.json')['next_due'],T+3600)




class IntegrationTests(OfflineTest):
    def test_local_failures_never_select_another_transport(self):
        clock=[T]
        cache_class=oauth.OAuthCache
        with patch('providers.claude.OAuthCache',side_effect=lambda state, **kwargs:cache_class(state,lambda:clock[0], **kwargs)), patch('providers.claude._claude_profile_token',return_value='mock-token'), patch('providers.claude._claude_oauth_local_request',return_value=(500,None,None)) as local, patch('collectors.log'):
            for offset in (0,60,600,1200):
                clock[0]=T+offset
                with self.assertRaises(collectors.Attempt):
                    claude.claude_oauth_usage(dt(clock[0]),{'state':self.state})
            self.assertEqual(local.call_count,3)
        for name in ('SSH_HOST','REMOTE_AGY','REMOTE_NEEDS','remote_batch','_claude_oauth_remote_request','claude_oauth_legacy_host','agy_remote'):
            self.assertFalse(hasattr(collectors,name),name)
        self.assertEqual([method for method,_ in collectors.CHAINS['agy']],['agy_usage'])

    def test_oauth_transport_rejects_redirects_and_inherited_proxies(self):
        from selfcheck import NoRedirect
        from unittest.mock import MagicMock
        import urllib.request
        opener=MagicMock()
        response=opener.open.return_value.__enter__.return_value
        response.status=200
        response.read.return_value=b'{}'
        response.headers.get.return_value=None
        with patch.dict(os.environ,{'https_proxy':'http://proxy.invalid'}), patch('urllib.request.build_opener',return_value=opener) as build:
            self.assertEqual(claude._claude_oauth_local_request('mock-token'),(200,{},None))
        handlers=build.call_args.args
        self.assertEqual(handlers[0].proxies,{})
        self.assertIsInstance(handlers[1],NoRedirect)
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'https://api.anthropic.com/api/oauth/usage')
        self.assertIsNone(handlers[1].redirect_request(request,None,302,'redirect',{},'https://untrusted.invalid'))
        opener.open.assert_called_once()

    def test_retry_after_metadata_survives_local_transport(self):
        import urllib.error
        error=urllib.error.HTTPError('https://example.invalid/usage',429,'limited',{'Retry-After':'1200'},None)
        with patch('urllib.request.OpenerDirector.open',side_effect=error):
            code,body,retry=claude._claude_oauth_local_request('fake-test-value')
        self.assertEqual((code,body,retry),(429,None,'1200'))

    def test_legacy_schedule_preserved_until_due_then_local_only(self):
        clock=[T]
        cache_class=oauth.OAuthCache
        path=self.state/'claude-oauth-cache.json'
        capture.atomic_json(path,{'last_transport':'claude_oauth_legacy_host','last_error':'http_429','backoff':True,'last_attempt_at':T-100,'next_due':T+900,'marker':'preserve'})
        before=path.read_bytes()
        with patch('providers.claude.OAuthCache',side_effect=lambda state, **kwargs:cache_class(state,lambda:clock[0], **kwargs)), patch('providers.claude._claude_profile_token',return_value='mock-token') as credentials, patch('providers.claude._claude_oauth_local_request',return_value=(200,usage(),None)) as local, patch('collectors.log'):
            for offset in (0,600,899):
                clock[0]=T+offset
                with self.assertRaises(collectors.Attempt):
                    claude.claude_oauth_usage(dt(clock[0]),{'state':self.state})
                self.assertEqual(path.read_bytes(),before)
            credentials.assert_not_called()
            clock[0]=T+900
            claude.claude_oauth_usage(dt(clock[0]),{'state':self.state})
            local.assert_called_once_with('mock-token')

    def test_local_429_retries_only_after_backoff(self):
        clock=[T]
        cache_class=oauth.OAuthCache
        with patch('providers.claude.OAuthCache',side_effect=lambda state, **kwargs:cache_class(state,lambda:clock[0], **kwargs)), patch('providers.claude._claude_profile_token',return_value='mock-token'), patch('providers.claude._claude_oauth_local_request',return_value=(429,None,'1200')) as local, patch('collectors.log'):
            for offset in (0,60,600,1199,1200):
                clock[0]=T+offset
                with self.assertRaises(collectors.Attempt):
                    claude.claude_oauth_usage(dt(clock[0]),{'state':self.state})
                self.assertEqual(local.call_count,2 if offset==1200 else 1)
            self.assertEqual(capture.read_json(self.state/'claude-oauth-cache.json')['next_due'],T+2400)

    def test_missing_or_expired_login_never_calls_usage_transport(self):
        clock=[T]
        cache_class=oauth.OAuthCache
        with patch('providers.claude.OAuthCache',side_effect=lambda state, **kwargs:cache_class(state,lambda:clock[0], **kwargs)), patch('providers.claude._claude_profile_token',return_value=None), patch('providers.claude._claude_oauth_local_request') as local, patch('collectors.log'):
            for offset in (0,599,600,1200):
                clock[0]=T+offset
                with self.assertRaises(collectors.Attempt):
                    claude.claude_oauth_usage(dt(clock[0]),{'state':self.state})
            local.assert_not_called()

    def test_three_real_collector_polls_no_ssh_and_failed_sources_keep_clocks(self):
        clock=[T]
        cache_class=oauth.OAuthCache
        legacy=parse(usage(),T-3600,'claude_oauth_legacy_host')
        capture.atomic_json(self.state/'claude-oauth-cache.json',{'last_transport':'claude_oauth_legacy_host','last_attempt_at':T-600,'next_due':T,'windows':oauth.merge_windows([legacy],T)})
        codex=dict(provider(8,T-300),provider='codex')
        codex['windows'][0].update(label='WEEK',measured_at=capture.stamp(T-500))
        capture.atomic_json(self.state/'sources.json',{'raw':{'providers':[codex],'sources':{}}})
        grok_data={'plan':'test','used_percent':9,'reset_at':capture.stamp(RESET),'source':'grok_session','measured_at':capture.stamp(T-700)}
        capture.atomic_json(self.state/'grok-usage.json',{'data':grok_data})
        commands=[]
        def subprocess_run(cmd,**kwargs):
            commands.append(cmd)
            self.assertNotIn('ssh',' '.join(cmd).lower())
            self.assertNotIn('ccburn',' '.join(cmd).lower())
            if Path(cmd[0]).name=='agy' and clock[0]==T:
                buckets=[dict(id=identity,window=win,remaining_fraction=1,reset_time=capture.stamp(T+hours*3600)) for identity,win,label,hours in core.AGY_BUCKETS]
                body={'status':'SUCCESS','command':{'name':'usage','data':{'groups':[{'buckets':buckets}]}}}
                return subprocess.CompletedProcess(cmd,0,json.dumps(body),'')
            return subprocess.CompletedProcess(cmd,1,'','offline')
        def popen(cmd,**kwargs):
            commands.append(cmd)
            self.assertNotIn('ssh',' '.join(cmd).lower())
            raise OSError('mock local failure')
        def which(name,*args):
            return '/mock/'+name if name in ('codex','tokscale','agy') else None
        with patch('providers.common.utcnow',side_effect=lambda:dt(clock[0])), patch('providers.claude.OAuthCache',side_effect=lambda state, **kwargs:cache_class(state,lambda:clock[0], **kwargs)), patch('providers.claude._claude_profile_token',return_value='mock-token'), patch('providers.claude._claude_oauth_local_request',side_effect=[(200,usage(fable=False),None),(500,None,None)]) as local, patch('providers.common.run',new=SUBPROCESS_RUNNER), patch('subprocess.run',side_effect=subprocess_run), patch('subprocess.Popen',side_effect=popen), patch('providers.common.which',side_effect=which), patch('providers.grok.shutil.which',return_value=None), patch('collectors.log') as logs:
            collector=collectors.Collector(self.state,daemon,config=__import__('dataclasses').replace(collectors.load_config(), enabled_providers=('grok','claude','codex','agy')))
            for offset in (0,60,600):
                clock[0]=T+offset
                if offset==600: collector=collectors.Collector(self.state,daemon,config=__import__('dataclasses').replace(collectors.load_config(), enabled_providers=('grok','claude','codex','agy')))
                status=collector.poll()
                self.assertEqual((status['remote_collection'],status['ssh_calls']),('disabled',0))
                raw=capture.read_json(self.state/'sources.json')['raw']
                by={p['provider']:p for p in raw['providers']}
                self.assertEqual(capture.read_json(self.state/'grok-usage.json')['data'],grok_data)
                agy=by['agy']
                if offset==0: original_agy_stamp=agy['measured_at']
                self.assertEqual(agy['measured_at'],original_agy_stamp)
                self.assertEqual(capture.epoch(agy['measured_at']),T)
                self.assertEqual({w['age_seconds'] for w in agy['windows']},{offset})
                self.assertEqual(agy['stale'],bool(offset))
                self.assertEqual(by['codex']['windows'][0]['age_seconds'],offset+500)
                for window in agy['windows']:
                    view,error=core.analyze_window(window,dt(clock[0]),'agy',agy['measured_at'])
                    self.assertIsNone(error)
                    self.assertEqual(view['age_seconds'],offset)
                    self.assertFalse(view['started'])
                fable=next(w for w in by['claude']['windows'] if w['label']=='FABLE WEEK')
                self.assertEqual(fable['measured_at'],capture.stamp(T-3600))
                self.assertEqual(fable['age_seconds'],3600+offset)
            self.assertEqual(local.call_count,2)
            poll_logs=[c.args[0] for c in logs.call_args_list if c.args[0].startswith('poll gen=')]
            self.assertEqual(len(poll_logs),3)
            self.assertTrue(all('remote_collection=disabled ssh_calls=0' in line for line in poll_logs))
        self.assertTrue(commands)
        # Assert outside collector exception handlers as well as at subprocess entry.
        for cmd in commands:
            self.assertNotIn('ssh',' '.join(cmd).lower())
            self.assertNotIn('ccburn',' '.join(cmd).lower())
        self.assertTrue(any(Path(cmd[0]).name=='agy' for cmd in commands))

    def test_active_runtime_has_no_remote_collection_paths(self):
        import re
        files=[*ROOT.glob('*.py'),*ROOT.glob('*.sh'),*ROOT.glob('providers/*.py')]
        for path in files:
            if path.name.startswith('test_'): continue
            with self.subTest(path=path.name):
                source=path.read_text()
                self.assertIsNone(re.search(r'(?:PACER_REMOTE_|def remote_batch|def agy_remote|def _claude_oauth_remote_request|BatchMode|ConnectTimeout|ccburn --)',source))
                self.assertIsNone(re.search(r"[\"']ssh[\"']",source))
        self.assertFalse((ROOT/'claude_oauth_probe.py').exists())

    def test_retired_remote_passive_cannot_override_local_or_oauth(self):
        capture.capture({'rate_limits':{'seven_day':{'used_percentage':42,'resets_at':RESET}}},self.state,capture.stamp(T),'box')
        previous=provider(99,T+100,'claude_statusline_legacy_host')
        previous['windows'].append(dict(provider(88,T+100,label='5H')['windows'][0],source='claude_statusline_legacy_host'))
        remote=unittest.mock.Mock(side_effect=AssertionError('remote capture forbidden'))
        with patch('providers.common.utcnow',return_value=dt(T+120)), patch('providers.claude.claude_oauth_usage',return_value=parse(usage(),T+60,'claude_oauth_legacy_host')):
            result,attempts=claude.claude_collect(dt(T+120),{'state':self.state,'remote':remote,'previous_claude':previous})
        self.assertEqual({w['label']:w['used_percent'] for w in result['windows']},{'ALL WEEK':43,'FABLE WEEK':12})
        self.assertEqual([a['method'] for a in attempts],['claude_statusline','claude_oauth_shared'])
        remote.assert_not_called()
        with patch('providers.common.utcnow',return_value=dt(T+120)), patch('providers.claude.claude_oauth_usage',side_effect=collectors.Attempt('offline')):
            result,_=claude.claude_collect(dt(T+120),{'state':self.state,'remote':remote,'previous_claude':previous})
        self.assertEqual([(w['label'],w['used_percent'],w['source']) for w in result['windows']],[('ALL WEEK',42,'claude_statusline')])
        remote.assert_not_called()


    def test_restart_and_failed_polls_drop_retired_windows_but_keep_local_oauth(self):
        for mixed in (False,True):
            with self.subTest(mixed=mixed):
                previous=provider(99,T+100,'claude_statusline_legacy_host')
                if mixed:
                    previous['source']='claude_merged'
                    previous['windows'][0]['source']='claude_statusline_legacy_host'
                    previous['windows'].extend([
                        dict(provider(4,T,'claude_statusline',label='5H')['windows'][0],source='claude_statusline',measured_at=capture.stamp(T)),
                        dict(provider(12,T,'claude_oauth_legacy_host',label='FABLE WEEK')['windows'][0],source='claude_oauth_legacy_host',measured_at=capture.stamp(T))])
                previous['provider']='claude'
                capture.atomic_json(self.state/'sources.json',{'raw':{'providers':[previous],'sources':{}}})
                original=collectors.run_chain
                def chain(name,now,ctx):
                    return original(name,now,ctx) if name=='claude' else (None,[])
                with patch('providers.common.utcnow',return_value=dt(T+120)), patch('collectors.run_chain',side_effect=chain), patch('providers.claude.claude_oauth_usage',side_effect=collectors.Attempt('offline')), patch('providers.common.which',return_value=None), patch('collectors.log'):
                    for _ in range(2):
                        collector=collectors.Collector(self.state,daemon,config=__import__('dataclasses').replace(collectors.load_config(), enabled_providers=('grok','claude','codex','agy')))
                        collector.poll()
                        collector.poll()
                providers=capture.read_json(self.state/'sources.json')['raw']['providers']
                if mixed:
                    windows=providers[0]['windows']
                    self.assertEqual({w['source'] for w in windows},{'claude_statusline','claude_oauth_legacy_host'})
                    self.assertEqual({w['label'] for w in windows},{'5H','FABLE WEEK'})
                    self.assertEqual({w['measured_at'] for w in windows},{capture.stamp(T)})
                else:
                    self.assertEqual(providers,[])

    def test_collector_merges_passive_and_oauth_and_survives_missing_sources(self):
        passive={'captured_at':capture.stamp(T),'rate_limits':{'seven_day':{'used_percentage':42,'resets_at':RESET}}}
        capture.atomic_json(self.state/'claude-statusline.json',passive)
        oauth_provider=parse(usage(),T+60,'claude_oauth_legacy_host')
        ctx={'state':self.state,'remote':lambda:{}}
        with patch('providers.common.utcnow',return_value=dt(T+120)),patch('providers.claude.claude_oauth_usage',return_value=oauth_provider):
            p,attempts=collectors.run_chain('claude',dt(T+120),ctx)
        self.assertEqual(next(w for w in p['windows'] if w['label']=='ALL WEEK')['used_percent'],43)
        ctx['previous_claude']=p
        with patch('providers.common.utcnow',return_value=dt(T+720)),patch('providers.claude.claude_oauth_usage',side_effect=collectors.Attempt('offline')):
            retained,_=collectors.run_chain('claude',dt(T+720),ctx)
        self.assertEqual(next(w for w in retained['windows'] if w['label']=='FABLE WEEK')['age_seconds'],660)
        self.assertEqual({w['label'] for w in retained['windows']},{'ALL WEEK','FABLE WEEK'})
        # Existing daemon merge preserves per-window provenance into raw snapshots.
        merged=daemon.merge_raw({'providers':[retained],'sources':{}},None,{'cache':None,'state':'unavailable','error_code':None,'attempted_at':None},None,None,dt(T+720))
        window=next(p for p in merged['providers'] if p['provider']=='claude')['windows'][0]
        self.assertIn('measured_at',window)
        self.assertIn('source',window)

    def test_complete_poll_persists_raw_ages_and_other_providers_still_run_each_poll(self):
        passive={'captured_at':capture.stamp(T),'rate_limits':{'seven_day':{'used_percentage':42,'resets_at':RESET}}}
        capture.atomic_json(self.state/'claude-statusline.json',passive)
        collector=collectors.Collector(self.state,daemon,config=__import__('dataclasses').replace(collectors.load_config(), enabled_providers=('grok','claude','codex','agy')))
        clock=[T+120]; counts={name:0 for name in ('codex','grok','agy')}
        original=collectors.run_chain
        def chain(name,now,ctx):
            if name=='claude': return original(name,now,ctx)
            counts[name]+=1
            if name=='grok':
                return {'provider':'grok','source':'grok_session','plan':'test','parsed':{'plan':'test','used_percent':5,'reset_at':capture.stamp(RESET)}},[{'method':'mock','ok':True}]
            p=provider(3,clock[0]);p.update(provider=name,plan='test')
            p['windows'][0]['label']='WEEK' if name=='codex' else 'ARGON WEEK'
            return p,[{'method':'mock','ok':True}]
        with patch('providers.common.utcnow',side_effect=lambda:dt(clock[0])),patch('collectors.run_chain',side_effect=chain),patch('providers.claude.claude_oauth_usage',return_value=parse(usage(),T+60,'claude_oauth_legacy_host')),patch('providers.common.which',return_value=None),patch('collectors.log'):
            collector.poll()
            clock[0]+=60
            collector.poll()
        raw=capture.read_json(self.state/'sources.json')['raw']
        windows=next(p for p in raw['providers'] if p['provider']=='claude')['windows']
        self.assertEqual({w['age_seconds'] for w in windows},{120})
        self.assertEqual(counts,{'codex':2,'grok':2,'agy':2})
        view,_=core.analyze_snapshot(raw,'claude',dt(clock[0]))
        self.assertTrue(all(w['age_seconds']==120 for w in view['providers'][0]['windows']))

    def test_minimal_tsv_six_columns_stay_stable(self):
        # Execute only the pure renderer functions; do not import or start an MCP server.
        import ast
        tree=ast.parse((ROOT/'mcp_server.py').read_text())
        functions=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in ('_fmt_pct','render_minimal')]
        namespace={'json':json}
        exec(compile(ast.Module(body=functions,type_ignores=[]),'mcp_renderer','exec'),namespace)
        window,_=core.analyze_window(dict(provider(43,T)['windows'][0],source='claude_oauth_usage',measured_at=capture.stamp(T)),dt(T+120),'claude')
        view={'checked_at':capture.stamp(T+120),'providers':[{'provider':'claude','plan':'Max','windows':[window]}]}
        text=namespace['render_minimal'](view,sequence=1)
        lines=text.splitlines()
        self.assertEqual(lines[1],'provider\tplan\twindow\tused_pct\tpace_pct\treset_in')
        self.assertEqual(len(lines[2].split('\t')),6)
        self.assertNotIn('age_seconds',lines[2])

    def test_history_route_and_provider_order_remain(self):
        html=(ROOT/'dashboard.html').read_text()
        self.assertIn("const ORDER=['grok','claude','codex','agy']",html)
        self.assertIn('/v1/history?hours=',html)
        self.assertIn('อายุ',html)
        self.assertIn("sourceLabel(w.source)+' · '+age(w,checkedAt)",html)
        self.assertIn("claude_statusline:'local'",html)
        self.assertNotIn("claude_statusline_legacy_host:'legacy'",html)
        self.assertNotIn("claude_oauth_legacy_host:'OAuth legacy'",html)
        with patch('dashboard.cache_for') as cached:
            cached.return_value.get.side_effect=lambda path,key,build:build([])
            result=dashboard.history_data(self.state,168,1800)
        self.assertEqual(result['hours'],168)
        self.assertEqual(result['series'],[])


if __name__=='__main__':
    unittest.main(verbosity=2)
