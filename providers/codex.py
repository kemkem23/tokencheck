"""Local official app-server, optional usage CLI, then rollout logs."""
from .common import Attempt, LIB_DIR, first_json, iso, parse_time, window
from datetime import datetime
import json
import os
import select
import subprocess
import sys
import time
from . import common as util

def codex_windows_from_rl(rl: dict, now: datetime, source: str) -> dict:
    windows = []
    for slot in ("primary", "secondary"):
        w = rl.get(slot)
        if not isinstance(w, dict):
            continue
        mins = w.get("windowDurationMins", w.get("window_minutes"))
        used = w.get("usedPercent", w.get("used_percent"))
        reset = parse_time(w.get("resetsAt", w.get("resets_at")))
        label = {300: ("5H", 5), 10080: ("WEEK", 168)}.get(int(mins) if isinstance(mins, (int, float)) else -1)
        if label and isinstance(used, (int, float)) and reset:
            windows.append(window(label[0], used, reset, label[1]))
    if not windows:
        raise Attempt("no_recognized_window")
    return {"provider": "codex", "plan": rl.get("planType"), "source": source, "windows": windows}


def codex_app_server(now: datetime, _ctx: dict) -> dict:
    exe = util.which("codex", ["~/.local/bin/codex"])
    if not exe:
        raise Attempt("codex_not_installed")
    try:
        p = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, cwd="/tmp")
    except OSError:
        raise Attempt("app_server_spawn_failed")
    try:
        def send(o: dict) -> None:
            p.stdin.write(json.dumps(o) + "\n")
            p.stdin.flush()
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "pacer-tokencheck", "version": "1.0"}}})
        send({"jsonrpc": "2.0", "method": "initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"})
        deadline = time.monotonic() + 25
        pending = b""
        while time.monotonic() < deadline:
            r, _, _ = select.select([p.stdout], [], [], 1)
            if not r:
                continue
            # readline() can wait forever for a newline after select() reports
            # a partial response. Read available bytes and frame them ourselves.
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                break
            pending += chunk
            if len(pending) > 1 << 20:
                raise Attempt("app_server_response_too_large")
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    o = json.loads(line)
                except (json.JSONDecodeError, UnicodeError):
                    continue
                if isinstance(o, dict) and o.get("id") == 2:
                    if "error" in o:
                        raise Attempt("rate_limits_rpc_error")
                    res = o.get("result") or {}
                    rl = res.get("rateLimits") or {}
                    prov = codex_windows_from_rl(rl, now, "codex_app_server")
                    credits = rl.get("credits") or {}
                    prov["extras"] = {
                        "credits_balance": credits.get("balance"),
                        "spend_control_reached": rl.get("spendControlReached"),
                        "rate_limit_reached_type": rl.get("rateLimitReachedType"),
                        "reset_credits_available": (res.get("rateLimitResetCredits") or {}).get("availableCount"),
                    }
                    return prov
        raise Attempt("app_server_timeout")
    finally:
        try:
            p.kill()
            p.wait(5)
        except Exception:
            pass
        p.stdin.close()
        p.stdout.close()


def codex_tokscale(now: datetime, _ctx: dict) -> dict:
    exe = util.which("tokscale", ["~/.npm-global/bin/tokscale", "~/.bun/bin/tokscale"])
    if not exe:
        raise Attempt("tokscale_not_installed")
    code, out, _ = util.run([exe, "usage", "--json"], 20)
    raw = first_json(out)
    items = raw if isinstance(raw, list) else (raw or {}).get("providers", []) if isinstance(raw, dict) else []
    for it in items:
        if isinstance(it, dict) and "codex" in str(it.get("provider", it.get("name", ""))).lower():
            windows = []
            for w in it.get("windows") or it.get("limits") or []:
                if not isinstance(w, dict):
                    continue
                mins = w.get("window_minutes") or w.get("windowMinutes")
                used = w.get("used_percent", w.get("usedPercent", w.get("percent")))
                reset = parse_time(w.get("resets_at") or w.get("resetsAt"))
                lab = {300: ("5H", 5), 10080: ("WEEK", 168)}.get(int(mins) if mins else -1)
                if lab and isinstance(used, (int, float)) and reset:
                    windows.append(window(lab[0], used, reset, lab[1]))
            if windows:
                return {"provider": "codex", "plan": it.get("plan"), "source": "tokscale", "windows": windows}
    raise Attempt(f"tokscale_no_codex_rc_{code}")


def codex_session_logs(now: datetime, _ctx: dict) -> dict:
    code, out, _ = util.run([sys.executable, str(LIB_DIR / "check_usage.py"), "--json"], 30)
    raw = first_json(out)
    if not isinstance(raw, dict) or not raw.get("windows"):
        raise Attempt(f"check_usage_rc_{code}")
    windows = [window(w["label"], w["used_percent"], w["resets_at"], w["window_hours"])
               for w in raw["windows"] if w.get("label") in ("5H", "WEEK")]
    measured = parse_time(raw.get("measured_at"))
    if measured and (now - measured).total_seconds() > 6 * 3600:
        raise Attempt("session_rate_limits_too_old")
    return {"provider": "codex", "plan": raw.get("plan"), "source": "codex_session_logs",
            "windows": windows, "measured_at": iso(measured) if measured else None}


# ---------------------------------------------------------------------------
