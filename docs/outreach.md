# Outreach to newsletter and podcast creators

Creators are Cited's best distribution: "Ask my archive in Claude" is a feature
they can offer readers for free, and one mention to 100k subscribers beats any
amount of posting.

**Before you send anything:** deploy the latest version, try Cited on that
creator's newsletter yourself, and pick 2–3 real answers it gave (with the post
links). Personal, specific emails get replies; templates don't. Never quote
results you haven't checked.

---

## Who to start with

Creators with large public archives (many free posts) get the best results.
Start with newsletters you already read and whose archives you've tested:

- Product & tech: Lenny's Newsletter, Aakash Gupta's Product Growth, and similar
- Podcasts with detailed show notes in their RSS feed
- Writers who already link readers to "start here" / archive pages

Track outreach in a simple sheet: name, newsletter URL, tested? (Y/N), best
example answer link, date sent, reply.

---

## Email / DM template

**Subject:** Your readers can now ask your archive questions in Claude

> Hi {first name},
>
> I've been reading {newsletter} since {when/what you liked}. I built a small free tool called Cited that lets Claude answer questions *from a specific newsletter's archive* — with exact quotes and links back to the post.
>
> I tried it on yours. Asking "{question}" brought back {post title} ({link}) and quoted: "{short real quote}". It also tells readers honestly when your archive *doesn't* cover something, instead of making it up.
>
> If you think it's useful, readers can add it in about a minute — instructions and a demo are at https://cited-7bys.onrender.com. A line in a future issue like *"You can now ask my archive questions in Claude"* would mean a lot, but no pressure either way.
>
> Happy to set up anything that would make it work better for {newsletter}.
>
> {Your name}

Keep it under 150 words; send from your personal address; one follow-up after a week, then stop.

---

## Copy a creator can paste into their newsletter

> **New: ask my archive in Claude.** Using Claude? Add the free *Cited* connector (Customize → Connectors → Add custom connector → `https://cited-7bys.onrender.com/mcp`), then ask things like *"What has {newsletter} said about {topic}? Quote the posts."* You'll get answers quoted from my posts, with links, and a heads-up when I haven't written about something.

## Ready-made prompts to give each creator

- What has {newsletter} said about {their signature topic}? Quote the posts.
- Which {newsletter} posts should I read first if I'm new to {topic}?
- Summarise how {author}'s view on {topic} changed over time, with links.
- (Podcasts) Which episodes of {podcast} discuss {topic}? Use the show's RSS feed.

---

## What to offer in return (optional)

- Pre-warm their newsletter on the server (`CITED_PREWARM_URLS`) so first answers are fast.
- Share anonymous aggregate stats from the admin dashboard (e.g. "readers asked 340 questions about your archive this month; top topics …") — counts only, never individual questions.
