"""A synthetic database, so the dashboard can be seen without waiting 14 days.

Useful for two things: trying vantage before you have any history of your own,
and taking screenshots without publishing your real repo names. The numbers are
generated, not real - the dashboard says so while a demo database is loaded.
"""

import datetime as dt
import random

from . import store

REPOS = [
    ("acme/tide-gauge", "Coastal sea-level ingest and anomaly detection", "Python", 34),
    ("acme/otter", "Tiny dependency-free job queue for SQLite", "Go", 118),
    ("acme/mesa-router", "Traffic microsimulation for small road networks", "Rust", 12),
    ("acme/leafwise", "Herbarium image classifier with a calibration harness", "Python", 61),
    ("acme/pitchfork", "Chord-aware audio segmentation", "TypeScript", 5),
    ("acme/dotfiles", "", "Shell", 2),
]

# Weight per repo, so traffic is lopsided the way it really is.
SHARE = [0.34, 0.27, 0.06, 0.20, 0.09, 0.04]

REFERRERS = [
    ("boards.greenhouse.io", 4, 3),
    ("linkedin.com", 21, 11),
    ("mail.google.com", 6, 4),
    ("google.com", 44, 29),
    ("github.com", 63, 18),
    ("news.ycombinator.com", 88, 52),
    ("duckduckgo.com", 9, 7),
]

PATHS = [
    ("/acme/otter", "Overview", 260, 141),
    ("/acme/otter/blob/main/README.md", "README.md", 74, 48),
    ("/acme/otter/blob/main/queue.go", "queue.go", 61, 33),
    ("/acme/otter/tree/main/internal", "internal", 38, 22),
    ("/acme/tide-gauge", "Overview", 96, 57),
    ("/acme/tide-gauge/blob/main/ingest/noaa.py", "noaa.py", 29, 18),
    ("/acme/tide-gauge/commits/main", "Commits", 17, 11),
    ("/acme/leafwise/releases", "Releases", 12, 9),
    ("/acme/otter/graphs/traffic", "/graphs/traffic", 41, 1),
    ("/acme/tide-gauge/pulse", "/pulse", 22, 1),
]

EVENTS = [
    (-9,  "Applied to Northbridge — staff platform engineer", "application"),
    (-23, "Posted otter v0.4 to Hacker News", "post"),
    (-38, "Sent GitHub link to recruiter at Vela", "outreach"),
]


def seed(conn, days=120, seed_value=7):
    """Fill `conn` with a plausible traffic history. Idempotent-ish: it wipes
    the demo tables first so re-seeding doesn't stack."""
    rng = random.Random(seed_value)
    for table in ("daily", "referrers", "paths", "repos", "events", "syncs"):
        conn.execute("DELETE FROM " + table)

    today = dt.date.today()
    for i, (name, desc, lang, stars) in enumerate(REPOS):
        store.upsert_repo(conn, {
            "name": name, "visibility": "public", "stars": stars, "forks": stars // 6,
            "description": desc, "language": lang,
            "url": "https://github.com/" + name,
            "pushed_at": (today - dt.timedelta(days=rng.randint(1, 30))).isoformat(),
            "created_at": "", "archived": 0,
        })

    # A slow upward drift, a weekday rhythm, and three deliberate bursts.
    bursts = {23: 9.0, 22: 5.5, 9: 3.4, 8: 2.2, 38: 2.6}
    for back in range(days):
        d = today - dt.timedelta(days=back)
        weekday = 0.55 if d.weekday() >= 5 else 1.0
        drift = 1.0 + 0.9 * (1 - back / float(days))
        burst = bursts.get(back, 1.0)
        base = 6.5 * weekday * drift * burst
        for i, (name, _, _, _) in enumerate(REPOS):
            lam = base * SHARE[i]
            views = max(0, int(rng.gauss(lam, lam * 0.55)))
            if views == 0 and rng.random() < 0.55:
                continue
            uniques = max(1, int(views * rng.uniform(0.42, 0.78))) if views else 0
            store.save_daily(conn, name, "views",
                             [{"timestamp": d.isoformat() + "T00:00:00Z",
                               "count": views, "uniques": uniques}])
            clones = max(0, int(rng.gauss(2.2, 2.0)))
            if clones:
                store.save_daily(conn, name, "clones",
                                 [{"timestamp": d.isoformat() + "T00:00:00Z",
                                   "count": clones,
                                   "uniques": max(1, int(clones * 0.7))}])

    # Referrer/path snapshots for the last few days, so "first seen" has range.
    for back in (6, 3, 0):
        snap = (today - dt.timedelta(days=back)).isoformat()
        scale = 1.0 - back * 0.04
        rows = REFERRERS if back < 6 else REFERRERS[1:]   # the ATS shows up later
        store.save_referrers(conn, "acme/otter", [
            {"referrer": r, "count": int(c * scale), "uniques": int(u * scale)}
            for r, c, u in rows], snapshot=snap)
        store.save_paths(conn, "acme/otter", [
            {"path": p, "title": t, "count": int(c * scale), "uniques": int(u * scale)}
            for p, t, c, u in PATHS], snapshot=snap)

    for back, label, kind in EVENTS:
        store.add_event(conn, (today - dt.timedelta(days=back)).isoformat(),
                        label, kind)

    now = dt.datetime.now().isoformat(timespec="seconds")
    store.record_sync(conn, now, now, len(REPOS), len(REPOS), 0, 3.1)
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('demo','1')",)
    conn.commit()
    return len(REPOS)


def is_demo(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='demo'").fetchone()
    return bool(row and row[0] == "1")
