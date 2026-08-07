"""Orchestrates one refresh: fetch every enabled source, classify what comes
back, collapse re-fetches of the same real-world event into one card, check
every surviving link is actually live, and write the result to the store.

Bounded on two axes, both enforced here rather than just documented:
  - per-request timeout (run.request_timeout) so one bad host can't stall
    everything behind it
  - a wall-clock budget on *fetching* (run.max_run_seconds) so a refresh
    doesn't spend unbounded time gathering raw data; whatever didn't get
    fetched is named in the run's notes and picked up on the next refresh

Classifying, grouping, link-checking and storing whatever *was* fetched is
deliberately unconditional — not bounded by the same clock. An earlier
version gated that whole phase behind "is there still time left in the
budget", and on a roster large enough that fetching alone could eat the
full budget (real-world network latency across thousands of sources, not
just the sandbox estimate this was tuned against), every single
successfully-fetched, successfully-classified item for that run was
silently discarded — a refresh that ran for half an hour without erroring
and stored nothing. Reproduced and fixed; see the regression test.
"""
from __future__ import annotations

import difflib
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed

import yaml

from .classify import Classifier
from .models import RawItem, canonical_url, content_hash, event_id, tier_priority
from .sources import FETCHERS, link_is_alive, make_session
from .store import Store


_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# A roster of a few thousand companies takes real, measurable time to parse
# (multi-second with PyYAML's pure-Python loader), and load_yaml is called
# on every single page view via server.py — so cache by path, keyed on the
# file's mtime. A hand edit or an addco.py write changes the mtime, which
# invalidates the cache automatically; nothing else needs to know caching
# is happening at all.
_yaml_cache: dict[str, tuple[float | None, dict]] = {}
_yaml_cache_lock = threading.Lock()


def load_yaml(path: str) -> dict:
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    with _yaml_cache_lock:
        cached = _yaml_cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.load(fh, Loader=_YAML_LOADER) or {}
    with _yaml_cache_lock:
        _yaml_cache[path] = (mtime, data)
    return data


def _title_similar(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.55


def _company_domains(company: dict) -> set[str]:
    domains = set()
    for src in company.get("sources", []):
        url = src.get("url")
        if url:
            host = url.split("//", 1)[-1].split("/", 1)[0].lower()
            if host.startswith("www."):
                host = host[4:]
            domains.add(host)
    return domains


def _group_into_events(classified: list, cfg: dict) -> list[dict]:
    """Collapse classified items from possibly-several sources into one
    event per real-world happening: same ticker + category, close dates
    (or both undated) and a similar headline."""
    buckets: dict[tuple, list] = {}
    for ci in classified:
        buckets.setdefault((ci.raw.ticker, ci.category), []).append(ci)

    events = []
    for (ticker, category), items in buckets.items():
        items.sort(key=lambda ci: (ci.raw.published or "9999-99-99"))
        clusters: list[list] = []
        for ci in items:
            placed = False
            for cluster in clusters:
                head = cluster[0]
                same_date = (ci.raw.published or None) == (head.raw.published or None)
                if same_date and _title_similar(ci.raw.title, head.raw.title):
                    cluster.append(ci)
                    placed = True
                    break
            if not placed:
                clusters.append([ci])

        for cluster in clusters:
            cluster.sort(key=lambda ci: tier_priority(ci.raw.tier))
            primary = cluster[0]
            summary = next((c.raw.summary for c in cluster if c.raw.summary), "")[:400]
            eid = event_id(ticker, category, primary.raw.published, primary.raw.title)
            chash = content_hash(primary.raw.title, category, primary.raw.published,
                                  primary.raw.published_time, summary)
            event = {
                "id": eid, "ticker": ticker, "company_name": primary.raw.company_name,
                "category": category, "label": primary.label, "headline": primary.raw.title,
                "summary": summary, "event_date": primary.raw.published,
                "event_time": primary.raw.published_time, "country": primary.raw.country,
                "region": primary.raw.region, "exchange": primary.raw.exchange,
                "tier": primary.raw.tier, "primary_url": canonical_url(primary.raw.link),
                "matched_on": sorted(set(m for c in cluster for m in c.matched_on)),
                "content_hash": chash,
            }
            sources = [{
                "url": canonical_url(c.raw.link), "tier": c.raw.tier,
                "source_type": c.raw.source_type, "publisher": c.raw.publisher,
                "title": c.raw.title,
            } for c in cluster]
            # de-dupe identical urls within the cluster
            seen = set()
            deduped_sources = []
            for s in sources:
                if s["url"] not in seen:
                    seen.add(s["url"])
                    deduped_sources.append(s)
            events.append({"event": event, "sources": deduped_sources})
    return events


def run(config_path: str, companies_path: str, *, verbose: bool = False,
        progress=None, only_ticker: str | None = None, no_news: bool = False):
    cfg = load_yaml(config_path)
    roster = load_yaml(companies_path)
    classifier = Classifier(cfg)
    user_agent = cfg.get("run", {}).get("user_agent", "CorporateEventTracker/1.0")
    contact_email = os.environ.get("SEC_CONTACT_EMAIL")
    if contact_email:
        # Lets a hosted deploy (e.g. GitHub Actions) supply the required SEC
        # contact address via a secret instead of committing it to config.yaml.
        user_agent = f"CorporateEventTracker/1.0 (research use; contact: {contact_email})"
    session = make_session(user_agent)

    allowed_domains = set((cfg.get("news", {}) or {}).get("allowed_domains", []))
    companies = [c for c in roster.get("companies", []) if c.get("enabled", True)]
    for c in companies:
        allowed_domains |= _company_domains(c)
    if only_ticker:
        companies = [c for c in companies if c["ticker"] == only_ticker]

    news_enabled = (cfg.get("news", {}) or {}).get("enabled", True) and not no_news
    max_run_seconds = float(cfg.get("run", {}).get("max_run_seconds", 240))
    max_workers = max(1, int(cfg.get("run", {}).get("max_workers", 8)))
    started = time.time()

    # Shuffled, not roster order — a run that hits its time budget partway
    # through (routine on a large roster, especially on a host that also
    # has its own tighter time constraints, like a free tier that sleeps
    # the process after ~15 minutes idle) would otherwise stall on the same
    # prefix of companies.yaml every single cycle, forever, and never reach
    # anything past it. A different random subset gets covered each time
    # instead, so coverage of the whole roster still converges over many
    # cycles even when no single cycle finishes everything.
    shuffled_companies = companies[:]
    random.shuffle(shuffled_companies)

    all_sources = []
    for c in shuffled_companies:
        for s in c.get("sources", []):
            if s.get("type") == "news" and not news_enabled:
                continue
            all_sources.append((c, s))
    total = len(all_sources)

    store = Store(cfg["output"]["db_path"])
    run_id = store.start_run()

    # Every (company, source) fetch is independent, so they run on a bounded
    # thread pool — different hosts overlap freely, requests to the *same*
    # host still queue behind that host's own delay (sources._be_polite is
    # lock-protected for exactly this). progress_lock guards the shared
    # counters below since several worker threads update them concurrently.
    classified_items: list = []
    skipped_companies: list = []
    progress_lock = threading.Lock()
    done = 0
    out_of_time = False

    def fetch_one(c, s):
        fetcher = FETCHERS.get(s["type"])
        if fetcher is None:
            return c, s, []
        if s["type"] == "news":
            return c, s, fetcher(s, c, cfg, session, allowed_domains)
        return c, s, fetcher(s, c, cfg, session)

    # Submitting is cheap regardless of count — even 4,000+ calls to
    # pool.submit() finish in milliseconds — so a budget check gating only
    # *submission* never actually has a chance to fire before everything's
    # already queued. The real bound has to be on *waiting*: as_completed's
    # own timeout, catching the TimeoutError it raises once the deadline
    # passes with futures still outstanding. (Confirmed with a standalone
    # repro: a 0.5s budget submitted 20 slow tasks in under 1ms, then the
    # process waited the full 6 seconds for all of them regardless — this
    # loop used to have exactly that shape.) Explicit pool lifecycle
    # management, not `with`, because `with`'s implicit exit calls
    # shutdown(wait=True) unconditionally — which would silently reintroduce
    # the same unbounded wait right after catching the timeout.
    pool = ThreadPoolExecutor(max_workers=max_workers)
    futures = {pool.submit(fetch_one, c, s): (c, s) for c, s in all_sources}
    try:
        deadline_remaining = max(0.0, started + max_run_seconds - time.time())
        completed_iter = as_completed(futures, timeout=deadline_remaining) if futures else iter(())
        for future in completed_iter:
            c, s = futures[future]
            label = f"{c['ticker']} · {s['type']}"
            with progress_lock:
                done += 1
                current_done = done
            if progress:
                progress(current_done, total, label)
            if verbose:
                print(f"[{current_done}/{total}] {label}")

            try:
                _, _, items = future.result()
            except Exception as exc:
                if verbose:
                    print(f"    ! {label} failed: {exc}")
                continue

            classified = [ci for item in items if (ci := classifier.classify(item)) is not None]
            with progress_lock:
                classified_items.extend(classified)
    except TimeoutError:
        out_of_time = True
        for future, (c, _s) in futures.items():
            if not future.done():
                skipped_companies.append(c["ticker"])
        # Don't wait for whatever's still running — their own request_timeout
        # bounds how much longer that can be, and a background refresh
        # cycle running a bit long in the background is a fine trade for
        # actually respecting the budget on the common path.
        pool.shutdown(wait=False, cancel_futures=True)
    else:
        pool.shutdown(wait=True)

    if out_of_time:
        with progress_lock:
            skipped_companies = sorted(set(skipped_companies))

    grouped = _group_into_events(classified_items, cfg)

    # Verify every surviving link is actually live before it can reach the
    # page. An event that loses its only link is dropped rather than shown
    # pointing somewhere dead. This always runs to completion over whatever
    # was grouped, regardless of run.max_run_seconds — that budget bounds
    # fetching (above), not this. Fetching already decided what got
    # gathered within budget; having done that work, throwing it away here
    # because the clock read past the budget during fetching would discard
    # a run's entire output. (It used to, silently — see the module
    # docstring.) Each check still has its own per-request timeout, so this
    # can't hang, just take a while on a very large batch — and it's
    # already a background thread, so that's a fine trade.
    def check_one(g):
        live = [s for s in g["sources"] if link_is_alive(session, s["url"], cfg)]
        return g, live

    kept = []
    link_check_failed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(check_one, g) for g in grouped]
        for future in as_completed(futures):
            g, live_sources = future.result()
            if not live_sources:
                link_check_failed += 1
                continue
            live_urls = {s["url"] for s in live_sources}
            if g["event"]["primary_url"] not in live_urls:
                live_sources.sort(key=lambda s: tier_priority(s["tier"]))
                g["event"]["primary_url"] = live_sources[0]["url"]
            g["sources"] = live_sources
            kept.append(g)

    new_count = 0
    changed_count = 0
    for g in kept:
        status = store.upsert_event(g["event"], g["sources"])
        if status == "new":
            new_count += 1
        elif status in ("date_moved", "updated"):
            changed_count += 1

    # Always recorded, not just when something's wrong — this is what makes
    # "why did a run store nothing" answerable from the History panel
    # instead of needing a terminal.
    stats = (f"{len(classified_items)} items classified, {len(grouped)} grouped into "
             f"events, {link_check_failed} failed their link check, {len(kept)} kept")
    notes = stats
    if out_of_time and skipped_companies:
        notes += "; ran out of time, skipped: " + ", ".join(sorted(set(skipped_companies)))
    store.finish_run(run_id, ok=True, companies=len(companies), sources=total,
                      new_count=new_count, changed_count=changed_count, notes=notes)
    store.close()

    if verbose:
        print(f"Done: {new_count} new, {changed_count} changed, {len(kept)} events kept.")
        if notes:
            print(f"  {notes}")
    return {"new": new_count, "changed": changed_count, "kept": len(kept), "notes": notes}
