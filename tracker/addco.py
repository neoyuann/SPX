"""Adds, pauses and removes companies by editing companies.yaml as text —
not by round-tripping it through a YAML dumper, which would strip every
comment in the file. Existing entries are edited in place with targeted
regexes; new entries are appended as a freshly formatted block. Every
write is validated by re-parsing the result before it touches disk; a
change that would produce invalid YAML is rejected and the file is left
untouched.
"""
from __future__ import annotations

import json
import os
import re
import tempfile

import yaml

from .sources import make_session

# Ticker may or may not be quoted in the file (see yaml_scalar below), so
# the block finder tolerates both — \b sits right after the ticker itself,
# before any closing quote, since \b never matches between two non-word
# characters (a quote followed by a newline, say).
_BLOCK_RE_TMPL = r"(\n[ \t]*-\s*ticker:\s*[\"']?{ticker}\b[\"']?.*?)(?=\n[ \t]*-\s*ticker:|\Z)"

# A roster of thousands of companies takes real time to parse with PyYAML's
# pure-Python loader; the libyaml-backed one (when available) is far
# faster, and every add/pause/remove here re-parses the whole file at
# least once to validate it.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _yaml_load(text: str) -> dict:
    return yaml.load(text, Loader=_YAML_LOADER) or {}


def yaml_scalar(value) -> str:
    """A YAML double-quoted scalar for any string. Always quoting is
    cheaper and safer than trying to detect which values need it — plain
    `ticker: ON` silently becomes the boolean `true` under YAML 1.1 (same
    for NO/YES/OFF/NULL/…, the classic "Norway problem"), and ON
    Semiconductor's own ticker is literally "ON". JSON string escaping
    produces a valid YAML double-quoted scalar for any input, colons and
    all, without needing a YAML-dumper round trip that would reformat the
    rest of the file."""
    return json.dumps(str(value), ensure_ascii=False)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _write_atomic(path: str, text: str):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".companies_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _validate(text: str) -> tuple[bool, str]:
    try:
        data = _yaml_load(text)
    except yaml.YAMLError as exc:
        return False, f"That change would produce invalid YAML: {exc}"
    companies = data.get("companies", [])
    tickers = [c.get("ticker") for c in companies]
    if len(tickers) != len(set(tickers)):
        return False, "That change would create a duplicate ticker."
    return True, ""


def list_companies(companies_path: str) -> list:
    # The read-only listing path is hit far more often than adds/edits (the
    # companies panel re-fetches it after every action), so it goes through
    # pipeline.load_yaml's mtime cache instead of a fresh parse each time.
    from .pipeline import load_yaml
    data = load_yaml(companies_path)
    out = []
    for c in data.get("companies", []):
        out.append({
            "ticker": c.get("ticker"), "name": c.get("name"),
            "exchange": c.get("exchange", ""), "country": c.get("country", ""),
            "sub_sector": c.get("sub_sector", ""),
            "region": c.get("region", ""), "enabled": c.get("enabled", True),
            "source_count": len(c.get("sources", [])),
        })
    return out


def _find_block(text: str, ticker: str):
    pattern = _BLOCK_RE_TMPL.format(ticker=re.escape(ticker))
    return re.search(pattern, text, re.DOTALL)


def set_enabled(ticker: str, enabled: bool, companies_path: str) -> dict:
    text = _read(companies_path)
    m = _find_block(text, ticker)
    if not m:
        return {"ok": False, "message": f"No company with ticker {ticker!r} found."}
    block = m.group(1)
    if re.search(r"\n[ \t]*enabled:\s*\S+", block):
        new_block = re.sub(r"(\n[ \t]*enabled:\s*)\S+", rf"\g<1>{str(enabled).lower()}", block, count=1)
    else:
        new_block = block + f"\n    enabled: {str(enabled).lower()}"
    new_text = text[:m.start(1)] + new_block + text[m.end(1):]

    ok, msg = _validate(new_text)
    if not ok:
        return {"ok": False, "message": msg}
    _write_atomic(companies_path, new_text)
    state = "resumed" if enabled else "paused"
    return {"ok": True, "message": f"{ticker} {state}."}


def remove_company(ticker: str, companies_path: str) -> dict:
    text = _read(companies_path)
    m = _find_block(text, ticker)
    if not m:
        return {"ok": False, "message": f"No company with ticker {ticker!r} found."}
    new_text = text[:m.start(1)] + text[m.end(1):]

    ok, msg = _validate(new_text)
    if not ok:
        return {"ok": False, "message": msg}
    _write_atomic(companies_path, new_text)
    return {"ok": True, "message": f"{ticker} removed."}


def _probe_rss(ir_url: str, cfg: dict | None) -> str | None:
    """Best-effort: look for a linked RSS/Atom feed on the IR homepage.
    Network failures here are swallowed — an ir_page source is a fine
    fallback and the company is still added either way."""
    try:
        from bs4 import BeautifulSoup
        timeout = tuple((cfg or {}).get("run", {}).get("request_timeout", [6, 15])) if cfg else (6, 15)
        ua = (cfg or {}).get("run", {}).get("user_agent", "CorporateEventTracker/1.0") if cfg else \
            "CorporateEventTracker/1.0"
        session = make_session(ua)
        r = session.get(ir_url, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "lxml")
        link = soup.find("link", attrs={"type": re.compile("rss|atom")})
        if link and link.get("href"):
            from urllib.parse import urljoin
            return urljoin(ir_url, link["href"])
    except Exception:
        pass
    return None


def _format_block(ticker: str, name: str, exchange: str, region: str, country: str,
                   sub_sector: str, aliases: list, sources: list) -> str:
    lines = [f"\n  - ticker: {yaml_scalar(ticker)}", f"    name: {yaml_scalar(name)}"]
    if exchange:
        lines.append(f"    exchange: {yaml_scalar(exchange)}")
    if region:
        lines.append(f"    region: {yaml_scalar(region)}")
    if country:
        lines.append(f"    country: {yaml_scalar(country)}")
    if sub_sector:
        lines.append(f"    sub_sector: {yaml_scalar(sub_sector)}")
    lines.append("    enabled: true")
    if aliases:
        alias_list = ", ".join(yaml_scalar(a) for a in aliases)
        lines.append(f"    aliases: [{alias_list}]")
    lines.append("    sources:")
    for s in sources:
        lines.append(f"      - type: {s['type']}")
        lines.append(f"        tier: {s['tier']}")
        for key, val in s.items():
            if key in ("type", "tier"):
                continue
            lines.append(f"        {key}: {val}")
    return "\n".join(lines) + "\n"


def add_company(ticker: str, name: str, *, ir_url: str | None = None, exchange: str = "",
                 region: str = "", country: str = "", sub_sector: str = "", cik: str | None = None,
                 aliases: list | None = None, no_news: bool = False,
                 config_path: str | None = None, companies_path: str = "companies.yaml") -> dict:
    ticker = (ticker or "").strip()
    name = (name or "").strip()
    if not ticker or not name:
        return {"ok": False, "message": "Ticker and name are both required."}

    text = _read(companies_path)
    if _find_block(text, ticker):
        return {"ok": False, "message": f"{ticker} is already tracked."}

    cfg = None
    if config_path and os.path.exists(config_path):
        cfg = _yaml_load(_read(config_path))

    sources = []
    if ir_url:
        feed = _probe_rss(ir_url, cfg)
        if feed:
            sources.append({"type": "rss", "tier": "company_ir", "url": feed})
        else:
            sources.append({
                "type": "ir_page", "tier": "company_ir", "url": ir_url,
                "selectors": None,  # replaced below with real nested fields
            })
    if cik:
        sources.append({"type": "sec_edgar", "tier": "regulatory", "cik": f'"{str(cik).strip()}"'})
    elif country.strip().lower() in ("us", "united states", "usa"):
        # No CIK given, but this reads as a US-listed company — a
        # `sec_edgar` source with no `cik:` auto-resolves one from the
        # ticker at fetch time (see tracker/sources.py resolve_cik).
        sources.append({"type": "sec_edgar", "tier": "regulatory"})
    if not no_news:
        sources.append({"type": "news", "tier": "news"})

    if not sources:
        return {"ok": False, "message": "Add at least an IR URL or a SEC CIK."}

    block = _format_block(ticker, name, exchange, region, country, sub_sector, aliases or [], [])
    # ir_page needs nested selector lines that _format_block's flat writer
    # can't express, so splice that source's YAML in by hand.
    body_lines = block.splitlines()
    insert_at = next(i for i, l in enumerate(body_lines) if l.strip() == "sources:") + 1
    src_lines = []
    for s in sources:
        if s["type"] == "ir_page":
            src_lines += [
                "      - type: ir_page", "        tier: company_ir", f"        url: {s['url']}",
                "        selectors:", '          item: "li, tr, article, .news-item"',
                '          title: "a, .title"', '          date: "time, .date, td:first-child"',
                '          link: "a"',
            ]
        elif s["type"] == "rss":
            src_lines += ["      - type: rss", "        tier: company_ir", f"        url: {s['url']}"]
        elif s["type"] == "sec_edgar":
            src_lines += ["      - type: sec_edgar", "        tier: regulatory"]
            if s.get("cik"):
                src_lines.append(f"        cik: {s['cik']}")
        elif s["type"] == "news":
            src_lines += ["      - type: news", "        tier: news"]
    body_lines[insert_at:insert_at] = src_lines
    block = "\n".join(body_lines) + "\n"

    new_text = text.rstrip("\n") + "\n" + block
    ok, msg = _validate(new_text)
    if not ok:
        return {"ok": False, "message": msg}
    _write_atomic(companies_path, new_text)
    note = " (found an RSS feed)" if any(s["type"] == "rss" for s in sources) else \
           " (no feed found, scraping the IR page directly)" if ir_url else ""
    return {"ok": True, "message": f"{ticker} added{note}."}
