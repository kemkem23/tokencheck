#!/usr/bin/env python3
"""Read the newest Codex CLI rate-limit event without loading whole sessions.

The provider is allowed to return one window (``secondary: null``), and the
ordering of ``primary``/``secondary`` is not a stable description of the window.
``window_minutes`` is therefore the source of truth: 300 minutes is 5H and
10080 minutes is WEEK.  The legacy top-level fields are retained for callers
such as tokguard.sh while ``windows`` carries the lossless representation.
Coverage is explicit: ``ok`` requires both expected windows, ``partial`` means
at least one expected window is present, and ``schema_changed`` means neither
expected window could be recognized.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


MAX_FILES = 64
MAX_TAIL_BYTES = 16 * 1024 * 1024
WINDOW_LABELS = {
    300: ("5H", 5),
    10080: ("WEEK", 168),
}
EXPECTED_WINDOW_LABELS = tuple(
    label for _, (label, _) in sorted(WINDOW_LABELS.items())
)


def recent_sessions(root: Path):
    files = []
    for path in root.rglob("*.jsonl"):
        try:
            files.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    files.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in files[:MAX_FILES]]


def tail_lines(path: Path):
    """Return complete lines from a bounded tail of *path*."""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - MAX_TAIL_BYTES)
        handle.seek(start)
        if start:
            handle.readline()  # discard the partial first line
        return handle.readlines()


def newest_rate_limits(path: Path):
    try:
        lines = tail_lines(path)
    except OSError:
        return None
    for raw in reversed(lines):
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if obj.get("type") != "event_msg":
            continue
        payload = obj.get("payload") or {}
        limits = payload.get("rate_limits")
        if payload.get("type") == "token_count" and isinstance(limits, dict):
            return {"rate_limits": limits, "timestamp": obj.get("timestamp")}
    return None


def find_latest_rate_limits(root: Path):
    best = None
    for path in recent_sessions(root):
        found = newest_rate_limits(path)
        if not found:
            continue
        timestamp = found.get("timestamp")
        try:
            sort_key = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            try:
                sort_key = path.stat().st_mtime
            except OSError:
                sort_key = 0
        if best is None or sort_key > best[0]:
            best = (sort_key, path, found)
    return best


def reset_iso(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
                timezone.utc
            ).isoformat().replace("+00:00", "Z")
        except ValueError:
            return None
    return None


def normalize_window(value):
    if not isinstance(value, dict):
        return None
    minutes = value.get("window_minutes")
    if not isinstance(minutes, (int, float)) or not float(minutes).is_integer():
        return None
    spec = WINDOW_LABELS.get(int(minutes))
    if not spec:
        return None
    used = value.get("used_percent")
    if not isinstance(used, (int, float)) or not 0 <= float(used) <= 100:
        used = None
    label, hours = spec
    return {
        "label": label,
        "window_minutes": int(minutes),
        "window_hours": hours,
        "used_percent": used,
        "resets_at": reset_iso(value.get("resets_at")),
    }


def human_reset(value):
    if not value:
        return "?"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return "?"
    return dt.strftime("%H:%M")


def build_output(found):
    _, _, result = found
    limits = result["rate_limits"]
    windows = []
    unknown_windows = []
    for key in ("primary", "secondary"):
        raw = limits.get(key)
        if raw is None:
            continue
        normalized = normalize_window(raw)
        if normalized:
            if not any(item["label"] == normalized["label"] for item in windows):
                windows.append(normalized)
        elif isinstance(raw, dict):
            unknown_windows.append({
                "position": key,
                "window_minutes": raw.get("window_minutes"),
            })
    windows.sort(key=lambda item: item["window_hours"])
    by_label = {item["label"]: item for item in windows}
    missing_windows = [
        label for label in EXPECTED_WINDOW_LABELS if label not in by_label
    ]
    if not missing_windows:
        schema_status = "ok"
    elif windows:
        schema_status = "partial"
    else:
        schema_status = "schema_changed"
    five = by_label.get("5H", {})
    week = by_label.get("WEEK", {})
    return {
        "5h_used": five.get("used_percent"),
        "5h_reset": human_reset(five.get("resets_at")),
        "weekly_used": week.get("used_percent"),
        "weekly_reset": human_reset(week.get("resets_at")),
        "windows": windows,
        "unknown_windows": unknown_windows,
        "missing_windows": missing_windows,
        "schema_status": schema_status,
        "measured_at": result.get("timestamp"),
        "plan": limits.get("plan_type"),
    }


def main():
    parser = argparse.ArgumentParser(description="Codex usage: duration-aware 5h + weekly")
    parser.add_argument("--file", help="Specific session .jsonl file")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    if args.file:
        path = Path(args.file).expanduser()
        item = newest_rate_limits(path) if path.exists() else None
        found = (path.stat().st_mtime, path, item) if item else None
    else:
        found = find_latest_rate_limits(Path.home() / ".codex" / "sessions")

    if not found:
        print("No rate_limits found")
        return 1

    output = build_output(found)
    if args.json:
        print(json.dumps(output, separators=(",", ":")))
    else:
        print(f"5h:     {output['5h_used'] if output['5h_used'] is not None else '?'}%  (reset {output['5h_reset']})")
        print(f"weekly: {output['weekly_used'] if output['weekly_used'] is not None else '?'}%  (reset {output['weekly_reset']})")
        if output["missing_windows"]:
            print(f"warning: missing expected Codex windows: {', '.join(output['missing_windows'])}")
        if output["unknown_windows"]:
            print("warning: unrecognized Codex window schema")
    return 0 if output["windows"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
