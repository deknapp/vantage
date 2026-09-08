"""SQLite store.

GitHub throws away repo traffic after 14 days and there is no way to get it
back. Everything here exists to beat that: each sync writes the rolling window
into a local database, so history accumulates for as long as you keep syncing.
Re-syncing the same day is idempotent.
"""

import os
import sqlite3
import datetime as dt

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    name        TEXT PRIMARY KEY,
    visibility  TEXT,
    stars       INTEGER DEFAULT 0,
    forks       INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    language    TEXT DEFAULT '',
    url         TEXT DEFAULT '',
    pushed_at   TEXT DEFAULT '',
    created_at  TEXT DEFAULT '',
    archived    INTEGER DEFAULT 0,
    tracked     INTEGER DEFAULT 1,
    first_seen  TEXT,
    last_sync   TEXT,
    last_error  TEXT
);

-- One row per repo/day/metric. `metric` is 'views' or 'clones'.
CREATE TABLE IF NOT EXISTS daily (
    repo    TEXT NOT NULL,
    day     TEXT NOT NULL,
    metric  TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    uniques INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, day, metric)
);
CREATE INDEX IF NOT EXISTS daily_day ON daily(day);

-- GitHub's own window-level totals for the rolling 14 days. The `uniques` here
-- is de-duplicated across the WHOLE window, unlike the per-day `uniques` in
-- `daily`, which only de-duplicate within one day. Summing daily uniques
-- therefore counts a returning visitor once per day they came back; this table
-- is the only place an honest "how many people" number can come from.
CREATE TABLE IF NOT EXISTS windows (
    repo     TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    metric   TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    uniques  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, snapshot, metric)
);
CREATE INDEX IF NOT EXISTS win_snapshot ON windows(snapshot);

-- Referrers and paths are 14-day rolling top-10 lists, not per-day series.
-- We keep every snapshot so we can say when a referrer first appeared.
CREATE TABLE IF NOT EXISTS referrers (
    repo     TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    referrer TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    uniques  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, snapshot, referrer)
);
CREATE INDEX IF NOT EXISTS ref_snapshot ON referrers(snapshot);

CREATE TABLE IF NOT EXISTS paths (
    repo     TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    path     TEXT NOT NULL,
    title    TEXT DEFAULT '',
    count    INTEGER NOT NULL DEFAULT 0,
    uniques  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, snapshot, path)
);
CREATE INDEX IF NOT EXISTS path_snapshot ON paths(snapshot);

-- User annotations: "applied to X", "posted resume", "sent link to recruiter".
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    day    TEXT NOT NULL,
    label  TEXT NOT NULL,
    kind   TEXT DEFAULT 'application',
    repo   TEXT DEFAULT '',
    note   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_day ON events(day);

CREATE TABLE IF NOT EXISTS syncs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    n_repos     INTEGER DEFAULT 0,
    n_ok        INTEGER DEFAULT 0,
    n_failed    INTEGER DEFAULT 0,
    seconds     REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def default_path():
    return os.environ.get(
        "VANTAGE_DB", os.path.expanduser("~/.vantage/vantage.db")
    )


def connect(path=None):
    path = path or default_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def today():
    return dt.date.today().isoformat()


def _day(ts):
    """'2026-09-04T00:00:00Z' -> '2026-09-04'."""
    return (ts or "")[:10]


def upsert_repo(conn, repo, error=None):
    now = dt.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO repos (name, visibility, stars, forks, description, language,
                              url, pushed_at, created_at, archived, first_seen,
                              last_sync, last_error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             visibility=excluded.visibility, stars=excluded.stars,
             forks=excluded.forks, description=excluded.description,
             language=excluded.language, url=excluded.url,
             pushed_at=excluded.pushed_at, archived=excluded.archived,
             last_sync=excluded.last_sync, last_error=excluded.last_error""",
        (repo["name"], repo["visibility"], repo.get("stars", 0),
         repo.get("forks", 0), repo.get("description", ""),
         repo.get("language", ""), repo.get("url", ""),
         repo.get("pushed_at", ""), repo.get("created_at", ""),
         repo.get("archived", 0), now, now, error or ""),
    )


def save_daily(conn, repo, metric, rows):
    """Upsert a day series. Counts for a given day only ever grow (today's row
    fills in as the day passes), so we keep the larger of old and new - that
    also protects history against a truncated or partial response."""
    for r in rows or []:
        day = _day(r.get("timestamp"))
        if not day:
            continue
        conn.execute(
            """INSERT INTO daily (repo, day, metric, count, uniques)
               VALUES (?,?,?,?,?)
               ON CONFLICT(repo, day, metric) DO UPDATE SET
                 count=MAX(daily.count, excluded.count),
                 uniques=MAX(daily.uniques, excluded.uniques)""",
            (repo, day, metric, r.get("count", 0), r.get("uniques", 0)),
        )


def save_referrers(conn, repo, rows, snapshot=None):
    snapshot = snapshot or today()
    for r in rows or []:
        conn.execute(
            """INSERT INTO referrers (repo, snapshot, referrer, count, uniques)
               VALUES (?,?,?,?,?)
               ON CONFLICT(repo, snapshot, referrer) DO UPDATE SET
                 count=excluded.count, uniques=excluded.uniques""",
            (repo, snapshot, r.get("referrer", "?"), r.get("count", 0),
             r.get("uniques", 0)),
        )


def save_paths(conn, repo, rows, snapshot=None):
    snapshot = snapshot or today()
    for r in rows or []:
        conn.execute(
            """INSERT INTO paths (repo, snapshot, path, title, count, uniques)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(repo, snapshot, path) DO UPDATE SET
                 title=excluded.title, count=excluded.count,
                 uniques=excluded.uniques""",
            (repo, snapshot, r.get("path", "?"), r.get("title", ""),
             r.get("count", 0), r.get("uniques", 0)),
        )


def save_window(conn, repo, metric, payload, snapshot=None):
    """Store GitHub's window-level totals verbatim.

    The traffic endpoints wrap their day series in a `count`/`uniques` pair for
    the whole 14-day window. That `uniques` is de-duplicated across all 14 days,
    which is the number a human means by "how many people looked" - and it
    cannot be reconstructed from the day rows, so it has to be kept as GitHub
    sends it."""
    if not payload:
        return
    snapshot = snapshot or today()
    conn.execute(
        """INSERT INTO windows (repo, snapshot, metric, count, uniques)
           VALUES (?,?,?,?,?)
           ON CONFLICT(repo, snapshot, metric) DO UPDATE SET
             count=excluded.count, uniques=excluded.uniques""",
        (repo, snapshot, metric, payload.get("count", 0) or 0,
         payload.get("uniques", 0) or 0),
    )


def save_traffic(conn, repo_name, data, snapshot=None):
    save_daily(conn, repo_name, "views", (data.get("views") or {}).get("views"))
    save_daily(conn, repo_name, "clones", (data.get("clones") or {}).get("clones"))
    save_window(conn, repo_name, "views", data.get("views"), snapshot)
    save_window(conn, repo_name, "clones", data.get("clones"), snapshot)
    save_referrers(conn, repo_name, data.get("popular/referrers"), snapshot)
    save_paths(conn, repo_name, data.get("popular/paths"), snapshot)


def record_sync(conn, started, finished, n_repos, n_ok, n_failed, seconds):
    conn.execute(
        """INSERT INTO syncs (started_at, finished_at, n_repos, n_ok, n_failed, seconds)
           VALUES (?,?,?,?,?,?)""",
        (started, finished, n_repos, n_ok, n_failed, round(seconds, 2)),
    )


def add_event(conn, day, label, kind="application", repo="", note=""):
    cur = conn.execute(
        "INSERT INTO events (day, label, kind, repo, note) VALUES (?,?,?,?,?)",
        (day, label, kind, repo, note),
    )
    return cur.lastrowid


def delete_event(conn, event_id):
    cur = conn.execute("DELETE FROM events WHERE id=?", (event_id,))
    return cur.rowcount


# ---------------------------------------------------------------- queries

def since_day(days):
    return (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()


def series(conn, days=90, repo=None, metric="views"):
    """Daily totals across all repos (or one), oldest first."""
    args = [metric, since_day(days)]
    sql = ("SELECT day, SUM(count) c, SUM(uniques) u FROM daily "
           "WHERE metric=? AND day>=?")
    if repo:
        sql += " AND repo=?"
        args.append(repo)
    sql += " GROUP BY day ORDER BY day"
    return [dict(r) for r in conn.execute(sql, args)]


def per_repo_series(conn, days=90, metric="views"):
    rows = conn.execute(
        "SELECT repo, day, count c, uniques u FROM daily "
        "WHERE metric=? AND day>=? ORDER BY repo, day",
        (metric, since_day(days)),
    )
    out = {}
    for r in rows:
        out.setdefault(r["repo"], []).append(
            {"day": r["day"], "c": r["c"], "u": r["u"]}
        )
    return out


def repo_totals(conn, days=90):
    rows = conn.execute(
        """SELECT d.repo AS repo,
                  SUM(CASE WHEN metric='views'  THEN count   ELSE 0 END) views,
                  SUM(CASE WHEN metric='views'  THEN uniques ELSE 0 END) visitors,
                  SUM(CASE WHEN metric='clones' THEN count   ELSE 0 END) clones,
                  SUM(CASE WHEN metric='clones' THEN uniques ELSE 0 END) cloners
           FROM daily d WHERE day>=? GROUP BY d.repo ORDER BY views DESC, clones DESC""",
        (since_day(days),),
    )
    return [dict(r) for r in rows]


def day_matrix(conn, days=7, metric="views"):
    """Per-repo, per-day rows for the last `days` days - the answer to "which
    repos did anyone look at today". Sparse: a repo/day with no traffic has no
    row, and callers fill the gap with zero."""
    rows = conn.execute(
        "SELECT repo, day, count c, uniques u FROM daily "
        "WHERE metric=? AND day>=? AND (count>0 OR uniques>0) "
        "ORDER BY repo, day",
        (metric, since_day(days)),
    )
    out = {}
    for r in rows:
        out.setdefault(r["repo"], {})[r["day"]] = {"c": r["c"], "u": r["u"]}
    return out


def day_totals(conn, day, metric="views"):
    """Every repo with traffic on one specific day, busiest first."""
    rows = conn.execute(
        "SELECT repo, count c, uniques u FROM daily "
        "WHERE metric=? AND day=? AND (count>0 OR uniques>0) "
        "ORDER BY c DESC, u DESC, repo",
        (metric, day),
    )
    return [dict(r) for r in rows]


def window_snapshots(conn):
    """Every snapshot date for which we hold GitHub's window totals, newest
    first."""
    return [r["snapshot"] for r in conn.execute(
        "SELECT DISTINCT snapshot FROM windows ORDER BY snapshot DESC")]


def window_totals(conn, snapshot=None, repo=None, metric="views"):
    """GitHub's own 14-day totals per repo, for one snapshot.

    `uniques` is de-duplicated per repo across the window. Adding it up across
    repos is an upper bound on people, not a headcount: one person who reads
    three of your repos is three uniques here. It is still far closer to the
    truth than summing daily uniques, which also double-counts across days."""
    snapshot = snapshot or latest_snapshot(conn, "windows")
    if not snapshot:
        return []
    args = [metric, snapshot]
    sql = ("SELECT repo, count, uniques FROM windows "
           "WHERE metric=? AND snapshot=?")
    if repo:
        sql += " AND repo=?"
        args.append(repo)
    sql += " ORDER BY uniques DESC, count DESC"
    return [dict(r) for r in conn.execute(sql, args)]


def snapshot_near(conn, target_day, tolerance=3):
    """The window snapshot closest to `target_day`, or None if we have nothing
    within `tolerance` days. Used to compare this fortnight's unique count with
    the one before it, which is only honest if a snapshot from back then
    actually exists."""
    best, best_gap = None, None
    target = dt.date.fromisoformat(target_day)
    for snap in window_snapshots(conn):
        gap = abs((dt.date.fromisoformat(snap) - target).days)
        if best_gap is None or gap < best_gap:
            best, best_gap = snap, gap
    if best_gap is not None and best_gap <= tolerance:
        return best
    return None


def latest_snapshot(conn, table):
    row = conn.execute("SELECT MAX(snapshot) s FROM %s" % table).fetchone()
    return row["s"] if row else None


def current_referrers(conn, repo=None):
    """The most recent 14-day referrer window, summed across repos."""
    snap = latest_snapshot(conn, "referrers")
    if not snap:
        return []
    args = [snap]
    sql = ("SELECT referrer, SUM(count) count, SUM(uniques) uniques, "
           "GROUP_CONCAT(repo) repos FROM referrers WHERE snapshot=?")
    if repo:
        sql += " AND repo=?"
        args.append(repo)
    sql += " GROUP BY referrer ORDER BY uniques DESC, count DESC"
    return [dict(r) for r in conn.execute(sql, args)]


def referrer_first_seen(conn):
    rows = conn.execute(
        "SELECT referrer, MIN(snapshot) first_seen, MAX(snapshot) last_seen, "
        "COUNT(DISTINCT snapshot) n_snapshots FROM referrers GROUP BY referrer"
    )
    return {r["referrer"]: dict(r) for r in rows}


def current_paths(conn, repo=None):
    snap = latest_snapshot(conn, "paths")
    if not snap:
        return []
    args = [snap]
    sql = ("SELECT path, title, repo, count, uniques FROM paths WHERE snapshot=?")
    if repo:
        sql += " AND repo=?"
        args.append(repo)
    sql += " ORDER BY uniques DESC, count DESC"
    return [dict(r) for r in conn.execute(sql, args)]


def events(conn, days=None):
    sql = "SELECT * FROM events"
    args = []
    if days:
        sql += " WHERE day>=?"
        args.append(since_day(days))
    sql += " ORDER BY day DESC, id DESC"
    return [dict(r) for r in conn.execute(sql, args)]


def repos(conn, tracked_only=True):
    sql = "SELECT * FROM repos"
    if tracked_only:
        sql += " WHERE tracked=1"
    sql += " ORDER BY name"
    return [dict(r) for r in conn.execute(sql)]


def coverage(conn):
    """How much history we have, and whether it predates GitHub's 14 days."""
    row = conn.execute(
        "SELECT MIN(day) first, MAX(day) last, COUNT(DISTINCT day) n FROM daily"
    ).fetchone()
    last_sync = conn.execute(
        "SELECT finished_at, seconds FROM syncs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "first_day": row["first"],
        "last_day": row["last"],
        "n_days": row["n"] or 0,
        "last_sync": last_sync["finished_at"] if last_sync else None,
        "last_sync_seconds": last_sync["seconds"] if last_sync else None,
        "n_syncs": conn.execute("SELECT COUNT(*) c FROM syncs").fetchone()["c"],
    }
