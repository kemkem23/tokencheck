#!/usr/bin/env python3
"""pacer-tokencheck daemon for local usage collection.

One process:
  * collector thread  - polls every provider every 60 s (collectors.py), never spends tokens
  * engine tick       - pacer tokencheck-service engine (daemon.py/core.py, unchanged)
                        recomputes pace every 5 s from the cached collector output
  * HTTP              - 127.0.0.1:48829  GET /v1/snapshot /v1/limits /v1/tokencheck?provider=
                        /v1/collector /health /healthz   (read-only; a GET never triggers a poll)
  * health watcher    - unhealthy > 3600 s => one webhook POST per incident (1 h debounce),
                        and <state>/unhealthy-since.json for the hourly routine
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from config import load_config

LIB = Path(__file__).resolve().parent
LOG_FILE_NAME = "collector.log"
UNHEALTHY_THRESHOLD_S = int(os.environ.get("PACER_UNHEALTHY_THRESHOLD_SECONDS", "3600"))
WEBHOOK_DEBOUNCE_S = int(os.environ.get("PACER_WEBHOOK_DEBOUNCE_SECONDS", "3600"))
TAILNET_IP = os.environ.get("PACER_TAILNET_IP", "")


def load(name: str, file: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, LIB / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


daemon = load("tokencheck_daemon", "daemon.py")
collectors = load("pacer_collectors", "collectors.py")
dashboard = load("pacer_dashboard", "dashboard.py")
_log_lock = threading.Lock()
_log_path: Path | None = None


def log(msg: str) -> None:
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {msg}"
    with _log_lock:
        print(line, file=sys.stderr, flush=True)
        if _log_path:
            try:
                if _log_path.exists() and _log_path.stat().st_size > 5 * 1024 * 1024:
                    _log_path.replace(_log_path.with_suffix(".log.1"))
                with _log_path.open("a") as f:
                    f.write(line + "\n")
            except OSError:
                pass


daemon.log = lambda m: log(f"engine: {m}")
collectors.set_logger(lambda m: log(f"collector: {m}"))

# ---- HTTP patches: extra routes + allow tailnet Host header (tailscale serve TCP forward)
EXTRA_HOSTS = {h.strip() for h in os.environ.get("PACER_ALLOWED_HOSTS", "").split(",") if h.strip()}
EXTRA_HOSTS |= set(load_config().allowed_hosts)
if TAILNET_IP:
    EXTRA_HOSTS.add(TAILNET_IP)
COLLECTOR: Any = None
WATCHER: Any = None
_orig_host_allowed = daemon.Handler.host_allowed
_orig_dispatch = daemon.Handler.dispatch
daemon.ROUTES = tuple(daemon.ROUTES) + ("/health", "/v1/collector", "/v1/unhealthy")


def host_allowed(self) -> bool:
    if _orig_host_allowed(self):
        return True
    host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
    return host in EXTRA_HOSTS


def dispatch(self, *, head: bool) -> None:
    if dashboard.handle(self, head=head):
        return
    route = urlsplit(self.path).path
    if route == "/health":
        self.path = self.path.replace("/health", "/healthz", 1)
    elif route in ("/v1/collector", "/v1/unhealthy") and self.admitted and self.host_allowed():
        parts = urlsplit(self.path)
        if (len(self.requestline) > 8192 or parts.query or parts.scheme or parts.netloc or parts.fragment
                or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length','0').strip() not in ('','0')):
            self.send_error_json(400,'invalid_request',head=head)
            return
        obj = (COLLECTOR.status if route == "/v1/collector" else WATCHER.state) if COLLECTOR else {}
        self.send_json(200, json.dumps(obj, sort_keys=True).encode(), head=head)
        return
    _orig_dispatch(self, head=head)


daemon.Handler.host_allowed = host_allowed
daemon.Handler.dispatch = dispatch


# ---- health watcher
def webhook_config(state: Path) -> tuple[str | None, str | None]:
    url = os.environ.get("PACER_TOKENCHECK_WEBHOOK_URL") or None
    header = os.environ.get("PACER_TOKENCHECK_WEBHOOK_HEADER") or None
    f = state / "webhook.url"
    if f.exists():
        try:
            lines = [l.strip() for l in f.read_text().splitlines()]
        except OSError:
            lines = []
        if not url and lines and lines[0] and not lines[0].startswith("#"):
            url = lines[0]
        if not header and len(lines) > 1 and lines[1]:
            header = lines[1]
    return url, header


class Watcher:
    def __init__(self, state: Path, engine: Any) -> None:
        self.state_dir = state
        self.engine = engine
        self.file = state / "unhealthy-since.json"
        self.state: dict[str, Any] = {"providers": {}, "incident": None, "webhook": {}}
        try:
            prev = json.loads(self.file.read_text())
            self.state["providers"] = prev.get("providers") or {}
            self.state["incident"] = prev.get("incident")
            self.state["webhook"] = prev.get("webhook") or {}
        except Exception:
            pass
        self.warned_unconfigured = False

    def current(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        st = COLLECTOR.status.get("providers", {}) if COLLECTOR else {}
        for name in ("claude", "codex", "grok", "agy"):
            p = st.get(name) or {}
            healthy = p.get("state") in ("ok", "disabled")
            reason = None if healthy else ((p.get("attempts") or [{}])[-1].get("reason") or p.get("state") or "no_poll_yet")
            out[name] = {"healthy": healthy, "state": p.get("state") or "unknown", "reason": reason,
                         "winner": p.get("winner")}
        code, info = self.engine.health()
        out["daemon"] = {"healthy": code == 200, "state": "ok" if code == 200 else "unhealthy",
                         "reason": info.get("reason_code"), "winner": None}
        poll_age = None
        if COLLECTOR and COLLECTOR.status.get("polled_at"):
            poll_age = (datetime.now(timezone.utc) - collectors.parse_time(COLLECTOR.status["polled_at"])).total_seconds()
        if poll_age is None or poll_age > 5 * collectors.POLL_SECONDS:
            out["daemon"] = {"healthy": False, "state": "collector_stalled", "reason": "collector_poll_too_old",
                             "winner": None}
        return out

    def check(self) -> None:
        now = datetime.now(timezone.utc)
        now_iso = collectors.iso(now)
        cur = self.current()
        provs = self.state["providers"]
        for name, c in cur.items():
            prev = provs.get(name) or {}
            if c["healthy"]:
                provs[name] = {"healthy": True, "since": None, "state": c["state"], "winner": c["winner"]}
            else:
                since = prev.get("since") if not prev.get("healthy", True) and prev.get("since") else now_iso
                provs[name] = {"healthy": False, "since": since, "state": c["state"], "reason": c["reason"],
                               "unhealthy_seconds": round((now - collectors.parse_time(since)).total_seconds())}
        over = sorted(n for n, p in provs.items()
                      if not p.get("healthy") and p.get("unhealthy_seconds", 0) > UNHEALTHY_THRESHOLD_S)
        inc = self.state.get("incident")
        if not over:
            if inc:
                log(f"watcher: incident {inc.get('id')} cleared")
            self.state["incident"] = None
        else:
            if not inc:
                inc = {"id": now.strftime("%Y%m%dT%H%M%SZ"), "opened_at": now_iso, "notified": [], "last_post_at": None}
                self.state["incident"] = inc
                log(f"watcher: incident {inc['id']} opened providers={over}")
            new = [n for n in over if n not in inc["notified"]]
            last = collectors.parse_time(inc.get("last_post_at"))
            due = not inc["notified"] or (new and (last is None or (now - last).total_seconds() >= WEBHOOK_DEBOUNCE_S))
            if due:
                self.post(over, inc, now_iso)
        self.state.update({"updated_at": now_iso, "threshold_seconds": UNHEALTHY_THRESHOLD_S,
                           "unhealthy_over_threshold": over,
                           "unhealthy_now": sorted(n for n, p in provs.items() if not p.get("healthy")),
                           "since": min((provs[n]["since"] for n in over), default=None)})
        url, _h = webhook_config(self.state_dir)
        self.state["webhook"]["configured"] = bool(url)
        daemon.atomic_write(self.file, json.dumps(self.state, sort_keys=True, indent=1).encode())

    def post(self, over: list[str], inc: dict, now_iso: str) -> None:
        url, header = webhook_config(self.state_dir)
        if not url:
            if not self.warned_unconfigured:
                log("watcher: webhook not configured (PACER_TOKENCHECK_WEBHOOK_URL or webhook.url); skipping POST")
                self.warned_unconfigured = True
            inc["last_post_at"] = now_iso
            inc["notified"] = sorted(set(inc["notified"]) | set(over))
            inc["post_result"] = "not_configured"
            return
        provs = self.state["providers"]
        payload = {
            "event": "pacer_tokencheck_unhealthy", "incident_id": inc["id"],
            "providers": over,
            "since": min(provs[n]["since"] for n in over),
            "host": socket.gethostname(),
            "snapshot_excerpt": {n: {k: provs[n].get(k) for k in ("state", "reason", "since", "unhealthy_seconds")}
                                 for n in provs},
            "collector_attempts": {n: (COLLECTOR.status.get("providers", {}).get(n) or {}).get("attempts")
                                   for n in over if n != "daemon"},
        }
        headers = {"Content-Type": "application/json", "User-Agent": "pacer-tokencheck/1"}
        if header:
            if ":" in header and not header.lower().startswith("bearer "):
                k, v = header.split(":", 1)
                headers[k.strip()] = v.strip()
            else:
                headers["Authorization"] = header if header.lower().startswith("bearer ") else f"Bearer {header}"
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                result = f"http_{r.status}"
        except urllib.error.HTTPError as e:
            result = f"http_{e.code}"
        except Exception as e:
            result = f"error_{e.__class__.__name__}"
        inc["last_post_at"] = now_iso
        inc["notified"] = sorted(set(inc["notified"]) | set(over))
        inc["post_result"] = result
        self.state["webhook"]["last_post_at"] = now_iso
        self.state["webhook"]["last_result"] = result
        log(f"watcher: webhook POST to {urlsplit(url).hostname} providers={over} result={result}")


def collector_loop(col: Any, watcher: Watcher, stop: threading.Event) -> None:
    next_at = time.monotonic()
    while not stop.is_set():
        try:
            col.poll()
        except Exception as e:
            log(f"collector: poll crashed {e.__class__.__name__}: {e}")
        try:
            watcher.check()
        except Exception as e:
            log(f"watcher: check crashed {e.__class__.__name__}: {e}")
        next_at += collectors.POLL_SECONDS
        now = time.monotonic()
        if now >= next_at:
            next_at = now + collectors.POLL_SECONDS
        stop.wait(max(1.0, next_at - now))


def main(argv=None) -> int:
    global COLLECTOR, WATCHER, _log_path
    config = load_config()
    ap = argparse.ArgumentParser(description="pacer tokencheck daemon")
    ap.add_argument("--port", type=int, default=config.port)
    ap.add_argument("--state-dir", default=str(config.state_dir))
    ap.add_argument("--once", action="store_true", help="one collector poll + one engine tick, print, exit")
    args = ap.parse_args(argv)
    if not 1 <= args.port <= 65535:
        ap.error('port must be 1..65535')
    if args.state_dir != str(config.state_dir):
        os.environ['PACER_STATE_DIR'] = args.state_dir
        config = load_config()
    state = config.state_dir
    daemon.ensure_state_dir(state)
    _log_path = state / LOG_FILE_NAME
    try:
        lock_fd = daemon.acquire_lock(state / "daemon.lock")
    except OSError:
        log("another pacer-tokencheck daemon holds the lock; exiting")
        return 75
    engine = daemon.Engine(state, state / "grok-usage.json" if "grok" in config.enabled_providers else None, None, enabled_providers=config.enabled_providers)
    # Rolling usage/pace series for PNG plots (MCP tokencheck_png).
    try:
        history = load("pacer_history", "history.py")
        _orig_persist = engine.persist

        def _persist_with_history(envelope, body, _orig=_orig_persist, _hist=history, _state=state):
            _orig(envelope, body)
            try:
                _hist.append_envelope(envelope, _state)
            except Exception as e:  # never break the live feed for plot history
                log(f"history: append failed {e.__class__.__name__}: {e}")

        engine.persist = _persist_with_history  # type: ignore[method-assign]
        log("history: wrapping Engine.persist → history.jsonl")
    except Exception as e:
        log(f"history: unavailable ({e.__class__.__name__}: {e})")
    COLLECTOR = collectors.Collector(state, daemon, config)
    WATCHER = Watcher(state, engine)
    if args.once:
        COLLECTOR.poll()
        env = engine.tick()
        WATCHER.check()
        print(json.dumps({"collector": COLLECTOR.status, "results": env["results"]["all"]}, indent=1, default=str))
        os.close(lock_fd)
        return 0
    server = daemon.LoopbackServer(args.port, engine)
    (state / "daemon.pid").write_text(str(os.getpid()))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    engine.safe_tick()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    threading.Thread(target=collector_loop, args=(COLLECTOR, WATCHER, stop), daemon=True, name="collector").start()
    log(f"pacer-tokencheck listening 127.0.0.1:{args.port} poll={collectors.POLL_SECONDS}s "
        f"tick={daemon.TICK_SECONDS}s state={state}")
    try:
        stop.wait(daemon.TICK_SECONDS)
        daemon.run_ticks(engine, stop)
    finally:
        server.shutdown()
        server.server_close()
        try:
            (state / "daemon.pid").unlink()
        except OSError:
            pass
        os.close(lock_fd)
        log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
