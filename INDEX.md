# tokencheck · project index

Status as of **2026-11-10 10:22 ICT** (Asia/Bangkok). Read from the running daemon, logs and the repo on disk.

tokencheck shows usage pace for Grok, Claude (including Fable), Codex and AGY. It uses zero inference tokens and runs on one box only.

## Areas

| Area | Key files / paths | Status |
|---|---|---|
| **Daemon**: polls every 60 s, recomputes pace every 5 s, serves HTTP on `127.0.0.1:48829` | `pacer_daemon.py` (lifecycle and outage watcher), `daemon.py`, `core.py`, `collectors.py`, `config.py`. Live install `~/.local/lib/pacer-tokencheck`, config `~/.config/pacer-tokencheck/config.toml`, state `~/.local/share/pacer-tokencheck` | Running, PID 3468515 since 2026-11-10 08:32 ICT (restarted with the same env to load the edge pusher). `/healthz` ready and live with 0 missed ticks and 0 persist errors. No open outage incident |
| **Provider: Grok** | `providers/grok.py`, via the dedicated `pacer-grokusage` tmux usage modal | ok |
| **Provider: Claude** (ALL WEEK, 5H when real) | `providers/claude.py`, `claude_oauth_cache.py` (shared OAuth gate, at least 600 s apart), `claude_capture.py` and the statusline hook. Isolated login at `<state>/claude-usage-config` | ok (`claude_merged`) |
| **Provider: Fable** (FABLE WEEK, scoped Claude limit) | Mapped from the `weekly_scoped` OAuth limits in `providers/claude.py` | ok |
| **Provider: Codex** | `providers/codex.py`: app-server, then tokscale, then session logs | ok (`codex_app_server`) |
| **Provider: AGY** | `providers/agy.py` | ok |
| **Claude token auto-renewal** | `claude_token_refresh.py` and `test_token_refresh.py`. Status in `<state>/claude-token-refresh.json` and `/v1/collector` → `providers.claude.token_refresh`. Dry check: `python3 claude_token_refresh.py` | Working. Verified real renewals at 22:16 ICT on 2026-11-09 and 06:12 ICT on 2026-11-10 (`last_result=refreshed`, 0 failures). Current token expires 14:12 ICT; next renewal around 13:12 ICT |
| **MCP**: 5 tools (`tokencheck_minimal`, `tokencheck_chart`, `tokencheck_png`, `tokencheck_snapshot`, `tokencheck_methods`) | `mcp_server.py`, `run-mcp.sh`. The working connector is `user-tokencheck`; the old `user-pacer-tokencheck` registration is stuck on the host side and is not used | ok |
| **PNG renderer**: one 1179×2556 iPhone screen in the order Grok, Claude, Codex, Fable, AGY | `plot_png.py`, `plot_matplotlib.py`, `fonts/`, `assets/logos/` | ok |
| **Edge dashboard** (optional, off by default): unlisted one-screen page on Cloudflare Workers, readable from anywhere | `edge_push.py` (`EdgePusher`: pushes the snapshot after each collector write, at least 55–60 s apart, 10 s timeout, backoff up to 900 s; status in `/v1/collector` → `edge_push`), `test_edge_push.py`, config keys `edge_push_enabled`, `edge_push_url`, `edge_push_secret_file`. Worker in `edge/`: `worker.template.js` (SQLite Durable Object `SnapshotStore`, `POST /ingest` with a bearer secret, page at `/<read-key>/`, `data.json`), `build.py` (inlines the logos into `edge/dist/worker.js`), `test_worker.mjs`, `wrangler.toml.example`. Deployed URL, read key and account stay out of the repo | Live since 2026-11-10 08:30 ICT. 110 pushes OK, last HTTP 200, 0 failures (10:21 ICT). Page about 14.9 KB brotli. Fit-to-screen layout deployed 09:31 ICT: one screen with no scrolling on phone portrait and landscape, iPad and desktop |
| **Dashboard and CLI** | `dashboard.py`, `dashboard.html`, `tokencheck.py` (six-column TSV plus JSON). Private dashboard at `http://<tailnet-host>:48829/dashboard` (tailnet only, no Funnel) | ok |
| **Routines** (Grok Bot) | Outage wake webhook `tokencheck-outage-wake`; daily limits audit around 09:00 ICT; hourly health check paused (as of 2026-11-08); the one-off first-renewal check (2026-11-09 22:30 ICT) is done and removed | Webhook configured; last post 2026-11-09 14:19 ICT returned HTTP 200. The one-off renewal check passed and was deleted |
| **Packaging and sharing** | `install.sh`, `install.py`, `config.example.toml`, `README.md`, `LICENSE` (MIT), `docs/assets/cover-*.png`, `test-offline.sh` (plus `node edge/test_worker.mjs` after `python edge/build.py`) | Public repo `kemkem23/tokencheck`. Offline tests: 134 repo, 130 live install, 127 mirror, plus the Worker smoke test |
| **Project map** (bilingual, separate from the repo) | Built from `/workspace/tokencheck-project-map`; tailnet only | Map version 2026-11-10.1 (45 nodes, 48 edges); served on the tailnet only |

## Decisions

- **2026-11-07:** Run on the Grok Bot box only. No kemcortex, SSH or remote collection.
- **2026-11-08:** Claude OAuth reads use only the isolated subscription profile. They never fall back to the default login, the setup-token or env tokens.
- **2026-11-08:** The PNG fits one iPhone screen, with dominant charts, small text, logos after names, Fable headers, and no empty Claude 5H panel.
- **2026-11-08:** The MCP connector is renamed `user-tokencheck`. Don't use `user-pacer-tokencheck`.
- **2026-11-08:** Clean release history under MIT. The repo went private at `892205b`, then public after the final sol ultra review at `a3defee` (webhook transport and usage-reader hardening; 113 tests).
- **2026-11-09:** Auto-renew the isolated Claude access token (about 8 h life) with `claude auth status` and an allowlisted environment, starting 60 minutes before expiry. Attempts are at least 600 s apart with 10/20/40/60 min backoff. The OAuth gate is not touched (`2adc4c3`, 125 offline tests).
- **2026-11-10:** Add an optional Cloudflare edge dashboard ("ส่งขึ้น cloudflare เลยโอเคนะ"). It uses a SQLite Durable Object because the KV free plan's 1000 writes a day can't take per-minute pushes. The page is unlisted (secret read key in the path, noindex, no-referrer, strict CSP, no external requests) and has its own ingest secret (`e32d54c`).
- **2026-11-10:** The edge page fits one screen on any device. Columns follow the aspect ratio (2 portrait, 3 landscape/desktop, 5 wide), rows share the visible height (`100dvh`, safe areas, `visualViewport`) and charts fill each card. Not-started AGY windows show as chips. As a last resort it scales down instead of scrolling (`dfaeec2`).

## Open next steps

- [ ] Remove the temporary MCP lifecycle diagnostic logging from the live install (the export already has it removed).
- [ ] Uninstall the old `user-pacer-tokencheck` connector, **only if kem asks** in his own typed message.
- [ ] kem uploads `docs/assets/cover-social.png` as the GitHub social preview in repo Settings; the API can't set it.
- [ ] Bot share template (export-bot-template): not started.
- [ ] Check routine prompts that still reference the old `user-pacer-tokencheck` namespace.
- [ ] Still unverified: Python 3.11 runtime and the systemd user service install path.
- [ ] Check the edge page on kem's real iPhone (Safari toolbars, notch); so far only headless Chrome at 9 sizes has been checked.
- [ ] Edge dashboard: rotate the read key or ingest secret only if kem asks; the Worker and secret files live outside the repo.
- [ ] The isolated Claude login itself expires around 2026-12-06 and then needs a manual re-login.
