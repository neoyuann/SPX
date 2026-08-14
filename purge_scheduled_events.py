"""Remove events produced by the filing scheduled-date scan so they can be
re-derived by the current extraction logic.

Why this exists: the scan that reads a filing's own text for "will host a
conference call on <date>" originally matched any future date sitting near a
scheduling keyword. That let a replay-availability date stand in for the
event itself — Simon Property's Q2 release announced a call that had already
happened, then said the replay would "be available until August 17, 2026",
and August 17 is what reached the page. Every event from that scan is
therefore suspect: the ones that look right can't be distinguished from the
ones that aren't without re-reading each filing.

Deleting them is not a hole in the "past events are never removed"
guarantee. That guarantee is about not losing real events when a source
stops mentioning them. These are records of events that were never
announced — better absent, and re-derived correctly on the next runs, than
left on the page carrying a date nobody disclosed.

Usage:  python purge_scheduled_events.py [--db data/events.db] [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sqlite3

# Exactly the headline shape _fetch_scheduled_event builds:
#   f"{company['name']}: {keyword.title()} scheduled"
SCHEDULED_HEADLINE = "%: % scheduled"


def _is_implausible(event_time: str | None) -> bool:
    """Mirrors sources._IMPLAUSIBLE_EVENT_HOURS: no meeting or earnings call
    starts between 10pm and 5am, so a stored time in that window marks a row
    that came from a proxy voting deadline rather than the meeting."""
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)?", event_time or "", re.IGNORECASE)
    if not m:
        return False
    hour, meridiem = int(m.group(1)), (m.group(3) or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return hour >= 22 or hour < 5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/events.db")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--implausible-times-only", action="store_true",
                    help="Only remove rows whose stored time is one no real "
                         "event starts at — the voting-deadline signature. "
                         "Use when the extraction is already correct and only "
                         "rows predating the fix need clearing, so the "
                         "correct ones aren't needlessly re-derived.")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, ticker, event_date, event_time, headline FROM events WHERE headline LIKE ?",
        (SCHEDULED_HEADLINE,)).fetchall()
    if args.implausible_times_only:
        rows = [r for r in rows if _is_implausible(r["event_time"])]

    if not rows:
        print("No scheduled-scan events found — nothing to purge.")
        return 0

    print(f"{len(rows)} event(s) from the scheduled-date scan:")
    for r in rows[:10]:
        print(f"  {r['ticker']:10s} {r['event_date']}  {r['event_time'] or '-':22s} {r['headline']}")
    if len(rows) > 10:
        print(f"  … and {len(rows) - 10} more")

    if args.dry_run:
        print("\n--dry-run: nothing deleted.")
        return 0

    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    # event_sources and changes reference events by id; clear them too rather
    # than leaving rows pointing at events that no longer exist.
    for table in ("event_sources", "changes"):
        conn.execute(f"DELETE FROM {table} WHERE event_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM events WHERE id IN ({marks})", ids)
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print(f"\nPurged {len(ids)} event(s). The next refresh re-derives them "
          "from the same filings using the corrected extraction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
