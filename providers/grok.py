"""Local Grok dashboard usage modal with slash-command safety gates."""
from .common import Attempt, ICT, iso, log
from datetime import datetime
import re
import shutil
import threading
import time
from datetime import timedelta
from . import common as util
from config import load_config
import shlex
GROK_TMUX = load_config().grok_tmux_pane

ANSI_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october november december".split())}


def _tmux(*args: str, timeout: float = 5) -> tuple[int, str]:
    code, out, _ = util.run(["tmux", *args], timeout)
    return code, out


def _pane() -> str:
    _c, out = _tmux("capture-pane", "-p", "-t", GROK_TMUX)
    out = ANSI_RE.sub("", out).replace("\r", "")
    return "\n".join(re.sub(r"[ \t]+[█▐▌┃][ \t]*$", "", l) for l in out.split("\n"))


def parse_grok_pane(text: str, now: datetime) -> dict | None:
    heads = list(re.finditer(r"^\s*(?:[│┃]\s*)?Weekly\s+limit\s+\(([^)\n]+)\)\s*(?:[│┃])?\s*$", text, re.M | re.I))
    if not heads:
        return None
    h = heads[-1]
    lines = [l for l in text[h.end():].split("\n") if l.strip() and not re.fullmatch(r"\s*[│┃]\s*(?:[│┃]\s*)?", l)]
    if len(lines) < 2:
        return None
    m = re.match(r"^\s*(?:[│┃]\s*)?[█▓▒░▐▌]+\s+(\d+(?:\.\d+)?)\s*%\s*(?:[│┃])?\s*$", lines[0])
    r = re.match(r"^\s*(?:[│┃]\s*)?Resets:\s*([A-Za-z]+)\s+(\d{1,2}),\s+(\d{1,2}):(\d{2})(?:\s*([AP]M))?\s*(?:[│┃])?\s*$",
                 lines[1], re.I)
    if not m or not r:
        return None
    used = float(m.group(1))
    month = MONTHS.get(r.group(1).lower())
    if not month or not 0 <= used <= 100:
        return None
    hour, minute = int(r.group(3)), int(r.group(4))
    if r.group(5):
        hour = hour % 12 + (12 if r.group(5).upper() == "PM" else 0)
    year = now.astimezone(ICT).year
    try:
        reset = datetime(year, month, int(r.group(2)), hour, minute, tzinfo=ICT)
        if reset < now - timedelta(minutes=10):
            reset = reset.replace(year=year + 1)
    except ValueError:
        return None
    if (reset - now).total_seconds() > 8 * 86400:
        return None
    payg = None
    pm = list(re.finditer(r"Pay-as-you-go:\s*\$([\d.]+)\s+used\s+of\s+\$([\d.]+)\s+limit", text, re.I))
    if pm:
        payg = {"used_usd": float(pm[-1].group(1)), "limit_usd": float(pm[-1].group(2))}
    return {"plan": h.group(1).strip(), "used_percent": used, "reset_at": iso(reset), "payg": payg}


_grok_lock = threading.Lock()


def _grok_input_empty(text: str) -> bool:
    rows = [l for l in text.split("\n") if re.match(r"^\s*│\s*❯", l)]
    return bool(rows) and re.match(r"^\s*│\s*❯\s*(?:Dispatch a new agent)?\s*│\s*$", rows[-1]) is not None


def _grok_focus_input() -> str:
    """Close any modal, then move focus to the prompt input (footer says Tab:list when focused)."""
    pane = _pane()
    for _ in range(3):
        if "Esc close" in pane or "Esc to cancel" in pane:
            _tmux("send-keys", "-t", GROK_TMUX, "Escape")
            time.sleep(0.4)
            pane = _pane()
        if "Tab:input" in pane:
            _tmux("send-keys", "-t", GROK_TMUX, "Tab")
            time.sleep(0.4)
            pane = _pane()
        if "Tab:list" in pane and "Esc close" not in pane:
            break
    return pane


def grok_dashboard_usage(now: datetime, _ctx: dict) -> dict:
    if not shutil.which("tmux"):
        raise Attempt("tmux_missing")
    exe = util.which("grok", ["~/.local/bin/grok", "~/.grok/bin/grok"])
    if not exe:
        raise Attempt("grok_not_installed")
    with _grok_lock:
        if _tmux("has-session", "-t", GROK_TMUX)[0] != 0:
            cwd = load_config().state_dir / "grok-cwd"
            cwd.mkdir(parents=True, exist_ok=True)
            _tmux("new-session", "-d", "-s", GROK_TMUX, "-x", "160", "-y", "50",
                  f"cd {shlex.quote(str(cwd))} && exec {shlex.quote(exe)} dashboard")
            for _ in range(30):
                time.sleep(0.5)
                if "Enter:create" in _pane() or "Enter:send" in _pane():
                    break
            else:
                raise Attempt("dashboard_not_ready")
        pane = _grok_focus_input()
        if "Tab:list" not in pane:
            raise Attempt("prompt_input_not_focused")
        if "No agents yet" not in pane and "+ New Agent" not in pane:
            raise Attempt("dashboard_not_visible")
        if not _grok_input_empty(pane):
            _tmux("send-keys", "-t", GROK_TMUX, *(["BSpace"] * 60))
            time.sleep(0.3)
            if not _grok_input_empty(_pane()):
                raise Attempt("input_not_empty_refusing_to_type")
        _tmux("send-keys", "-t", GROK_TMUX, "-l", "/usage")
        # Safety gate: only press Enter when Grok shows /usage as a slash command
        # (otherwise Enter would create an agent and spend tokens).
        ok = False
        for _ in range(10):
            time.sleep(0.3)
            if re.search(r"❯\s*/usage\s+View usage", _pane()):
                ok = True
                break
        if not ok:
            _tmux("send-keys", "-t", GROK_TMUX, *(["BSpace"] * 10))
            raise Attempt("slash_popup_missing_aborted")
        _tmux("send-keys", "-t", GROK_TMUX, "Enter")
        ok = False
        for _ in range(15):
            time.sleep(0.3)
            p = _pane()
            if re.search(r"❯\s*show\s+View usage", p) or "Usage limit" in p:
                ok = True
                break
        if not ok:
            _tmux("send-keys", "-t", GROK_TMUX, "Escape", *(["BSpace"] * 10))
            raise Attempt("usage_submenu_missing_aborted")
        if re.search(r"❯\s*show\s+View usage", _pane()):
            _tmux("send-keys", "-t", GROK_TMUX, "Enter")
        parsed = None
        for _ in range(30):
            time.sleep(0.4)
            parsed = parse_grok_pane(_pane(), now)
            if parsed:
                break
        text = _pane()
        _grok_focus_input()
        if not parsed:
            if re.search(r"balance exhausted|402", text, re.I):
                raise Attempt("billing_exhausted")
            raise Attempt("usage_modal_unparsed")
        if "No agents yet" not in text:
            log("warning: grok dashboard shows agents; verify none was created by the collector")
    return {"provider": "grok", "plan": parsed["plan"], "source": "grok_session",
            "method": "grok_dashboard_usage_modal", "parsed": parsed}
