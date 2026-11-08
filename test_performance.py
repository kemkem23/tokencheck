"""Offline regressions for history bucket selection and optional MCP plotting."""
import importlib
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import dashboard

class HistoryDownsampleTests(unittest.TestCase):
    def sample(self, stamp, used, pace, provider='claude', label='WEEK'):
        return {'t': datetime.fromtimestamp(stamp,timezone.utc).isoformat().replace('+00:00','Z'),
                'providers':[{'n':provider,'w':[{'l':label,'u':used,'p':pace}]}]}

    def test_newest_point_wins_with_last_equal_timestamp_and_sorted_buckets(self):
        now=1800000000
        samples=[self.sample(now-5,20,30), self.sample(now-3605,3,5),
                 self.sample(now-10,10,20),self.sample(now-5,25,35),
                 self.sample(now-4,float('nan'),1),self.sample(now+1,90,90)]
        with patch('dashboard.time.time',return_value=now):
            result=dashboard._history_data(samples,24,1800)
        self.assertEqual(len(result['series']),1)
        series=result['series'][0]
        self.assertEqual((series['provider'],series['window']),('claude','ALL WEEK'))
        self.assertEqual([(p['used'],p['pace']) for p in series['points']],[(3,5),(25,35)])
        self.assertEqual([p['t'] for p in series['points']], [samples[1]['t'],samples[3]['t']])
        json.dumps(result,allow_nan=False)

    def test_window_boundaries_and_separate_provider_series(self):
        now=1800000000
        samples=[self.sample(now-86400,1,2), self.sample(now-86401,99,99),
                 self.sample(now,3,4,provider='codex'),self.sample(now-7*86400-1,9,9)]
        with patch('dashboard.time.time',return_value=now):
            result=dashboard._history_data(samples,24,300)
        self.assertEqual([(s['provider'],s['window'],len(s['points'])) for s in result['series']],
                         [('claude','ALL WEEK',1),('codex','WEEK',1)])
        self.assertEqual(result['series'][0]['points'][0]['used'],1)
        self.assertEqual(result['series'][1]['points'][0]['used'],3)
        self.assertEqual(result['history_hours'],round(86401/3600,2))

class LazyPlottingTests(unittest.TestCase):
    def test_text_tools_work_without_plotting_dependency_and_png_loads_on_demand(self):
        # Supply a finder that rejects plotting on import, as on a text-only install.
        class RejectPlot:
            def find_spec(self,fullname,path=None,target=None):
                if fullname=='plot_png' or fullname.startswith('matplotlib'):
                    raise AssertionError('text tool imported plotting')
        finder=RejectPlot()
        sys.meta_path.insert(0,finder)
        try:
            module=importlib.import_module('mcp_server')
            snapshot={'sequence':7,'results':{'all':{'checked_at':'2026-01-01T00:00:00Z','providers':[]}}}
            with patch.object(module,'get',return_value=snapshot):
                result=module.tokencheck_minimal()
                self.assertIn('provider\tplan\twindow\tused_pct\tpace_pct\treset_in',result)
                self.assertEqual(json.loads(result.split('json\t')[1])['seq'],7)
                self.assertEqual(json.loads(module.tokencheck_snapshot(full=True)),snapshot)
                self.assertEqual(set(json.loads(module.tokencheck_methods())),{'collector','unhealthy','health'})
            self.assertEqual(set(module.mcp._tool_manager._tools),
                {'tokencheck_chart','tokencheck_minimal','tokencheck_png','tokencheck_snapshot','tokencheck_methods'})
        finally:
            sys.meta_path.remove(finder)
        plot=types.ModuleType('plot_png');calls=[]
        def render(*a,**kw):
            calls.append((a,kw))
            a[1].write_bytes(b"offline PNG stub")
        plot.render_png=render
        from plot_png import atomic_write
        plot.atomic_write=atomic_write
        with tempfile.TemporaryDirectory() as state, patch.dict(sys.modules,plot_png=plot), \
             patch.object(module,'STATE',state),patch.object(module,'get',return_value=snapshot), \
             patch.object(module.hist,'load_samples',return_value=[]):
            result=module.tokencheck_png(provider='codex',hours=72)
            self.assertEqual(len(calls),1)
            primary=Path(result.splitlines()[0].split("=",1)[1])
            self.assertEqual(primary.read_bytes(),(Path(state)/"tokencheck-usage-pace.png").read_bytes())
            self.assertTrue(all(kw['provider']=='codex' and kw['hours']==72 for _,kw in calls))
            self.assertIn('alias='+str(Path(state)/'tokencheck-usage-pace.png'),result)
            self.assertIn('provider=codex hours=72',result)

if __name__=='__main__':unittest.main()
