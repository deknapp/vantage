"""GitHub REST access, authenticated by borrowing the gh CLI's token.

We shell out to `gh` exactly once (for the token) and then talk to the API
directly over urllib. Spawning `gh api` per request costs ~200ms of process
startup each; with four endpoints across dozens of repos that dominates the
runtime. One token plus a thread pool turns a ~30s sync into a ~2s one.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://api.github.com"
UA = "vantage/0.1 (+https://github.com/deknapp/vantage)"


class GhError(Exception):
    pass


class Forbidden(GhError):
    """No push access - the traffic API is owner/collaborator only."""


def token():
    """Read the token from the gh CLI, or from GITHUB_TOKEN as a fallback."""
    env = os.environ.get("VANTAGE_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if env:
        return env.strip()
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=15
        )
    except FileNotFoundError:
        raise GhError(
            "gh CLI not found. Install it (brew install gh) and run `gh auth login`,\n"
            "or set GITHUB_TOKEN to a token with the `repo` scope."
        )
    if out.returncode != 0:
        raise GhError(
            "gh is installed but not authenticated. Run `gh auth login`.\n"
            + out.stderr.strip()
        )
    return out.stdout.strip()


def whoami(tok):
    return get("/user", tok)["login"]


def get(path, tok, params=None, retries=3):
    """GET one API path. Returns parsed JSON, or raises."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", UA)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                link = resp.headers.get("Link", "")
                return json.loads(body) if body else None, link
        except urllib.error.HTTPError as e:
            if e.code == 403 and "rate limit" in (e.read().decode("utf-8", "replace")).lower():
                reset = e.headers.get("X-RateLimit-Reset")
                if reset and attempt < retries - 1:
                    wait = max(0, int(reset) - int(time.time())) + 1
                    if wait <= 60:
                        time.sleep(wait)
                        continue
                raise GhError("GitHub rate limit exhausted for " + path)
            if e.code == 403:
                raise Forbidden(path)
            if e.code == 404:
                raise Forbidden(path)
            if e.code in (202, 500, 502, 503) and attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise GhError("HTTP %s for %s" % (e.code, path))
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise GhError("network error for %s: %s" % (path, e.reason))
    raise GhError("gave up on " + path)


def get_json(path, tok, params=None):
    data, _ = get(path, tok, params)
    return data


def paginate(path, tok, params=None):
    """Follow Link headers until exhausted."""
    params = dict(params or {})
    params.setdefault("per_page", 100)
    page = 1
    out = []
    while True:
        params["page"] = page
        data, link = get(path, tok, params)
        if not data:
            break
        out.extend(data)
        if 'rel="next"' not in link:
            break
        page += 1
        if page > 20:  # 2000 repos is plenty
            break
    return out


def list_repos(tok, owner=None, affiliation="owner", include_forks=False,
               include_archived=False, visibility="all"):
    """Repos we can read traffic for - i.e. ones we have push access to."""
    if owner:
        repos = paginate("/users/%s/repos" % owner, tok, {"type": "owner", "sort": "pushed"})
    else:
        repos = paginate("/user/repos", tok, {"affiliation": affiliation, "sort": "pushed"})
    out = []
    for r in repos:
        if r.get("fork") and not include_forks:
            continue
        if r.get("archived") and not include_archived:
            continue
        if visibility == "public" and r.get("private"):
            continue
        if visibility == "private" and not r.get("private"):
            continue
        out.append({
            "name": r["full_name"],
            "visibility": "private" if r.get("private") else "public",
            "stars": r.get("stargazers_count", 0),
            "forks": r.get("forks_count", 0),
            "description": r.get("description") or "",
            "pushed_at": r.get("pushed_at") or "",
            "created_at": r.get("created_at") or "",
            "language": r.get("language") or "",
            "archived": 1 if r.get("archived") else 0,
            "url": r.get("html_url") or "",
        })
    return out


TRAFFIC_ENDPOINTS = ("views", "clones", "popular/referrers", "popular/paths")


def fetch_traffic(tok, full_name, workers=4):
    """All four traffic endpoints for one repo. Returns (data, error_or_None)."""
    result = {}

    def one(ep):
        return ep, get_json("/repos/%s/traffic/%s" % (full_name, ep), tok)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for ep, data in pool.map(one, TRAFFIC_ENDPOINTS):
                result[ep] = data
    except Forbidden:
        return None, "no push access (traffic needs admin/push on the repo)"
    except GhError as e:
        return None, str(e)
    return result, None


def fetch_all(tok, repos, workers=8, progress=None):
    """Fetch traffic for many repos in parallel. Yields (repo, data, error)."""
    def one(repo):
        data, err = fetch_traffic(tok, repo["name"])
        return repo, data, err

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (repo, data, err) in enumerate(pool.map(one, repos)):
            if progress:
                progress(i + 1, len(repos), repo["name"], err)
            yield repo, data, err
