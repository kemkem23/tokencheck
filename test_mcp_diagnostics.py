"""MCP imports and wrapper launches have no diagnostic file side effects."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parent


class NoDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='mcp-no-diagnostics-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        config = self.root / 'config.json'
        config.write_text(json.dumps({'state_dir': str(self.home / 'state'), 'enabled_providers': []}))
        self.env = {'HOME': str(self.home), 'PACER_CONFIG': str(config),
                    'PYTHONPATH': str(SOURCE), 'PYTHONDONTWRITEBYTECODE': '1',
                    'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}

    def test_import_does_not_write_logs_create_state_or_replace_exception_hook(self):
        code = '''import sys, logging
before = sys.excepthook
import mcp_server
assert sys.excepthook is before
assert not any(isinstance(h, logging.FileHandler)
               for log in logging.Logger.manager.loggerDict.values()
               if isinstance(log, logging.Logger) for h in log.handlers)
'''
        result = subprocess.run([sys.executable, '-c', code], env=self.env, cwd='/tmp',
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, result.stderr), (b'', b''))
        self.assertEqual(list(self.home.rglob('*')), [])

    def test_wrapper_has_no_diagnostic_writes_and_preserves_stdio_on_starter_failure(self):
        source = self.root / 'source'
        source.mkdir()
        wrapper = source / 'run-mcp.sh'
        wrapper.write_bytes((SOURCE / 'run-mcp.sh').read_bytes())
        wrapper.chmod(0o755)
        (source / 'venv').symlink_to(sys.prefix, target_is_directory=True)
        starter = source / 'start-daemon.sh'
        starter.write_text('#!/bin/bash\nprintf "starter-output\\n"\nexit 7\n')
        starter.chmod(0o755)
        (source / 'mcp_server.py').write_text('import sys; sys.stdout.write(sys.stdin.read())\n')
        before = set(self.root.rglob('*'))
        result = subprocess.run([str(wrapper)], env=self.env, cwd='/tmp',
                                input=b'client-input\n', capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'client-input\n')
        self.assertEqual(result.stderr, b'starter-output\n')
        self.assertEqual(set(self.root.rglob('*')), before)


if __name__ == '__main__':
    unittest.main()
