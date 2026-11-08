"""Reproducible user installation; dry-run needs no network or writes."""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from config import load_config, config_path

LIB=Path(__file__).resolve().parent

def backup(path):
    if path.exists():
        dest=path.with_name(path.name+'.bak-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f'))
        shutil.copy2(path,dest)

def install(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--offline',action='store_true',help='use already installed venv dependencies; never download')
    parser.add_argument('--tailscale-serve',action='store_true',help='explicitly configure optional tailnet TCP forward')
    parser.add_argument('--claude-statusline',action='store_true',help='opt in to wrapping the local Claude statusline')
    parser.add_argument('--claude-settings',type=Path,help='settings file to wrap; default ~/.claude/settings.json')
    args=parser.parse_args(argv)
    selected=config_path()
    # An explicitly selected new path is valid for installation; create defaults there.
    config=load_config() if selected.exists() else load_config_default()
    unit=Path.home()/'.config/systemd/user/pacer-tokencheck.service'
    plan={'config':str(selected),'state_dir':str(config.state_dir),'port':config.port,
          'venv':str(LIB/'venv'),'unit':str(unit),'claude_settings':str(args.claude_settings or Path.home()/'.claude/settings.json') if (args.claude_statusline or args.claude_settings) else None,
          'tailscale_serve':args.tailscale_serve,'daemon_started':False}
    if args.dry_run:
        print(json.dumps(plan,indent=2));return 0
    selected.parent.mkdir(parents=True,exist_ok=True)
    if not selected.exists():
        content=config.json()
        if selected.suffix=='.json':selected.write_text(json.dumps(content,indent=2)+'\n')
        else:
            selected.write_text('\n'.join(f'{k} = {json.dumps(v)}' for k,v in content.items())+'\n')
        selected.chmod(0o600)
    python=LIB/'venv/bin/python'
    if not python.exists():
        subprocess.run([sys.executable,'-m','venv',str(LIB/'venv')],check=True)
    if args.offline:
        subprocess.run([str(python),'-c','import mcp, matplotlib, resvg_py'],check=True)
    else:
        subprocess.run([str(python),'-m','pip','install','-r',str(LIB/'requirements.txt')],check=True)
    config.state_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    if args.claude_statusline or args.claude_settings:
        from install_claude_statusline import install as wrap
        wrap(args.claude_settings or Path.home()/'.claude/settings.json')
    unit.parent.mkdir(parents=True,exist_ok=True)
    backup(unit)
    def quote(value):return '"'+str(value).replace('%','%%').replace('\\','\\\\').replace('"','\\"')+'"'
    env={'PACER_CONFIG':str(selected),'PACER_STATE_DIR':str(config.state_dir),'PACER_CLAUDE_CONFIG_DIR':str(config.claude_config_dir)}
    unit.write_text('[Unit]\nDescription=Local quota and pace monitor\nAfter=network.target\n\n[Service]\nType=simple\n'+
        ''.join('Environment='+quote(k+'='+v)+'\n' for k,v in env.items())+
        'Environment='+quote('PATH='+str(Path.home()/'.local/bin')+':'+str(Path.home()/'.grok/bin')+':/usr/local/bin:/usr/bin:/bin')+'\n'+
        'ExecStart='+quote(python)+' '+quote(LIB/'pacer_daemon.py')+'\nRestart=on-failure\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n')
    if args.tailscale_serve:
        subprocess.run(['tailscale','serve','--bg',f'--tcp={config.port}',f'tcp://127.0.0.1:{config.port}'],check=True)
    print(json.dumps(plan,indent=2))
    print('To start: systemctl --user daemon-reload && systemctl --user enable --now pacer-tokencheck.service')
    return 0

def load_config_default():
    # Validate compatibility overrides without requiring the new file to exist.
    previous=os.environ.pop('PACER_CONFIG',None)
    try:
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'defaults.json';path.write_text('{}')
            return load_config(path)
    finally:
        if previous is not None:os.environ['PACER_CONFIG']=previous

if __name__=='__main__':
    raise SystemExit(install())
