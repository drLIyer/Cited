import sqlite3
import time
from datetime import datetime, timezone

import httpx
import pytest

import metrics

DAY = 86400
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "h.db"
    metrics.configure(True, lambda: path)
    yield path
    metrics.configure(False, lambda: path)


def _search(path, ts, sub):
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, ts REAL, url TEXT, subdomain TEXT,
                 question TEXT, answer TEXT, sources TEXT, confidence REAL, tool TEXT, level TEXT)""")
    c.execute("INSERT INTO history (ts, url, subdomain, question, answer, sources, confidence, tool) "
              "VALUES (?, '', ?, 'q', 'a', '[]', 0.5, 'search')", (ts, sub))
    c.commit(); c.close()


def test_clients_counted_once_per_day_and_ip_never_stored(db):
    for _ in range(5):
        metrics.note_client("203.0.113.7", NOW)
    metrics.note_client("198.51.100.2", NOW)
    raw = db.read_bytes()
    assert b"203.0.113.7" not in raw and b"198.51.100.2" not in raw
    g = metrics.growth(db, 30, NOW)
    assert g["days"][-1]["clients"] == 2


def test_hashes_rolled_up_and_deleted_next_day(db):
    metrics.note_client("203.0.113.7", NOW)
    metrics.note_client("198.51.100.2", NOW)
    metrics.note_client("203.0.113.7", NOW + DAY)          # new day → yesterday rolled up
    c = sqlite3.connect(db)
    days_left = {r[0] for r in c.execute("SELECT day FROM client_days")}
    assert days_left == {"2026-11-23"}                      # yesterday's hashes are gone
    assert c.execute("SELECT n FROM client_daily WHERE day='2026-11-22'").fetchone()[0] == 2
    g = metrics.growth(db, 30, NOW + DAY)
    assert [d["clients"] for d in g["days"][-2:]] == [2, 1]


def test_same_ip_on_different_days_is_unlinkable(db):
    metrics.note_client("203.0.113.7", NOW)
    h1 = sqlite3.connect(db).execute("SELECT h FROM client_days").fetchone()[0]
    metrics.note_client("203.0.113.7", NOW + DAY)
    h2 = sqlite3.connect(db).execute("SELECT h FROM client_days").fetchone()[0]
    assert h1 != h2                                         # daily salt rotates


def test_growth_series_and_week_over_week(db):
    for i in range(7):                                      # this week: 14 searches, 2 newsletters
        _search(db, NOW - i * DAY, "a"); _search(db, NOW - i * DAY, "b")
    for i in range(7, 14):                                  # previous week: 7 searches, 1 newsletter
        _search(db, NOW - i * DAY, "a")
    g = metrics.growth(db, 30, NOW)
    assert len(g["days"]) == 30 and g["days"][-1]["day"] == "2026-11-22"
    assert g["days"][-1]["searches"] == 2 and g["days"][-1]["newsletters"] == 2
    w = g["week"]
    assert (w["searches"], w["searches_prev"], w["searches_change_pct"]) == (14, 7, 100)
    assert (w["newsletters"], w["newsletters_prev"]) == (2, 1)


def test_growth_with_no_history(db):
    g = metrics.growth(db, 30, NOW)
    assert g["week"]["searches"] == 0 and g["week"]["searches_change_pct"] is None


def test_disabled_counts_nothing(tmp_path):
    path = tmp_path / "x.db"
    metrics.configure(False, lambda: path)
    metrics.note_client("1.2.3.4")
    assert not path.exists()


async def test_overview_includes_growth(server_factory):
    s = server_factory(CITED_TRANSPORT="http", CITED_ADMIN_TOKEN="s3cret")
    s._pipeline.prewarm()
    s._history.save("https://a.substack.com", "q", "a", [], 0.6, update_memory=False, level="medium")
    app = s.build_http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        o = (await c.get("/admin/api/overview", headers={"Authorization": "Bearer s3cret"})).json()
    assert o["growth"]["week"]["searches"] == 1 and len(o["growth"]["days"]) == 30
