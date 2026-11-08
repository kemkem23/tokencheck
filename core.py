"""Pure tokencheck engine: snapshot analysis, pacing verdicts and the chart.

Shared by the CLI client (tokencheck.py) and the 5-second service daemon so
both quote one formula. Everything here is a pure function of (raw snapshot,
clock): no subprocess, no network, no filesystem and no environment reads.
The CLI and the daemon own every side effect.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any


# Bump when any computed field, rounding, threshold or exit rule changes. The
# CLI compares this with the service's engine_version before trusting a
# precomputed view.
ENGINE_VERSION = 14
MAX_RESPONSE_BYTES = 1024 * 1024
FUTURE_CLOCK_GRACE_SECONDS = 5 * 60
RESET_GRACE_SECONDS = 10 * 60
PROVIDERS = ("claude", "codex", "grok", "agy")
# Every view the service precomputes: `all` plus one per provider, so the exit
# code of `--provider claude` never absorbs another provider's state.
VIEWS = ("all",) + PROVIDERS
SOURCE_KEYS = {
    "claude": ("claude_quota",),
    "codex": ("codex_quota",),
    "grok": ("grok_quota", "grok_manual"),
    "agy": ("agy_quota",),
}
SOURCE_MAX_AGE_SECONDS = {
    "claude_quota": 20 * 60,
    "codex_quota": 20 * 60,
    "grok_quota": 45 * 60,
    "grok_manual": 45 * 60,
    "agy_quota": 20 * 60,
    "grok_monthly": 13 * 60 * 60,
    "models_monthly": 13 * 60 * 60,
}
WINDOW_HOURS = {
    "5H": 5, "WEEK": 168,
    "GEM 5H": 5, "3P 5H": 5,
    "GEM WEEK": 168, "3P WEEK": 168,
}
WINDOW_MAX_FUTURE_SECONDS = {5: 6 * 60 * 60, 168: 8 * 24 * 60 * 60}
AGY_BUCKETS = (
    ("gemini-5h", "5h", "GEM 5H", 5),
    ("3p-5h", "5h", "3P 5H", 5),
    ("gemini-weekly", "weekly", "GEM WEEK", 168),
    ("3p-weekly", "weekly", "3P WEEK", 168),
)
def _agy_sibling_bucket_id(bucket_id: str) -> str | None:
    """Return the paired bucket ID for a given agy bucket (5h ↔ weekly)."""
    if bucket_id.endswith("-5h"):
        return bucket_id[:-3] + "-weekly"
    if bucket_id.endswith("-weekly"):
        return bucket_id[:-7] + "-5h"
    return None


SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/-]{1,80}$")
ICT = timezone(timedelta(hours=7), name="ICT")

# Chart glyphs use light cells for readable terminal output:
# font `░` renders as a dense stipple that is nearly indistinguishable from the
# solid block, so the used/remaining split was unreadable. A solid block against
# a single mid-dot is the widest contrast available in one monospace cell, and
# ASCII `|` marks pace inside the bar (requested 2026-10-02). Every bar is
# printed next to its own number so glyphs are never the only proportion claim.
CHART_FILL = "█"
CHART_EMPTY = "·"
CHART_CARET = "|"  # Public compatibility name; marker now lives inside the bar.
CHART_DEFAULT_WIDTH = 48
CHART_MIN_WIDTH = 10
CHART_MAX_WIDTH = 200
# Lane budget from delta_vs_pace_pp. Mirrors the "ด่าน pace" rule in AGENTS.md.
LANE_THRESHOLDS = ((-10.0, 10), (-3.0, 5), (3.0, 3))
LANE_ABOVE_PACE = 1
ANSI = {"green": "\033[32m", "amber": "\033[33m", "red": "\033[31m", "reset": "\033[0m"}


class TokencheckError(Exception):
    def __init__(self, code: str, exit_code: int = 3):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def number_or_none(value: Any, *, minimum: float | None = None) -> float | int | None:
    if not is_number(value):
        return None
    number = float(value)
    if minimum is not None and number < minimum:
        return None
    return int(number) if number.is_integer() else number


def safe_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not SAFE_IDENTIFIER.fullmatch(value):
        return None
    return value


def safe_plan(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 80 or any(ord(char) < 32 for char in value):
        return None
    return value


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_ict(value: datetime) -> str:
    return value.astimezone(ICT).isoformat(timespec="milliseconds")


def rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def format_duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds <= 0:
        overdue = abs(seconds)
        if overdue < 60:
            return "due"
        return f"expired {overdue // 60}m"
    if seconds >= 86400:
        return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 60}m"


def compute_pace(reset_at: datetime | None, window_hours: int, now: datetime) -> float | None:
    if reset_at is None or window_hours <= 0:
        return None
    window_seconds = window_hours * 3600
    elapsed = window_seconds - (reset_at - now).total_seconds()
    return max(0.0, min(100.0, elapsed / window_seconds * 100.0))


def ui_color(used_percent: float | None, pace_percent: float | None) -> str:
    if used_percent is None:
        return "none"
    if used_percent >= 98:
        return "red"
    if pace_percent is not None and used_percent > pace_percent + 3:
        return "amber"
    return "green"


def reset_state(reset_at: datetime | None, window_hours: int, now: datetime) -> tuple[str, int | None]:
    if reset_at is None:
        return "missing_or_invalid", None
    seconds = round((reset_at - now).total_seconds())
    if seconds < -RESET_GRACE_SECONDS:
        return "expired", seconds
    if seconds <= 0:
        return "due", seconds
    if seconds > WINDOW_MAX_FUTURE_SECONDS.get(window_hours, window_hours * 3600 * 1.2):
        return "beyond_contract", seconds
    return "future", seconds


def source_health(
    sources: dict[str, Any],
    keys: tuple[str, ...],
    now: datetime,
    fallback_measured_at: Any = None,
) -> dict[str, Any]:
    key = next((candidate for candidate in keys if isinstance(sources.get(candidate), dict)), keys[0])
    raw = sources.get(key) if isinstance(sources.get(key), dict) else None
    state = safe_identifier(raw.get("state")) if raw else None
    state = state if state in {"ok", "stale", "unavailable", "schema_changed"} else "unknown"
    last_attempt_at = raw.get("last_attempt_at") if raw and parse_time(raw.get("last_attempt_at")) else None
    last_success_at = raw.get("last_success_at") if raw and parse_time(raw.get("last_success_at")) else None
    if last_success_at is None and parse_time(fallback_measured_at):
        last_success_at = fallback_measured_at
    success_time = parse_time(last_success_at)
    observed_age = round((now - success_time).total_seconds()) if success_time else None
    max_age = SOURCE_MAX_AGE_SECONDS.get(key)
    time_valid = observed_age is not None and observed_age >= -FUTURE_CLOCK_GRACE_SECONDS
    within_age = time_valid and (max_age is None or observed_age <= max_age)
    fresh = state == "ok" and within_age
    if fresh:
        trust = "fresh"
    elif success_time is not None:
        trust = "last_good"
    elif state in {"unavailable", "schema_changed", "stale"}:
        trust = "invalid"
    else:
        trust = "unknown"
    reason = None
    if state == "ok" and not time_valid:
        reason = "source_timestamp_invalid"
    elif state == "ok" and not within_age:
        reason = "source_too_old"
    elif state != "ok":
        reason = f"source_{state}"
    return {
        "key": key,
        "source_state": state,
        "trust": trust,
        "fresh": fresh,
        "active_source": safe_identifier(raw.get("active_source")) if raw else None,
        "last_attempt_at": last_attempt_at,
        "last_success_at": last_success_at,
        "observed_age_seconds": observed_age,
        "max_age_seconds": max_age,
        "error_code": safe_identifier(raw.get("error_code")) if raw else None,
        "reason_code": reason,
    }


def analyze_window(raw: dict[str, Any], now: datetime, provider: str | None = None, measured_at: Any = None) -> tuple[dict[str, Any] | None, str | None]:
    label_raw = raw.get("label")
    label = label_raw.strip().upper() if isinstance(label_raw, str) else ""
    if label == "WEEKLY":
        label = "WEEK"
    expected_hours = WINDOW_HOURS.get(label)
    hours_raw = raw.get("window_hours")
    if expected_hours is None:
        expected_hours = 168 if label.endswith(' WEEK') else (5 if label.endswith('5H') else None)
    if expected_hours is None and provider == 'claude' and label and is_number(hours_raw) and hours_raw > 0:
        expected_hours = float(hours_raw)
    if expected_hours is None or not is_number(hours_raw) or float(hours_raw) != expected_hours:
        return None, "invalid_window_identity"
    used_raw = raw.get("used_percent")
    used = float(used_raw) if is_number(used_raw) and 0 <= float(used_raw) <= 100 else None
    if used_raw is not None and used is None:
        return None, "invalid_used_percent"
    reset_raw = raw.get("resets_at")
    reset = parse_time(reset_raw)
    state, seconds = reset_state(reset, expected_hours, now)
    pace = compute_pace(reset, expected_hours, now)
    measured_at = raw.get("measured_at") or measured_at
    measured = parse_time(measured_at)
    not_started = (provider == 'agy' and used == 0 and reset is not None and measured is not None
                   and abs((reset - measured).total_seconds() - expected_hours * 3600) <= 120)
    if not_started:
        pace = None
    color = ui_color(used, pace)
    if color == "red":
        assessment = "critical"
    elif not_started:
        assessment = "not_started"
    elif state != "future" or pace is None or used is None:
        assessment = "unknown"
    elif color == "amber":
        assessment = "ahead_of_pace"
    else:
        assessment = "safe"
    return {
        "label": label,
        "source": safe_identifier(raw.get("source")),
        "measured_at": measured_at if measured else None,
        "age_seconds": max(0, round((now - measured).total_seconds(), 3)) if measured else None,
        "window_hours": expected_hours,
        "used_percent": rounded(used),
        "remaining_percent": rounded(100 - used) if used is not None else None,
        "resets_at": reset_raw if reset else None,
        "resets_at_ict": iso_ict(reset) if reset else None,
        "seconds_to_reset": seconds,
        "resets_in": format_duration(seconds),
        "reset_state": state,
        "started": not not_started,
        "pace_percent": rounded(pace),
        "delta_vs_pace_pp": rounded(used - pace) if used is not None and pace is not None else None,
        "ui_color": color,
        "pacing_assessment": assessment,
    }, None


def window_blocks_reliability(window: dict[str, Any]) -> bool:
    """True when this window fail-closes the whole provider.

    `due` is the post-reset grace declared by RESET_GRACE_SECONDS. The
    window's own numbers belong to the cycle that just ended, so
    analyze_window already marks it unknown — that is honest for the
    window and must not overwrite siblings or set reliable=False.
    expired / missing_or_invalid / beyond_contract are real contract
    failures and still fail-close, as does any other unknown.
    """
    state = window.get("reset_state")
    if state in {"expired", "missing_or_invalid", "beyond_contract"}:
        return True
    return window.get("pacing_assessment") == "unknown" and state != "due"


def provider_result(
    name: str,
    raw: dict[str, Any] | None,
    sources: dict[str, Any],
    snapshot_stale: bool,
    snapshot_stale_valid: bool,
    now: datetime,
    degraded_reason: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if degraded_reason:
        warnings.append(f"{degraded_reason}:{name}")
    measured_at = raw.get("measured_at") if raw and parse_time(raw.get("measured_at")) else None
    health = source_health(sources, SOURCE_KEYS[name], now, measured_at)
    if raw is None or "stale" not in raw:
        provider_stale = snapshot_stale
        if not snapshot_stale_valid:
            warnings.append(f"snapshot_stale_invalid:{name}")
    elif isinstance(raw.get("stale"), bool):
        provider_stale = raw["stale"]
    else:
        provider_stale = True
        warnings.append(f"provider_stale_invalid:{name}")
    health["provider_stale"] = provider_stale
    if raw is None:
        warnings.append(f"provider_missing:{name}")
        return {
            "provider": name,
            "plan": None,
            "source": None,
            "health": health,
            "coverage": {"present_windows": [], "missing_windows": []},
            "status": "unknown",
            "reliable": False,
            "windows": [],
        }, warnings

    windows_raw = raw.get("windows")
    invalid = not isinstance(windows_raw, list) or not windows_raw
    windows: list[dict[str, Any]] = []
    labels: set[str] = set()
    if isinstance(windows_raw, list):
        for item in windows_raw:
            if not isinstance(item, dict):
                invalid = True
                warnings.append(f"invalid_window:{name}")
                continue
            window, error = analyze_window(item, now, name, measured_at)
            if error or window is None or window["label"] in labels:
                invalid = True
                warnings.append(f"{error or 'duplicate_window'}:{name}")
                continue
            labels.add(window["label"])
            windows.append(window)
    windows.sort(key=lambda item: item["window_hours"])

    required = (
        {"WEEK"} if name == "grok"
        else ({"GEM 5H", "3P 5H", "GEM WEEK", "3P WEEK"} if name == "agy" else set())
    )
    missing = sorted(required - labels)
    if missing:
        warnings.append(f"window_coverage_partial:{name}")
    if health["reason_code"]:
        warnings.append(f"{health['reason_code']}:{name}")
    if provider_stale:
        warnings.append(f"last_good_only:{name}")

    assessments = [window["pacing_assessment"] for window in windows]
    reliable = (
        degraded_reason is None
        and not invalid
        and bool(windows)
        and not missing
        and health["fresh"]
        and not provider_stale
        and not any(window_blocks_reliability(window) for window in windows)
    )
    # analyze_window only knows used vs pace. Once the provider as a whole is
    # untrustworthy, every window must stop quoting that arithmetic as a
    # dispatch word — especially `safe`, which the pace gate greps first.
    # A merely `due` window is unknown for itself and does not take this path.
    if not reliable:
        for window in windows:
            window["pacing_assessment"] = "unknown"
        assessments = [window["pacing_assessment"] for window in windows]
    if "critical" in assessments:
        status = "critical"
    elif not reliable or invalid or "unknown" in assessments:
        status = "unknown"
    elif "ahead_of_pace" in assessments:
        status = "ahead_of_pace"
    else:
        status = "safe"
    return {
        "provider": name,
        "plan": safe_plan(raw.get("plan")),
        "source": safe_identifier(raw.get("source")),
        "measured_at": measured_at,
        "health": health,
        "coverage": {"present_windows": sorted(labels), "missing_windows": missing},
        "status": status,
        "reliable": reliable,
        "windows": windows,
    }, warnings


def monthly_usage(raw: dict[str, Any], sources: dict[str, Any], now: datetime) -> dict[str, Any]:
    grok_raw = raw.get("grok") if isinstance(raw.get("grok"), dict) else {}
    models_raw = raw.get("models") if isinstance(raw.get("models"), list) else []
    models = []
    for item in models_raw[:64]:
        if not isinstance(item, dict):
            continue
        model_id = safe_identifier(item.get("id"))
        if not model_id:
            continue
        models.append({
            "id": model_id,
            "label": safe_plan(item.get("label")),
            "model": safe_identifier(item.get("model")),
            "tokens_month": number_or_none(item.get("tokens_month"), minimum=0),
            "cost_month": number_or_none(item.get("cost_month"), minimum=0),
            "message_count": number_or_none(item.get("message_count"), minimum=0),
            "measured_at": item.get("measured_at") if parse_time(item.get("measured_at")) else None,
        })
    return {
        "period": raw.get("monthly_period") if isinstance(raw.get("monthly_period"), str) else None,
        "is_quota": False,
        "grok": {
            "tokens_month": number_or_none(grok_raw.get("tokens_month"), minimum=0),
            "cost_month": number_or_none(grok_raw.get("cost_month"), minimum=0),
        },
        "models": models,
        "health": {
            "grok_monthly": source_health(sources, ("grok_monthly",), now),
            "models_monthly": source_health(sources, ("models_monthly",), now),
        },
    }


def base_output(now: datetime) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "checked_at": iso_z(now),
        "checked_at_utc": iso_z(now),
        "checked_at_ict": iso_ict(now),
        "display_timezone": "Asia/Bangkok",
        "quota_unit": "provider_reported_percent",
        "absolute_token_limit": None,
        "absolute_token_limit_available": False,
        "overall": {
            "data_health": "unavailable",
            "quota_pressure": "unknown",
            "action": "unknown",
            "reason_codes": [],
        },
        "providers": [],
        "monthly_usage": {"is_quota": False},
        "warnings": [],
    }


def analyze_snapshot(
    raw: Any,
    selected: str,
    now: datetime,
    *,
    degraded_reason: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Compute one view of a raw `/api/usage/limits`-shaped snapshot at `now`.

    `degraded_reason` marks a computation the caller cannot vouch for (the
    service was down or frozen, so this raw may be older than it looks). Every
    provider is then unreliable: last-good numbers stay visible as history but
    no window can say `safe` or `critical`, and the exit code is 1 when some
    number is still readable, 3 when none is.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), list):
        raise TokencheckError("invalid_envelope")
    sources = raw.get("sources") if isinstance(raw.get("sources"), dict) else {}
    requested = list(PROVIDERS) if selected == "all" else [selected]
    enabled = raw.get('enabled_providers')
    if selected == 'all' and isinstance(enabled, list) and all(n in PROVIDERS for n in enabled):
        requested = [n for n in requested if n in enabled]

    provider_map: dict[str, dict[str, Any] | None] = {}
    duplicates: set[str] = set()
    for item in raw["providers"]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("provider", "")).lower()
        if name not in PROVIDERS:
            continue
        if name in provider_map:
            duplicates.add(name)
        else:
            provider_map[name] = item

    # The top-level stale bit predates per-provider health. Each provider-level
    # boolean overrides it; providers that omit the field inherit this legacy
    # fallback independently so mixed old/new payloads still fail closed.
    snapshot_stale_raw = raw.get("stale")
    snapshot_stale_valid = isinstance(snapshot_stale_raw, bool)
    snapshot_stale = snapshot_stale_raw if snapshot_stale_valid else True

    results = []
    warnings: list[str] = []
    for name in requested:
        provider_raw = None if name in duplicates else provider_map.get(name)
        result, provider_warnings = provider_result(
            name,
            provider_raw,
            sources,
            snapshot_stale,
            snapshot_stale_valid,
            now,
            degraded_reason,
        )
        if name in duplicates:
            provider_warnings.append(f"provider_duplicate:{name}")
        results.append(result)
        warnings.extend(provider_warnings)

    usable = [provider for provider in results if any(window["used_percent"] is not None for window in provider["windows"])]
    reliable = [provider for provider in results if provider["reliable"]]
    critical = any(provider["status"] == "critical" for provider in results)
    ahead = any(provider["status"] == "ahead_of_pace" for provider in results)
    unknown = any(provider["status"] == "unknown" for provider in results)

    if len(reliable) == len(results):
        data_health = "healthy"
    elif usable:
        data_health = "degraded"
    else:
        data_health = "unavailable"
    if critical:
        quota_pressure = "critical"
        action = "avoid"
        exit_code = 2
    elif not usable:
        quota_pressure = "unknown"
        action = "unknown"
        exit_code = 3
    elif unknown:
        quota_pressure = "mixed"
        action = "caution"
        exit_code = 1
    elif ahead:
        quota_pressure = "ahead_of_pace"
        action = "caution"
        exit_code = 1
    elif data_health != "healthy":
        quota_pressure = "safe"
        action = "caution"
        exit_code = 1
    else:
        quota_pressure = "safe"
        action = "proceed"
        exit_code = 0

    reason_codes = []
    for provider in results:
        for window in provider["windows"]:
            assessment = window["pacing_assessment"]
            if assessment in {"critical", "ahead_of_pace", "unknown"}:
                reason_codes.append(f"{assessment}:{provider['provider']}:{window['label']}")
        if not provider["reliable"]:
            reason_codes.append(f"unreliable:{provider['provider']}")
    if degraded_reason:
        reason_codes.append(degraded_reason)

    output = base_output(now)
    output["overall"] = {
        "data_health": data_health,
        "quota_pressure": quota_pressure,
        "action": action,
        "reason_codes": sorted(set(reason_codes)),
    }
    output["providers"] = results
    output["monthly_usage"] = monthly_usage(raw, sources, now)
    output["warnings"] = sorted(set(warnings))
    return output, exit_code


def analyze_views(raw: Any, now: datetime) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Every view in VIEWS from one raw snapshot and one clock reading.

    This is the daemon's tick: one `now` for all views so `all` and the
    per-provider views can never disagree about time.
    """
    results: dict[str, dict[str, Any]] = {}
    exit_codes: dict[str, int] = {}
    for view in VIEWS:
        results[view], exit_codes[view] = analyze_snapshot(raw, view, now)
    return results, exit_codes


def parse_agy_usage(raw: Any, now: datetime) -> tuple[dict[str, Any] | None, str, str | None]:
    if not isinstance(raw, dict):
        return None, "schema_changed", "invalid_envelope"
    command = raw.get("command")
    if raw.get("status") != "SUCCESS" or not isinstance(command, dict) or command.get("name") != "usage":
        return None, "unavailable", "usage_command_failed"
    data = command.get("data")
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return None, "schema_changed", "groups_missing"
    buckets: dict[str, dict[str, Any]] = {}
    expected_ids = {item[0] for item in AGY_BUCKETS}
    for group in groups:
        values = group.get("buckets") if isinstance(group, dict) else None
        if not isinstance(values, list):
            return None, "schema_changed", "groups_invalid"
        for bucket in values:
            if not isinstance(bucket, dict):
                return None, "schema_changed", "bucket_invalid"
            bucket_id = bucket.get("id")
            if bucket_id not in expected_ids or bucket_id in buckets:
                return None, "schema_changed", "bucket_coverage_changed"
            buckets[bucket_id] = bucket
    if set(buckets) != expected_ids:
        return None, "schema_changed", "bucket_coverage_changed"
    windows = []
    for bucket_id, expected_window, label, hours in AGY_BUCKETS:
        bucket = buckets[bucket_id]
        remaining = bucket.get("remaining_fraction")
        if bucket.get("window") != expected_window or not is_number(remaining) or not 0 <= float(remaining) <= 1:
            return None, "schema_changed", "bucket_invalid"

        disabled = bucket.get("disabled")
        if disabled is True:
            # agy 1.2.6 omits reset_time when a 5h bucket is disabled because the
            # sibling weekly bucket is exhausted. remaining_fraction is then a
            # placeholder (observed: 1) and must not be converted to used_percent.
            reset_raw = bucket.get("reset_time")
            reset = parse_time(reset_raw)
            horizon_hours = hours
            if reset is None:
                sibling_id = _agy_sibling_bucket_id(bucket_id)
                sibling_spec = next((item for item in AGY_BUCKETS if item[0] == sibling_id), None)
                sibling_bucket = buckets.get(sibling_id) if sibling_id else None
                sibling_reset_raw = sibling_bucket.get("reset_time") if sibling_bucket else None
                sibling_reset = parse_time(sibling_reset_raw)
                if sibling_spec is None or sibling_reset is None:
                    return None, "schema_changed", "bucket_invalid"
                reset = sibling_reset
                reset_raw = sibling_reset_raw
                horizon_hours = sibling_spec[3]
            state, _seconds = reset_state(reset, horizon_hours, now)
            if state not in {"future", "due"}:
                return None, "schema_changed", "reset_horizon_invalid"
            windows.append({"label": label, "used_percent": 100,
                            "resets_at": reset_raw, "window_hours": hours})
            continue

        if "disabled" in bucket and disabled is not False:
            return None, "schema_changed", "bucket_invalid"

        reset_raw = bucket.get("reset_time")
        reset = parse_time(reset_raw)
        if reset is None:
            return None, "schema_changed", "bucket_invalid"
        state, _seconds = reset_state(reset, hours, now)
        if state not in {"future", "due"}:
            return None, "schema_changed", "reset_horizon_invalid"
        used = round((1 - float(remaining)) * 100, 2)
        windows.append({"label": label, "used_percent": 0 if used == 0 else used,
                        "resets_at": reset_raw, "window_hours": hours})
    return ({"provider": "agy", "plan": None, "source": "agy_usage", "measured_at": iso_z(now),
             "stale": False, "windows": windows}, "ok", None)


def parse_json(payload: bytes) -> Any:
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TokencheckError("invalid_json") from None


def bar_cells(percent: float | None, width: int) -> int | None:
    """Cells that represent `percent` of a `width`-cell bar, half-up rounded.

    `math.floor(x + 0.5)` instead of `round()` because Python rounds halves to
    even, which would make 50% of a 10-cell bar render as 4 cells one day and
    5 the next depending on the value. Half-up is boring and testable.
    """
    if percent is None:
        return None
    return max(0, min(width, math.floor(percent / 100.0 * width + 0.5)))


def lane_budget(delta: float | None, assessment: str) -> int | None:
    """Lanes this window can fund. None means "cannot be determined"."""
    if assessment == "critical":
        return 0
    if assessment == "unknown" or delta is None:
        return None
    for limit, lanes in LANE_THRESHOLDS:
        if delta <= limit:
            return lanes
    return LANE_ABOVE_PACE


def lane_text(lanes: int | None) -> str:
    if lanes is None:
        return "lanes unknown"
    if lanes == 0:
        return "0 lanes"
    return f"{lanes} lane" if lanes == 1 else f"{lanes} lanes"


def paint(text: str, color: str, enabled: bool) -> str:
    if not enabled or color not in ANSI:
        return text
    return f"{ANSI[color]}{text}{ANSI['reset']}"


def window_rows(output: dict[str, Any]) -> list[tuple[str, dict[str, Any] | None, bool]]:
    rows: list[tuple[str, dict[str, Any] | None, bool]] = []
    for provider in output.get("providers", []):
        name = str(provider.get("provider", "?"))
        reliable = bool(provider.get("reliable"))
        windows = provider.get("windows") or []
        if not windows:
            rows.append((name, None, reliable))
            continue
        for window in windows:
            rows.append((name, window, reliable))
    return rows


def tightest_window(provider: dict[str, Any]) -> dict[str, Any] | None:
    """The window that limits this provider: highest delta, unknowns first.

    A provider is only as free as its most-burned window, so WEEK sitting at
    -30pp does not license work when 5H is at +12pp. Windows whose delta could
    not be computed outrank every known delta because "unknown" must not be
    silently treated as slack.
    """
    windows = [window for window in provider.get("windows") or []]
    if not windows:
        return None
    unknown = [window for window in windows if window.get("delta_vs_pace_pp") is None]
    if unknown:
        return unknown[0]
    return max(windows, key=lambda window: float(window["delta_vs_pace_pp"]))


def reliability_note(provider: dict[str, Any]) -> str:
    """Label historical figures without claiming why this provider is unreliable."""
    if provider.get("reliable"):
        return ""
    health = provider.get("health")
    age = health.get("observed_age_seconds") if isinstance(health, dict) else None
    age_text = f"{age:g}s" if is_number(age) and age >= 0 else "unknown"
    return f"STALE/UNRELIABLE age {age_text}"


def render_window_line(
    label: str,
    label_width: int,
    window: dict[str, Any] | None,
    width: int,
    color: bool,
    reliable: bool = True,
    freshness: str = "",
) -> list[str]:
    padded = label.ljust(label_width)
    suffix = f" · {freshness}" if freshness else ""
    if window is None:
        return [f"{padded}  (no usable window){suffix}"]

    used = window.get("used_percent")
    pace = window.get("pace_percent")
    delta = window.get("delta_vs_pace_pp")
    assessment = str(window.get("pacing_assessment", "unknown")) if reliable else "unknown"
    filled = bar_cells(used, width)
    cells = list("?" * width if filled is None else CHART_FILL * filled + CHART_EMPTY * (width - filled))
    # Replace a cell, including a used cell, so the marker stays visible and
    # the requested width never changes. Paint runs around it, never over it.
    if pace is not None:
        cells[min(width - 1, bar_cells(pace, width) or 0)] = CHART_CARET
    bar = "".join(cells)
    if filled is not None and reliable:
        fill_color = str(window.get("ui_color", "none"))
        segments = bar.split(CHART_CARET)
        for index, segment in enumerate(segments):
            count = segment.count(CHART_FILL)
            segments[index] = paint(CHART_FILL * count, fill_color, color) + segment[count:]
        bar = CHART_CARET.join(segments)
    used_text = "  ? used" if filled is None else f"{float(used):5.1f}% used"
    delta_text = "delta unknown" if delta is None else f"{float(delta):+.2f}pp"
    pace_text = "pace ?" if pace is None else f"pace {float(pace):.1f}%"
    lanes = lane_budget(delta, assessment) if reliable else None
    note = f"{pace_text} · {delta_text} · {assessment} · {lane_text(lanes)}"
    if not reliable:
        note += " (data not trustworthy)"
    return [f"{padded}  [{bar}] {used_text} · {note}{suffix}"]


def render_chart(output: dict[str, Any], width: int = CHART_DEFAULT_WIDTH, color: bool = False) -> str:
    overall = output.get("overall", {}) if isinstance(output.get("overall"), dict) else {}
    lines = [
        "tokencheck chart · "
        f"{output.get('checked_at_ict', '?')} ICT · "
        f"health={overall.get('data_health')} · "
        f"pressure={overall.get('quota_pressure')} · "
        f"action={overall.get('action')}",
        "",
    ]
    reasons = overall.get("reason_codes")
    if isinstance(reasons, list) and reasons:
        lines.insert(1, "reasons · " + "  ".join(str(reason) for reason in reasons[:12]))

    rows = window_rows(output)
    if not rows:
        lines.append("(no providers in snapshot)")
    label_width = max((len(f"{name} {(window or {}).get('label', '—')}") for name, window, _ in rows), default=1)
    freshness = {str(provider.get("provider", "?")): reliability_note(provider)
                 for provider in output.get("providers", [])}
    for name, window, reliable in rows:
        lines.extend(
            render_window_line(
                f"{name} {(window or {}).get('label', '—')}",
                label_width,
                window,
                width,
                color,
                reliable,
                freshness.get(name, ""),
            )
        )

    verdicts = []
    for provider in output.get("providers", []):
        window = tightest_window(provider)
        name = str(provider.get("provider", "?"))
        # A provider whose data is not trustworthy funds nothing, however
        # comfortable its delta looks. `reliable` already folds in staleness,
        # missing windows and source health, so it is the gate here.
        reliable = bool(provider.get("reliable"))
        status = str(provider.get("status", "unknown")) if reliable else "unknown"
        if window is None:
            verdicts.append((name, "—", status, None, None, reliable))
            continue
        delta = window.get("delta_vs_pace_pp")
        assessment = str(window.get("pacing_assessment", "unknown"))
        lanes = lane_budget(delta, assessment) if reliable else None
        verdicts.append((name, str(window.get("label", "?")), status, delta, lanes, reliable))
    if verdicts:
        lines.append("")
        lines.append("verdict · tightest window per provider")
        name_width = max(len(item[0]) for item in verdicts)
        window_width = max(len(item[1]) for item in verdicts)
        state_width = max(len(item[2]) for item in verdicts)
        for name, label, status, delta, lanes, reliable in verdicts:
            delta_text = "  delta unknown" if delta is None else f"{float(delta):+9.2f}pp"
            suffix = "" if reliable else f"  (data not trustworthy) · {freshness[name]}"
            lines.append(
                f"  {name.ljust(name_width)}  {label.ljust(window_width)}  "
                f"{status.ljust(state_width)}  {delta_text}  {lane_text(lanes)}{suffix}"
            )

    lines.append("")
    lines.append(
        f"legend · {CHART_FILL}=used  {CHART_EMPTY}=remaining  "
        f"{CHART_CARET}=pace position  1 cell = {100.0 / width:.2f}%"
    )
    lines.append("lanes · delta <=-10pp:10  <=-3pp:5  <=+3pp:3  >+3pp:1 (ahead_of_pace)  critical:0")
    return "\n".join(lines)
