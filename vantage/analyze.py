"""Build the one payload both the terminal report and the web app render."""

import datetime as dt

from . import classify, demo, store

#: How many days of per-repo columns the grid carries. Seven reads as "this
#: week" and still fits an 80-column terminal at eight characters a day.
RECENT_DAYS = 7


def _fill_days(rows, days):
    """Turn a sparse day series into a dense one ending today."""
    by_day = {r["day"]: r for r in rows}
    end = dt.date.today()
    out = []
    for i in range(days - 1, -1, -1):
        d = (end - dt.timedelta(days=i)).isoformat()
        r = by_day.get(d)
        out.append({"day": d, "c": (r or {}).get("c", 0) or 0,
                    "u": (r or {}).get("u", 0) or 0})
    return out


def _spikes(timeline, min_uniques=3):
    """Days that stand out from the baseline: >= mean + 2 sd, and non-trivial."""
    vals = [d["u"] for d in timeline]
    n = len(vals)
    if n < 7:
        return []
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    sd = var ** 0.5
    threshold = max(mean + 2 * sd, min_uniques)
    return [d for d in timeline if d["u"] >= threshold and d["u"] > 0]


def _day_labels(days):
    """The last `days` calendar days, oldest first."""
    end = dt.date.today()
    return [(end - dt.timedelta(days=i)).isoformat()
            for i in range(days - 1, -1, -1)]


def recent_grid(conn, days=7, repo=None):
    """Views per day per repo for the last `days` days.

    This is the question `vantage` was worst at answering: the 90-day table
    said which repos were busy this quarter, and the sparkline bucketed days
    together, so "did anyone look at anything today" had no answer anywhere.
    Rows are dense - a repo that was quiet every day still shows, as zeroes -
    but only repos with at least one view in the window appear at all."""
    labels = _day_labels(days)
    matrix = store.day_matrix(conn, days, "views")
    rows = []
    for name, by_day in matrix.items():
        if repo and name != repo:
            continue
        cells = [{"day": d,
                  "c": (by_day.get(d) or {}).get("c", 0),
                  "u": (by_day.get(d) or {}).get("u", 0)} for d in labels]
        total = sum(c["c"] for c in cells)
        if not total:
            continue
        rows.append({
            "repo": name,
            "cells": cells,
            "views": total,
            "visitor_days": sum(c["u"] for c in cells),
            "today_views": cells[-1]["c"],
            "today_uniques": cells[-1]["u"],
        })
    rows.sort(key=lambda r: (-r["today_views"], -r["views"], r["repo"]))
    totals = [{"day": d,
               "c": sum(r["cells"][i]["c"] for r in rows),
               "u": sum(r["cells"][i]["u"] for r in rows)}
              for i, d in enumerate(labels)]
    return {"days": labels, "rows": rows, "totals": totals}


def _today_block(conn, cov, repo=None):
    """What happened today, said plainly - including when the answer is
    'nothing', which is the common case and was previously indistinguishable
    from 'vantage does not track this'."""
    day = dt.date.today().isoformat()
    rows = [r for r in store.day_totals(conn, day, "views")
            if not repo or r["repo"] == repo]
    clones = [r for r in store.day_totals(conn, day, "clones")
              if not repo or r["repo"] == repo]
    synced_today = (cov.get("last_sync") or "")[:10] == day
    return {
        "day": day,
        "repos": [{"repo": r["repo"], "views": r["c"], "uniques": r["u"]}
                  for r in rows],
        "views": sum(r["c"] for r in rows),
        "visitor_days": sum(r["u"] for r in rows),
        "clones": sum(r["c"] for r in clones),
        "n_repos": len(rows),
        # Without a sync today there is simply no row to read, which is a
        # different statement from "nobody came".
        "synced_today": synced_today,
        "last_sync": cov.get("last_sync"),
    }


def _unique_visitors(conn, cov, repo=None):
    """GitHub's own 14-day de-duplicated visitor count, and whether we can
    honestly compare it with the fortnight before."""
    snaps = store.window_snapshots(conn)
    if not snaps:
        return None
    latest = snaps[0]
    rows = store.window_totals(conn, latest, repo, "views")
    clone_rows = store.window_totals(conn, latest, repo, "clones")
    prior_snap = store.snapshot_near(
        conn, (dt.date.fromisoformat(latest) - dt.timedelta(days=14)).isoformat())
    prior = None
    if prior_snap and prior_snap != latest:
        prior = sum(r["uniques"] for r in
                    store.window_totals(conn, prior_snap, repo, "views"))
    total = sum(r["uniques"] for r in rows)
    return {
        "snapshot": latest,
        "stale_days": (dt.date.today() - dt.date.fromisoformat(latest)).days,
        "visitors": total,
        "views": sum(r["count"] for r in rows),
        "cloners": sum(r["uniques"] for r in clone_rows),
        "prior_snapshot": prior_snap if prior is not None else None,
        "prior_visitors": prior,
        "delta": (total - prior) if prior is not None else None,
        "per_repo": {r["repo"]: r for r in rows},
        "n_repos": len([r for r in rows if r["count"]]),
    }


def build(conn, days=90, repo=None):
    window = days
    timeline = _fill_days(store.series(conn, window, repo, "views"), window)
    clones = _fill_days(store.series(conn, window, repo, "clones"), window)

    # Compare the trailing 14 days against the 14 before them.
    recent = timeline[-14:]
    prior = timeline[-28:-14] if len(timeline) >= 28 else []
    recent_views = sum(d["c"] for d in recent)
    recent_visitors = sum(d["u"] for d in recent)
    prior_views = sum(d["c"] for d in prior)
    prior_visitors = sum(d["u"] for d in prior)

    referrers = store.current_referrers(conn, repo)
    first_seen = store.referrer_first_seen(conn)
    for r in referrers:
        cat = classify.category(r["referrer"])
        r["category"] = cat
        r["category_label"] = classify.label(cat)
        r["weight"] = classify.weight(cat)
        r["why"] = classify.spec(cat)["why"]
        fs = first_seen.get(r["referrer"], {})
        r["first_seen"] = fs.get("first_seen")
        r["last_seen"] = fs.get("last_seen")
        r["repos"] = sorted(set((r.get("repos") or "").split(",")))

    paths = store.current_paths(conn, repo)
    for p in paths:
        kind = classify.path_kind(p["path"])
        p["kind"] = kind
        p["kind_label"] = classify.PATH_KINDS[kind][0]

    depth = classify.depth_score(paths)
    signal = classify.referrer_signal(referrers)
    verdict_text, verdict_level = classify.verdict(signal, depth, recent_visitors)

    # A delta is only honest once we actually hold data for the earlier window.
    cov = store.coverage(conn)
    prior_start = (dt.date.today() - dt.timedelta(days=27)).isoformat()
    comparable = bool(cov["first_day"]) and cov["first_day"] <= prior_start

    uniq = _unique_visitors(conn, cov, repo)
    today_block = _today_block(conn, cov, repo)
    grid = recent_grid(conn, days=RECENT_DAYS, repo=repo)

    repo_rows = store.repo_totals(conn, window)
    per_repo = store.per_repo_series(conn, window, "views")
    meta = {r["name"]: r for r in store.repos(conn)}
    for row in repo_rows:
        s = _fill_days(
            [{"day": x["day"], "c": x["c"], "u": x["u"]}
             for x in per_repo.get(row["repo"], [])], window)
        row["spark"] = [d["c"] for d in s]
        row["spark_u"] = [d["u"] for d in s]
        # Sub-windows, so a repo that got read *today* is visible without
        # squinting at a sparkline that buckets several days into one column.
        row["today_views"] = s[-1]["c"]
        row["today_uniques"] = s[-1]["u"]
        row["views_7d"] = sum(d["c"] for d in s[-7:])
        row["visitor_days_7d"] = sum(d["u"] for d in s[-7:])
        row["views_14d"] = sum(d["c"] for d in s[-14:])
        row["visitor_days"] = row["visitors"]  # summed daily uniques, not people
        w = (uniq or {}).get("per_repo", {}).get(row["repo"])
        row["unique_visitors_14d"] = w["uniques"] if w else None
        m = meta.get(row["repo"], {})
        row["visibility"] = m.get("visibility", "")
        row["stars"] = m.get("stars", 0)
        row["url"] = m.get("url", "")
        row["description"] = m.get("description", "")
        row["language"] = m.get("language", "")
        row["last_error"] = m.get("last_error", "")

    # Path-kind rollup, self-visits kept separate rather than silently dropped.
    kinds = {}
    for p in paths:
        k = kinds.setdefault(p["kind"], {"kind": p["kind"],
                                         "label": classify.PATH_KINDS[p["kind"]][0],
                                         "why": classify.PATH_KINDS[p["kind"]][1],
                                         "count": 0, "uniques": 0})
        k["count"] += p["count"]
        k["uniques"] += p["uniques"]

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "is_demo": demo.is_demo(conn),
        "window_days": window,
        "repo_filter": repo,
        "summary": {
            "views_14d": recent_views,
            # `visitors_14d` is daily uniques summed - visitor-DAYS. It counts
            # one person who came back on Tuesday and Thursday twice. Kept
            # because it is what the day series supports, but never presented
            # as a headcount; `unique_visitors_14d` below is GitHub's own
            # de-duplicated number and is what the report leads with.
            "visitors_14d": recent_visitors,
            "visitor_days_14d": recent_visitors,
            "unique_visitors_14d": (uniq or {}).get("visitors"),
            "unique_visitors_prior_14d": (uniq or {}).get("prior_visitors"),
            "unique_visitors_delta": (uniq or {}).get("delta"),
            "unique_cloners_14d": (uniq or {}).get("cloners"),
            "views_prior_14d": prior_views,
            "visitors_prior_14d": prior_visitors,
            "views_delta": recent_views - prior_views,
            "visitors_delta": recent_visitors - prior_visitors,
            "views_window": sum(d["c"] for d in timeline),
            "visitors_window": sum(d["u"] for d in timeline),
            "clones_window": sum(d["c"] for d in clones),
            "cloners_window": sum(d["u"] for d in clones),
            "n_repos": len(repo_rows),
            "depth_score": round(depth, 3),
            "comparable": comparable,
        },
        "verdict": {"text": verdict_text, "level": verdict_level},
        "today": today_block,
        "recent": grid,
        "uniques": uniq,
        "signal": signal,
        "timeline": timeline,
        "clone_timeline": clones,
        "spikes": _spikes(timeline),
        "repos": repo_rows,
        "referrers": referrers,
        "paths": paths,
        "path_kinds": sorted(kinds.values(), key=lambda k: -k["uniques"]),
        "events": store.events(conn),
        "coverage": cov,
        "caveats": [
            "GitHub keeps only 14 days of traffic. Days before your first sync "
            "are gone for good; the chart fills in as vantage keeps snapshotting.",
            "Two different unique counts exist and they do not agree. "
            "'Unique visitors' is GitHub's own 14-day figure, de-duplicated "
            "across the whole fortnight, per repo. 'Visitor-days' sums the "
            "per-day uniques, so someone who came back on three days counts "
            "three times. Only the second can be charted per day.",
            "Unique visitors are summed across repos, so one person who read "
            "three of your repos counts three times in the headline. It is an "
            "upper bound on people - GitHub never exposes a cross-repo "
            "de-duplicated number.",
            "Referrers and paths are a rolling 14-day top-10 - not a full log, "
            "and not per-day.",
            "Your own visits count. Paths under /graphs or /pulse are almost "
            "certainly you, and are labelled as such.",
            "No referrer identifies a person or a company. Everything here is "
            "an inference from a domain name.",
        ],
    }
