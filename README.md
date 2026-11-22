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
