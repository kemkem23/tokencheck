#!/usr/bin/env python3
"""Read the tokencheck service's precomputed quota pacing, failing closed.

The helper is deliberately stdlib-only and read-only. Live mode performs one
bounded GET to the loopback tokencheck service, which recomputes every view
every 5 seconds from collector caches. When the service is down, frozen or
unreadable it recomputes locally from the service's last on-disk snapshot and
marks every provider unreliable. It never launches a provider CLI, never calls
the orchestrator API (that API proxies back to this service) and never starts
the daemon. Test mode accepts a bounded JSON payload on stdin and a
deterministic clock.

All arithmetic lives in core.py, shared with the daemon.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)


def _load_core() -> Any:
    # A unique module name instead of `import core`: the skill directory is not
    # a package and a bare `core` on sys.path could be anybody's module.
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

# Re-export the engine so `tokencheck.<name>` keeps working for callers and
# tests written against the single-file v12 helper.
for _name in dir(core):
    if not _name.startswith("__"):
        globals().setdefault(_name, getattr(core, _name))

TokencheckError = core.TokencheckError
MAX_RESPONSE_BYTES = core.MAX_RESPONSE_BYTES
PROVIDERS = core.PROVIDERS
CHART_DEFAULT_WIDTH = core.CHART_DEFAULT_WIDTH
CHART_MIN_WIDTH = core.CHART_MIN_WIDTH
CHART_MAX_WIDTH = core.CHART_MAX_WIDTH

SERVICE_HOST = "127.0.0.1"
SERVICE_PATH = "/v1/snapshot"
SERVICE_SCHEMA_VERSION = 1
HTTP_TIMEOUT_SECONDS = 1
# The daemon ticks every 5 seconds; three missed ticks means it is frozen even
# if its HTTP thread still answers with the last generation.
SERVICE_MAX_AGE_SECONDS = 15
SERVICE_MAX_FUTURE_SECONDS = 5
# The on-disk snapshot carries five precomputed views plus raw input.
MAX_SNAPSHOT_FILE_BYTES = 8 * MAX_RESPONSE_BYTES


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise TokencheckError("invalid_arguments", 64)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


from config import load_config

def service_url() -> str:
    """Loopback only. The port may be moved for tests; the host never can."""
    return f"http://{SERVICE_HOST}:{load_config().port}{SERVICE_PATH}"


def state_dir() -> Path:
    return load_config().state_dir


def read_stdin() -> bytes:
    payload = sys.stdin.buffer.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise TokencheckError("response_too_large")
    return payload


def fetch_service() -> bytes:
    opener = build_opener(ProxyHandler({}), NoRedirect())
    request = Request(
        service_url(),
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "pacer-tokencheck/13"},
    )
    limit = MAX_SNAPSHOT_FILE_BYTES
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > limit:
                raise TokencheckError("service_response_too_large")
            payload = response.read(limit + 1)
    except TokencheckError:
        raise
    except HTTPError as error:
        raise TokencheckError(f"service_http_{error.code}") from None
    except (URLError, TimeoutError, OSError):
        raise TokencheckError("service_unavailable") from None
    if len(payload) > limit:
        raise TokencheckError("service_response_too_large")
    return payload


def read_snapshot_file() -> Any:
    path = state_dir() / "snapshot.json"
    try:
        with open(path, "rb") as handle:
            payload = handle.read(MAX_SNAPSHOT_FILE_BYTES + 1)
    except OSError:
        return None
    if len(payload) > MAX_SNAPSHOT_FILE_BYTES:
        return None
    try:
        return core.parse_json(payload)
    except TokencheckError:
        return None


def envelope_raw(envelope: Any) -> Any:
    if isinstance(envelope, dict) and isinstance(envelope.get("raw"), dict) \
            and isinstance(envelope["raw"].get("providers"), list):
        return envelope["raw"]
    return None


def precomputed_view(envelope: Any, selected: str) -> tuple[dict[str, Any], int] | None:
    """The service's own view for `selected`, or None if the envelope is unusable."""
    if not isinstance(envelope, dict):
        return None
    results = envelope.get("results")
    exit_codes = envelope.get("exit_codes")
    if not isinstance(results, dict) or not isinstance(exit_codes, dict):
        return None
    view = results.get(selected)
    code = exit_codes.get(selected)
    if not isinstance(view, dict) or view.get("schema_version") != 1 \
            or not isinstance(view.get("providers"), list) or not isinstance(view.get("overall"), dict):
        return None
    if isinstance(code, bool) or code not in {0, 1, 2, 3}:
        return None
    return view, code


def service_metadata(mode: str, envelope: Any, now: datetime, reason: str | None) -> dict[str, Any]:
    computed = core.parse_time(envelope.get("computed_at")) if isinstance(envelope, dict) else None
    engine = envelope.get("engine_version") if isinstance(envelope, dict) else None
    sequence = envelope.get("sequence") if isinstance(envelope, dict) else None
    return {
        "mode": mode,
        "url": service_url(),
        "computed_at": core.iso_z(computed) if computed else None,
        "age_seconds": round((now - computed).total_seconds()) if computed else None,
        "max_age_seconds": SERVICE_MAX_AGE_SECONDS,
        "engine_version": engine if core.is_number(engine) else None,
        "client_engine_version": core.ENGINE_VERSION,
        "sequence": sequence if core.is_number(sequence) else None,
        "reason_code": reason,
    }


def service_rejection(envelope: Any, now: datetime) -> str | None:
    """Why this envelope's precomputed view must not be used, if at all."""
    if not isinstance(envelope, dict) or envelope.get("schema_version") != SERVICE_SCHEMA_VERSION:
        return "service_schema_mismatch"
    computed = core.parse_time(envelope.get("computed_at"))
    if computed is None:
        return "service_schema_mismatch"
    age = (now - computed).total_seconds()
    if age < -SERVICE_MAX_FUTURE_SECONDS:
        return "service_clock_future"
    if age > SERVICE_MAX_AGE_SECONDS:
        return "service_stale"
    if envelope.get("engine_version") != core.ENGINE_VERSION:
        return "engine_version_mismatch"
    return None


def live_result(selected: str, now: datetime) -> tuple[dict[str, Any], int]:
    envelope: Any = None
    try:
        envelope = core.parse_json(fetch_service())
        reason = service_rejection(envelope, now)
    except TokencheckError as error:
        if error.exit_code == 64:
            raise
        if error.code == "invalid_json":
            reason = "service_schema_mismatch"
        else:
            reason = error.code if error.code.startswith("service_") else "service_unavailable"

    if reason is None:
        view = precomputed_view(envelope, selected)
        if view is not None:
            output, exit_code = view
            output = dict(output)
            output["service"] = service_metadata("service", envelope, now, None)
            return output, exit_code
        reason = "service_schema_mismatch"

    # A fresh service on another engine version only disagrees about formula,
    # not about data age: recompute its raw here and trust the result as much
    # as the raw itself deserves. This is the rollout window where the CLI was
    # overlaid before the daemon was replaced, or the other way round.
    if reason == "engine_version_mismatch" and envelope_raw(envelope) is not None:
        output, exit_code = core.analyze_snapshot(envelope_raw(envelope), selected, now)
        output["service"] = service_metadata("local_compute", envelope, now, reason)
        return output, exit_code

    # Everything else means nobody is vouching for the age of the raw input.
    # Use the freshest raw we can read without touching a provider, and fail
    # closed on it.
    source = envelope if envelope_raw(envelope) is not None else read_snapshot_file()
    raw = envelope_raw(source)
    if raw is None:
        raise TokencheckError(reason)
    output, exit_code = core.analyze_snapshot(raw, selected, now, degraded_reason=reason)
    output["service"] = service_metadata("local_fallback", source, now, reason)
    return output, exit_code


def parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = core.parse_time(value)
    if parsed is None:
        raise TokencheckError("invalid_now", 64)
    return parsed


def print_json(output: dict[str, Any]) -> None:
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = JsonArgumentParser(description="Check live LLM quota and pace")
    parser.add_argument("--self-check", action="store_true", help="validate config and check daemon readiness/provider status")
    parser.add_argument("--validate-config", action="store_true", help="validate and print configuration without collection")
    parser.add_argument("--provider", choices=core.VIEWS, default="all")
    parser.add_argument("--json", action="store_true",
                        help="print JSON (the default; explicit alias, cannot be combined with --chart)")
    parser.add_argument(
        "--chart",
        action="store_true",
        help="print a computed pace chart instead of JSON (paste it verbatim; never redraw it by hand)",
    )
    parser.add_argument("--chart-width", type=int, default=CHART_DEFAULT_WIDTH,
                        help=f"bar cells, {CHART_MIN_WIDTH}-{CHART_MAX_WIDTH} (default {CHART_DEFAULT_WIDTH})")
    parser.add_argument("--chart-color", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--stdin", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--now", help=argparse.SUPPRESS)
    emit = print_json
    try:
        args = parser.parse_args(argv)
        if args.validate_config:
            print_json(load_config().json())
            return 0
        if args.self_check:
            from selfcheck import check
            result, code = check()
            print_json(result)
            return code
        if args.json and args.chart:
            raise TokencheckError("json_conflicts_with_chart", 64)
        if args.now and not args.stdin:
            raise TokencheckError("now_requires_stdin", 64)
        if args.chart_width < CHART_MIN_WIDTH or args.chart_width > CHART_MAX_WIDTH:
            raise TokencheckError("invalid_chart_width", 64)
        if args.chart:
            color = args.chart_color == "always" or (
                args.chart_color == "auto"
                and sys.stdout.isatty()
                and not os.environ.get("NO_COLOR")
            )
            emit = lambda result: print(core.render_chart(result, args.chart_width, color))  # noqa: E731
        now = parse_now(args.now)
        if args.stdin:
            raw = core.parse_json(read_stdin())
            output, exit_code = core.analyze_snapshot(raw, args.provider, now)
        else:
            output, exit_code = live_result(args.provider, now)
        emit(output)
        return exit_code
    except TokencheckError as error:
        now = datetime.now(timezone.utc)
        output = core.base_output(now)
        output["overall"]["reason_codes"] = [error.code]
        output["warnings"] = [error.code]
        emit(output)
        return error.exit_code
    except Exception:
        now = datetime.now(timezone.utc)
        output = core.base_output(now)
        output["overall"]["reason_codes"] = ["internal_error"]
        output["warnings"] = ["internal_error"]
        emit(output)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
