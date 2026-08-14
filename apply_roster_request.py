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
    ticker = f.get("ticker", "").strip()
    if not ticker:
        print("No ticker in the request, so there was nothing to apply.")
        return 1

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
            exchange=f.get("exchange", ""), country=f.get("country", ""),
            sub_sector=f.get("sub-sector", ""), cik=f.get("sec cik") or None,
            aliases=aliases, config_path=CONFIG, companies_path=COMPANIES,
        )

    print(res.get("message", "Nothing to report."))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
