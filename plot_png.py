#!/usr/bin/env python3
"""Offline usage/recorded-pace PNGs: bundled-font SVG with lazy rasterizers."""
from __future__ import annotations

import json
import math
import os
import tempfile
import unicodedata
import warnings
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

from history import append_envelope, load_samples, state_dir

ICT = timezone(timedelta(hours=7), name="ICT")
PROVIDER_ORDER = ("claude", "codex", "grok", "agy")
BG, CARD, USAGE, PACE = "#050914", "#0d192c", "#53e7ff", "#b497ff"
GRID, TEXT, SUBTEXT = "#243d53", "#effcff", "#a8bfd2"
FONT = Path(__file__).resolve().parent / "fonts" / "NotoSansThai.ttf"
WIDTH, HEIGHT, PIXEL_RATIO = 393, 852, 3
GAP_SECONDS = 180  # Missing >3 normal 60s ticks is a visible break, never interpolation.


def parse_t(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(ICT) if dt.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def finite(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _series(samples, provider="all"):
    """Ordered real windows; missing windows/ticks and resets break each series."""
    ordered = sorted(((parse_t(s.get("t")), s) for s in samples if isinstance(s, dict)
                      and parse_t(s.get("t")) is not None), key=lambda pair: pair[0])
    # Last occurrence wins for a duplicate timestamp.
    ordered = list(dict(ordered).items())
    keys = dict.fromkeys((p.get("n"), w["l"]) for _, s in ordered
                        for p in s.get("providers", []) if isinstance(p, dict)
                        and p.get("n") in PROVIDER_ORDER and provider in ("all", p.get("n"))
                        for w in p.get("w", []) if isinstance(w, dict) and isinstance(w.get("l"), str))
    result = []
    for name, label in sorted(keys, key=lambda k: PROVIDER_ORDER.index(k[0])):
        points = []
        for t, sample in ordered:
            w = next((w for p in sample.get("providers", []) if isinstance(p, dict) and p.get("n") == name
                      for w in p.get("w", []) if isinstance(w, dict) and w.get("l") == label), None)
            points.append((t, finite(w.get("u")) if w else None,
                           finite(w.get("p")) if w and w.get("started") is not False else None, w))
        result.append((name, label, points))
    return result


def panels_from_samples(samples: list[dict[str, Any]], provider_filter: str = "all"):
    """Compatibility helper returning (provider, window, [(time, usage, pace)])."""
    return [(n, l, [(t, u, p) for t, u, p, _ in pts]) for n, l, pts in _series(samples, provider_filter)]


def latest_reset_hours(samples, provider, label):
    for n, l, pts in _series(samples, provider):
        if l.strip().upper() == label.strip().upper():
            w = next((row[3] for row in reversed(pts) if row[3] is not None), {})
            return w.get("r"), finite(w.get("h"))
    return None, None


def theoretical_pace_line(pts, resets_at, window_hours, now):
    """Legacy helper retained for callers; charts exclusively use recorded pace."""
    reset = parse_t(resets_at)
    h = finite(window_hours)
    if not pts or reset is None or h is None or h <= 0:
        return None
    start = reset - timedelta(hours=h)
    end = max(pts[-1][0], now)
    span = (end - pts[0][0]).total_seconds()
    if span <= 0:
        return None
    xs = [pts[0][0] + timedelta(seconds=span*i/64) for i in range(65)]
    return xs, [max(0, min(100, (t-start).total_seconds()/(h*3600)*100)) for t in xs]


@lru_cache(maxsize=1)
def _metrics():
    return json.loads(FONT.with_name("metrics.json").read_text(encoding="utf-8"))


def _clean(text):
    # XML 1.0-safe text, with controls/newlines normalized for wrapping.
    return ''.join(c if ord(c) >= 32 and not 0xD800 <= ord(c) <= 0xDFFF and ord(c) not in (0xFFFE, 0xFFFF)
                   else ' ' for c in str(text))


def _text_clusters(token):
    """Keep marks, Thai spacing vowels and preposed vowels with their base.

    This is a conservative character fallback, not Thai word segmentation.
    Whole whitespace-delimited tokens are preferred by the wrapper.
    """
    clusters = []
    leading_vowels = 'เแโใไ'
    spacing_vowels = 'ะาำๅ'
    for char in token:
        attached = (unicodedata.category(char).startswith('M')
                    or char in spacing_vowels
                    or (clusters and clusters[-1][-1] in leading_vowels))
        if clusters and attached:
            clusters[-1] += char
        else:
            clusters.append(char)
    return clusters


def _wrap(text, size, width=366):
    """Wrap at whitespace; split only oversized tokens at safe cluster edges."""
    metrics = _metrics()
    def measure(value):
        return sum(metrics.get(str(ord(c)), 1.0) for c in value) * size * 1.08

    lines, line = [], ''
    for token in _clean(text).split():
        candidate = line + ' ' + token if line else token
        if measure(candidate) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ''
        if measure(token) <= width:
            line = token
            continue
        for cluster in _text_clusters(token):
            if line and measure(line + cluster) > width:
                lines.append(line)
                line = ''
            line += cluster
    return lines + ([line] if line else []) or ['']


def _segments(points, field):
    """No bridges across absent/nonfinite samples, resets, decreases or outages."""
    runs, run, previous = [], [], None
    for row in points:
        t, u, p, w = row
        value = row[field]
        broken = (previous is not None and (
            (t-previous[0]).total_seconds() > GAP_SECONDS
            or (w or {}).get('started') != (previous[3] or {}).get('started')
            or (parse_t((w or {}).get('r')) and parse_t((previous[3] or {}).get('r'))
                and abs((parse_t(w['r'])-parse_t(previous[3]['r'])).total_seconds()) > 60)
            or any(row[i] is not None and previous[i] is not None and row[i] < previous[i] - 0.01 for i in (1, 2))))
        if value is None or broken:
            if run: runs.append(run)
            run = []
        if value is not None:
            run.append((t, value))
        previous = row
    if run: runs.append(run)
    return runs


def _current_panels(samples, provider):
    """Latest sample defines membership; history never resurrects an absent window."""
    panels = []
    for name, label, pts in _series(samples, provider):
        _, used, _, window = pts[-1]
        if used is None or window is None:
            continue
        if name == 'claude' and label.strip().upper() == '5H' and parse_t(window.get('r')) is None:
            continue
        panels.append((name, label, pts))
    return panels


def _panel_order_key(panel):
    """Display-only order; never changes eligibility or historical series.

    Grok (WEEK first), Claude ALL WEEK, Claude 5H, Codex (WEEK first),
    Claude FABLE WEEK, AGY ARGON WEEK, then other active windows. Extra
    windows sort by provider and label, stripped/casefolded, then exact strings.
    Inactive AGY rows stay last: GEM 5H, 3P 5H, GEM WEEK, 3P WEEK, then
    other labels alphabetically. Future registered providers use the extra slot.
    """
    name, label, points = panel
    normalized = label.strip().casefold()
    tie = (name.strip().casefold(), normalized, name, label)
    if name == 'agy' and points[-1][3].get('started') is False:
        inactive = {'gem 5h': 0, '3p 5h': 1, 'gem week': 2, '3p week': 3}
        return (7, inactive.get(normalized, 4), *tie)
    if name == 'grok':
        return (0, 0 if normalized == 'week' else 1, *tie)
    if name == 'codex':
        return (3, 0 if normalized == 'week' else 1, *tie)
    priority = {('claude', 'all week'): 1, ('claude', '5h'): 2,
                ('claude', 'fable week'): 4, ('agy', 'argon week'): 5}
    return (priority.get((name, normalized), 6), 0, *tie)


def _display_header(provider, label):
    """Visible aliases only; canonical IDs drive selection, series and ordering."""
    aliases = {('claude', 'ALL WEEK'): ('CLAUDE', 'WEEK'),
               ('claude', 'FABLE WEEK'): ('FABLE', 'WEEK')}
    return aliases.get((provider, label), (provider.upper(), label))


# Original MIT provider identifiers; only hash-verified passive geometry is embedded.
LOGO_DIR = Path(__file__).resolve().parent / 'assets' / 'logos'
LOGO_HEIGHT = 7.0  # Logical pixels, approximately the 9px provider text's cap height.
LOGO_GAP = 2.0
_LOGO_FOR_PROVIDER = {'grok': 'grok', 'claude': 'claude', 'codex': 'openai', 'agy': 'antigravity'}
# SHA256 and tight artwork bounds (x, y, width, height).
_LOGO_SPECS = {
    'grok': ('683540dd20c7b10f2f4020f03ed00e7c2de10a2760003f0e7da48f590aa44e28', (0, 0, 10, 10)),
    'claude': ('83e4b3a300a40ff7c0efc8cc6a7b8ef382b214e5efc0f328e0c169a057b27b95', (0, 0, 10, 10)),
    'openai': ('3f8f3d349fef97cfa88fbfc686a55b3a1658b99d7a59cfaebfca4591625b715f', (0, 0, 10, 10)),
    'antigravity': ('325b9925270f38690d03f2069912e8d0d7589c01077f5dd6f452197dcfe84944', (0, 0, 10, 10)),
}


@lru_cache(maxsize=4)
def _logo_template(name):
    """Validate and cache local original artwork, never resolve external resources."""
    import hashlib
    import re
    import xml.etree.ElementTree as ET
    if name not in _LOGO_SPECS:
        raise ValueError('Unknown bundled logo')
    digest, bounds = _LOGO_SPECS[name]
    try:
        raw = (LOGO_DIR / (name + '.svg')).read_bytes()
    except OSError as exc:
        raise RuntimeError(f'Bundled logo is missing or unreadable: {name}') from exc
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f'Bundled logo integrity check failed: {name}')
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('SVG document types/entities are forbidden')
    root = ET.fromstring(raw)
    _validate_svg(raw.decode("utf-8"))
    _validate_svg(ET.tostring(root, encoding='unicode'))
    ids = {e.get('id'): '_LOGO_INSTANCE_' + e.get('id') for e in root.iter() if e.get('id')}
    for element in root.iter():
        for key, value in list(element.attrib.items()):
            if key == 'id':
                element.set(key, ids[value])
            elif 'url(' in value:
                old = re.fullmatch(r'url\(#([A-Za-z_][A-Za-z0-9_.:-]*)\)', value).group(1)
                element.set(key, f'url(#{ids[old]})')
    root.set('viewBox', ' '.join(str(v) for v in bounds))
    root.set('width', f'{LOGO_HEIGHT*bounds[2]/bounds[3]:.6f}')
    root.set('height', str(LOGO_HEIGHT))
    root.set('x', '_LOGO_X_')
    root.set('y', '_LOGO_Y_')
    root.set('preserveAspectRatio', 'xMidYMid meet')
    # Isolate identifier SVG defaults from the chart's rounded line styling.
    root.set('stroke-linecap', root.get('stroke-linecap', 'butt'))
    root.set('stroke-linejoin', root.get('stroke-linejoin', 'miter'))
    root.set('stroke', root.get('stroke', 'none'))
    root.set('data-role', 'provider-logo')
    root.set('data-logo', name)
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    return ET.tostring(root, encoding='unicode')


def _provider_logo(provider, visible_name, x, baseline, index):
    name = _LOGO_FOR_PROVIDER.get(provider)
    if name is None:
        return ''  # Future providers need an explicitly sourced asset, never a substitute.
    advance = sum(_metrics().get(str(ord(c)), 1.0) for c in visible_name) * 9
    return (_logo_template(name).replace('_LOGO_INSTANCE_', f'logo-{index}-{name}-')
            .replace('_LOGO_X_', f'{x+advance+LOGO_GAP:.6f}')
            .replace('_LOGO_Y_', f'{baseline-LOGO_HEIGHT:.6f}'))


def _reset_in(window, now):
    if window.get('started') is False:
        return 'ยังไม่เริ่ม'
    reset = parse_t(window.get('r'))
    if reset is None:
        return 'Reset unavailable'
    seconds = (reset - now).total_seconds()
    if seconds <= 0:
        return 'Reset due'
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    duration = f'{days}d {hours}h' if days else f'{hours}h {minutes}m' if hours else f'{minutes}m' if minutes else '<1m'
    return 'Reset in ' + duration


def _title_layout(title):
    for size, max_lines in ((16, 2), (14, 2), (12, 3)):
        lines = _wrap(title, size, WIDTH-32)
        if len(lines) <= max_lines:
            return lines, size
    raise ValueError('Title is too long for the fixed portrait; use a shorter title')


def render_svg(samples, *, provider="all", hours=None, title=None):
    """Fixed phone portrait of populated current windows and their recorded history.

    None means 24h; nonpositive hours means all history. Unrepresentable layouts
    fail explicitly instead of dropping windows, clipping text, or shrinking type.
    """
    now = datetime.now(ICT)
    lookback = 24.0 if hours is None else finite(hours)
    if lookback is None:
        raise ValueError('hours must be finite')
    cutoff = now - timedelta(hours=lookback) if lookback > 0 else None
    samples = [s for s in samples if isinstance(s, dict) and (t := parse_t(s.get('t'))) is not None
               and t <= now and (cutoff is None or t >= cutoff)]
    panels = sorted(_current_panels(samples, provider), key=_panel_order_key)
    active = [p for p in panels if not (p[0] == 'agy' and p[2][-1][3].get('started') is False)]
    inactive = [p for p in panels if p not in active]
    title_lines, title_size = _title_layout(title or 'Tokencheck / Usage & pace')
    title_step = title_size * 1.3
    title_bottom = 28 + title_step * (len(title_lines)-1)
    start_y = title_bottom + 72
    columns = 2 if len(active) > 1 else 1
    card_w = (WIDTH-24-8*(columns-1))/columns
    headers = [_display_header(name, label) for name, label, _ in active]
    wrapped = [_wrap(label,10,card_w-65) for _, label in headers]
    # Extra label lines reserve both header space and a majority-area plot.
    row_min = [174 + 36*(max(len(lines) for lines in wrapped[i:i+columns])-1)
               for i in range(0,len(active),columns)]
    inactive_labels = [_wrap(label,11,150) for _,label,_ in inactive]
    inactive_heights = [max(24,14*len(lines)+10) for lines in inactive_labels]
    inactive_h = 25 + sum(inactive_heights) if inactive else 0
    rows = len(row_min)
    available = HEIGHT-26-start_y-inactive_h-(10 if inactive and active else 0)-8*max(0,rows-1)
    if sum(row_min) > available:
        raise ValueError('Current windows/labels exceed readable fixed-portrait capacity')
    extra = min(70, (available-sum(row_min))/rows) if rows else 0
    row_heights = [h+extra for h in row_min]
    chunks = []
    def text(x,y,value,size=12,color=SUBTEXT,anchor='start',role=''):
        chunks.append(f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}" fill="{color}" text-anchor="{anchor}" data-role="{role}">{escape(_clean(value))}</text>')
    def rect(x,y,w,h,fill,rx=0,extra=''):
        chunks.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" rx="{rx}" fill="{fill}" {extra}/>')
    def line(x1,y1,x2,y2,color,width=1,extra=''):
        chunks.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" stroke="{color}" stroke-width="{width}" {extra}/>')
    def pct(value):
        if value is None: return '—'
        number = f'{value:.2f}'.rstrip('0').rstrip('.') if abs(value)<10000 else f'{value:.2g}'
        return number+'%'
    for i,label in enumerate(title_lines):
        text(16,28+title_step*i,label,title_size,TEXT,role='title')
    look = f'{lookback:g}h history' if lookback>0 else 'All history'
    text(16,title_bottom+19,f'{look} · {now:%d %b %Y %H:%M} ICT',11)
    line(16,title_bottom+35,32,title_bottom+35,USAGE,2)
    text(38,title_bottom+39,'Usage',11,TEXT)
    line(90,title_bottom+35,108,title_bottom+35,PACE,1.5,'stroke-dasharray="4 3"')
    text(114,title_bottom+39,'Pace / marker',11,PACE)
    text(WIDTH-16,title_bottom+39,'Scale 0–100%',11,SUBTEXT,'end')
    earliest = min((parse_t(s['t']) for s in samples),default=now)
    t0 = cutoff or earliest
    text(16,title_bottom+57,f'{t0:%d %b %H:%M} – {now:%d %b %H:%M} ICT',10)
    y=start_y
    for row,card_h in enumerate(row_heights):
        for col in range(columns):
            index=row*columns+col
            if index>=len(active): break
            name,label,pts=active[index]
            labels=wrapped[index]
            x=12+col*(card_w+8)
            rect(x,y,card_w,card_h,CARD,11,f'stroke="{GRID}" data-kind="active" data-provider="{name}" data-window="{escape(_clean(label),quote=True)}"')
            line(x+12,y,x+43,y,USAGE,2)
            text(x+10,y+14,headers[index][0],9,PACE,role='provider')
            chunks.append(_provider_logo(name,headers[index][0],x+10,y+14,index))
            for i,lab in enumerate(labels):
                text(x+card_w-10,y+14+i*14,lab,10,TEXT,'end',role='window')
            shift=14*(len(labels)-1)
            used,pace,w=pts[-1][1:]
            text(x+10,y+31+shift,pct(used),13,USAGE,role='used')
            text(x+card_w-10,y+31+shift,'Pace '+pct(pace) if w.get('started') is not False else 'ยังไม่เริ่ม',9,PACE,'end',role='pace')
            text(x+10,y+45+shift,_reset_in(w,now),9,SUBTEXT,role='reset')
            bx,by,bw=x+card_w-62,y+40+shift,49
            rect(bx,by,bw,4,'#071120',1,f'stroke="{GRID}" data-role="battery"')
            rect(bx+bw,by+1,2,2,GRID,0.5)
            fill_w=(bw-2)*min(100,max(0,used))/100
            for cell in range(12):
                left=cell*(bw-2)/12
                amount=max(0,min((bw-2)/12-1,fill_w-left))
                if amount:rect(bx+1+left,by+1,amount,2,USAGE,0.3)
            if pace is not None:
                px=bx+bw*min(100,max(0,pace))/100
                chunks.append(f'<path d="M {px-2:.2f} {by-4:.2f} L {px+2:.2f} {by-4:.2f} L {px:.2f} {by-1:.2f} Z" fill="{PACE}" data-role="pace-marker"/>')
                line(px,by,px,by+5,PACE,0.8)
            left,right,top,bottom=x+21,x+card_w-7,y+55+shift,y+card_h-16
            # This passive plot-area rectangle also makes geometry inspectable in tests.
            rect(left,top,right-left,bottom-top,'none',extra='data-role="plot-area"')
            span=max(1,(now-t0).total_seconds())
            def xy(t,v):return left+(t-t0).total_seconds()/span*(right-left),bottom-min(100,max(0,v))/100*(bottom-top)
            for value in (0,50,100):
                gy=xy(t0,value)[1];line(left,gy,right,gy,GRID,0.6)
                text(left-4,gy+3,str(value),9,SUBTEXT,'end',role='scale')
            text(left,bottom+11,t0.strftime('%H:%M'),9,SUBTEXT,role='time')
            text(right,bottom+11,now.strftime('%H:%M'),9,SUBTEXT,'end',role='time')
            for field,color in ((1,USAGE),(2,PACE)):
                for run in _segments(pts,field):
                    coords=[xy(t,v) for t,v in run]
                    d='M '+' L '.join(f'{xx:.2f} {yy:.2f}' for xx,yy in coords)
                    if field==1 and len(coords)>1:
                        chunks.append(f'<path d="{d} L {coords[-1][0]:.2f} {bottom:.2f} L {coords[0][0]:.2f} {bottom:.2f} Z" fill="url(#usage-fill)"/>')
                    dash='stroke-dasharray="3 3"' if field==2 else ''
                    if len(coords)>1:
                        chunks.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="4" stroke-opacity="0.10" {dash}/>')
                        chunks.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.3" {dash}/>')
                    else:
                        xx,yy=coords[0]
                        chunks.append(f'<circle cx="{xx:.2f}" cy="{yy:.2f}" r="1.3" fill="{color}"/>')
        y+=card_h+8
    if active:y-=8
    if inactive:
        if active:y+=10
        text(16,y+14,'AGY / WINDOWS NOT STARTED',10,PACE)
        y+=25
        for (name,label,pts),labels,h in zip(inactive,inactive_labels,inactive_heights):
            rect(12,y,WIDTH-24,h-2,CARD,5,f'data-kind="inactive" data-window="{escape(_clean(label),quote=True)}"')
            for i,lab in enumerate(labels):text(22,y+16+i*14,lab,11,TEXT,role='inactive-window')
            text(244,y+16,pct(pts[-1][1]),12,USAGE,'end',role='inactive-used')
            text(WIDTH-22,y+16,'ยังไม่เริ่ม',11,PACE,'end',role='inactive-state')
            y+=h
    if not panels:
        rect(12,start_y,WIDTH-24,140,CARD,12,f'stroke="{GRID}"')
        text(26,start_y+40,'No current populated windows',18,TEXT)
        text(26,start_y+70,'No history samples or real usage in this range.',12)
        text(26,start_y+96,'Waiting for recorded local usage.',12)
    latest=max((parse_t(s['t']) for s in samples),default=None)
    footer=f'Sampled {latest:%H:%M} ICT · Gaps & resets disconnected' if latest else 'LOCAL HISTORY · No inferred samples'
    text(16,HEIGHT-10,footer,10)
    defs='<defs><linearGradient id="usage-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#53e7ff" stop-opacity="0.20"/><stop offset="1" stop-color="#53e7ff" stop-opacity="0.02"/></linearGradient></defs>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" font-family="Noto Sans Thai" stroke-linecap="round" stroke-linejoin="round">'
            +defs+f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{BG}"/>'+''.join(chunks)+'</svg>')


def atomic_write(path, data):
    """Publish a complete PNG without exposing partially written bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.'+path.name+'-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as handle: handle.write(data)
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)
    return path


def _validate_svg(svg):
    """Validate passive chart/logo vectors with resolved, document-local references."""
    import re
    import xml.etree.ElementTree as ET
    if '<!DOCTYPE' in svg.upper() or '<!ENTITY' in svg.upper() or '<?' in svg:
        raise ValueError('SVG document types/entities/processing instructions are forbidden')
    root=ET.fromstring(svg)
    allowed={'svg','defs','linearGradient','stop','rect','text','line','path','circle',
             'g','clipPath','mask','filter','feFlood','feColorMatrix','feOffset',
             'feGaussianBlur','feComposite','feBlend'}
    attributes={'x','y','x1','y1','x2','y2','cx','cy','r','rx','width','height','viewBox',
                'preserveAspectRatio','font-family','font-size','text-anchor','fill',
                'fill-opacity','fill-rule','clip-rule','stroke','stroke-width','stroke-opacity',
                'stroke-linecap','stroke-linejoin','stroke-dasharray','opacity','d','id',
                'offset','stop-color','stop-opacity','transform','clip-path','mask','filter',
                'filterUnits','color-interpolation-filters','flood-opacity','flood-color',
                'result','in','in2','type','values','dx','dy','stdDeviation','operator','mode',
                'maskUnits','maskContentUnits','clipPathUnits'}
    ids=set()
    references=[]
    for element in root.iter():
        tag=element.tag
        if tag.startswith('{http://www.w3.org/2000/svg}'):
            tag=tag.split('}',1)[1]
        if tag not in allowed:
            raise ValueError('Unsupported SVG element')
        for key,value in element.attrib.items():
            if key not in attributes and not key.startswith('data-'):
                raise ValueError('Unsupported or external SVG attribute')
            if key=='id':
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.:-]*',value) or value in ids:
                    raise ValueError('Invalid or duplicate SVG id')
                ids.add(value)
            if key.startswith('data-'):
                continue  # Escaped descriptive text, never a resource attribute.
            if 'url(' in value or (key in ('clip-path','mask','filter') and value!='none'):
                match=re.fullmatch(r'url\(#([A-Za-z_][A-Za-z0-9_.:-]*)\)',value)
                if not match or key not in ('fill','stroke','clip-path','mask','filter'):
                    raise ValueError('External SVG resources are forbidden')
                references.append(match.group(1))
            elif key in ('fill','stroke','stop-color','flood-color'):
                if not re.fullmatch(r'#[0-9A-Fa-f]{3,8}|none|white|black',value):
                    raise ValueError('Unsupported SVG paint')
    if any(ref not in ids for ref in references):
        raise ValueError('Unresolved SVG reference')
    return root


@lru_cache(maxsize=4)
def _fallback_logo_png(name):
    """Lazy Matplotlib vector fallback, including local clips/masks; no network.

    Uses original Bezier paths and paints. Like the chart fallback, decorative
    SVG filter effects are omitted; they do not replace or recolor the artwork.
    """
    import io
    import re
    import xml.etree.ElementTree as ET
    from PIL import Image, ImageChops
    from fontTools.pens.basePen import BasePen
    from fontTools.svgLib.path import parse_path
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.path import Path as MplPath
    from matplotlib.patches import PathPatch, Rectangle
    from matplotlib.colors import to_rgba
    from matplotlib.transforms import Affine2D

    root=ET.fromstring(_logo_template(name))
    vx,vy,vw,vh=map(float,root.get('viewBox').split())
    width=max(1,round(float(root.get('width'))*PIXEL_RATIO))
    height=round(LOGO_HEIGHT*PIXEL_RATIO)
    ids={e.get('id'):e for e in root.iter() if e.get('id')}
    def blank():return Image.new('RGBA',(width,height),(0,0,0,0))

    class Pen(BasePen):
        def __init__(self):
            super().__init__(None);self.vertices=[];self.codes=[];self.start=None
        def _moveTo(self,p):
            self.start=p;self.vertices.append(p);self.codes.append(MplPath.MOVETO)
        def _lineTo(self,p):self.vertices.append(p);self.codes.append(MplPath.LINETO)
        def _curveToOne(self,a,b,c):
            self.vertices.extend((a,b,c));self.codes.extend((MplPath.CURVE4,)*3)
        def _qCurveToOne(self,a,b):
            self.vertices.extend((a,b));self.codes.extend((MplPath.CURVE3,)*2)
        def _closePath(self):
            self.vertices.append(self.start);self.codes.append(MplPath.CLOSEPOLY)
        def _endPath(self):pass

    inherited_keys=('fill','stroke','fill-opacity','stroke-opacity','stroke-width')
    def draw_shape(node,style):
        fig=Figure(figsize=(width/72,height/72),dpi=72,facecolor=(0,0,0,0))
        canvas=FigureCanvasAgg(fig)
        ax=fig.add_axes((0,0,1,1));ax.set_xlim(vx,vx+vw);ax.set_ylim(vy+vh,vy);ax.axis('off')
        paint={'facecolor':to_rgba(style.get('fill','black'),float(style.get('fill-opacity',1))),
               'edgecolor':to_rgba(style.get('stroke','none'),float(style.get('stroke-opacity',1))),
               'linewidth':float(style.get('stroke-width',1))*width/vw}
        if node.tag.split('}')[-1]=='path':
            pen=Pen();parse_path(node.get('d'),pen)
            artist=PathPatch(MplPath(pen.vertices,pen.codes),**paint)
        else:
            artist=Rectangle((float(node.get('x',0)),float(node.get('y',0))),
                             float(node.get('width')),float(node.get('height')),**paint)
        if node.get('transform'):
            # The verified assets use only translation on clip rectangles.
            match=re.fullmatch(r'translate\(\s*([-+0-9.eE]+)[ ,]+([-+0-9.eE]+)\s*\)',node.get('transform'))
            if not match:raise ValueError('Unsupported bundled-logo fallback transform')
            artist.set_transform(Affine2D().translate(*map(float,match.groups()))+ax.transData)
        ax.add_patch(artist);canvas.draw()
        result=Image.frombytes('RGBA',(width,height),bytes(canvas.buffer_rgba()))
        fig.clear()
        return result

    def render_node(node,inherited,definition=False):
        tag=node.tag.split('}')[-1]
        if tag in ('defs','filter') or tag.startswith('fe') or (tag in ('clipPath','mask') and not definition):
            return blank()
        style=dict(inherited)
        style.update({k:node.get(k) for k in inherited_keys if node.get(k) is not None})
        if tag in ('path','rect'):
            layer=draw_shape(node,style)
        elif tag in ('svg','g','clipPath','mask'):
            layer=blank()
            for child in node:layer.alpha_composite(render_node(child,style))
        else:
            raise ValueError('Unsupported bundled-logo fallback geometry')
        for key in ('clip-path','mask'):
            reference=node.get(key)
            if reference:
                target=ids[reference[5:-1]]
                mask=render_node(target,{'fill':'white'},definition=True)
                alpha=mask.getchannel('A')
                if key=='mask':alpha=ImageChops.multiply(alpha,mask.convert('L'))
                layer.putalpha(ImageChops.multiply(layer.getchannel('A'),alpha))
        if node.get('opacity'):
            factor=float(node.get('opacity'))
            layer.putalpha(layer.getchannel('A').point(lambda a:round(a*factor)))
        return layer

    result=render_node(root,{'fill':'black'})
    output=io.BytesIO();result.save(output,format='PNG')
    return output.getvalue()


def _fallback_with_logos(svg):
    """Keep the existing chart fallback, adding its nested original vector identifiers."""
    import io
    from PIL import Image
    from plot_matplotlib import rasterize
    result=Image.open(io.BytesIO(rasterize(svg,FONT))).convert('RGBA')
    root=_validate_svg(svg)
    for node in root:
        if node.get('data-role')=='provider-logo':
            logo=Image.open(io.BytesIO(_fallback_logo_png(node.get('data-logo')))).convert('RGBA')
            result.alpha_composite(logo,(round(float(node.get('x'))*PIXEL_RATIO),
                                         round(float(node.get('y'))*PIXEL_RATIO)))
    output=io.BytesIO();result.save(output,format='PNG')
    return output.getvalue()


def render_png(samples: list[dict[str, Any]], out_path: Path, *, provider: str = 'all',
               hours: float | None = None, title: str | None = None) -> Path:
    if not FONT.is_file():
        raise RuntimeError('Bundled chart font is missing')
    svg=render_svg(samples,provider=provider,hours=hours,title=title)
    _validate_svg(svg)
    try:
        from resvg_py import svg_to_bytes
        data=svg_to_bytes(svg_string=svg, font_files=[str(FONT)], skip_system_fonts=True,
                          font_family='Noto Sans Thai', sans_serif_family='Noto Sans Thai', zoom=PIXEL_RATIO)
    except (ImportError, ValueError, RuntimeError) as exc:
        warnings.warn(f'resvg unavailable ({type(exc).__name__}); using Matplotlib fallback', RuntimeWarning)
        data=_fallback_with_logos(svg)
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise RuntimeError('Rasterizer did not return a PNG')
    return atomic_write(out_path,data)


def seed_from_snapshot(state: Path | None = None) -> int:
    """Append current snapshot into history; retained for explicit legacy callers."""
    st = state_dir(state)
    snap = st / 'snapshot.json'
    if snap.exists():
        try: append_envelope(json.loads(snap.read_text(encoding='utf-8')),st)
        except Exception: pass
    return len(load_samples(st))


def main() -> int:
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider',default='all')
    parser.add_argument('--hours',type=float,default=24.0)
    parser.add_argument('--out',required=True)
    parser.add_argument('--state',default=None)
    args=parser.parse_args()
    print(render_png(load_samples(state_dir(args.state)),Path(args.out),provider=args.provider,hours=args.hours))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
