"""The local dashboard.

A stdlib HTTP server bound to loopback, serving one static page plus a small
JSON API over the same SQLite store the CLI writes. No framework, no build
step, no CDN - `vantage serve` and the browser opens.

Two hardening details, because a server on localhost is still a server any
page in your browser can try to reach:
  * the Host header must be a loopback name, which blocks DNS rebinding;
  * mutating routes require a custom header, which a cross-origin form or
    <img> cannot set without a preflight the server never approves.
"""

import json
import os
import posixpath
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, analyze, store

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
GUARD_HEADER = "X-Vantage-Local"

_local = threading.local()


def _conn(db):
    """One SQLite connection per handler thread."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = store.connect(db)
        _local.conn = c
    return c


class Handler(BaseHTTPRequestHandler):
    server_version = "vantage/" + __version__
    db = None
    default_days = 30

    # --------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):
        if os.environ.get("VANTAGE_VERBOSE"):
            super().log_message(fmt, *args)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1", "")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # The page is entirely self-contained; forbid anything external.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src data:; connect-src 'self'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, default=str))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 1 << 20:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    # ------------------------------------------------------------ routes
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain")
        path, _, query = self.path.partition("?")
        params = _parse_query(query)

        if path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if path == "/api/data":
            days = _int(params.get("days"), self.default_days)
            repo = params.get("repo") or None
            data = analyze.build(_conn(self.db), days=days, repo=repo)
            data["db_path"] = self.db
            return self._json(200, data)
        if path == "/api/health":
            return self._json(200, {"ok": True, "version": __version__})
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, "forbidden", "text/plain")
        if self.headers.get(GUARD_HEADER) != "1":
            return self._json(403, {"error": "missing local guard header"})
        path = self.path.partition("?")[0]
        conn = _conn(self.db)

        if path == "/api/sync":
            return self._sync(conn)
        if path == "/api/note":
            b = self._body()
            label = (b.get("label") or "").strip()[:200]
            day = (b.get("day") or store.today())[:10]
            if not label:
                return self._json(400, {"error": "label required"})
            eid = store.add_event(conn, day, label,
                                  (b.get("kind") or "application")[:40],
                                  (b.get("repo") or "")[:200],
                                  (b.get("note") or "")[:1000])
            conn.commit()
            return self._json(200, {"id": eid})
        if path == "/api/note/delete":
            b = self._body()
            n = store.delete_event(conn, int(b.get("id") or 0))
            conn.commit()
            return self._json(200, {"deleted": n})
        return self._send(404, "not found", "text/plain")

    def _sync(self, conn):
        import datetime as dt
        import time

        from . import gh
        t0 = time.time()
        started = dt.datetime.now().isoformat(timespec="seconds")
        try:
            tok = gh.token()
            repos = gh.list_repos(tok)
        except gh.GhError as e:
            return self._json(500, {"error": str(e)})
        ok = failed = 0
        for repo, data, err in gh.fetch_all(tok, repos, workers=8):
            store.upsert_repo(conn, repo, err)
            if err:
                failed += 1
                continue
            store.save_traffic(conn, repo["name"], data)
            ok += 1
        conn.commit()
        secs = time.time() - t0
        store.record_sync(conn, started,
                          dt.datetime.now().isoformat(timespec="seconds"),
                          len(repos), ok, failed, secs)
        conn.commit()
        return self._json(200, {"ok": ok, "failed": failed,
                                "seconds": round(secs, 2)})

    def _file(self, name, ctype):
        # posixpath.basename pins us inside assets/ regardless of the request.
        safe = posixpath.basename(name)
        full = os.path.join(ASSETS, safe)
        if not os.path.isfile(full):
            return self._send(404, "not found", "text/plain")
        with open(full, "rb") as fh:
            return self._send(200, fh.read(), ctype)


def _parse_query(q):
    out = {}
    for pair in q.split("&"):
        if not pair:
            continue
        k, _, v = pair.partition("=")
        from urllib.parse import unquote_plus
        out[unquote_plus(k)] = unquote_plus(v)
    return out


def _int(v, default):
    try:
        return max(1, min(3650, int(v)))
    except (TypeError, ValueError):
        return default


def serve(port=7373, host="127.0.0.1", days=30, open_browser=True, db=None):
    db = db or store.default_path()
    store.connect(db).close()  # create the schema before the first request

    Handler.db = db
    Handler.default_days = days

    for attempt in range(10):
        try:
            httpd = ThreadingHTTPServer((host, port + attempt), Handler)
            break
        except OSError:
            if attempt == 9:
                print("could not bind a port in %d-%d" % (port, port + 9))
                return 1
    else:
        return 1

    url = "http://%s:%d/" % (host, httpd.server_address[1])
    print("  vantage dashboard  %s" % url)
    print("  serving %s   (ctrl-c to stop)" % db)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        httpd.server_close()
    return 0
