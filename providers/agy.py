"""Local AGY usage panel and bucket normalization."""
from .common import Attempt, first_json, parse_time, window
from typing import Any
from datetime import datetime
import json
import re
from . import common as util

AGY_KNOWN = {"gemini-5h", "3p-5h", "gemini-weekly", "3p-weekly"}


def parse_agy(raw: Any, now: datetime, source: str, core: Any) -> dict:
    """Tolerant wrapper over core.parse_agy_usage: extra buckets (e.g. Argon) are
    reported in extras instead of failing the whole provider."""
    extras = []
    if isinstance(raw, dict):
        raw = json.loads(json.dumps(raw))
        for g in ((raw.get("command") or {}).get("data") or {}).get("groups") or []:
            if isinstance(g, dict) and isinstance(g.get("buckets"), list):
                keep = []
                for b in g["buckets"]:
                    if isinstance(b, dict) and b.get("id") not in AGY_KNOWN:
                        extras.append({"id": b.get("id"), "name": b.get("name"),
                                       "remaining_fraction": b.get("remaining_fraction"),
                                       "reset_time": b.get("reset_time")})
                    else:
                        keep.append(b)
                g["buckets"] = keep
    prov, state, err = core.parse_agy_usage(raw, now)
    if prov is None:
        raise Attempt(err or state)
    prov["source"] = source
    if extras:
        prov["extras"] = {"extra_buckets": extras}
    labels = {w['label'] for w in prov['windows']}
    for bucket in extras:
        identity = str(bucket.get('id') or '') + ' ' + str(bucket.get('name') or '')
        text = identity.lower()
        hours = 168 if re.search(r'week(?:ly)?|7[- ]?d', text) else (5 if re.search(r'5[- ]?h|5[- ]?hour', text) else None)
        remaining = bucket.get('remaining_fraction')
        if hours is None or not isinstance(remaining, (int, float)) or isinstance(remaining, bool) or not 0 <= remaining <= 1 or not parse_time(bucket.get('reset_time')):
            continue
        base = re.sub(r'[^A-Z0-9]+', ' ', str(bucket.get('id') or bucket.get('name') or '').upper()).strip()
        base = re.sub(r'\b(GEMINI|WTU|WEEKLY|WEEK|LIMIT|5H|7D)\b', '', base)
        base = ' '.join(base.split()) or 'EXTRA'
        label = base + (' WEEK' if hours == 168 else ' 5H')
        if label not in labels:
            prov['windows'].append(window(label, (1-remaining)*100, bucket['reset_time'], hours))
            labels.add(label)
    return prov


def agy_local(now: datetime, ctx: dict) -> dict:
    exe = util.which("agy", ["~/.local/bin/agy", "~/.npm-global/bin/agy"])
    if not exe:
        raise Attempt("agy_not_installed")
    code, out, _ = util.run([exe, "-p", "/usage", "--output-format", "json", "--print-timeout", "35s"], 45, cwd="/tmp")
    raw = first_json(out)
    if raw is None:
        raise Attempt(f"agy_rc_{code}")
    return parse_agy(raw, now, "agy_usage", ctx["core"])
