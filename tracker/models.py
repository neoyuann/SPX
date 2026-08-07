"""Shared data shapes used between sources, classify, pipeline and store.

Deliberately plain dicts-in, dicts-out at the module boundaries (pipeline
hands the store plain dicts, render reads plain dicts back) so store.py can
stay a thin SQLite wrapper with no ORM. The dataclasses here exist to keep
the *shape* of an item consistent while it moves through the pipeline.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional


# Tier authority order, most trustworthy first. Used to pick which source
# becomes an event's headline link when several corroborate the same thing.
TIER_RANK = {"regulatory": 0, "company_ir": 1, "newswire": 2, "news": 3}


def tier_priority(tier: str) -> int:
    return TIER_RANK.get(tier, 99)


@dataclass
class RawItem:
    """One fetched item, before classification or dedup."""
    ticker: str
    company_name: str
    country: str
    region: str
    exchange: str
    source_type: str          # rss | ir_page | sec_edgar | news
    tier: str                 # regulatory | company_ir | newswire | news
    title: str
    link: str
    published: Optional[str] = None   # ISO date (YYYY-MM-DD) if known
    published_time: Optional[str] = None  # verbatim time-of-day string, if the source gave one
    summary: str = ""
    publisher: str = ""       # domain or feed name, for display
    sec_item: Optional[str] = None    # 8-K item code, if this came from EDGAR


@dataclass
class ClassifiedItem:
    raw: RawItem
    category: str
    label: str
    score: float
    matched_on: list = field(default_factory=list)


def canonical_url(url: str) -> str:
    """Strip tracking noise so the same article under different query
    strings still dedupes to the same id."""
    if not url:
        return url
    base = url.split("#", 1)[0]
    if "?" in base:
        head, _, query = base.partition("?")
        keep = [p for p in query.split("&")
                if p and not p.split("=")[0].lower().startswith(("utm_", "ref", "src"))]
        base = head + ("?" + "&".join(keep) if keep else "")
    return base.rstrip("/")


def event_id(ticker: str, category: str, event_date: Optional[str], title: str) -> str:
    """Stable id for an event grouping. Same ticker + category + date +
    a normalised slice of the headline collapses re-fetches of the same
    real-world event into one row instead of duplicating it."""
    norm_title = "".join(ch.lower() for ch in title if ch.isalnum() or ch.isspace())
    norm_title = " ".join(norm_title.split())[:60]
    key = f"{ticker}|{category}|{event_date or ''}|{norm_title}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def content_hash(headline: str, category: str, event_date: Optional[str],
                  event_time: Optional[str], summary: str) -> str:
    key = f"{headline}|{category}|{event_date or ''}|{event_time or ''}|{summary}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
