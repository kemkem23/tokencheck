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
function R(s, now, ratio) {
  var esc = function (v) { return String(v).replace(/[&<>"']/g, function (c) { return '&#' + c.charCodeAt(0) + ';'; }); };
  var pct = function (v) { if (v === null || v === undefined) return '—'; var a = Math.abs(v); return (a < 10 && v % 1 ? v.toFixed(a < 1 ? 2 : 1) : String(Math.round(v * 10) / 10)) + '%'; };
  var dur = function (sec) { sec = Math.max(0, Math.floor(sec)); var m = Math.floor(sec / 60), d = Math.floor(m / 1440), h = Math.floor(m % 1440 / 60), mm = m % 60;
    return d ? d + 'd ' + h + 'h' : h ? h + 'h ' + mm + 'm' : m ? m + 'm' : sec + 's'; };
  if (!s || !s.panels) return '<p class="empty">No snapshot yet</p>';
  var snapAge = (now - Date.parse(s.t)) / 1000;
  var logo = function (p) { var k = { grok: 'grok', claude: 'claude', codex: 'openai', agy: 'antigravity' }[p]; if (!k) return '';
    return '<svg class="lg" style="width:' + (0.72 * (ratio[k] || 1)).toFixed(2) + 'em" aria-hidden="true"><use href="#lg-' + k + '"/></svg>'; };
  var chart = function (w) {
    var n = (w.hu || []).length; if (n < 2) return '';
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
    return '<svg class="ch" viewBox="0 0 100 40" preserveAspectRatio="none"><line x1="0" y1="20" x2="100" y2="20"/>' + out + '</svg>';
  };
  var html = '', small = '';
  s.panels.forEach(function (w) {
    var age = (w.age || 0) + snapAge;
    var reset = w.st === false ? 'ยังไม่เริ่ม' : (w.r ? (Date.parse(w.r) - now > 0 ? 'Reset in ' + dur((Date.parse(w.r) - now) / 1000) : 'Reset due') : '');
    if (w.st === false) { small += '<div class="row"><span>' + esc(w.p.toUpperCase()) + logo(w.p) + ' ' + esc(w.l) + '</span><b>' + pct(w.u) + '</b><i>' + reset + '</i></div>'; return; }
    var over = w.pc !== null && w.pc !== undefined && w.u > w.pc;
    var cells = ''; for (var c = 0; c < 10; c++) cells += '<i' + (w.u >= (c + 0.5) * 10 || (c === 0 && w.u > 0) ? ' class="on"' : '') + '></i>';
    html += '<section class="card' + (over ? ' over' : '') + (w.stale ? ' st' : '') + '"><header><span class="pv">' + esc(w.hd[0]) + logo(w.p) + '</span><span class="wl">' + esc(w.hd[1]) + '</span></header>' +
      '<div class="nums"><b>' + pct(w.u) + '</b><span class="pc">Pace ' + pct(w.pc) + '</span></div>' +
      '<div class="bat">' + cells + (w.pc !== null && w.pc !== undefined ? '<em style="left:' + Math.max(0, Math.min(100, w.pc)) + '%"></em>' : '') + '</div>' +
      chart(w) + '<footer><span>' + reset + '</span><span' + (age > 900 ? ' class="old"' : '') + '>' + dur(age) + ' ago</span></footer></section>';
  });
  var stale = snapAge > 300;
  return '<div class="top"><h1>tokencheck</h1><span class="badge' + (stale ? ' stale' : '') + '">' + (stale ? 'STALE · ' : '') + 'updated ' + dur(snapAge) + ' ago</span></div>' +
    '<div class="lg2"><span class="ku">Usage</span><span class="kp">Pace</span><span>24h · 0–100%</span></div>' +
    '<main>' + html + '</main>' + (small ? '<div class="small">' + small + '</div>' : '');
}

const CSS = `:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#050914;color:#effcff;font:11px/1.25 -apple-system,system-ui,sans-serif;padding:max(8px,env(safe-area-inset-top)) 8px 8px}
.top{display:flex;justify-content:space-between;align-items:center;margin:2px 4px 4px}h1{font-size:14px;margin:0;font-weight:600}
.badge{font-size:10px;color:#a8bfd2;padding:2px 6px;border-radius:9px;background:#0d192c}.badge.stale{background:#7a1d2c;color:#fff;font-weight:700}
.lg2{display:flex;gap:10px;font-size:9.5px;color:#a8bfd2;margin:0 4px 6px}.ku:before,.kp:before{content:"";display:inline-block;width:12px;height:0;border-top:2px solid #53e7ff;vertical-align:middle;margin-right:3px}.kp{color:#b497ff}.kp:before{border-top:1.5px dashed #b497ff}
main{display:grid;grid-template-columns:1fr 1fr;gap:6px}.card{background:#0d192c;border:1px solid #243d53;border-radius:10px;padding:6px 7px 5px;min-width:0}.card.over{border-color:#a8792a}.card.st{opacity:.75}
header{display:flex;justify-content:space-between;font-size:9.5px;letter-spacing:.02em}.pv{color:#b497ff;font-weight:600;display:flex;align-items:center;gap:3px}.wl{color:#effcff}
.lg{height:.72em;display:inline-block}.nums{display:flex;justify-content:space-between;align-items:baseline;margin-top:1px}.nums b{font-size:15px;color:#53e7ff;font-weight:650}.pc{font-size:9.5px;color:#b497ff}
.bat{position:relative;display:flex;gap:1px;height:4px;margin:3px 0 2px}.bat i{flex:1;background:#071120;border-radius:1px}.bat i.on{background:#53e7ff}.bat em{position:absolute;top:-3px;width:1.5px;height:10px;background:#b497ff;transform:translateX(-50%)}
.ch{display:block;width:100%;height:auto;aspect-ratio:1.9;margin:2px 0}.ch line{stroke:#243d53;stroke-width:.4;vector-effect:non-scaling-stroke}.ch polyline{fill:none;vector-effect:non-scaling-stroke;stroke-linejoin:round}.ch .u{stroke:#53e7ff;stroke-width:1.6}.ch .p{stroke:#b497ff;stroke-width:1.1;stroke-dasharray:3 2}
footer{display:flex;justify-content:space-between;font-size:9px;color:#a8bfd2}.old{color:#ffb86b}.small{margin-top:6px;background:#0d192c;border:1px solid #243d53;border-radius:10px;padding:4px 8px}
.row{display:grid;grid-template-columns:1fr auto auto;gap:8px;font-size:9.5px;color:#a8bfd2;padding:1px 0}.row span{display:flex;align-items:center;gap:3px}.row b{color:#53e7ff;font-weight:600}.row i{font-style:normal}.empty{color:#a8bfd2}`;

function page(text, key) {
  let s = null; try { s = JSON.parse(text); } catch (e) {}
  const body = R(s, Date.now(), RATIO);
  const script = `(function(){var R=${R.toString()};var ratio=${JSON.stringify(RATIO)};var s=null;
function draw(){document.getElementById('app').innerHTML=R(s,Date.now(),ratio);}
function pull(){fetch('data.json',{cache:'no-cache'}).then(function(r){return r.ok?r.json():null}).then(function(j){if(j){s=j;draw();}}).catch(function(){});}
try{s=JSON.parse(document.getElementById('d').textContent)}catch(e){}
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
