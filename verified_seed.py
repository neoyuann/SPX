"""Loads a handful of hand-verified real events straight into the database,
no network needed. Useful to see the dashboard populated immediately after
cloning, before your first real `python -m tracker run`.

Each entry below was checked against its primary source at the time this
file was written (August 2026) — the URL in each `sources` list is the
actual company release or filing, not a search result.

    python verified_seed.py
"""
from __future__ import annotations

from tracker.models import content_hash, event_id
from tracker.pipeline import load_yaml
from tracker.store import Store

SEED = [
    {
        "ticker": "STM", "company_name": "STMicroelectronics N.V.",
        "category": "earnings_release", "label": "Earnings Release",
        "headline": "STMicroelectronics reports Second Quarter 2026 financial results",
        "summary": "Q2 2026 results announced before market open, followed by a "
                    "conference call at 9:30 a.m. CET the same day.",
        "event_date": "2026-07-23", "event_time": "09:30 CET",
        "country": "France", "region": "EU", "exchange": "NYSE / Euronext Paris",
        "tier": "company_ir",
        "primary_url": "https://newsroom.st.com/media-center/press-item.html/c3399.html",
        "matched_on": ["8-K item 2.02", "quarter financial results"],
        "sources": [
            {"url": "https://newsroom.st.com/media-center/press-item.html/c3399.html",
             "tier": "company_ir", "source_type": "ir_page", "publisher": "newsroom.st.com",
             "title": "STMicroelectronics announces timing for Q2 2026 earnings release and conference call"},
            {"url": "https://www.globenewswire.com/news-release/2026/07/03/3321641/0/en/"
                    "STMicroelectronics-Announces-Timing-for-Second-Quarter-2026-Earnings-"
                    "Release-and-Conference-Call.html",
             "tier": "newswire", "source_type": "news", "publisher": "globenewswire.com",
             "title": "STMicroelectronics Announces Timing for Second Quarter 2026 Earnings Release and Conference Call"},
        ],
    },
    {
        "ticker": "IFX", "company_name": "Infineon Technologies AG",
        "category": "earnings_release", "label": "Earnings Release",
        "headline": "Infineon: Q3 FY2026 concluded with record sales driven by AI demand",
        "summary": "Fiscal Q3 2026 results, revenue €4.172bn, segment margin 19.1%; "
                    "full-year 2026 outlook raised to around €16.3bn.",
        "event_date": "2026-08-05", "event_time": None,
        "country": "Germany", "region": "EU", "exchange": "XETRA",
        "tier": "company_ir",
        "primary_url": "https://www.infineon.com/press-release/2026/infpr202608-125",
        "matched_on": ["quarter financial results", "revenue"],
        "sources": [
            {"url": "https://www.infineon.com/press-release/2026/infpr202608-125",
             "tier": "company_ir", "source_type": "ir_page", "publisher": "infineon.com",
             "title": "Q3 FY 2026 concluded with record sales driven by strong AI business"},
        ],
    },
    {
        "ticker": "NXPI", "company_name": "NXP Semiconductors N.V.",
        "category": "earnings_call", "label": "Earnings Call",
        "headline": "NXP Semiconductors Q2 2026 earnings conference call",
        "summary": "Conference call to review second-quarter 2026 results, "
                    "4:30 p.m. Eastern Daylight Time.",
        "event_date": "2026-07-28", "event_time": "16:30 EDT",
        "country": "United States", "region": "EU", "exchange": "NASDAQ",
        "tier": "regulatory",
        "primary_url": "https://www.sec.gov/Archives/edgar/data/0001413447/000141344726000044/nxp2q26exhibit991.htm",
        "matched_on": ["8-K item 2.02", "conference call"],
        "sources": [
            {"url": "https://www.sec.gov/Archives/edgar/data/0001413447/000141344726000044/nxp2q26exhibit991.htm",
             "tier": "regulatory", "source_type": "sec_edgar", "publisher": "sec.gov",
             "title": "NXP Semiconductors N.V. Q2 2026 8-K exhibit 99.1"},
        ],
    },
    {
        "ticker": "ASML", "company_name": "ASML Holding N.V.",
        "category": "earnings_release", "label": "Earnings Release",
        "headline": "ASML reports €9.3 billion total net sales and €2.9 billion net income in Q2 2026",
        "summary": "Q2 2026 results with investor call at 15:00 CET; full-year 2026 "
                    "net sales outlook raised to €43-45bn.",
        "event_date": "2026-07-15", "event_time": "15:00 CET",
        "country": "Netherlands", "region": "EU", "exchange": "NASDAQ / Euronext Amsterdam",
        "tier": "company_ir",
        "primary_url": "https://www.asml.com/en/news/press-releases/2026/q2-2026-financial-results",
        "matched_on": ["quarter financial results", "net income"],
        "sources": [
            {"url": "https://www.asml.com/en/news/press-releases/2026/q2-2026-financial-results",
             "tier": "company_ir", "source_type": "ir_page", "publisher": "asml.com",
             "title": "ASML reports Q2 2026 financial results"},
        ],
    },
]


def main():
    cfg = load_yaml("config.yaml")
    store = Store(cfg["output"]["db_path"])
    for e in SEED:
        eid = event_id(e["ticker"], e["category"], e["event_date"], e["headline"])
        chash = content_hash(e["headline"], e["category"], e["event_date"],
                              e["event_time"], e["summary"])
        event = {**e, "id": eid, "content_hash": chash}
        status = store.upsert_event(event, e["sources"])
        print(f"  {e['ticker']:<6} {e['headline'][:60]:<60} [{status}]")
    store.close()
    print(f"\nSeeded {len(SEED)} verified events into {cfg['output']['db_path']}.")
    print("Run `python -m tracker render` to build out/dashboard.html, or "
          "`python -m tracker serve` to see them live.")


if __name__ == "__main__":
    main()
