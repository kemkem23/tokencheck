# tokencheck edge dashboard (optional)

This is a Cloudflare Worker that serves one unlisted, self-contained dashboard page. It is rendered at the edge, so the first paint already has the numbers and no VPN is needed. The tokencheck daemon pushes a compact snapshot to it after each poll (`edge_push.py`).

- `GET /<READ_KEY>/` returns the HTML page. CSS, JS and the provider logo sprite are inline, and the page makes no external requests. It refreshes every 60 s from `data.json`.
- `GET /<READ_KEY>/data.json` returns the latest snapshot.
- `POST /ingest` with `Authorization: Bearer <INGEST_SECRET>` stores a snapshot (at most 256 KB, `v: 1`).
- Every other path or method returns `404`. Responses carry `X-Robots-Tag: noindex`, an ETag, and `Cache-Control: private, no-cache`, so revalidation is a fast `304`.
- Storage is one SQLite-backed Durable Object (Workers Free plan). Workers KV's free tier allows 1,000 writes per day, which can't hold per-minute pushes.

The snapshot holds only percentages, pace, reset times, observation ages and a 24 h history in 10-minute buckets. It has no tokens, emails, account IDs or paths. Anyone with the read-key URL can see these usage numbers; keep the link private.

## Build and deploy

```sh
python3 edge/build.py            # writes edge/dist/worker.js (inlines assets/logos via plot_png)
node edge/test_worker.mjs        # offline routing/auth smoke test (Node 20+)
umask 077
python3 -c "import secrets;print(secrets.token_urlsafe(32))" > ~/.secrets/tokencheck-edge-read-key
python3 -c "import secrets;print(secrets.token_urlsafe(32))" > ~/.secrets/tokencheck-edge-ingest-secret
cp edge/wrangler.toml.example edge/wrangler.toml   # set <WORKER_NAME>
npx wrangler deploy -c edge/wrangler.toml
npx wrangler secret put READ_KEY -c edge/wrangler.toml       < ~/.secrets/tokencheck-edge-read-key
npx wrangler secret put INGEST_SECRET -c edge/wrangler.toml  < ~/.secrets/tokencheck-edge-ingest-secret
```

Then enable the push in `config.toml` and restart the daemon:

```toml
edge_push_enabled = true
edge_push_url = "https://<WORKER_NAME>.<SUBDOMAIN>.workers.dev/ingest"
edge_push_secret_file = "~/.secrets/tokencheck-edge-ingest-secret"
```

Open `https://<WORKER_NAME>.<SUBDOMAIN>.workers.dev/<READ_KEY>/`. Push status shows under `edge_push` in `/v1/collector`.
