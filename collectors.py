"""Provider orchestration and stable local cache contracts. No inference."""
from providers.common import Attempt, iso, parse_time, log, set_logger
from providers import claude, codex, grok, agy, common as util
from config import load_config
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
import threading
import time
import json
POLL_SECONDS = load_config().poll_interval

CHAINS: dict[str, list[tuple[str, Callable]]] = {
    "claude": [("claude_merged", claude.claude_collect)],
    "codex": [("codex_app_server", codex.codex_app_server), ("tokscale", codex.codex_tokscale),
              ("codex_session_logs", codex.codex_session_logs)],
    "grok": [("grok_dashboard_usage_modal", grok.grok_dashboard_usage)],
    "agy": [("agy_usage", agy.agy_local)],
}


def run_chain(name: str, now: datetime, ctx: dict) -> tuple[dict | None, list[dict]]:
    if name == "claude":
        return claude.claude_collect(now, ctx)
    attempts = []
    for method, fn in CHAINS[name]:
        t0 = time.monotonic()
        try:
            prov = fn(now, ctx)
            attempts.append({"method": method, "ok": True, "ms": round((time.monotonic() - t0) * 1000)})
            return prov, attempts
        except Attempt as e:
            attempts.append({"method": method, "ok": False, "reason": str(e),
                             "ms": round((time.monotonic() - t0) * 1000)})
        except Exception as e:  # bug guard; never crash the poll
            attempts.append({"method": method, "ok": False, "reason": f"exception:{e.__class__.__name__}",
                             "ms": round((time.monotonic() - t0) * 1000)})
    return None, attempts


# ---------------------------------------------------------------------------
# Writer

def observation_ages(provider: dict, now: datetime) -> dict:
    """Keep original measurement clocks; recompute ages on successes and failures."""
    windows = []
    for item in provider.get("windows", []):
        measured_at = item.get("measured_at") or provider.get("measured_at")
        measured = parse_time(measured_at)
        windows.append(dict(item, measured_at=measured_at,
                            age_seconds=max(0, (now - measured).total_seconds()) if measured else None))
    return dict(provider, windows=windows)


class Collector:
    def __init__(self, state_dir: Path, daemon_mod: Any, config=None) -> None:
        self.config = config or load_config()
        self.state = state_dir
        self.d = daemon_mod
        self.core = daemon_mod.core
        self.sources_file = state_dir / "sources.json"
        self.grok_file = state_dir / "grok-usage.json"
        self.status_file = state_dir / "collector-status.json"
        self.generation = 0
        self.failures = 0
        self.last: dict[str, dict] = {}
        self.sources: dict[str, dict] = {}
        self.status: dict[str, Any] = {}
        self.lock = threading.Lock()
        try:
            prev = json.loads(self.sources_file.read_text())
            self.generation = int(prev.get("source_generation") or 0)
            for p in prev["raw"]["providers"]:
                if p["provider"] == "claude":
                    p = claude.box_only_claude(p)
                    if p is None:
                        continue
                self.last[p["provider"]] = p
            self.sources = prev["raw"]["sources"]
        except Exception:
            pass

    def write(self, path: Path, obj: Any) -> None:
        self.d.atomic_write(path, json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1).encode())

    def poll(self) -> dict[str, Any]:
        now = util.utcnow()
        now_iso = iso(now)
        ctx = {"core": self.core, "state": self.state,
               "previous_claude": self.last.get("claude"), "oauth_interval": self.config.oauth_interval,
               "claude_auto_refresh": self.config.claude_auto_refresh}
        results: dict[str, tuple[dict | None, list[dict]]] = {}
        threads = []
        for name in CHAINS:
            if name not in self.config.enabled_providers:
                continue
            t = threading.Thread(target=lambda n=name: results.__setitem__(n, run_chain(n, now, ctx)), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(150)

        any_fail = False
        status_providers: dict[str, Any] = {}
        for name in ("claude", "codex", "agy"):
            prov, attempts = results.get(name, (None, [{"method": "thread", "ok": False, "reason": "timeout"}]))
            key = f"{name}_quota"
            if name not in self.config.enabled_providers:
                self.last.pop(name, None)
                self.sources[key] = self.d.source_status(None, "disabled", now_iso)
                status_providers[name] = {"winner": None, "state": "disabled", "attempts": []}
                continue
            if prov is not None:
                prov.setdefault("measured_at", now_iso)
                if prov.get("measured_at") is None:
                    prov["measured_at"] = now_iso
                prov = observation_ages(prov, now)
                if name == "claude":
                    prov["stale"] = all(w["age_seconds"] > claude.CLAUDE_STATUSLINE_MAX_AGE for w in prov["windows"])
                else:
                    prov["stale"] = False
                prov["attempts"] = attempts
                self.last[name] = prov
                self.sources[key] = self.d.source_status(self.sources.get(key), "stale" if prov.get("stale") else "ok", now_iso,
                                                         success_at=prov["measured_at"],
                                                         active_source=prov["source"])
            else:
                any_fail = True
                prev = self.last.get(name)
                if prev is not None:
                    prev = dict(observation_ages(prev, now), stale=True, attempts=attempts)
                    self.last[name] = prev
                st = "stale" if (self.sources.get(key) or {}).get("last_success_at") else "unavailable"
                self.sources[key] = self.d.source_status(
                    self.sources.get(key), st, now_iso,
                    active_source=(prev or {}).get("source") or CHAINS[name][0][0],
                    error_code=attempts[-1].get("reason") if attempts else "no_method")
            status_providers[name] = {"winner": prov["source"] if prov else None,
                                      "state": self.sources[key]["state"], "attempts": attempts}
            if name == "claude" and ctx.get("claude_token_refresh") is not None:
                status_providers[name]["token_refresh"] = ctx["claude_token_refresh"]

        # Grok -> grok-usage.json (cache contract schema v2)
        gprov, gatt = results.get("grok", (None, [{"method": "thread", "ok": False, "reason": "timeout"}]))
        grok_version = "unknown"
        if gprov is not None:
            pr = gprov["parsed"]
            data = {"plan": pr["plan"], "used_percent": pr["used_percent"], "reset_at": pr["reset_at"],
                    "source": "grok_session", "measured_at": now_iso}
            if pr.get("payg") is not None:
                data["payg"] = pr["payg"]
            usable = pr["used_percent"] < 100
            cache = {"schema_version": 2, "generated_at": now_iso, "session": self.config.grok_tmux_pane,
                     "grok_version": grok_version, "state": "ok", "data": data,
                     "access": {"state": "usable" if usable else "unusable",
                                "reason": "usage_modal_ok" if usable else "weekly_limit_reached",
                                "checked_at": now_iso}}
            self.write(self.grok_file, cache)
        elif "grok" not in self.config.enabled_providers:
            cache = {"state": "disabled"}
            gatt = []
        else:
            any_fail = True
            reason = gatt[-1].get("reason") if gatt else "no_method"
            try:
                prev = json.loads(self.grok_file.read_text())
            except Exception:
                prev = None
            cache = {"schema_version": 2, "generated_at": now_iso, "session": self.config.grok_tmux_pane,
                     "grok_version": grok_version, "state": "stale" if prev and prev.get("data") else "unavailable",
                     "data": (prev or {}).get("data"), "error_code": reason,
                     "access": {"state": "unusable" if reason == "billing_exhausted" else "unknown",
                                "reason": "billing_exhausted" if reason == "billing_exhausted" else "access_check_failed",
                                "checked_at": now_iso}}
            self.write(self.grok_file, cache)
        status_providers["grok"] = {"winner": "grok_dashboard_usage_modal" if gprov else None,
                                    "state": "disabled" if "grok" not in self.config.enabled_providers else ("ok" if gprov else cache["state"]), "attempts": gatt}

        self.generation += 1
        self.failures = self.failures + 1 if any_fail else 0
        envelope = {
            "schema_version": 1, "source_generation": self.generation, "failures": self.failures,
            "generated_at": now_iso, "next_attempt_at": iso(now + timedelta(seconds=self.config.poll_interval)),
            "collector": "pacer-tokencheck", "poll_seconds": self.config.poll_interval,
            "raw": {"providers": [self.last[n] for n in ("claude", "codex", "agy") if n in self.last],
                    "sources": {k: v for k, v in self.sources.items() if k in ("claude_quota", "codex_quota", "agy_quota")},
                    "models": [], "measured_at": now_iso},
        }
        self.write(self.sources_file, envelope)

        with self.lock:
            self.status = {"polled_at": now_iso, "poll_seconds": self.config.poll_interval,
                           "generation": self.generation, "providers": status_providers,
                           "remote_collection": "disabled", "ssh_calls": 0,
                           "extras": {}, "token_spend": "none (usage/rate-limit reads only)"}
            self.write(self.status_file, self.status)
        summary = " ".join(f"{n}={v['winner'] or ('disabled' if v['state'] == 'disabled' else 'FAIL:' + (v['attempts'][-1].get('reason', '?') if v['attempts'] else '?'))}"
                           for n, v in status_providers.items())
        log(f"poll gen={self.generation} remote_collection=disabled ssh_calls=0 {summary}")
        return self.status
