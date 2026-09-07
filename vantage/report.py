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
    A("  %s   %s   %s" % (
        _stat(ink, "visitors", s["visitors_14d"],
              s["visitors_delta"] if cmp_ok else None),
        _stat(ink, "views", s["views_14d"],
              s["views_delta"] if cmp_ok else None),
        _stat(ink, "clones", s["clones_window"], None),
    ))
    A(ink.dim("     last 14 days%s        clones over %dd"
              % (", vs the 14 before" if cmp_ok else " (no earlier history yet)",
                 data["window_days"])))

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
        A("  " + ink.dim("%s %s %s %s %s" % (
            _pad("repo", 24), _pad("trend", 20),
            _pad("views", 7, ">"), _pad("visits", 7, ">"),
            _pad("clones", 7, ">"))))
        for r in rows[:top]:
            A("  %s %s %s" % (
                _pad(r["repo"].split("/")[-1], 24),
                ink.blue(_pad(spark(r["spark"], 20), 20)),
                ink.bold("%7d" % r["views"]) + "%7d%7d" % (r["visitors"], r["clones"]),
            ))

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
