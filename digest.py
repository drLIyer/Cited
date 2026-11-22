"""
Daily email digest for the Cited owner (like ThinkWithMe's health digest, but daily).

Contents (counts only — never users' questions or IP addresses):
  health status + anything needing attention, searches and confidence,
  errors by type, bugs fixed, auto-fix agent activity, threats blocked.

Configure with environment variables on the server (never commit these):
  CITED_DIGEST_TO        recipient address — required to enable the digest
  CITED_DIGEST_HOUR_UTC  hour of day (UTC, 0–23) to send; default 13
  CITED_PUBLIC_URL       base URL for the dashboard link, e.g. https://cited-7bys.onrender.com
  Either Resend (https://resend.com):
    RESEND_API_KEY, CITED_DIGEST_FROM (e.g. "Cited <alerts@yourdomain.com>";
    Resend's test sender onboarding@resend.dev only delivers to your own Resend address)
  or SMTP:
    SMTP_HOST, SMTP_PORT (default 587, STARTTLS), SMTP_USER, SMTP_PASSWORD,
    CITED_DIGEST_FROM (defaults to SMTP_USER)
"""
from __future__ import annotations

import html
import logging
import os
import smtplib
import sqlite3
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Optional

import httpx

log = logging.getLogger("cited.digest")

LEVEL_NAMES = {"high": "High", "medium": "Moderate", "low": "Limited"}


def config() -> dict:
    to = os.getenv("CITED_DIGEST_TO", "").strip()
    provider = ("resend" if os.getenv("RESEND_API_KEY") else
                "smtp" if os.getenv("SMTP_HOST") else None)
    try:
        hour = max(0, min(23, int(os.getenv("CITED_DIGEST_HOUR_UTC", "13"))))
    except ValueError:
        hour = 13
    return {"enabled": bool(to and provider), "to": to, "provider": provider, "hour_utc": hour,
            "from": os.getenv("CITED_DIGEST_FROM", "").strip() or os.getenv("SMTP_USER", ""),
            "public_url": os.getenv("CITED_PUBLIC_URL", "").rstrip("/")}


def status_for_dashboard() -> dict:
    c = config()
    return {"enabled": c["enabled"], "provider": c["provider"], "hour_utc": c["hour_utc"],
            "to_set": bool(c["to"])}


# ── Schedule bookkeeping ──────────────────────────────────────────────────────

def _meta(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(db_path, timeout=5)
    c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return c


def last_sent(db_path: Path) -> Optional[str]:
    db = _meta(db_path)
    try:
        row = db.execute("SELECT value FROM meta WHERE key = 'digest_last_sent'").fetchone()
        return row[0] if row else None
    finally:
        db.close()


def mark_sent(db_path: Path, day: str) -> None:
    db = _meta(db_path)
    try:
        db.execute("INSERT INTO meta (key, value) VALUES ('digest_last_sent', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (day,))
        db.commit()
    finally:
        db.close()


def is_due(db_path: Path, now: Optional[datetime] = None) -> bool:
    c = config()
    if not c["enabled"]:
        return False
    now = now or datetime.now(timezone.utc)
    return now.hour >= c["hour_utc"] and last_sent(db_path) != now.date().isoformat()


# ── Building ──────────────────────────────────────────────────────────────────

def build(data: dict, public_url: str = "") -> tuple[str, str, str]:
    """Return (subject, text, html) from the collected data."""
    h, st, sec, fx, fx1, af, err = (data["health"], data.get("searches"), data.get("security") or {},
                                    data.get("fixes") or {}, data.get("fixes_24h") or {},
                                    data.get("autofix") or {}, data.get("errors") or [])
    searches = st["searches"] if st else 0
    errors_24h = sum(g["count"] for g in err if g["level"] in ("ERROR", "CRITICAL"))
    threats = sec.get("total_24h", 0)
    status = h["status"]
    subject = (f"Cited daily digest — {status.upper()}: {searches} searches, "
               f"{errors_24h} errors, {threats} threats blocked")

    sections: list[tuple[str, list[str]]] = []

    lines = [h.get("summary", "")]
    for c in h["checks"].values():
        if c["status"] not in ("ok", "disabled"):
            lines.append(f"• {c.get('label')}: {c['status']}"
                         + (f" — {c['detail']}" if c.get("detail") else ""))
            if c.get("action"):
                lines.append(f"    What to do: {c['action']}")
    sections.append(("Status", lines))

    if st:
        lv = st["levels"]
        pct = lambda n: f"{round(n / searches * 100)}%" if searches else "0%"
        lines = [f"{searches} searches in the last 24 hours.",
                 f"Confidence: {lv['high']} High ({pct(lv['high'])}) · {lv['medium']} Moderate "
                 f"({pct(lv['medium'])}) · {lv['low']} Limited ({pct(lv['low'])})."]
        weak = sorted([s for s in st["by_source"] if s["low_confidence"]],
                      key=lambda s: -s["low_confidence"])[:3]
        if weak:
            lines.append("Most Limited-coverage searches: " +
                         ", ".join(f"{s['label']} ({s['low_confidence']})" for s in weak) + ".")
        sections.append(("Searches & confidence", lines))

    bugs = [g for g in err if g["category"] == "error"]
    lines = [f"{fx.get('open_errors', 0)} open bug type(s) · "
             f"{sum(1 for g in bugs if g['state'] == 'regressed')} regressed."]
    for g in sorted(bugs, key=lambda g: -g["count"])[:5]:
        lines.append(f"• [{g['state']}] {g['name']}: {g['message'][:140]} (×{g['count']})")
    if len(lines) == 1:
        lines.append("No errors in the last 24 hours.")
    sections.append(("Errors (24 h)", lines))

    sections.append(("Bugs fixed", [
        f"Last 24 h: {fx1.get('fixed', 0)} ({fx1.get('auto_fixed', 0)} by the auto-fix agent). "
        f"Last 30 days: {fx.get('fixed', 0)} — {fx.get('holding', 0)} holding, "
        f"{fx.get('regressed', 0)} regressed."]))

    s24 = af.get("stats_24h") or {}
    if af.get("enabled"):
        lines = [f"{s24.get('applied', 0)} fixes applied · {s24.get('worked', 0)} worked · "
                 f"{s24.get('failed', 0)} didn't work · {s24.get('skipped', 0)} skipped (safety limit). "
                 f"{s24.get('learned_fixes', 0)} fixes remembered in the playbook."]
        for a in [a for a in af.get("recent", []) if a["result"] in ("applied", "failed")][:5]:
            lines.append(f"• {a['action_name']} — {a['problem'][:120]} ({a['result']})")
        sections.append(("Auto-fix agent (24 h)", lines))

    if sec.get("enabled"):
        lines = [f"{threats} blocked in the last 24 hours ({sec.get('total_30d', 0)} in 30 days)."]
        lines += [f"• {t['name']}: {t['24h']}" for t in sec["types"] if t["24h"]]
        sections.append(("Threats blocked", lines))

    link = f"{public_url}/admin" if public_url else "/admin on your Cited server"
    text = subject + "\n" + "=" * 60 + "\n\n"
    for title, ls in sections:
        text += f"{title.upper()}\n" + "\n".join(ls) + "\n\n"
    text += f"Dashboard: {link}\n\n— Cited"

    colors = {"ok": "#27ae60", "degraded": "#e67e22", "unavailable": "#e74c3c"}
    esc = html.escape
    body = "".join(
        f'<h3 style="margin:20px 0 6px;color:#2E3F73;font-size:15px">{esc(title)}</h3>'
        + "".join(f'<div style="margin:2px 0">{esc(l)}</div>' for l in ls)
        for title, ls in sections)
    html_out = (
        '<div style="font-family:-apple-system,Segoe UI,Inter,sans-serif;font-size:14px;color:#1b2440;max-width:640px">'
        f'<div style="background:#2E3F73;color:#fff;padding:14px 18px;border-radius:10px">'
        f'<b style="font-size:17px">Cited <span style="color:#F5B23E">daily digest</span></b> '
        f'<span style="background:{colors.get(status, "#888")};padding:2px 10px;border-radius:999px;'
        f'font-size:12px;font-weight:700;margin-left:8px">{esc(status.upper())}</span></div>'
        f'{body}'
        + (f'<p style="margin-top:22px"><a href="{esc(link)}" style="color:#2E3F73;font-weight:700">'
           f'Open the admin dashboard →</a></p>' if public_url else "")
        + '</div>')
    return subject, text, html_out


# ── Sending ───────────────────────────────────────────────────────────────────

def send(subject: str, text: str, html_body: str) -> str:
    """Send via the configured provider. Returns a short description; raises on failure."""
    c = config()
    if not c["to"]:
        raise RuntimeError("CITED_DIGEST_TO is not set.")
    if c["provider"] == "resend":
        if not c["from"]:
            raise RuntimeError("Set CITED_DIGEST_FROM (a sender on a domain verified in Resend).")
        r = httpx.post("https://api.resend.com/emails", timeout=20, json={
            "from": c["from"], "to": [c["to"]], "subject": subject, "text": text, "html": html_body},
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"})
        if r.status_code >= 300:
            raise RuntimeError(f"Resend returned {r.status_code}: {r.text[:300]}")
        return "sent via Resend"
    if c["provider"] == "smtp":
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, c["from"], c["to"]
        msg.set_content(text)
        msg.add_alternative(html_body, subtype="html")
        port = int(os.getenv("SMTP_PORT", "587"))
        ctx = ssl.create_default_context()
        if port == 465:
            server = smtplib.SMTP_SSL(os.environ["SMTP_HOST"], port, context=ctx, timeout=20)
        else:
            server = smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=20)
            server.starttls(context=ctx)
        try:
            if os.getenv("SMTP_USER"):
                server.login(os.environ["SMTP_USER"], os.getenv("SMTP_PASSWORD", ""))
            server.send_message(msg)
        finally:
            server.quit()
        return "sent via SMTP"
    raise RuntimeError("No email provider configured (set RESEND_API_KEY or SMTP_HOST).")
