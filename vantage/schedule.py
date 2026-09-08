"""Install a daily sync, so the 14-day window never runs out.

GitHub's traffic API only serves the last 14 days. Every sync re-fetches that
whole window and `store.save_daily` keeps the larger of old and new, so missing
a day costs nothing - the next sync backfills it. What is unrecoverable is a
gap longer than the window: those days are gone from GitHub before anything
asks for them. Referrers and paths are weaker still, since they are snapshots
of a rolling top-10 rather than a day series - a referrer that appears and
disappears inside a gap is never recorded at all.

So the job of a scheduler here is not "sync every day or lose data". It is
"never go 14 days without syncing", and the status output says which of those
two situations you are actually in.

macOS gets a launchd agent rather than a crontab line, and the difference
matters on a laptop: cron simply skips a job whose time passed while the
machine was asleep, and a closed lid at 09:00 every day is exactly how a
14-day gap accumulates without anyone noticing. launchd remembers a missed
StartCalendarInterval and runs it once the machine wakes.

The other thing that has to be right is PATH. launchd and cron both run with a
minimal environment that does not include Homebrew, and `gh.token()` shells out
to `gh` by bare name - so a scheduler written the obvious way installs cleanly,
runs daily, and fails every single time. Both writers below pin an explicit
PATH built from where `gh` actually is on this machine.
"""

import datetime as dt
import os
import plistlib
import shutil
import subprocess
import sys

LABEL = "com.vantage.sync"
MARK_BEGIN = "# >>> vantage sync >>>"
MARK_END = "# <<< vantage sync <<<"

WINDOW_DAYS = 14  # GitHub's traffic retention


# ------------------------------------------------------------------ shared

def log_path():
    return os.path.expanduser("~/.vantage/logs/sync.log")


def command(db=None):
    """The argv the scheduler should run.

    `sys.executable -m vantage` rather than the `vantage` script: the console
    script may live in a venv or pipx dir that is not on the scheduler's PATH,
    while sys.executable is an absolute path that is by construction the
    interpreter this package is importable from.
    """
    argv = [sys.executable, "-m", "vantage", "sync", "--quiet"]
    if db:
        argv += ["--db", db]
    return argv


def package_root():
    """The directory `vantage` is importable from.

    Not decoration: the shipped `bin/vantage` launcher runs the package
    straight out of a checkout by setting PYTHONPATH itself, so on that install
    - which is the documented one - `python -m vantage` finds nothing unless
    the scheduler passes the same hint. Pinning it is a no-op for a pip install,
    where this resolves to site-packages.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def search_path():
    """PATH for the job: the system default plus wherever `gh` really is."""
    base = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    gh = shutil.which("gh")
    if gh:
        d = os.path.dirname(gh)
        if d not in base:
            base.insert(0, d)
    return ":".join(base)


def gh_warning():
    """Non-fatal warnings to show at install time, so a silent failure is loud now."""
    out = []
    if not shutil.which("gh"):
        out.append("gh is not on your PATH - the scheduled sync will fail until "
                   "it is installed and `gh auth login` has been run.")
    return out


# ------------------------------------------------------------------ macOS

def _plist_path():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)


def _launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install_launchd(hour, minute, db=None):
    path = _plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(os.path.dirname(log_path()), exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": command(db),
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "EnvironmentVariables": {
            "PATH": search_path(),
            "HOME": os.path.expanduser("~"),
            "PYTHONPATH": package_root(),
        },
        "StandardOutPath": log_path(),
        "StandardErrorPath": log_path(),
        "RunAtLoad": False,
        "ProcessType": "Background",
    }
    with open(path, "wb") as fh:
        plistlib.dump(plist, fh)

    uid = os.getuid()
    domain = "gui/%d" % uid
    # bootout first so a re-install replaces rather than collides; it fails
    # harmlessly when nothing is loaded.
    _launchctl("bootout", "%s/%s" % (domain, LABEL))
    r = _launchctl("bootstrap", domain, path)
    if r.returncode != 0:
        # Older macOS wants load -w; try it before giving up.
        r2 = _launchctl("load", "-w", path)
        if r2.returncode != 0:
            return False, (r.stderr or r2.stderr or "launchctl refused the job").strip()
    return True, path


def remove_launchd():
    path = _plist_path()
    uid = os.getuid()
    _launchctl("bootout", "gui/%d/%s" % (uid, LABEL))
    _launchctl("unload", "-w", path)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def launchd_installed():
    path = _plist_path()
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        try:
            pl = plistlib.load(fh)
        except Exception:
            return {"path": path, "when": "?", "argv": [], "loaded": False}
    cal = pl.get("StartCalendarInterval") or {}
    loaded = _launchctl("print", "gui/%d/%s" % (os.getuid(), LABEL)).returncode == 0
    return {
        "path": path,
        "when": "%02d:%02d" % (cal.get("Hour", 0), cal.get("Minute", 0)),
        "argv": pl.get("ProgramArguments", []),
        "loaded": loaded,
    }


# ------------------------------------------------------------------- cron

def _crontab_read():
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    # An empty crontab exits non-zero with "no crontab for <user>".
    return r.stdout if r.returncode == 0 else ""


def _crontab_write(text):
    r = subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or "").strip()


def _strip_block(text):
    out, skipping = [], False
    for line in text.splitlines():
        if line.strip() == MARK_BEGIN:
            skipping = True
            continue
        if line.strip() == MARK_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip()


def install_cron(hour, minute, db=None):
    argv = " ".join(command(db))
    block = "\n".join([
        MARK_BEGIN,
        "PATH=" + search_path(),
        "PYTHONPATH=" + package_root(),
        "%d %d * * * %s >> %s 2>&1" % (int(minute), int(hour), argv, log_path()),
        MARK_END,
    ])
    os.makedirs(os.path.dirname(log_path()), exist_ok=True)
    body = _strip_block(_crontab_read())
    text = (body + "\n\n" if body else "") + block + "\n"
    ok, err = _crontab_write(text)
    return ok, (err or "crontab") if not ok else "crontab"


def remove_cron():
    current = _crontab_read()
    if MARK_BEGIN not in current:
        return False
    ok, _ = _crontab_write(_strip_block(current) + "\n")
    return ok


def cron_installed():
    text = _crontab_read()
    if MARK_BEGIN not in text:
        return None
    when, argv = "?", []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) > 5 and parts[2] == "*" and "vantage" in line:
            when = "%02d:%02d" % (int(parts[1]), int(parts[0]))
            argv = parts[5:]
            break
    return {"path": "crontab", "when": when, "argv": argv, "loaded": True}


# ---------------------------------------------------------------- generic

def backend():
    return "launchd" if sys.platform == "darwin" else "cron"


def install(hour, minute, db=None):
    if backend() == "launchd":
        return install_launchd(hour, minute, db)
    return install_cron(hour, minute, db)


def remove():
    return remove_launchd() if backend() == "launchd" else remove_cron()


def installed():
    return launchd_installed() if backend() == "launchd" else cron_installed()


# ---------------------------------------------------------------- health

def health(conn):
    """How close the store is to losing data, in the store's own terms."""
    row = conn.execute(
        "SELECT finished_at, n_ok, n_failed FROM syncs "
        "ORDER BY id DESC LIMIT 1").fetchone()
    last = row["finished_at"] if row else None
    days_since = None
    if last:
        try:
            days_since = (dt.date.today() - dt.date.fromisoformat(last[:10])).days
        except ValueError:
            days_since = None

    # Days inside the window that the store has no row for at all. These are
    # still recoverable: the next sync re-fetches the whole window.
    have = {r["day"] for r in conn.execute(
        "SELECT DISTINCT day FROM daily WHERE day >= ?",
        ((dt.date.today() - dt.timedelta(days=WINDOW_DAYS)).isoformat(),))}
    missing = []
    for i in range(1, WINDOW_DAYS + 1):
        d = (dt.date.today() - dt.timedelta(days=i)).isoformat()
        if d not in have:
            missing.append(d)

    return {
        "last_sync": last,
        "days_since": days_since,
        "slack_days": None if days_since is None else max(0, WINDOW_DAYS - days_since),
        "missing_in_window": sorted(missing),
        "n_failed": row["n_failed"] if row else 0,
    }
