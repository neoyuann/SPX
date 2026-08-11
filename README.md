# Corporate event monitor

Tracks scheduled and announced company events — earnings calls, guidance changes,
investor days, capital markets days, M&A, AGMs, sales results and the rest — from
official sources only, and serves them as an interactive page.

The point isn't just to collect events. It's to notice when one **moves**: a
capital markets day pushed back three weeks, guidance quietly revised, an AGM
date changed. That's what the database is for.

`companies.yaml` currently tracks roughly 2,900 companies across Korea, Japan,
China, Taiwan, Hong Kong, the US and a handful of European names, bulk-loaded
from a roster spreadsheet (see **Bulk-importing a roster spreadsheet** below)
plus a few hand-tuned entries with real IR feeds. History goes back to
`run.history_from` (2020-01-01 by default) as far as each company's sources
actually reach — see **Only official sources** below for what that means in
practice per market.

---

## Start here

**Double-click `START TRACKER (Windows).bat`.**

It installs what it needs the first time, starts the tracker, and opens the
dashboard in your browser. Leave the terminal window it opens alone; closing
that window stops the tracker.

If Windows shows a SmartScreen warning on first run (it does this for any
downloaded `.bat`), click **More info → Run anyway**.

Before your first real run, open `config.yaml` and put a real email address in
`run.user_agent` — SEC EDGAR rejects requests without one.

### Why there's no dashboard.html to open directly

Earlier versions shipped one, and it caused real confusion: **Refresh now** did
nothing and **+ Add / remove companies** couldn't save anything. That wasn't a
bug. A saved HTML file is just text on disk — browsers block it from fetching
st.com or sec.gov (that's CORS, a security rule no code can work around), and it
has no way to write to your files. Both buttons need a running program behind
them.

So the launcher is the only entry point now. Start it, and everything works:
refresh fetches live, and the company panel edits your real tracked list.

The one exception is the copy the GitHub Pages workflow publishes for you —
see **Sharing this with someone who won't install anything** below.

---

## How refreshing works

**Every visit rebuilds the page from the database and serves it instantly.**
Visiting the page, or hitting your browser's refresh button, never itself
starts a scrape — you're never waiting on one just because you opened a tab.
What you see is whatever the tracker already knows, which — as long as
`serve` has been running for a while — is current, because of the next part:

**The tracker refreshes itself continuously in the background, on its own,
whether or not anyone's looking.** The first cycle starts the moment `serve`
launches, not at a fixed time of day, and it repeats every
`refresh.background_interval_minutes` (default 60) for as long as the process
keeps running. So: start the tracker *before* you plan to check it — leave
`START TRACKER (Windows).bat` open, or host `serve` somewhere always-on (see
**Sharing this online** below) — and by the time you open the browser, it's
already been keeping itself up to date. A roster this size (~2,900 companies)
takes roughly 20 minutes for one full cycle; with the default 60-minute
interval that's about 40 minutes idle between cycles, so data is never more
than about an hour old if the server's been running that whole time.

**Refresh now** forces an *extra* cycle immediately, on top of the background
one — useful for "I want the very latest, right now," not something you need
to press just to see data at all. While a cycle runs (background-triggered or
button-triggered — the bar looks the same either way, since a page visit
never knows or cares which one is running), the bar shows live progress per
source — `54 of 4142 sources · 006800.KS · news` — so you can see it moving,
not just guess that it hasn't hung. Once idle, the bar shows both when it last
finished and when the next background cycle is due: `Last refreshed
2026-08-07 14:52 SGT · next automatic refresh 2026-08-07 15:52 SGT`.

### If refresh wasn't completing for you

Two real bugs were behind that, both now fixed and tested:

1. **No bound on how long a single request could take.** A slow or
   silently-dropping IR site could eat its full timeout on both the connect
   and the read side, and with nine companies across several sources each,
   that added up to a refresh that might never visibly finish. Timeouts are
   now split and tightened (`request_timeout: [6, 15]` — 6s to connect, 15s to
   receive), so a bad host fails fast instead of stalling everything behind it.
2. **No bound on the whole run.** Even with tighter per-request timeouts, nine
   companies times several sources times a worst-case timeout each could still
   run long. `run.max_run_seconds` (default 240) now caps the whole thing —
   once hit, remaining companies are skipped and named in the run's notes, to
   be picked up on the next refresh, rather than left to run indefinitely.

Both are enforced in code, not just documented — a source that hangs for 20
seconds against a 2-second timeout fails in 2 seconds; a run given a 2.5-second
budget against three slow sources processes what it can and skips the rest.

A related bug meant that if the *page's own status check* hit any transient
error while a refresh was running, the bar would wrongly claim the server was
gone. That's fixed too: a single failed check now retries quietly, and only a
sustained loss of contact shows as an error. The page also checks
`/api/ping` once on load, so "no live server at all" (you're viewing a static
file) and "server is running but hit an error" now look different, where
before they looked identical.

If a refresh still doesn't complete for you, the terminal running
`tracker serve` will show exactly which source is stuck — that's the fastest
way to tell whether it's a specific site or something environmental (a
firewall, a proxy, a VPN) blocking outbound requests.

**Refresh history**, linked in the bar, shows every completed run — background
and manual alike, there's no separate log for each. **All times on the page
are Singapore time**, labelled `SGT` — stored in UTC and converted for
display. The one exception is an event's own start time, reproduced exactly
as the company stated it, in the company's own timezone.

### Keeping it running

`serve` needs to be running for the background refresh to happen at all —
this matters more than it used to, now that visiting the page doesn't trigger
one itself. Start it once and leave it; it idles at essentially zero cost
between cycles. Easiest is just leaving the `START TRACKER (Windows).bat`
window open. To have it start automatically: Task Scheduler → new task →
trigger *At log on* → action `python -m tracker serve`, start-in set to the
project folder. For an always-current page with nobody needing to keep a
laptop on, host `serve` somewhere always-on instead — see **Sharing this
online** below; the free tiers of the usual hosts sleep when idle, which
fights this model directly (see the callout there).

If you'd rather not run a server, `python -m tracker run` does a one-off fetch
and writes a static `out/dashboard.html`. You lose the continuous background
refresh and the company-management panel that way — the page tells you so if
you open a static copy — but the data itself is identical either way, and
`import_excel_companies.py` / `python -m tracker add` still work against
`companies.yaml` regardless of whether `serve` is running.

---

## Adding a company

**Do this on the page — you never need to open a code file or edit YAML by
hand for day-to-day changes.** Press **+ Add / remove companies** in the bar
at the top. That opens a panel with:

- Every company currently tracked, with its source count — filterable by a
  search box, since the roster runs into the thousands
- **Pause** on each — stops fetching it, keeps its past events
- **Remove** on each — same, plus takes it off the list entirely
- A form to add a new one: ticker, company name, investor relations URL
  (optional), exchange, country, sub-sector, an optional SEC CIK, and other
  names the press uses for it

Submit the form and the tracker probes the IR site for a news feed — that
takes a few seconds — then writes the entry and **immediately starts a
background fetch scoped to just that company**, pulling its history back to
`run.history_from` (2020-01-01 by default) and any announced future events,
without waiting for the next scheduled or on-visit refresh. The status bar
shows this running the same way a full refresh does. A company you add this
way is permanent: it stays in the roster — and in every filter, search and
country/sub-sector list — until you remove it, across restarts and page
refreshes alike, because it's written straight into `companies.yaml`.

If the country you type is `US` (or `United States`), a `sec_edgar` source is
added automatically even without a CIK — see **SEC CIK auto-resolution**
under *Source types* below.

That panel is a thin front end over `companies.yaml` — it edits the same file
you'd edit by hand, as text, so the comments at the top of that file survive
and nothing is hidden from you. If a change would produce an invalid file,
it's rolled back automatically and the panel explains why rather than leaving
you with a broken roster.

*(This needs `python -m tracker serve` running. A static `out/dashboard.html`
opened directly can't write to your files — press the button there and it'll
tell you the one command to run.)*

### Bulk-importing a roster spreadsheet

If you're starting from a spreadsheet rather than adding companies one at a
time — one sheet per market, a bold row marking the start of each sub-sector,
then Company Name / Ticker rows below it (Bloomberg-style tickers like
`2330 TT Equity`) — `import_excel_companies.py` reads the whole thing in:

```bash
pip install openpyxl          # only this script needs it
python import_excel_companies.py "Company_Name_Tickers_and_Sub_Categories.xlsx"
```

It derives a ticker per market (`2330.TT Equity` → `2330.TW`, `700 HK Equity`
→ `0700.HK`, `AAPL US Equity` → `AAPL`, and so on), tracks the sheet name as
`country` and the bold header as `sub_sector`, and gives every company a
`news` source plus, for the US sheet, a `sec_edgar` source with no CIK (see
below). **A company already in `companies.yaml` is never overwritten** — only
its `country` and `sub_sector` are filled in or corrected, so a hand-tuned
entry with a real RSS feed or CIK keeps it. Re-run it any time the sheet
changes; it's safe to run repeatedly.

### If you'd rather script it

```bash
python -m tracker add \
  -t 2330.TW \
  -n "Taiwan Semiconductor Manufacturing Company" \
  --ir https://investor.tsmc.com/english \
  --exchange "TWSE / NYSE" --country Taiwan --alias TSMC
```

Add `--cik 0001046179` for a US filer to also pull SEC filings.

### Or edit the file directly

```yaml
  - ticker: 2330.TW
    name: Taiwan Semiconductor Manufacturing Company
    exchange: TWSE / NYSE
    region: TW
    country: Taiwan
    enabled: true
    aliases: ["TSMC"]
    sources:
      - type: rss
        tier: company_ir
        url: https://investor.tsmc.com/english/rss/news
      - type: news
        tier: news
```

Don't know the feed URL? Add the IR homepage as an `ir_page` source and run
`python -m tracker discover -t 2330.TW` for paste-ready blocks. After any of
the three methods, `python -m tracker verify` confirms every URL responds.

### Source types

| type | what it does | when to use it |
|---|---|---|
| `rss` | reads an RSS/Atom feed | always first choice — fast, clean, stable |
| `ir_page` | scrapes an HTML page using CSS selectors | when there's no feed |
| `sec_edgar` | pulls filings from SEC EDGAR by CIK | any US-listed issuer |
| `news` | queries a news index, then drops anything not whitelisted | corroboration |

#### SEC CIK auto-resolution

A `sec_edgar` source doesn't need a `cik:` line any more:

```yaml
      - type: sec_edgar
        tier: regulatory
```

Left blank like this, the CIK is resolved automatically from the company's
ticker against SEC's own published `company_tickers.json`, cached once per
run. A CIK you set explicitly always wins — this is purely for the common
case (added through the panel, or bulk-imported) where you only ever knew
the ticker. If a ticker can't be resolved (an ETF, a name SEC doesn't carry,
a typo) that one source is silently skipped rather than failing the run.

### Source tiers

Tier drives the coloured rail down the left edge of each card, and decides
which source becomes a card's primary link when several report the same
event. `regulatory` → `company_ir` → `newswire` → `news`, most authoritative
first.

---

## Reading the page

### Summary boxes

Six numbers across the top, always over the *whole* tracked set — they don't
move when you change filters below, so they stay a stable at-a-glance read:

| box | counts |
|---|---|
| New | events with status `new` — first seen since the last refresh |
| Date Moved | events whose scheduled date shifted |
| Revised | events whose headline, summary or category changed |
| Upcoming (7d) | dated events from today through 7 days out, inclusive |
| Upcoming (31d) | dated events from today through 31 days out, inclusive |
| Tracked | companies currently in the roster |

### Filters

**Events Category** (the row of chips) is what used to just be "Category" —
same thing, one per taxonomy entry in `config.yaml`, click to toggle. **Country**
filters by market — Korea / Japan / China / Taiwan / Hong Kong / US / Europe,
the same split as the sheets in a bulk-imported roster, not by individual
stock exchange. **Sub-sector** filters by the GICS-style sectors from that
same roster (Information Technology, Consumer Staples, Health Care, …). Both
populate themselves from whatever's actually in `companies.yaml`, so a
company you add with a new country or sub-sector value shows up as a new
filter option immediately.

### Next 7 days

The board at the top holds every dated event between today and seven days out,
soonest first, each with an explicit countdown: `TODAY`, `TOMORROW`, `IN 3 DAYS`.
Two days or closer turns red. When the window is empty it tells you what the next
dated event is and how far off, rather than going blank.

### Upcoming, then Past

Below the filters the page is split into two sections, and an event appears in
exactly one of them — never both.

**Upcoming** comes first: everything dated today or later, soonest to furthest.
**Past** sits below it: everything already happened, most recent back to earliest.

Inside each section, events are grouped by the country the company is listed in.
Countries are ordered by their leading event, so the section as a whole still
reads soonest-first (and most-recent-first in Past). Within a country, events run
in date order.

**Show past events** is the checkbox at the end of the Show row. It's ticked by
default. Untick it and the Past section disappears entirely, leaving only
Upcoming.

### Dates

The **When** row has four presets and two date boxes, and they're one system: a
preset just fills the boxes, and only the boxes drive the filter. So you can
click *Next 30 days* and then drag the end date out, or type any range you like —
`from` 2026-03-01 `to` 2026-06-30 to look at a specific quarter. Leave both empty
for everything held. *Clear filters* resets them.

### Event times

Each card shows the event's start time under the date:

| shown | means |
|---|---|
| `15:00 CEST` | the company stated a time; it's reproduced verbatim, in their timezone |
| `TIME TBC` | the event is scheduled but no time has been published yet |
| `NA` | no time applies — a past release, a filing, an undated item |

The drawer spells the same thing out in full under **Time**.

### Cards

**Each box is one real-world event, not one article.** When the IR release, the
Business Wire copy and a Reuters piece all cover the same acquisition, they
collapse onto a single card — the most authoritative becomes the headline link,
the rest show as `+2 sources`. Click any box for the full source list, exact
dates, match reasoning and change history. Links open at the publisher.

- **Rail colour** — who said it: green regulatory, navy company IR, brown wire, grey press
- **Striped rail + Δ stamp** — the date moved, showing from and to
- **Blue dot** — new since the last refresh

---

## Sharing this with someone who won't install anything

`tracker serve` on your own machine only your machine can reach — that's
local, not a public link. There are two fundamentally different shapes a
public link can take, and which one fits depends on what you actually need
from it.

### GitHub Pages — genuinely free, no sleep, no ephemeral disk, the default recommendation

`.github/workflows/refresh.yml` runs the tracker on GitHub's own servers on a
schedule and publishes a static snapshot to Pages — a URL like
`https://you.github.io/your-repo/` that works for anyone, on any device, with
nothing installed, and **actually costs nothing regardless of traffic** (for
a public repo, Actions minutes are unlimited; a private repo gets 2,000
free minutes/month, and one run a day at this project's scale uses a small
fraction of that). Unlike a service that has to stay running, there's no
sleep-on-idle to fight — each run just does its job and exits.

Setup:

1. Push this folder to a GitHub repository.
2. Repo **Settings → Pages → Build and deployment → Source: "GitHub Actions"**.
3. Repo **Settings → Secrets and variables → Actions → New repository secret**,
   name `SEC_CONTACT_EMAIL`, value a real address.
4. Push to `main`, or use the **Run workflow** button under the **Actions**
   tab to trigger it right away instead of waiting for the schedule.

**It accumulates real history across runs, not just whatever one run finds.**
The workflow commits `data/events.db` back to the repo after every run (see
the "Persist accumulated data" step), so the next scheduled run continues
from where the last one left off instead of starting over from the 4 seed
events every time — the same never-delete, only-add-or-update database this
whole project is built around, just persisted via git commits instead of a
long-lived disk. Combined with the shuffled fetch order (see **Things worth
knowing**), coverage of a large roster grows across scheduled runs even
though any single run is time-boxed well under GitHub's own job limits
(`TRACKER_MAX_RUN_SECONDS` in the workflow, currently 1,500s — no 15-minute
sleep timer to work around here, so it can afford a bigger budget than the
600s default tuned for interactive hosts).

The trade-off, and it's real: this copy refreshes on a schedule, not the
instant someone opens it, and it doesn't have the **+ Add / remove
companies** panel — that needs a live Python process behind it, which a
static Pages site doesn't have. Add companies by editing `companies.yaml`
(by hand, via `python -m tracker add`, or via `import_excel_companies.py`)
and pushing — same as any other change to the repo; the next scheduled run
picks it up. If a public repo works for you, you can also just shorten the
cron schedule for fresher data — Actions minutes are unlimited there, so
there's no cost to running it hourly instead of daily.

### A live link with the company panel and instant refresh — needs somewhere that stays running

For the full interactive experience (**Refresh now**, the company panel,
data that's current the moment you open it because it's been refreshing
itself continuously) you need `tracker serve` reachable from outside your
machine and **actually running continuously** — the background refresh only
happens while the process is alive, same as locally.

**Free tiers that sleep the service when idle fight this directly, and it's
not just a footnote — it actively breaks the always-on design.** [Render's
free plan](https://render.com) sleeps after ~15 minutes with no inbound
requests, killing an in-progress cycle, and its disk is ephemeral — every
restart, sleep-and-wake included, resets to exactly what's committed to
GitHub, discarding anything scraped since. If you've hit this, you're not
missing a setting; it's a structural mismatch between that tier and this
app, not a bug to work around. A few real ways past it:

- **Run it locally instead** (`START TRACKER (Windows).bat`) — genuinely
  free, no sleep, no ephemeral disk, works today with zero changes. The
  honest best option if you're comfortable leaving a PC on.
- **[Oracle Cloud's "Always Free" tier](https://www.oracle.com/cloud/free/)**
  — a real, persistent, always-on small VM (ARM Ampere A1 or x86 micro
  instances) at zero cost indefinitely, not a time-limited trial. This is
  the closest thing to "Render, but actually free and not fighting the
  design" available today. The catch is setup: it's a bare VM, not a
  connect-your-repo-and-click-deploy platform, so you're SSHing in,
  installing Python, and running `tracker serve` yourself (a systemd service
  or a persistent `tmux`/`screen` session both work) rather than following a
  quickstart. Oracle also requires a credit card for identity verification
  even though the tier itself is free, and has been known to reclaim
  under-utilized Always Free instances in some regions — worth reading their
  current terms before committing to it for something you care about.
- **A paid "no sleep + persistent disk" tier** — Render's Starter plan
  (roughly $7/month) is the cheapest one on Render that qualifies; `render.yaml`
  already has a commented persistent-disk block ready to uncomment if you go
  this way. [Railway](https://railway.app) and [Fly.io](https://fly.io) are
  the other usual names in this space, technically a good fit (both support
  persistent volumes and can be configured not to scale to zero) — but
  neither currently has a true unlimited free tier the way they once did
  (Railway moved to a small starting credit + usage-based pricing; Fly.io's
  free allowance is limited and terms shift), so check current pricing
  before assuming "free" there.

Either path is a legitimate answer to "shareable link" — pick GitHub Pages if
what matters is that anyone can open it at zero cost with zero setup and
periodic freshness is fine; pick an always-on host (paid, local, or Oracle's
free VM) if what matters is that it behaves exactly like your local copy,
refresh button and all, continuously.

---

## How it decides what counts as an event

`config.yaml` holds a weighted regex taxonomy. Each item is scored against every
category; highest score wins, and anything below `classifier.min_score` is
dropped as noise. SEC 8-K item codes bypass the keywords entirely — Item 2.02 is
an earnings release as a matter of fact, not inference.

Every card shows what it matched on, in the drawer under **Matched on**. When
something lands in the wrong bucket, that tells you which pattern to adjust.
Bump a weight, add a pattern, re-run. Nothing is recompiled.

Borderline items are genuinely borderline — "analyst briefing and fab site visit"
is defensibly Investor Day or Corporate Access. Tune the weights to your own
preference rather than treating the default as correct.

### Adding a category

```yaml
    capacity_announcement:
      label: "Capacity"
      enabled: true
      patterns:
        - ['\b(fab|plant)\s+(expansion|ramp|opening)\b', 4.0]
        - ['\bcapacity\s+(expansion|addition)\b', 3.5]
```

---

## Only official sources

Two mechanisms enforce this:

1. Company IR sites, exchange filings and PR wires are read directly. That's the
   company speaking for itself.
2. Everything from the news adapter is filtered against `news.allowed_domains`
   **after** retrieval. A domain not on that list can never reach the page, no
   matter how it ranks. Company-owned domains are added automatically from
   `companies.yaml`.

To go strictly first-party, run `python -m tracker run --no-news`, or set
`news.enabled: false`.

---

## Change detection

Each event gets a stable id from its ticker, category, calendar year and a
normalised slice of its headline (with any date phrase in that headline
stripped out first) — deliberately *not* the exact date, and not the URL,
because the exact date is precisely the thing that's allowed to change
without becoming a different event. A content hash over the rest — headline,
category, summary, time — catches everything else. On every run:

| status | meaning |
|---|---|
| `new` | first time seen |
| `date_moved` | a scheduled date shifted — the one that matters |
| `updated` | headline, summary or category revised |
| `unchanged` | still there, nothing moved |

Every change is written to a `changes` table and shown as change history in the
drawer, so you can see when something shifted and what it shifted from.
**Changed only** and **New only** are the monitoring view; everything else is
browsing.

### Nothing is ever removed

The database only ever inserts or updates an event — there is no code path
that deletes one. If a company's RSS feed rolls an old item off, its news
lookback window passes, an IR page gets redesigned, or a link that used to
work now 404s, that event simply isn't touched on runs where it doesn't
reappear — it stays exactly as last stored, at its last known status, with
its full change history intact. It does **not** disappear from the page. The
page always grows by addition and correction, never by silent removal;
`Store.mark_unseen_as_stale` in `tracker/store.py` is a no-op that exists
specifically to document this decision, so a future edit doesn't
accidentally wire deletion in. `python -m tracker run --no-news` or a
company you `Remove` from the roster are the only two ways an event stops
being *fetched* — and removing a company only stops new events for it; its
past events stay in the database (`Pause` keeps a company's events too;
`Remove` does the same, it just also drops the company from future runs).

When the same real-world event *does* reappear with a corrected date, that's
handled as `date_moved` on the existing row rather than a second, duplicate
event — including when the correction is embedded in the headline text
itself ("...on 12 November 2026" → "...on 19 November 2026"), which is
common for calendar-invite-style announcements.

---

## Commands

```
python -m tracker serve              run the local server (this is the main one)
python -m tracker serve --port 9000 --open
python -m tracker add -t T -n "Name" --ir URL [--cik N] [--country C] [--alias X]
python -m tracker verify             check every configured URL
python -m tracker discover -t STM    find feed URLs for a company
python -m tracker run                one-off fetch, writes a static page
python -m tracker run -t STM         one company only
python -m tracker run --no-news      official sources only
python -m tracker render             rebuild the static page without fetching
python -m tracker list               show the roster
python verified_seed.py              load 4 hand-verified real events, no network needed
python import_excel_companies.py F   bulk-import a roster spreadsheet (needs: pip install openpyxl)
```

---

## Things worth knowing before you rely on it

**Every link on the page is checked before it's shown, not just constructed.**
Earlier versions of this had a real bug: when the scraper couldn't find a
genuine per-item link, it silently fell back to the IR homepage, which is
exactly the "link goes to the wrong place" failure mode. That's fixed — an
item without its own distinct link is dropped, not faked — and every
surviving URL gets a live HEAD/GET check before it can reach the database. An
event whose only link goes dead loses that link (or the whole card, if
nothing else corroborates it) on the next refresh, not on some future audit.

**Check the feed URLs first.** The URLs shipped in `companies.yaml` are the
conventional ones for each IR site, but IR platforms get migrated and paths move.
Run `python -m tracker verify` before your first real run and fix whatever fails.
Expect a handful to need correcting.

**Scrapers break.** `ir_page` sources depend on CSS selectors, and a site
redesign will silently return nothing. `verify` catches dead URLs but not a page
that loads and yields zero items — watch the failure count in the refresh bar,
and watch for a company that suddenly goes quiet. Prefer `rss` wherever a feed
exists.

**JavaScript-rendered pages return nothing.** Several Asian IR sites build their
announcement lists client-side, so `requests` sees an empty shell. For those, use
the exchange filing feed instead (HKEXnews, SGX, TDnet, MOPS) or add Playwright.

**History only goes back as far as your sources reach.** `history_from:
2020-01-01` sets the window the page *displays*, but the tracker can only show
what it has actually collected: SEC EDGAR genuinely reaches back to 2020 (the
workhorse for the ~1,245 US names, once each one's CIK auto-resolves — see
above), RSS feeds typically carry 20–50 recent items regardless of the date on
them, and a plain `news` source (what most of the ~1,700 non-US, bulk-imported
names have, since a spreadsheet ticker alone doesn't tell you a company's IR
URL) only reaches back about 30 days. So: full 2020-onward history for most US
names from the first real run; everyone else fills in from the day you start
running the tracker daily, faster for any company you've given a real `rss` or
`ir_page` source. Right now the database holds exactly the 4 events in
`verified_seed.py` — hand-checked against their primary source, not scraped —
because that's what could be individually confirmed real from this
environment. Your first `python -m tracker run` (or the first `serve`
auto-refresh) against live sources replaces that with your actual coverage.

**A roster in the thousands takes real time to fetch, by design, not by
accident.** Sources are fetched on a bounded thread pool (`run.max_workers`),
so different companies' different IR hosts run concurrently — but a handful of
large public endpoints (SEC EDGAR, Google News) are shared by *every* company
using them, and those requests still queue behind that host's own courtesy
delay (`run.host_delay_overrides`). With ~2,900 companies mostly on a shared
`news` source, one full pass takes on the order of 20 minutes; `max_run_seconds`
is set high enough to usually finish that in one run, and if it doesn't,
whatever's left is named in the run's notes and picked up next time — same
graceful-skip behaviour as a small roster, just visible more often at this
scale. None of this blocks the page itself; it's a background thread, and
"Refresh now" starts it without waiting.

**Parsing a roster this size costs something too — paid once, not per page
view.** `companies.yaml` at ~2,900 entries takes real, measurable time
(seconds, not milliseconds) to parse. `tracker serve` pays that cost once at
startup ("Loading companies.yaml…" in the terminal) and again whenever the
file's modified time changes — i.e. right after an add/pause/remove — and
caches the result in between, so ordinary page views and filter changes stay
instant. If you installed via `pip install -r requirements.txt`, PyYAML's
compiled parser (when your platform's wheel includes it, which the common
Windows/macOS wheels do) makes this faster still.

**Respect the sites.** Small IR sites keep the full one-second-per-host delay.
Don't remove it, and check each site's terms and `robots.txt` before adding
one — some IR providers prohibit automated collection, and a licensed feed is
the right answer for anything client-facing. The faster rates in
`host_delay_overrides` are deliberately scoped to large public endpoints that
publish their own higher limits (SEC EDGAR: up to 10 requests/second by their
own fair-access guideline) — extend that list only for a host you've checked
tolerates it.

**Most non-US, bulk-imported companies only have a `news` source today.** A
roster spreadsheet gives a ticker and a name, not an IR URL — so those
companies are tracked (and searchable, and filterable) from the moment
they're imported, but their event coverage is corroboration-only (whitelisted
press, filtered by domain, same as everywhere else) until you give an
individual company a real `rss` or `ir_page` source, which then also unlocks
that company's own historical archive rather than just the last ~30 days.
This is a real gap, not a rounding error — treat this roster as "everyone's on
the list and searchable" more than "everyone has full regulatory-grade
coverage," and add proper sources for whichever names you actually watch
closely.

**This is a monitoring aid, not a system of record.** Verify anything that goes
into a model or a note against the primary document, which is exactly one click
away in the drawer.
