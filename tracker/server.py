"""Local server.

Serves the dashboard at http://127.0.0.1:<port>. Every visit rebuilds the page
from the database and serves it immediately — visiting or refreshing the page
never itself starts a scrape, so you never wait on one. Instead, `serve` keeps
the data fresh on its own: a refresh cycle starts the moment this process
launches, then again every `refresh.background_interval_minutes`, for as long
as it keeps running, regardless of whether anyone is looking at the page. The
practical upshot: start the tracker before you plan to check it (leave
`START TRACKER (Windows).bat` open, or host `serve` somewhere always-on) and
it's already current by the time you open the browser. **Refresh now** is
still there for "I want the very latest, right now" — it forces an extra
cycle on top of the background one, it doesn't replace it.

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
from .render import _to_sgt, build_payload, page_html
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
        self.next_background_run = ""
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

    def maybe_refresh(self, force: bool = True) -> bool:
        """Start a full refresh if one isn't already going. Returns True if
        started. `force` exists for the call shape, not as a real gate any
        more — a visit never calls this (see do_GET), so the only callers
        left are the background scheduler and the Refresh now button, both
        of which always want one started regardless of how fresh the data
        already looks."""
        if self.running:
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
                "last_run": _sgt(self.last_finished),
                "data_version": self.data_version,
                "error": self.last_error,
                "next_background_run": self.next_background_run,
                "history": self.history()}

    def history(self, limit: int = 20) -> list:
        try:
            store = Store(self.cfg()["output"]["db_path"])
            rows = store.refresh_history(limit)
            store.close()
        except Exception:
            return []
        for r in rows:
            r["finished_at"] = _sgt(r.get("finished_at"))
            r["started_at"] = _sgt(r.get("started_at"))
        return rows

    def payload(self) -> dict:
        events, meta = build_payload(self.cfg(), self.roster())
        return {"events": events, "meta": meta, "version": self.data_version}


def _sgt(value):
    """Every timestamp the page shows is Singapore time — stored as UTC,
    converted here so the page never has to (it used to just label a raw
    UTC value " SGT" without actually converting it, which was wrong by
    exactly the UTC+8 offset)."""
    dt = parse_utc(value)
    return _to_sgt(dt.isoformat()) if dt else ""


def background_scheduler(manager: RefreshManager):
    """Keeps the data current on its own, independent of anyone visiting —
    that's the whole point: a page load never triggers a scrape (see
    do_GET), so something has to. The first cycle starts the moment this
    thread does (i.e. the moment `serve` launches), not at a fixed clock
    time, so restarting the tracker doesn't mean waiting until tomorrow for
    fresh data. After that it repeats every
    `refresh.background_interval_minutes`, measured start-to-start: if a
    cycle takes 20 minutes and the interval is 60, there's about 40 minutes
    idle before the next one begins. A cycle that overruns the interval
    just runs back-to-back into the next one, with a small floor so this
    loop can never spin without pausing at all.
    """
    while True:
        cycle_started = time.time()
        manager.maybe_refresh(force=True)
        while manager.running:
            time.sleep(1)

        ref = manager.cfg().get("refresh", {}) or {}
        interval_minutes = float(ref.get("background_interval_minutes", 60))
        elapsed = time.time() - cycle_started
        wait = max(60.0, interval_minutes * 60 - elapsed)
        target = _now() + timedelta(seconds=wait)
        manager.next_background_run = _to_sgt(target.isoformat())
        print(f"[background] refresh done, next one at {target:%Y-%m-%d %H:%M} UTC "
              f"(in {wait/60:.0f} min)")
        time.sleep(wait)


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
            # Deliberately does not trigger a scrape. Visiting or refreshing
            # the page always just renders whatever's already in the
            # database, instantly — keeping that current is background_
            # scheduler's job, running independently of anyone looking.
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
    if "you@example.com" in str(cfg.get("run", {}).get("user_agent", "")):
        print("[warn] config.yaml's run.user_agent still has the placeholder email — "
              "SEC EDGAR silently rejects requests without a real contact address, "
              "so every US company's sec_edgar source will fail until you put yours in.")

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

    threading.Thread(target=background_scheduler, args=(manager,), daemon=True).start()

    url = f"http://127.0.0.1:{port}"
    server = ThreadingHTTPServer((host, port), Handler)
    if on_host:
        print(f"Corporate event monitor listening on {host}:{port}")
    else:
        print(f"Corporate event monitor running at {url}")
    print("Every visit rebuilds the page from the database instantly — refreshing "
          "runs in the background, on its own schedule, whether or not anyone's looking.")
    print("Ctrl+C to stop.")
    if open_browser and not on_host:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()
