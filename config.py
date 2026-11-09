"""Validated shared configuration. Loading reads only the selected config file."""
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
import tomllib

PROVIDERS = ('grok', 'claude', 'codex', 'agy')

@dataclass(frozen=True)
class Config:
    state_dir: Path
    port: int = 48829
    poll_interval: int = 60
    oauth_interval: int = 600
    claude_config_dir: Path = None
    enabled_providers: tuple = ()
    grok_tmux_pane: str = 'pacer-grokusage'
    allowed_hosts: tuple = ()
    claude_auto_refresh: bool = True

    def json(self):
        return dict(asdict(self), state_dir=str(self.state_dir), claude_config_dir=str(self.claude_config_dir))


def config_path():
    return Path(os.environ.get('PACER_CONFIG', '~/.config/pacer-tokencheck/config.toml')).expanduser()


def load_config(path=None):
    selected = Path(path).expanduser() if path else config_path()
    raw = {}
    if selected.exists():
        with selected.open('rb') as stream:
            raw = json.load(stream) if selected.suffix == '.json' else tomllib.load(stream)
    elif path or 'PACER_CONFIG' in os.environ:
        raise ValueError('selected config does not exist')
    if not isinstance(raw, dict) or set(raw) - set(Config.__dataclass_fields__):
        raise ValueError('unknown configuration keys')
    # Compatibility overrides win over file values; defaults remain HOME based.
    if os.environ.get('PACER_STATE_DIR'):
        raw['state_dir'] = os.environ['PACER_STATE_DIR']
    if os.environ.get('PACER_CLAUDE_CONFIG_DIR'):
        raw['claude_config_dir'] = os.environ['PACER_CLAUDE_CONFIG_DIR']
    for key in ('state_dir','claude_config_dir'):
        if key in raw and (not isinstance(raw[key],str) or not raw[key]):
            raise ValueError(f'invalid {key}')
    state = Path(raw.get('state_dir', '~/.local/share/pacer-tokencheck')).expanduser()
    isolated = Path(raw.get('claude_config_dir', state / 'claude-usage-config')).expanduser()
    if not state.is_absolute() or not isolated.is_absolute():
        raise ValueError('configuration paths must be absolute or HOME based')
    if isolated.resolve() == (Path.home()/'.claude').resolve():
        raise ValueError('Claude usage config must be isolated from default login')
    raw.update(state_dir=state, claude_config_dir=isolated)
    for key, default, low, high in [('port',48829,1,65535), ('poll_interval',60,60,86400), ('oauth_interval',600,600,86400*7)]:
        val=raw.get(key,default)
        if type(val) is not int or not low <= val <= high:
            raise ValueError(f'invalid {key}')
    for key, choices in [('enabled_providers',PROVIDERS), ('allowed_hosts',None)]:
        value=raw.get(key, [])
        if not isinstance(value,(list,tuple)) or any(not isinstance(x,str) or not x or (choices and x not in choices) for x in value) or len(set(value)) != len(value):
            raise ValueError(f'invalid {key}')
        if key=='allowed_hosts' and any(any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:' for c in x) for x in value):
            raise ValueError('invalid allowed_hosts')
        raw[key]=tuple(value)
    if type(raw.get('claude_auto_refresh', True)) is not bool:
        raise ValueError('invalid claude_auto_refresh')
    pane=raw.get('grok_tmux_pane','pacer-grokusage')
    if not isinstance(pane,str) or not pane or pane.startswith('-') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:%' for c in pane):
        raise ValueError('invalid grok_tmux_pane')
    return Config(**raw)

if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Validate and print effective configuration without collecting usage')
    parser.add_argument('--field',choices=Config.__dataclass_fields__)
    args=parser.parse_args()
    try:
        result=load_config().json()
        print(result[args.field] if args.field else json.dumps(result,indent=2))
    except (ValueError,OSError) as error:
        parser.exit(64, f'configuration error: {error}\n')
