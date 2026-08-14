"""Apply a roster-change request submitted as a GitHub issue form.

The published dashboard has no server behind it, so its "add / remove
companies" panel can't write to companies.yaml directly. Instead each
control opens one of the issue forms in .github/ISSUE_TEMPLATE, and the
roster-request workflow runs this to apply it — which keeps the roster in
the repo (reviewable, revertible) while still letting someone change it
from the page without installing anything or hand-editing a file.

Reads the issue body on stdin, writes a one-line result to stdout, and
exits non-zero when nothing was applied so the workflow can report back.
"""
from __future__ import annotations

import re
import sys

from tracker.addco import add_company, remove_company, set_enabled, list_companies

COMPANIES = "companies.yaml"
CONFIG = "config.yaml"

# Bloomberg-style "<code> <market> Equity" -> the suffixed form the roster
# uses. Worth translating rather than rejecting: the roster was imported
# from a Bloomberg-shaped spreadsheet, so that's the notation someone
# looking at this list will have to hand, and "9987 HK Equity" would
# otherwise be stored verbatim as a ticker that matches nothing.
_BLOOMBERG_MARKETS = {
    "HK": (".HK", "HKEX", "Hong Kong"),
    "JP": (".T", "TSE", "Japan"),
    "JT": (".T", "TSE", "Japan"),
    "TT": (".TW", "TWSE", "Taiwan"),
    "KS": (".KS", "KRX", "Korea"),
    "KP": (".KS", "KRX", "Korea"),
    "CH": (".CH", "SSE / SZSE", "China"),
    "C1": (".CH", "SSE / SZSE", "China"),
    "C2": (".CH", "SSE / SZSE", "China"),
    "US": ("", "", "US"),
    "UN": ("", "NYSE", "US"),
    "UW": ("", "NASDAQ", "US"),
    "UQ": ("", "NASDAQ", "US"),
}
_BLOOMBERG_RE = re.compile(
    r"^\s*([A-Za-z0-9]+)\s+([A-Za-z0-9]{2})(?:\s+Equity)?\s*$", re.IGNORECASE)


def normalise_ticker(raw: str) -> tuple[str, str, str]:
    """Returns (ticker, exchange, country) — exchange/country empty unless a
    Bloomberg market code supplied them."""
    raw = (raw or "").strip()
    m = _BLOOMBERG_RE.match(raw)
    if not m:
        return raw, "", ""
    code, market = m.group(1).upper(), m.group(2).upper()
    if market not in _BLOOMBERG_MARKETS:
        return raw, "", ""
    suffix, exchange, country = _BLOOMBERG_MARKETS[market]
    return f"{code}{suffix}", exchange, country


def parse_form(body: str) -> dict:
    """GitHub renders an issue form as "### Label\n\n value" sections. Absent
    optional fields come through as the literal "_No response_"."""
    fields = {}
    for m in re.finditer(r"^###\s+(.+?)\s*\n+(.*?)(?=\n###\s|\Z)", body, re.S | re.M):
        label = m.group(1).strip().lower()
        value = m.group(2).strip()
        if value in ("_No response_", "_No response_.", ""):
            continue
        fields[label] = value
    return fields


def main() -> int:
    body = sys.stdin.read()
    f = parse_form(body)
    raw_ticker = f.get("ticker", "").strip()
    if not raw_ticker:
        print("No ticker in the request, so there was nothing to apply.")
        return 1
    ticker, bb_exchange, bb_country = normalise_ticker(raw_ticker)
    note = "" if ticker == raw_ticker else f" (read '{raw_ticker}' as {ticker})"

    action = f.get("what should happen", "").strip().lower()
    if action.startswith("remove"):
        res = remove_company(ticker, COMPANIES)
    elif action.startswith("pause"):
        current = {c["ticker"]: c for c in list_companies(COMPANIES)}.get(ticker)
        if not current:
            print(f"{ticker} isn't on the roster, so there was nothing to pause or resume.")
            return 1
        res = set_enabled(ticker, not current.get("enabled", True), COMPANIES)
    else:
        aliases = [a.strip() for a in f.get("other names it's reported under", "").split(",") if a.strip()]
        res = add_company(
            ticker, f.get("company name", ""),
            ir_url=f.get("investor relations page") or None,
            # A Bloomberg market code already says which exchange and country
            # this is, so fill those in when the form left them blank rather
            # than storing the company with neither (both are filters on the
            # dashboard, and "Other"/blank hides it from both).
            exchange=f.get("exchange") or bb_exchange,
            country=f.get("country") or bb_country,
            sub_sector=f.get("sub-sector", ""), cik=f.get("sec cik") or None,
            aliases=aliases, config_path=CONFIG, companies_path=COMPANIES,
        )

    print(res.get("message", "Nothing to report.") + note)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
