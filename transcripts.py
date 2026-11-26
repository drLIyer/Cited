"""
Podcast transcript repositories (e.g. github.com/aakashg/growth-podcast-transcripts).

Expected layout (the Growth Podcast format):
  index.json                one entry per episode: title, date, guests, youtube_url,
                            substack_url, duration, word_count, file, …
  episodes/<date>-<slug>.md YAML front matter, then turns:
                                **Speaker Name** (HH:MM:SS)
                                <blank line>
                                what they said …
                            A line `<!-- sponsor -->` directly above a turn marks it as
                            non-conversation (ad reads, promos, hand-backs) — skipped.

Passages are built from consecutive speaker turns (~250 words), labelled with
who spoke, and link to the exact moment on YouTube (…&t=<seconds>s) so every
quote can be checked — the licence of the Growth Podcast transcripts asks for
exactly that, plus naming the show and linking its site (the `attribution`
field in library.json).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

import fetcher as _fetcher
import pipeline as _pipeline

TURN_RE    = re.compile(r"^\*\*(?P<speaker>[^*]+)\*\*\s*\((?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\)\s*$")
SPONSOR    = "<!-- sponsor -->"
CHUNK_WORDS = 250
UNSEPARATED = "speakers (not separated)"


def raw_base(repo_url: str, branch: str = "main") -> str:
    """https://github.com/owner/repo → https://raw.githubusercontent.com/owner/repo/main/"""
    parts = urlsplit(repo_url)
    bits = [b for b in parts.path.split("/") if b]
    if parts.hostname not in ("github.com", "www.github.com") or len(bits) < 2:
        raise _fetcher.NotSubstackError(f"Not a GitHub repository URL: {repo_url}")
    return f"https://raw.githubusercontent.com/{bits[0]}/{bits[1]}/{branch}/"


def entry_hash(entry: dict) -> str:
    """Fingerprint of an index entry — changes when the episode is added or edited."""
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:24]


def entry_url(entry: dict) -> str:
    return entry.get("substack_url") or entry.get("youtube_url") or entry["file"]


async def fetch_index(repo_url: str) -> tuple[str, list[dict]]:
    base = raw_base(repo_url)
    async with _fetcher._client() as client:
        try:
            resp = await _fetcher._get(client, base + "index.json")
        except httpx.HTTPStatusError as exc:
            raise _fetcher.FetchError(f"Transcript index returned {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise _fetcher.FetchError(f"Network error fetching transcript index: {exc}") from exc
    try:
        entries = resp.json()
    except ValueError as exc:
        raise _fetcher.FetchError("Transcript index isn't valid JSON") from exc
    if not isinstance(entries, list):
        raise _fetcher.FetchError("Transcript index should be a list of episodes")
    return base, [e for e in entries if isinstance(e, dict) and e.get("file")]


async def fetch_episodes(base: str, entries: list[dict], concurrency: int = 4) -> list[_fetcher.Post]:
    """Download episode files and turn them into Posts (text = transcript body)."""
    sem = asyncio.Semaphore(concurrency)

    async def one(entry: dict) -> _fetcher.Post:
        async with sem, _fetcher._client() as client:
            try:
                resp = await _fetcher._get(client, base + entry["file"].lstrip("/"))
            except httpx.HTTPError as exc:
                raise _fetcher.FetchError(f"Couldn't fetch {entry['file']}: {exc}") from exc
        body = resp.content.decode("utf-8", "replace")
        if body.startswith("---"):
            end = body.find("\n---", 3)
            body = body[end + 4:] if end != -1 else body
        guests = entry.get("guests") or []
        if isinstance(guests, str):
            guests = [g.strip() for g in guests.split(",") if g.strip()]
        return _fetcher.Post(
            title=entry.get("title") or entry["file"], url=entry_url(entry),
            date=str(entry.get("date") or "")[:10], text=body, audience="everyone",
            meta={"hash": entry_hash(entry), "youtube_url": entry.get("youtube_url") or "",
                  "guests": guests, "kind": "episode"})

    return list(await asyncio.gather(*(one(e) for e in entries)))


def _seconds(ts: str) -> int:
    parts = [int(p) for p in ts.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def timestamp_link(youtube_url: str, seconds: int) -> str:
    if not youtube_url:
        return ""
    parts = urlsplit(youtube_url)
    q = {k: v for k, v in parse_qs(parts.query).items() if k != "t"}
    q["t"] = [f"{seconds}s"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q, doseq=True), ""))


_AD_START = re.compile(r"\b(?:this|today'?s|today’s)\s+(?:episode|show|podcast)\s+is\s+(?:also\s+)?"
                       r"(?:brought to you|sponsored|presented)\s+by\b", re.I)
_AD_CTA   = re.compile(r"dot\s+(?:com|co|io|so|ai|app)\b|\.(?:com|co|io|so|ai|app)\b|\bslash\b|% off|"
                       r"percent off|free trial|discount|promo code|\bcode\b", re.I)
_SENT_RE  = re.compile(r"(?<=[.!?])\s+")
AD_MAX_SENTENCES = 12      # an ad read rarely runs longer; bounds how much we can remove


def scrub_ads(text: str) -> str:
    """Remove unmarked ad reads inside a turn: from "This episode is brought to you by …"
    through the ad's call to action (URL / discount / free trial). If no call to action
    follows, only a short span is removed, so real conversation isn't over-deleted."""
    if not _AD_START.search(text):
        return text
    sents = _SENT_RE.split(text)
    keep, i = [], 0
    while i < len(sents):
        if _AD_START.search(sents[i]):
            window = sents[i:i + AD_MAX_SENTENCES]
            last_cta = max((j for j, s in enumerate(window) if _AD_CTA.search(s)), default=None)
            i += (last_cta + 1) if last_cta is not None else min(3, len(window))
            continue
        keep.append(sents[i])
        i += 1
    return " ".join(keep).strip()


def parse_turns(body: str) -> list[dict]:
    """[{speaker, ts, seconds, text, sponsor}] from a transcript body."""
    turns: list[dict] = []
    marked = False
    cur: Optional[dict] = None
    for line in body.splitlines():
        s = line.strip()
        if s == SPONSOR:
            marked = True
            continue
        m = TURN_RE.match(s)
        if m:
            cur = {"speaker": m.group("speaker").strip(), "ts": m.group("ts"),
                   "seconds": _seconds(m.group("ts")), "text": "", "sponsor": marked}
            turns.append(cur)
            marked = False
            continue
        if cur is not None and s and not s.startswith("#"):
            cur["text"] = (cur["text"] + " " + s).strip()
    for t in turns:
        t["text"] = scrub_ads(t["text"])
    return [t for t in turns if t["text"]]


def chunk_episode(post: _fetcher.Post, words: int = CHUNK_WORDS) -> list[_pipeline.Chunk]:
    """Passages of consecutive non-sponsor turns, each linking to its first turn's moment."""
    yt = post.meta.get("youtube_url", "")
    chunks: list[_pipeline.Chunk] = []
    buf: list[str] = []
    count, start = 0, None

    def flush():
        nonlocal buf, count, start
        if buf:
            chunks.append(_pipeline.Chunk(title=post.title, url=timestamp_link(yt, start) or post.url,
                                          date=post.date, text="\n".join(buf)))
        buf, count, start = [], 0, None

    for t in parse_turns(post.text):
        if t["sponsor"]:
            continue
        speaker = "Speaker" if t["speaker"].lower() == UNSEPARATED else t["speaker"]
        tw = t["text"].split()
        # Long monologues are split so each passage stays a quotable size.
        for i in range(0, len(tw), words):
            piece = " ".join(tw[i:i + words])
            if count and count + min(len(tw) - i, words) > words:
                flush()
            if start is None:
                start = t["seconds"]
            buf.append(f"{speaker} ({t['ts']}): {piece}")
            count += min(len(tw) - i, words)
    flush()
    return [c for c in chunks if len(c.text) >= 40]
