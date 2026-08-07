"""Orchestrates one refresh: fetch every enabled source, classify what comes
back, collapse re-fetches of the same real-world event into one card, check
every surviving link is actually live, and write the result to the store.

Bounded on two axes, both enforced here rather than just documented:
  - per-request timeout (run.request_timeout) so one bad host can't stall
    everything behind it
  - a whole-run wall-clock budget (run.max_run_seconds) so a refresh always
    finishes; whatever didn't get to run is named in the run's notes and
    picked up on the next refresh
"""
from __future__ import annotations

import difflib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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

    all_sources = []
    for c in companies:
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

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for c, s in all_sources:
            if time.time() - started > max_run_seconds:
                out_of_time = True
                skipped_companies.append(c["ticker"])
                continue
            futures[pool.submit(fetch_one, c, s)] = (c, s)

        for future in as_completed(futures):
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

    if out_of_time:
        with progress_lock:
            skipped_companies = sorted(set(skipped_companies))

    grouped = _group_into_events(classified_items, cfg)

    # Verify every surviving link is actually live before it can reach the
    # page. An event that loses its only link is dropped rather than shown
    # pointing somewhere dead. Same bounded-concurrency approach as fetching.
    def check_one(g):
        live = [s for s in g["sources"] if link_is_alive(session, s["url"], cfg)]
        return g, live

    kept = []
    if time.time() - started <= max_run_seconds:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(check_one, g) for g in grouped]
            for future in as_completed(futures):
                if time.time() - started > max_run_seconds:
                    out_of_time = True
                    continue
                g, live_sources = future.result()
                if not live_sources:
                    continue
                live_urls = {s["url"] for s in live_sources}
                if g["event"]["primary_url"] not in live_urls:
                    live_sources.sort(key=lambda s: tier_priority(s["tier"]))
                    g["event"]["primary_url"] = live_sources[0]["url"]
                g["sources"] = live_sources
                kept.append(g)
    else:
        out_of_time = True

    new_count = 0
    changed_count = 0
    for g in kept:
        status = store.upsert_event(g["event"], g["sources"])
        if status == "new":
            new_count += 1
        elif status in ("date_moved", "updated"):
            changed_count += 1

    notes = ""
    if out_of_time and skipped_companies:
        notes = "ran out of time, skipped: " + ", ".join(sorted(set(skipped_companies)))
    store.finish_run(run_id, ok=True, companies=len(companies), sources=total,
                      new_count=new_count, changed_count=changed_count, notes=notes)
    store.close()

    if verbose:
        print(f"Done: {new_count} new, {changed_count} changed, {len(kept)} events kept.")
        if notes:
            print(f"  {notes}")
    return {"new": new_count, "changed": changed_count, "kept": len(kept), "notes": notes}
