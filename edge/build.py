#!/usr/bin/env python3
"""Build dist/worker.js: inline the hash-verified provider logos as an SVG sprite. No network."""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE.parent
sys.path.insert(0, str(LIB))
import plot_png  # noqa: E402

NS = '{http://www.w3.org/2000/svg}'


def symbol(name):
    raw = plot_png._logo_template(name).replace('_LOGO_INSTANCE_', f'lg-{name}-')
    root = ET.fromstring(raw.replace('_LOGO_X_', '0').replace('_LOGO_Y_', '0'))
    vb = [float(v) for v in root.get('viewBox').split()]
    inner = ''.join(ET.tostring(child, encoding='unicode') for child in root)
    inner = re.sub(r'\sxmlns(:\w+)?="[^"]*"', '', inner).replace('ns0:', '')
    keep = ' '.join(f'{k}="{root.get(k)}"' for k in ('fill', 'stroke', 'stroke-linecap', 'stroke-linejoin') if root.get(k))
    return (f'<symbol id="lg-{name}" viewBox="{root.get("viewBox")}"><g {keep}>{inner}</g></symbol>', vb[2] / vb[3])


def build(out=HERE / 'dist' / 'worker.js'):
    parts, ratios = [], {}
    for name in ('grok', 'claude', 'openai', 'antigravity'):
        sym, ratio = symbol(name)
        parts.append(sym)
        ratios[name] = round(ratio, 4)
    sprite = '<svg width="0" height="0" style="position:absolute" aria-hidden="true">' + ''.join(parts) + '</svg>'
    src = (HERE / 'worker.template.js').read_text()
    src = src.replace('__LOGOS__', json.dumps(sprite), 1).replace('__RATIOS__', json.dumps(ratios), 1)
    out.parent.mkdir(exist_ok=True)
    out.write_text(src)
    return out


if __name__ == '__main__':
    print(build())
