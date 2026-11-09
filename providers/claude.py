"""Passive capture plus one persistent isolated OAuth usage budget."""
from .common import Attempt, HOME, log, parse_time, window
from typing import Any
from pathlib import Path
from datetime import datetime
import json
import math
import re
import time
from datetime import timedelta
from datetime import timezone
import urllib.error
import urllib.request
from . import common as util
from config import load_config
from claude_oauth_cache import OAuthCache, merge_windows
from claude_capture import read_json, stamp
import claude_token_refresh

CLAUDE_STATUSLINE_CACHE = load_config().state_dir / "claude-statusline.json"
CLAUDE_STATUSLINE_MAX_AGE = 15 * 60


def limit_hours(w: dict, group: str, now: datetime) -> float:
    """Use the declared period, then group/start metadata, then remaining time."""
    hours = w.get("window_hours")
    if isinstance(hours, (int, float)) and not isinstance(hours, bool) and math.isfinite(hours) and hours > 0:
        return hours
    known = {"weekly": 168, "week": 168, "session": 5, "5h": 5,
             "five_hour": 5, "daily": 24, "hourly": 1, "monthly": 720}
    if group in known:
        return known[group]
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(h|d)", group)
    if match and float(match[1]) > 0:
        return float(match[1]) * (24 if match[2] == "d" else 1)
    reset = parse_time(w.get("resets_at"))
    start = parse_time(w.get("starts_at") or w.get("window_started_at"))
    return max(1, (reset - (start or now)).total_seconds() / 3600) if reset else 1


def claude_from_rate_limits(rl: dict, now: datetime, source: str, plan: str | None = "Max") -> dict:
    """Map provider-reported limits only; inactive scoped limits still carry usage."""
    entries = []
    if isinstance(rl.get("limits"), list):
        for w in rl["limits"]:
            if not isinstance(w, dict):
                continue
            kind = str(w.get("kind") or w.get("group") or "limit").lower()
            group = str(w.get("group") or kind).lower()
            scope = w.get("scope") or {}
            model = scope.get("model") if isinstance(scope, dict) else None
            name = model.get("display_name") if isinstance(model, dict) else None
            name = " ".join(str(name).upper().split()) if name else ""
            if kind == "weekly_all":
                label, group = "ALL WEEK", "weekly"
            elif kind == "weekly_scoped" or group == "weekly":
                label, group = (name or kind.replace("_", " ").upper()) + " WEEK", "weekly"
            elif group in ("session", "5h", "five_hour"):
                label = (name + " " if name else "") + "5H"
            else:
                label = kind.replace("_", " ").upper()
            if label == "FABLE WEEK" and not (kind == "weekly_scoped" and name == "FABLE"):
                continue
            entries.append((label, w.get("percent"), w, group))
    for key, w in rl.items():
        if not isinstance(w, dict):
            continue
        if key == "five_hour":
            label, group = "5H", "session"
        elif key == "seven_day":
            label, group = "ALL WEEK", "weekly"
        elif key.startswith("seven_day_") and key not in ("seven_day_breakdown", "seven_day_spend", "seven_day_extra_usage"):
            label, group = key[len("seven_day_"):].replace("_", " ").upper() + " WEEK", "weekly"
        else:
            continue
        if label == "FABLE WEEK":
            continue
        entries.append((label, w.get("used_percentage", w.get("utilization")), w, group))
    windows = []
    for label, used, w, group in entries:
        reset = parse_time(w.get("resets_at"))
        if any(existing["label"] == label for existing in windows):
            continue
        if isinstance(used, (int, float)) and not isinstance(used, bool) and 0 <= used <= 100 and reset:
            extra = {"mapping": "weekly_scoped_model_fable"} if label == "FABLE WEEK" else {}
            windows.append(window(label, used, reset, limit_hours(w, group, now), **extra))
    if not windows:
        raise Attempt("no_usage_windows")
    return {"provider": "claude", "plan": plan, "source": source, "windows": windows}


def claude_statusline_data(data: dict, now: datetime, source: str) -> dict:
    windows = []
    rl = data.get("rate_limits")
    if not isinstance(rl, dict):
        raise Attempt("statusline_has_no_rate_limits")
    for key in ("five_hour", "seven_day"):
        meta = (data.get("window_meta") or {}).get(key) or {}
        cap = parse_time(meta.get("captured_at") or data.get("captured_at"))
        if cap is None or cap > now + timedelta(seconds=5):
            continue
        try:
            provider = claude_from_rate_limits({key: rl.get(key)}, now, source)
        except Attempt:
            continue
        for w in provider["windows"]:
            w.update(source=source, measured_at=stamp(cap.timestamp()),
                     age_seconds=max(0, (now - cap).total_seconds()),
                     host=meta.get("host") or data.get("host"))
            windows.append(w)
    if not windows:
        raise Attempt("no_statusline_capture_yet")
    return {"provider": "claude", "plan": "Max", "source": source,
            "measured_at": max((w["measured_at"] for w in windows), key=lambda value: parse_time(value).timestamp()), "windows": windows}


def claude_statusline(now: datetime, ctx: dict) -> dict:
    path = Path(ctx.get("state", CLAUDE_STATUSLINE_CACHE.parent)) / "claude-statusline.json"
    return claude_statusline_data(read_json(path), now, "claude_statusline")


def _claude_profile_token() -> str | None:
    config = load_config().claude_config_dir
    # Never consume the default interactive login or inherited environment token.
    if config.resolve() == (HOME / ".claude").resolve():
        return None
    credential = config / '.credentials.json'
    if credential.resolve().parent != config.resolve():
        return None  # An isolated profile must not link to another login's secrets.
    try:
        d = json.loads(credential.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    o = d.get("claudeAiOauth") if isinstance(d, dict) else None
    if not isinstance(o, dict) or not o.get("accessToken"):
        return None
    scopes = o.get("scopes") or []
    if "user:profile" not in scopes:
        return None
    exp = o.get("expiresAt")
    if isinstance(exp, (int, float)) and exp / 1000 < time.time() + 60:
        return None  # Re-login/refresh only through the isolated Claude flow.
    return o["accessToken"]


def _claude_oauth_local_request(token: str) -> tuple[int, Any, Any]:
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "pacer-tokencheck/1", "Accept": "application/json"})
    # Bearer credentials must never follow redirects to another endpoint.
    from selfcheck import NoRedirect
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=15) as response:
            return response.status, json.loads(response.read(1 << 20)), response.headers.get("Retry-After")
    except urllib.error.HTTPError as error:
        return error.code, None, error.headers.get("Retry-After")


def claude_oauth_usage(now: datetime, ctx: dict) -> dict:
    state = Path(ctx.get("state", CLAUDE_STATUSLINE_CACHE.parent))

    def select_request():
        # Local credentials are consulted only when due under the shared gate.
        token = _claude_profile_token()
        # Missing/expired login consumes a local attempt; no other transport.
        if token is None:
            return "claude_oauth_usage", lambda: (0, None, None)
        return "claude_oauth_usage", lambda: _claude_oauth_local_request(token)

    def parse(body, received, source):
        measured = datetime.fromtimestamp(received, timezone.utc)
        provider = claude_from_rate_limits(body, measured, source)
        provider["measured_at"] = stamp(received)
        return provider

    provider = OAuthCache(state, interval=ctx.get("oauth_interval", 600)).get(select_request, parse, log)
    if provider is None:
        raise Attempt("oauth_no_cached_windows")
    return provider


def box_only_claude(provider: dict | None) -> dict | None:
    """Drop retired remote-passive windows, including inherited provider sources."""
    if not isinstance(provider, dict):
        return None
    windows = [w for w in provider.get("windows", []) if isinstance(w, dict)
               # Legacy validation only: retired passive capture is not usable.
               and not ((w.get("source") or provider.get("source") or "").startswith("claude_statusline_") )]
    return dict(provider, windows=windows) if windows else None


def claude_token_renewal(ctx: dict) -> dict | None:
    """Renew the isolated access token ahead of expiry; never raises, never logs secrets."""
    if not ctx.get("claude_auto_refresh"):
        return None
    state = Path(ctx.get("state", CLAUDE_STATUSLINE_CACHE.parent))
    try:
        return claude_token_refresh.maybe_refresh(state, load_config().claude_config_dir, log=log)
    except Exception as error:
        return {"enabled": True, "last_result": "error_" + error.__class__.__name__}


def claude_collect(now: datetime, ctx: dict) -> tuple[dict | None, list[dict]]:
    providers, attempts = [], []
    renewal = claude_token_renewal(ctx)
    if renewal is not None:
        ctx["claude_token_refresh"] = renewal
    for method, fn in (("claude_statusline", claude_statusline),
                       ("claude_oauth_shared", claude_oauth_usage)):
        started = time.monotonic()
        try:
            providers.append(fn(now, ctx))
            attempt = {"method": method, "ok": True}
        except Exception as error:
            attempt = {"method": method, "ok": False,
                       "reason": str(error) if isinstance(error, Attempt) else error.__class__.__name__}
        attempt["ms"] = round((time.monotonic() - started) * 1000)
        attempts.append(attempt)
    # Retain each last-good observation independently, including Fable's own clock.
    providers.append(box_only_claude(ctx.get("previous_claude")))
    windows = merge_windows(providers, util.utcnow().timestamp())
    if not windows:
        return None, attempts
    return {"provider": "claude", "plan": "Max", "source": "claude_merged",
            "measured_at": max((w["measured_at"] for w in windows), key=lambda value: parse_time(value).timestamp()), "windows": windows}, attempts
