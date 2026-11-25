<p align="center"><img src=".claude-plugin/icon.png" width="96" alt="Cited"></p>

# Cited

**Ask any newsletter or podcast. Get answers you can check.**

Cited lets Claude search a newsletter's or podcast's own posts and answer with **exact quotes, links to the source**, and an **honest confidence rating** — so you know when an answer is grounded and when it isn't. Works with Substack (including custom domains), Medium, and any RSS or podcast feed.

<p align="center"><img src="docs/demo.gif" width="560" alt="Claude answering with quotes from a newsletter, a High-confidence badge, then a Limited-coverage disclaimer for an off-topic question"><br>
<sub>Illustration with a fictional newsletter. Real answers quote and link real posts.</sub></p>

**Website:** <https://cited-7bys.onrender.com> · **Add to claude.ai:** `https://cited-7bys.onrender.com/mcp`

### Why use it instead of plain Claude?

| | Claude on its own | Claude + Cited |
|---|---|---|
| Finds older posts | Only what web search ranks | Searches the newsletter's own posts (up to 200) |
| Sources | Sometimes, from search snippets | Exact quote, title, date and link for every claim |
| Knows when it doesn't know | Rarely says | High / Moderate / Limited confidence + a disclaimer when coverage is limited |
| Podcasts | Hit or miss | Searches show notes via the RSS feed |

Under the hood: hybrid BM25 + vector search with cross-encoder reranking over the source's public posts.

## Install

**claude.ai (web, desktop, mobile):** Customize → Connectors → **+ Add** → **Add custom connector** → name it *Cited*, URL `https://cited-7bys.onrender.com/mcp`, **No sign in** → **Add**. Then switch it on from the **+** menu in a chat.

**Claude Code (plugin):**

```
/plugin install cited --marketplace drLIyer/Cited
```

or from a shell:

```bash
claude plugin marketplace add drLIyer/Cited
claude plugin install cited@cited-plugins
```


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
| Newsletter library | Newsletters in `library.json` get a permanent full-archive index (free posts) in `library.db` next to the history DB; refreshed incrementally every 60 min (`CITED_LIBRARY_REFRESH_MINUTES`) |

| Auto-fix agent | On by default with an admin token; `CITED_AUTOHEAL=0` turns it off |
| Daily digest | `CITED_DIGEST_TO` + `RESEND_API_KEY` & `CITED_DIGEST_FROM` (or `SMTP_HOST`/`SMTP_USER`/`SMTP_PASSWORD`); `CITED_DIGEST_HOUR_UTC` (default 13) |

The server runs a background health check every 60 s (`CITED_HEALTH_MONITOR_SECONDS`) and logs every warning, error, crash, and blocked threat for the dashboard.

**Newsletter library.** For the newsletters listed in `library.json`, Cited stores every free post's passages and embeddings permanently. Searches on them use the stored index — fast from the first question, complete beyond the 200-post limit, and unaffected by restarts. Each refresh fetches newest-first and stops at the first page with nothing new, so only new or edited posts are embedded; a weekly full rescan drops deleted or newly-paywalled posts. **Respecting writers' AI opt-outs.** Before indexing or fetching any site, Cited checks whether the writer has opted out of AI: robots.txt blocks on AI crawlers (or on Cited, or everyone), `noai` meta tags / `X-Robots-Tag` headers, Cloudflare Content Signals, and the TDM Reservation Protocol. Opted-out sites aren't fetched or indexed, and anything already stored is deleted. Default policy `strict` treats blocks on AI *training* crawlers as an opt-out too; `CITED_OPTOUT_POLICY=retrieval` honours only blocks on AI assistant/answer crawlers. A writer who gives you permission directly can be included with `"permission": "granted"` in `library.json`.

**Ask across the library.** With the library loaded, users don't need a URL: *"How do teams run product discovery?"* searches every library newsletter at once, and *"What does Marty Cagan say about empowered teams?"* routes to SVPG automatically (writers and publication names are matched via `aliases` in `library.json`). Answers show which newsletters cover the topic best.

Non-Substack sites work too: Cited finds the site's RSS/Atom feed automatically and, for WordPress-style feeds, pages back through `?paged=2, 3, …` to collect the full archive. Add a newsletter by adding its URL to `library.json` and redeploying.

**Auto-fix agent.** After each health check, the agent looks at new errors and applies *safe* fixes only — reload the search models, clear one feed's cache, pause a failing feed for 15 min, free memory, or block an attacking IP for 60 min. It never changes code. It checks whether each fix worked, remembers the ones that did, and re-applies them immediately if the same error returns. Every action has an hourly cap and appears on the dashboard; a fix that didn't work is flagged for you.

## Privacy

The hosted service logs the newsletter URL and question for each search (deleted after 90 days, never sold). Local installs keep history only on your machine. See [PRIVACY.md](PRIVACY.md).

## License

MIT. See [LICENSE](LICENSE).
