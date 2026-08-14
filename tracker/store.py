"""SQLite persistence. One event per real-world happening, with its
corroborating sources in a side table and every change it's ever gone
through in another. This is the only state the tracker keeps between runs.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    company_name TEXT NOT NULL,
    category TEXT NOT NULL,
    label TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT DEFAULT '',
    event_date TEXT,
    event_time TEXT,
    country TEXT,
    region TEXT,
    exchange TEXT,
    tier TEXT,
    primary_url TEXT,
    matched_on TEXT DEFAULT '[]',
    status TEXT DEFAULT 'new',
    prev_event_date TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_sources (
    event_id TEXT NOT NULL,
    url TEXT NOT NULL,
    tier TEXT,
    source_type TEXT,
    publisher TEXT,
    title TEXT,
    FOREIGN KEY (event_id) REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS idx_event_sources_event ON event_sources(event_id);

CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_event ON changes(event_id);

-- Link-check results, cached across runs. Verifying every event's link
-- means a network round-trip per URL, and at a few thousand events that
-- phase costs far more than the fetching it follows — enough that it used
-- to blow its own time budget and silently drop most of a run's output.
-- A URL that answered fine an hour ago is not worth re-asking about every
-- single hourly run, so results are remembered and only rechecked once
-- they go stale (see pipeline.link_check_ttl_hours).
CREATE TABLE IF NOT EXISTS link_checks (
    url TEXT PRIMARY KEY,
    ok INTEGER NOT NULL,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refresh_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER DEFAULT 1,
    companies_count INTEGER DEFAULT 0,
    sources_count INTEGER DEFAULT 0,
    new_count INTEGER DEFAULT 0,
    changed_count INTEGER DEFAULT 0,
    notes TEXT DEFAULT ''
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # -- runs -----------------------------------------------------------
    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO refresh_runs (started_at) VALUES (?)", (_now_iso(),))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, *, ok: bool, companies: int, sources: int,
                    new_count: int, changed_count: int, notes: str = ""):
        self.conn.execute(
            "UPDATE refresh_runs SET finished_at=?, ok=?, companies_count=?, "
            "sources_count=?, new_count=?, changed_count=?, notes=? WHERE id=?",
            (_now_iso(), int(ok), companies, sources, new_count, changed_count, notes, run_id))
        self.conn.commit()

    def last_run(self) -> dict:
        row = self.conn.execute(
            "SELECT * FROM refresh_runs WHERE finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else {}

    def refresh_history(self, limit: int = 20) -> list:
        rows = self.conn.execute(
            "SELECT * FROM refresh_runs WHERE finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- link checks ------------------------------------------------------
    def fresh_link_checks(self, ttl_hours: float) -> dict[str, bool]:
        """Every cached result still inside the TTL, as {url: ok}. Read once
        per run rather than queried per-URL — a few thousand single-row
        lookups from worker threads costs more than one scan."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT url, ok FROM link_checks WHERE checked_at >= ?", (cutoff,)).fetchall()
        return {r["url"]: bool(r["ok"]) for r in rows}

    def record_link_checks(self, results: dict[str, bool]):
        if not results:
            return
        now = _now_iso()
        self.conn.executemany(
            "INSERT INTO link_checks (url, ok, checked_at) VALUES (?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET ok=excluded.ok, checked_at=excluded.checked_at",
            [(url, 1 if ok else 0, now) for url, ok in results.items()])
        self.conn.commit()

    # -- events -----------------------------------------------------------
    def upsert_event(self, event: dict, sources: list[dict]) -> str:
        """event: id, ticker, company_name, category, label, headline, summary,
        event_date, event_time, country, region, exchange, tier, primary_url,
        matched_on (list), content_hash.
        Returns status: new | date_moved | updated | unchanged.
        """
        now = _now_iso()
        row = self.conn.execute("SELECT * FROM events WHERE id=?", (event["id"],)).fetchone()

        if row is None:
            self.conn.execute(
                "INSERT INTO events (id, ticker, company_name, category, label, headline, "
                "summary, event_date, event_time, country, region, exchange, tier, primary_url, "
                "matched_on, status, prev_event_date, first_seen, last_seen, changed_at, content_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event["id"], event["ticker"], event["company_name"], event["category"],
                 event["label"], event["headline"], event.get("summary", ""),
                 event.get("event_date"), event.get("event_time"), event.get("country"),
                 event.get("region"), event.get("exchange"), event.get("tier"),
                 event.get("primary_url"), json.dumps(event.get("matched_on", [])),
                 "new", None, now, now, now, event["content_hash"]))
            self._replace_sources(event["id"], sources)
            self.conn.commit()
            return "new"

        status = "unchanged"
        prev_event_date = row["prev_event_date"]
        if row["event_date"] != event.get("event_date"):
            status = "date_moved"
            prev_event_date = row["event_date"]
            self._record_change(event["id"], "event_date", row["event_date"],
                                 event.get("event_date"), now)
        elif row["content_hash"] != event["content_hash"]:
            status = "updated"
            for field, old, new in (
                ("headline", row["headline"], event["headline"]),
                ("summary", row["summary"], event.get("summary", "")),
                ("category", row["category"], event["category"]),
                ("event_time", row["event_time"], event.get("event_time")),
            ):
                if old != new:
                    self._record_change(event["id"], field, old, new, now)

        self.conn.execute(
            "UPDATE events SET company_name=?, category=?, label=?, headline=?, summary=?, "
            "event_date=?, event_time=?, country=?, region=?, exchange=?, tier=?, primary_url=?, "
            "matched_on=?, status=?, prev_event_date=?, last_seen=?, changed_at=?, content_hash=? "
            "WHERE id=?",
            (event["company_name"], event["category"], event["label"], event["headline"],
             event.get("summary", ""), event.get("event_date"), event.get("event_time"),
             event.get("country"), event.get("region"), event.get("exchange"), event.get("tier"),
             event.get("primary_url"), json.dumps(event.get("matched_on", [])), status,
             prev_event_date, now, now if status != "unchanged" else row["changed_at"],
             event["content_hash"], event["id"]))
        self._replace_sources(event["id"], sources)
        self.conn.commit()
        return status

    def mark_unseen_as_stale(self, ticker: str, seen_ids: set[str]):
        """Sources can stop mentioning an item (it rolled off an RSS feed)
        without the underlying event having actually changed — so we don't
        delete anything here. Reserved for future pruning; currently a
        no-op that documents the decision explicitly."""
        return

    def _replace_sources(self, event_id: str, sources: list[dict]):
        self.conn.execute("DELETE FROM event_sources WHERE event_id=?", (event_id,))
        self.conn.executemany(
            "INSERT INTO event_sources (event_id, url, tier, source_type, publisher, title) "
            "VALUES (?,?,?,?,?,?)",
            [(event_id, s["url"], s.get("tier"), s.get("source_type"),
              s.get("publisher", ""), s.get("title", "")) for s in sources])

    def _record_change(self, event_id: str, field: str, old, new, when: str):
        self.conn.execute(
            "INSERT INTO changes (event_id, field, old_value, new_value, changed_at) "
            "VALUES (?,?,?,?,?)", (event_id, field, str(old) if old is not None else None,
                                    str(new) if new is not None else None, when))

    def get_events(self, history_from: str | None = None, tickers: list[str] | None = None) -> list[dict]:
        q = "SELECT * FROM events WHERE 1=1"
        args = []
        if history_from:
            q += " AND (event_date IS NULL OR event_date >= ?)"
            args.append(history_from)
        if tickers:
            q += f" AND ticker IN ({','.join('?' * len(tickers))})"
            args.extend(tickers)
        rows = [dict(r) for r in self.conn.execute(q, args).fetchall()]
        for r in rows:
            r["matched_on"] = json.loads(r["matched_on"] or "[]")
            src_rows = self.conn.execute(
                "SELECT url, tier, source_type, publisher, title FROM event_sources "
                "WHERE event_id=?", (r["id"],)).fetchall()
            r["sources"] = [dict(s) for s in src_rows]
            chg_rows = self.conn.execute(
                "SELECT field, old_value, new_value, changed_at FROM changes "
                "WHERE event_id=? ORDER BY id DESC", (r["id"],)).fetchall()
            r["change_history"] = [dict(c) for c in chg_rows]
        return rows

    def all_tickers(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT ticker FROM events").fetchall()
        return [r["ticker"] for r in rows]
