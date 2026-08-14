"""Builds the payload (events + meta) from the store, and renders it into a
single self-contained interactive HTML page. Used by both the live server
(server.py, rebuilt on every visit) and the static exporter (`tracker run`
/ `tracker render`, written once to out/dashboard.html).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from .store import Store

SGT = timezone(timedelta(hours=8))


def _to_sgt(iso_value: str | None) -> str | None:
    if not iso_value:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(SGT).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def build_payload(cfg: dict, roster: dict) -> tuple[list, dict]:
    run_cfg = cfg.get("run", {}) or {}
    history_from = run_cfg.get("history_from")
    lookahead_days = int(run_cfg.get("lookahead_days", 400))
    cutoff = (datetime.now(timezone.utc) + timedelta(days=lookahead_days)).date().isoformat()

    store = Store(cfg["output"]["db_path"])
    raw_events = store.get_events(history_from=history_from)
    last_run = store.last_run()
    store.close()

    # Country and sub-sector are company-level classification, not a fact
    # about any one scrape — joined from the roster at render time so that
    # relabelling a company (say, correcting its sub-sector) updates every
    # one of its past events immediately, without needing a rescrape. A
    # removed company's old events keep whatever was last stored.
    roster_by_ticker = {c["ticker"]: c for c in roster.get("companies", [])}

    events = []
    for e in raw_events:
        if e.get("event_date") and e["event_date"] > cutoff:
            continue
        company = roster_by_ticker.get(e["ticker"], {})
        events.append({
            "id": e["id"], "ticker": e["ticker"], "company": e["company_name"],
            "category": e["category"], "label": e["label"], "headline": e["headline"],
            "summary": e.get("summary", ""), "event_date": e.get("event_date"),
            "event_time": e.get("event_time"),
            "country": company.get("country") or e.get("country") or "Other",
            "sub_sector": company.get("sub_sector") or "Unclassified",
            "region": e.get("region"), "exchange": e.get("exchange"), "tier": e.get("tier"),
            "primary_url": e.get("primary_url"), "sources": e.get("sources", []),
            "matched_on": e.get("matched_on", []), "status": e.get("status"),
            "prev_event_date": e.get("prev_event_date"),
            "first_seen_sgt": _to_sgt(e.get("first_seen")),
            "last_seen_sgt": _to_sgt(e.get("last_seen")),
            "change_history": [
                {**c, "changed_at_sgt": _to_sgt(c.get("changed_at"))}
                for c in e.get("change_history", [])
            ],
        })

    companies = [
        {"ticker": c["ticker"], "name": c["name"], "country": c.get("country") or "Other",
         "sub_sector": c.get("sub_sector") or "Unclassified",
         "region": c.get("region"), "enabled": c.get("enabled", True)}
        for c in roster.get("companies", [])
    ]
    categories = []
    for key, cat in (cfg.get("classifier", {}).get("categories", {}) or {}).items():
        if cat.get("enabled", True):
            categories.append({"key": key, "label": cat.get("label", key)})

    meta = {
        "categories": categories,
        "companies": companies,
        "history_from": history_from,
        "lookahead_days": lookahead_days,
        "change_window_days": int(cfg.get("output", {}).get("change_window_days", 14)),
        "last_run_sgt": _to_sgt(last_run.get("finished_at")),
        "generated_at_sgt": datetime.now(SGT).strftime("%Y-%m-%d %H:%M"),
        # Lets the published page point its "add / remove companies" controls
        # at this repo's issue forms, which is how roster changes get made
        # when there's no server behind the page. GitHub Actions sets
        # GITHUB_REPOSITORY; empty elsewhere, and the panel adapts.
        "repo_slug": os.environ.get("GITHUB_REPOSITORY", ""),
    }
    return events, meta


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

_CSS = """
:root{
  --bg:#0b0f14; --panel:#121821; --panel2:#0f141b; --border:#232d3a; --text:#e6edf3;
  --muted:#8b9bb0; --accent:#4f8cff; --red:#e5534b; --green:#3fb950; --amber:#d29922;
  --rail-regulatory:#3fb950; --rail-company_ir:#4f8cff; --rail-newswire:#c99a3e; --rail-news:#8b9bb0;
  --radius:10px;
}
@media (prefers-color-scheme: light){
  :root{ --bg:#f5f7fa; --panel:#ffffff; --panel2:#f0f2f5; --border:#dde3ea; --text:#131a22; --muted:#5b6b7c; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
button{font:inherit;cursor:pointer}
.wrap{max-width:1200px;margin:0 auto;padding:0 20px 60px}
.topbar{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--border);padding:14px 20px}
.topbar-row{max-width:1200px;margin:0 auto;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.title{font-size:19px;font-weight:700;margin:0}
.subtitle{color:var(--muted);font-size:12.5px;margin-top:2px}
.spacer{flex:1}
.btn{background:var(--panel);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 14px;font-weight:600}
.btn:hover{border-color:var(--accent)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.small{padding:5px 10px;font-size:12.5px}
.status-line{font-size:12.5px;color:var(--muted)}
.status-line.error{color:var(--red)}
.status-line.running{color:var(--accent)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);margin-right:5px}
.dot.running{background:var(--accent);animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

.filters{background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;margin:18px 0}
.filter-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.filter-row:last-child{margin-bottom:0}
.filter-label{color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;min-width:60px}
.chip{border:1px solid var(--border);background:var(--panel2);border-radius:999px;padding:5px 12px;font-size:12.5px;color:var(--muted)}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
input[type=text],input[type=date],select{background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:6px 10px;font:inherit}
.checkline{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted)}

.strip-title{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:22px 0 8px}
.strip{display:flex;gap:10px;overflow-x:auto;padding-bottom:4px}
.strip-card{flex:0 0 auto;min-width:160px;background:var(--panel);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px}
.strip-card .cd{font-size:11px;font-weight:700;color:var(--accent)}
.strip-card .cd.soon{color:var(--red)}
.strip-card .ct{font-size:12.5px;font-weight:600;margin-top:3px}
.strip-empty{color:var(--muted);font-size:13px}

.section-title{font-size:15px;font-weight:700;margin:26px 0 10px;display:flex;align-items:center;gap:8px}
.section-count{color:var(--muted);font-weight:400;font-size:12.5px}
.country-group{margin-bottom:18px}
.country-name{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:14px 0 8px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px}
.card{position:relative;background:var(--panel);border:1px solid var(--border);border-left:4px solid var(--rail-news);border-radius:var(--radius);padding:11px 13px 10px;cursor:pointer;transition:border-color .1s}
.card:hover{border-color:var(--accent)}
.card.rail-regulatory{border-left-color:var(--rail-regulatory)}
.card.rail-company_ir{border-left-color:var(--rail-company_ir)}
.card.rail-newswire{border-left-color:var(--rail-newswire)}
.card.rail-news{border-left-color:var(--rail-news)}
.card.date_moved{border-left-style:dashed}
.card-top{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
.card-cat{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
.card-newdot{width:7px;height:7px;border-radius:50%;background:var(--accent);flex:0 0 auto;margin-top:3px}
.card-headline{font-size:13.5px;font-weight:600;margin:5px 0 6px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card-meta{display:flex;justify-content:space-between;align-items:flex-end;gap:6px}
.card-co{font-size:12px;color:var(--muted)}
.card-date{font-size:11.5px;text-align:right}
.card-date .d{font-weight:700}
.card-date .t{color:var(--muted);display:block;font-size:10.5px}
.card-delta{font-size:10px;color:var(--amber);margin-top:2px}
.card-src{font-size:10.5px;color:var(--muted);margin-top:5px}
.empty{color:var(--muted);padding:30px 0;text-align:center}

.overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:50;display:none}
.overlay.open{display:block}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(480px,100%);background:var(--panel);border-left:1px solid var(--border);z-index:51;
  transform:translateX(100%);transition:transform .18s ease;overflow-y:auto;padding:22px}
.drawer.open{transform:translateX(0)}
.drawer h2{font-size:17px;margin:0 0 4px}
.drawer-close{position:absolute;top:16px;right:16px;background:none;border:none;color:var(--muted);font-size:20px}
.kv{display:grid;grid-template-columns:90px 1fr;gap:6px 10px;font-size:13px;margin:14px 0}
.kv b{color:var(--muted);font-weight:600}
.tag{display:inline-block;background:var(--panel2);border:1px solid var(--border);border-radius:5px;padding:2px 7px;font-size:11px;margin:2px 4px 0 0}
.srclist{list-style:none;padding:0;margin:8px 0}
.srclist li{border:1px solid var(--border);border-radius:8px;padding:9px 11px;margin-bottom:7px}
.srclist .tier{font-size:10px;text-transform:uppercase;font-weight:700;color:var(--muted)}
.chglist{list-style:none;padding:0;margin:8px 0;font-size:12.5px}
.chglist li{border-bottom:1px solid var(--border);padding:6px 0}

.modal-panel{position:fixed;top:0;right:0;bottom:0;width:min(560px,100%);background:var(--panel);border-left:1px solid var(--border);
  z-index:51;transform:translateX(100%);transition:transform .18s ease;overflow-y:auto;padding:22px}
.modal-panel.open{transform:translateX(0)}
.co-row{display:flex;align-items:center;gap:10px;border:1px solid var(--border);border-radius:8px;padding:9px 11px;margin-bottom:7px}
.co-row .n{font-weight:600;font-size:13px}
.co-row .s{color:var(--muted);font-size:11.5px}
.co-row.paused{opacity:.5}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:6px}
.form-grid input,.form-grid select{width:100%}
.form-grid .full{grid-column:1/-1}
.msg{font-size:12.5px;margin-top:8px}
.msg.ok{color:var(--green)}
.msg.err{color:var(--red)}
.static-note{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:10px 14px;font-size:12.5px;color:var(--muted);margin-bottom:16px}
.hist-table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:10px}
.hist-table th,.hist-table td{border-bottom:1px solid var(--border);padding:6px 8px;text-align:left}

.summary{display:grid;grid-template-columns:repeat(6,1fr);border:1px solid var(--border);border-radius:var(--radius);
  background:var(--panel);overflow:hidden;margin:18px 0}
.summary-box{padding:16px 14px;text-align:center;border-right:1px solid var(--border)}
.summary-box:last-child{border-right:none}
.summary-num{font-size:24px;font-weight:800;line-height:1}
.summary-label{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-top:6px}
.summary-num.c-new{color:var(--accent)}
.summary-num.c-moved{color:var(--red)}
.summary-num.c-revised{color:var(--amber)}
@media (max-width:760px){.summary{grid-template-columns:repeat(3,1fr)}.summary-box:nth-child(3){border-right:none}}
.co-search{width:100%;margin-bottom:10px}
.co-list-empty{color:var(--muted);font-size:12.5px;padding:10px 0}
"""


def page_html(events: list, meta: dict, live: bool = True) -> str:
    events_json = json.dumps(events, ensure_ascii=False).replace("</script", "<\\/script")
    meta_json = json.dumps(meta, ensure_ascii=False).replace("</script", "<\\/script")
    history_from = meta.get("history_from") or "2020-01-01"
    n_companies = len({c["ticker"] for c in meta.get("companies", [])})
    repo = meta.get("repo_slug") or ""
    # On the published (static) page there is no server behind the page, so
    # the two buttons that need one are not rendered at all rather than
    # rendered-and-then-apologising: "Refresh now" can't trigger anything
    # (the schedule does that on its own), and History needs a live query.
    # The refresh time those controls were there to explain is shown
    # directly instead. Managing companies still works without a server —
    # it goes through the repo (see the panel), not a local process.
    if live:
        header_controls = (
            '<a class="btn small" id="historyBtn" href="#">History</a>'
            '<button class="btn small" id="companiesBtn">+ Add / remove companies</button>'
            '<button class="btn primary small" id="refreshBtn">Refresh now</button>'
        )
    else:
        header_controls = (
            '<button class="btn small" id="companiesBtn">+ Add / remove companies</button>'
        )
    history_panel = ("""<div class="modal-panel" id="historyPanel">
  <button class="drawer-close" id="historyClose">&times;</button>
  <h2>Refresh history</h2>
  <table class="hist-table"><thead><tr><th>Finished (SGT)</th><th>New</th><th>Changed</th><th>Companies</th><th>Notes</th></tr></thead>
  <tbody id="historyBody"></tbody></table>
</div>""" if live else "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Corporate Event Monitor</title>
<style>{_CSS}</style></head>
<body>
<div class="topbar"><div class="topbar-row">
  <div><p class="title">Corporate Event Monitor</p>
    <p class="subtitle">Tracking {n_companies} companies · official sources only</p></div>
  <div class="spacer"></div>
  <div class="status-line" id="statusLine"><span class="dot" id="statusDot"></span><span id="statusText">Loading…</span></div>
  {header_controls}
</div></div>

<div class="wrap">
  <div class="summary" id="summaryRow"></div>

  <div class="filters">
    <div class="filter-row" id="catRow"><span class="filter-label">Events Category</span></div>
    <div class="filter-row">
      <span class="filter-label">Search</span>
      <input type="text" id="searchBox" placeholder="Ticker or company name…">
      <span class="filter-label" style="min-width:auto">Country</span>
      <select id="countrySel"><option value="">All</option></select>
      <span class="filter-label" style="min-width:auto">Sub-sector</span>
      <select id="subSectorSel"><option value="">All</option></select>
    </div>
    <div class="filter-row">
      <span class="filter-label">When</span>
      <button class="chip" data-preset="7">Next 7 days</button>
      <button class="chip" data-preset="30">Next 30 days</button>
      <button class="chip" data-preset="quarter">This quarter</button>
      <button class="chip" data-preset="all">All</button>
      <input type="date" id="fromDate"> <span style="color:var(--muted)">to</span> <input type="date" id="toDate">
    </div>
    <div class="filter-row">
      <span class="filter-label">Show</span>
      <label class="checkline"><input type="checkbox" id="changedOnly"> Changed only</label>
      <label class="checkline"><input type="checkbox" id="newOnly"> New only</label>
      <label class="checkline"><input type="checkbox" id="showPast" checked> Show past events</label>
      <span class="spacer"></span>
      <button class="btn small" id="clearBtn">Clear filters</button>
    </div>
  </div>

  <div class="strip-title">Next 7 days</div>
  <div class="strip" id="strip"></div>

  <div id="upcomingSection"></div>
  <div id="pastSection"></div>
</div>

<div class="overlay" id="overlay"></div>
<div class="drawer" id="drawer">
  <button class="drawer-close" id="drawerClose">&times;</button>
  <div id="drawerBody"></div>
</div>
<div class="modal-panel" id="companiesPanel">
  <button class="drawer-close" id="companiesClose">&times;</button>
  <h2>Companies</h2>
  <p id="coRepoNote" style="color:var(--muted);font-size:12px;margin-top:-4px;display:none">
    Changes here open a short form on GitHub. Once you submit it, the roster updates
    automatically and the change shows up on this page after the next hourly refresh.</p>
  <input type="text" id="coSearch" class="co-search" placeholder="Filter this list by ticker or name…">
  <div id="companiesList"></div>
  <h2 style="margin-top:22px">Add a company</h2>
  <p style="color:var(--muted);font-size:12px;margin-top:-4px">
    It's tracked permanently — it stays on this list, in every filter, until you remove it,
    even after closing or refreshing the page. History back to {history_from} is pulled
    automatically right after you add it.</p>
  <div class="form-grid">
    <input type="text" id="newTicker" placeholder="Ticker (e.g. 2330.TW)">
    <input type="text" id="newName" placeholder="Company name">
    <input type="text" id="newIr" class="full" placeholder="Investor relations URL (optional)">
    <input type="text" id="newExchange" placeholder="Exchange">
    <input type="text" id="newCountry" placeholder="Country (e.g. US, Japan, Europe…)">
    <input type="text" id="newSubSector" placeholder="Sub-sector (e.g. Consumer Staples)">
    <input type="text" id="newCik" placeholder="SEC CIK (optional — auto-resolved if US and left blank)">
    <input type="text" id="newAliases" placeholder="Aliases, comma separated">
  </div>
  <button class="btn primary small" id="addCoBtn" style="margin-top:10px">Add company</button>
  <div class="msg" id="addCoMsg"></div>
</div>
{history_panel}

<script>
const LIVE = {str(live).lower()};
const EVENTS = {events_json};
const META = {meta_json};
{_JS}
</script>
</body></html>"""


_JS = r"""
const TIER_LABEL = {regulatory:'Regulatory', company_ir:'Company IR', newswire:'Newswire', news:'News'};
let state = { categories: new Set(META.categories.map(c=>c.key)), search:'', country:'', subSector:'',
              from:'', to:'', changedOnly:false, newOnly:false, showPast:true };
let liveOk = false;

function fmtDate(d){ if(!d) return null; return d; }
function daysUntil(d){
  const today = new Date(); today.setHours(0,0,0,0);
  const target = new Date(d + 'T00:00:00');
  return Math.round((target - today) / 86400000);
}
function countdownLabel(n){
  if(n===0) return 'TODAY'; if(n===1) return 'TOMORROW';
  if(n<0) return (-n)+' DAYS AGO'; return 'IN '+n+' DAYS';
}

function buildCatRow(){
  const row = document.getElementById('catRow');
  META.categories.forEach(c=>{
    const b = document.createElement('button');
    b.className = 'chip on'; b.textContent = c.label; b.dataset.cat = c.key;
    b.onclick = ()=>{ state.categories.has(c.key) ? state.categories.delete(c.key) : state.categories.add(c.key);
                       b.classList.toggle('on'); render(); };
    row.appendChild(b);
  });
  const countrySel = document.getElementById('countrySel');
  const countries = [...new Set(META.companies.map(c=>c.country).filter(Boolean))].sort();
  countries.forEach(c=>{ const o=document.createElement('option'); o.value=c; o.textContent=c; countrySel.appendChild(o); });
  const subSel = document.getElementById('subSectorSel');
  const subSectors = [...new Set(META.companies.map(c=>c.sub_sector).filter(Boolean))].sort();
  subSectors.forEach(s=>{ const o=document.createElement('option'); o.value=s; o.textContent=s; subSel.appendChild(o); });
}

function applyFilters(list){
  return list.filter(e=>{
    if(!state.categories.has(e.category)) return false;
    if(state.country && e.country !== state.country) return false;
    if(state.subSector && e.sub_sector !== state.subSector) return false;
    if(state.changedOnly && !['date_moved','updated'].includes(e.status)) return false;
    if(state.newOnly && e.status !== 'new') return false;
    if(state.search){
      const q = state.search.toLowerCase();
      if(!(e.ticker.toLowerCase().includes(q) || e.company.toLowerCase().includes(q) || e.headline.toLowerCase().includes(q))) return false;
    }
    if(state.from && e.event_date && e.event_date < state.from) return false;
    if(state.to && e.event_date && e.event_date > state.to) return false;
    if((state.from || state.to) && !e.event_date) return false;
    return true;
  });
}

function computeSummary(){
  const today = todayStr();
  const in7 = new Date(Date.now()+7*86400000).toISOString().slice(0,10);
  const in31 = new Date(Date.now()+31*86400000).toISOString().slice(0,10);
  let counts = { new:0, date_moved:0, updated:0, up7:0, up31:0 };
  EVENTS.forEach(e=>{
    if(e.status==='new') counts.new++;
    if(e.status==='date_moved') counts.date_moved++;
    if(e.status==='updated') counts.updated++;
    if(e.event_date && e.event_date>=today && e.event_date<=in7) counts.up7++;
    if(e.event_date && e.event_date>=today && e.event_date<=in31) counts.up31++;
  });
  return counts;
}

function renderSummary(){
  const c = computeSummary();
  const tracked = META.companies.length;
  const boxes = [
    ['c-new', c.new, 'New'],
    ['c-moved', c.date_moved, 'Date Moved'],
    ['c-revised', c.updated, 'Revised'],
    ['', c.up7, 'Upcoming (7d)'],
    ['', c.up31, 'Upcoming (31d)'],
    ['', tracked, 'Tracked'],
  ];
  document.getElementById('summaryRow').innerHTML = boxes.map(([cls,num,label])=>
    `<div class="summary-box"><div class="summary-num ${cls}">${num}</div><div class="summary-label">${label}</div></div>`).join('');
}

function railClass(e){ return 'rail-' + (e.tier || 'news'); }

function cardHtml(e){
  const dateBit = e.event_date
    ? `<div class="d">${e.event_date}</div><span class="t">${e.event_time || (e.event_date < todayStr() ? 'NA' : 'TIME TBC')}</span>`
    : `<div class="d">—</div><span class="t">NA</span>`;
  const delta = e.status==='date_moved' && e.prev_event_date ? `<div class="card-delta">was ${e.prev_event_date}</div>` : '';
  const srcCount = e.sources.length>1 ? `<div class="card-src">+${e.sources.length-1} source${e.sources.length>2?'s':''}</div>` : '';
  const newDot = e.status==='new' ? '<span class="card-newdot"></span>' : '';
  return `<div class="card ${railClass(e)} ${e.status==='date_moved'?'date_moved':''}" data-id="${e.id}">
    <div class="card-top"><span class="card-cat">${e.label}</span>${newDot}</div>
    <div class="card-headline">${escapeHtml(e.headline)}</div>
    <div class="card-meta"><span class="card-co">${e.ticker} · ${e.country}</span>
      <span class="card-date">${dateBit}${delta}</span></div>
    ${srcCount}
  </div>`;
}

function escapeHtml(s){ const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
function todayStr(){ return new Date().toISOString().slice(0,10); }

function groupByCountry(list){
  const by = {};
  list.forEach(e=>{ (by[e.country] ||= []).push(e); });
  const order = Object.keys(by).sort((a,b)=>{
    const ea = by[a].find(e=>e.event_date)?.event_date || '9999';
    const eb = by[b].find(e=>e.event_date)?.event_date || '9999';
    return ea.localeCompare(eb);
  });
  return order.map(k=>[k, by[k]]);
}

function render(){
  renderSummary();
  const today = todayStr();
  const filtered = applyFilters(EVENTS);

  const dated = filtered.filter(e=>e.event_date);
  const strip = dated.filter(e=>{ const n = daysUntil(e.event_date); return n>=0 && n<=7; })
                      .sort((a,b)=>a.event_date.localeCompare(b.event_date));
  const stripEl = document.getElementById('strip');
  stripEl.innerHTML = strip.length ? strip.map(e=>{
    const n = daysUntil(e.event_date);
    return `<div class="strip-card" data-id="${e.id}"><div class="cd ${n<=2?'soon':''}">${countdownLabel(n)}</div>
      <div class="ct">${escapeHtml(e.headline)}</div>
      <div style="color:var(--muted);font-size:11px;margin-top:3px">${e.ticker} · ${e.label}</div></div>`;
  }).join('') : (()=>{ const next = dated.filter(e=>e.event_date>=today).sort((a,b)=>a.event_date.localeCompare(b.event_date))[0];
      return `<div class="strip-empty">Nothing in the next 7 days.${next?` Next up: ${escapeHtml(next.headline)} on ${next.event_date}.`:''}</div>`; })();

  let upcoming = filtered.filter(e=>!e.event_date || e.event_date >= today)
                          .sort((a,b)=>(a.event_date||'9999').localeCompare(b.event_date||'9999'));
  let past = state.showPast ? filtered.filter(e=>e.event_date && e.event_date < today)
                          .sort((a,b)=>b.event_date.localeCompare(a.event_date)) : [];

  const upEl = document.getElementById('upcomingSection');
  upEl.innerHTML = `<div class="section-title">Upcoming <span class="section-count">(${upcoming.length})</span></div>` +
    (upcoming.length ? groupByCountry(upcoming).map(([country, list])=>
      `<div class="country-group"><div class="country-name">${escapeHtml(country)}</div>
       <div class="cards">${list.map(cardHtml).join('')}</div></div>`).join('')
     : '<div class="empty">No upcoming events match the current filters.</div>');

  const pastEl = document.getElementById('pastSection');
  pastEl.innerHTML = state.showPast ? (`<div class="section-title">Past <span class="section-count">(${past.length})</span></div>` +
    (past.length ? groupByCountry(past).map(([country, list])=>
      `<div class="country-group"><div class="country-name">${escapeHtml(country)}</div>
       <div class="cards">${list.map(cardHtml).join('')}</div></div>`).join('')
     : '<div class="empty">No past events match the current filters.</div>')) : '';

  document.querySelectorAll('[data-id]').forEach(el=>{
    el.onclick = ()=> openDrawer(EVENTS.find(e=>e.id===el.dataset.id));
  });
}

function openDrawer(e){
  const body = document.getElementById('drawerBody');
  const changes = e.change_history.length ? `<h3 style="font-size:13px">Change history</h3>
    <ul class="chglist">${e.change_history.map(c=>`<li><b>${c.field}</b>: ${escapeHtml(c.old_value||'—')} → ${escapeHtml(c.new_value||'—')}
      <div style="color:var(--muted)">${c.changed_at_sgt||''} SGT</div></li>`).join('')}</ul>` : '';
  const matched = e.matched_on.length ? e.matched_on.map(m=>`<span class="tag">${escapeHtml(m)}</span>`).join('') : '<span class="tag">—</span>';
  const sources = e.sources.slice().sort((a,b)=> tierRank(a.tier)-tierRank(b.tier)).map(s=>
    `<li><span class="tier">${TIER_LABEL[s.tier]||s.tier}</span><br>
     <a href="${s.url}" target="_blank" rel="noopener">${escapeHtml(s.title || s.publisher || s.url)}</a>
     <div style="color:var(--muted);font-size:11px">${escapeHtml(s.publisher||'')}</div></li>`).join('');
  body.innerHTML = `<h2>${escapeHtml(e.headline)}</h2>
    <div style="color:var(--muted)">${e.ticker} · ${escapeHtml(e.company)} · ${escapeHtml(e.label)}</div>
    <div class="kv">
      <b>Date</b><span>${e.event_date || 'Undated'}</span>
      <b>Time</b><span>${e.event_time || (e.event_date && e.event_date < todayStr() ? 'NA' : 'TIME TBC')}</span>
      <b>Country</b><span>${escapeHtml(e.country)}</span>
      <b>Sub-sector</b><span>${escapeHtml(e.sub_sector||'—')}</span>
      <b>Exchange</b><span>${escapeHtml(e.exchange||'—')}</span>
      <b>Status</b><span>${e.status}</span>
      <b>First seen</b><span>${e.first_seen_sgt||''} SGT</span>
    </div>
    ${e.summary ? `<p>${escapeHtml(e.summary)}</p>` : ''}
    <h3 style="font-size:13px">Matched on</h3><div>${matched}</div>
    <h3 style="font-size:13px">Sources (${e.sources.length})</h3><ul class="srclist">${sources}</ul>
    ${changes}`;
  document.getElementById('overlay').classList.add('open');
  document.getElementById('drawer').classList.add('open');
}
function tierRank(t){ return {regulatory:0, company_ir:1, newswire:2, news:3}[t] ?? 9; }

function closeDrawer(){ document.getElementById('overlay').classList.remove('open'); document.getElementById('drawer').classList.remove('open'); }
document.getElementById('drawerClose').onclick = closeDrawer;
document.getElementById('overlay').onclick = ()=>{ closeDrawer(); closePanel('companiesPanel'); closePanel('historyPanel'); };

// Null-safe: the history panel isn't rendered on the published page, and
// the overlay's click handler closes every panel without knowing which
// ones this build actually has.
function closePanel(id){ const el = document.getElementById(id); if(el){ el.classList.remove('open'); } document.getElementById('overlay').classList.remove('open'); }

document.getElementById('searchBox').oninput = e=>{ state.search = e.target.value; render(); };
document.getElementById('countrySel').onchange = e=>{ state.country = e.target.value; render(); };
document.getElementById('subSectorSel').onchange = e=>{ state.subSector = e.target.value; render(); };
document.getElementById('changedOnly').onchange = e=>{ state.changedOnly = e.target.checked; render(); };
document.getElementById('newOnly').onchange = e=>{ state.newOnly = e.target.checked; render(); };
document.getElementById('showPast').onchange = e=>{ state.showPast = e.target.checked; render(); };
document.getElementById('fromDate').onchange = e=>{ state.from = e.target.value; render(); };
document.getElementById('toDate').onchange = e=>{ state.to = e.target.value; render(); };
document.getElementById('clearBtn').onclick = ()=>{
  state.search=''; state.country=''; state.subSector=''; state.from=''; state.to=''; state.changedOnly=false; state.newOnly=false; state.showPast=true;
  document.getElementById('searchBox').value=''; document.getElementById('countrySel').value='';
  document.getElementById('subSectorSel').value='';
  document.getElementById('fromDate').value=''; document.getElementById('toDate').value='';
  document.getElementById('changedOnly').checked=false; document.getElementById('newOnly').checked=false;
  document.getElementById('showPast').checked=true;
  state.categories = new Set(META.categories.map(c=>c.key));
  document.querySelectorAll('#catRow .chip').forEach(b=>b.classList.add('on'));
  render();
};
document.querySelectorAll('[data-preset]').forEach(b=>{
  b.onclick = ()=>{
    const p = b.dataset.preset;
    const today = new Date();
    const fmt = d=>d.toISOString().slice(0,10);
    if(p==='7'){ state.from=fmt(today); state.to=fmt(new Date(Date.now()+7*86400000)); }
    else if(p==='30'){ state.from=fmt(today); state.to=fmt(new Date(Date.now()+30*86400000)); }
    else if(p==='quarter'){ const q=Math.floor(today.getMonth()/3); const start=new Date(today.getFullYear(), q*3, 1); const end=new Date(today.getFullYear(), q*3+3, 0);
      state.from=fmt(start); state.to=fmt(end); }
    else { state.from=''; state.to=''; }
    document.getElementById('fromDate').value = state.from;
    document.getElementById('toDate').value = state.to;
    render();
  };
});

// -- Companies panel --------------------------------------------------
// With no server behind the published page, roster changes go through the
// repo that builds it: each control opens a prefilled issue, and a workflow
// applies it to companies.yaml and rebuilds. Same panel, same list, no
// local Python and no editing a file by hand.
const REPO = (META.repo_slug || '').trim();
const CAN_REQUEST = !LIVE && !!REPO;

function issueUrl(template, fields){
  const q = new URLSearchParams({template: template});
  for(const k in fields){ q.set(k, fields[k]); }
  return 'https://github.com/' + REPO + '/issues/new?' + q.toString();
}

document.getElementById('companiesBtn').onclick = ()=>{
  if(!LIVE && !CAN_REQUEST){ alert('This copy of the page has no repository linked, so the roster can only be changed from the tracker it was built from.'); return; }
  document.getElementById('overlay').classList.add('open');
  document.getElementById('companiesPanel').classList.add('open');
  if(CAN_REQUEST){ document.getElementById('coRepoNote').style.display = 'block'; }
  loadCompanies();
};
document.getElementById('companiesClose').onclick = ()=> closePanel('companiesPanel');

let companiesCache = [];
const CO_LIST_CAP = 300;   // roster can run into the thousands; render a capped, search-filtered slice

function loadCompanies(){
  const list = document.getElementById('companiesList');
  if(!LIVE){
    // The roster is already embedded in this page — no server to ask.
    companiesCache = (META.companies || []).slice();
    renderCompaniesList();
    return;
  }
  list.innerHTML = '<div class="co-list-empty">Loading…</div>';
  fetch('/api/companies').then(r=>r.json()).then(data=>{
    companiesCache = data.companies || [];
    renderCompaniesList();
  });
}

function renderCompaniesList(){
  const list = document.getElementById('companiesList');
  const q = (document.getElementById('coSearch').value || '').trim().toLowerCase();
  let matches = q
    ? companiesCache.filter(c => c.ticker.toLowerCase().includes(q) || c.name.toLowerCase().includes(q))
    : companiesCache;
  const total = matches.length;
  const shown = matches.slice(0, CO_LIST_CAP);
  const note = total > CO_LIST_CAP
    ? `<div class="co-list-empty">Showing ${CO_LIST_CAP} of ${total} matches — narrow your search to see more.</div>` : '';
  if(!companiesCache.length){
    list.innerHTML = '<div class="co-list-empty">No companies tracked yet.</div>';
    return;
  }
  list.innerHTML = `<div class="co-list-empty">${companiesCache.length} companies tracked${q ? `, ${total} match "${escapeHtml(q)}"` : ''}</div>` +
    shown.map(c=>`
      <div class="co-row ${c.enabled? '' : 'paused'}">
        <div style="flex:1"><div class="n">${escapeHtml(c.ticker)} — ${escapeHtml(c.name)}</div>
        <div class="s">${escapeHtml(c.country||'')}${c.sub_sector? ' · '+escapeHtml(c.sub_sector):''} · ${c.source_count||0} sources · ${c.enabled? 'active':'paused'}</div></div>
        <button class="btn small" data-act="toggle" data-ticker="${escapeHtml(c.ticker)}" data-enabled="${!c.enabled}">${c.enabled?'Pause':'Resume'}</button>
        <button class="btn small" data-act="remove" data-ticker="${escapeHtml(c.ticker)}">Remove</button>
      </div>`).join('') + note;
  list.querySelectorAll('[data-act]').forEach(btn=>{
    btn.onclick = ()=>{
      const act = btn.dataset.act, ticker = btn.dataset.ticker;
      if(!LIVE){
        window.open(issueUrl('remove-company.yml', {
          title: '[roster] ' + (act==='remove' ? 'remove ' : 'pause/resume ') + ticker,
          ticker: ticker, action: act==='remove' ? 'Remove' : 'Pause or resume'
        }), '_blank');
        return;
      }
      const body = act==='toggle' ? {ticker, enabled: btn.dataset.enabled==='true'} : {ticker};
      fetch('/api/companies/'+act, {method:'POST', body: JSON.stringify(body)})
        .then(r=>r.json()).then(()=> loadCompanies());
    };
  });
}
document.getElementById('coSearch').oninput = renderCompaniesList;

document.getElementById('addCoBtn').onclick = ()=>{
  const msg = document.getElementById('addCoMsg'); msg.textContent = 'Adding…'; msg.className='msg';
  const body = {
    ticker: document.getElementById('newTicker').value.trim(),
    name: document.getElementById('newName').value.trim(),
    ir: document.getElementById('newIr').value.trim(),
    exchange: document.getElementById('newExchange').value.trim(),
    country: document.getElementById('newCountry').value.trim(),
    sub_sector: document.getElementById('newSubSector').value.trim(),
    cik: document.getElementById('newCik').value.trim(),
    aliases: document.getElementById('newAliases').value.trim(),
  };
  if(!body.ticker || !body.name){ msg.textContent='Ticker and name are required.'; msg.className='msg err'; return; }
  if(!LIVE){
    window.open(issueUrl('add-company.yml', {
      title: '[roster] add ' + body.ticker,
      ticker: body.ticker, name: body.name, ir: body.ir, exchange: body.exchange,
      country: body.country, sub_sector: body.sub_sector, cik: body.cik, aliases: body.aliases
    }), '_blank');
    msg.textContent = 'Opening a request on GitHub — submit it there and the next hourly build picks it up.';
    msg.className = 'msg ok';
    return;
  }
  fetch('/api/companies/add', {method:'POST', body: JSON.stringify(body)}).then(r=>r.json()).then(res=>{
    msg.textContent = res.message || (res.ok? 'Added.' : 'Could not add company.');
    msg.className = 'msg ' + (res.ok? 'ok':'err');
    if(res.ok){ ['newTicker','newName','newIr','newExchange','newCountry','newSubSector','newCik','newAliases'].forEach(id=>document.getElementById(id).value='');
      loadCompanies(); }
  });
};

// -- History panel ------------------------------------------------------
// Only rendered on the live server; the published page shows its refresh
// time in the status line instead. Guarded rather than assumed present, so
// a missing control can't throw here and take the whole script (filters,
// rendering, everything below) down with it.
const historyBtn = document.getElementById('historyBtn');
if(historyBtn){
  historyBtn.onclick = (ev)=>{
    ev.preventDefault();
    if(!LIVE || !liveOk){ alert('History needs the live server running.'); return; }
    fetch('/api/status').then(r=>r.json()).then(s=>{
      document.getElementById('historyBody').innerHTML = (s.history||[]).map(h=>
        `<tr><td>${h.finished_at||''}</td><td>${h.new_count}</td><td>${h.changed_count}</td><td>${h.companies_count}</td><td>${escapeHtml(h.notes||'')}</td></tr>`).join('');
      document.getElementById('overlay').classList.add('open');
      document.getElementById('historyPanel').classList.add('open');
    });
  };
  document.getElementById('historyClose').onclick = ()=> closePanel('historyPanel');
}

// -- Refresh + status polling --------------------------------------------
function setStatus(text, cls){
  document.getElementById('statusText').textContent = text;
  document.getElementById('statusLine').className = 'status-line ' + (cls||'');
  document.getElementById('statusDot').className = 'dot ' + (cls==='running' ? 'running':'');
}

const refreshBtn = document.getElementById('refreshBtn');
if(refreshBtn){
  refreshBtn.onclick = ()=>{
    if(!LIVE || !liveOk){ alert('Run "python -m tracker serve" and open the live page to refresh.'); return; }
    fetch('/api/refresh', {method:'POST'}).then(r=>r.json()).then(pollLoop);
  };
}

let lastVersion = null;
function pollLoop(){
  fetch('/api/status').then(r=>r.json()).then(s=>{
    if(s.running){ setStatus((s.message||'Refreshing…'), 'running'); setTimeout(pollLoop, 1200); return; }
    if(s.error){ setStatus('Error: ' + s.error, 'error'); }
    else {
      const next = s.next_background_run ? ` · next automatic refresh ${s.next_background_run} SGT` : '';
      setStatus(`Last refreshed ${s.last_run||'—'} SGT${next}`, '');
    }
    if(lastVersion !== null && s.data_version !== lastVersion){
      fetch('/api/data').then(r=>r.json()).then(d=>{
        EVENTS.length=0; EVENTS.push(...d.events);
        Object.assign(META, d.meta);
        render();
      });
    }
    lastVersion = s.data_version;
  }).catch(()=>{});
}

function init(){
  buildCatRow();
  render();
  if(!LIVE){
    var t = (META.last_run_sgt || META.generated_at_sgt || '').trim();
    setStatus(t ? ('Last refreshed ' + t + ' SGT · updates hourly') : 'Updates hourly', '');
    return;
  }
  fetch('/api/ping').then(r=>r.ok ? r.json() : Promise.reject())
    .then(()=>{ liveOk = true; pollLoop(); setInterval(pollLoop, 5000); })
    .catch(()=>{ liveOk = false; setStatus('No live server detected — this looks like a saved copy of the page.', 'error'); });
}
init();
"""
