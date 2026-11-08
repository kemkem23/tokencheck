"""Offline rendering contracts; synthetic compact samples, no collection or network."""
import importlib.util
import json
import struct
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import plot_png as plot

NOW = datetime(2026,11,8,12,tzinfo=plot.ICT)
class Clock(datetime):
    @classmethod
    def now(cls,tz=None): return NOW.astimezone(tz)

def sample(seconds=0, used=0.53, pace=7.11, label='ARGON WEEK', **extra):
    return {'t':(NOW+timedelta(seconds=seconds)).isoformat(),
            'providers':[{'n':'agy','w':[dict(l=label,u=used,p=pace,**extra)]}]}

class SvgTests(unittest.TestCase):
    def setUp(self):
        self.clock=patch.object(plot,'datetime',Clock);self.clock.start();self.addCleanup(self.clock.stop)

    def test_fixed_scale_real_windows_and_sample_pace(self):
        svg=plot.render_svg([sample(-60),sample()])
        root=ET.fromstring(svg)
        texts=[e.text for e in root.iter() if e.tag.endswith('}text')]
        self.assertTrue({'0','100','0.53%','Pace 7.11%'} <= set(texts))
        self.assertNotIn('5H',svg)
        self.assertIn('stroke-dasharray',svg)
        self.assertIn('ICT',svg)
        self.assertEqual(len(plot.panels_from_samples([sample()])),1)

    def test_unstarted_hides_pace_marker_and_reset_claim(self):
        svg=plot.render_svg([sample(pace=99,started=False,r='2026-11-09T00:00:00Z')])
        self.assertIn('ยังไม่เริ่ม',svg)
        self.assertNotIn('Reset in',svg)
        self.assertIn('data-kind="inactive"',svg)
        self.assertNotIn('data-kind="active"',svg)
        self.assertNotIn('99%',svg)
        self.assertNotIn('Pace marker:',svg)
        self.assertIsNone(plot.panels_from_samples([sample(started=False)])[0][2][0][2])

    def test_escape_controls_long_thai_titles_and_labels(self):
        text='ทดสอบ <script> & '+('ยาวมาก '*8)+'\x00'
        svg=plot.render_svg([sample(label=text)],title='ทดสอบ <script> & ICT')
        root=plot._validate_svg(svg)
        self.assertNotIn('<script',svg)
        self.assertIn('&lt;script',svg)
        self.assertIn('&amp;',svg)
        self.assertGreater(float(root.get('height')),800)
        self.assertTrue(all(sum(plot._metrics().get(str(ord(c)),1)*17*1.08 for c in s)<=383 for s in plot._wrap(text,17)))

    def test_wrap_prefers_whole_tokens_and_preserves_thai_clusters(self):
        measure = lambda s: sum(plot._metrics().get(str(ord(c)),1) for c in s)*21*1.08
        token = 'โควตาและอัตราการใช้งานจริง'
        width = measure(token) + 1
        self.assertEqual(plot._wrap('Usage '+token,21,width),['Usage',token])
        self.assertEqual(plot._wrap('  Usage   pace  ',21,measure('Usage pace')+1),['Usage pace'])
        self.assertEqual(plot._wrap('',21),[''])
        self.assertEqual(plot._text_clusters('เก้ากำแป้'),['เก้า','กำ','แป้'])
        # Force many breaks inside an actually oversized, unbroken Thai token.
        token = 'เก้ากำแป้โค้ไก่ใส่'*8
        clusters = plot._text_clusters(token)
        lines = plot._wrap(token,21,measure('เก้ากำ')+1)
        self.assertGreater(len(lines),1)
        self.assertEqual(''.join(lines),token)
        boundaries = set()
        offset = 0
        for cluster in clusters:
            offset += len(cluster)
            boundaries.add(offset)
        offset = 0
        for line in lines:
            offset += len(line)
            self.assertIn(offset,boundaries)
            self.assertNotIn(line[-1],'เแโใไ')
            self.assertFalse(__import__('unicodedata').category(line[0]).startswith('M'))
            self.assertLessEqual(measure(line),measure('เก้ากำ')+1)

    def test_mcp_title_omits_repeated_time_and_keeps_nonpositive_hours(self):
        import mcp_server
        with tempfile.TemporaryDirectory() as d:
            def render(samples,path,**kwargs):
                path.write_bytes(b'offline PNG stub')
            with patch.object(mcp_server,'STATE',d),patch.object(mcp_server,'get',return_value={}), \
                 patch.object(mcp_server.hist,'load_samples',return_value=[]), \
                 patch.object(plot,'render_png',side_effect=render) as renderer:
                result=mcp_server.tokencheck_png(hours=0)
            self.assertEqual(renderer.call_args.kwargs['title'],'Tokencheck / Usage & pace')
            self.assertEqual(renderer.call_args.kwargs['hours'],0)
            self.assertIn('provider=all hours=0',result)
            self.assertEqual((Path(d)/'tokencheck-usage-pace.png').read_bytes(),b'offline PNG stub')

    def test_empty_stale_invalid_future_and_default_24_hours(self):
        for data in [[],[sample(-86401)],[sample(1)],[dict(t='bad',providers=[])],
                     [dict(t='2026-11-08T11:00:00',providers=[])]]:
            self.assertIn('No history samples',plot.render_svg(data))
        self.assertNotIn('No history samples',plot.render_svg([sample(-86401)],hours=0))
        self.assertNotIn('No history samples',plot.render_svg([sample(-86400)]))
        with self.assertRaises(ValueError):plot.render_svg([],hours=float('nan'))

    def test_nonfinite_missing_and_no_false_line_across_gap_reset(self):
        for value in [None,float('nan'),float('inf'),True,'3']:
            rows=plot._series([sample(-120),sample(-60,used=value),sample()])[0][2]
            self.assertEqual([len(s) for s in plot._segments(rows,1)],[1,1])
            plot._validate_svg(plot.render_svg([sample(used=value)]))
        for data in [[sample(-240),sample()], [sample(-60,used=90),sample(used=1)],
                     [sample(-60,r='2026-11-09T00:00:00Z'),sample(r='2026-11-16T00:00:00Z')],
                     [sample(-120),{'t':(NOW-timedelta(seconds=60)).isoformat(),'providers':[]},sample()]]:
            self.assertEqual(len(plot._segments(plot._series(data)[0][2],1)),2)

    def test_order_duplicate_and_missing_latest_window(self):
        pts=plot._series([sample(),sample(-60),sample(used=8)])[0][2]
        self.assertEqual([p[1] for p in pts],[0.53,8])
        svg=plot.render_svg([sample(-60),{'t':NOW.isoformat(),'providers':[]}])
        self.assertIn('No current populated windows',svg)
        self.assertNotIn('ARGON WEEK',svg)

    def test_claude_5h_requires_current_finite_usage_and_real_reset(self):
        def claude(seconds=0, used=0, reset='2026-11-09T00:00:00Z'):
            data=sample(seconds,used=used,label='5H',r=reset)
            data['providers'][0]['n']='claude'
            return data
        for invalid in [None,float('nan'),float('inf'),True,'0']:
            self.assertNotIn('data-window="5H"',plot.render_svg([claude(-60),claude(used=invalid)]))
        for invalid in [None,'','not-a-date','2026-11-09T00:00:00']:
            self.assertNotIn('data-window="5H"',plot.render_svg([claude(reset=invalid)]))
        absent={'t':NOW.isoformat(),'providers':[]}
        self.assertNotIn('data-window="5H"',plot.render_svg([claude(-60),absent]))
        svg=plot.render_svg([claude(-60,used=2),claude(used=0)])
        self.assertIn('data-window="5H"',svg)
        self.assertIn('>0%</text>',svg)
        self.assertIn('Reset in',svg)
        self.assertEqual(len(plot._current_panels([claude(-60),claude()], 'all')[0][2]),2)

    def portrait_sample(self):
        providers=[]
        for name,labels in [('claude',['ALL WEEK','FABLE WEEK']),('codex',['WEEK']),
                            ('grok',['WEEK']),('agy',['ARGON WEEK'])]:
            providers.append({'n':name,'w':[{'l':label,'u':0.53 if name=='agy' else 43,
                                           'p':7.11,'r':'2026-11-09T00:00:00Z'} for label in labels]})
        providers[-1]['w'] += [{'l':label,'u':0,'p':99,'started':False,
                               'r':'2026-11-09T00:00:00Z'}
                              for label in ['GEM 5H','3P 5H','GEM WEEK','3P WEEK']]
        return {'t':NOW.isoformat(),'providers':providers}

    def display_order(self, data, provider='all'):
        root=ET.fromstring(plot.render_svg(data,provider=provider))
        cards=[e for e in root if e.get('data-kind')=='active']
        # SVG insertion order must equal visual row-major order.
        self.assertEqual(cards,sorted(cards,key=lambda e:(float(e.get('y')),float(e.get('x')))))
        return ([(e.get('data-provider'),e.get('data-window')) for e in cards],
                [e.get('data-window') for e in root if e.get('data-kind')=='inactive'])

    def test_requested_row_major_order_and_scrambled_history(self):
        import copy,random
        expected=[('grok','WEEK'),('claude','ALL WEEK'),('codex','WEEK'),
                  ('claude','FABLE WEEK'),('agy','ARGON WEEK')]
        inactive=['GEM 5H','3P 5H','GEM WEEK','3P WEEK']
        data=self.portrait_sample()
        self.assertEqual(self.display_order([data]),(expected,inactive))
        for seed in range(5):
            rng=random.Random(seed)
            latest=copy.deepcopy(data);earlier=copy.deepcopy(data)
            earlier['t']=(NOW-timedelta(seconds=60)).isoformat()
            for item in [latest,earlier]:
                rng.shuffle(item['providers'])
                for provider in item['providers']:rng.shuffle(provider['w'])
            self.assertEqual(self.display_order([latest,earlier]),(expected,inactive))
            self.assertEqual(self.display_order([latest],provider='claude')[0],
                             [('claude','ALL WEEK'),('claude','FABLE WEEK')])

    def test_real_claude_5h_immediately_follows_all_week(self):
        data=self.portrait_sample()
        data['providers'][0]['w'].append({'l':'5H','u':0,'p':0,'r':'2026-11-09T00:00:00Z'})
        expected=[('grok','WEEK'),('claude','ALL WEEK'),('claude','5H'),
                  ('codex','WEEK'),('claude','FABLE WEEK'),('agy','ARGON WEEK')]
        self.assertEqual(self.display_order([data])[0],expected)
        data['providers'][0]['w'][-1]['r']=None
        self.assertEqual(self.display_order([data])[0],[p for p in expected if p!=('claude','5H')])

    def test_header_aliases_only_preserve_canonical_identity_series_and_order(self):
        import copy
        data=self.portrait_sample()
        data['providers'][0]['w'].append({'l':'5H','u':0,'p':0,'r':'2026-11-09T00:00:00Z'})
        original=copy.deepcopy(data)
        series_before=plot._series([data])
        root=ET.fromstring(plot.render_svg([data]))
        canonical=[(e.get('data-provider'),e.get('data-window')) for e in root
                   if e.get('data-kind')=='active']
        self.assertEqual(canonical,[('grok','WEEK'),('claude','ALL WEEK'),('claude','5H'),
                                    ('codex','WEEK'),('claude','FABLE WEEK'),('agy','ARGON WEEK')])
        def visible_pairs(tree):
            providers=[e.text for e in tree if e.get('data-role')=='provider']
            windows=[e.text for e in tree if e.get('data-role')=='window']
            return list(zip(providers,windows))
        self.assertEqual(visible_pairs(root),[('GROK','WEEK'),('CLAUDE','WEEK'),('CLAUDE','5H'),
                                             ('CODEX','WEEK'),('FABLE','WEEK'),('AGY','ARGON WEEK')])
        filtered=ET.fromstring(plot.render_svg([data],provider='claude'))
        self.assertEqual(visible_pairs(filtered),[('CLAUDE','WEEK'),('CLAUDE','5H'),('FABLE','WEEK')])
        self.assertEqual(plot._display_header('grok','FABLE WEEK'),('GROK','FABLE WEEK'))
        self.assertEqual(plot._display_header('claude','OTHER WEEK'),('CLAUDE','OTHER WEEK'))
        self.assertEqual(data,original)
        self.assertEqual(plot._series([data]),series_before)
        # For these single-line canonical headers, the only SVG differences
        # must be visible header text and adjacent-logo placement; all chart
        # metadata, geometry and traces remain exact.
        with patch.object(plot,'_display_header',side_effect=lambda n,l:(n.upper(),l)):
            unaliased=ET.fromstring(plot.render_svg([data]))
        for tree in (root,unaliased):
            for e in list(tree):
                if e.get('data-role')=='provider-logo':tree.remove(e)
                elif e.get('data-role') in ('provider','window'):e.text='HEADER'
        self.assertEqual(ET.tostring(root),ET.tostring(unaliased))

    def test_extra_windows_have_stable_ties_and_are_not_dropped(self):
        import copy,random
        providers=[{'n':name,'w':[{'l':label,'u':0,'p':0} for label in labels]}
                   for name,labels in [('grok',['Zulu','WEEK','alpha']),
                                       ('codex',['Zulu','WEEK','alpha']),
                                       ('claude',['zeta','Alpha',' alpha ','alpha']),
                                       ('agy',['Zulu','Alpha']),('future',['Zulu','Alpha'])]]
        data={'t':NOW.isoformat(),'providers':providers}
        expected=[('grok','WEEK'),('grok','alpha'),('grok','Zulu'),
                  ('codex','WEEK'),('codex','alpha'),('codex','Zulu'),
                  ('agy','Alpha'),('agy','Zulu'),('claude',' alpha '),
                  ('claude','Alpha'),('claude','alpha'),('claude','zeta'),
                  ('future','Alpha'),('future','Zulu')]
        # More windows than one canvas can hold: exercise ordering before the
        # unchanged explicit capacity check, including a future registry entry.
        with patch.object(plot,'PROVIDER_ORDER',plot.PROVIDER_ORDER+('future',)):
            for seed in range(5):
                scrambled=copy.deepcopy(data);rng=random.Random(seed)
                rng.shuffle(scrambled['providers'])
                for p in scrambled['providers']:rng.shuffle(p['w'])
                panels=sorted(plot._current_panels([scrambled],'all'),key=plot._panel_order_key)
                self.assertEqual([(n,l) for n,l,_ in panels],expected)
                with self.assertRaisesRegex(ValueError,'capacity'):plot.render_svg([scrambled])
        # Extra inactive labels sort deterministically after the canonical rows.
        data=self.portrait_sample()
        data['providers'][-1]['w'] += [{'l':l,'u':0,'started':False} for l in ['Zulu','Alpha']]
        panels=sorted(plot._current_panels([data],'all'),key=plot._panel_order_key)
        self.assertEqual([l for n,l,p in panels if p[-1][3].get('started') is False],
                         ['GEM 5H','3P 5H','GEM WEEK','3P WEEK','Alpha','Zulu'])

    def test_fixed_portrait_current_charts_inactive_rows_and_bounds(self):
        data=self.portrait_sample()
        for title in [None,'ภาพรวมการใช้งาน · โควตาและอัตราการใช้งานจริง <ทดสอบ> & ICT']:
            root=ET.fromstring(plot.render_svg([data],title=title))
            self.assertEqual((root.get('width'),root.get('height')),('393','852'))
            cards=[e for e in root if e.get('data-kind')=='active']
            rows=[e for e in root if e.get('data-kind')=='inactive']
            self.assertEqual(len(cards),5)
            self.assertEqual(len(rows),4)
            self.assertTrue(all(float(e.get('height'))<=30 for e in rows))
            states=[e.text for e in root if e.get('data-role')=='inactive-state']
            self.assertEqual(states,['ยังไม่เริ่ม']*4)
            self.assertEqual([e.text for e in root if e.get('data-role')=='inactive-used'],['0%']*4)
            self.assertEqual(len([e for e in root if e.get('data-role')=='pace-marker']),5)
            for e in root:
                if e.tag.endswith('}rect'):
                    x,y=float(e.get('x',0)),float(e.get('y',0))
                    self.assertGreaterEqual(x,0);self.assertGreaterEqual(y,0)
                    self.assertLessEqual(x+float(e.get('width')),393.01)
                    self.assertLessEqual(y+float(e.get('height')),852.01)
                if e.tag.endswith('}text'):
                    x,y,size=float(e.get('x')),float(e.get('y')),float(e.get('font-size'))
                    self.assertGreaterEqual(size,9)
                    width=sum(plot._metrics().get(str(ord(c)),1) for c in e.text or '')*size
                    left=x-width if e.get('text-anchor')=='end' else x
                    self.assertGreaterEqual(left,-0.01)
                    self.assertLessEqual(left+width,393.01)
                    self.assertGreaterEqual(y-size,0)
                    self.assertLessEqual(y+size*.35,852)
                    if e.get('data-role')=='used':self.assertEqual(size,13)
                    if e.get('data-role')=='reset':self.assertEqual(size,9)
                    if e.get('data-role')=='window':self.assertEqual(size,10)
            for i,a in enumerate(cards+rows):
                ax,ay,aw,ah=[float(a.get(k)) for k in ('x','y','width','height')]
                for b in (cards+rows)[i+1:]:
                    bx,by,bw,bh=[float(b.get(k)) for k in ('x','y','width','height')]
                    self.assertTrue(ax+aw<=bx or bx+bw<=ax or ay+ah<=by or by+bh<=ay)

    def test_plot_dominates_every_active_card_with_compact_secondary_metrics(self):
        data=self.portrait_sample()
        # Also exercise wrapped labels and a longer Thai header on the same canvas.
        for provider,title,long_labels in [('all',None,False),
                ('all','ภาพรวมการใช้งาน · โควตาและอัตราการใช้งานจริง <ทดสอบ> & ICT',False),
                ('claude',None,True),('agy',None,False)]:
            if long_labels:
                data=self.portrait_sample()
                data['providers'][0]['w'][0]['l']='A longer current window label'
            root=ET.fromstring(plot.render_svg([data],provider=provider,title=title))
            cards=[e for e in root if e.get('data-kind')=='active']
            plots=[e for e in root if e.get('data-role')=='plot-area']
            self.assertEqual(len(cards),len(plots))
            for card,chart in zip(cards,plots):
                x,y,w,h=[float(card.get(k)) for k in ('x','y','width','height')]
                px,py,pw,ph=[float(chart.get(k)) for k in ('x','y','width','height')]
                self.assertGreater(pw*ph,w*h*.50)
                self.assertGreater(ph,h*.59)
                self.assertGreaterEqual(px,x)
                self.assertGreaterEqual(py,y)
                self.assertLessEqual(px+pw,x+w+.01)
                self.assertLessEqual(py+ph,y+h+.01)
                if provider=='all' and title is None:
                    # Prior card had only ~31 logical px of plot height.
                    self.assertGreaterEqual(ph,120)
                    self.assertGreater(ph,3.5*31)
                metrics=[e for e in root if e.get('data-role') in ('used','pace','reset','window','provider')
                         and x<=float(e.get('x'))<=x+w and y<=float(e.get('y'))<=y+h]
                for e in metrics:
                    self.assertLessEqual(float(e.get('font-size')),13)
                    self.assertLess(float(e.get('y'))+float(e.get('font-size'))*.4,py)
                bars=[e for e in root if e.get('data-role')=='battery'
                      and x<=float(e.get('x'))<=x+w and y<=float(e.get('y'))<=y+h]
                self.assertEqual(len(bars),1)
                self.assertLessEqual(float(bars[0].get('height')),4)
                self.assertLessEqual(float(bars[0].get('width')),49)
            self.assertEqual(len([e for e in root if e.get('data-role')=='time']),2*len(cards))

    def test_unpopulated_windows_hidden_and_provider_filter_preserved(self):
        data=self.portrait_sample()
        data['providers'][0]['w'].append({'l':'NO DATA','u':None})
        svg=plot.render_svg([data],provider='claude')
        self.assertEqual(svg.count('data-kind="active"'),2)
        self.assertNotIn('NO DATA',svg)
        self.assertNotIn('data-provider="agy"',svg)
        self.assertNotIn('data-kind="inactive"',svg)

    def test_capacity_fails_explicitly_instead_of_clipping_or_dropping(self):
        data=self.portrait_sample()
        data['providers'][0]['w']*=5
        for i,w in enumerate(data['providers'][0]['w']):
            data['providers'][0]['w'][i]=dict(w,l='Window '+str(i))
        with self.assertRaisesRegex(ValueError,'capacity'):
            plot.render_svg([data])
        with self.assertRaisesRegex(ValueError,'Title is too long'):
            plot.render_svg([],title='long title '*100)
        self.assertEqual(plot._reset_in({'r':(NOW+timedelta(days=2,hours=3)).isoformat()},NOW),'Reset in 2d 3h')
        self.assertEqual(plot._reset_in({'r':(NOW-timedelta(seconds=1)).isoformat()},NOW),'Reset due')

    def test_original_identifier_mapping_alignment_geometry_and_no_external_references(self):
        import hashlib,re
        data=self.portrait_sample()
        data['providers'][0]['w'].append({'l':'5H','u':0,'p':0,'r':'2026-11-09T00:00:00Z'})
        svg=plot.render_svg([data]);root=plot._validate_svg(svg)
        logos=[e for e in root if e.get('data-role')=='provider-logo']
        providers=[e for e in root if e.get('data-role')=='provider']
        labels=[e for e in root if e.get('data-role')=='window']
        cards=[e for e in root if e.get('data-kind')=='active']
        self.assertEqual([e.get('data-logo') for e in logos],
                         ['grok','claude','claude','openai','claude','antigravity'])
        self.assertEqual([e.text for e in providers],['GROK','CLAUDE','CLAUDE','CODEX','FABLE','AGY'])
        for logo,provider,label,card in zip(logos,providers,labels,cards):
            name=logo.get('data-logo');x,y,w,h=[float(logo.get(k)) for k in ('x','y','width','height')]
            advance=sum(plot._metrics().get(str(ord(c)),1) for c in provider.text)*9
            self.assertAlmostEqual(x,float(provider.get('x'))+advance+2,places=5)
            self.assertAlmostEqual(y+h,float(provider.get('y')),places=5)
            self.assertEqual(h,7)
            _,_,vw,vh=plot._LOGO_SPECS[name][1]
            self.assertAlmostEqual(w/h,vw/vh,places=5)
            label_width=sum(plot._metrics().get(str(ord(c)),1) for c in label.text)*float(label.get('font-size'))
            self.assertLess(x+w,float(label.get('x'))-label_width)
            self.assertGreaterEqual(x,float(card.get('x')))
            self.assertGreaterEqual(y,float(card.get('y')))
            self.assertLessEqual(x+w,float(card.get('x'))+float(card.get('width')))
            raw=(plot.LOGO_DIR/(name+'.svg')).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),plot._LOGO_SPECS[name][0])
            original=ET.fromstring(raw)
            paths=lambda tree:[(e.get('d'),e.get('fill'),e.get('stroke')) for e in tree.iter() if e.tag.endswith('}path')]
            self.assertEqual(paths(logo),paths(original))
        self.assertNotIn('foreignObject',svg)
        self.assertNotIn('href=',svg)
        self.assertNotIn('<image',svg)
        self.assertNotIn('http',svg.replace('http://www.w3.org/2000/svg',''))
        self.assertNotIn('style=',svg)
        self.assertEqual(plot._provider_logo('future','FUTURE',0,14,0),'')

    def test_repeated_logo_ids_are_unique_local_and_cached_offline(self):
        import re
        data={'t':NOW.isoformat(),'providers':[{'n':n,'w':[{'l':l,'u':0,'p':0} for l in ('WEEK','EXTRA')]}
                                               for n in ('grok','codex')]}
        plot._logo_template.cache_clear()
        with patch('socket.socket',side_effect=AssertionError('network forbidden')):
            first=plot.render_svg([data])
            # Every logo has now been parsed. Repeated rendering must not reopen files.
            with patch.object(Path,'read_bytes',side_effect=AssertionError('logo was not cached')):
                self.assertEqual(plot.render_svg([data]),first)
        root=plot._validate_svg(first)
        ids=[e.get('id') for e in root.iter() if e.get('id')]
        self.assertEqual(len(ids),len(set(ids)))
        refs=re.findall(r'url\(#([^)]*)\)',first)
        self.assertTrue(set(refs)<=set(ids))
        self.assertTrue(all(i=='usage-fill' or i.startswith('logo-') for i in ids))
        self.assertEqual(plot._logo_template.cache_info().misses,2)

    def test_missing_modified_and_unsafe_logos_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as d:
            try:
                plot._logo_template.cache_clear()
                with patch.object(plot,'LOGO_DIR',Path(d)):
                    with self.assertRaisesRegex(RuntimeError,'missing'):
                        plot._logo_template('claude')
                    output=Path(d)/'existing.png';output.write_bytes(b'previous PNG')
                    with self.assertRaisesRegex(RuntimeError,'missing'):
                        plot.render_png([self.portrait_sample()],output)
                    self.assertEqual(output.read_bytes(),b'previous PNG')
                    (Path(d)/'claude.svg').write_text('<svg><script>unsafe</script></svg>')
                    with self.assertRaisesRegex(ValueError,'integrity'):
                        plot._logo_template('claude')
            finally:plot._logo_template.cache_clear()
        unsafe=['<?xml-stylesheet href="https://invalid/style.css"?><svg/>','<svg><foreignObject/></svg>','<svg><g style="fill:red"/></svg>',
                '<svg><g clip-path="URL(https://invalid/x)"/></svg>',
                '<svg><g clip-path="url(#missing)"/></svg>',
                '<svg><g id="duplicate"/><g id="duplicate"/></svg>',
                '<!DOCTYPE svg [<!ENTITY test "unsafe">]><svg/>',
                '<svg><use href="file:///tmp/private"/></svg>']
        for svg in unsafe:
            with self.subTest(svg=svg),self.assertRaises(ValueError):plot._validate_svg(svg)

    def test_logos_render_offline_in_default_and_lazy_fallback(self):
        import resvg_py
        from PIL import Image,ImageChops
        data=self.portrait_sample()
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'logos.png';plain=Path(d)/'plain.png'
            with patch('socket.socket',side_effect=AssertionError('network forbidden')):
                plot.render_png([data],path)
                with patch.object(plot,'_provider_logo',return_value=''):
                    plot.render_png([data],plain)
            full=Image.open(path).convert('RGB');without=Image.open(plain).convert('RGB')
            logos=[e for e in ET.fromstring(plot.render_svg([data])) if e.get('data-role')=='provider-logo']
            for node in logos:
                x,y,w,h=[float(node.get(k))*3 for k in ('x','y','width','height')]
                box=(int(x),int(y),int(x+w)+1,int(y+h)+1)
                self.assertIsNotNone(ImageChops.difference(full.crop(box),without.crop(box)).getbbox())
            with patch.object(resvg_py,'svg_to_bytes',side_effect=ValueError('offline fallback')), \
                 patch.object(plot,'_fallback_logo_png',wraps=plot._fallback_logo_png) as draw, \
                 self.assertWarns(RuntimeWarning):
                plot.render_png([data],path)
            self.assertEqual([c.args[0] for c in draw.call_args_list],['grok','claude','openai','claude','antigravity'])
            with Image.open(path) as rendered:
                self.assertEqual(rendered.size,(1179,2556))
                # Neutral original identifier color is retained in fallback.
                colors=rendered.convert('RGB').getcolors(rendered.width*rendered.height)
                self.assertIn((239,252,255),[color for count,color in colors])

    def test_reject_active_or_external_svg(self):
        for body in ['<image href="https://invalid/x"/>','<text href="file:///tmp/x">x</text>',
                     '<rect fill="url(https://invalid/x)"/>','<text onclick="x">x</text>']:
            with self.assertRaises(ValueError):plot._validate_svg('<svg>'+body+'</svg>')

    def test_bundled_font_covers_thai_and_latin_and_matches_metrics(self):
        from fontTools.ttLib import TTFont
        f=TTFont(plot.FONT); cmap=f.getBestCmap()
        self.assertTrue(all(ord(c) in cmap for c in 'ยังไม่เริ่ม Usage Pace ICT 0123456789 – — · % / & < >'))
        self.assertIn('SIL OPEN FONT LICENSE',plot.FONT.with_name('OFL.txt').read_text())
        for c in 'กมA01':
            self.assertAlmostEqual(plot._metrics()[str(ord(c))],f['hmtx'][cmap[ord(c)]][0]/f['head'].unitsPerEm,places=4)

    def test_explicit_font_options_real_png_and_no_matplotlib(self):
        import resvg_py
        with tempfile.TemporaryDirectory() as d, patch.object(resvg_py,'svg_to_bytes',wraps=resvg_py.svg_to_bytes) as render:
            path=plot.render_png([sample()],Path(d)/'chart.png',title='ยังไม่เริ่ม / Usage')
            self.assertEqual(path.read_bytes()[:8],b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II',path.read_bytes()[16:24]),(1179,2556))
            self.assertTrue(render.call_args.kwargs['skip_system_fonts'])
            self.assertEqual(render.call_args.kwargs['font_files'],[str(plot.FONT)])
        code='import plot_png,sys,tempfile; from pathlib import Path; from datetime import datetime,timezone; '+\
             'samples=[{"t":datetime.now(timezone.utc).isoformat(),"providers":[{"n":"claude","w":[{"l":"ALL WEEK","u":0,"p":1}]}]}]; '+\
             'plot_png.render_png(samples,Path(tempfile.mkdtemp())/"x.png"); '+\
             'assert not any(n=="matplotlib" or n.startswith("matplotlib.") for n in sys.modules)'
        subprocess.run([sys.executable,'-c',code],check=True,capture_output=True)

    def test_lazy_fallback_missing_dependency_and_render_error(self):
        import resvg_py
        import plot_matplotlib
        for failure in [ImportError('offline missing'),ValueError('render failed')]:
            with tempfile.TemporaryDirectory() as d, patch.object(resvg_py,'svg_to_bytes',side_effect=failure), \
                 patch.object(plot_matplotlib,'rasterize',wraps=plot_matplotlib.rasterize) as fallback, self.assertWarns(RuntimeWarning):
                out=plot.render_png([sample()],Path(d)/'x.png')
                self.assertTrue(out.read_bytes().startswith(b'\x89PNG'))
                self.assertEqual(struct.unpack('>II',out.read_bytes()[16:24]),(1179,2556))
                self.assertEqual(fallback.call_count,1)

    def test_failure_preserves_existing_output_and_cleans_temp(self):
        import resvg_py
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'x.png';path.write_bytes(b'previous')
            with patch.object(resvg_py,'svg_to_bytes',return_value=b'bad'),self.assertRaises(RuntimeError):
                plot.render_png([],path)
            self.assertEqual(path.read_bytes(),b'previous')
            with patch.object(plot.os,'replace',side_effect=OSError('fail')),self.assertRaises(OSError):
                plot.atomic_write(path,b'new')
            self.assertEqual(list(Path(d).iterdir()),[path])
            with patch.object(plot,'FONT',Path(d)/'missing.ttf'),self.assertRaises(RuntimeError):
                plot.render_png([],path)
            with patch.object(resvg_py,'svg_to_bytes',side_effect=ValueError('failed')), \
                 patch('plot_matplotlib.rasterize',side_effect=RuntimeError('failed')), \
                 self.assertWarns(RuntimeWarning),self.assertRaises(RuntimeError):
                plot.render_png([],path)
            self.assertEqual(path.read_bytes(),b'previous')


    def test_mcp_failed_render_preserves_alias(self):
        import mcp_server
        with tempfile.TemporaryDirectory() as d:
            alias=Path(d)/'tokencheck-usage-pace.png';alias.write_bytes(b'previous')
            with patch.object(mcp_server,'STATE',d),patch.object(mcp_server,'get',return_value={}), \
                 patch.object(mcp_server.hist,'load_samples',return_value=[]), \
                 patch.object(plot,'render_png',side_effect=RuntimeError('render failed')) as render, \
                 patch.object(plot,'atomic_write') as copy,self.assertRaises(RuntimeError):
                mcp_server.tokencheck_png()
            self.assertEqual(render.call_count,1)
            copy.assert_not_called()
            self.assertEqual(alias.read_bytes(),b'previous')

if __name__=='__main__':unittest.main()
