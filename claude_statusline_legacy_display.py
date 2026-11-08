#!/usr/bin/env python3
"""Preserve the former local hook's visible output, without writing capture state."""
import json
import sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
rl = data.get('rate_limits') if isinstance(data, dict) else None
row = []
if isinstance(rl, dict) and rl:
    for k, lab in (('five_hour', '5h'), ('seven_day', 'wk')):
        w = rl.get(k) or {}
        if isinstance(w.get('used_percentage'), (int, float)):
            row.append(f"{lab} {w['used_percentage']:.0f}%")
model = ((data.get('model') or {}).get('display_name')) if isinstance(data, dict) else None
print(' · '.join([x for x in [model] + row if x]))
