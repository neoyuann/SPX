"""Shared data shapes used between sources, classify, pipeline and store.

Deliberately plain dicts-in, dicts-out at the module boundaries (pipeline
hands the store plain dicts, render reads plain dicts back) so store.py can
stay a thin SQLite wrapper with no ORM. The dataclasses here exist to keep
the *shape* of an item consistent while it moves through the pipeline.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional


# Tier authority order, most trustworthy first. Used to pick which source
# becomes an event's headline link when several corroborate the same thing.
TIER_RANK = {"regulatory": 0, "company_ir": 1, "newswire": 2, "news": 3}

_MONTHS = (r"January|February|March|April|May|June|July|August|"
           r"September|October|November|December")

# Tried in order of specificity: ISO, "Month Day, Year", the day-before-month
# order common outside the US ("12 November 2026"), then a bare "Month Day"
# with no year. Shared between sources.py (pulling a scheduled date out of
# prose) and event_id below (stripping one out of a headline) so the two
# never drift apart.
DATE_PHRASE_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(rf"\b(?:{_MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+20\d{{2}}\b", re.IGNORECASE),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\s+20\d{{2}}\b", re.IGNORECASE),
    re.compile(rf"\b(?:{_MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?\b", re.IGNORECASE),
]


def strip_date_phrases(text: str) -> str:
    """Remove any recognisable calendar-date phrase from text. Some IR
    headlines embed the date itself ("...Investor Day on 12 November
    2026") — when the company corrects that date, the new headline text
    differs from the old one *only* in the date, which would otherwise
    change event_id's hash and register as a brand-new event rather than
    the same one moving. Stripped before hashing, not before display."""
    for pattern in DATE_PHRASE_PATTERNS:
        text = pattern.sub(" ", text)
    return text


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
    # True when `published` is a historical fact that cannot be revised — a
    # filing's filing date, above all. False for anything whose date is a
    # plan that may move (a scheduled call, a meeting) or an estimate (a
    # news item's publish date standing in for an unstated event date).
    # event_id() keys on the exact date for facts and on the calendar year
    # for the rest; see the note there for why the two differ.
    date_is_fact: bool = False


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


def event_id(ticker: str, category: str, event_date: Optional[str], title: str,
              date_is_fact: bool = False) -> str:
    """Stable id for an event grouping.

    Deliberately excludes the *exact* date. A scheduled date correction is
    the flagship thing this tool exists to notice ("a capital markets day
    pushed back three weeks") — that only works if the corrected event
    keeps the same id, so store.upsert_event sees it as the same row with a
    changed event_date (status date_moved) rather than a same-titled event
    at a new id, which would insert a duplicate and silently leave the
    stale row behind. That was a real bug here until this was narrowed to
    a year bucket instead of the full date.

    Only the calendar year is kept, not the exact date, so a within-year
    correction still merges while two genuinely different years of a
    recurring, identically-titled event (an annual "Investor Day" with no
    year in its own headline, say) don't collapse into one record that
    just perpetually "moves" from one year to the next. A correction that
    crosses a year boundary (pushed from December into January) is the
    known edge this doesn't catch — same trade-off, smaller blast radius.
    Undated events group by title alone.

    The title itself is also stripped of any date phrase before hashing —
    some IR headlines spell the date out ("...on 12 November 2026"), and a
    corrected headline otherwise differs from the old one in exactly the
    text this function is trying to make date-corrections ignore.

    When date_is_fact is set, the *exact* date is keyed on instead. A
    filing's filing date is history, not a plan — it cannot be revised, so
    there is no date-correction to preserve, and the year bucket actively
    destroys data for these: SEC filing titles are generated from the form
    and its item codes ("CATERPILLAR INC files 8-K — items 2.02,7.01,9.01"),
    so every quarterly earnings 8-K a company files carries an identical
    title, and bucketing by year collapsed all four of them into a single
    event. That is why a company could show one filing for a whole year.
    """
    title_no_date = strip_date_phrases(title)
    norm_title = "".join(ch.lower() for ch in title_no_date if ch.isalnum() or ch.isspace())
    norm_title = " ".join(norm_title.split())[:60]
    date_key = (event_date or "undated") if date_is_fact else ((event_date or "")[:4] or "undated")
    key = f"{ticker}|{category}|{date_key}|{norm_title}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def content_hash(headline: str, category: str, event_date: Optional[str],
                  event_time: Optional[str], summary: str) -> str:
    key = f"{headline}|{category}|{event_date or ''}|{event_time or ''}|{summary}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
