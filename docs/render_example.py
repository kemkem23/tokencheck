"""Render the documentation chart from synthetic data, with no state/provider reads.

Run from the source root: ./venv/bin/python docs/render_example.py
The chart uses the project's original provider identifiers and bundled OFL font.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plot_png

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone(timedelta(hours=7)))


class ExampleClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz)


def samples():
    rows = []
    for minute in range(1441):
        t = NOW - timedelta(minutes=1440-minute)
        progress = minute / 1440
        providers = []
        for name, windows in [('grok', [('WEEK', 65, 40)]),
                              ('claude', [('ALL WEEK', 38, 52), ('FABLE WEEK', 9, 52)]),
                              ('codex', [('WEEK', 17, 22)]),
                              ('agy', [('ARGON WEEK', 0.8, 11)])]:
            windows = [{'l': label, 'u': used * (0.6 + 0.4 * progress),
                        'p': max(0, pace - (1-progress)*14.28), 'h': 168,
                        'r': (NOW+timedelta(hours=168*(1-pace/100))).isoformat()}
                       for label, used, pace in windows]
            if name == 'agy':
                windows += [{'l': label, 'u': 0, 'p': None, 'started': False}
                            for label in ('GEM 5H', '3P 5H', 'GEM WEEK', '3P WEEK')]
            providers.append({'n': name, 'w': windows})
        rows.append({'t': t.isoformat(), 'providers': providers})
    return rows


def render_cover(width, height, path):
    """Original geometric cover: grid, computed curves, and plain text only."""
    import math
    from resvg_py import svg_to_bytes
    from html import escape
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           f'<rect width="{width}" height="{height}" fill="#050914"/>']
    for i in range(18):
        x=width*(0.44+i*0.035)
        parts.append(f'<path d="M{x} 0V{height}" stroke="#193244" fill="none"/>')
    for i in range(11):
        y=height*i/10
        parts.append(f'<path d="M{width*0.44} {y}H{width}" stroke="#193244" fill="none"/>')
    for color,shift in [('#53e7ff',0),('#b497ff',0.14),('#9fe870',0.28)]:
        points=[]
        for i in range(121):
            t=i/120
            x=width*(0.44+0.56*t)
            rise=(1/(1+math.exp(-9*(t-0.55-shift))))
            y=height*(0.95-0.91*rise)
            points.append(f'{x:.2f},{y:.2f}')
        parts.append(f'<path d="M'+ ' L'.join(points)+f'" fill="none" stroke="{color}" stroke-width="4"/>')
    for text,x,y,size,color in [
        ('LOCAL SIGNAL. CLEAR PACE.',0.055,0.13,18,'#53e7ff'),
        ('tokencheck',0.055,0.38,86,'#effcff'),
        ('Token usage. At your pace.',0.055,0.54,30,'#effcff'),
        ('Grok · Claude · Codex · Fable · AGY',0.055,0.69,24,'#a8bfd2'),
        ('ZERO INFERENCE TOKENS  /  LOCAL  /  MCP + CHARTS',0.055,0.88,16,'#53e7ff')]:
        parts.append(f'<text x="{width*x}" y="{height*y}" font-family="Noto Sans Thai" font-size="{size}" fill="{color}">{escape(text)}</text>')
    svg=''.join(parts)+'</svg>'
    plot_png._validate_svg(svg)
    data=svg_to_bytes(svg_string=svg,font_files=[str(plot_png.FONT)],skip_system_fonts=True,
                      font_family='Noto Sans Thai',sans_serif_family='Noto Sans Thai')
    plot_png.atomic_write(path,data)


if __name__ == '__main__':
    assets=Path(__file__).parent/'assets'
    with patch.object(plot_png, 'datetime', ExampleClock):
        plot_png.render_png(samples(), assets/'usage-screenshot.png',
                            title='Tokencheck / Synthetic example')
    render_cover(1600,500,assets/'cover-hero.png')
    render_cover(1280,640,assets/'cover-social.png')
