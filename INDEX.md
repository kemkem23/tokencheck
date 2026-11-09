# tokencheck · project index

Status as of **2026-11-09 18:20 ICT** (Asia/Bangkok). Read from the running daemon, logs and the repo on disk.

tokencheck shows usage pace for Grok, Claude (including Fable), Codex and AGY. It uses zero inference tokens and runs on one box only.

## Areas

| Area | Key files / paths | Status |
|---|---|---|
| **Daemon**: polls every 60 s, recomputes pace every 5 s, serves HTTP on `127.0.0.1:48829` | `pacer_daemon.py` (lifecycle and outage watcher), `daemon.py`, `core.py`, `collectors.py`, `config.py`. Live install `~/.local/lib/pacer-tokencheck`, config `~/.config/pacer-tokencheck/config.toml`, state `~/.local/share/pacer-tokencheck` | Running, PID 2422562 since 2026-11-09 15:41 ICT. `/healthz` ready and live with 0 tick errors. No open outage incident |
| **Provider: Grok** | `providers/grok.py`, via the dedicated `pacer-grokusage` tmux usage modal | ok |
| **Provider: Claude** (ALL WEEK, 5H when real) | `providers/claude.py`, `claude_oauth_cache.py` (shared OAuth gate, at least 600 s apart), `claude_capture.py` and the statusline hook. Isolated login at `<state>/claude-usage-config` | ok (`claude_merged`) |
| **Provider: Fable** (FABLE WEEK, scoped Claude limit) | Mapped from the `weekly_scoped` OAuth limits in `providers/claude.py` | ok |
| **Provider: Codex** | `providers/codex.py`: app-server, then tokscale, then session logs | ok (`codex_app_server`) |
| **Provider: AGY** | `providers/agy.py` | ok |
| **Claude token auto-renewal** | `claude_token_refresh.py` and `test_token_refresh.py`. Status in `<state>/claude-token-refresh.json` and `/v1/collector` → `providers.claude.token_refresh`. Dry check: `python3 claude_token_refresh.py` | Live since 2026-11-09 15:41 ICT. The current access token expires 22:19 ICT; the first renewal is planned for 21:19 ICT. No real renewal has happened yet |
| **MCP**: 5 tools (`tokencheck_minimal`, `tokencheck_chart`, `tokencheck_png`, `tokencheck_snapshot`, `tokencheck_methods`) | `mcp_server.py`, `run-mcp.sh`. The working connector is `user-tokencheck`; the old `user-pacer-tokencheck` registration is stuck on the host side and is not used | ok |
| **PNG renderer**: one 1179×2556 iPhone screen in the order Grok, Claude, Codex, Fable, AGY | `plot_png.py`, `plot_matplotlib.py`, `fonts/`, `assets/logos/` | ok |
| **Dashboard and CLI** | `dashboard.py`, `dashboard.html`, `tokencheck.py` (six-column TSV plus JSON). Private dashboard at `http://<tailnet-host>:48829/dashboard` (tailnet only, no Funnel) | ok |
| **Routines** (Grok Bot) | Outage wake webhook `tokencheck-outage-wake`; daily limits audit around 09:00 ICT; hourly health check paused (as of 2026-11-08); a one-off check of the first auto-renewal at 22:30 ICT on 2026-11-09 | Webhook configured; last post 2026-11-09 14:19 ICT returned HTTP 200 |
| **Packaging and sharing** | `install.sh`, `install.py`, `config.example.toml`, `README.md`, `LICENSE` (MIT), `docs/assets/cover-*.png`, `test-offline.sh` | Public repo `kemkem23/tokencheck` |
| **Project map** (bilingual, separate from the repo) | Built from `/workspace/tokencheck-project-map`; tailnet only | Map version 2026-11-09.1 (42 nodes, 44 edges); served on the tailnet only |

## Decisions

- **2026-11-07:** Run on the Grok Bot box only. No kemcortex, SSH or remote collection.
- **2026-11-08:** Claude OAuth reads use only the isolated subscription profile. They never fall back to the default login, the setup-token or env tokens.
- **2026-11-08:** The PNG fits one iPhone screen, with dominant charts, small text, logos after names, Fable headers, and no empty Claude 5H panel.
- **2026-11-08:** The MCP connector is renamed `user-tokencheck`. Don't use `user-pacer-tokencheck`.
- **2026-11-08:** Clean release history under MIT. The repo went private at `892205b`, then public after the final sol ultra review at `a3defee` (webhook transport and usage-reader hardening; 113 tests).
- **2026-11-09:** Auto-renew the isolated Claude access token (about 8 h life) with `claude auth status` and an allowlisted environment, starting 60 minutes before expiry. Attempts are at least 600 s apart with 10/20/40/60 min backoff. The OAuth gate is not touched (`2adc4c3`, 125 offline tests).

## Open next steps

- [ ] Check the first real auto-renewal: due around 21:19 ICT on 2026-11-09, with the routine check at 22:30 ICT. Expect `token_refresh.last_result=refreshed`.
- [ ] Remove the temporary MCP lifecycle diagnostic logging from the live install (the export already has it removed).
- [ ] Uninstall the old `user-pacer-tokencheck` connector, **only if kem asks** in his own typed message.
- [ ] kem uploads `docs/assets/cover-social.png` as the GitHub social preview in repo Settings; the API can't set it.
- [ ] Bot share template (export-bot-template): not started.
- [ ] Check routine prompts that still reference the old `user-pacer-tokencheck` namespace.
- [ ] Still unverified: Python 3.11 runtime and the systemd user service install path.
- [ ] The isolated Claude login itself expires around 2026-12-06 and then needs a manual re-login.
