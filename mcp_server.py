#!/usr/bin/env python3
"""pacer-tokencheck MCP (stdio). Read-only client of the local daemon on
127.0.0.1 at the configured port. Never contacts a provider itself.

Output modes:
  tokencheck_chart    – human pace bars (existing)
  tokencheck_minimal  – stable TSV (one row per window) + json\t rows
  tokencheck_png      – dark usage vs recorded pace PNG
  tokencheck_snapshot – raw JSON
  tokencheck_methods  – collector winners / health
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import Tool as MCPTool


class _CompatibilityMCP(FastMCP):
    """Compact catalog for clients that require name/description/inputSchema only."""

    async def list_tools(self):
        result = []
        for tool in await super().list_tools():
            schema = deepcopy(tool.inputSchema)
            # Current arguments are scalar fields; remove their display titles
            # without changing property names, types, defaults or enum values.
            schema.pop("title", None)
            for field in schema.get("properties", {}).values():
                field.pop("title", None)
            result.append(MCPTool(name=tool.name, description=tool.description,
                                  inputSchema=schema))
        return result

LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))

import history as hist  # noqa: E402

from config import load_config
CONFIG = load_config()
BASE = f"http://127.0.0.1:{CONFIG.port}"
STATE = str(CONFIG.state_dir)
Provider = Literal["all", "claude", "codex", "grok", "agy"]
mcp = _CompatibilityMCP("tokencheck")
ICT = timezone(timedelta(hours=7), name="ICT")


def get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    from selfcheck import NoRedirect
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=5) as r:
        return json.loads(r.read())


def _fmt_pct(value, *, decimals: int) -> str:
    """Stable numeric text for TSV (no assessment labels)."""
    if value is None:
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    s = f"{f:.{decimals}f}".rstrip("0").rstrip(".")
    if "." not in s:
        s += ".0"
    return s


def render_minimal(view: dict, *, sequence: object = None) -> str:
    """Stable TSV + trailing json\t row array for coding/data mapping.

    One row per provider window. No assessment/pressure/action/lanes/color.
    """
    checked_ict = view.get("checked_at_ict") or view.get("checked_at") or ""
    checked = view.get("checked_at") or ""
    seq = "" if sequence is None else sequence
    rows: list[dict] = []
    for provider in view.get("providers") or []:
        name = provider.get("provider") or ""
        plan = provider.get("plan")
        plan_s = "" if plan is None else str(plan)
        for w in provider.get("windows") or []:
            rows.append({
                "provider": name,
                "plan": plan_s,
                "window": w.get("label") or "",
                "used_pct": w.get("used_percent"),
                "pace_pct": w.get("pace_percent"),
                "started": w.get("started", True),
                "reset_in": w.get("resets_in") or "",
                "resets_at": w.get("resets_at"),
                "resets_at_ict": w.get("resets_at_ict"),
                "window_hours": w.get("window_hours"),
            })
    lines = [
        f"tokencheck_minimal\tchecked_at_ict={checked_ict}\tseq={seq}",
        "provider\tplan\twindow\tused_pct\tpace_pct\treset_in",
    ]
    for r in rows:
        lines.append(
            f"{r['provider']}\t{r['plan']}\t{r['window']}\t"
            f"{_fmt_pct(r['used_pct'], decimals=1)}\t"
            f"{'-' if not r['started'] else _fmt_pct(r['pace_pct'], decimals=2)}\t"
            f"{r['reset_in']}"
        )
    meta = {
        "checked_at": checked,
        "checked_at_ict": checked_ict,
        "seq": seq,
        "rows": [
            {
                "provider": r["provider"],
                "plan": r["plan"] if r["plan"] else None,
                "window": r["window"],
                "used_pct": r["used_pct"],
                "pace_pct": r["pace_pct"],
                "reset_in": r["reset_in"] or None,
                "resets_at": r["resets_at"],
                "resets_at_ict": r["resets_at_ict"],
                "window_hours": r["window_hours"],
            }
            for r in rows
        ],
    }
    lines.append("")
    lines.append("json\t" + json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


@mcp.tool(structured_output=False, description="Show local usage and pace as a text chart.")
def tokencheck_chart(provider: Provider = "all", width: int = 48) -> str:
    """Pace chart (5H / WEEK windows, used vs pace) for Claude, Codex, Grok, AGY.
    Collected every 60 s locally without spending tokens. Paste verbatim.
    Exit code meaning: 0 proceed, 1 caution, 2 critical, 3 unknown."""
    env = dict(os.environ, PACER_STATE_DIR=STATE, NO_COLOR="1")
    p = subprocess.run([sys.executable, str(LIB / "tokencheck.py"), "--provider", provider, "--chart",
                        "--chart-width", str(max(10, min(200, width))), "--chart-color", "never"],
                       capture_output=True, text=True, timeout=15, env=env)
    return f"{p.stdout.rstrip()}\n[exit_code={p.returncode}]"


@mcp.tool(structured_output=False, description="Return local usage and pace as TSV with a trailing JSON row.")
def tokencheck_minimal(provider: Provider = "all") -> str:
    """Stable TSV for coding/data mapping: one row per provider window.

    Header + columns provider/plan/window/used_pct/pace_pct/reset_in (tab-separated),
    then a blank line and json\t{checked_at,checked_at_ict,seq,rows:[...]}.
    No assessment/pressure/action/lanes. Local daemon only."""
    envelope = get("/v1/snapshot")
    results = envelope.get("results") or {}
    view = results.get(provider) or results.get("all") or {}
    return render_minimal(view, sequence=envelope.get("sequence"))


@mcp.tool(structured_output=False, description="Render local usage and pace history as PNG files; return their paths.")
def tokencheck_png(provider: Provider = "all", hours: float = 24.0) -> str:
    """Generate a dark PNG: Usage (cyan) vs recorded Pace (violet dashed)
    over time for populated current windows, with compact unstarted AGY rows
    (default last 24h). Uses rolling history.jsonl
    (daemon appends every ~60s). Returns absolute PNG path(s). hours= lookback."""
    # Load rasterizers only on a PNG request.
    import plot_png as plot

    envelope = get("/v1/snapshot")
    samples = hist.load_samples(STATE)
    ts = datetime.now(ICT).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(STATE) / "png"
    out_dir.mkdir(parents=True, exist_ok=True)
    primary = out_dir / f"tokencheck-{provider}-{ts}.png"
    # Stable local alias alongside the daemon state.
    alias = Path(STATE) / "tokencheck-usage-pace.png"
    title = "Tokencheck / Usage & pace"
    plot.render_png(samples, primary, provider=provider,
                    hours=hours if hours and hours > 0 else 0, title=title)
    plot.atomic_write(alias, primary.read_bytes())
    n = len(samples)
    span = ""
    if samples:
        span = f" · history_points={n} first={samples[0].get('t')} last={samples[-1].get('t')}"
    return (
        f"png={primary}\n"
        f"alias={alias}\n"
        f"provider={provider} hours={hours}{span}\n"
        f"note=pace dashed line uses recorded samples; "
        f"usage line is measured samples (thin until history accumulates)"
    )


@mcp.tool(structured_output=False, description="Return local usage snapshot JSON.")
def tokencheck_snapshot(provider: Provider = "all", full: bool = False) -> str:
    """Raw JSON. full=false: the computed verdict for one provider view
    (/v1/tokencheck). full=true: the whole /v1/snapshot envelope (raw+results+sources)."""
    data = get("/v1/snapshot") if full else get(f"/v1/tokencheck?provider={provider}")
    return json.dumps(data, ensure_ascii=False, indent=1)


@mcp.tool(structured_output=False, description="Return local collector methods and health as JSON.")
def tokencheck_methods() -> str:
    """Which collection method won per provider on the last 60 s poll, every attempt
    tried (official first, then fallbacks), plus health/outage watcher state."""
    out = {"collector": get("/v1/collector"), "unhealthy": get("/v1/unhealthy"), "health": None}
    try:
        out["health"] = get("/health")
    except Exception as e:  # 503 when stale
        out["health"] = {"error": str(e)}
    return json.dumps(out, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    mcp.run()
