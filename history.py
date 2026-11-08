#!/usr/bin/env python3
"""Rolling series of tokencheck used% / pace% samples for PNG plots.

Append-only JSONL under <state>/history.jsonl. Compact per-tick points so a
week of 60 s samples stays small (~10k lines). 1 sample/min, always retains 7d. Stdlib only.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

HISTORY_FILE = "history.jsonl"
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 3600
MIN_KEEP_AGE_SECONDS = 7 * 24 * 3600  # never drop samples younger than 7d
MIN_INTERVAL_SECONDS = 55  # at most ~1 sample/minute on disk
DEFAULT_MAX_LINES = 11_000  # 7d x 1440/day = 10080 + margin
SCHEMA = 1


def state_dir(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    from config import load_config
    return load_config().state_dir


def history_path(state: str | Path | None = None) -> Path:
    return state_dir(state) / HISTORY_FILE


def sample_from_envelope(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Compact one history point from a /v1/snapshot envelope."""
    if not isinstance(envelope, dict):
        return None
    results = envelope.get("results")
    if not isinstance(results, dict):
        return None
    view = results.get("all")
    if not isinstance(view, dict):
        return None
    overall = view.get("overall") if isinstance(view.get("overall"), dict) else {}
    providers_out: list[dict[str, Any]] = []
    for provider in view.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        name = provider.get("provider")
        if not isinstance(name, str):
            continue
        windows_out: list[dict[str, Any]] = []
        for window in provider.get("windows") or []:
            if not isinstance(window, dict):
                continue
            label = window.get("label")
            if not isinstance(label, str):
                continue
            windows_out.append({
                "l": label,
                "u": window.get("used_percent"),
                "p": window.get("pace_percent"),
                "started": window.get("started", True),
                "d": window.get("delta_vs_pace_pp"),
                "a": window.get("pacing_assessment"),
                "r": window.get("resets_at"),
                "h": window.get("window_hours"),
            })
        providers_out.append({
            "n": name,
            "ok": bool(provider.get("reliable")),
            "src": provider.get("source"),
            "plan": provider.get("plan"),
            "st": provider.get("status"),
            "w": windows_out,
        })
    return {
        "v": SCHEMA,
        "t": envelope.get("computed_at") or view.get("checked_at"),
        "seq": envelope.get("sequence"),
        "exit": (envelope.get("exit_codes") or {}).get("all"),
        "action": overall.get("action"),
        "health": overall.get("data_health"),
        "pressure": overall.get("quota_pressure"),
        "reasons": overall.get("reason_codes") or [],
        "providers": providers_out,
    }


def sample_from_view(view: dict[str, Any], *, computed_at: str | None = None,
                     sequence: Any = None, exit_code: Any = None) -> dict[str, Any] | None:
    """Same compact shape from a /v1/tokencheck result (or results.all)."""
    if not isinstance(view, dict):
        return None
    # /v1/tokencheck wraps as {result, exit_code, ...}
    if isinstance(view.get("result"), dict) and "providers" not in view:
        exit_code = view.get("exit_code", exit_code)
        computed_at = view.get("computed_at") or computed_at
        sequence = view.get("sequence", sequence)
        view = view["result"]
    envelope = {
        "computed_at": computed_at or view.get("checked_at"),
        "sequence": sequence,
        "exit_codes": {"all": exit_code},
        "results": {"all": view},
    }
    return sample_from_envelope(envelope)


def append_sample(sample: dict[str, Any], state: str | Path | None = None,
                  *, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
                  max_lines: int = DEFAULT_MAX_LINES) -> Path | None:
    if not sample or not sample.get("t"):
        return None
    path = history_path(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(sample, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        return None
    # Opportunistic prune every ~64 appends (size heuristic).
    try:
        if path.stat().st_size > 0 and (path.stat().st_size // 256) % 64 == 0:
            prune(path, max_age_seconds=max_age_seconds, max_lines=max_lines)
    except OSError:
        pass
    return path


def append_envelope(envelope: dict[str, Any], state: str | Path | None = None) -> Path | None:
    sample = sample_from_envelope(envelope)
    if sample is None:
        return None
    # Dedup: skip if last line has same t+seq.
    path = history_path(state)
    if path.exists():
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 4096))
                tail = handle.read().decode("utf-8", errors="ignore")
            last = tail.strip().splitlines()[-1] if tail.strip() else ""
            if last:
                prev = json.loads(last)
                if prev.get("t") == sample.get("t") and prev.get("seq") == sample.get("seq"):
                    return path
                from datetime import datetime
                pt = datetime.fromisoformat(str(prev.get("t")).replace("Z", "+00:00")).timestamp()
                nt = datetime.fromisoformat(str(sample.get("t")).replace("Z", "+00:00")).timestamp()
                if 0 <= nt - pt < MIN_INTERVAL_SECONDS:
                    return path
        except Exception:
            pass
    return append_sample(sample, state)


def prune(path: Path, *, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
          max_lines: int = DEFAULT_MAX_LINES,
          min_keep_age_seconds: int = MIN_KEEP_AGE_SECONDS) -> None:
    """Drop old lines, but never drop samples inside the last min_keep window (7d)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return
    now = time.time()
    cutoff = now - max_age_seconds
    floor = now - min_keep_age_seconds
    kept: list[str] = []
    for ln in lines:
        try:
            obj = json.loads(ln)
            t = obj.get("t")
            if isinstance(t, str):
                from datetime import datetime
                ts = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
                # Always keep recent floor window; else apply max_age.
                age_ok = ts >= floor or ts >= cutoff
            else:
                age_ok = True
        except Exception:
            age_ok = True
        if age_ok:
            kept.append(ln)
    if len(kept) > max_lines:
        # Prefer newest, but still never drop inside floor when possible.
        recent: list[str] = []
        older: list[str] = []
        for ln in kept:
            try:
                obj = json.loads(ln)
                t = obj.get("t")
                if isinstance(t, str):
                    from datetime import datetime
                    ts = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
                    (recent if ts >= floor else older).append(ln)
                else:
                    recent.append(ln)
            except Exception:
                recent.append(ln)
        # Keep all recent; fill remaining budget with newest older lines.
        budget = max_lines - len(recent)
        if budget < 0:
            # Extreme: keep the newest max_lines of the floor window itself.
            kept = recent[-max_lines:]
        else:
            kept = (older[-budget:] if budget else []) + recent
    if len(kept) == len(lines) and kept == lines:
        return
    # Rewrite if changed (count or content)
    if "\n".join(kept) == "\n".join(lines):
        return
    tmp = path.with_suffix(".jsonl.tmp")
    try:
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass



def load_samples(state: str | Path | None = None, *,
                 max_age_seconds: int | None = None) -> list[dict[str, Any]]:
    path = history_path(state)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    cutoff = None
    if max_age_seconds is not None:
        cutoff = time.time() - max_age_seconds
    try:
        with open(path, encoding="utf-8") as handle:
            for ln in handle:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if cutoff is not None and isinstance(obj.get("t"), str):
                    try:
                        from datetime import datetime
                        if datetime.fromisoformat(obj["t"].replace("Z", "+00:00")).timestamp() < cutoff:
                            continue
                    except Exception:
                        pass
                for provider in obj.get("providers") or []:
                    if provider.get("n") == "claude":
                        for window in provider.get("w") or []:
                            if window.get("l") == "WEEK":
                                window["l"] = "ALL WEEK"
                out.append(obj)
    except OSError:
        return []
    return out


def series_for(samples: list[dict[str, Any]], provider: str, label: str
               ) -> list[tuple[str, float | None, float | None, float | None]]:
    """Return [(t, used, pace, delta), ...] for one provider/window label."""
    rows: list[tuple[str, float | None, float | None, float | None]] = []
    want_label = label.strip().upper()
    for sample in samples:
        t = sample.get("t")
        if not isinstance(t, str):
            continue
        for provider_row in sample.get("providers") or []:
            if provider_row.get("n") != provider:
                continue
            for window in provider_row.get("w") or []:
                if str(window.get("l", "")).strip().upper() != want_label:
                    continue
                rows.append((t, window.get("u"), window.get("p"), window.get("d")))
    return rows
