"""Fetchers. Each returns a list of RawItem. Nothing here decides what's an
event — that's classify.py's job — these just normalise whatever a feed,
page, filing index or news search hands back.

Only two kinds of source ever reach the page: the company's own channels
(rss / ir_page / sec_edgar) and news items that survive the domain
whitelist applied in pipeline.py. Nothing else is ever fetched as a
content source.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from datetime import datetime, date
from typing import Optional
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .models import RawItem

_TIME_RE = re.compile(
    r"\b(\d{1,2}[:.]\d{2}\s?(?:am|pm|AM|PM)?)\s*"
    r"(CET|CEST|EST|EDT|GMT|UTC|SGT|HKT|JST|KST|BST|PT|ET|PDT|PST|IST)?\b"
)

# One clock per host, so requests to the same site stay spaced out even
# across different companies/sources that happen to share a domain.
_last_hit: dict[str, float] = {}


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _be_polite(url: str, delay: float):
    host = _host(url)
    if not host or delay <= 0:
        return
    last = _last_hit.get(host)
    now = time.monotonic()
    if last is not None:
        wait = delay - (now - last)
        if wait > 0:
            time.sleep(wait)
    _last_hit[host] = time.monotonic()


def make_session(user_agent: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent, "Accept": "*/*"})
    return s


def _get(session: requests.Session, url: str, cfg: dict, **kw):
    timeout = tuple(cfg.get("run", {}).get("request_timeout", [6, 15]))
    delay = float(cfg.get("run", {}).get("polite_delay_seconds", 1.0))
    _be_polite(url, delay)
    return session.get(url, timeout=timeout, **kw)


def link_is_alive(session: requests.Session, url: str, cfg: dict) -> bool:
    """Every link shown on the page has been checked, not just constructed."""
    if not url:
        return False
    timeout = tuple(cfg.get("run", {}).get("request_timeout", [6, 15]))
    delay = float(cfg.get("run", {}).get("polite_delay_seconds", 1.0))
    try:
        _be_polite(url, delay)
        r = session.head(url, timeout=timeout, allow_redirects=True)
        if r.status_code < 400:
            return True
        if r.status_code in (405, 403):
            _be_polite(url, delay)
            r = session.get(url, timeout=timeout, stream=True)
            r.close()
            return r.status_code < 400
        return False
    except requests.RequestException:
        return False


def extract_event_datetime(text: str, fallback_date: Optional[str]):
    """Best-effort: pull an explicit calendar date and time-of-day out of
    prose ("...on October 30, 2026 at 15:00 CEST..."). Falls back to the
    item's own publish date when the text doesn't name one — right for a
    same-day release, approximate for a forward calendar invite that
    happens to omit the date from its own headline."""
    months = (r"January|February|March|April|May|June|July|August|"
              r"September|October|November|December")
    # Tried in order of specificity: ISO, "Month Day[, ]Year", the
    # day-before-month order common outside the US ("12 November 2026"),
    # then a bare "Month Day" with no year at all.
    date_patterns = [
        r"\b\d{4}-\d{2}-\d{2}\b",
        rf"\b(?:{months})\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+20\d{{2}}\b",
        rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{months})\s+20\d{{2}}\b",
        rf"\b(?:{months})\s+\d{{1,2}}(?:st|nd|rd|th)?\b",
    ]
    event_date = None
    for pattern in date_patterns:
        m = re.search(pattern, text)
        if not m:
            continue
        try:
            dt = dateparser.parse(m.group(0), fuzzy=True, default=datetime(date.today().year, 1, 1))
            event_date = dt.date().isoformat()
            break
        except (ValueError, OverflowError):
            continue
    if event_date is None:
        event_date = fallback_date

    time_match = _TIME_RE.search(text)
    if time_match:
        clock = time_match.group(1).strip()
        event_time = f"{clock} {time_match.group(2)}" if time_match.group(2) else clock
    else:
        event_time = None
    return event_date, event_time


def _entry_date(entry) -> Optional[str]:
    for key in ("published_parsed", "updated_parsed"):
        val = getattr(entry, key, None) or entry.get(key)
        if val:
            try:
                return datetime(*val[:6]).date().isoformat()
            except Exception:
                continue
    return None


def fetch_rss(source: dict, company: dict, cfg: dict, session: requests.Session) -> list[RawItem]:
    url = source["url"]
    r = _get(session, url, cfg)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    max_items = int(cfg.get("run", {}).get("max_items_per_source", 250))
    items = []
    for entry in parsed.entries[:max_items]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        summary = re.sub("<[^<]+?>", " ", entry.get("summary", "") or "")
        pub = _entry_date(entry)
        event_date, event_time = extract_event_datetime(f"{title} {summary}", pub)
        items.append(RawItem(
            ticker=company["ticker"], company_name=company["name"],
            country=company.get("country", ""), region=company.get("region", ""),
            exchange=company.get("exchange", ""),
            source_type="rss", tier=source.get("tier", "company_ir"),
            title=title, link=link, published=event_date, published_time=event_time,
            summary=summary.strip()[:600], publisher=_host(url),
        ))
    return items


def fetch_ir_page(source: dict, company: dict, cfg: dict, session: requests.Session) -> list[RawItem]:
    url = source["url"]
    r = _get(session, url, cfg)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "lxml")
    sel = source.get("selectors", {}) or {}
    item_sel = sel.get("item", "li")
    title_sel = sel.get("title", "a")
    date_sel = sel.get("date", "time")
    link_sel = sel.get("link", "a")

    max_items = int(cfg.get("run", {}).get("max_items_per_source", 250))
    items = []
    seen_links = set()
    for node in soup.select(item_sel)[: max_items * 3]:
        title_node = node.select_one(title_sel)
        link_node = node.select_one(link_sel) or title_node
        if title_node is None or link_node is None:
            continue
        title = title_node.get_text(strip=True)
        href = link_node.get("href") if link_node.has_attr("href") else None
        if not title or not href:
            continue
        link = urljoin(url, href)
        if link in seen_links:
            continue
        seen_links.add(link)

        date_node = node.select_one(date_sel)
        raw_date_text = ""
        pub = None
        if date_node is not None:
            raw_date_text = date_node.get_text(strip=True)
            if date_node.has_attr("datetime"):
                raw_date_text = date_node["datetime"]
            try:
                pub = dateparser.parse(raw_date_text, fuzzy=True).date().isoformat()
            except Exception:
                pub = None

        event_date, event_time = extract_event_datetime(f"{title} {raw_date_text}", pub)
        items.append(RawItem(
            ticker=company["ticker"], company_name=company["name"],
            country=company.get("country", ""), region=company.get("region", ""),
            exchange=company.get("exchange", ""),
            source_type="ir_page", tier=source.get("tier", "company_ir"),
            title=title, link=link, published=event_date, published_time=event_time,
            summary="", publisher=_host(url),
        ))
        if len(items) >= max_items:
            break
    return items


_SEC_FORM_HINT = {"8-K": "regulatory event", "6-K": "regulatory event",
                   "10-Q": "quarterly report", "10-K": "annual report",
                   "DEF 14A": "proxy statement", "DEFA14A": "proxy statement",
                   "425": "merger filing", "SC 14D9": "tender offer filing"}


def fetch_sec_edgar(source: dict, company: dict, cfg: dict, session: requests.Session) -> list[RawItem]:
    cik = str(source.get("cik", "")).strip().zfill(10)
    if not cik.strip("0"):
        return []
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    r = _get(session, url, cfg)
    r.raise_for_status()
    data = r.json()
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accns = recent.get("accessionNumber", [])
    items_field = recent.get("items", [""] * len(forms))
    primary_docs = recent.get("primaryDocument", [""] * len(forms))

    history_from = cfg.get("run", {}).get("history_from")
    max_items = int(cfg.get("run", {}).get("max_items_per_source", 250))
    items = []
    for form, fdate, accn, item_code, primary_doc in list(
            zip(forms, dates, accns, items_field, primary_docs))[:max_items]:
        if history_from and fdate < history_from:
            continue
        if form not in _SEC_FORM_HINT:
            continue
        accn_nodash = accn.replace("-", "")
        filing_url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                      f"{accn_nodash}/{primary_doc}" if primary_doc else
                      f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}")
        title = f"{company['name']} files {form} ({_SEC_FORM_HINT[form]})"
        if item_code:
            title += f" — items {item_code}"
        items.append(RawItem(
            ticker=company["ticker"], company_name=company["name"],
            country=company.get("country", ""), region=company.get("region", ""),
            exchange=company.get("exchange", ""),
            source_type="sec_edgar", tier="regulatory",
            title=title, link=filing_url, published=fdate, published_time=None,
            summary=f"SEC form {form} filed {fdate}.", publisher="sec.gov",
            sec_item=item_code or None,
        ))
    return items


def _domain_of(url: str) -> str:
    host = _host(url)
    return host[4:] if host.startswith("www.") else host


def _is_whitelisted(url: str, allowed_domains: set[str]) -> bool:
    domain = _domain_of(url)
    return any(domain == d or domain.endswith("." + d) for d in allowed_domains)


def fetch_news(source: dict, company: dict, cfg: dict, session: requests.Session,
                allowed_domains: set[str]) -> list[RawItem]:
    """Google News RSS search, filtered hard against the whitelist *after*
    retrieval. A domain not on that list can never reach the page no matter
    how the search ranks it — this function enforces that, not just the
    caller."""
    names = [company["name"]] + list(company.get("aliases", []))
    query = " OR ".join(f'"{n}"' for n in names[:4])
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    r = _get(session, url, cfg)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)

    max_items = int(cfg.get("run", {}).get("max_items_per_source", 250))
    items = []
    for entry in parsed.entries[:max_items]:
        title = (entry.get("title") or "").strip()
        source_info = entry.get("source") or {}
        publisher_url = source_info.get("href") or ""
        publisher_name = source_info.get("title") or ""
        # Fall back to sniffing the domain out of the link itself when the
        # feed doesn't give a <source href>. Either way it must clear the
        # whitelist before it's kept.
        candidate = publisher_url or entry.get("link") or ""
        if not candidate or not _is_whitelisted(candidate, allowed_domains):
            continue
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        pub = _entry_date(entry)
        event_date, event_time = extract_event_datetime(title, pub)
        items.append(RawItem(
            ticker=company["ticker"], company_name=company["name"],
            country=company.get("country", ""), region=company.get("region", ""),
            exchange=company.get("exchange", ""),
            source_type="news", tier="news",
            title=title, link=link, published=event_date, published_time=event_time,
            summary="", publisher=publisher_name or _domain_of(candidate),
        ))
    return items


FETCHERS = {
    "rss": fetch_rss,
    "ir_page": fetch_ir_page,
    "sec_edgar": fetch_sec_edgar,
    "news": fetch_news,
}
