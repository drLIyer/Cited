# Launch posts (drafts)

Post these yourself after the directory listing is live (or at least after the
deploy is tested). Replace every `{…}`. Use a **real** screenshot or recording
— the demo GIF in this repo uses a fictional newsletter.

Suggested order: directory approved → creator outreach (week 1) → Show HN +
Reddit (same day) → Product Hunt (a Tuesday–Thursday) → X/LinkedIn threads
alongside each.

---

## Show HN

**Title:** Show HN: Cited – Ask any newsletter or podcast in Claude, with quotes and a confidence score

**Text:**

> Hi HN — I built Cited, an MCP server that lets Claude search a specific newsletter's or podcast's own posts and answer with exact quotes and links.
>
> Why: when I asked Claude what a newsletter had said about something, it either summarised from web search snippets (mostly recent, popular posts) or from memory. I wanted answers grounded in the archive itself, and an honest signal when the archive doesn't cover the question.
>
> How it works: it fetches up to the 200 most recent public posts (Substack API, Medium RSS, or any RSS/podcast feed), chunks them, and runs hybrid BM25 + embedding search (all-MiniLM-L6-v2) with a cross-encoder re-ranker. Each answer gets a High/Moderate/Limited coverage rating from the similarity scores; on Limited, Claude is told to say so and flag anything from general knowledge.
>
> Some things I spent more time on than expected: SSRF protection (it fetches user-supplied URLs, so it pins DNS at connect time and is HTTPS-only), defusing XML from untrusted feeds, and a small self-healing loop that pauses failing feeds and remembers what fixed past errors.
>
> Free, no sign-up, MIT-licensed. Add `https://cited-7bys.onrender.com/mcp` as a custom connector in claude.ai, or install the Claude Code plugin. Code: https://github.com/drLIyer/Cited
>
> Limits: public posts only (no paywalled content), and many RSS feeds only list recent items. Feedback very welcome — especially on the confidence thresholds.

---

## Product Hunt

**Name:** Cited
**Tagline:** Ask any newsletter or podcast in Claude — answers you can check
**Topics:** Artificial Intelligence, Productivity, Newsletters

**Description:**
> Cited lets Claude search a newsletter's or podcast's own posts and answer with exact quotes, links, and an honest High/Moderate/Limited confidence rating. Works with Substack, Medium, and any RSS or podcast feed. Free, no sign-up.

**Maker's first comment:**
> Hey Product Hunt 👋 I read a lot of newsletters and kept wanting to ask "what has {writer} said about X?" — and get the actual posts back, not a summary from memory. Cited does that inside Claude: it searches the source's archive, quotes it with links, and tells you plainly when the source doesn't cover your question. Try it with a newsletter you love and tell me what it gets right or wrong!

**Gallery:** icon, real conversation screenshot (High confidence), real Limited-coverage screenshot, the install steps.

---

## Reddit — r/ClaudeAI

**Title:** I made a free connector that lets Claude answer from a specific newsletter's archive (with quotes + a confidence score)

> Add `https://cited-7bys.onrender.com/mcp` as a custom connector (Customize → Connectors → Add custom connector, no sign-in), then ask e.g. *"What has {newsletter} said about {topic}? Quote the posts."*
>
> It searches the newsletter's own posts (Substack, Medium, RSS, podcast feeds), returns the passages with links, and rates coverage High/Moderate/Limited — if the newsletter barely covers your question, Claude says so instead of bluffing.
>
> Open source (MIT): https://github.com/drLIyer/Cited. Would love feedback on which newsletters/podcasts work well or badly.

*(Check the subreddit's self-promotion rules first; post once, reply to every comment.)*

---

## X / Twitter thread

1/ I built Cited: ask any newsletter or podcast a question in Claude, and get answers quoted from the source — with links and an honest confidence score. Free. 🧵 {GIF/video}

2/ The problem: ask Claude what a newsletter said about X and you get a summary of whatever web search found — usually recent or popular posts. Older gems are invisible, and you can't tell what's grounded.

3/ Cited searches the newsletter's own archive (up to 200 posts), by meaning, and hands Claude the exact passages with title, date and link.

4/ Every answer is rated High / Moderate / Limited coverage. Limited = Claude tells you the source barely covers it and flags anything from general knowledge.

5/ Works with Substack (incl. custom domains), Medium, any RSS feed — and podcasts via show notes.

6/ Add it in a minute: claude.ai → Customize → Connectors → Add custom connector → https://cited-7bys.onrender.com/mcp. Open source: github.com/drLIyer/Cited

---

## LinkedIn

> I shipped a small side project: **Cited**, a free connector that lets Claude answer questions from a specific newsletter's or podcast's archive — with exact quotes, links, and an honest confidence rating.
>
> I read a lot of product newsletters, and "what has {writer} said about pricing?" usually got me a vague summary. Cited searches the source itself and shows its work. When the newsletter doesn't cover a topic, it says so — which turned out to be the feature I value most.
>
> Try it: https://cited-7bys.onrender.com — I'd love to hear which newsletters you'd use it with.
