"""GoatCounter access, for the traffic GitHub cannot see.

GitHub's traffic API counts views of a *repository page*. It says nothing
about a published site, because GitHub Pages has no server-side analytics at
all -- no logs, no API, nothing. A site's own visitors are therefore a
separate source, and this module is that source.

Nothing here is specific to one account. The site and the API token come from
the environment or from the local database, in that order, and a token is
created by the site's owner under [username] -> API in their GoatCounter
settings. `vantage site --login` writes them down; until then the rest of
vantage behaves exactly as it did before.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import store

UA = "vantage/0.1 (+https://github.com/deknapp/vantage)"

# The documented server-side limit is 4 requests per second. We make a handful
# of calls per sync, so the cheapest correct thing is to stay under it rather
# than to handle being refused.
MIN_INTERVAL = 0.3

SITE_KEY = "goatcounter_site"
TOKEN_KEY = "goatcounter_token"


class GoatError(Exception):
    pass


class NotConfigured(GoatError):
    """No site or token. Not an error on its own -- the feature is optional."""


def normalise_site(value):
    """Accept 'name', 'name.goatcounter.com' or a full URL, return the origin.

    People copy whichever of the three is in front of them, and a wrong guess
    here fails as a 404 several steps later where it is hard to read.
    """
    v = (value or "").strip().rstrip("/")
    if not v:
        return ""
    if "://" in v:
        parts = urllib.parse.urlsplit(v)
        return "%s://%s" % (parts.scheme, parts.netloc)
    if "." in v:
        return "https://" + v
    return "https://%s.goatcounter.com" % v


def config(conn):
    """(site, token), from the environment first so a shell can override."""
    site = os.environ.get("VANTAGE_GOATCOUNTER_SITE") or store.get_meta(conn, SITE_KEY)
    token = (os.environ.get("VANTAGE_GOATCOUNTER_TOKEN")
             or os.environ.get("GOATCOUNTER_TOKEN")
             or store.get_meta(conn, TOKEN_KEY))
    return normalise_site(site), (token or "").strip()


def configured(conn):
    site, token = config(conn)
    return bool(site and token)


def save_config(conn, site=None, token=None):
    if site is not None:
        store.set_meta(conn, SITE_KEY, normalise_site(site))
    if token is not None:
        store.set_meta(conn, TOKEN_KEY, token.strip())


class Client:
    def __init__(self, site, token):
        if not site or not token:
            raise NotConfigured(
                "no GoatCounter site configured. Run `vantage site --login`, "
                "or set VANTAGE_GOATCOUNTER_SITE and VANTAGE_GOATCOUNTER_TOKEN."
            )
        self.site = normalise_site(site)
        self.token = token
        self._last = 0.0

    def get(self, path, params=None, retries=3):
        url = self.site + "/api/v0" + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v not in (None, "")})
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": UA,
        })
        for attempt in range(retries):
            wait = MIN_INTERVAL - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self._last = time.time()
                    return json.loads(r.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as e:
                self._last = time.time()
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:400]
                except Exception:                                  # noqa: BLE001
                    pass
                if e.code in (401, 403):
                    raise GoatError(
                        "GoatCounter refused the token (%s). Create one under "
                        "[username] -> API in your GoatCounter settings, then "
                        "run `vantage site --login`." % e.code)
                if e.code == 404:
                    raise GoatError(
                        "no GoatCounter site at %s -- check the site name." % self.site)
                # 429 is the documented rate limit; 5xx is theirs, not ours.
                if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise GoatError("GoatCounter %s: %s" % (e.code, body or url))
            except urllib.error.URLError as e:
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise GoatError("could not reach %s: %s" % (self.site, e.reason))
        raise GoatError("gave up on " + url)

    # -- endpoints -------------------------------------------------------

    def hits(self, start, end, limit=200):
        """Paths and events with visitor counts, plus a daily series each.

        Events (the link clicks) come back in the same list as page views,
        told apart by the `event` flag rather than by a separate call.
        """
        out = []
        exclude = []
        while True:
            params = {"start": start, "end": end, "limit": limit, "daily": "true"}
            if exclude:
                # Documented pagination: exclude what you already have.
                params["exclude_paths"] = ",".join(str(i) for i in exclude)
            data = self.get("/stats/hits", params)
            batch = data.get("hits") or []
            out.extend(batch)
            if not data.get("more") or not batch:
                break
            exclude.extend(h.get("path_id") for h in batch if h.get("path_id"))
            if len(out) > 5000:      # a portfolio has nothing like this many
                break
        return out

    def toprefs(self, start, end, limit=100):
        data = self.get("/stats/toprefs", {"start": start, "end": end, "limit": limit})
        return data.get("stats") or data.get("refs") or []

    def total(self, start, end):
        return self.get("/stats/total", {"start": start, "end": end})


def client(conn):
    site, token = config(conn)
    return Client(site, token)
