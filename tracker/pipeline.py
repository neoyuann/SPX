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


def _trusted_by_construction(url: str) -> bool:
    """A sec.gov Archives URL isn't guessed — sources.py assembles it from
    SEC's own submissions index, out of the accession number and primary
    document name that index reports for a filing it says exists. A HEAD
    request to confirm that adds a network round-trip per filing (the bulk
    of every URL on a US-heavy roster) to re-learn what the authoritative
    index already said."""
    return url.startswith("https://www.sec.gov/Archives/")


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
    env_budget = os.environ.get("TRACKER_MAX_RUN_SECONDS")
    if env_budget:
        # Lets a one-shot CI run (GitHub Actions, no 15-minute sleep timer
        # to worry about) use a more generous budget than the shared
        # config.yaml value — tuned for interactive hosts — without editing
        # that file.
        max_run_seconds = float(env_budget)
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
    # page, so an event is never shown pointing somewhere dead.
    #
    # Three things keep this affordable, because a naive "one network
    # round-trip per URL, every run" does not survive contact with a real
    # roster. At a few thousand events this phase costs far more than the
    # fetching it follows, and when it blew its own budget it silently
    # dropped whatever hadn't been checked yet — a run reporting "18356
    # grouped into events, 0 failed their link check, 858 kept" was
    # discarding 95% of its own output, which is what made the page look
    # like it had almost nothing on it:
    #
    #   1. URLs that are trusted by construction are never fetched at all.
    #      A sec.gov Archives URL is assembled here from SEC's own
    #      submissions index (accession number + primary document), so
    #      asking the network to confirm what that index already stated is
    #      pure cost — and it's ~94% of all source URLs on a US-heavy
    #      roster.
    #   2. Results are cached in the store and reused while fresh, so an
    #      hourly run re-checks a given URL once a day, not 24 times.
    #   3. Anything still unchecked when the budget runs out is *kept*
    #      rather than dropped. These events were classified from a source
    #      that was successfully fetched moments earlier; the link check is
    #      a guard against link rot, not a precondition for the event being
    #      real. Discarding them trades a whole run's work for a guarantee
    #      that isn't worth that much — they get verified on a later run.
    def check_one(g):
        live = []
        checked: dict[str, bool] = {}
        for s in g["sources"]:
            url = s["url"]
            if _trusted_by_construction(url):
                live.append(s)
                continue
            ok = cached_checks.get(url)
            if ok is None:
                ok = link_is_alive(session, url, cfg)
                checked[url] = ok
            if ok:
                live.append(s)
        return g, live, checked

    link_check_ttl = float((cfg.get("run", {}) or {}).get("link_check_ttl_hours", 24))
    cached_checks = store.fresh_link_checks(link_check_ttl)
    kept = []
    link_check_failed = 0
    link_check_skipped = 0
    new_checks: dict[str, bool] = {}
    link_check_budget = float((cfg.get("run", {}) or {}).get("max_link_check_seconds", 900))
    check_pool = ThreadPoolExecutor(max_workers=max_workers)
    futures = {check_pool.submit(check_one, g): g for g in grouped}
    done_groups = set()

    def keep(g, live_sources):
        if not live_sources:
            return False
        live_urls = {s["url"] for s in live_sources}
        if g["event"]["primary_url"] not in live_urls:
            live_sources.sort(key=lambda s: tier_priority(s["tier"]))
            g["event"]["primary_url"] = live_sources[0]["url"]
        g["sources"] = live_sources
        kept.append(g)
        return True

    try:
        for future in as_completed(futures, timeout=link_check_budget) if futures else iter(()):
            g, live_sources, checked = future.result()
            done_groups.add(id(g))
            new_checks.update(checked)
            if not keep(g, live_sources):
                link_check_failed += 1
    except TimeoutError:
        out_of_time = True
        check_pool.shutdown(wait=False, cancel_futures=True)
        for future, g in futures.items():
            if id(g) not in done_groups:
                link_check_skipped += 1
                keep(g, list(g["sources"]))
    else:
        check_pool.shutdown(wait=True)
    store.record_link_checks(new_checks)

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
             f"events, {link_check_failed} failed their link check, "
             f"{link_check_skipped} kept unverified (link check ran long), {len(kept)} kept")
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
