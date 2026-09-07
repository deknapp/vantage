"""Build the one payload both the terminal report and the web app render."""

import datetime as dt

from . import classify, demo, store


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

    repo_rows = store.repo_totals(conn, window)
    per_repo = store.per_repo_series(conn, window, "views")
    meta = {r["name"]: r for r in store.repos(conn)}
    for row in repo_rows:
        s = _fill_days(
            [{"day": x["day"], "c": x["c"], "u": x["u"]}
             for x in per_repo.get(row["repo"], [])], window)
        row["spark"] = [d["c"] for d in s]
        row["spark_u"] = [d["u"] for d in s]
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
            "visitors_14d": recent_visitors,
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
            "Daily unique visitors are de-duplicated within a day, not across "
            "days, so a window total counts a returning visitor more than once.",
            "Referrers and paths are a rolling 14-day top-10 - not a full log, "
            "and not per-day.",
            "Your own visits count. Paths under /graphs or /pulse are almost "
            "certainly you, and are labelled as such.",
            "No referrer identifies a person or a company. Everything here is "
            "an inference from a domain name.",
        ],
    }
