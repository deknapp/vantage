# vantage

**See who is looking at your GitHub.** A fast terminal summary and a local
dashboard, built on the `gh` CLI. No dependencies, no accounts, no data leaving
your machine.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/dashboard-dark.png">
  <img alt="The vantage dashboard, showing a verdict banner, stat tiles, a daily traffic chart, and referrers ranked by how strongly they imply someone is evaluating you" src="docs/dashboard-light.png">
</picture>

<sub>Screenshot uses `vantage demo` — generated data, not real traffic.</sub>

```console
$ vantage
  ✓ tide-gauge     1/6
  ✓ otter          2/6
  …
  synced 6 repos in 3.1s

  ●  Yes - 3 visits came from boards.greenhouse.io, which is a hiring platform.

  163 views  -16   53 unique visitors  -6   780 clones

  Today · 2026-09-08
    leafwise     4 views · 3 unique visitors
    tide-gauge   4 views · 2 unique visitors
```

## Why

GitHub shows you repo traffic, but with two problems:

1. **It deletes it after 14 days.** There is no API to get it back. If you did
   not look, it never happened.
2. **It shows one repo at a time,** as raw counts, with no read on what they
   mean. Fifty views from Google and three from a Greenhouse job board look
   identical on that page — but only one of them means somebody is deciding
   whether to interview you.

vantage fixes both. Every sync snapshots all your repos into a local SQLite
database, so history accumulates for as long as you keep running it. Then it
grades each referring domain by how strongly it implies evaluation rather than
passing traffic, and gives you the answer as a sentence.

## Install

Needs Python 3.8+, and the [GitHub CLI](https://cli.github.com) already logged
in (`gh auth login`). vantage borrows `gh`'s token — there is nothing else to
set up.

```bash
git clone https://github.com/deknapp/vantage
cd vantage
pip install -e .          # or: pipx install .
```

Or skip installing entirely — it is stdlib-only, so it runs from the checkout,
and the bundled launcher can be symlinked straight onto your PATH:

```bash
python3 -m vantage                              # run in place
ln -s "$PWD/bin/vantage" /usr/local/bin/vantage # or put it on PATH
```

## Use

```bash
vantage                 # sync, then print the report — the one you'll type
vantage today           # which repos were viewed today, and the week around it
vantage sync            # fetch only
vantage report          # print only
vantage serve           # open the dashboard at localhost:7373
vantage demo            # synthetic data, to see it all without waiting 14 days
vantage schedule        # install a daily sync, and check it is still firing
```

Mark the days that matter, and the timeline shows whether anything followed:

```bash
vantage note "Applied to Northbridge — staff platform engineer"
vantage note "Sent GitHub link to a recruiter" --date 2026-09-02 --kind outreach
```

Other useful flags:

```bash
vantage report --days 365 --repo deknapp/taper
vantage report --json | jq .signal        # the whole payload, for scripting
vantage sync --visibility public          # skip private repos
vantage sync --owner someone-else         # needs push access to their repos
vantage export --days 365 -o traffic.csv
```

Run it on a schedule, so the window never lapses:

```bash
vantage schedule install        # daily at 09:00
vantage schedule install --at 07:30
vantage schedule                # is it still firing, and how much slack is left
vantage schedule remove
```

## The 14-day window, and what a gap actually costs

GitHub serves only the last 14 days of traffic and deletes the rest. That is
the reason this tool stores anything at all — but the usual advice that follows
("sync every day or lose data") is wrong, and worth being precise about,
because the precise version is less alarming and more useful.

Every sync re-fetches the **whole** 14-day window, and `save_daily` keeps the
larger of the stored and fetched value for each day. So a missed day costs
nothing: tomorrow's sync backfills it. What is unrecoverable is a gap **longer
than the window** — those days aged out of GitHub before anything asked for
them. `vantage schedule` reports the distance to that cliff rather than nagging
about yesterday:

```console
$ vantage schedule
  scheduled daily at 09:15 (launchd, loaded)
    ~/Library/LaunchAgents/com.vantage.sync.plist

  Last sync today (2026-09-08 14:24).
  14 days of slack: each sync re-fetches the whole 14-day window,
  so a missed day costs nothing until the gap reaches 14.
```

Referrers and paths are the weaker case, and the status text does not pretend
otherwise: they are snapshots of a rolling top-10, not a day series, so a
referrer that appears and disappears inside a gap is never recorded at all.
That — not the day counts — is the real argument for syncing daily.

### Why launchd on macOS, and not cron

`vantage schedule` writes a launchd agent on macOS and a marked crontab block
elsewhere. The difference is not neatness. **cron silently skips a job whose
time passed while the machine was asleep**, and a laptop with a closed lid at
09:00 every day is precisely how a 14-day gap accumulates without anyone
noticing. launchd remembers a missed `StartCalendarInterval` and runs it on
wake, which is the behaviour this tool needs.

Two environment pins in the generated job are load-bearing, and both were
verified by running the installed agent and then breaking each one:

| Pin | Without it |
|---|---|
| `PATH` includes wherever `gh` actually lives | `gh CLI not found` — launchd and cron both run with a minimal PATH that excludes Homebrew, and auth is `gh auth token` |
| `PYTHONPATH` set to the package root | `No module named vantage` — the shipped `bin/vantage` launcher runs from a checkout by setting `PYTHONPATH` itself, so `python -m vantage` finds nothing without the same hint |

Neither failure is visible at install time. The job installs cleanly, reports
success, and then fails every night into a log nobody reads — so both are
asserted in the tests rather than left to be discovered in a month with a hole
in the history.

## Views, visitors, and the difference

Three numbers get called "visitors" in traffic tools and they are not the same
number. vantage names each one and never substitutes one for another:

| | What it counts | Where it comes from |
|---|---|---|
| **Page views** | Every page load, including repeats. | Summed from the day series. |
| **Unique visitors** | People, de-duplicated across the whole 14 days, per repo. | GitHub's own window figure, stored verbatim. |
| **Visitor-days** | Daily uniques added up — someone who came back on three days counts three times. | Summed from the day series. |

Only the middle one is close to a headcount, and it is the one the report leads
with. It cannot be computed from the daily numbers — GitHub calculates it over
the whole fortnight and returns it alongside the day series, so vantage keeps it
as sent. The other two are useful and are labelled as what they are.

Two honest limits on even the good number: it is summed across repos, so one
person who reads three of your repos counts three times (GitHub never exposes a
cross-repo de-duplicated figure), and it only exists for GitHub's rolling 14
days, so there is no 90-day equivalent.

## Which repos were read today

`vantage today` answers the small question you actually ask several times a day,
and it syncs first so the answer is not stale:

```console
$ vantage today

  Today · 2026-09-08 · last sync 2026-09-08 09:12:04

    chilecule     6 views · 2 unique visitors
    polarswim     1 view  · 1 unique visitor

  Views per day, by repo  ·  views (unique visitors that day)

  repo             Wed 02  Thu 03  Fri 04  Sat 05  Sun 06  Mon 07   today
  chilecule            ·       ·   6 (2)   4 (1)   1 (1)   1 (1)   6 (2)
  polarswim        3 (1)       ·   5 (1)   3 (1)   1 (1)   1 (1)   1 (1)
  taper                ·       ·   3 (1)       ·   1 (1)       ·       ·
  ─────────────────────────────────────────────────────────────────────
  all repos        3 (1)       ·  14 (4)   9 (4)   5 (5)   7 (5)   7 (3)
```

A `·` means nobody. An empty day is stated rather than left blank, and so is a
day that has not been synced — GitHub counts days in UTC and lags a few hours,
so a quiet morning is not yet evidence of a quiet day.

The same grid, and a Today card, are the first two panels on the dashboard.

## How the signal is scored

GitHub reports the referring **domain**, never a person or a company. So the
question "is an employer looking?" can only ever be answered by inference. The
inference vantage makes is that some domains only exist inside a hiring
workflow:

| Bucket | Weight | Why |
|---|---|---|
| Hiring platform (Lever, Greenhouse, Ashby, Workday, iCIMS…) | 1.00 | Those pages are seen by recruiters and hiring managers, not the public. Close to direct evidence. |
| Professional network (LinkedIn) · Email & chat | 0.80 | Someone clicked through from your profile, or your link is being passed around. |
| Job board (Indeed, Glassdoor, Built In…) | 0.70 | Job-seeking context, but public. |
| Other / unrecognised | 0.40 | A company's own site or internal wiki lands here. Worth a look. |
| Search · AI assistant | 0.30 | Ordinary discovery. Tracks findability, not who. |
| Social & community | 0.20 | Public sharing. One post can dwarf everything else. |
| GitHub itself | 0.05 | Mostly your own browsing. |

Subdomains match on label boundaries, so `acme.myworkdayjobs.com` and
`boards.greenhouse.io` are both recognised, while `notlever.co` is not.

Alongside that, **reading depth** — the share of page views that land on source
files, directory trees, commits, or releases rather than the README. A visitor
who opens `queue.go` is evaluating you; one who bounces off the landing page is
not. Your own visits to `/graphs/traffic` and `/pulse` are detected and held out
of the ratio, since GitHub counts the owner like anyone else.

The dashboard encodes all of this in one channel: referrer bars use an ordinal
blue ramp where **darker means stronger evidence**, so a short dark bar
(three visits from Greenhouse) visibly outranks a long pale one (fifty from
Hacker News).

## What this cannot tell you

Stated plainly, because a tool that implies more than it knows is worse than no
tool:

- **No referrer identifies anyone.** Every reading here is an inference from a
  domain name. A Greenhouse referral is strong evidence *somebody* in a hiring
  workflow clicked; it is not proof, and it never names them.
- **History starts when you start.** Days before your first sync are gone. The
  chart fills in from here.
- **Unique visitors are per repo, not per person across your account.** One
  person who reads three of your repos is three uniques in the headline. It is
  an upper bound; GitHub publishes no cross-repo de-duplicated count.
- **Only the 14-day window has a real unique count.** Anything longer can only
  be visitor-days — daily uniques summed, which counts a returning visitor once
  per day they came back. The report says which of the two it is showing.
- **Referrers and paths are a rolling 14-day top ten,** not a log, and not
  per-day. vantage keeps every snapshot, which is how it can tell you when a
  domain *first* appeared, but it cannot rebuild the days in between.
- **Clone counts include CI, mirrors, and bots,** so they run high and mean much
  less than views.
- **Traffic needs push access,** so this works for your own repos and ones you
  collaborate on. Nobody else's.

## Design notes

A few decisions worth the words, since this doubles as a work sample:

**Auth by borrowing, not by asking.** vantage shells out to `gh auth token`
once, then talks to the REST API over `urllib`. Running `gh api` per request
costs ~200ms of process startup each, and a sync is four endpoints per repo —
that dominates everything. One token plus a thread pool turns a 30-second sync
into a 3-second one. There is no separate login, and no token stored anywhere.

**The database is the product.** The API is a 14-day window sliding over data
GitHub throws away. Everything else follows from treating each sync as an
append to permanent local history. Re-syncing is idempotent, and a day's counts
are stored with `MAX(old, new)` so a partial response can never shrink a day
already recorded.

**Zero dependencies, deliberately.** Standard library only — `sqlite3`,
`urllib`, `http.server`, hand-written SVG. The dashboard loads no CDN and
survives being offline. It also means `git clone && python3 -m vantage` works
on a machine you have never set up.

**Local means local.** The server binds loopback, rejects any `Host` header that
is not a loopback name (blocking DNS rebinding), requires a custom header on
mutating routes (which a cross-origin page cannot set without a preflight that
never succeeds), and sends a CSP that forbids every external origin.

**The charts are built to a spec.** One shared axis, never two. Categorical hues
in a fixed order, never cycled. Palettes checked for colour-vision separation
rather than eyeballed, in both light and dark. A legend whenever there is more
than one series, direct labels used sparingly, and a table view under every
chart so nothing is gated behind colour or hover.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Covers domain classification and its boundary cases, the storage semantics that
make re-syncing safe, the aggregation both front-ends read, and — pinned
deliberately — that a visitor-day is never printed under the word "visitors",
and that a day with no traffic says so instead of rendering as nothing.

## License

MIT
