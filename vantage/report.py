"""Terminal report. Colour when attached to a tty, plain text when piped."""

import os
import shutil
import sys

BLOCKS = " ▁▂▃▄▅▆▇█"


class Ink:
    def __init__(self, enabled):
        self.on = enabled

    def _w(self, code, s):
        return "\033[%sm%s\033[0m" % (code, s) if self.on else s

    def dim(self, s):     return self._w("2", s)
    def bold(self, s):    return self._w("1", s)
    def blue(self, s):    return self._w("38;5;68", s)
    def green(self, s):   return self._w("38;5;71", s)
    def amber(self, s):   return self._w("38;5;179", s)
    def red(self, s):     return self._w("38;5;167", s)
    def violet(self, s):  return self._w("38;5;104", s)
    def grey(self, s):    return self._w("38;5;245", s)


def use_colour(force=None):
    if force is not None:
        return force
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def spark(values, width=None):
    """Unicode sparkline, scaled to the series max."""
    if not values:
        return ""
    if width and len(values) > width:
        # bucket down to `width` points, keeping peaks visible
        step = len(values) / float(width)
        bucketed = []
        for i in range(width):
            chunk = values[int(i * step):max(int((i + 1) * step), int(i * step) + 1)]
            bucketed.append(max(chunk) if chunk else 0)
        values = bucketed
    hi = max(values)
    if hi <= 0:
        return BLOCKS[0] * len(values)
    return "".join(BLOCKS[min(8, int(round(v / hi * 8)))] for v in values)


def _pad(s, n, align="<"):
    """Pad to a visible width. Colour must be applied *after* padding, never
    before - ANSI escapes are bytes the format spec would otherwise count."""
    s = _clip(s, n)
    gap = " " * max(0, n - len(s))
    return (s + gap) if align == "<" else (gap + s)


def _fmt_delta(n, ink):
    if n > 0:
        return ink.green("+%d" % n)
    if n < 0:
        return ink.red(str(n))
    return ink.grey("0")


def _bar(frac, width=18, filled="█", empty="·"):
    n = int(round(max(0.0, min(1.0, frac)) * width))
    return filled * n + empty * (width - n)


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def _weekday(day):
    y, m, d = (int(x) for x in day.split("-"))
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][
        __import__("datetime").date(y, m, d).weekday()]


def _today_section(A, ink, data, rule, w):
    """The first thing anyone actually wants to know: did anything happen today,
    and to which repo. Says so explicitly when the answer is no - a silent
    section reads as a missing feature rather than as an empty day."""
    t = data["today"]
    A("")
    A(rule)
    head = "  " + ink.bold("Today") + ink.dim("  ·  " + t["day"])
    if t["last_sync"]:
        head += ink.dim("  ·  last sync " + t["last_sync"].replace("T", " "))
    A(head)
    A("")

    if not t["synced_today"]:
        A("  " + ink.amber("!") + "  Not synced today - GitHub has not been asked yet.")
        A(ink.dim("     Run `vantage sync` for today's numbers."))
        return

    if not t["repos"]:
        A("  " + ink.grey("○") + "  No repo has been viewed today.")
        A(ink.dim("     GitHub counts days in UTC and lags a few hours, so today's"))
        A(ink.dim("     row keeps filling in - re-sync later before reading much"))
        A(ink.dim("     into a quiet morning."))
        return

    hi = max(r["views"] for r in t["repos"]) or 1
    for r in t["repos"][:12]:
        A("  %s %s %s %s" % (
            _pad(r["repo"].split("/")[-1], 24),
            ink.green(_bar(r["views"] / float(hi), 12)),
            ink.bold("%4d" % r["views"]) + ink.dim(" " + _plural(r["views"], "view")),
            ink.dim("· %d unique %s" % (r["uniques"],
                                        _plural(r["uniques"], "visitor"))),
        ))
    A("")
    A(ink.dim("     %d %s from %d unique %s across %d %s%s."
              % (t["views"], _plural(t["views"], "view"),
                 t["visitor_days"], _plural(t["visitor_days"], "visitor"),
                 t["n_repos"], _plural(t["n_repos"], "repo"),
                 (" · %d clones" % t["clones"]) if t["clones"] else "")))
    A(ink.dim("     Unique is per repo per day: the same person on two repos"))
    A(ink.dim("     counts twice."))


def _grid_section(A, ink, data, rule, w):
    """Views per day per repo. One column per day, one row per repo, so
    "which repos were read on Thursday" is a glance rather than an inference
    from a bucketed sparkline."""
    grid = data["recent"]
    rows = grid["rows"]
    if not rows:
        return
    name_w = 20
    cell_w = 8
    n_days = max(3, min(len(grid["days"]), (w - 2 - name_w) // cell_w))
    days = grid["days"][-n_days:]
    offset = len(grid["days"]) - n_days

    A("")
    A(rule)
    A("  " + ink.bold("Views per day, by repo")
      + ink.dim("  ·  views (unique visitors that day)"))
    A("")

    head = "  " + _pad("repo", name_w)
    for i, d in enumerate(days):
        lab = "today" if i == len(days) - 1 else "%s %s" % (_weekday(d), d[8:])
        head += _pad(lab, cell_w, ">")
    A(ink.dim(head))

    def cells_for(cells):
        line = ""
        for c in cells[offset:]:
            if not c["c"]:
                line += _pad("·", cell_w, ">")
            else:
                line += _pad("%d (%d)" % (c["c"], c["u"]), cell_w, ">")
        return line

    for r in rows[:12]:
        name = _pad(r["repo"].split("/")[-1], name_w)
        body = cells_for(r["cells"])
        line = "  " + (ink.bold(name) if r["today_views"] else name)
        A(line + (ink.blue(body) if r["today_views"] else body))

    A("  " + ink.dim("─" * min(w - 2, name_w + cell_w * n_days)))
    A("  " + ink.dim(_pad("all repos", name_w)) + ink.dim(cells_for(grid["totals"])))
    A("")
    A(ink.dim("     · = nobody · today's column is still filling in (UTC, lagged)"))


def render_today(data, ink=None):
    """The `vantage today` view: what happened today, and the days around it."""
    ink = ink or Ink(use_colour())
    cols = shutil.get_terminal_size((100, 24)).columns
    w = max(60, min(cols, 100))
    out = []
    A = out.append
    rule = ink.dim("─" * w)
    A("")
    A(ink.bold("  vantage") + ink.dim("  ·  today"))
    _today_section(A, ink, data, rule, w)
    _grid_section(A, ink, data, rule, w)
    A("")
    A(ink.dim("  `vantage report` for referrers, reading depth and the verdict."))
    A("")
    return "\n".join(out)


def render(data, ink=None, top=10):
    ink = ink or Ink(use_colour())
    cols = shutil.get_terminal_size((100, 24)).columns
    w = max(60, min(cols, 100))
    out = []
    A = out.append
    rule = ink.dim("─" * w)

    s = data["summary"]
    cov = data["coverage"]

    A("")
    A(ink.bold("  vantage") + ink.dim("  ·  who is looking at your GitHub"))
    A(rule)

    # --- the headline answer -------------------------------------------
    level = data["verdict"]["level"]
    mark = {"strong": ink.green("●"), "moderate": ink.amber("●"),
            "weak": ink.grey("●"), "none": ink.grey("○")}[level]
    A("")
    A("  " + mark + "  " + ink.bold(_wrap(data["verdict"]["text"], w - 6, "     ")))

    # --- headline numbers ----------------------------------------------
    A("")
    # A delta is only meaningful once we hold data for the earlier window;
    # before that "+25" would just be measuring when syncing started.
    cmp_ok = s.get("comparable")
    uniq = data.get("uniques")
    # Two different unique counts exist. GitHub's window figure is the honest
    # headcount-ish one; summed daily uniques (visitor-days) is what a day
    # chart can show. Label whichever we print for exactly what it is, and
    # never call either of them "visitors" unqualified.
    if uniq:
        people = _stat(ink, "unique visitors", uniq["visitors"], uniq["delta"])
    else:
        people = _stat(ink, "visitor-days", s["visitor_days_14d"],
                       s["visitors_delta"] if cmp_ok else None)
    A("  %s   %s   %s" % (
        _stat(ink, "views", s["views_14d"],
              s["views_delta"] if cmp_ok else None),
        people,
        _stat(ink, "clones", s["clones_window"], None),
    ))
    A(ink.dim("     last 14 days%s        clones over %dd"
              % (", vs the 14 before" if cmp_ok else " (no earlier history yet)",
                 data["window_days"])))
    A("")
    if uniq:
        A(ink.dim("     unique visitors = GitHub's own count, de-duplicated across the"))
        A(ink.dim("     whole 14 days, added up per repo - so one person who read three"))
        A(ink.dim("     of your repos counts three times, but a daily regular counts once."))
        A(ink.dim("     Summing the per-day uniques instead gives %d visitor-days."
                  % s["visitor_days_14d"]))
        if uniq.get("stale_days"):
            A(ink.dim("     (from the sync on %s, %d %s ago)"
                      % (uniq["snapshot"], uniq["stale_days"],
                         _plural(uniq["stale_days"], "day"))))
    else:
        A(ink.dim("     visitor-days = per-day unique visitors summed, so someone who"))
        A(ink.dim("     came back on three days counts three times. Run `vantage sync`"))
        A(ink.dim("     to start recording GitHub's de-duplicated 14-day visitor count."))

    _today_section(A, ink, data, rule, w)
    _grid_section(A, ink, data, rule, w)

    # --- timeline -------------------------------------------------------
    tl = data["timeline"]
    if any(d["c"] for d in tl):
        A("")
        A("  " + ink.bold("Daily traffic") + ink.dim("  (%d days · ▔ views, ▁ visitors)"
                                                     % len(tl)))
        A("  " + ink.blue(spark([d["c"] for d in tl], w - 4)))
        A("  " + ink.violet(spark([d["u"] for d in tl], w - 4)))
        first, last = tl[0]["day"], tl[-1]["day"]
        pad = (w - 4) - len(first) - len(last)
        A("  " + ink.dim(first + " " * max(1, pad) + last))

    for sp in data["spikes"][:3]:
        A("  " + ink.amber("▲") + ink.dim(" spike %s · %d visitors" % (sp["day"], sp["u"])))

    # --- referrers ------------------------------------------------------
    refs = data["referrers"]
    if refs:
        A("")
        A(rule)
        A("  " + ink.bold("Where they came from") + ink.dim("  (GitHub's rolling 14-day top 10)"))
        A("")
        hi = max(r["uniques"] for r in refs) or 1
        for r in refs[:top]:
            colour = {"ats": ink.green, "professional": ink.green,
                      "email": ink.green, "job_board": ink.amber,
                      "search": ink.blue, "social": ink.violet,
                      "ai": ink.blue, "code": ink.grey,
                      "other": ink.amber}.get(r["category"], ink.grey)
            A("  %s %s %s  %s" % (
                _pad(r["referrer"], 28),
                colour(_bar(r["uniques"] / float(hi), 14)),
                ink.bold("%4d" % r["uniques"]),
                ink.dim(r["category_label"]),
            ))
        A("")
        A(ink.dim("     bar = unique visitors · green = a hiring-shaped referrer"))

    # --- how deeply they read -------------------------------------------
    kinds = [k for k in data["path_kinds"] if k["kind"] != "self"]
    if kinds:
        A("")
        A(rule)
        depth = s["depth_score"]
        A("  " + ink.bold("How deeply they read") + ink.dim("  ·  depth %d%%" % round(depth * 100)))
        A("")
        hi = max(k["uniques"] for k in kinds) or 1
        for k in kinds[:6]:
            deep = k["kind"] in ("source", "browse", "history", "release")
            colour = ink.green if deep else ink.grey
            A("  %s %s %s" % (
                _pad(k["label"], 28),
                colour(_bar(k["uniques"] / float(hi), 14)),
                ink.bold("%4d" % k["uniques"]),
            ))
        A("")
        A(ink.dim("     depth = share of views on code, not the README"))

    # --- repos ----------------------------------------------------------
    rows = [r for r in data["repos"] if r["views"] or r["clones"]]
    if rows:
        A("")
        A(rule)
        A("  " + ink.bold("By repo") + ink.dim("  (%d days)" % data["window_days"]))
        A("")
        # "visits" used to head a column of summed daily uniques, which is not
        # visits and not visitors. Split into the two things it was conflating:
        # views over several spans, and GitHub's de-duplicated 14-day people.
        A("  " + ink.dim("%s %s %s %s %s %s %s%s" % (
            _pad("repo", 20), _pad("trend", 16),
            _pad("today", 6, ">"), _pad("7d", 6, ">"),
            _pad("14d", 6, ">"), _pad("%dd" % data["window_days"], 6, ">"),
            _pad("people", 7, ">"), _pad("clones", 7, ">"))))
        for r in rows[:top]:
            name = _pad(r["repo"].split("/")[-1], 20)
            people = r.get("unique_visitors_14d")
            A("  %s %s%s%s%s%s%s" % (
                ink.bold(name) if r.get("today_views") else name,
                ink.blue(_pad(spark(r["spark"], 16), 16)),
                (ink.green(_pad(str(r["today_views"]), 6, ">"))
                 if r.get("today_views") else ink.grey(_pad("·", 6, ">"))),
                _pad(str(r.get("views_7d", 0)), 6, ">"),
                _pad(str(r.get("views_14d", 0)), 6, ">"),
                ink.bold(_pad(str(r["views"]), 6, ">")),
                _pad("—" if people is None else str(people), 7, ">")
                + _pad(str(r["clones"]), 7, ">"),
            ))
        A("")
        A(ink.dim("     today/7d/14d/%dd are page views · people = GitHub's unique"
                  % data["window_days"]))
        A(ink.dim("     visitors over its own rolling 14 days, de-duplicated"))

    # --- events ----------------------------------------------------------
    ev = data["events"][:5]
    if ev:
        A("")
        A(rule)
        A("  " + ink.bold("Your markers"))
        A("")
        for e in ev:
            A("  %s  %s %s" % (ink.dim(e["day"]), e["label"],
                               ink.dim("(" + e["kind"] + ")")))

    # --- footer ----------------------------------------------------------
    A("")
    A(rule)
    hist = "no history yet"
    if cov["first_day"]:
        hist = "history %s → %s (%d days)" % (cov["first_day"], cov["last_day"],
                                              cov["n_days"])
    synced = cov["last_sync"] or "never"
    A(ink.dim("  %s · %d syncs · last %s" % (hist, cov["n_syncs"], synced)))
    A(ink.dim("  GitHub keeps 14 days; vantage keeps the rest. `vantage serve` for charts."))
    A("")
    return "\n".join(out)


def _stat(ink, label, value, delta):
    v = ink.bold("%d" % value)
    d = ("  " + _fmt_delta(delta, ink)) if delta is not None else ""
    return "%s %s%s" % (v, ink.dim(label), d)


def _clip(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n - 1] + "…"


def _wrap(text, width, indent):
    words, lines, cur = text.split(), [], ""
    for word in words:
        if len(cur) + len(word) + 1 > width:
            lines.append(cur)
            cur = word
        else:
            cur = (cur + " " + word).strip()
    if cur:
        lines.append(cur)
    return ("\n" + indent).join(lines)
