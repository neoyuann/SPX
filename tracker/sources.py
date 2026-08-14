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
import threading
import time
import urllib.parse
from datetime import datetime, date, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from .models import DATE_PHRASE_PATTERNS, RawItem

# A clock time needs a *qualifier* to count — either am/pm or a named zone.
# Both used to be optional, which meant a bare "\d{1,2}[.:]\d{2}" matched any
# decimal in the prose around an earnings release: "$0.45 per share" was
# being stored as an event time of "0.45", and 6 of the 9 times ever
# captured were figures like that rather than times. The negative lookbehind
# keeps a currency amount or a longer number from being read as a clock even
# when a zone happens to follow it.
_TZ_NAMES = (r"CET|CEST|EST|EDT|GMT|UTC|SGT|HKT|JST|KST|BST|PDT|PST|CST|CDT|MST|MDT|"
             r"IST|AEST|AEDT|NZST|MSK|EET|EEST|WET|ET|PT|CT|MT")
_TZ_WORDS = (r"(?:US\s+)?(?:Eastern|Pacific|Central|Mountain|London|Paris|Frankfurt|"
             r"Tokyo|Hong\s+Kong|Singapore|Sydney|Seoul|Taipei|Beijing)"
             r"(?:\s+(?:Standard|Daylight|Summer))?(?:\s+Time)?")
_TIME_RE = re.compile(
    r"(?<![\d.$€£¥])"
    r"(\d{1,2}(?::\d{2})?(?:\.\d{2})?)\s*"
    r"(?:(a\.?m\.?|p\.?m\.?)\s*)?"
    rf"(?:({_TZ_NAMES})\b|({_TZ_WORDS}))?",
    re.IGNORECASE)


def _parse_clock(text: str) -> Optional[str]:
    """Pull a genuine time-of-day out of prose, or None. Requires a real
    qualifier (am/pm or a named zone) so figures in the surrounding text
    can't be mistaken for a clock — see _TIME_RE."""
    for m in _TIME_RE.finditer(text):
        clock, meridiem, tz_abbr, tz_word = m.groups()
        if not meridiem and not tz_abbr and not tz_word:
            continue
        if "." in clock and not meridiem:
            continue  # "0.45" with no am/pm is a number, not a time
        hour_str = clock.replace(".", ":").split(":")[0]
        try:
            hour = int(hour_str)
        except ValueError:
            continue
        if hour > 23 or (meridiem and hour > 12):
            continue
        minute = clock.replace(".", ":").split(":")[1] if ":" in clock.replace(".", ":") else "00"
        if int(minute) > 59:
            continue
        out = f"{hour}:{minute}"
        if meridiem:
            out += " " + meridiem.replace(".", "").lower()
        zone = tz_abbr or tz_word
        if zone:
            out += " " + " ".join(zone.split()).upper() if tz_abbr else " " + " ".join(zone.split())
        return out.strip()
    return None

# One clock per host, so requests to the same site stay spaced out even
# across different companies/sources that happen to share a domain — and
# across worker threads, since a run with many companies fetches several
# hosts concurrently (see pipeline.py). The lock only ever guards a dict
# read/write, never the sleep itself, so it doesn't serialise unrelated hosts.
_last_hit: dict[str, float] = {}
_last_hit_lock = threading.Lock()


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _delay_for(host: str, cfg: dict) -> float:
    overrides = (cfg.get("run", {}) or {}).get("host_delay_overrides", {}) or {}
    if host in overrides:
        return float(overrides[host])
    return float((cfg.get("run", {}) or {}).get("polite_delay_seconds", 1.0))


class PolitenessQueueBacklogged(Exception):
    """Raised instead of sleeping when a shared host's politeness queue is
    backed up past a sane ceiling. See _be_polite for why this exists."""


def _be_polite(url: str, cfg: dict):
    host = _host(url)
    delay = _delay_for(host, cfg)
    if not host or delay <= 0:
        return
    # Reserve a slot exactly once, under the lock, then sleep to it outside
    # the lock. A thread must never re-read the shared marker after this —
    # doing that let concurrent callers keep pushing each other's wake time
    # forward (an earlier version of this function livelocked that way).
    #
    # If the reservation queue for this host is already backed up past
    # max_polite_wait_seconds, don't reserve a slot or sleep for it at all —
    # raise instead, so the caller treats this one fetch as failed (it'll
    # get another chance next run) rather than blocking a worker thread for
    # a very long time. This matters a lot at real scale: with thousands of
    # companies sharing one host (news.google.com, say) and a bounded pool
    # of workers, upfront submission means many threads dequeue a task and
    # start sleeping *before* the overall run-level deadline check ever gets
    # a chance to matter to them — a thread already sleeping here can't be
    # cancelled, and since ThreadPoolExecutor's worker threads are
    # non-daemon, the whole process (and whatever's waiting on it — a CI
    # job, a shell) doesn't exit until every one of them finishes. Without
    # this ceiling, a deep-enough backlog turned a ~25-minute intended
    # budget into 5 real hours on GitHub Actions before this was caught.
    ceiling = float((cfg.get("run", {}) or {}).get("max_polite_wait_seconds", 45))
    with _last_hit_lock:
        now = time.monotonic()
        last = _last_hit.get(host)
        next_slot = now if last is None else max(now, last + delay)
        wait = next_slot - now
        if wait > ceiling:
            raise PolitenessQueueBacklogged(
                f"{host}'s queue is {wait:.0f}s deep (over the {ceiling:.0f}s ceiling)")
        _last_hit[host] = next_slot
    if wait > 0:
        time.sleep(wait)


def make_session(user_agent: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent, "Accept": "*/*"})
    return s


class ResponseTooSlow(requests.exceptions.Timeout):
    """Raised when a response's *total* download time exceeds
    max_response_seconds, even though no single gap between chunks was
    ever long enough to trip requests' own per-read socket timeout.

    That gap-only guarantee is a real trap: `timeout=(connect, read)`
    bounds the pause between successive reads, not how long the whole
    body takes to arrive — a server that trickles bytes down just fast
    enough to keep resetting that per-chunk clock can hold a connection
    open indefinitely without ever raising anything. Because
    ThreadPoolExecutor's worker threads are non-daemon, one thread stuck
    that way blocks the whole process from exiting no matter what
    pipeline.py's run-level deadline does — this is what turned a
    ~25-minute GitHub Actions budget into a 5-hour hang before it was
    caught. Streaming with our own wall-clock check closes that gap.
    """


def _get(session: requests.Session, url: str, cfg: dict, **kw):
    run_cfg = cfg.get("run", {}) or {}
    timeout = tuple(run_cfg.get("request_timeout", [6, 15]))
    max_total = float(run_cfg.get("max_response_seconds", 60))
    _be_polite(url, cfg)
    r = session.get(url, timeout=timeout, stream=True, **kw)
    deadline = time.monotonic() + max_total
    chunks = []
    try:
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                chunks.append(chunk)
            if time.monotonic() > deadline:
                raise ResponseTooSlow(
                    f"{url} took longer than {max_total:.0f}s to fully download")
    finally:
        r.close()
    # iter_content() consumed the stream ourselves, so requests never got a
    # chance to populate r._content — do that manually so callers' usual
    # r.json() / r.text / r.content keep working unchanged.
    r._content = b"".join(chunks)
    r._content_consumed = True
    return r


def link_is_alive(session: requests.Session, url: str, cfg: dict) -> bool:
    """Every link shown on the page has been checked, not just constructed."""
    if not url:
        return False
    timeout = tuple(cfg.get("run", {}).get("request_timeout", [6, 15]))
    try:
        _be_polite(url, cfg)
        r = session.head(url, timeout=timeout, allow_redirects=True)
        if r.status_code < 400:
            return True
        if r.status_code in (405, 403):
            _be_polite(url, cfg)
            r = session.get(url, timeout=timeout, stream=True)
            r.close()
            return r.status_code < 400
        return False
    except (requests.RequestException, PolitenessQueueBacklogged, ResponseTooSlow):
        return False


def extract_event_datetime(text: str, fallback_date: Optional[str]):
    """Best-effort: pull an explicit calendar date and time-of-day out of
    prose ("...on October 30, 2026 at 15:00 CEST..."). Falls back to the
    item's own publish date when the text doesn't name one — right for a
    same-day release, approximate for a forward calendar invite that
    happens to omit the date from its own headline."""
    # Same pattern list event_id() uses to strip a date phrase out of a
    # headline before hashing — kept in one place (models.py) so the two
    # can't drift apart. Tried in order of specificity: ISO, "Month
    # Day[, ]Year", the day-before-month order common outside the US
    # ("12 November 2026"), then a bare "Month Day" with no year at all.
    event_date = None
    for pattern in DATE_PHRASE_PATTERNS:
        m = pattern.search(text)
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

    return event_date, _parse_clock(text)


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
        # Capped *before* anything scans it, not just before storage — a
        # feed entry's raw title/summary is attacker- or garbage-controlled
        # text of unbounded length, and regex matching against it (here and
        # in classify.py) runs on the calling thread with the GIL held for
        # the whole call. A stray multi-hundred-KB entry once turned one
        # `.search()` into an hours-long stall that blocked everything else
        # in the process, not just that one item — see classify.py.
        title = (entry.get("title") or "").strip()[:300]
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        summary = re.sub("<[^<]+?>", " ", entry.get("summary", "") or "")[:600].strip()
        pub = _entry_date(entry)
        event_date, event_time = extract_event_datetime(f"{title} {summary}", pub)
        items.append(RawItem(
            ticker=company["ticker"], company_name=company["name"],
            country=company.get("country", ""), region=company.get("region", ""),
            exchange=company.get("exchange", ""),
            source_type="rss", tier=source.get("tier", "company_ir"),
            title=title, link=link, published=event_date, published_time=event_time,
            summary=summary, publisher=_host(url),
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
        # Capped for the same reason fetch_rss caps its title/summary: an
        # over-broad selector can match a container holding a whole page's
        # text, and unbounded text reaching a regex search (here or in
        # classify.py) can stall the calling thread for a very long time.
        title = title_node.get_text(strip=True)[:300]
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
            raw_date_text = date_node.get_text(strip=True)[:200]
            if date_node.has_attr("datetime"):
                raw_date_text = date_node["datetime"][:200]
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

_NON_US_SUFFIXES = (".HK", ".TW", ".T", ".KS", ".CH")

# SEC's own ticker->CIK map, fetched once per process and shared by every
# company that needs it — a `sec_edgar` source doesn't have to carry an
# explicit `cik:` any more, it can resolve one from the company's ticker.
_cik_map: Optional[dict] = None
_cik_map_lock = threading.Lock()


def _load_cik_map(session: requests.Session, cfg: dict) -> dict:
    global _cik_map
    with _cik_map_lock:
        if _cik_map is not None:
            return _cik_map
        try:
            r = _get(session, "https://www.sec.gov/files/company_tickers.json", cfg)
            r.raise_for_status()
            data = r.json()
            _cik_map = {
                str(rec.get("ticker", "")).upper().strip(): str(rec.get("cik_str", "")).zfill(10)
                for rec in data.values()
                if rec.get("ticker") and rec.get("cik_str")
            }
        except Exception as exc:
            print(f"[sec_edgar] couldn't load SEC's ticker list, CIK auto-resolve is off this run: {exc}")
            _cik_map = {}
        return _cik_map


def resolve_cik(session: requests.Session, cfg: dict, ticker: str) -> Optional[str]:
    base = ticker.strip().upper()
    for suffix in _NON_US_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return _load_cik_map(session, cfg).get(base)


# An 8-K's own *metadata* only ever carries a filing date — the date it was
# disclosed, not any date it might describe — so sec_edgar's items above are
# structurally unable to produce an "upcoming" event no matter how far
# lookahead_days reaches. But the filing's own document (the press release
# it furnishes) often does name a real future date: an item 2.02 earnings
# 8-K commonly says "will host a conference call on [date]", and an item
# 7.01 Reg FD 8-K is the standard vehicle for announcing an investor day or
# capital markets day weeks or months out. This is the one place that
# information can come from for the ~1,250 companies that only have
# sec_edgar + news (no ir_page calendar) configured.
#
# Kept deliberately narrow to avoid two different failure modes:
#   - cost: only chases the document for filings that are both the right
#     item code AND recently filed, not the company's whole filing history,
#     so this adds roughly one extra request per company per run at most,
#     not one per historical filing
#   - correctness: SEC filing text is full of unrelated dates (fiscal
#     year-ends, prior-period comparisons, boilerplate) — a bare date match
#     isn't enough signal, so a candidate is only trusted when a scheduling
#     keyword sits close to it AND the date is a sane distance in the
#     future. Getting this wrong would put a wrong date on the page with
#     the same confidence as a verified filing fact, which is worse than
#     just not showing it.
_SCHEDULE_ITEM_CODES = {"2.02", "7.01", "8.01"}
# A proxy statement has no 8-K item code, but it is the single most reliable
# forward-looking filing there is: every US company files one ahead of its
# annual meeting, and it opens by stating exactly when and where that
# meeting will be held ("...will be held on May 15, 2027 at 9:00 a.m.
# Eastern Time"). Filed further ahead of the event than an earnings 8-K, so
# it gets a longer lookback than the item-code forms.
_SCHEDULE_FORMS = {"DEF 14A", "DEFA14A"}
_SCHEDULE_LOOKBACK_DAYS = 30
_SCHEDULE_PROXY_LOOKBACK_DAYS = 120
# A full annual cycle plus margin: an AGM is routinely announced ten or
# eleven months ahead, and a tighter horizon silently discarded those.
_SCHEDULE_MAX_FUTURE_DAYS = 400
_SCHEDULE_TEXT_CAP = 20000
_SCHEDULE_KEYWORDS_RE = re.compile(
    r"\b(conference\s+call|webcast|earnings\s+call|investor\s+day|"
    r"analyst\s+day|capital\s+markets?\s+day|"
    r"annual\s+meeting(?:\s+of\s+(?:the\s+)?(?:share|stock)holders)?|"
    r"special\s+meeting(?:\s+of\s+(?:the\s+)?(?:share|stock)holders)?|"
    r"extraordinary\s+general\s+meeting|annual\s+general\s+meeting)\b",
    re.IGNORECASE)


# Dates that sit next to a scheduling keyword but do not date the event.
# The one that actually caused wrong dates on the page: an earnings release
# says the call is "today ... Monday, August 10, 2026" and then, a sentence
# later, that "an audio replay of the conference call will be available
# until August 17, 2026". Scanning for any future date near the words
# "conference call" rejected the real (already past) call date and reported
# the replay's expiry as the event — an event that never existed, dated a
# week after the call it referred to.
_SCHEDULE_NEGATIVE_RE = re.compile(
    r"\b(replay|rebroadcast|re-?air|archiv\w*|on[-\s]?demand|podcast|transcript|"
    r"available\s+(?:until|through|for|from)|accessible\s+(?:until|through)|"
    r"expires?|expiring|through\s+\w+\s+\d|until|"
    # Dates that appear right beside "annual meeting" in a proxy but date
    # something else entirely. The record date ("holders of record as of
    # March 15, 2027 are entitled to vote at the Annual Meeting") and the
    # fiscal year end ("for the fiscal year ending December 31, 2026") are
    # the two that actually produced wrong entries — several meetings were
    # stored as 31 December, which is a year end, not a meeting.
    r"fiscal\s+year|year\s+end(?:ing|ed)?|record\s+date|"
    r"(?:holders?|shareholders?|stockholders?)\s+of\s+record|as\s+of\s+the\s+close|"
    # Proxy voting deadlines. A proxy statement says votes "must be received
    # by 11:59 p.m. Eastern Time on September 21, 2026" — the day before the
    # meeting it refers to — which was being stored as the meeting itself,
    # producing a duplicate event dated a day early and timed 11:59 p.m.
    r"must\s+be\s+received|received\s+by|submitted\s+by|deadline|cut[-\s]?off|"
    r"vote\s+(?:your|by)|voting\s+instructions?|proxy\s+card)\b",
    re.IGNORECASE)

# No shareholder meeting, earnings call or investor day starts late at night.
# A clock in these hours is a deadline or a boilerplate cut-off that slipped
# past the wording checks above, and it discredits the date it sits with —
# so the whole candidate is dropped, not just its time.
_IMPLAUSIBLE_EVENT_HOURS = set(range(22, 24)) | set(range(0, 5))


def _clock_hour24(clock: str) -> Optional[int]:
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)?", clock or "", re.IGNORECASE)
    if not m:
        return None
    hour = int(m.group(1))
    meridiem = (m.group(3) or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return hour

# Sentence-ish split. The "p.m." in "6:00 p.m. Eastern Daylight Time,
# Monday, August 10, 2026" is not a sentence end, and treating it as one
# splits the clock away from the date it belongs to — which loses the
# announcement entirely, since neither half then has both a keyword and a
# date. Hence the negative lookbehind for a.m./p.m.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])(?<![ap]\.m\.)\s+(?=[A-Z0-9])", re.IGNORECASE)


def _find_scheduled_date(doc_text: str, today: date) -> Optional[tuple[str, str, Optional[str]]]:
    """Returns (date, matched keyword, time-of-day or None), or None when the
    document doesn't announce a genuine upcoming event.

    Works sentence by sentence rather than by proximity window. A date only
    counts when it shares a sentence with the scheduling keyword it's meant
    to date, and when nothing in that sentence marks it as a replay or
    availability window (see _SCHEDULE_NEGATIVE_RE). Crucially, a sentence
    that announces an event which has already happened yields nothing at
    all — it does *not* fall through to some other future-looking date
    elsewhere in the document, which is exactly how a replay-expiry date
    ended up on the page as though it were a scheduled call."""
    text = doc_text[:_SCHEDULE_TEXT_CAP]
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        kw = _SCHEDULE_KEYWORDS_RE.search(sentence)
        if not kw or _SCHEDULE_NEGATIVE_RE.search(sentence):
            continue
        for pattern in DATE_PHRASE_PATTERNS:
            for m in pattern.finditer(sentence):
                try:
                    dt = dateparser.parse(m.group(0), fuzzy=True,
                                           default=datetime(today.year, 1, 1))
                except (ValueError, OverflowError):
                    continue
                found = dt.date()
                if found <= today:
                    # This sentence dates its event in the past, so there is
                    # no upcoming event here. Stop rather than hunting for a
                    # future date elsewhere — the alternative is reporting an
                    # unrelated date as though it were this event's.
                    return None
                if found > today + timedelta(days=_SCHEDULE_MAX_FUTURE_DAYS):
                    continue
                # The clock usually sits in the same sentence as the date, on
                # either side of it ("today from 5:00 p.m. ... Monday, August
                # 10, 2026" puts it before). The zone can trail the end of a
                # range ("5:00 p.m. to 6:00 p.m. Eastern Daylight Time"), so
                # fall back to the sentence's own zone when the clock didn't
                # carry one.
                clock = _parse_clock(sentence[m.end():]) or _parse_clock(sentence)
                hour = _clock_hour24(clock) if clock else None
                if hour is not None and hour in _IMPLAUSIBLE_EVENT_HOURS:
                    return None
                if clock and not re.search(rf"({_TZ_NAMES})\b|{_TZ_WORDS}", clock, re.IGNORECASE):
                    zone = re.search(rf"\b({_TZ_NAMES})\b|\b({_TZ_WORDS})", sentence, re.IGNORECASE)
                    if zone:
                        clock = f"{clock} {' '.join(zone.group(0).split())}"
                return found.isoformat(), kw.group(0), clock
    return None


_EXHIBIT_NAME_RE = re.compile(r"ex-?99", re.IGNORECASE)
_MAX_EXHIBIT_CANDIDATES = 3


def _find_exhibit_urls(session: requests.Session, cfg: dict, cik: str, accn_nodash: str) -> list[str]:
    """A large filer's 8-K cover page (primaryDocument — the only thing
    fetched below before this existed) is typically a bare item list that
    says "see Exhibit 99.1" with no narrative text of its own; the actual
    "will host a conference call on [date]" language lives in that exhibit,
    a separate document in the same filing. EX-99.x is the SEC's own
    convention for a press-release exhibit, so that's what this looks for
    in the filing's document index rather than guessing a filename."""
    try:
        idx_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn_nodash}/index.json"
        r = _get(session, idx_url, cfg)
        r.raise_for_status()
        entries = ((r.json().get("directory") or {}).get("item") or [])
    except Exception:
        return []
    urls = []
    for entry in entries:
        name = str(entry.get("name", ""))
        etype = str(entry.get("type", ""))
        if "99" in etype or _EXHIBIT_NAME_RE.search(name):
            urls.append(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn_nodash}/{name}")
        if len(urls) >= _MAX_EXHIBIT_CANDIDATES:
            break
    return urls


def _fetch_scheduled_event(session: requests.Session, cfg: dict, company: dict, cik: str,
                            accn_nodash: str, filing_url: str, form: str, fdate: str,
                            item_code: str) -> Optional[RawItem]:
    is_proxy = form in _SCHEDULE_FORMS
    lookback = _SCHEDULE_PROXY_LOOKBACK_DAYS if is_proxy else _SCHEDULE_LOOKBACK_DAYS
    if fdate < (date.today() - timedelta(days=lookback)).isoformat():
        return None
    if not is_proxy:
        codes = {c.strip() for c in item_code.split(",")} if item_code else set()
        if not codes & _SCHEDULE_ITEM_CODES:
            return None
    # Check the cover page first (cheap — already have the URL, no extra
    # index lookup), then fall back to the press-release exhibit only if
    # that comes up empty, since most filings have one and it's where this
    # sort of language actually lives.
    candidates = [filing_url]
    for i, doc_url in enumerate(candidates):
        try:
            r = _get(session, doc_url, cfg)
            r.raise_for_status()
            doc_text = BeautifulSoup(r.content, "lxml").get_text(" ", strip=True)
            found = _find_scheduled_date(doc_text, date.today())
        except Exception:
            found = None
        if found:
            sched_date, keyword, sched_time = found
            return RawItem(
                ticker=company["ticker"], company_name=company["name"],
                country=company.get("country", ""), region=company.get("region", ""),
                exchange=company.get("exchange", ""),
                source_type="sec_edgar", tier="regulatory",
                # Collapse whitespace: the keyword is matched against text
                # extracted from a filing's HTML, so it can straddle a line
                # break ("Annual Meeting\nOf Stockholders") and carry it
                # straight into the headline.
                title=f"{company['name']}: {' '.join(keyword.split()).title()} scheduled",
                link=doc_url, published=sched_date, published_time=sched_time,
                summary=f"Found in {form} filed {fdate}.", publisher="sec.gov",
                # Deliberately no sec_item here, unlike the filing-fact item
                # above: setting it would make classify() bypass keyword
                # scoring via sec_item_map and force this into "Earnings
                # Release" for every 2.02 filing, even when the title says
                # "Conference Call scheduled" or "Capital Markets Day
                # scheduled" — the keyword match on the title itself lands
                # this in the right category instead.
                sec_item=None,
            )
        if i == 0:
            candidates.extend(_find_exhibit_urls(session, cfg, cik, accn_nodash))
    return None


def _filing_rows(block: dict) -> list[tuple]:
    """Flatten one submissions block (the "recent" object, or an older shard
    file, which uses the same column-arrays shape) into per-filing tuples."""
    forms = block.get("form", []) or []
    return list(zip(
        forms,
        block.get("filingDate", []) or [],
        block.get("accessionNumber", []) or [],
        block.get("items", [""] * len(forms)) or [""] * len(forms),
        block.get("primaryDocument", [""] * len(forms)) or [""] * len(forms),
    ))


def fetch_sec_edgar(source: dict, company: dict, cfg: dict, session: requests.Session) -> list[RawItem]:
    cik = str(source.get("cik") or "").strip()
    if cik:
        cik = cik.zfill(10)
    else:
        cik = resolve_cik(session, cfg, company["ticker"])
        if not cik:
            return []
    if not cik.strip("0"):
        return []
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    r = _get(session, url, cfg)
    r.raise_for_status()
    data = r.json()
    filings = data.get("filings") or {}
    history_from = cfg.get("run", {}).get("history_from")

    # "recent" holds only the latest slice of a company's filing history —
    # roughly the last thousand filings — and everything older lives in
    # separate shard files that this never read. For an active filer that
    # slice can cover as little as a year or two, which is why history
    # thinned out sharply the further back you looked (64 events stored for
    # 2020 against 6,678 for 2025) even though history_from asked for 2020.
    # Each shard declares the date range it covers, so only the ones that
    # actually overlap the requested window are fetched.
    rows = _filing_rows(filings.get("recent") or {})
    for shard in (filings.get("files") or []):
        if history_from and str(shard.get("filingTo", "")) < history_from:
            continue
        name = str(shard.get("name", "")).strip()
        if not name:
            continue
        try:
            sr = _get(session, f"https://data.sec.gov/submissions/{name}", cfg)
            sr.raise_for_status()
            rows.extend(_filing_rows(sr.json()))
        except Exception:
            # One unreadable shard shouldn't cost the company its recent
            # filings, which are already gathered above.
            continue

    max_items = int(cfg.get("run", {}).get("max_items_per_source", 250))
    items = []
    for form, fdate, accn, item_code, primary_doc in rows[:max_items]:
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
            # The filing date is a matter of record and never moves, and
            # these titles repeat verbatim across a year's filings — so the
            # exact date, not the year, is what makes each filing its own
            # event. See event_id().
            date_is_fact=True,
        ))
        scheduled = _fetch_scheduled_event(session, cfg, company, cik, accn_nodash,
                                            filing_url, form, fdate, item_code)
        if scheduled:
            items.append(scheduled)
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
        title = (entry.get("title") or "").strip()[:300]
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
