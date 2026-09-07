"""Turning referrers and paths into an answer to "is anyone hiring looking?"

The honest framing, which the UI repeats: GitHub never tells you *who*
visited. It gives you a daily unique-visitor count and a 14-day top-10 list of
referring domains and viewed paths. Nothing here identifies a person or a
company. What it does is bucket the evidence by how strongly it points at
someone evaluating you rather than passing through:

  * a referrer that only exists inside a hiring workflow (an ATS like Lever or
    Greenhouse) is close to direct evidence - those pages are seen by
    recruiters and hiring managers, not by the general public;
  * a professional network or a webmail referrer is suggestive;
  * search and social are ordinary discovery;
  * github.com is mostly your own browsing.

Everything is a heuristic. Treat it as a lead, not a fact.
"""

CATEGORIES = {
    "ats": {
        "label": "Hiring platform",
        "weight": 1.0,
        "why": "Only reachable from inside a hiring workflow - the strongest signal you get.",
        "domains": [
            "lever.co", "jobs.lever.co", "hire.lever.co",
            "greenhouse.io", "boards.greenhouse.io", "my.greenhouse.io",
            "ashbyhq.com", "jobs.ashbyhq.com",
            "workday.com", "myworkdayjobs.com", "myworkdaysite.com",
            "smartrecruiters.com", "jobvite.com", "icims.com", "taleo.net",
            "workable.com", "breezy.hr", "teamtailor.com", "recruitee.com",
            "bamboohr.com", "rippling.com", "gem.com", "hired.com",
            "triplebyte.com", "wellfound.com", "angel.co", "otta.com",
            "dover.com", "paradox.ai", "phenom.com", "eightfold.ai",
            "successfactors.com", "brassring.com", "applytojob.com",
            "joinhandshake.com", "handshake.com", "ripplematch.com",
            "hackerrank.com", "codesignal.com", "karat.com", "woven.teams",
            "usajobs.gov", "lanl.gov", "governmentjobs.com",
        ],
    },
    "job_board": {
        "label": "Job board",
        "weight": 0.7,
        "why": "Job-seeking context, but public - could be you or a bot as easily as an employer.",
        "domains": [
            "indeed.com", "glassdoor.com", "ziprecruiter.com", "dice.com",
            "builtin.com", "builtinnyc.com", "monster.com", "simplyhired.com",
            "careerbuilder.com", "themuse.com", "levels.fyi", "hiring.cafe",
            "remoteok.com", "weworkremotely.com", "climatebase.org",
            "workatastartup.com", "ycombinator.com",
        ],
    },
    "professional": {
        "label": "Professional network",
        "weight": 0.8,
        "why": "Someone clicked through from your profile or a post - common recruiter path.",
        "domains": [
            "linkedin.com", "lnkd.in", "www.linkedin.com", "static.licdn.com",
            "xing.com", "polywork.com", "read.cv", "cv.rocks",
        ],
    },
    "email": {
        "label": "Email / chat",
        "weight": 0.8,
        "why": "A link you sent, or one being passed around internally.",
        "domains": [
            "mail.google.com", "outlook.com", "outlook.live.com",
            "outlook.office.com", "outlook.office365.com", "mail.yahoo.com",
            "proton.me", "mail.proton.me", "superhuman.com", "hey.com",
            "teams.microsoft.com", "statics.teams.cdn.office.net",
            "slack.com", "app.slack.com", "discord.com", "notion.so",
        ],
    },
    "ai": {
        "label": "AI assistant",
        "weight": 0.3,
        "why": "Someone asked a chatbot and followed a link, or a crawler came through.",
        "domains": [
            "chatgpt.com", "chat.openai.com", "claude.ai", "perplexity.ai",
            "gemini.google.com", "copilot.microsoft.com", "phind.com",
        ],
    },
    "search": {
        "label": "Search",
        "weight": 0.3,
        "why": "Organic discovery. Volume here tracks how findable you are, not who is looking.",
        "domains": [
            "google.com", "www.google.com", "bing.com", "duckduckgo.com",
            "search.brave.com", "ecosia.org", "yandex.ru", "baidu.com",
            "yahoo.com", "startpage.com", "kagi.com", "search.marcia.com",
        ],
    },
    "social": {
        "label": "Social / community",
        "weight": 0.2,
        "why": "Public sharing. Spiky by nature - one post can dwarf everything else.",
        "domains": [
            "reddit.com", "old.reddit.com", "news.ycombinator.com",
            "lobste.rs", "x.com", "twitter.com", "t.co", "bsky.app",
            "facebook.com", "instagram.com", "youtube.com", "mastodon.social",
            "hachyderm.io", "fosstodon.org", "medium.com", "dev.to",
            "substack.com", "hashnode.com", "stackoverflow.com", "quora.com",
        ],
    },
    "code": {
        "label": "GitHub / code host",
        "weight": 0.05,
        "why": "Mostly your own browsing plus GitHub's internal navigation.",
        "domains": [
            "github.com", "gist.github.com", "githubusercontent.com",
            "gitlab.com", "bitbucket.org", "sourcegraph.com", "npmjs.com",
            "pypi.org", "crates.io", "readthedocs.io", "stackblitz.com",
            "codepen.io", "huggingface.co", "kaggle.com", "colab.research.google.com",
        ],
    },
}

# Domains whose *root* is generic enough that we match on suffix only.
_INDEX = {}
for _key, _spec in CATEGORIES.items():
    for _d in _spec["domains"]:
        _INDEX[_d] = _key

OTHER = {
    "label": "Other",
    "weight": 0.4,
    "why": "Unrecognized referrer - worth a look; a company's own site or wiki lands here.",
}


def category(referrer):
    """Bucket a referrer domain. GitHub reports bare hostnames."""
    host = (referrer or "").strip().lower().rstrip("/")
    if not host or host in ("none", "direct"):
        return "direct"
    if host in _INDEX:
        return _INDEX[host]
    # suffix match: "jobs.acme.lever.co" -> "lever.co"
    parts = host.split(".")
    for i in range(len(parts) - 1):
        suffix = ".".join(parts[i:])
        if suffix in _INDEX:
            return _INDEX[suffix]
    return "other"


def spec(cat):
    if cat == "other":
        return OTHER
    if cat == "direct":
        return {"label": "Direct / unknown", "weight": 0.4,
                "why": "No referrer reported - a pasted link, a bookmark, or a privacy-preserving browser."}
    return CATEGORIES.get(cat, OTHER)


def label(cat):
    return spec(cat)["label"]


def weight(cat):
    return spec(cat)["weight"]


# ------------------------------------------------------------------ paths

def path_kind(path):
    """What a viewed path says about the visitor's intent."""
    p = (path or "").lower()
    if "/graphs/" in p or "/pulse" in p or "/settings" in p or "/network" in p:
        return "self"          # traffic pages: essentially always the owner
    if "/blob/" in p or "/raw/" in p:
        return "source"        # reading actual code
    if "/tree/" in p:
        return "browse"
    if "/commits" in p or "/commit/" in p or "/compare/" in p:
        return "history"
    if "/issues" in p or "/pull" in p or "/discussions" in p:
        return "collab"
    if "/releases" in p or "/tags" in p or "/actions" in p:
        return "release"
    if p.count("/") <= 2:
        return "landing"       # /owner/repo
    return "other"


PATH_KINDS = {
    "landing": ("Repo landing page", "Read the README - the front door."),
    "source":  ("Reading source", "Opened actual code files. This is evaluation, not a drive-by."),
    "browse":  ("Browsing the tree", "Clicked into directories - looking for structure."),
    "history": ("Commits & diffs", "Checking how the work was actually done over time."),
    "collab":  ("Issues / PRs", "Looking at collaboration surface."),
    "release": ("Releases & CI", "Checking whether it ships and whether it builds."),
    "self":    ("Your own pages", "Traffic/insights pages - almost certainly you."),
    "other":   ("Other pages", ""),
}


def depth_score(paths):
    """0-1: how much of the viewing was deep reading vs bouncing off the README.

    `paths` is a list of dicts with `path` and `uniques`. Self-visits are
    excluded from both sides of the ratio so your own checking doesn't inflate
    or deflate the number.
    """
    deep = shallow = 0
    for p in paths:
        kind = path_kind(p.get("path", ""))
        u = p.get("uniques", 0) or 0
        if kind == "self":
            continue
        if kind in ("source", "browse", "history", "release"):
            deep += u
        else:
            shallow += u
    total = deep + shallow
    return (deep / total) if total else 0.0


# ------------------------------------------------------------------ scoring

def referrer_signal(referrers):
    """Weighted 'someone is evaluating me' score over a referrer window.

    Returns a dict with the 0-100 score, the per-category breakdown, and the
    strongest single piece of evidence. The score is deliberately dominated by
    the ATS bucket: one hit from a Lever board matters more than fifty from
    Google.
    """
    buckets = {}
    for r in referrers:
        cat = category(r.get("referrer", ""))
        b = buckets.setdefault(cat, {"category": cat, "label": label(cat),
                                     "weight": weight(cat), "count": 0,
                                     "uniques": 0, "domains": []})
        b["count"] += r.get("count", 0) or 0
        b["uniques"] += r.get("uniques", 0) or 0
        b["domains"].append(r.get("referrer", ""))

    weighted = sum(b["uniques"] * b["weight"] for b in buckets.values())
    raw = sum(b["uniques"] for b in buckets.values())
    # Saturating curve: 8 weighted unique visitors reads as a strong signal.
    score = 100.0 * (1 - 2.718281828 ** (-weighted / 8.0)) if weighted else 0.0

    strong = [b for b in buckets.values()
              if b["category"] in ("ats", "professional", "email", "job_board")]
    strong.sort(key=lambda b: (weight(b["category"]), b["uniques"]), reverse=True)

    return {
        "score": round(score, 1),
        "weighted": round(weighted, 2),
        "unique_referred": raw,
        "buckets": sorted(buckets.values(),
                          key=lambda b: (-b["weight"], -b["uniques"])),
        "top_signal": strong[0] if strong else None,
    }


def verdict(sig, depth, visitors_14d):
    """One plain-English sentence for the top of the dashboard."""
    ats = next((b for b in sig["buckets"] if b["category"] == "ats"), None)
    pro = next((b for b in sig["buckets"] if b["category"] in
                ("professional", "email", "job_board")), None)
    if ats:
        return ("Yes - %d visit%s came from %s, which is a hiring platform." %
                (ats["uniques"], "" if ats["uniques"] == 1 else "s",
                 ", ".join(sorted(set(ats["domains"])))), "strong")
    if pro and depth >= 0.4:
        return ("Probably - traffic from %s, and visitors are opening source "
                "files rather than bouncing off the README." %
                ", ".join(sorted(set(pro["domains"]))), "moderate")
    if pro:
        return ("Maybe - traffic from %s, but visitors mostly stopped at the "
                "README." % ", ".join(sorted(set(pro["domains"]))), "moderate")
    if depth >= 0.5 and visitors_14d >= 3:
        return ("Unclear who, but somebody is reading properly - most page "
                "views are source files, not the landing page.", "moderate")
    if visitors_14d == 0:
        return ("No traffic in the last 14 days. Nothing to read into - keep "
                "syncing and the history builds up.", "none")
    return ("Nothing that looks like recruiting yet - the traffic reads as "
            "ordinary discovery.", "weak")
