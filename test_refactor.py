"""Offline configuration, transport isolation, dashboard and installation regressions."""
import contextlib
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from email.message import Message
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import config
import collectors
import core
import daemon
import dashboard
from history_cache import HistoryCache
import selfcheck

ROOT=Path(__file__).resolve().parent

class Offline(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.cfgfile=self.root/'config.json'
        self.cfgfile.write_text(json.dumps({'state_dir':str(self.root/'state')}))
        env=patch.dict(os.environ,{'HOME':str(self.root),'PACER_CONFIG':str(self.cfgfile)},clear=True)
        env.start(); self.addCleanup(env.stop)
        for target in ('urllib.request.urlopen','urllib.request.OpenerDirector.open','socket.create_connection','providers.claude._claude_profile_token','providers.common.run'):
            p=patch(target,side_effect=AssertionError('live path forbidden'))
            p.start();self.addCleanup(p.stop)

    def configure(self, **values):
        self.cfgfile.write_text(json.dumps(dict(state_dir=str(self.root/'state'),**values)))
        return config.load_config()

class ConfigTests(Offline):
    def test_defaults_and_compatibility_precedence(self):
        cfg=config.load_config()
        self.assertEqual((cfg.port,cfg.poll_interval,cfg.oauth_interval),(48829,60,600))
        self.assertEqual(cfg.claude_config_dir,cfg.state_dir/'claude-usage-config')
        self.assertEqual(cfg.enabled_providers,())
        with patch.dict(os.environ,{'PACER_STATE_DIR':str(self.root/'override')}):
            cfg=config.load_config()
            self.assertEqual(cfg.claude_config_dir,self.root/'override/claude-usage-config')
        with patch.dict(os.environ,{'PACER_CLAUDE_CONFIG_DIR':str(self.root/'isolated')}):
            self.assertEqual(config.load_config().claude_config_dir,self.root/'isolated')

    def test_toml_and_invalid_values(self):
        path=self.root/'config.toml'
        path.write_text('port=49999\nenabled_providers=["codex"]\noauth_interval=900\n')
        self.assertEqual(config.load_config(path).oauth_interval,900)
        for values in ({'port':0},{'port':True},{'poll_interval':59},{'oauth_interval':599},{'enabled_providers':['bad']},{'enabled_providers':['codex','codex']},{'unknown':1},{'state_dir':'relative'},{'grok_tmux_pane':'-bad'},{'claude_config_dir':str(self.root/'.claude')}):
            self.cfgfile.write_text(json.dumps(values))
            with self.subTest(values=values),self.assertRaises(ValueError):config.load_config()

    def test_missing_explicit_config_fails(self):
        self.cfgfile.unlink()
        with self.assertRaises(ValueError):config.load_config()

    def test_cli_history_hook_and_mcp_share_config(self):
        self.configure(port=49998)
        import tokencheck,history
        self.assertEqual(tokencheck.service_url(),'http://127.0.0.1:49998/v1/snapshot')
        self.assertEqual(tokencheck.state_dir(),history.state_dir())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(tokencheck.main(['--validate-config']),0)
        self.assertEqual(json.loads(out.getvalue())['port'],49998)
        # Verify launcher/hook/MCP consume the shared resolver, without starting them.
        for name in ('claude_statusline_hook.py','mcp_server.py','start-daemon.sh','stop-daemon.sh'):
            self.assertIn('load_config' if name.endswith('.py') else 'config.py',(ROOT/name).read_text())

class ProviderTests(Offline):
    def test_default_poll_has_no_provider_discovery_credentials_or_gate(self):
        cfg=self.configure()
        cfg.state_dir.mkdir()
        with patch('collectors.run_chain',side_effect=AssertionError('provider called')), patch('providers.common.which',side_effect=AssertionError('provider discovered')):
            status=collectors.Collector(cfg.state_dir,daemon,cfg).poll()
        self.assertEqual({v['state'] for v in status['providers'].values()},{'disabled'})
        self.assertTrue(all(v['attempts']==[] for v in status['providers'].values()))
        self.assertFalse((cfg.state_dir/'claude-oauth-cache.json').exists())
        self.assertFalse((cfg.state_dir/'claude-oauth.lock').exists())
        self.assertFalse(cfg.claude_config_dir.exists())

    def test_optional_providers_skip_all_live_paths_and_keep_other_results(self):
        cfg=self.configure(enabled_providers=['codex'])
        cfg.state_dir.mkdir()
        now=datetime.now(timezone.utc)
        provider={'provider':'codex','source':'mock','measured_at':now.isoformat(),'windows':[{'label':'5H','used_percent':1,'window_hours':5,'resets_at':(now+timedelta(hours=4)).isoformat()}]}
        with patch('collectors.run_chain',return_value=(provider,[{'method':'mock','ok':True}])) as chain:
            col=collectors.Collector(cfg.state_dir,daemon,cfg)
            status=col.poll()
        self.assertEqual([c.args[0] for c in chain.call_args_list],['codex'])
        self.assertEqual({n:v['state'] for n,v in status['providers'].items()},{'claude':'disabled','codex':'ok','agy':'disabled','grok':'disabled'})
        engine=daemon.Engine(cfg.state_dir,None,None,enabled_providers=cfg.enabled_providers)
        envelope=engine.tick()
        self.assertEqual([p['provider'] for p in envelope['results']['all']['providers']],['codex'])
        self.assertNotIn('unreliable:claude',envelope['results']['all']['overall']['reason_codes'])
        self.assertFalse((cfg.state_dir/'grok-usage.json').exists())

    def test_exception_and_failure_preserve_other_providers_and_measurements(self):
        cfg=self.configure(enabled_providers=['codex','agy'])
        cfg.state_dir.mkdir()
        now=datetime.now(timezone.utc)
        stamp=now.isoformat()
        good={'provider':'codex','source':'mock','measured_at':stamp,'windows':[]}
        def fail(now,ctx):raise RuntimeError('mock')
        def success(now,ctx):return dict(good)
        with patch.dict(collectors.CHAINS,{'codex':[('mock',success)],'agy':[('failure',fail)]}):
            col=collectors.Collector(cfg.state_dir,daemon,cfg)
            status=col.poll()
        self.assertEqual(status['providers']['codex']['state'],'ok')
        self.assertEqual(status['providers']['agy']['attempts'][0]['reason'],'exception:RuntimeError')
        with patch('collectors.run_chain',return_value=(None,[{'reason':'offline'}])):
            col.poll()
        self.assertEqual(col.last['codex']['measured_at'],stamp)
        self.assertTrue(col.last['codex']['stale'])

    def test_configured_oauth_interval_keeps_existing_gate_and_clock(self):
        from claude_oauth_cache import OAuthCache
        from claude_capture import atomic_json,read_json
        state=self.root/'oauth';state.mkdir()
        atomic_json(state/'claude-oauth-cache.json',{'last_attempt_at':1000,'next_due':5000,'marker':'keep'})
        request=lambda: (_ for _ in ()).throw(AssertionError('not due'))
        OAuthCache(state,clock=lambda:1500,interval=900).get(request,None)
        data=read_json(state/'claude-oauth-cache.json')
        self.assertEqual(data,{'last_attempt_at':1000,'next_due':5000,'marker':'keep'})
        self.assertTrue((state/'claude-oauth.lock').exists())

class HistoryTests(Offline):
    def sample(self,used=1):
        return {'t':datetime.now(timezone.utc).isoformat(),'providers':[{'n':'codex','w':[{'l':'5H','u':used,'p':2}]}]}

    def test_cache_hit_does_not_reopen_and_shape_is_stable(self):
        path=self.root/'history.jsonl';path.write_text(json.dumps(self.sample())+'\n')
        first=dashboard.history_data(self.root,168,1800)
        with patch('pathlib.Path.open',side_effect=AssertionError('reopened unchanged history')):
            self.assertIs(dashboard.history_data(self.root,168,1800),first)
        self.assertEqual(set(first),{'hours','step','generated_at','history_hours','series'})
        self.assertEqual(set(first['series'][0]),{'provider','window','points'})
        self.assertEqual(set(first['series'][0]['points'][0]),{'t','used','pace'})

    def test_append_partial_truncate_rotation_and_same_size_rewrite(self):
        path=self.root/'history.jsonl'
        path.write_bytes(b'{"v":1}\n')
        cache=HistoryCache();build=lambda samples:samples
        self.assertEqual(cache.get(path,'raw',build),[{'v':1}])
        with path.open('ab') as out:out.write(b'{"v":')
        self.assertEqual(cache.get(path,'raw',build),[{'v':1}])
        with path.open('ab') as out:out.write(b'2}\n')
        self.assertEqual(cache.get(path,'raw',build),[{'v':1},{'v':2}])
        path.write_bytes(b'{"v":3}\n')
        self.assertEqual(cache.get(path,'raw',build),[{'v':3}])
        path.write_bytes(b'{"v":4}\n')
        os.utime(path,ns=(path.stat().st_atime_ns,path.stat().st_mtime_ns+1000000))
        self.assertEqual(cache.get(path,'raw',build),[{'v':4}])
        replacement=self.root/'new';replacement.write_bytes(b'{"v":5}\n');replacement.replace(path)
        self.assertEqual(cache.get(path,'raw',build),[{'v':5}])
        path.unlink()
        self.assertEqual(cache.get(path,'raw',build),[])

    def test_incremental_append_seeks_and_downsample_invalidates(self):
        path=self.root/'history.jsonl';path.write_text(json.dumps(self.sample())+'\n')
        before=dashboard.history_data(self.root,168,1800)
        offset=path.stat().st_size
        with path.open('a') as out:out.write(json.dumps(self.sample(9))+'\n')
        original=Path.open;seeks=[]
        class Reader:
            def __init__(self,stream):self.stream=stream
            def __getattr__(self,k):return getattr(self.stream,k)
            def __enter__(self):return self
            def __exit__(self,*args):self.stream.close()
            def seek(self,offset,*args):seeks.append(offset);return self.stream.seek(offset,*args)
        def opening(p,*args,**kwargs):return Reader(original(p,*args,**kwargs))
        with patch.object(Path,'open',opening):after=dashboard.history_data(self.root,168,1800)
        self.assertIn(offset,seeks)
        self.assertIsNot(before,after)
        self.assertEqual(after['series'][0]['points'][-1]['used'],9)

    def test_truncate_and_regrow_is_not_mistaken_for_append(self):
        path=self.root/'history.jsonl'
        path.write_bytes(b'{"v":1}\n')
        cache=HistoryCache()
        self.assertEqual(cache.get(path,'raw',lambda x:x),[{'v':1}])
        path.write_bytes(b'{"v":99}\n{"v":100}\n')
        self.assertEqual(cache.get(path,'raw',lambda x:x),[{'v':99},{'v':100}])

    def test_concurrent_cache_access_stays_bounded(self):
        path=self.root/'history.jsonl';path.write_text('{"v":1}\n'*12000)
        cache=HistoryCache();answers=[]
        threads=[threading.Thread(target=lambda:answers.append(cache.get(path,'size',len))) for _ in range(8)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(answers,[11000]*8)

class MemoryHandler:
    def __init__(self,path='/dashboard',headers=None,engine=None):
        self.path=path;self.requestline='GET '+path+' HTTP/1.1';self.admitted=True
        self.headers=Message()
        for key,val in (headers or {}).items():self.headers[key]=val
        self.wfile=io.BytesIO();self.response_headers={};self.status=None
        self.server=SimpleNamespace(engine=engine,server_address=("127.0.0.1",48829))
    def host_allowed(self):return self.headers.get('Host','localhost')=='localhost'
    def send_response(self,code):self.status=code
    def send_header(self,key,val):self.response_headers[key]=val
    def end_headers(self):pass
    def send_error_json(self,status,code,**kwargs):self.status=status

class DashboardTests(Offline):
    def engine(self,text='test'):
        return SimpleNamespace(lock=threading.Lock(),published={'envelope':{'results':{'all':{'providers':[],'checked_at':'2026-11-08T00:00:00Z','note':text}}}})

    def test_embedded_view_escapes_script_and_uses_published_engine(self):
        engine=self.engine('</script><script>alert(1)</script>&\u2028')
        html=dashboard.embedded_dashboard(engine).decode()
        embedded=html.split('type="application/json">')[1].split('</script>')[0]
        self.assertNotIn('<',embedded);self.assertNotIn('&',embedded)
        self.assertEqual(json.loads(embedded),engine.published['envelope'])
        self.assertNotIn('__PACER_SNAPSHOT__',html)

    def test_gzip_etag_304_head_and_encoding_quality(self):
        engine=self.engine()
        first=MemoryHandler(headers={'Accept-Encoding':'gzip'},engine=engine)
        dashboard.handle(first)
        self.assertEqual(first.status,200)
        body=first.wfile.getvalue()
        self.assertEqual(int(first.response_headers['Content-Length']),len(body))
        self.assertIn(b'pacer-snapshot',gzip.decompress(body))
        self.assertEqual(first.response_headers['Vary'],'Accept-Encoding')
        second=MemoryHandler(headers={'Accept-Encoding':'gzip','If-None-Match':'W/'+first.response_headers['ETag']},engine=engine)
        dashboard.handle(second)
        self.assertEqual(second.status,304);self.assertEqual(second.wfile.getvalue(),b'')
        self.assertEqual(second.response_headers['Content-Length'],first.response_headers['Content-Length'])
        head=MemoryHandler(headers={'Accept-Encoding':'gzip'},engine=engine)
        dashboard.handle(head,head=True)
        self.assertEqual(head.wfile.getvalue(),b'')
        self.assertEqual(head.response_headers['ETag'],first.response_headers['ETag'])
        plain=MemoryHandler(headers={'Accept-Encoding':'gzip;q=0, *;q=1'},engine=engine)
        dashboard.handle(plain)
        self.assertNotIn('Content-Encoding',plain.response_headers)
        self.assertNotEqual(plain.response_headers['ETag'],first.response_headers['ETag'])

    def test_history_route_defaults_and_request_guards(self):
        engine=self.engine();engine.state_dir=self.root
        handler=MemoryHandler('/v1/history',engine=engine)
        dashboard.handle(handler)
        result=json.loads(handler.wfile.getvalue())
        self.assertEqual((result['hours'],result['step']),(168,1800))
        for path,headers,status in [('/dashboard',{'Host':'evil'},421),('/v1/history?hours=9',{},400),('/v1/history?hours=24&hours=72',{},400),('/dashboard',{'Content-Length':'1'},400)]:
            handler=MemoryHandler(path,headers,engine)
            dashboard.handle(handler,head=True)
            self.assertEqual(handler.status,status);self.assertEqual(handler.wfile.getvalue(),b'')

    @unittest.skipUnless(shutil.which('node'),'node unavailable')
    def test_actual_client_renders_before_fetch_and_history_never_blocks_snapshot(self):
        html=dashboard.embedded_dashboard(self.engine()).decode()
        script=html.split('<script>')[1].split('</script>')[0]
        fixture=json.dumps({'results':{'all':{'providers':[],'checked_at':'2026-11-08T00:00:00Z'}}})
        harness='''const vm=require('vm');const nodes={};let fetched=0,frame;
const make=()=>({textContent:'',dataset:{},append(){},replaceChildren(){},setAttribute(){}});
const initial=FIXTURE;
const context={console,Intl,Date,Number,Math,JSON,Error,Promise,AbortSignal,
 document:{querySelector:s=>nodes[s]||(nodes[s]=make()),querySelectorAll:()=>[],createDocumentFragment:make},
 window:{matchMedia:()=>({matches:false})},ResizeObserver:class{disconnect(){} observe(){}},
 performance:{mark(){}},requestAnimationFrame:fn=>{frame=fn},setTimeout:fn=>fn(),setInterval(){},
 fetch:url=>{fetched++;return url.includes('history')?new Promise(()=>{}):Promise.resolve({ok:true,json:async()=>initial})}};
context.document.querySelector('#pacer-snapshot').textContent=JSON.stringify(initial);
vm.createContext(context);vm.runInContext(SCRIPT,context);
if(fetched!==0||!nodes['#updated'].textContent)throw Error('initial render waited for fetch');
frame();setImmediate(()=>{if(fetched!==2||!nodes['#updated'].textContent)throw Error('snapshot blocked by history');console.log('PASS immediate render and pending history');});
'''.replace('FIXTURE',fixture).replace('SCRIPT',json.dumps(script))
        result=subprocess.run([shutil.which('node'),'-e',harness],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

class WatcherTests(Offline):
    def test_disabled_providers_clear_outage_without_notifications(self):
        # Import lifecycle module only: no main(), collector loop, or server startup.
        import pacer_daemon as service
        status={'polled_at':datetime.now(timezone.utc).isoformat(),
                'providers':{name:{'state':'disabled','attempts':[]} for name in config.PROVIDERS}}
        engine=SimpleNamespace(health=lambda:(200,{'ready':True}))
        self.root.joinpath('unhealthy-since.json').write_text(json.dumps({'providers':{'claude':{'healthy':False,'since':'2026-01-01T00:00:00Z','unhealthy_seconds':99999}},'incident':{'id':'old'}}))
        with patch.object(service,'COLLECTOR',SimpleNamespace(status=status)):
            watcher=service.Watcher(self.root,engine)
            with patch.object(watcher,'post',side_effect=AssertionError('disabled incident')):
                watcher.check()
        self.assertEqual(watcher.state['unhealthy_now'],[])
        self.assertIsNone(watcher.state['incident'])

    def test_extra_host_and_body_guards_on_lifecycle_routes(self):
        import pacer_daemon as service
        engine=SimpleNamespace(state_dir=self.root)
        handler=MemoryHandler('/dashboard',{'Host':'evil.example'},engine)
        self.assertFalse(service.host_allowed(handler))
        with patch.object(service,'EXTRA_HOSTS',{'monitor.example'}):
            handler.headers.replace_header('Host','monitor.example:48829')
            self.assertTrue(service.host_allowed(handler))
            handler.headers.replace_header('Host','monitor.example.evil:48829')
            self.assertFalse(service.host_allowed(handler))
        handler=MemoryHandler('/v1/collector',{'Content-Length':'1'},engine)
        service.dispatch(handler,head=True)
        self.assertEqual(handler.status,400)

class SelfCheckInstallTests(Offline):
    def test_self_check_reports_readiness_and_providers_without_collection(self):
        self.configure(port=49997)
        calls=[]
        class Opener:
            def open(self,request,timeout):
                calls.append(request.full_url)
                data={'ready':True} if request.full_url.endswith('healthz') else {'providers':{'codex':{'state':'ok'}}}
                response=io.BytesIO(json.dumps(data).encode());response.status=200
                return response
        result,code=selfcheck.check(Opener())
        self.assertEqual(code,0);self.assertTrue(result['ready'])
        self.assertEqual(calls,['http://127.0.0.1:49997/healthz','http://127.0.0.1:49997/v1/collector'])
        class Down:
            def open(self,*args,**kwargs):raise OSError('down')
        self.assertEqual(selfcheck.check(Down())[1],1)
        self.cfgfile.write_text('{"port":0}')
        self.assertEqual(selfcheck.check(Opener())[1],64)

    def test_install_dry_run_temp_home_and_config_no_actions(self):
        import install
        self.configure(port=49996)
        before=set(self.root.rglob('*'))
        with patch('subprocess.run',side_effect=AssertionError('dry-run subprocess')),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(install.install(['--dry-run','--offline']),0)
        result=json.loads(out.getvalue())
        self.assertEqual(result['port'],49996)
        self.assertFalse(result['tailscale_serve']);self.assertFalse(result['daemon_started'])
        self.assertIsNone(result['claude_settings'])
        self.assertEqual(set(self.root.rglob('*')),before)

    def test_statusline_pins_config_and_state_and_preserves_original(self):
        import install_claude_statusline as installer
        cfg=self.configure(port=49995)
        settings=self.root/'custom/settings.json';settings.parent.mkdir()
        original={'statusLine':{'type':'command','command':'printf preserved','padding':2},'other':True}
        settings.write_text(json.dumps(original))
        self.assertTrue(installer.install(settings));self.assertFalse(installer.install(settings))
        saved=json.loads(settings.read_text())
        command=saved['statusLine']['command']
        self.assertIn('PACER_CONFIG='+str(self.cfgfile),command)
        self.assertIn('PACER_STATE_DIR='+str(cfg.state_dir),command)
        self.assertTrue(saved['other']);self.assertEqual(saved['statusLine']['padding'],2)
        self.assertEqual(len(list(settings.parent.glob('*.bak-*'))),1)

if __name__=='__main__':unittest.main()
