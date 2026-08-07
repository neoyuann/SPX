"""Command-line entry point: python -m tracker <command> ..."""
from __future__ import annotations

import argparse
import os
import sys

from .addco import add_company, list_companies
from .pipeline import load_yaml, run as run_pipeline
from .render import build_payload, page_html
from .sources import link_is_alive, make_session


def cmd_serve(args):
    from .server import serve
    serve(config_path=args.config, companies_path=args.companies,
          port=args.port, open_browser=args.open)


def cmd_add(args):
    cfg = load_yaml(args.config) if os.path.exists(args.config) else {}
    result = add_company(
        args.ticker, args.name, ir_url=args.ir, exchange=args.exchange or "",
        region=args.region or "", country=args.country or "", cik=args.cik,
        aliases=args.alias or [], no_news=args.no_news,
        config_path=args.config, companies_path=args.companies)
    print(result["message"])
    sys.exit(0 if result["ok"] else 1)


def cmd_list(args):
    companies = list_companies(args.companies)
    if not companies:
        print("No companies tracked yet.")
        return
    w = max(len(c["ticker"]) for c in companies)
    for c in companies:
        flag = "" if c["enabled"] else "  [paused]"
        print(f"{c['ticker']:<{w}}  {c['name']:<40}  {c['source_count']} sources{flag}")


def cmd_verify(args):
    cfg = load_yaml(args.config)
    roster = load_yaml(args.companies)
    session = make_session(cfg.get("run", {}).get("user_agent", "CorporateEventTracker/1.0"))
    ok_count = fail_count = 0
    for c in roster.get("companies", []):
        for s in c.get("sources", []):
            url = s.get("url")
            if not url:
                continue
            alive = link_is_alive(session, url, cfg)
            status = "OK  " if alive else "FAIL"
            print(f"[{status}] {c['ticker']:<10} {s['type']:<9} {url}")
            ok_count += alive
            fail_count += (not alive)
    print(f"\n{ok_count} ok, {fail_count} failed.")
    sys.exit(1 if fail_count else 0)


def cmd_discover(args):
    import re
    from bs4 import BeautifulSoup
    cfg = load_yaml(args.config)
    roster = load_yaml(args.companies)
    company = next((c for c in roster.get("companies", []) if c["ticker"] == args.ticker), None)
    if not company:
        print(f"No company with ticker {args.ticker!r} in {args.companies}.")
        sys.exit(1)
    session = make_session(cfg.get("run", {}).get("user_agent", "CorporateEventTracker/1.0"))
    found = []
    for s in company.get("sources", []):
        if s.get("type") != "ir_page":
            continue
        try:
            timeout = tuple(cfg.get("run", {}).get("request_timeout", [6, 15]))
            r = session.get(s["url"], timeout=timeout)
            r.raise_for_status()
            soup = BeautifulSoup(r.content, "lxml")
            for link in soup.find_all("link", attrs={"type": re.compile("rss|atom")}):
                if link.get("href"):
                    found.append(link["href"])
        except Exception as exc:
            print(f"  could not fetch {s['url']}: {exc}")
    if found:
        print("Found feed(s) — paste-ready block:\n")
        print(f"      - type: rss\n        tier: company_ir\n        url: {found[0]}")
    else:
        print("No feed found. Keep the ir_page source and tune its selectors instead.")


def cmd_run(args):
    cfg_check = load_yaml(args.config)
    if "you@example.com" in str(cfg_check.get("run", {}).get("user_agent", "")):
        print("[warn] config.yaml's run.user_agent still has the placeholder email — "
              "SEC EDGAR silently rejects requests without a real contact address, "
              "so every US company's sec_edgar source will fail until you put yours in.")
    result = run_pipeline(args.config, args.companies, verbose=True,
                           only_ticker=args.ticker, no_news=args.no_news)
    cfg = load_yaml(args.config)
    roster = load_yaml(args.companies)
    events, meta = build_payload(cfg, roster)
    out_path = cfg["output"]["html_path"]
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page_html(events, meta, live=False))
    print(f"Wrote {out_path}")


def cmd_render(args):
    cfg = load_yaml(args.config)
    roster = load_yaml(args.companies)
    events, meta = build_payload(cfg, roster)
    out_path = cfg["output"]["html_path"]
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page_html(events, meta, live=False))
    print(f"Wrote {out_path}")


def main():
    p = argparse.ArgumentParser(prog="python -m tracker")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--companies", default="companies.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("serve", help="run the local server")
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--open", action="store_true")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("add", help="add a company")
    sp.add_argument("-t", "--ticker", required=True)
    sp.add_argument("-n", "--name", required=True)
    sp.add_argument("--ir", help="investor relations URL")
    sp.add_argument("--exchange")
    sp.add_argument("--region")
    sp.add_argument("--country")
    sp.add_argument("--cik")
    sp.add_argument("--alias", action="append")
    sp.add_argument("--no-news", action="store_true")
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser("list", help="show the roster")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("verify", help="check every configured URL")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("discover", help="find feed URLs for a company")
    sp.add_argument("-t", "--ticker", required=True)
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("run", help="one-off fetch, writes a static page")
    sp.add_argument("-t", "--ticker")
    sp.add_argument("--no-news", action="store_true")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("render", help="rebuild the static page without fetching")
    sp.set_defaults(func=cmd_render)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
