// tokencheck edge dashboard: one unlisted, self-contained page rendered at the edge.
// Routes: GET /<READ_KEY>/ (HTML), GET /<READ_KEY>/data.json, POST /ingest (Bearer INGEST_SECRET).
// Anything else is 404. Secrets come from Worker secrets READ_KEY and INGEST_SECRET.
import { DurableObject } from 'cloudflare:workers';

const LOGOS = __LOGOS__;      // hidden <svg> sprite with <symbol id="lg-...">
const RATIO = __RATIOS__;     // logo width/height
const MAX_BODY = 256 * 1024;
const STALE_S = 300;
const BASE_HEADERS = { 'X-Robots-Tag': 'noindex, nofollow', 'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff' };

export class SnapshotStore extends DurableObject {
  async write(text) { this.memo = text; await this.ctx.storage.put('snap', text); }
  async read() { if (this.memo === undefined) this.memo = (await this.ctx.storage.get('snap')) || null; return this.memo; }
}

let memo = { at: 0, text: null };
function store(env) { return env.STORE.get(env.STORE.idFromName('snapshot'), { locationHint: 'apac' }); }
async function snapshotText(env) {
  if (memo.text !== null && Date.now() - memo.at < 10000) return memo.text;
  const text = await store(env).read();
  memo = { at: Date.now(), text };
  return text;
}

function notFound() { return new Response('Not found', { status: 404, headers: { ...BASE_HEADERS, 'Cache-Control': 'no-store' } }); }

async function digest(value) { return new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))); }
async function sameSecret(given, expected) {
  if (!given || !expected) return false;
  const [a, b] = await Promise.all([digest(given), digest(expected)]);
  return crypto.subtle.timingSafeEqual(a, b);
}

// ---- shared renderer: runs in the Worker for first paint and in the browser on refresh ----
// Layout is pure CSS (columns from the viewport aspect ratio, rows share the visible height),
// so the first paint already fits; the page script only fine-tunes (visualViewport, last-resort scale).
function R(s, now, ratio) {
  var esc = function (v) { return String(v).replace(/[&<>"']/g, function (c) { return '&#' + c.charCodeAt(0) + ';'; }); };
  var pct = function (v) { if (v === null || v === undefined) return '—'; var a = Math.abs(v); return (a < 10 && v % 1 ? v.toFixed(a < 1 ? 2 : 1) : String(Math.round(v * 10) / 10)) + '%'; };
  var dur = function (sec) { sec = Math.max(0, Math.floor(sec)); var m = Math.floor(sec / 60), d = Math.floor(m / 1440), h = Math.floor(m % 1440 / 60), mm = m % 60;
    return d ? d + 'd ' + h + 'h' : h ? h + 'h ' + mm + 'm' : m ? m + 'm' : sec + 's'; };
  if (!s || !s.panels) return '<p class="empty">No snapshot yet</p>';
  var snapAge = (now - Date.parse(s.t)) / 1000;
  var logo = function (p) { var k = { grok: 'grok', claude: 'claude', codex: 'openai', agy: 'antigravity' }[p]; if (!k) return '';
    return '<svg class="lg" style="width:' + (0.8 * (ratio[k] || 1)).toFixed(2) + 'em" aria-hidden="true"><use href="#lg-' + k + '"/></svg>'; };
  var chart = function (w) {
    var n = (w.hu || []).length; if (n < 2) return '<div class="chw"></div>';
    var W = 100, H = 40, out = '';
    [[w.hu, 'u'], [w.hp, 'p']].forEach(function (pair) {
      // Bridge short sampling gaps (<=20 min); break on longer gaps and on resets (drop > 5 pp).
      var arr = pair[0] || [], runs = [], run = [], prev = null, last = -99;
      for (var i = 0; i < n; i++) { var v = arr[i];
        if (v === null || v === undefined) continue;
        if (run.length && (i - last > 3 || v < prev - 5)) { runs.push(run); run = []; }
        run.push((i / (n - 1) * W).toFixed(1) + ',' + (H - Math.max(0, Math.min(100, v)) / 100 * H).toFixed(1));
        prev = v; last = i; }
      if (run.length) runs.push(run);
      runs.forEach(function (r) { if (r.length === 1) r.push(r[0]); out += '<polyline class="' + pair[1] + '" points="' + r.join(' ') + '"/>'; });
    });
    return '<div class="chw"><svg class="ch" viewBox="0 0 100 40" preserveAspectRatio="none"><line x1="0" y1="20" x2="100" y2="20"/>' + out + '</svg></div>';
  };
  var cards = '', chips = '', n = 0;
  s.panels.forEach(function (w) {
    var age = (w.age || 0) + snapAge;
    var reset = w.st === false ? 'ยังไม่เริ่ม' : (w.r ? (Date.parse(w.r) - now > 0 ? 'Reset in ' + dur((Date.parse(w.r) - now) / 1000) : 'Reset due') : '');
    if (w.st === false) { chips += '<span class="chip">' + esc(w.l) + ' <b>' + pct(w.u) + '</b></span>'; return; }
    n++;
    var over = w.pc !== null && w.pc !== undefined && w.u > w.pc;
    var cells = ''; for (var c = 0; c < 10; c++) cells += '<i' + (w.u >= (c + 0.5) * 10 || (c === 0 && w.u > 0) ? ' class="on"' : '') + '></i>';
    cards += '<section class="card' + (over ? ' over' : '') + (w.stale ? ' st' : '') + '"><header><span class="pv">' + esc(w.hd[0]) + logo(w.p) + '</span><span class="wl">' + esc(w.hd[1]) + '</span></header>' +
      '<div class="nums"><b>' + pct(w.u) + '</b><span class="pc">Pace ' + pct(w.pc) + '</span></div>' +
      '<div class="bat">' + cells + (w.pc !== null && w.pc !== undefined ? '<em style="left:' + Math.max(0, Math.min(100, w.pc)) + '%"></em>' : '') + '</div>' +
      chart(w) + '<footer><span>' + reset + '</span><span' + (age > 900 ? ' class="old"' : '') + '>' + dur(age) + ' ago</span></footer></section>';
  });
  // Not-started AGY windows: tiny chips. They sit in the grid's free last cell when the
  // column count leaves one (classes r2/r3/r4/r5), otherwise in one line under the grid.
  var cls = [2, 3, 4, 5].filter(function (c) { return n % c; }).map(function (c) { return 'r' + c; }).join(' ');
  var chipBody = chips ? '<span class="cht">AGY' + logo('agy') + ' ยังไม่เริ่ม</span>' + chips : '';
  var stale = snapAge > 300;
  return '<div class="top"><h1>tokencheck</h1><span class="ku">Usage</span><span class="kp">Pace</span><span class="sc">24h · 0–100%</span>' +
    '<span class="badge' + (stale ? ' stale' : '') + '">' + (stale ? 'STALE · ' : '') + 'updated ' + dur(snapAge) + ' ago</span></div>' +
    '<main class="' + cls + '">' + cards + (chips ? '<section class="chipcell">' + chipBody + '</section>' : '') + '</main>' +
    (chips ? '<div class="chipline">' + chipBody + '</div>' : '');
}

const CSS = `:root{color-scheme:dark;--c:2;--g:clamp(3px,0.8vmin,8px);font-size:clamp(9px,calc(0.55vmin + 0.5vmax + 3px),15px)}
*{box-sizing:border-box}html{height:100%;background:#050914}
body{margin:0;height:100vh;height:100dvh;overflow:hidden;background:#050914;color:#effcff;font:1em/1.2 -apple-system,system-ui,sans-serif;
padding:max(var(--g),env(safe-area-inset-top)) max(var(--g),env(safe-area-inset-right)) max(var(--g),env(safe-area-inset-bottom)) max(var(--g),env(safe-area-inset-left));display:flex}
body.scroll{overflow:auto}#app{flex:1;min-width:0;min-height:0;display:flex;flex-direction:column;gap:var(--g);transform-origin:0 0}
.top{display:flex;align-items:center;gap:.8em;font-size:.9em;color:#a8bfd2;white-space:nowrap;min-width:0}h1{font-size:1.3em;margin:0;font-weight:600;color:#effcff}
.sc{overflow:hidden;text-overflow:ellipsis}.badge{margin-left:auto;padding:.1em .55em;border-radius:1em;background:#0d192c}.badge.stale{background:#7a1d2c;color:#fff;font-weight:700}
.ku:before,.kp:before{content:"";display:inline-block;width:1.1em;border-top:2px solid #53e7ff;vertical-align:middle;margin-right:.3em}.kp{color:#b497ff}.kp:before{border-top:1.5px dashed #b497ff}
main{flex:1;min-height:0;display:grid;grid-template-columns:repeat(var(--c),minmax(0,1fr));grid-auto-rows:minmax(0,1fr);gap:var(--g)}
.card,.chipcell{background:#0d192c;border:1px solid #243d53;border-radius:.9em;padding:.45em .6em .4em;min-width:0;min-height:0;overflow:hidden;display:flex;flex-direction:column}
.card.over{border-color:#a8792a}.card.st{opacity:.75}
header{display:flex;justify-content:space-between;gap:.4em;font-size:.88em;white-space:nowrap}.pv{color:#b497ff;font-weight:600;display:flex;align-items:center;gap:.25em}.wl{overflow:hidden;text-overflow:ellipsis}
.lg{height:.8em;display:inline-block;flex:none}.nums{display:flex;justify-content:space-between;align-items:baseline}.nums b{font-size:1.45em;color:#53e7ff;font-weight:650}.pc{font-size:.88em;color:#b497ff}
.bat{position:relative;display:flex;gap:1px;height:.38em;min-height:3px;margin:.25em 0 .2em;flex:none}.bat i{flex:1;background:#071120;border-radius:1px}.bat i.on{background:#53e7ff}.bat em{position:absolute;top:-.3em;width:1.5px;height:1em;background:#b497ff;transform:translateX(-50%)}
.chw{flex:1 1 0;min-height:1em;position:relative;margin:.15em 0}.ch{position:absolute;inset:0;width:100%;height:100%}
.ch line{stroke:#243d53;stroke-width:.5;vector-effect:non-scaling-stroke}.ch polyline{fill:none;vector-effect:non-scaling-stroke;stroke-linejoin:round}.ch .u{stroke:#53e7ff;stroke-width:1.6}.ch .p{stroke:#b497ff;stroke-width:1.1;stroke-dasharray:3 2}
footer{display:flex;justify-content:space-between;gap:.4em;font-size:.82em;color:#a8bfd2;white-space:nowrap}.old{color:#ffb86b}
.chipcell{display:none;flex-flow:row wrap;align-content:center;gap:.35em}.chipline{display:flex;flex-wrap:wrap;align-items:center;gap:.35em;font-size:.85em;min-width:0}
.cht{display:flex;align-items:center;gap:.25em;color:#b497ff;font-weight:600;font-size:.88em;white-space:nowrap}
.chip{background:#071120;border:1px solid #243d53;border-radius:1em;padding:.05em .5em;font-size:.82em;color:#a8bfd2;white-space:nowrap}.chip b{color:#53e7ff;font-weight:600}
.r2 .chipcell{display:flex}.r2~.chipline{display:none}
@media (min-aspect-ratio:5/4){:root{--c:3}.r2 .chipcell{display:none}.r2~.chipline{display:flex}.r3 .chipcell{display:flex}.r3~.chipline{display:none}}
@media (min-aspect-ratio:2/1){:root{--c:5}.r3 .chipcell{display:none}.r3~.chipline{display:flex}.r5 .chipcell{display:flex}.r5~.chipline{display:none}}
.empty{color:#a8bfd2}`;

// Fine-tune only: use the visual viewport height, and scale down as a last resort instead of scrolling.
function FIT() {
  var b = document.body, a = document.getElementById('app'), vv = window.visualViewport;
  b.style.height = (vv ? vv.height : window.innerHeight) + 'px';
  a.style.transform = ''; a.style.width = ''; a.style.height = ''; a.style.flex = '';
  var overflow = function () {
    if (a.scrollHeight > a.clientHeight + 1 || a.scrollWidth > a.clientWidth + 1) return true;
    var els = a.querySelectorAll('.card,.chipcell,.chipline,.top');
    for (var i = 0; i < els.length; i++) if (els[i].offsetParent && (els[i].scrollHeight > els[i].clientHeight + 1 || els[i].scrollWidth > els[i].clientWidth + 1)) return true;
    return false;
  };
  var s = 1, w = a.clientWidth, h = a.clientHeight;
  while (overflow() && s > 0.5) { s -= 0.05; a.style.flex = 'none'; a.style.width = (w / s) + 'px'; a.style.height = (h / s) + 'px'; a.style.transform = 'scale(' + s + ')'; }
  b.classList.toggle('scroll', overflow());
  b.setAttribute('data-scale', s.toFixed(2));
}

function page(text, key) {
  let s = null; try { s = JSON.parse(text); } catch (e) {}
  const body = R(s, Date.now(), RATIO);
  const script = `(function(){var R=${R.toString()};var FIT=${FIT.toString()};var ratio=${JSON.stringify(RATIO)};var s=null;
function draw(){document.getElementById('app').innerHTML=R(s,Date.now(),ratio);FIT();}
function pull(){fetch('data.json',{cache:'no-cache'}).then(function(r){return r.ok?r.json():null}).then(function(j){if(j){s=j;draw();}}).catch(function(){});}
try{s=JSON.parse(document.getElementById('d').textContent)}catch(e){}
var t;function refit(){clearTimeout(t);t=setTimeout(FIT,60);}FIT();
addEventListener('resize',refit);addEventListener('orientationchange',refit);if(window.visualViewport)visualViewport.addEventListener('resize',refit);
setInterval(pull,60000);setInterval(draw,15000);document.addEventListener('visibilitychange',function(){if(!document.hidden)pull();});})();`;
  const data = (text || 'null').replace(/</g, '\\u003c');
  return `<!doctype html><html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex,nofollow"><meta name="referrer" content="no-referrer"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="theme-color" content="#050914"><title>tokencheck</title><style>${CSS}</style></head><body>${LOGOS}<div id="app">${body}</div><script id="d" type="application/json">${data}</script><script>${script}</script></body></html>`;
}

async function etagFor(text) {
  const d = await digest(text || 'none');
  return '"' + Array.from(d.slice(0, 8), (b) => b.toString(16).padStart(2, '0')).join('') + '"';
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === '/ingest') {
      if (request.method !== 'POST') return notFound();
      const auth = request.headers.get('Authorization') || '';
      if (!auth.startsWith('Bearer ') || !(await sameSecret(auth.slice(7), env.INGEST_SECRET))) return notFound();
      const len = Number(request.headers.get('Content-Length') || 0);
      if (len > MAX_BODY) return new Response('Too large', { status: 413, headers: BASE_HEADERS });
      const text = await request.text();
      if (text.length > MAX_BODY) return new Response('Too large', { status: 413, headers: BASE_HEADERS });
      let s; try { s = JSON.parse(text); } catch (e) { return new Response('Bad JSON', { status: 400, headers: BASE_HEADERS }); }
      if (!s || s.v !== 1 || !Array.isArray(s.panels) || typeof s.t !== 'string') return new Response('Bad snapshot', { status: 400, headers: BASE_HEADERS });
      await store(env).write(text);
      memo = { at: Date.now(), text };
      return new Response('ok', { headers: { ...BASE_HEADERS, 'Cache-Control': 'no-store' } });
    }
    const key = env.READ_KEY;
    if (!key || (request.method !== 'GET' && request.method !== 'HEAD')) return notFound();
    const base = '/' + key;
    const p = url.pathname;
    if (p === base) return Response.redirect(url.origin + base + '/', 301);
    const isPage = p === base + '/', isData = p === base + '/data.json';
    if (!isPage && !isData) return notFound();
    const text = await snapshotText(env);
    const tag = await etagFor((isPage ? 'p' : 'd') + (text || ''));
    const headers = { ...BASE_HEADERS, ETag: tag, 'Cache-Control': 'private, no-cache',
      'Content-Type': isPage ? 'text/html; charset=utf-8' : 'application/json; charset=utf-8' };
    if (isPage) headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
    if (request.headers.get('If-None-Match') === tag) return new Response(null, { status: 304, headers });
    return new Response(request.method === 'HEAD' ? null : (isPage ? page(text, key) : (text || 'null')), { headers });
  },
};
