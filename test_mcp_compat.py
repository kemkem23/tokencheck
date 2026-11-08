"""Offline compact-catalog regressions using an isolated server copy."""
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SOURCE = Path(__file__).resolve().parent
PROVIDER = {"default": "all", "enum": ["all", "claude", "codex", "grok", "agy"],
            "type": "string"}
SCHEMAS = {
    "tokencheck_chart": {"properties": {"provider": PROVIDER,
                                         "width": {"default": 48, "type": "integer"}},
                         "type": "object"},
    "tokencheck_minimal": {"properties": {"provider": PROVIDER}, "type": "object"},
    "tokencheck_png": {"properties": {"provider": PROVIDER,
                                       "hours": {"default": 24.0, "type": "number"}},
                       "type": "object"},
    "tokencheck_snapshot": {"properties": {"provider": PROVIDER,
                                            "full": {"default": False, "type": "boolean"}},
                            "type": "object"},
    "tokencheck_methods": {"properties": {}, "type": "object"},
}


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mcp-compat-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.config = self.root / 'fixture-config.json'
        self.config.write_text('{"enabled_providers": []}')
        server = (SOURCE / "mcp_server.py").read_text()
        (self.root / "mcp_server.py").write_text(server)
        (self.root / "config.py").write_text(
            "from pathlib import Path\nfrom types import SimpleNamespace\n"
            "def load_config():\n"
            f" return SimpleNamespace(port=9, state_dir=Path({str(self.root / 'state')!r}))\n"
        )
        (self.root / "history.py").write_text(
            "def load_samples(*args, **kwargs):\n"
            " raise AssertionError('History must not be read in compatibility tests')\n"
        )

    def run_async(self, body):
        code = """import asyncio, contextlib, importlib.abc, io, json, sys
from types import SimpleNamespace
from unittest import mock
import anyio
from mcp import types
from mcp.server.fastmcp import FastMCP
class RejectImports(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0] in {'collectors', 'pacer_daemon', 'plot_png', 'plot_matplotlib', 'matplotlib', 'resvg_py'}:
   raise AssertionError('Unexpected provider/renderer import: ' + fullname)
sys.meta_path.insert(0, RejectImports())
import mcp_server as server
async def main():
""" + textwrap.indent(textwrap.dedent(body).strip(), " ") + "\nasyncio.run(main())\n"
        result = subprocess.run(
            [sys.executable, "-c", code], cwd="/tmp",
            env={"HOME": str(self.home), "PACER_CONFIG": str(self.config),
                 "PYTHONPATH": str(self.root), "PYTHONDONTWRITEBYTECODE": "1",
                 "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, result.stderr), ("", ""))

    def test_catalog_preserves_five_argument_schemas_without_optional_metadata(self):
        self.run_async(f"""
            expected = {SCHEMAS!r}
            tools = await server.mcp.list_tools()
            assert isinstance(server.mcp, FastMCP)
            assert len(tools) == 5
            assert {{tool.name for tool in tools}} == set(expected)
            for tool in tools:
                assert type(tool) is types.Tool
                wire = tool.model_dump(mode='json', by_alias=True, exclude_none=True)
                assert set(wire) == {{'name', 'description', 'inputSchema'}}, wire
                assert wire['inputSchema'] == expected[tool.name], wire
                assert wire['description'] and '\\n' not in wire['description']
                assert len(wire['description']) < 200
        """)

    def test_all_tool_calls_are_text_only_and_snapshot_text_is_unchanged(self):
        self.run_async(f"""
            handler = server.mcp._mcp_server.request_handlers[types.CallToolRequest]
            payload = 'private-compat-payload π\\t{{"value": 1}}\\n'
            for name in {list(SCHEMAS)!r}:
                tool = server.mcp._tool_manager._tools[name]
                with mock.patch.object(tool, 'fn', new=lambda **kwargs: payload):
                    result = await handler(types.CallToolRequest(
                        params=types.CallToolRequestParams(name=name, arguments={{}})))
                wire = result.model_dump(mode='json', by_alias=True, exclude_none=True)
                assert wire == {{'content': [{{'type': 'text', 'text': payload}}], 'isError': False}}, wire
            data = {{'private-compat-payload': 'π', 'values': [1, None]}}
            with mock.patch.object(server, 'get', return_value=data) as get:
                result = await handler(types.CallToolRequest(params=types.CallToolRequestParams(
                    name='tokencheck_snapshot', arguments={{'provider': 'codex'}})))
                get.assert_called_once_with('/v1/tokencheck?provider=codex')
            wire = result.model_dump(mode='json', by_alias=True, exclude_none=True)
            assert wire == {{'content': [{{'type': 'text', 'text': json.dumps(data, ensure_ascii=False, indent=1)}}],
                            'isError': False}}, wire
            assert not (server.LIB / 'host.log').exists()
        """)

    def test_transport_uses_unmodified_sdk_stdio(self):
        self.run_async("""
            assert type(server.mcp).run_stdio_async is FastMCP.run_stdio_async
            assert server.mcp.name == 'tokencheck'
        """)


if __name__ == '__main__':
    unittest.main()
