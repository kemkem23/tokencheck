"""Small shared transport and window utilities."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

HOME = Path.home()
ICT = timezone(timedelta(hours=7))


LIB_DIR = Path(__file__).resolve().parent.parent

_log_fn: Callable[[str], None] = lambda m: print(f"pacer-collector: {m}", flush=True)


def set_logger(fn: Callable[[str], None]) -> None:
    global _log_fn
    _log_fn = fn


def log(msg: str) -> None:
    _log_fn(msg)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, timezone.utc)
        except (OverflowError, ValueError, OSError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def which(name: str, extra: list[str] = ()) -> str | None:
    for cand in extra:
        p = Path(os.path.expanduser(cand))
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return shutil.which(name)


def run(cmd: list[str], timeout: float, cwd: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as e:
        return 127, "", f"oserror:{e.errno}"


def first_json(text: str) -> Any:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[start:])[0]
    except json.JSONDecodeError:
        return None


def window(label: str, used: float, resets_at: datetime | str | None, hours: int, **extra: Any) -> dict:
    r = resets_at if isinstance(resets_at, str) else (iso(resets_at) if resets_at else None)
    w = {"label": label, "used_percent": round(float(used), 2), "resets_at": r, "window_hours": hours}
    w.update(extra)
    return w


class Attempt(Exception):
    """Safe transport failure reason."""
