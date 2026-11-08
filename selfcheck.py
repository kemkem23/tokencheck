"""Read-only config and local HTTP readiness check; never invokes collectors."""
import json
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from config import load_config

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def check(opener=None):
    try:
        config=load_config()
        result={'config_valid':True,'state_dir':str(config.state_dir),'port':config.port,'ready':False}
    except (ValueError,OSError) as error:
        return {'config_valid':False,'ready':False,'error':str(error)},64
    opener=opener or build_opener(ProxyHandler({}),NoRedirect())
    try:
        def get(route):
            request=Request(f'http://127.0.0.1:{config.port}{route}',headers={'Accept':'application/json'})
            with opener.open(request,timeout=3) as response:
                if response.status != 200: raise ValueError('daemon HTTP status')
                return json.loads(response.read(2*1024*1024))
        health=get('/healthz')
        status=get('/v1/collector')
        result.update(ready=health.get('ready') is True, health=health, providers=status.get('providers',{}))
        return result,0 if result['ready'] else 1
    except Exception as error:
        result['error']=error.__class__.__name__
        return result,1
