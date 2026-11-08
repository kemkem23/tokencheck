"""Offline entrypoint tests. Every daemon starter is a temporary stub."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parent

class WrapperLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mcp-entrypoint-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source with spaces'
        self.root.mkdir()
        self.wrapper = self.root / 'run-mcp.sh'
        wrapper = (SOURCE / 'run-mcp.sh').read_text()
        self.wrapper.write_text(wrapper)
        self.wrapper.chmod(0o755)
        # Preserve the interpreter's venv, not just its symlink to system Python.
        (self.root / 'venv').symlink_to(Path(sys.prefix), target_is_directory=True)
        self.starter = self.root / 'start-daemon.sh'
        self.starter.write_text('#!/bin/bash\necho starter-stdout\necho starter-stderr >&2\nexit 7\n')
        self.starter.chmod(0o755)
        (self.root / 'mcp_server.py').write_text(
            'import json,sys\nprint(json.dumps({"input":sys.stdin.read(),"args":sys.argv[1:]}))\n')

    def launch(self, argv, env, cwd='/tmp'):
        return subprocess.run(argv, env=env, cwd=cwd, input='stdin survives\n',
                              text=True, capture_output=True, timeout=5)

    def test_absolute_wrapper_and_explicit_bash_without_path_tools(self):
        for argv in ([str(self.wrapper)], ['/bin/bash', str(self.wrapper)]):
            with self.subTest(argv=argv):
                result = self.launch(argv, {'PATH':'/nonexistent','LC_ALL':'C.UTF-8'})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {'input':'stdin survives\n','args':[]})
                self.assertEqual(result.stderr.splitlines(), ['starter-stdout','starter-stderr'])

    def test_relative_wrapper_with_empty_environment(self):
        result = self.launch(['/bin/bash','run-mcp.sh'], {}, cwd=str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['input'], 'stdin survives\n')

    def test_cdpath_cannot_pollute_library_path_or_stdout(self):
        result = self.launch(['/bin/bash','source with spaces/run-mcp.sh'],
                             {'PATH':'/nonexistent','CDPATH':self.temp.name}, cwd=self.temp.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['input'], 'stdin survives\n')

    def test_server_import_has_no_plotting_or_collector_imports(self):
        code = '''import importlib.abc, sys, json
class Reject(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0] in {'plot_png','plot_matplotlib','matplotlib','resvg_py','collectors','pacer_daemon'}:
   raise AssertionError('Unexpected eager import: '+fullname)
sys.meta_path.insert(0, Reject())
sys.path[:0] = sys.argv[1:]
import mcp_server
print(json.dumps(sorted(mcp_server.mcp._tool_manager._tools)))
'''
        config = Path(self.temp.name) / 'config.json'
        config.write_text(json.dumps({'state_dir':str(Path(self.temp.name)/'state'),'enabled_providers':[]}))
        home = Path(self.temp.name) / 'home'
        home.mkdir()
        import_source = Path(self.temp.name) / 'import-source'
        import_source.mkdir()
        server = (SOURCE / 'mcp_server.py').read_text()
        (import_source / 'mcp_server.py').write_text(server)
        result = subprocess.run([sys.executable,'-c',code,str(import_source),str(SOURCE)], cwd='/tmp',
                                env={'HOME':str(home),'PACER_CONFIG':str(config),
                                     'PYTHONDONTWRITEBYTECODE':'1','LC_ALL':'C.UTF-8'},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout), sorted([
            'tokencheck_chart','tokencheck_minimal','tokencheck_png','tokencheck_snapshot','tokencheck_methods']))
        self.assertEqual(result.stderr,'')

if __name__ == '__main__':
    unittest.main()
