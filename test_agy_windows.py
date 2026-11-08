"""Retained AGY window regression, adapted to provider modules and unittest."""
import unittest

class AGYWindowsTests(unittest.TestCase):
    def test_extra_buckets_and_idle_windows(self):
        import core
        from providers import agy
        from datetime import datetime,timezone,timedelta
        now=datetime.now(timezone.utc)
        buckets=[dict(id=id,window=win,remaining_fraction=1,reset_time=(now+timedelta(hours=h)).isoformat()) for id,win,label,h in core.AGY_BUCKETS]
        buckets += [dict(id='gemini-argon-wtu-weekly',name='Gemini 4 Argon Weekly Limit',remaining_fraction=.9947,reset_time=(now+timedelta(hours=167)).isoformat()),dict(id='unknown',name='Unknown',remaining_fraction=.9,reset_time=now.isoformat()),dict(id='other-5h',remaining_fraction=.7,reset_time=(now+timedelta(hours=4)).isoformat())]
        p=agy.parse_agy(dict(status='SUCCESS',command=dict(name='usage',data=dict(groups=[dict(buckets=buckets)]))),now,'test',core)
        assert len(p['windows'])==6
        for w in p['windows']:
         a,e=core.analyze_window(w,now,'agy',p['measured_at']);assert not e
         if w['label'] in ['GEM 5H','3P 5H','GEM WEEK','3P WEEK']:
          assert a['pace_percent'] is None and a['started'] is False
         elif w['label']=='ARGON WEEK':
          assert a['used_percent']==.53 and a['pace_percent']>0
        for name in ['claude','codex','grok']:
         a,e=core.analyze_window(dict(label='WEEK',window_hours=168,used_percent=0,resets_at=(now+timedelta(hours=168)).isoformat()),now,name,now.isoformat())
         assert a['pace_percent']==0 and a['started'] is True
