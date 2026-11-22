# Cited
Precise, sourced answers from any Substack newsletter, Medium publication, or RSS/podcast feed — an MCP server for Claude.

Cited searches a source's public posts (hybrid BM25 + vector search, cross-encoder reranking) and returns ranked excerpts with titles, dates, and links so Claude can cite them.

## Install

**Claude Code (plugin):**

```
/plugin install cited --marketplace drLIyer/Cited
```

or from a shell:

```bash
claude plugin marketplace add drLIyer/Cited
claude plugin install cited@cited-plugins
```

**claude.ai (custom connector):** Customize → Connectors → **+ Add** → **Add custom connector**, URL `https://cited-7bys.onrender.com/mcp`, no sign-in.

## Coverage

Substack: up to the 200 most recent public posts. Medium and RSS/podcast feeds: whatever the feed publishes (often the latest 10–50 items). Each post is indexed up to its first 15,000 characters. Paywalled content is not included. Only `https://` sources are fetched.

## Running the hosted server

| | |
|---|---|
| Admin dashboard | `https://<host>/admin` (sign in with `CITED_ADMIN_TOKEN`) |
| Health | `/health/live` (process up) · `/health/ready` (can serve searches; use for platform health checks) · `/health` (full report) |
| Rate limit | `CITED_RATE_LIMIT_PER_MIN` (default 15 searches/min per IP) |
| Confidence limits | ThinkWithMe defaults: High = best ≥ 0.62 & avg ≥ 0.59, Moderate = best ≥ 0.55 (`CITED_CONF_*`) |
| Persistent data | Set `CITED_HISTORY_DB` to a path on a persistent disk, or history and the error log reset on every deploy |

| Auto-fix agent | On by default with an admin token; `CITED_AUTOHEAL=0` turns it off |
| Daily digest | `CITED_DIGEST_TO` + `RESEND_API_KEY` & `CITED_DIGEST_FROM` (or `SMTP_HOST`/`SMTP_USER`/`SMTP_PASSWORD`); `CITED_DIGEST_HOUR_UTC` (default 13) |

The server runs a background health check every 60 s (`CITED_HEALTH_MONITOR_SECONDS`) and logs every warning, error, crash, and blocked threat for the dashboard.

**Auto-fix agent.** After each health check, the agent looks at new errors and applies *safe* fixes only — reload the search models, clear one feed's cache, pause a failing feed for 15 min, free memory, or block an attacking IP for 60 min. It never changes code. It checks whether each fix worked, remembers the ones that did, and re-applies them immediately if the same error returns. Every action has an hourly cap and appears on the dashboard; a fix that didn't work is flagged for you.

## Privacy

The hosted service logs the newsletter URL and question for each search (deleted after 90 days, never sold). Local installs keep history only on your machine. See [PRIVACY.md](PRIVACY.md).

## License

MIT. See [LICENSE](LICENSE).
