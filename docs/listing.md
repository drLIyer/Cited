# Cited — directory listing kit

Copy-paste text for Anthropic's directory and community MCP directories.
Everything here matches what the code actually does; keep it that way when
editing (Anthropic's review checks descriptions against behaviour).

---

## Basics

| Field | Value |
|---|---|
| Name | Cited |
| Tagline (≤ 80 chars) | Ask any newsletter or podcast. Get answers quoted from the source. |
| Category | Research · Knowledge · Reading |
| Server URL (remote MCP) | `https://cited-7bys.onrender.com/mcp` |
| Transport | Streamable HTTP (stateless) |
| Authentication | None (public data only) |
| Website | https://cited-7bys.onrender.com |
| Source code | https://github.com/drLIyer/Cited |
| License | MIT |
| Privacy policy | https://github.com/drLIyer/Cited/blob/main/PRIVACY.md |
| Support | https://github.com/drLIyer/Cited/issues · *(add a support email)* |
| Icon | `.claude-plugin/icon.png` (1024 × 1024 PNG) |
| Plugin repo (Claude Code) | `drLIyer/Cited` — marketplace `cited-plugins`, plugin `cited` |

## Short description (≈ 160 chars)

Search a newsletter's or podcast's own posts and get answers with exact quotes, links, and an honest High / Moderate / Limited confidence rating.

## Long description

Cited lets Claude answer questions from a specific newsletter or podcast — with receipts.

Give Claude a Substack (including custom domains), Medium publication, or any RSS/podcast feed and ask a question. Cited fetches the source's public posts, searches them by meaning (hybrid keyword + semantic search with re-ranking), and returns the most relevant passages with each post's title, date, and link. Claude answers from those passages and cites them.

Every result carries a confidence rating — **High**, **Moderate**, or **Limited coverage** — based on how closely the source matches the question. When coverage is Limited, Claude tells the user plainly that the source barely covers the topic and that the answer draws on general AI knowledge.

- Searches up to the 200 most recent public posts of a Substack; RSS and podcast feeds include whatever the feed lists.
- Works with podcasts via their RSS feed (show notes and episode summaries).
- Read-only: Cited never changes anything. Paywalled posts are not included.
- Free, no account needed. Fair-use limit of 15 searches per minute.

## Tools

| Tool | Title | Annotations | What it does |
|---|---|---|---|
| `search_newsletter` | Search a newsletter or podcast | readOnlyHint, idempotentHint, openWorldHint | Searches a source's public posts for a question; returns ranked excerpts with title, date, link, per-excerpt match %, and a confidence rating (plus a disclaimer when coverage is limited). |
| `list_posts` | List a newsletter's posts | readOnlyHint, idempotentHint, openWorldHint | Lists the public posts/episodes Cited can see, with dates and links. |
| `search_library` | Search the Cited library | readOnlyHint, idempotentHint, openWorldHint | Searches Cited's pre-indexed library of product newsletters (full archives of free posts) at once — routed by the writers named in the question, or across all of them — and reports which newsletters cover the topic best. |
| `list_library` | List the Cited library | readOnlyHint, idempotentHint | Lists the newsletters in the library with URLs, authors and indexed post counts. |

## Example prompts

1. What has Lenny's Newsletter said about pricing a B2B SaaS product? Quote the posts.
2. Search https://news.aakashg.com for advice on PM interviews and give me the top 3 ideas with links.
3. Find the podcast RSS feed for Acquired, then tell me which episodes discuss Costco.
4. List the posts from https://stratechery.com/feed and summarise the themes from the last month.
5. How do the best product leaders run discovery? Compare what Marty Cagan and Teresa Torres say.
6. Which product newsletters have written the most about pricing, and what do they recommend?

## Use cases (for the directory's "Use cases" step)

- **Research a writer's thinking** — "What does this newsletter recommend for X?" with quotes and links.
- **Catch up on a podcast** — find which episodes covered a topic from the show notes.
- **Check a claim** — confirm whether a newsletter actually said something, and where.
- **Know the limits** — the confidence rating shows when a source doesn't cover a topic.

## Data handling (for the "Data handling" step)

- Receives: the newsletter/feed URL and the search question. Never the rest of the conversation.
- Stores: if the operator enables logging, the URL, question, top excerpt, and timestamp for up to 90 days; an error log (no questions, no IPs) for up to 30 days.
- IP addresses: never stored; held in memory only to temporarily block abusive clients.
- No data is sold or shared. Outbound requests go only to the newsletter/feed host.
- Writers' AI opt-outs are respected: sites that block AI crawlers in robots.txt, use `noai` tags, or reserve text & data mining rights are never fetched or indexed.

## Screenshots to upload

- `docs/demo.gif` (illustration — replace with a real recording once deployed; see below)
- A real claude.ai conversation showing a cited answer with the confidence line
- A real Limited-coverage answer showing the disclaimer

**Recording a real demo:** with Cited added in claude.ai, ask one of the example prompts, then a clearly off-topic follow-up. Record with your OS screen recorder (macOS: ⇧⌘5) and convert to GIF, e.g.
`ffmpeg -i demo.mov -vf "fps=10,scale=720:-1:flags=lanczos" demo.gif`.

---

# Where to submit

### 1. Anthropic directory (highest impact)
- Portal: https://claude.ai/directory/manage → **Submit new**
  - **MCP connector** → reaches claude.ai users. Use the "Basics", descriptions, tools, use cases and data handling above.
  - **Plugin bundle** → reaches Claude Code users. Repo `drLIyer/Cited` (must be public; `main` must contain the plugin files).
- Needs a paid Claude plan. Before submitting, run:
  `claude plugin validate --strict --profile directory .`
- Review checklist (already done in code): tool titles + read-only annotations ✓, accurate descriptions ✓, no instructions telling Claude to prefer Cited over other tools ✓, privacy policy ✓, icon ✓, HTTPS on port 443 ✓, clear error messages ✓.
- Still to do by you: add a support email to `PRIVACY.md` and this listing; deploy the PR; test as a custom connector first.

### 2. Community MCP directories
Submission processes change — check each site's "submit" page. Use the short/long descriptions above.

| Directory | How it usually works |
|---|---|
| Official MCP Registry (registry.modelcontextprotocol.io) | Publish metadata with their CLI; check their docs for the current `server.json` format |
| awesome-mcp-servers (GitHub, punkpeye/awesome-mcp-servers) | Pull request adding one line under the right category |
| Smithery (smithery.ai) | Add a server from a GitHub repo or a remote URL |
| PulseMCP (pulsemcp.com) | Submission form |
| mcp.so | Submission form / GitHub issue |
| Glama (glama.ai/mcp) | Indexes public GitHub repos; claim the listing |

One-line entry for awesome lists:
`- [Cited](https://github.com/drLIyer/Cited) 🐍 ☁️ - Search any Substack, Medium, RSS or podcast feed and get answers with exact quotes, links, and a confidence rating.`
