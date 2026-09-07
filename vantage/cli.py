"""vantage command line."""

import argparse
import datetime as dt
import json
import os
import sys
import time
import webbrowser

from . import __version__, analyze, gh, report, store


def _eprint(*a):
    print(*a, file=sys.stderr)


# ------------------------------------------------------------------ sync

def cmd_sync(args, conn):
    ink = report.Ink(report.use_colour(None if args.color is None else args.color))
    t0 = time.time()
    started = dt.datetime.now().isoformat(timespec="seconds")
    try:
        tok = gh.token()
    except gh.GhError as e:
        _eprint(ink.red("error: ") + str(e))
        return 2

    quiet = args.quiet
    if not quiet:
        _eprint(ink.dim("  discovering repos…"))
    try:
        repos = gh.list_repos(
            tok, owner=args.owner,
            affiliation=args.affiliation,
            include_forks=args.include_forks,
            include_archived=args.include_archived,
            visibility=args.visibility,
        )
    except gh.GhError as e:
        _eprint(ink.red("error: ") + str(e))
        return 2

    if args.repo:
        wanted = set(args.repo)
        repos = [r for r in repos
                 if r["name"] in wanted or r["name"].split("/")[-1] in wanted]

    if not repos:
        _eprint(ink.amber("  no repos matched."))
        return 1

    n_ok = n_failed = 0
    total = len(repos)
    width = max((len(r["name"].split("/")[-1]) for r in repos), default=10)

    def progress(i, n, name, err):
        if quiet:
            return
        short = name.split("/")[-1]
        mark = ink.red("✗") if err else ink.green("✓")
        line = "  %s %-*s %s" % (mark, width, short, ink.dim("%d/%d" % (i, n)))
        if err:
            line += ink.dim("  " + err)
        _eprint(line)

    for repo, data, err in gh.fetch_all(tok, repos, workers=args.workers,
                                        progress=progress):
        store.upsert_repo(conn, repo, err)
        if err:
            n_failed += 1
            continue
        store.save_traffic(conn, repo["name"], data)
        n_ok += 1
    conn.commit()

    elapsed = time.time() - t0
    store.record_sync(conn, started,
                      dt.datetime.now().isoformat(timespec="seconds"),
                      total, n_ok, n_failed, elapsed)
    conn.commit()
    if not quiet:
        _eprint(ink.dim("  synced %d repo%s in %.1fs%s" % (
            n_ok, "" if n_ok == 1 else "s", elapsed,
            (", %d failed" % n_failed) if n_failed else "")))
    return 0


# ---------------------------------------------------------------- report

def cmd_report(args, conn):
    data = analyze.build(conn, days=args.days, repo=args.repo)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    if not data["coverage"]["first_day"]:
        _eprint("No data yet. Run `vantage sync` first.")
        return 1
    ink = report.Ink(report.use_colour(None if args.color is None else args.color))
    print(report.render(data, ink, top=args.top))
    return 0


# ----------------------------------------------------------------- serve

def cmd_serve(args, conn):
    from . import web
    conn.close()  # the server opens its own per-thread connections
    return web.serve(port=args.port, host=args.host, days=args.days,
                     open_browser=not args.no_open, db=args.db)


# ----------------------------------------------------------------- notes

def cmd_note(args, conn):
    if args.remove:
        n = store.delete_event(conn, args.remove)
        conn.commit()
        print("removed" if n else "no such marker")
        return 0 if n else 1
    if args.list or not args.label:
        rows = store.events(conn)
        if not rows:
            print("No markers yet.  vantage note 'Applied to Acme' --date 2026-09-05")
            return 0
        for e in rows:
            print("%4d  %s  %-34s %s" % (e["id"], e["day"], e["label"], e["kind"]))
        return 0
    day = args.date or store.today()
    try:
        dt.date.fromisoformat(day)
    except ValueError:
        _eprint("error: --date must be YYYY-MM-DD")
        return 2
    eid = store.add_event(conn, day, " ".join(args.label), args.kind,
                          args.repo or "", args.note or "")
    conn.commit()
    print("marker %d added on %s" % (eid, day))
    return 0


def cmd_repos(args, conn):
    rows = store.repos(conn)
    if not rows:
        print("No repos known yet. Run `vantage sync`.")
        return 1
    for r in rows:
        flag = "!" if r["last_error"] else " "
        print("%s %-40s %-8s %s" % (flag, r["name"], r["visibility"],
                                    r["last_error"] or ""))
    return 0


def cmd_export(args, conn):
    import csv
    data = analyze.build(conn, days=args.days)
    out = open(args.out, "w", newline="") if args.out else sys.stdout
    try:
        w = csv.writer(out)
        w.writerow(["day", "repo", "metric", "count", "uniques"])
        rows = conn.execute(
            "SELECT day, repo, metric, count, uniques FROM daily "
            "WHERE day>=? ORDER BY day, repo, metric",
            (store.since_day(args.days),))
        for r in rows:
            w.writerow([r["day"], r["repo"], r["metric"], r["count"], r["uniques"]])
    finally:
        if args.out:
            out.close()
            _eprint("wrote " + args.out)
    return 0


def cmd_db(args, conn):
    print(args.db or store.default_path())
    return 0


# ------------------------------------------------------------------ main

def build_parser():
    # Shared flags, accepted either before or after the subcommand. SUPPRESS
    # keeps an unset subcommand flag from clobbering the value given globally.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS,
                        help="SQLite path (default ~/.vantage/vantage.db)")
    common.add_argument("--color", dest="color", action="store_true",
                        default=argparse.SUPPRESS, help="force colour output")
    common.add_argument("--no-color", dest="color", action="store_false",
                        default=argparse.SUPPRESS, help="disable colour output")

    p = argparse.ArgumentParser(
        prog="vantage",
        parents=[common],
        description="Who is looking at your GitHub repos. Uses the gh CLI for auth.",
        epilog="With no command, vantage syncs and then prints the report.",
    )
    p.add_argument("--version", action="version", version="vantage " + __version__)
    p.set_defaults(db=None, color=None, cmd=None)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("sync", parents=[common], help="fetch traffic from GitHub into the local store")
    s.add_argument("--owner", help="fetch a specific user's repos instead of yours")
    s.add_argument("--repo", action="append",
                   help="limit to a repo (repeatable; name or owner/name)")
    s.add_argument("--affiliation", default="owner",
                   help="owner | owner,collaborator | owner,organization_member")
    s.add_argument("--visibility", default="all",
                   choices=["all", "public", "private"])
    s.add_argument("--include-forks", action="store_true")
    s.add_argument("--include-archived", action="store_true")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_sync)

    r = sub.add_parser("report", parents=[common], help="print the terminal summary")
    r.add_argument("--days", type=int, default=90)
    r.add_argument("--repo", help="limit to one repo (owner/name)")
    r.add_argument("--top", type=int, default=10)
    r.add_argument("--json", action="store_true", help="emit the raw payload")
    r.set_defaults(func=cmd_report)

    v = sub.add_parser("serve", parents=[common], help="launch the local dashboard")
    v.add_argument("--port", type=int, default=7373)
    v.add_argument("--host", default="127.0.0.1")
    v.add_argument("--days", type=int, default=90)
    v.add_argument("--no-open", action="store_true", help="don't open a browser")
    v.set_defaults(func=cmd_serve)

    n = sub.add_parser("note", parents=[common], help="mark a day ('applied to X') to correlate with spikes")
    n.add_argument("label", nargs="*")
    n.add_argument("--date", help="YYYY-MM-DD (default today)")
    n.add_argument("--kind", default="application",
                   help="application | outreach | post | other")
    n.add_argument("--repo", help="associate with a repo")
    n.add_argument("--note", help="longer free-text note")
    n.add_argument("--list", action="store_true", help="list markers")
    n.add_argument("--remove", type=int, metavar="ID", help="delete a marker")
    n.set_defaults(func=cmd_note)

    rp = sub.add_parser("repos", parents=[common], help="list tracked repos and any sync errors")
    rp.set_defaults(func=cmd_repos)

    e = sub.add_parser("export", parents=[common], help="dump the daily table as CSV")
    e.add_argument("--days", type=int, default=365)
    e.add_argument("-o", "--out", help="file to write (default stdout)")
    e.set_defaults(func=cmd_export)

    d = sub.add_parser("db", parents=[common], help="print the database path")
    d.set_defaults(func=cmd_db)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    db = args.db or store.default_path()
    args.db = db
    conn = store.connect(db)
    try:
        if not args.cmd:
            # Bare `vantage`: sync, then report. The thing you actually want.
            sync_args = parser.parse_args(["sync"])
            sync_args.db = db
            sync_args.color = args.color
            rc = cmd_sync(sync_args, conn)
            if rc != 0:
                return rc
            rep_args = parser.parse_args(["report"])
            rep_args.db = db
            rep_args.color = args.color
            return cmd_report(rep_args, conn)
        return args.func(args, conn)
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
