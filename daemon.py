#!/usr/bin/env python3
"""tokencheck service: recompute quota pacing every 5 seconds, never ask a provider.

The engine reads local collector caches and publishes one immutable generation
per tick. sources.json contains Claude/Codex/AGY; grok-usage.json contains Grok.
An optional historical monthly cache remains readable for the JSON contract.
The full lifecycle and collectors live in pacer_daemon.py; this module never
collects on an HTTP request. No credentials or provider subprocesses are used
by the engine itself.

stdlib only. core.py must sit next to this file.
"""

from __future__ import annotations

import calendar
import errno
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit


def _load_core() -> Any:
    # Same unique module name as the CLI: the skill directory is not a package.
    name = "tokencheck_core"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parent / "core.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = _load_core()

DAEMON_VERSION = 1
SNAPSHOT_SCHEMA_VERSION = 1
SOURCES_SCHEMA_VERSION = 1
TICK_SECONDS = 5.0
# Three missed ticks: the CLI and the orchestrator use the same number to call
# the service frozen even while its HTTP thread still answers.
READY_MAX_AGE_SECONDS = 15
# Disk copy is the CLI's fallback input, not the live feed. Rewrite it when the
# merged raw changes, otherwise at most once a minute.
PERSIST_MAX_INTERVAL_SECONDS = 60.0
SEQUENCE_RESTART_GAP = math.ceil(PERSIST_MAX_INTERVAL_SECONDS / TICK_SECONDS) + 1

LOOPBACK_HOST = "127.0.0.1"

SOURCES_CAP_BYTES = core.MAX_RESPONSE_BYTES
GROK_CACHE_CAP_BYTES = 64 * 1024
MONTHLY_CACHE_CAP_BYTES = core.MAX_RESPONSE_BYTES
SNAPSHOT_CAP_BYTES = 8 * core.MAX_RESPONSE_BYTES

# Ported from src/pipelines/usage-limits.ts so the merged raw is the same shape
# and carries the same verdicts the orchestrator tracker produced.
GROK_USAGE_MAX_AGE_SECONDS = 45 * 60
MONTHLY_CACHE_MAX_AGE_SECONDS = 13 * 60 * 60
CLOCK_SKEW_GRACE_SECONDS = 5 * 60
RESET_PAST_GRACE_SECONDS = 10 * 60
GROK_RESET_MAX_FUTURE_SECONDS = 8 * 24 * 60 * 60
QUOTA_SOURCE_KEYS = ("claude_quota", "codex_quota", "agy_quota")
COLLECTOR_PROVIDERS = ("claude", "codex", "agy")
MONTHLY_STATES = {"ok", "stale", "unavailable", "schema_changed"}
ICT = timezone(timedelta(hours=7))

HTTP_MAX_CONCURRENT = 16
HTTP_SOCKET_TIMEOUT_SECONDS = 2.0
HTTP_MAX_REQUEST_LINE = 2048
ROUTES = ("/v1/snapshot", "/v1/limits", "/v1/tokencheck", "/healthz")

EXIT_SOFTWARE = 70


def log(message: str) -> None:
    print(f"tokencheck-daemon: {message}", file=sys.stderr, flush=True)


def iso_ms(value: datetime) -> str:
    """JavaScript `toISOString()` shape, like every timestamp the collector writes."""
    return core.iso_z(value)


def is_nonneg_number(value: Any) -> bool:
    return core.is_number(value) and float(value) >= 0


# ---------------------------------------------------------------------------
# Bounded file reads


class InputError(Exception):
    def __init__(self, state: str, code: str, mtime: datetime | None = None) -> None:
        super().__init__(code)
        self.state = state
        self.code = code
        self.mtime = mtime


def read_capped_json(path: Path, cap: int) -> tuple[Any, datetime]:
    """Parse a regular file of at most `cap` bytes, or raise InputError."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise InputError("unavailable", "file_unavailable") from None
    except OSError:
        raise InputError("unavailable", "file_read_failed") from None
    try:
        info = os.fstat(fd)
        mtime = datetime.fromtimestamp(info.st_mtime, timezone.utc)
        if not stat.S_ISREG(info.st_mode):
            raise InputError("unavailable", "file_read_failed", mtime)
        if info.st_size > cap:
            raise InputError("schema_changed", "cache_too_large", mtime)
        chunks = []
        remaining = cap + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    except InputError:
        raise
    except OSError:
        raise InputError("unavailable", "file_read_failed") from None
    finally:
        os.close(fd)
    if len(payload) > cap:
        raise InputError("schema_changed", "cache_too_large", mtime)
    try:
        return json.loads(payload), mtime
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise InputError("schema_changed", "invalid_json", mtime) from None


# ---------------------------------------------------------------------------
# sources.json (quota collector, W3 `scripts/refresh-tokencheck-sources.ts`)


def parse_sources_envelope(data: Any) -> dict[str, Any] | None:
    """Mirror readSources(): reject anything the collector would refuse too."""
    if not isinstance(data, dict) or data.get("schema_version") != SOURCES_SCHEMA_VERSION:
        return None
    generation = data.get("source_generation")
    failures = data.get("failures")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        return None
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        return None
    if core.parse_time(data.get("next_attempt_at")) is None:
        return None
    raw = data.get("raw")
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), list) \
            or not isinstance(raw.get("sources"), dict) or not isinstance(raw.get("models"), list):
        return None
    return data


# ---------------------------------------------------------------------------
# grok-usage.json (ported from src/pipelines/grok-usage.ts parseGrokUsageCache
# and the Grok branch of UsageLimitsTracker.refresh)

RFC3339_WITH_ZONE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(?:Z|[+-](\d{2}):(\d{2}))$"
)


def valid_iso(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    match = RFC3339_WITH_ZONE.match(value)
    if not match:
        return False
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    off_hour = int(match.group(7)) if match.group(7) is not None else 0
    off_minute = int(match.group(8)) if match.group(8) is not None else 0
    if not 1 <= month <= 12 or day < 1 or hour > 23 or minute > 59 or second > 59 \
            or off_hour > 23 or off_minute > 59:
        return False
    if year < 1 or day > calendar.monthrange(year, month)[1]:
        return False
    return core.parse_time(value) is not None


def valid_payg(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    return is_nonneg_number(value.get("used_usd")) and is_nonneg_number(value.get("limit_usd"))


def parse_grok_usage_cache(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    state = raw.get("state")
    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or schema_version not in (1, 2) \
            or not valid_iso(raw.get("generated_at")) \
            or not isinstance(raw.get("session"), str) or not raw["session"] \
            or not isinstance(raw.get("grok_version"), str) or not raw["grok_version"] \
            or state not in MONTHLY_STATES:
        return None
    data = None
    raw_data = raw.get("data")
    if raw_data is not None:
        if not isinstance(raw_data, dict):
            return None
        used = raw_data.get("used_percent")
        plan = raw_data.get("plan")
        if raw_data.get("source") != "grok_session" \
                or (plan is not None and not isinstance(plan, str)) \
                or not core.is_number(used) or not 0 <= float(used) <= 100 \
                or not valid_iso(raw_data.get("reset_at")) \
                or not valid_iso(raw_data.get("measured_at")) \
                or not valid_payg(raw_data.get("payg")):
            return None
        data = {
            "plan": plan,
            "used_percent": used,
            "reset_at": raw_data["reset_at"],
            "source": "grok_session",
            "measured_at": raw_data["measured_at"],
        }
        if "payg" in raw_data:
            data["payg"] = raw_data["payg"]
    if state == "ok" and data is None:
        return None
    access = None
    raw_access = raw.get("access")
    if schema_version == 2:
        if not isinstance(raw_access, dict):
            return None
        if raw_access.get("state") not in ("usable", "unusable", "unknown") \
                or not isinstance(raw_access.get("reason"), str) or not raw_access["reason"] \
                or (raw_access.get("checked_at") is not None and not valid_iso(raw_access.get("checked_at"))):
            return None
        access = {
            "state": raw_access["state"],
            "reason": raw_access["reason"],
            "checked_at": raw_access.get("checked_at"),
        }
        if access["state"] != "unknown" and access["checked_at"] is None:
            return None
    elif "access" in raw:
        return None
    error_code = raw.get("error_code", None) if "error_code" in raw else None
    has_error = "error_code" in raw
    if has_error and (not isinstance(error_code, str) or not error_code):
        return None
    if (state == "ok") == has_error:
        return None
    cache = {
        "schema_version": schema_version,
        "generated_at": raw["generated_at"],
        "session": raw["session"],
        "grok_version": raw["grok_version"],
        "state": state,
        "data": data,
    }
    if access is not None:
        cache["access"] = access
    if has_error:
        cache["error_code"] = error_code
    return cache


def read_grok_usage(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"cache": None, "state": "unavailable", "attempted_at": None, "error_code": "disabled"}
    try:
        data, mtime = read_capped_json(path, GROK_CACHE_CAP_BYTES)
    except InputError as error:
        return {
            "cache": None,
            "state": error.state,
            "attempted_at": iso_ms(error.mtime) if error.mtime else None,
            "error_code": error.code,
        }
    cache = parse_grok_usage_cache(data)
    if cache is None:
        return {"cache": None, "state": "schema_changed", "attempted_at": iso_ms(mtime),
                "error_code": "invalid_cache"}
    return {"cache": cache, "state": "ok", "attempted_at": cache["generated_at"], "error_code": None}


def grok_stale_reason(credits: dict[str, Any], now: datetime) -> str | None:
    measured = core.parse_time(credits.get("measured_at"))
    if measured is None:
        return "cache_expired"
    age = (now - measured).total_seconds()
    if age < -CLOCK_SKEW_GRACE_SECONDS or age > GROK_USAGE_MAX_AGE_SECONDS:
        return "cache_expired"
    if credits.get("reset_at"):
        reset = core.parse_time(credits["reset_at"])
        if reset is None:
            return "reset_horizon_invalid"
        until = (reset - now).total_seconds()
        if until > GROK_RESET_MAX_FUTURE_SECONDS:
            return "reset_horizon_invalid"
        if until < -RESET_PAST_GRACE_SECONDS:
            return "reset_expired"
    return None


def current_grok_access(access: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    if access is None:
        return {
            "access_state": "unknown",
            "access_reason": "legacy_cache_has_no_access_check",
            "access_checked_at": None,
            "access_trust": "unknown",
            "access_age_seconds": None,
        }
    checked = core.parse_time(access.get("checked_at")) if access.get("checked_at") else None
    age = (now - checked).total_seconds() if checked else None
    age_seconds = None if age is None else max(0, math.floor(age))
    if age is None or age < -CLOCK_SKEW_GRACE_SECONDS:
        return {
            "access_state": "unknown",
            "access_reason": "access_check_time_invalid",
            "access_checked_at": access.get("checked_at"),
            "access_trust": "unknown",
            "access_age_seconds": age_seconds,
        }
    if age > GROK_USAGE_MAX_AGE_SECONDS:
        return {
            "access_state": "unknown",
            "access_reason": "access_check_expired",
            "access_checked_at": access.get("checked_at"),
            "access_trust": "stale",
            "access_age_seconds": age_seconds,
        }
    return {
        "access_state": access["state"],
        "access_reason": access["reason"],
        "access_checked_at": access.get("checked_at"),
        "access_trust": "unknown" if access["state"] == "unknown" else "fresh",
        "access_age_seconds": age_seconds,
    }


def resolve_grok_credits_status(cache_status: str, stale_reason: str | None,
                                access_state: str, access_trust: str) -> str:
    if cache_status == "schema_changed":
        return "schema_changed"
    if cache_status == "ok" and not stale_reason and access_state == "usable" and access_trust == "fresh":
        return "ok"
    if access_state == "unusable" or (access_state == "unknown" and not stale_reason):
        return "unavailable"
    if cache_status == "ok":
        return "stale"
    return cache_status


def grok_provider_from_credits(credits: dict[str, Any]) -> dict[str, Any] | None:
    if credits.get("used_percent") is None:
        return None
    return {
        "provider": "grok",
        "plan": credits.get("plan"),
        "access_state": credits["access_state"],
        "access_reason": credits["access_reason"],
        "access_checked_at": credits["access_checked_at"],
        "access_trust": credits["access_trust"],
        "access_age_seconds": credits["access_age_seconds"],
        "windows": [{
            "label": "WEEK",
            "used_percent": credits["used_percent"],
            "resets_at": credits.get("reset_at"),
            "window_hours": 168,
        }],
    }


# ---------------------------------------------------------------------------
# usage-monthly.json (ported from parseMonthlyUsageCache / cacheSourceStatus)


def valid_monthly_model(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key in ("id", "label", "model"):
        if not isinstance(value.get(key), str):
            return False
    for key in ("tokens_month", "cost_month", "message_count"):
        if value.get(key) is not None and not is_nonneg_number(value.get(key)):
            return False
    return isinstance(value.get("measured_at"), str) and core.parse_time(value["measured_at"]) is not None


def parse_monthly_usage_cache(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("schema_version") != 2 or isinstance(raw.get("schema_version"), bool) \
            or not isinstance(raw.get("period"), str) or not re.fullmatch(r"\d{4}-\d{2}", raw["period"]) \
            or not isinstance(raw.get("generated_at"), str) or core.parse_time(raw["generated_at"]) is None \
            or not isinstance(raw.get("tokscale_version"), str):
        return None
    grok = raw.get("grok")
    models = raw.get("models")
    if not isinstance(grok, dict) or grok.get("state") not in MONTHLY_STATES:
        return None
    if not isinstance(models, dict) or models.get("state") not in MONTHLY_STATES \
            or not isinstance(models.get("data"), list):
        return None
    for section in (grok, models):
        measured = section.get("measured_at")
        if measured is not None and (not isinstance(measured, str) or core.parse_time(measured) is None):
            return None
    if grok.get("data") is not None:
        if not isinstance(grok["data"], dict):
            return None
        for key in ("tokens_month", "cost_month"):
            if grok["data"].get(key) is not None and not is_nonneg_number(grok["data"][key]):
                return None
    if not all(valid_monthly_model(item) for item in models["data"]):
        return None
    return raw


def usage_month_period(now: datetime) -> str:
    return now.astimezone(ICT).strftime("%Y-%m")


def source_status(previous: dict[str, Any] | None, state: str, attempt_at: str, *,
                  success_at: Any = ..., active_source: str | None = None,
                  error_code: str | None = None) -> dict[str, Any]:
    """Port of sourceStatus(): success only moves on a real success."""
    if success_at is ...:
        success_at = attempt_at if state == "ok" else (previous or {}).get("last_success_at")
    success = core.parse_time(success_at) if success_at else None
    attempt = core.parse_time(attempt_at)
    status: dict[str, Any] = {
        "state": state,
        "last_attempt_at": attempt_at,
        "last_success_at": success_at or None,
        "age_seconds": None if success is None or attempt is None
        else max(0, math.floor((attempt - success).total_seconds())),
    }
    if active_source:
        status["active_source"] = active_source
    if error_code:
        status["error_code"] = error_code
    return status


def cache_source_status(previous: dict[str, Any] | None, section: dict[str, Any] | None,
                        generated_at: str | None, now_iso: str, now: datetime,
                        period_matches: bool) -> dict[str, Any]:
    source = "tokscale_monthly_worker"
    if section is None or not generated_at:
        return source_status(previous, "stale" if (previous or {}).get("last_success_at") else "unavailable",
                             now_iso, active_source=source, error_code="cache_unavailable")
    if not period_matches:
        return source_status(previous, "stale", now_iso, active_source=source, error_code="period_mismatch")
    measured = core.parse_time(section.get("measured_at")) if section.get("measured_at") else None
    age = math.inf if measured is None else (now - measured).total_seconds()
    expired = age > MONTHLY_CACHE_MAX_AGE_SECONDS
    state = ("stale" if expired else "ok") if section["state"] == "ok" else section["state"]
    status = source_status(previous, state, now_iso, success_at=section.get("measured_at"),
                           active_source=source,
                           error_code=section.get("error_code") or ("cache_expired" if expired else None))
    status["last_attempt_at"] = generated_at
    return status


def read_monthly_cache(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, "disabled"
    try:
        data, _mtime = read_capped_json(path, MONTHLY_CACHE_CAP_BYTES)
    except InputError as error:
        return None, error.code
    cache = parse_monthly_usage_cache(data)
    return (cache, None) if cache is not None else (None, "invalid_cache")


# ---------------------------------------------------------------------------
# Merge


def provider_named(raw: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), list):
        return None
    for item in raw["providers"]:
        if isinstance(item, dict) and item.get("provider") == name:
            return item
    return None


def provider_copy(previous: dict[str, Any] | None, nxt: dict[str, Any] | None, source: str,
                  measured_at: str | None, stale: bool) -> dict[str, Any] | None:
    provider = nxt if nxt is not None else previous
    if provider is None:
        return None
    out = json.loads(json.dumps(provider))
    out["source"] = source
    out["measured_at"] = measured_at if measured_at is not None else provider.get("measured_at")
    out["stale"] = stale
    return out


def quota_source_makes_snapshot_stale(status: Any) -> bool:
    if not isinstance(status, dict):
        return False
    if status.get("state") == "ok":
        return False
    return status.get("error_code") != "disabled"


def latest_successful_at(sources: dict[str, Any], fallback: str) -> str:
    latest = fallback
    latest_time = core.parse_time(fallback) if fallback else None
    for status in sources.values():
        if not isinstance(status, dict) or not status.get("last_success_at"):
            continue
        when = core.parse_time(status["last_success_at"])
        if when is not None and (latest_time is None or when > latest_time):
            latest, latest_time = status["last_success_at"], when
    return latest


def merge_raw(quota: dict[str, Any] | None, quota_problem: str | None, grok_read: dict[str, Any],
              monthly: dict[str, Any] | None, previous: dict[str, Any] | None,
              now: datetime) -> dict[str, Any]:
    """One `/api/usage/limits`-shaped snapshot from the three caches.

    `quota` is the last-good collector raw (None if there never was one) and
    `quota_problem` says why the current sources.json could not be used. The
    merge never invents a success: every last_success_at comes from a cache.
    """
    now_iso = iso_ms(now)
    previous = previous if isinstance(previous, dict) else {}
    prev_sources = previous.get("sources") if isinstance(previous.get("sources"), dict) else {}
    quota_raw = quota or {}
    quota_sources = quota_raw.get("sources") if isinstance(quota_raw.get("sources"), dict) else {}
    sources: dict[str, Any] = {}

    collected: dict[str, dict[str, Any]] = {}
    for name in COLLECTOR_PROVIDERS:
        item = provider_named(quota_raw, name)
        if item is not None:
            collected[name] = json.loads(json.dumps(item))
    for key in QUOTA_SOURCE_KEYS:
        status = quota_sources.get(key)
        if isinstance(status, dict):
            sources[key] = json.loads(json.dumps(status))
    if quota_problem:
        # The collector output on disk is unreadable. Keep last-good numbers as
        # history but never let them pass as current.
        for key in QUOTA_SOURCE_KEYS:
            prior = sources.get(key)
            sources[key] = source_status(
                prior, "stale" if (prior or {}).get("last_success_at") else "unavailable",
                (prior or {}).get("last_attempt_at") or now_iso,
                success_at=(prior or {}).get("last_success_at"),
                active_source=(prior or {}).get("active_source"),
                error_code=quota_problem,
            )
        for item in collected.values():
            item["stale"] = True

    # Grok weekly quota.
    cache = grok_read["cache"]
    access = current_grok_access(cache.get("access") if cache else None, now)
    cached_credits = None
    if cache and cache.get("data"):
        data = cache["data"]
        cached_credits = {
            "plan": data["plan"],
            "used_percent": data["used_percent"],
            "reset_at": data["reset_at"],
            "source": "grok_session",
            "status": cache["state"],
            "measured_at": data["measured_at"],
            **access,
        }
    stale_reason = grok_stale_reason(cached_credits, now) if cached_credits else None
    credits = None
    if cached_credits:
        credits = dict(cached_credits)
        credits["status"] = resolve_grok_credits_status(
            cached_credits["status"], stale_reason, cached_credits["access_state"], cached_credits["access_trust"])
    grok_provider = grok_provider_from_credits(credits) if credits else None
    previous_grok = provider_named(previous, "grok")
    previous_grok_status = prev_sources.get("grok_quota") or prev_sources.get("grok_manual")
    grok_entry = None
    if grok_provider:
        grok_entry = provider_copy(previous_grok, grok_provider, "grok_session",
                                   credits.get("measured_at") or now_iso, credits["status"] != "ok")
        if credits["status"] != "ok":
            grok_state = credits["status"]
        else:
            grok_state = "stale" if stale_reason else "ok"
        grok_error = (cache.get("error_code") if cache else None) or stale_reason
        if not grok_error and (credits["access_state"] != "usable" or credits["access_trust"] != "fresh"):
            grok_error = credits["access_reason"]
        sources["grok_quota"] = source_status(previous_grok_status, grok_state, now_iso,
                                              success_at=credits.get("measured_at") or now_iso,
                                              active_source="grok_session", error_code=grok_error)
    else:
        grok_entry = provider_copy(previous_grok, None, (previous_grok or {}).get("source") or "grok_session",
                                   (previous_grok or {}).get("measured_at"), True)
        reported = (cache or {}).get("state") if grok_read["state"] == "ok" else grok_read["state"]
        if reported == "schema_changed":
            no_data_state = "schema_changed"
        elif previous_grok:
            no_data_state = "stale"
        else:
            no_data_state = reported or "unavailable"
        sources["grok_quota"] = source_status(
            previous_grok_status, no_data_state, now_iso, active_source="grok_session",
            error_code=grok_read["error_code"] or (cache or {}).get("error_code") or "file_unavailable")
    sources["grok_quota"]["last_attempt_at"] = grok_read["attempted_at"] or now_iso
    sources["grok_manual"] = dict(sources["grok_quota"])

    # Monthly token totals (never quota).
    period = usage_month_period(now)
    period_matches = bool(monthly) and monthly.get("period") == period
    generated_at = monthly.get("generated_at") if monthly else None
    sources["grok_monthly"] = cache_source_status(
        prev_sources.get("grok_monthly"), monthly.get("grok") if monthly else None,
        generated_at, now_iso, now, period_matches)
    sources["models_monthly"] = cache_source_status(
        prev_sources.get("models_monthly"), monthly.get("models") if monthly else None,
        generated_at, now_iso, now, period_matches)
    cached_grok_month = monthly["grok"].get("data") if period_matches else None
    cached_models = monthly["models"]["data"] if period_matches else None
    previous_grok_usage = previous.get("grok") if isinstance(previous.get("grok"), dict) else None
    previous_same_period = previous.get("monthly_period") == period
    grok_base = cached_grok_month
    if grok_base is None and previous_same_period and previous_grok_usage:
        grok_base = {"tokens_month": previous_grok_usage.get("tokens_month"),
                     "cost_month": previous_grok_usage.get("cost_month")}
    snapshot_credits = None
    if credits:
        snapshot_credits = {key: value for key, value in credits.items()}
    elif previous_grok_usage and isinstance(previous_grok_usage.get("credits"), dict):
        snapshot_credits = dict(previous_grok_usage["credits"])
        snapshot_credits["status"] = "schema_changed" if sources["grok_quota"]["state"] == "schema_changed" \
            else "stale"
    grok_usage = None
    if grok_base or snapshot_credits:
        grok_usage = {
            "tokens_month": (grok_base or {}).get("tokens_month"),
            "cost_month": (grok_base or {}).get("cost_month"),
            "credits": snapshot_credits,
        }
    if cached_models is not None:
        models = json.loads(json.dumps(cached_models))
    elif previous_same_period and isinstance(previous.get("models"), list):
        models = previous["models"]
    else:
        models = []

    providers = [collected[name] for name in ("claude", "codex") if name in collected]
    if grok_entry:
        providers.append(grok_entry)
    if "agy" in collected:
        providers.append(collected["agy"])

    stale = (sources.get("claude_quota") or {}).get("state") != "ok" \
        or (sources.get("codex_quota") or {}).get("state") != "ok" \
        or quota_source_makes_snapshot_stale(sources.get("grok_quota")) \
        or quota_source_makes_snapshot_stale(sources.get("agy_quota"))
    fallback_measured = quota_raw.get("measured_at") if isinstance(quota_raw.get("measured_at"), str) else ""
    if not fallback_measured and isinstance(previous.get("measured_at"), str):
        fallback_measured = previous["measured_at"]
    return {
        "providers": providers,
        "grok": grok_usage,
        "models": models,
        "measured_at": latest_successful_at(sources, fallback_measured),
        "stale": stale,
        "sources": sources,
        "monthly_period": period,
    }


def source_report(raw: dict[str, Any], next_attempt_at: str | None, now: datetime) -> dict[str, Any]:
    """Per-source freshness as seen at this tick (age from the source's own success)."""
    report = {}
    for key, status in sorted(raw["sources"].items()):
        if key == "grok_manual" or not isinstance(status, dict):
            continue
        success = core.parse_time(status.get("last_success_at")) if status.get("last_success_at") else None
        report[key] = {
            "state": status.get("state"),
            "observed_at": status.get("last_success_at"),
            "last_success_at": status.get("last_success_at"),
            "last_attempt_at": status.get("last_attempt_at"),
            "age_seconds": None if success is None else round((now - success).total_seconds()),
            "max_age_seconds": core.SOURCE_MAX_AGE_SECONDS.get(key),
            "error_code": status.get("error_code"),
            "active_source": status.get("active_source"),
            "next_attempt_at": next_attempt_at if key in QUOTA_SOURCE_KEYS else None,
        }
    return report


# ---------------------------------------------------------------------------
# Durable state

CLOCK_AGE_KEYS = {"age_seconds", "access_age_seconds"}


def without_clock_ages(value: Any) -> Any:
    """Drop fields recomputed from the clock every tick: they are not new data."""
    if isinstance(value, dict):
        return {k: without_clock_ages(v) for k, v in value.items() if k not in CLOCK_AGE_KEYS}
    if isinstance(value, list):
        return [without_clock_ages(item) for item in value]
    return value



def ensure_state_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(errno.ENOTDIR, "state path is not a directory", str(path))
    if info.st_uid != os.getuid():
        raise OSError(errno.EPERM, "state directory owned by another user", str(path))
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)


def atomic_write(path: Path, payload: bytes) -> None:
    """temp in the same directory → fsync → rename → fsync directory."""
    directory = path.parent
    tmp = directory / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.close(fd)
    try:
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def acquire_lock(path: Path) -> int:
    """Exclusive for the life of the process; the kernel drops it on any exit."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def load_previous_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        data, _mtime = read_capped_json(path, SNAPSHOT_CAP_BYTES)
    except InputError:
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        return None
    raw = data.get("raw")
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), list) \
            or not isinstance(raw.get("sources"), dict):
        return None
    return data


# ---------------------------------------------------------------------------
# Engine


class Engine:
    """Owns the tick and the one published generation. Thread-safe reads."""

    def __init__(self, state_dir: Path, grok_file: Path | None, monthly_file: Path | None,
                 clock=None, enabled_providers=None) -> None:
        self.enabled_providers = enabled_providers
        self.state_dir = state_dir
        self.sources_file = state_dir / "sources.json"
        self.snapshot_file = state_dir / "snapshot.json"
        self.grok_file = grok_file
        self.monthly_file = monthly_file
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.started_at = self.clock()
        self.lock = threading.Lock()
        self.published: dict[str, Any] | None = None   # {"envelope", "bytes", ...}
        self.sequence = 0
        self.tick_count = 0
        self.tick_errors = 0
        self.missed_ticks = 0
        self.last_error: str | None = None
        self.last_tick_ms: float | None = None
        self.last_persist_mono: float | None = None
        self.last_persist_digest: str | None = None
        self.persist_errors = 0
        self.quota_last_good: dict[str, Any] | None = None
        self.quota_generation: int | None = None
        self.quota_generated_at: datetime | None = None
        self.quota_next_attempt: str | None = None
        self.last_logged: str | None = None
        self.previous_raw: dict[str, Any] | None = None
        prior = load_previous_snapshot(self.snapshot_file)
        if prior is not None:
            seq = prior.get("sequence")
            if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
                # The disk copy may lag the last served generation by up to
                # PERSIST_MAX_INTERVAL_SECONDS of ticks; skip past them so a
                # restart never reuses a sequence a reader already saw.
                self.sequence = seq + SEQUENCE_RESTART_GAP
            self.previous_raw = prior["raw"]
            # Restart keeps last-good collector numbers as history only; the
            # sources' own timestamps still decide whether they are fresh.
            self.quota_last_good = {
                "providers": [p for p in prior["raw"]["providers"]
                              if isinstance(p, dict) and p.get("provider") in COLLECTOR_PROVIDERS],
                "sources": {k: v for k, v in prior["raw"]["sources"].items() if k in QUOTA_SOURCE_KEYS},
                "measured_at": prior["raw"].get("measured_at"),
                "models": [],
            }
            gen = prior.get("source_generation")
            self.quota_generation = gen if isinstance(gen, int) and not isinstance(gen, bool) else None

    def read_quota(self) -> str | None:
        """Refresh last-good collector raw. Returns why the file is unusable, if it is."""
        try:
            data, _mtime = read_capped_json(self.sources_file, SOURCES_CAP_BYTES)
        except InputError as error:
            return "sources_missing" if error.code == "file_unavailable" else f"sources_{error.code}"
        envelope = parse_sources_envelope(data)
        if envelope is None:
            return "sources_invalid_schema"
        generation = envelope["source_generation"]
        generated_at = core.parse_time(envelope.get("generated_at"))
        if self.quota_generation is not None and generation < self.quota_generation \
                and self.quota_last_good is not None \
                and (generated_at is None or self.quota_generated_at is None
                     or generated_at <= self.quota_generated_at):
            # An older file restored over a newer one: never move backwards. A
            # collector whose state was reset restarts at 1 with a newer
            # generated_at and is accepted.
            return "sources_generation_regressed"
        self.quota_last_good = envelope["raw"]
        self.quota_generation = generation
        self.quota_generated_at = generated_at
        self.quota_next_attempt = envelope["next_attempt_at"]
        return None

    def tick(self) -> dict[str, Any]:
        started = time.monotonic()
        now = self.clock()
        quota_problem = self.read_quota()
        grok_read = read_grok_usage(self.grok_file)
        monthly, monthly_problem = read_monthly_cache(self.monthly_file)
        raw = merge_raw(self.quota_last_good, quota_problem, grok_read, monthly, self.previous_raw, now)
        if self.enabled_providers is not None:
            raw['enabled_providers'] = list(self.enabled_providers)
            raw['providers'] = [p for p in raw['providers'] if p.get('provider') in self.enabled_providers]
        results, exit_codes = core.analyze_views(raw, now)
        with self.lock:
            sequence = self.sequence + 1
        inputs = {
            "sources_json": {"state": "ok" if quota_problem is None else "error", "error_code": quota_problem,
                             "source_generation": self.quota_generation,
                             "next_attempt_at": self.quota_next_attempt},
            "grok_usage": {"state": grok_read["state"], "error_code": grok_read["error_code"]},
            "usage_monthly": {"state": "ok" if monthly_problem is None else "error",
                              "error_code": monthly_problem},
        }
        envelope = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "engine_version": core.ENGINE_VERSION,
            "daemon_version": DAEMON_VERSION,
            "source_generation": self.quota_generation,
            "sequence": sequence,
            "computed_at": iso_ms(now),
            "raw": raw,
            "results": results,
            "exit_codes": exit_codes,
            "sources": source_report(raw, self.quota_next_attempt, now),
            "inputs": inputs,
            "service": {
                "tick_seconds": TICK_SECONDS,
                "ready_max_age_seconds": READY_MAX_AGE_SECONDS,
                "started_at": iso_ms(self.started_at),
                "tick_count": self.tick_count + 1,
                "tick_errors": self.tick_errors,
                "missed_ticks": self.missed_ticks,
                "persist_errors": self.persist_errors,
            },
        }
        body = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if len(body) > SNAPSHOT_CAP_BYTES:
            raise ValueError("snapshot_too_large")
        published = {
            "envelope": envelope,
            "bytes": body,
            "computed_at": now,
            "limits": json.dumps(self.limits_view(envelope), ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode(),
            "views": {view: json.dumps({
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "engine_version": core.ENGINE_VERSION,
                "computed_at": envelope["computed_at"],
                "sequence": sequence,
                "provider": view,
                "exit_code": exit_codes[view],
                "result": results[view],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() for view in core.VIEWS},
        }
        with self.lock:
            self.sequence = sequence
            self.tick_count += 1
            self.published = published
            self.previous_raw = raw
            self.last_error = None
            self.last_tick_ms = round((time.monotonic() - started) * 1000, 1)
        self.persist(envelope, body)
        if self.last_logged is not None and self.last_logged.startswith("tick failed"):
            log("tick recovered")
            self.last_logged = None
        return envelope

    @staticmethod
    def limits_view(envelope: dict[str, Any]) -> dict[str, Any]:
        """Raw `/api/usage/limits` fields plus the canonical verdict, additive only."""
        view = dict(envelope["raw"])
        view["computed_at"] = envelope["computed_at"]
        view["sequence"] = envelope["sequence"]
        view["engine_version"] = envelope["engine_version"]
        view["tokencheck"] = envelope["results"]["all"]
        view["tokencheck_exit_code"] = envelope["exit_codes"]["all"]
        view["source_freshness"] = envelope["sources"]
        return view

    def persist(self, envelope: dict[str, Any], body: bytes) -> None:
        digest_input = json.dumps({"raw": without_clock_ages(envelope["raw"]),
                                   "g": envelope["source_generation"]}, sort_keys=True).encode()
        digest = hashlib.sha256(digest_input).hexdigest()
        mono = time.monotonic()
        if digest == self.last_persist_digest and self.last_persist_mono is not None \
                and mono - self.last_persist_mono < PERSIST_MAX_INTERVAL_SECONDS:
            return
        try:
            atomic_write(self.snapshot_file, body)
        except OSError as error:
            # Disk trouble must not stop the live feed; the file keeps last-good.
            self.persist_errors += 1
            self.log_once(f"persist failed: {error.__class__.__name__} errno={getattr(error, 'errno', None)}")
            return
        self.last_persist_digest = digest
        self.last_persist_mono = mono

    def safe_tick(self) -> None:
        try:
            self.tick()
        except Exception as error:  # keep serving the last generation; it will age out
            with self.lock:
                self.tick_errors += 1
                self.last_error = error.__class__.__name__
            self.log_once(f"tick failed: {error.__class__.__name__}: {error}")

    def log_once(self, message: str) -> None:
        # A failure that repeats every 5 s must not flood the journal.
        if message != self.last_logged:
            log(message)
            self.last_logged = message

    def health(self) -> tuple[int, dict[str, Any]]:
        with self.lock:
            published = self.published
            info = {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "live": True,
                "engine_version": core.ENGINE_VERSION,
                "daemon_version": DAEMON_VERSION,
                "sequence": self.sequence if published else None,
                "tick_count": self.tick_count,
                "tick_errors": self.tick_errors,
                "missed_ticks": self.missed_ticks,
                "persist_errors": self.persist_errors,
                "last_error": self.last_error,
                "last_tick_ms": self.last_tick_ms,
            }
        now = self.clock()
        if published is None:
            info.update(ready=False, computed_at=None, age_seconds=None, reason_code="no_tick_yet")
            return HTTPStatus.SERVICE_UNAVAILABLE, info
        age = (now - published["computed_at"]).total_seconds()
        ready = -5 <= age <= READY_MAX_AGE_SECONDS
        info.update(ready=ready, computed_at=iso_ms(published["computed_at"]), age_seconds=round(age, 3),
                    reason_code=None if ready else ("service_clock_future" if age < -5 else "service_stale"))
        return (HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE), info


def run_ticks(engine: Engine, stop: threading.Event, tick_seconds: float = TICK_SECONDS) -> None:
    """Monotonic schedule; after a stall or suspend skip the missed ticks, never burst."""
    next_at = time.monotonic()
    while not stop.is_set():
        engine.safe_tick()
        next_at += tick_seconds
        now = time.monotonic()
        if now >= next_at:
            missed = int((now - next_at) // tick_seconds) + 1
            with engine.lock:
                engine.missed_ticks += missed
            next_at += missed * tick_seconds
        stop.wait(max(0.0, next_at - time.monotonic()))


# ---------------------------------------------------------------------------
# HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "pacer-tokencheck"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = HTTP_SOCKET_TIMEOUT_SECONDS

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # no per-request log; a busy UI must not fill the journal

    def setup(self) -> None:
        super().setup()
        self.admitted = self.server.slots.acquire(blocking=False)  # type: ignore[attr-defined]

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            if self.admitted:
                self.server.slots.release()  # type: ignore[attr-defined]

    def send_json(self, status: int, body: bytes, *, head: bool = False,
                  extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def send_error_json(self, status: int, code: str, *, head: bool = False,
                        extra: dict[str, str] | None = None) -> None:
        body = json.dumps({"error": code}).encode()
        self.send_json(status, body, head=head, extra=extra)

    def reject_other_method(self) -> None:
        self.send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", extra={"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = reject_other_method

    def do_GET(self) -> None:
        self.dispatch(head=False)

    def do_HEAD(self) -> None:
        self.dispatch(head=True)

    def host_allowed(self) -> bool:
        # DNS-rebinding guard: a browser page cannot make its Host header say loopback.
        host = self.headers.get("Host")
        if host is None:
            return True  # HTTP/1.0 clients may omit it; the socket is loopback-only anyway
        port = self.server.server_address[1]
        return host in {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}

    def dispatch(self, *, head: bool) -> None:
        if not self.admitted:
            self.send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "busy", head=head, extra={"Retry-After": "1"})
            return
        if len(self.requestline) > HTTP_MAX_REQUEST_LINE:
            self.send_error_json(HTTPStatus.REQUEST_URI_TOO_LONG, "uri_too_long", head=head)
            return
        if not self.host_allowed():
            self.send_error_json(HTTPStatus.MISDIRECTED_REQUEST, "host_not_allowed", head=head)
            return
        length = self.headers.get("Content-Length")
        if (length is not None and length.strip() not in {"", "0"}) or self.headers.get("Transfer-Encoding"):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "body_not_allowed", head=head)
            return
        parts = urlsplit(self.path)
        if parts.scheme or parts.netloc or parts.fragment:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "absolute_uri_not_allowed", head=head)
            return
        engine: Engine = self.server.engine  # type: ignore[attr-defined]
        route = parts.path
        if route not in ROUTES:
            self.send_error_json(HTTPStatus.NOT_FOUND, "unknown_route", head=head)
            return
        try:
            query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query),
                              max_num_fields=4)
        except ValueError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid_query", head=head)
            return
        if route == "/healthz":
            if query:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "unexpected_query", head=head)
                return
            status, info = engine.health()
            self.send_json(status, json.dumps(info, sort_keys=True).encode(), head=head)
            return
        with engine.lock:
            published = engine.published
        if published is None:
            self.send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "no_tick_yet", head=head,
                                 extra={"Retry-After": "5"})
            return
        if route == "/v1/tokencheck":
            names = [key for key, _value in query]
            if any(key != "provider" for key in names) or len(names) > 1:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "unexpected_query", head=head)
                return
            provider = query[0][1] if query else "all"
            if provider not in core.VIEWS:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "unknown_provider", head=head)
                return
            self.send_json(HTTPStatus.OK, published["views"][provider], head=head)
            return
        if query:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "unexpected_query", head=head)
            return
        body = published["bytes"] if route == "/v1/snapshot" else published["limits"]
        self.send_json(HTTPStatus.OK, body, head=head)


socketserver_bind = TCPServer.server_bind


class LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, port: int, engine: Engine) -> None:
        self.engine = engine
        self.slots = threading.BoundedSemaphore(HTTP_MAX_CONCURRENT)
        super().__init__((LOOPBACK_HOST, port), Handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind() calls socket.getfqdn(), a reverse name lookup
        # (nscd/DNS). The service must not touch the network beyond loopback.
        socketserver_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


# ---------------------------------------------------------------------------
# Entry


def self_test() -> int:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    raw = merge_raw(None, "sources_missing", read_grok_usage(None), None, None, now)
    results, exit_codes = core.analyze_views(raw, now)
    if set(results) != set(core.VIEWS) or any(code not in (0, 1, 2, 3) for code in exit_codes.values()):
        return EXIT_SOFTWARE
    print(json.dumps({"engine_version": core.ENGINE_VERSION, "daemon_version": DAEMON_VERSION,
                      "views": sorted(results), "exit_codes": exit_codes}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Compatibility launcher; the full daemon owns process lifecycle."""
    argv = sys.argv[1:] if argv is None else argv
    if argv == ['--self-test']:
        return self_test()
    from pacer_daemon import main as run
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
