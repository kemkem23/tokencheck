"""Offline starter regressions; every executable and state file is isolated."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parent


class StarterLaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mcp-starter-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "source with spaces"
        self.root.mkdir()
        self.state = self.base / "state with spaces"
        self.state.mkdir()
        self.pid = os.getpid()
        (self.state / "daemon.pid").write_text(str(self.pid))
        self.calls = self.base / "calls.jsonl"

        for name in ("start-daemon.sh", "run-mcp.sh"):
            content = (SOURCE / name).read_text()
            target = self.root / name
            target.write_text(content)
            target.chmod(0o755)

        python = self.root / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(
            f"#!{sys.executable}\n"
            "import json, runpy, sys\n"
            f"with open({str(self.calls)!r}, 'a') as output:\n"
            " output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "sys.argv = sys.argv[1:]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n"
        )
        python.chmod(0o755)
        (self.root / "config.py").write_text(
            "import sys\n"
            "assert sys.argv[1:] == ['--field', 'state_dir']\n"
            f"print({str(self.state)!r})\n"
        )
        (self.root / "mcp_server.py").write_text(
            "import json, sys\n"
            "print(json.dumps({'input': sys.stdin.read(), 'args': sys.argv[1:]}))\n"
        )
        # These harmless stubs make an accidental start/readiness check observable.
        for name in ("pacer_daemon.py", "tokencheck.py"):
            (self.root / name).write_text("raise SystemExit(91)\n")

    def assert_launch(self, entrypoint, env, *, bash=False, relative=False):
        self.calls.unlink(missing_ok=True)
        script = self.root / entrypoint
        cwd = self.base if relative else Path("/tmp")
        argument = str(script.relative_to(cwd)) if relative else str(script)
        argv = (["/bin/bash"] if bash else []) + [argument]
        result = subprocess.run(
            argv, cwd=cwd, env=env, input="stdin survives\n", text=True,
            capture_output=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        running = f"pacer-tokencheck already running pid={self.pid}\n"
        if entrypoint == "run-mcp.sh":
            self.assertEqual(
                json.loads(result.stdout), {"input": "stdin survives\n", "args": []},
            )
            self.assertEqual(result.stderr, running)
            expected = ["config.py", "mcp_server.py"]
        else:
            self.assertEqual(result.stdout, running)
            self.assertEqual(result.stderr, "")
            expected = ["config.py"]
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([Path(call[0]).name for call in calls], expected)
        self.assertEqual((self.state / "daemon.pid").read_text(), str(self.pid))
        self.assertFalse((self.state / "daemon.stderr").exists())

    def test_direct_starter_with_minimal_and_empty_path(self):
        for path in ("/nonexistent", ""):
            with self.subTest(path=path):
                self.assert_launch("start-daemon.sh", {"PATH": path})

    def test_explicit_bash_starter_with_minimal_and_empty_path(self):
        for path in ("/nonexistent", ""):
            with self.subTest(path=path):
                self.assert_launch("start-daemon.sh", {"PATH": path}, bash=True)

    def test_registered_direct_wrapper_with_minimal_and_empty_path(self):
        for path in ("/nonexistent", ""):
            with self.subTest(path=path):
                self.assert_launch("run-mcp.sh", {"PATH": path})

    def test_entrypoints_with_empty_environment(self):
        for entrypoint in ("start-daemon.sh", "run-mcp.sh"):
            for bash in (False, True):
                with self.subTest(entrypoint=entrypoint, bash=bash):
                    self.assert_launch(entrypoint, {}, bash=bash)

    def test_relative_entrypoints_ignore_cdpath(self):
        for entrypoint in ("start-daemon.sh", "run-mcp.sh"):
            for bash in (False, True):
                with self.subTest(entrypoint=entrypoint, bash=bash):
                    self.assert_launch(
                        entrypoint, {"PATH": "/usr/bin:/bin", "CDPATH": str(self.base)},
                        bash=bash, relative=True,
                    )


if __name__ == "__main__":
    unittest.main()
