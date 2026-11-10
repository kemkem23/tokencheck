// Offline smoke test of edge/dist/worker.js routing, auth and rendering (Node 20+). No network.
import fs from 'node:fs';
import assert from 'node:assert/strict';
const here = new URL('.', import.meta.url).pathname;
const src = fs.readFileSync(here + 'dist/worker.js', 'utf8')
  .replace("import { DurableObject } from 'cloudflare:workers';", 'class DurableObject { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }');
const tmp = fs.mkdtempSync('/tmp/tokencheck-edge-') + '/worker.mjs';
fs.writeFileSync(tmp, src);
crypto.subtle.timingSafeEqual ??= (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
const mod = await import(tmp);
const storage = new Map();
const obj = new mod.SnapshotStore({ storage: { put: async (k, v) => storage.set(k, v), get: async (k) => storage.get(k) } }, {});
const env = { READ_KEY: 'read-test', INGEST_SECRET: 'ingest-test', STORE: { idFromName: () => 'x', get: () => obj } };
const r = (u, init) => mod.default.fetch(new Request('https://w.example' + u, init), env);
const snap = JSON.stringify({ v: 1, t: new Date().toISOString(), panels: [
  { p: 'grok', l: 'WEEK', hd: ['GROK', 'WEEK'], u: 3, pc: 71, r: new Date(Date.now() + 86400e3).toISOString(), st: true, age: 20, hu: [1, 2, 3], hp: [1, 2, 3] },
  { p: 'agy', l: 'GEM 5H', hd: ['AGY', 'GEM 5H'], u: 0, pc: null, st: false, age: 20, hu: [], hp: [] }] });
const auth = { Authorization: 'Bearer ingest-test' };
assert.equal((await r('/ingest', { method: 'POST', body: snap })).status, 404);
assert.equal((await r('/ingest', { method: 'POST', body: snap, headers: { Authorization: 'Bearer wrong' } })).status, 404);
assert.equal((await r('/ingest')).status, 404);
assert.equal((await r('/ingest', { method: 'POST', body: '{', headers: auth })).status, 400);
assert.equal((await r('/ingest', { method: 'POST', body: snap, headers: auth })).status, 200);
for (const p of ['/', '/read-wrong/', '/read-test/x', '/robots.txt']) assert.equal((await r(p)).status, 404, p);
assert.equal((await r('/read-test/', { method: 'POST' })).status, 404);
const page = await r('/read-test/');
assert.equal(page.status, 200);
assert.match(page.headers.get('x-robots-tag'), /noindex/);
const html = await page.text();
assert.match(html, /GROK/); assert.match(html, /3%/); assert.match(html, /ยังไม่เริ่ม/);
assert.doesNotMatch(html, /https?:\/\/(?!www\.w3\.org)/);   // no external requests
// one-screen layout: real viewport, safe areas, aspect-ratio columns, fit script, AGY chips
assert.match(html, /viewport-fit=cover/); assert.match(html, /100dvh/); assert.match(html, /safe-area-inset-bottom/);
assert.match(html, /min-aspect-ratio:5\/4/); assert.match(html, /min-aspect-ratio:2\/1/);
assert.match(html, /visualViewport/); assert.match(html, /orientationchange/); assert.match(html, /class="chipcell"/); assert.match(html, /class="chip"/);
assert.match(html, /<main class="[^"]*r2/);   // 5 active panels in 2 columns leave a free cell for the chips
assert.doesNotMatch(html, /aspect-ratio:1\.9/);
assert.equal((await r('/read-test/', { headers: { 'If-None-Match': page.headers.get('etag') } })).status, 304);
assert.equal(await (await r('/read-test/data.json')).text(), snap);
console.log('edge worker smoke test OK');
