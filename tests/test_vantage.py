"""Tests for the parts that hold opinions: classification, storage semantics,
and the aggregation both front-ends read. No network, no gh CLI.

    python3 -m unittest discover -s tests -v
"""

import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vantage import analyze, classify, report, store, web  # noqa: E402


def day(offset):
    return (dt.date.today() + dt.timedelta(days=offset)).isoformat()


def stamp(offset):
    return day(offset) + "T00:00:00Z"


class TestClassify(unittest.TestCase):
    def test_known_domains(self):
        self.assertEqual(classify.category("jobs.lever.co"), "ats")
        self.assertEqual(classify.category("linkedin.com"), "professional")
        self.assertEqual(classify.category("google.com"), "search")
        self.assertEqual(classify.category("github.com"), "code")
        self.assertEqual(classify.category("news.ycombinator.com"), "social")

    def test_subdomain_suffix_match(self):
        # An ATS gives every customer its own subdomain, so a suffix match is
        # the only way these ever get recognised.
        self.assertEqual(classify.category("acme.myworkdayjobs.com"), "ats")
        self.assertEqual(classify.category("boards.greenhouse.io"), "ats")
        self.assertEqual(classify.category("some-co.ashbyhq.com"), "ats")

    def test_case_and_whitespace(self):
        self.assertEqual(classify.category("  LinkedIn.COM  "), "professional")

    def test_unknown_and_empty(self):
        self.assertEqual(classify.category("intranet.acme.corp"), "other")
        self.assertEqual(classify.category(""), "direct")
        self.assertEqual(classify.category(None), "direct")

    def test_partial_domain_is_not_a_match(self):
        # "notlever.co" must not match "lever.co" - suffix matching is on
        # label boundaries, not raw string endings.
        self.assertEqual(classify.category("notlever.co"), "other")

    def test_ats_outranks_everything(self):
        self.assertGreater(classify.weight("ats"), classify.weight("professional"))
        self.assertGreater(classify.weight("professional"), classify.weight("search"))
        self.assertGreater(classify.weight("search"), classify.weight("code"))

    def test_path_kinds(self):
        self.assertEqual(classify.path_kind("/deknapp/taper"), "landing")
        self.assertEqual(classify.path_kind("/deknapp/taper/blob/main/a.py"), "source")
        self.assertEqual(classify.path_kind("/deknapp/taper/tree/main/src"), "browse")
        self.assertEqual(classify.path_kind("/deknapp/taper/graphs/traffic"), "self")
        self.assertEqual(classify.path_kind("/deknapp/taper/pulse"), "self")
        self.assertEqual(classify.path_kind("/deknapp/taper/commits/main"), "history")

    def test_depth_excludes_self_visits(self):
        paths = [
            {"path": "/u/r", "uniques": 4},                  # shallow
            {"path": "/u/r/blob/main/x.py", "uniques": 4},    # deep
            {"path": "/u/r/graphs/traffic", "uniques": 100},  # you, ignored
        ]
        self.assertAlmostEqual(classify.depth_score(paths), 0.5)

    def test_depth_of_nothing_is_zero(self):
        self.assertEqual(classify.depth_score([]), 0.0)
        self.assertEqual(classify.depth_score([{"path": "/u/r/pulse", "uniques": 9}]), 0.0)

    def test_signal_ranks_ats_above_volume(self):
        one_ats = classify.referrer_signal(
            [{"referrer": "jobs.lever.co", "count": 1, "uniques": 1}])
        much_github = classify.referrer_signal(
            [{"referrer": "github.com", "count": 90, "uniques": 15}])
        self.assertGreater(one_ats["score"], much_github["score"])
        self.assertEqual(one_ats["top_signal"]["category"], "ats")

    def test_signal_of_nothing(self):
        sig = classify.referrer_signal([])
        self.assertEqual(sig["score"], 0.0)
        self.assertIsNone(sig["top_signal"])

    def test_verdict_names_the_ats(self):
        sig = classify.referrer_signal(
            [{"referrer": "boards.greenhouse.io", "count": 3, "uniques": 2}])
        text, level = classify.verdict(sig, 0.5, 5)
        self.assertEqual(level, "strong")
        self.assertIn("greenhouse.io", text)

    def test_verdict_with_no_traffic(self):
        text, level = classify.verdict(classify.referrer_signal([]), 0.0, 0)
        self.assertEqual(level, "none")


class TestStore(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = store.connect(self.path)

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_resync_is_idempotent(self):
        rows = [{"timestamp": stamp(-1), "count": 5, "uniques": 3}]
        for _ in range(3):
            store.save_daily(self.conn, "u/r", "views", rows)
        got = self.conn.execute("SELECT count, uniques FROM daily").fetchall()
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0][0], got[0][1]), (5, 3))

    def test_today_only_grows(self):
        # Today's row fills in as the day passes; a later sync must not be
        # able to shrink a day we already recorded.
        store.save_daily(self.conn, "u/r", "views",
                         [{"timestamp": stamp(0), "count": 9, "uniques": 4}])
        store.save_daily(self.conn, "u/r", "views",
                         [{"timestamp": stamp(0), "count": 12, "uniques": 5}])
        store.save_daily(self.conn, "u/r", "views",
                         [{"timestamp": stamp(0), "count": 2, "uniques": 1}])
        row = self.conn.execute("SELECT count, uniques FROM daily").fetchone()
        self.assertEqual((row[0], row[1]), (12, 5))

    def test_history_outlives_githubs_window(self):
        # The whole point: a day that has aged out of GitHub's 14-day window
        # survives a sync that no longer reports it.
        store.save_daily(self.conn, "u/r", "views",
                         [{"timestamp": stamp(-40), "count": 7, "uniques": 4}])
        store.save_daily(self.conn, "u/r", "views",
                         [{"timestamp": stamp(-1), "count": 1, "uniques": 1}])
        days = [r[0] for r in self.conn.execute("SELECT day FROM daily ORDER BY day")]
        self.assertIn(day(-40), days)

    def test_views_and_clones_do_not_collide(self):
        rows = [{"timestamp": stamp(-1), "count": 5, "uniques": 3}]
        store.save_daily(self.conn, "u/r", "views", rows)
        store.save_daily(self.conn, "u/r", "clones",
                         [{"timestamp": stamp(-1), "count": 99, "uniques": 40}])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0], 2)

    def test_events_round_trip(self):
        eid = store.add_event(self.conn, day(-2), "Applied to Acme")
        self.assertEqual(len(store.events(self.conn)), 1)
        self.assertEqual(store.delete_event(self.conn, eid), 1)
        self.assertEqual(store.events(self.conn), [])

    def test_referrer_snapshots_track_first_seen(self):
        store.save_referrers(self.conn, "u/r",
                             [{"referrer": "linkedin.com", "count": 2, "uniques": 1}],
                             snapshot=day(-5))
        store.save_referrers(self.conn, "u/r",
                             [{"referrer": "linkedin.com", "count": 4, "uniques": 2},
                              {"referrer": "jobs.lever.co", "count": 1, "uniques": 1}],
                             snapshot=day(0))
        seen = store.referrer_first_seen(self.conn)
        self.assertEqual(seen["linkedin.com"]["first_seen"], day(-5))
        self.assertEqual(seen["jobs.lever.co"]["first_seen"], day(0))
        # only the newest snapshot is "current"
        current = {r["referrer"] for r in store.current_referrers(self.conn)}
        self.assertEqual(current, {"linkedin.com", "jobs.lever.co"})


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = store.connect(self.path)

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def seed(self):
        store.upsert_repo(self.conn, {
            "name": "u/r", "visibility": "public", "stars": 1, "forks": 0,
            "description": "d", "language": "Python", "url": "https://x",
            "pushed_at": "", "created_at": "", "archived": 0})
        for i in range(0, 30):
            store.save_daily(self.conn, "u/r", "views",
                             [{"timestamp": stamp(-i), "count": 2, "uniques": 1}])
        store.save_daily(self.conn, "u/r", "views",
                         [{"timestamp": stamp(-3), "count": 40, "uniques": 20}])
        store.save_referrers(self.conn, "u/r", [
            {"referrer": "jobs.lever.co", "count": 3, "uniques": 2},
            {"referrer": "github.com", "count": 20, "uniques": 6}])
        store.save_paths(self.conn, "u/r", [
            {"path": "/u/r", "title": "Overview", "count": 10, "uniques": 6},
            {"path": "/u/r/blob/main/a.py", "title": "a.py", "count": 5, "uniques": 3},
            {"path": "/u/r/graphs/traffic", "title": "traffic", "count": 9, "uniques": 1}])
        self.conn.commit()

    def test_empty_database_still_builds(self):
        d = analyze.build(self.conn, days=30)
        self.assertEqual(d["summary"]["visitors_14d"], 0)
        self.assertEqual(d["verdict"]["level"], "none")
        self.assertEqual(len(d["timeline"]), 30)
        self.assertFalse(d["summary"]["comparable"])

    def test_full_payload(self):
        self.seed()
        d = analyze.build(self.conn, days=30)
        self.assertEqual(len(d["timeline"]), 30)
        self.assertEqual(d["timeline"][-1]["day"], day(0))
        self.assertEqual(d["verdict"]["level"], "strong")
        self.assertIn("lever.co", d["verdict"]["text"])
        self.assertTrue(d["summary"]["comparable"])
        # the spike day is picked out
        self.assertIn(day(-3), [s["day"] for s in d["spikes"]])
        # self-visits are labelled, not silently dropped
        kinds = {k["kind"] for k in d["path_kinds"]}
        self.assertIn("self", kinds)
        # ...but they stay out of the depth ratio: 3 deep of 9 non-self
        self.assertAlmostEqual(d["summary"]["depth_score"], 3 / 9.0, places=3)

    def test_timeline_is_dense_and_ends_today(self):
        self.seed()
        d = analyze.build(self.conn, days=90)
        self.assertEqual(len(d["timeline"]), 90)
        days = [x["day"] for x in d["timeline"]]
        self.assertEqual(days, sorted(days))
        self.assertEqual(days[-1], day(0))
        self.assertEqual(d["timeline"][0]["c"], 0)  # before any history

    def test_repo_filter(self):
        self.seed()
        store.save_daily(self.conn, "u/other", "views",
                         [{"timestamp": stamp(0), "count": 100, "uniques": 50}])
        self.conn.commit()
        everything = analyze.build(self.conn, days=30)
        just_one = analyze.build(self.conn, days=30, repo="u/r")
        self.assertGreater(everything["summary"]["views_window"],
                           just_one["summary"]["views_window"])


class TestUniqueVisitors(unittest.TestCase):
    """The distinction the report exists to stop blurring: GitHub's 14-day
    de-duplicated visitor count is NOT the sum of the daily unique counts, and
    only the first is a headcount."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = store.connect(self.path)

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def payload(self):
        # One visitor who came back on five days: five visitor-days, one person.
        for i in range(5):
            store.save_daily(self.conn, "u/r", "views",
                             [{"timestamp": stamp(-i), "count": 3, "uniques": 1}])
        store.save_traffic(self.conn, "u/r", {
            "views": {"count": 15, "uniques": 1, "views": []},
            "clones": {"count": 4, "uniques": 2, "clones": []},
        })
        self.conn.commit()
        return analyze.build(self.conn, days=30)

    def test_window_uniques_are_kept_not_recomputed(self):
        d = self.payload()
        self.assertEqual(d["summary"]["unique_visitors_14d"], 1)
        self.assertEqual(d["summary"]["visitor_days_14d"], 5)

    def test_per_repo_people_come_from_the_window(self):
        d = self.payload()
        row = [r for r in d["repos"] if r["repo"] == "u/r"][0]
        self.assertEqual(row["unique_visitors_14d"], 1)
        self.assertEqual(row["visitor_days"], 5)

    def test_missing_window_data_degrades_rather_than_lying(self):
        # A database written before windows were recorded must not present
        # visitor-days as if they were people.
        store.save_daily(self.conn, "u/r", "views",
                         [{"timestamp": stamp(-1), "count": 3, "uniques": 2}])
        self.conn.commit()
        d = analyze.build(self.conn, days=30)
        self.assertIsNone(d["summary"]["unique_visitors_14d"])
        self.assertEqual(d["summary"]["visitor_days_14d"], 2)
        self.assertIn("visitor-days", report.render(d, report.Ink(False)))

    def test_report_never_calls_visitor_days_visitors(self):
        text = report.render(self.payload(), report.Ink(False))
        self.assertIn("1 unique visitors", text)
        self.assertIn("5 visitor-days", text)


class TestPerDayPerRepo(unittest.TestCase):
    """"Which repos did anyone look at today" has to be answerable, including
    when the answer is "none" - silence there reads as a missing feature."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = store.connect(self.path)

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass

    def test_grid_ends_today_and_is_dense(self):
        store.save_daily(self.conn, "u/a", "views",
                         [{"timestamp": stamp(-2), "count": 4, "uniques": 2}])
        self.conn.commit()
        g = analyze.recent_grid(self.conn, days=7)
        self.assertEqual(len(g["days"]), 7)
        self.assertEqual(g["days"][-1], day(0))
        row = g["rows"][0]
        self.assertEqual(len(row["cells"]), 7)
        self.assertEqual(row["cells"][-3]["c"], 4)
        self.assertEqual(row["cells"][-1]["c"], 0)

    def test_quiet_repos_are_left_out_of_the_grid(self):
        store.save_daily(self.conn, "u/a", "views",
                         [{"timestamp": stamp(-1), "count": 1, "uniques": 1}])
        store.save_daily(self.conn, "u/quiet", "views",
                         [{"timestamp": stamp(-40), "count": 9, "uniques": 5}])
        self.conn.commit()
        g = analyze.recent_grid(self.conn, days=7)
        self.assertEqual([r["repo"] for r in g["rows"]], ["u/a"])

    def test_repos_viewed_today_sort_first(self):
        store.save_daily(self.conn, "u/busy-last-week", "views",
                         [{"timestamp": stamp(-3), "count": 50, "uniques": 20}])
        store.save_daily(self.conn, "u/read-today", "views",
                         [{"timestamp": stamp(0), "count": 1, "uniques": 1}])
        self.conn.commit()
        g = analyze.recent_grid(self.conn, days=7)
        self.assertEqual(g["rows"][0]["repo"], "u/read-today")

    def test_today_names_the_repos_viewed(self):
        store.save_daily(self.conn, "u/a", "views",
                         [{"timestamp": stamp(0), "count": 6, "uniques": 2}])
        store.save_daily(self.conn, "u/b", "views",
                         [{"timestamp": stamp(-1), "count": 9, "uniques": 4}])
        store.record_sync(self.conn, "", dt.datetime.now().isoformat(), 2, 2, 0, 1.0)
        self.conn.commit()
        d = analyze.build(self.conn, days=30)
        self.assertEqual([r["repo"] for r in d["today"]["repos"]], ["u/a"])
        self.assertEqual(d["today"]["views"], 6)
        self.assertTrue(d["today"]["synced_today"])

    def test_a_quiet_day_says_so_out_loud(self):
        store.save_daily(self.conn, "u/a", "views",
                         [{"timestamp": stamp(-1), "count": 9, "uniques": 4}])
        store.record_sync(self.conn, "", dt.datetime.now().isoformat(), 1, 1, 0, 1.0)
        self.conn.commit()
        d = analyze.build(self.conn, days=30)
        self.assertEqual(d["today"]["repos"], [])
        self.assertIn("No repo has been viewed today",
                      report.render(d, report.Ink(False)))

    def test_never_synced_today_is_not_reported_as_no_traffic(self):
        store.save_daily(self.conn, "u/a", "views",
                         [{"timestamp": stamp(-1), "count": 9, "uniques": 4}])
        store.record_sync(self.conn, "", day(-3) + "T09:00:00", 1, 1, 0, 1.0)
        self.conn.commit()
        d = analyze.build(self.conn, days=30)
        self.assertFalse(d["today"]["synced_today"])
        self.assertIn("Not synced today", report.render(d, report.Ink(False)))


class TestWebHelpers(unittest.TestCase):
    def test_query_parsing(self):
        self.assertEqual(web._parse_query("days=30&repo=a%2Fb"),
                         {"days": "30", "repo": "a/b"})
        self.assertEqual(web._parse_query(""), {})

    def test_days_is_clamped_and_never_crashes(self):
        self.assertEqual(web._int("30", 90), 30)
        self.assertEqual(web._int("junk", 90), 90)
        self.assertEqual(web._int(None, 90), 90)
        self.assertEqual(web._int("-5", 90), 1)
        self.assertEqual(web._int("99999", 90), 3650)


if __name__ == "__main__":
    unittest.main(verbosity=2)
