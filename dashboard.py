"""Read-only live dashboard and bounded history API; stdlib only."""
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl
import history
import gzip
import hashlib
from history_cache import cache_for

def _history_data(samples, hours, step):
    now = time.time()
    buckets = {}
    earliest = latest = None
    for sample in samples:
        try:
            ts = datetime.fromisoformat(sample['t'].replace('Z','+00:00')).timestamp()
            if not now-7*86400 <= ts <= now: continue
        except (KeyError, ValueError, TypeError): continue
        earliest = ts if earliest is None else min(earliest,ts)
        latest = ts if latest is None else max(latest,ts)
        if ts < now-hours*3600: continue
        providers = sample.get('providers')
        for provider in providers if isinstance(providers,list) else []:
            if not isinstance(provider,dict) or not isinstance(provider.get('n'),str): continue
            windows = provider.get('w')
            for window in windows if isinstance(windows,list) else []:
                if not isinstance(window,dict) or not isinstance(window.get('l'),str): continue
                u,p=window.get('u'),window.get('p')
                if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (u,p)): continue
                label=window['l']
                if provider['n']=='claude' and label=='WEEK': label='ALL WEEK'
                key=(provider['n'],label)
                bucket=int(ts)//step
                dest=buckets.setdefault(key,{})
                if bucket not in dest or ts>=dest[bucket][0]:
                    dest[bucket]=(ts,u,p)
    return {'hours':hours,'step':step,'generated_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
            'history_hours':round((latest-earliest)/3600,2) if earliest is not None else 0,
            'series':[{'provider':key[0],'window':key[1],'points':[{'t':datetime.fromtimestamp(v[0],timezone.utc).isoformat().replace('+00:00','Z'),'used':v[1],'pace':v[2]} for _,v in sorted(values.items())]} for key,values in buckets.items()]}

def history_data(state, hours, step):
    path = history.history_path(state)
    return cache_for(path).get(path, (hours,step), lambda samples: _history_data(samples,hours,step))


def embedded_dashboard(engine):
    # Published generations are immutable. Read under the engine lock; no collection.
    with engine.lock:
        published = engine.published
    envelope = published['envelope'] if published else None
    snapshot = {'results': {'all': envelope['results']['all']}} if envelope else None
    payload = json.dumps(snapshot, allow_nan=False, separators=(',',':'), ensure_ascii=True)
    # Escape HTML delimiters, including </script>, and JS line separators.
    payload = payload.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    return (Path(__file__).parent/'dashboard.html').read_bytes().replace(b'__PACER_SNAPSHOT__', payload.encode())


def send_cached(handler, body, content_type, head=False):
    accepted = handler.headers.get('Accept-Encoding', '')
    encodings = {}
    for item in accepted.lower().split(','):
        fields = item.strip().split(';')
        try:
            quality = next((float(x.strip()[2:]) for x in fields[1:] if x.strip().startswith('q=')), 1.0)
        except ValueError:
            quality = 0
        encodings[fields[0]] = quality
    compressed = encodings.get('gzip', encodings.get('*',0)) > 0
    if compressed: body = gzip.compress(body, mtime=0)
    etag = '"' + hashlib.sha256(body).hexdigest() + '"'
    matches = handler.headers.get('If-None-Match','').split(',')
    unchanged = any(x.strip().removeprefix('W/') in (etag,'*') for x in matches)
    handler.send_response(304 if unchanged else 200)
    headers = {'Content-Type': content_type, 'Cache-Control':'private, max-age=0, must-revalidate',
               'ETag':etag, 'Vary':'Accept-Encoding', 'X-Content-Type-Options':'nosniff'}
    # On 304 Content-Length describes the selected 200 representation, per HTTP.
    headers['Content-Length'] = str(len(body))
    if compressed: headers['Content-Encoding']='gzip'
    if content_type.startswith('text/html'):
        headers['Content-Security-Policy'] = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
    for key,value in headers.items(): handler.send_header(key,value)
    handler.end_headers()
    if not head and not unchanged: handler.wfile.write(body)


def handle(handler, head=False):
    parts=urlsplit(handler.path)
    if parts.path not in ('/','/dashboard','/v1/history'): return False
    # Preserve the base handler's admission and DNS-rebinding/body guards.
    if not handler.admitted: handler.send_error_json(503,'busy',head=head); return True
    if len(handler.requestline)>8192: handler.send_error_json(414,'uri_too_long',head=head); return True
    if not handler.host_allowed(): handler.send_error_json(421,'host_not_allowed',head=head); return True
    if parts.scheme or parts.netloc or parts.fragment or handler.headers.get('Transfer-Encoding') or handler.headers.get('Content-Length','0').strip() not in ('','0'):
        handler.send_error_json(400,'invalid_request',head=head); return True
    try:
        query=parse_qsl(parts.query,keep_blank_values=True,strict_parsing=True,max_num_fields=2)
        if parts.path!='/v1/history':
            if query: raise ValueError()
            body=embedded_dashboard(handler.server.engine)
            send_cached(handler,body,'text/html; charset=utf-8',head)
        else:
            q=dict(query)
            if len(q)!=len(query) or set(q)-{'hours','step'}: raise ValueError()
            hours=int(q.get('hours',168))
            if hours not in (24,72,168): raise ValueError()
            step=int(q.get('step',{24:300,72:900,168:1800}[hours]))
            if not 60<=step<=86400: raise ValueError()
            result=history_data(handler.server.engine.state_dir,hours,step)
            send_cached(handler,json.dumps(result,allow_nan=False,separators=(',',':')).encode(),'application/json; charset=utf-8',head)
    except ValueError: handler.send_error_json(400,'invalid_query',head=head)
    return True
