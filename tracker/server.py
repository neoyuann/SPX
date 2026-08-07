"""Local server.

Serves the dashboard at http://127.0.0.1:<port>. Every visit rebuilds the page
from the database, so you always see the latest stored data immediately — and
if that data is stale, a re-scrape starts in the background and the page updates
itself when it finishes. You never wait on a spinner.

A daily run is scheduled from `refresh.daily_at` in config.yaml, so the tracker
stays current even on days you don't open it.

    python -m tracker serve
    python -m tracker serve --port 9000 --open
"""
from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .pipeline import run as run_pipeline, load_yaml
from .render import build_payload, page_html
from .store import Store


def _now():
    return datetime.now(timezone.utc)


def parse_utc(value):
    """Parse a stored timestamp into an aware UTC datetime, or None.

    Timestamps have been written by more than one code path over time, some
    with an offset and some without, so assume UTC when none is given.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class RefreshManager:
    """Owns the scrape. One at a time, always on a background thread."""

    def __init__(self, config_path: str, companies_path: str):
        self.config_path = config_path
        self.companies_path = companies_path
        self.lock = threading.Lock()
        self.running = False
        self.message = ""
        self.data_version = 0
        self.last_error = ""
        self.next_daily = ""
        self.last_finished = self._last_finished_from_db()

    # -- config is re-read every time so edits to companies.yaml take effect
    #    on the next refresh without restarting the server
    def cfg(self):
        return load_yaml(self.config_path)

    def roster(self):
        return load_yaml(self.companies_path)

    def _last_finished_from_db(self):
        try:
            store = Store(self.cfg()["output"]["db_path"])
            last = store.last_run()
            store.close()
            return last.get("finished_at") or ""
        except Exception:
            return ""

    def is_stale(self) -> bool:
        ref = self.cfg().get("refresh", {}) or {}
        if not ref.get("on_visit", True):
            return False
        last = parse_utc(self.last_finished)
        if last is None:
            return True
        mins = float(ref.get("min_interval_minutes", 30))
        return _now() - last > timedelta(minutes=mins)

    def maybe_refresh(self, force: bool = False) -> bool:
        """Start a full refresh if one isn't already going. Returns True if started."""
        if self.running:
            return False
        if not force and not self.is_stale():
            return False
        with self.lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self._work, args=(None,), daemon=True).start()
        return True

    def start_scoped(self, ticker: str) -> bool:
        """Fetch one company's full history right away — used right after
        it's added, so its events show up without waiting for the next
        stale-triggered or daily refresh. Skipped (not queued) if a refresh
        is already running; that company still gets picked up by it, or by
        the next one, since it's already in companies.yaml by then."""
        if self.running:
            return False
        with self.lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self._work, args=(ticker,), daemon=True).start()
        return True

    def _work(self, only_ticker: str | None):
        started = time.time()
        try:
            if only_ticker:
                self.message = f"pulling history for {only_ticker}…"
            else:
                roster = self.roster()
                names = [c["ticker"] for c in roster.get("companies", []) if c.get("enabled", True)]
                n_sources = sum(len(c.get("sources", [])) for c in roster.get("companies", [])
                               if c.get("enabled", True))
                self.message = f"starting · {len(names)} companies, {n_sources} sources"

            def progress(done, total, label):
                self.message = f"{done} of {total} sources · {label}"

            run_pipeline(self.config_path, self.companies_path,
                         verbose=False, progress=progress, only_ticker=only_ticker)
            self.last_error = ""
        except Exception as exc:
            self.last_error = str(exc)[:200]
        finally:
            self.last_finished = self._last_finished_from_db() or \
                _now().isoformat(timespec="seconds")
            self.data_version += 1
            self.running = False
            self.message = ""
            print(f"[refresh] finished in {time.time()-started:.0f}s"
                  + (f" — error: {self.last_error}" if self.last_error else ""))

    def status(self) -> dict:
        return {"running": self.running, "message": self.message,
                "last_run": _iso(self.last_finished),
                "data_version": self.data_version,
                "error": self.last_error,
                "next_daily": self.next_daily,
                "history": self.history()}

    def history(self, limit: int = 20) -> list:
        try:
            store = Store(self.cfg()["output"]["db_path"])
            rows = store.refresh_history(limit)
            store.close()
        except Exception:
            return []
        for r in rows:
            r["finished_at"] = _iso(r.get("finished_at"))
            r["started_at"] = _iso(r.get("started_at"))
        return rows

    def payload(self) -> dict:
        events, meta = build_payload(self.cfg(), self.roster())
        return {"events": events, "meta": meta, "version": self.data_version}


def _iso(value):
    """Always hand the page an unambiguous UTC timestamp; it renders SGT."""
    dt = parse_utc(value)
    return dt.isoformat(timespec="seconds") if dt else ""


def daily_scheduler(manager: RefreshManager):
    """Fire a refresh once a day at refresh.daily_at."""
    while True:
        ref = manager.cfg().get("refresh", {}) or {}
        at = ref.get("daily_at")
        if not at:
            time.sleep(600)
            continue
        try:
            hh, mm = [int(x) for x in str(at).split(":")[:2]]
        except Exception:
            print(f"[daily] can't read refresh.daily_at = {at!r}; expected HH:MM")
            time.sleep(3600)
            continue
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        manager.next_daily = target.isoformat(timespec="seconds")
        print(f"[daily] next scheduled run {target:%Y-%m-%d %H:%M}")
        time.sleep(wait)
        print("[daily] starting scheduled refresh")
        manager.maybe_refresh(force=True)
        time.sleep(60)   # don't re-fire inside the same minute


class Handler(BaseHTTPRequestHandler):
    manager: RefreshManager = None

    def log_message(self, fmt, *args):
        pass   # the default logger is too chatty for a personal tool

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        m = Handler.manager
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            # A failure here must not blank the page, so anything unexpected
            # from the staleness check is swallowed and logged.
            # Kick off a background refresh if the data is stale, then serve
            # what we have straight away. The page picks up the new data itself.
            try:
                m.maybe_refresh()
            except Exception as exc:
                print(f"[warn] couldn't start background refresh: {exc}")
            events, meta = build_payload(m.cfg(), m.roster())
            self._send(200, page_html(events, meta, live=True), "text/html; charset=utf-8")

        elif path == "/api/ping":
            # A dedicated, trivial route the page checks once on load so it
            # can tell "no live server" apart from "server had an error" —
            # both used to show up to the reader as the same vague failure.
            self._send(200, json.dumps({"ok": True}), "application/json")

        elif path == "/api/status":
            self._json_route(m.status)

        elif path == "/api/companies":
            from .addco import list_companies
            self._json_route(lambda: {"companies": list_companies(m.companies_path)})

        elif path == "/api/data":
            self._json_route(m.payload)

        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")

    def _json_route(self, fn):
        """Run fn() and send its result as JSON. On failure, still send valid
        JSON with an 'error' field — never let an exception fall through to a
        bare HTML error page, which breaks res.json() on the client and reads
        to the person as a dead server when the server is actually fine."""
        try:
            self._send(200, json.dumps(fn(), ensure_ascii=False),
                       "application/json; charset=utf-8")
        except Exception as exc:
            print(f"[error] {self.path}: {exc}")
            self._send(200, json.dumps({"error": str(exc)[:300]}),
                       "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_POST(self):
        m = Handler.manager
        path = self.path.split("?")[0]

        if path == "/api/refresh":
            try:
                started = m.maybe_refresh(force=True)
                # `started` is False when a refresh was already under way —
                # that's a normal outcome, not a failure, and the page says so.
                self._send(200, json.dumps({"started": started, **m.status()}),
                           "application/json")
            except Exception as exc:
                print(f"[error] /api/refresh: {exc}")
                self._send(200, json.dumps({"started": False, "running": False,
                                            "error": str(exc)[:300]}), "application/json")
            return

        if path.startswith("/api/companies/"):
            from . import addco
            action = path.rsplit("/", 1)[-1]
            body = self._body()
            try:
                if action == "add":
                    result = addco.add_company(
                        body.get("ticker", ""), body.get("name", ""),
                        ir_url=body.get("ir") or None,
                        exchange=body.get("exchange", ""),
                        region=body.get("region", ""),
                        country=body.get("country", ""),
                        sub_sector=body.get("sub_sector", ""),
                        cik=body.get("cik") or None,
                        aliases=[a.strip() for a in (body.get("aliases") or "").split(",") if a.strip()],
                        no_news=bool(body.get("no_news")),
                        config_path=m.config_path, companies_path=m.companies_path)
                    if result.get("ok"):
                        started = m.start_scoped(body.get("ticker", "").strip())
                        result["message"] = result["message"] + (
                            " Pulling its history now — it'll appear as soon as that finishes."
                            if started else
                            " A refresh is already running; its history will follow that, or press Refresh now once it's done.")
                elif action == "toggle":
                    result = addco.set_enabled(body.get("ticker", ""),
                                               bool(body.get("enabled")),
                                               companies_path=m.companies_path)
                elif action == "remove":
                    result = addco.remove_company(body.get("ticker", ""),
                                                  companies_path=m.companies_path)
                else:
                    result = {"ok": False, "message": f"Unknown action '{action}'."}
            except Exception as exc:
                result = {"ok": False, "message": f"Couldn't write companies.yaml: {exc}"}

            try:
                from .addco import list_companies
                result["companies"] = list_companies(m.companies_path)
            except Exception as exc:
                result["companies"] = []
                result.setdefault("message", f"Saved, but couldn't reload the list: {exc}")
            self._send(200, json.dumps(result, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return

        self._send(404, "Not found", "text/plain; charset=utf-8")


def serve(config_path="config.yaml", companies_path="companies.yaml",
          port=None, open_browser=False):
    manager = RefreshManager(config_path, companies_path)
    Handler.manager = manager
    cfg = manager.cfg()
    # Parsing companies.yaml is the one part of a page view that scales with
    # roster size — cheap for a handful of companies, a real fraction of a
    # second (or a few, with a roster in the thousands) for a big one. Pay
    # that cost once here rather than on whichever browser tab happens to
    # be first; every request after this hits the warm cache.
    print("Loading companies.yaml...")
    n_companies = len(manager.roster().get("companies", []))
    print(f"Tracking {n_companies} companies.")

    # A host like Render assigns the port at run time and tells the app via
    # $PORT. It also routes traffic from outside the container, so binding to
    # 127.0.0.1 there would make the service unreachable and the deploy would
    # fail its health check. Locally we keep 127.0.0.1, which is the safer
    # default because it can't be reached from the rest of the network.
    env_port = os.environ.get("PORT")
    on_host = bool(env_port)
    port = port or (int(env_port) if env_port else None) \
        or (cfg.get("refresh", {}) or {}).get("port", 8765)
    host = "0.0.0.0" if on_host else "127.0.0.1"

    threading.Thread(target=daily_scheduler, args=(manager,), daemon=True).start()

    url = f"http://127.0.0.1:{port}"
    server = ThreadingHTTPServer((host, port), Handler)
    if on_host:
        print(f"Corporate event monitor listening on {host}:{port}")
    else:
        print(f"Corporate event monitor running at {url}")
    print("Every visit rebuilds the page from the database and refreshes stale "
          "sources in the background.")
    print("Ctrl+C to stop.")
    if open_browser and not on_host:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()
