"""activity_build — the /activity session-activity log (ARCH-191 / ARCH-239 item-2).

A windowed table of every agentic session — one row per top-level dispatch (an overnight
orchestrator, a full-monty drain, an interactive co's agentctl spawn, or a co session's
captured Task-tool subagents) — that expands into its full dispatch tree: every spawned
worker with its agent, model, duration, cost, turns, and outcome. Cost is summed down the
whole parent->child chain by the cost_join engine in agent-platform.

Data source: the agent-activity-report producer writes ~/Convivy/agent-runs/activity/
sessions.json every 15 min (cost_join over the run-dir corpus). This page reads that
snapshot at request time (ARCH-182), embeds it, and filters client-side by window —
matching the date-picker UX of the other dashboards (ARCH-191). The snapshot is at most
~15 min stale; live in-flight cost is a later pass.

Known gap surfaced inline: an interactive co session's OWN main-loop cost is not yet in
the corpus (it lives in ~/.claude/projects, not a run-dir), so those rows show captured
subagents only and carry a "workers only" marker (`main_loop_captured: false`). That marker
was previously a tooltip only; the page now also shows a standing caveat under the totals
whenever the current window contains such a row, since a reader who never hovers should
still learn that Total cost and Avg / session undercount those sessions
(issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment 903).
"""
from __future__ import annotations

import json
from pathlib import Path

import design_system as ds
import orient_time
from paths import STATE_ROOT

ACTIVITY_FILE = Path.home() / "Convivy" / "agent-runs" / "activity" / "sessions.json"

# agent-activity-report (agent-platform) writes this snapshot every 15 minutes
# (the agent-activity-report job's plist fires at :02/:17/:32/:47 past the hour)
# — a fixed, frequent cadence, so a couple of missed cycles is already a real
# signal rather than weekend noise, unlike a once-daily harvest.
_STALLED_AFTER_HOURS = 2
_EXPECTED_CADENCE = "every 15 minutes"


def _load_snapshot() -> dict:
    try:
        return json.loads(ACTIVITY_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"generated_at": None, "session_count": 0, "sessions": []}


def freshness(snap: dict) -> ds.Freshness:
    """This dashboard's own freshness verdict — read verbatim by /activity's
    own page and by its /overview card, so the two can't disagree
    (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment 902)."""
    gen = snap.get("generated_at")
    text = orient_time.fmt(gen) or "no freshness reported"
    hours = orient_time.hours_since(gen)
    stalled = hours is not None and hours > _STALLED_AFTER_HOURS
    return ds.Freshness(
        text=text, stalled=stalled, cadence=_EXPECTED_CADENCE,
        reason="The activity-report producer may have stopped running.",
    )


def live_html() -> str:
    """Render the /activity page from the current snapshot at request time."""
    return _render(_load_snapshot())


def thin(snap: dict) -> dict:
    """`snap` with each session's `nodes` (its dispatch tree) removed.

    The page embeds this, and fetches one session's tree from
    `/activity/nodes` when its row is expanded
    (issue_01M388E7WD8C83T5430YS88EJG). On 2026-09-29 the full snapshot held
    1,959 sessions and rendered as 14.7 MB, and `nodes` was 13.5 MB of it:
    every tree was shipped to every viewer although a reader opens a few."""
    return {**snap, "sessions": [{k: v for k, v in s.items() if k != "nodes"}
                                 for s in snap.get("sessions", [])]}


def session_nodes(session_id: str) -> list | None:
    """One session's dispatch tree, or None when no session has that id."""
    for s in _load_snapshot().get("sessions", []):
        if s.get("session_id") == session_id:
            return s.get("nodes") or []
    return None


def build() -> None:
    out = STATE_ROOT / "site"
    out.mkdir(parents=True, exist_ok=True)
    snap = _load_snapshot()
    (out / "activity.html").write_text(_render(snap), encoding="utf-8")
    print(f"Wrote site/activity.html ({snap['session_count']} sessions).")


def _render(snap: dict) -> str:
    data_json = json.dumps(thin(snap), ensure_ascii=False).replace("</", "<\\/")
    fresh = freshness(snap)
    page_head = (
        '<div class="page-head">'
        '<h1>Session activity</h1>'
        '<p class="sub">Every agentic session and its full dispatch tree — cost summed '
        'down each parent&#8594;worker chain. Click a row to expand its workers.</p>'
        f'<p class="asof">snapshot {fresh.text} · refreshed every 15&nbsp;min</p>'
        "</div>"
    )
    banner = ds.stalled_banner_html("Session activity", fresh)
    body = page_head + banner + _BODY + _SCRIPT.replace("__DATA__", data_json)
    return ds.page_shell(
        title="session activity",
        nav_html=ds.nav_component("/activity", "full"),
        body=body,
        extra_css=_CSS,
    )


_CSS = r"""
.page-head h1 { margin:0 0 4px; }
.page-head .sub { color:var(--ink-2); margin:0 0 2px; max-width:70ch; }
.page-head .asof { color:var(--ink-2); font-size:12px; margin:0 0 14px; }
.filters { display:flex; flex-wrap:wrap; gap:16px; align-items:flex-end; margin:0 0 16px; }
.filters .fg { display:flex; flex-direction:column; gap:4px; }
.filters label { font-size:12px; color:var(--ink-2); }
.statcards { display:flex; flex-wrap:wrap; gap:14px; margin:0 0 16px; }
.statcards .card { background:var(--surface-2); border:1px solid var(--line); border-radius:10px; padding:12px 16px; min-width:130px; }
.statcards .card .k { font-size:12px; color:var(--ink-2); }
.statcards .card .v { font-size:22px; font-weight:600; margin-top:2px; }
.srow { cursor:pointer; }
.srow:hover { background:var(--brand-wash); }
.srow .twist { display:inline-block; width:1em; color:var(--ink-2); transition:transform .12s; }
.srow.open .twist { transform:rotate(90deg); }
.pill { display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px; border:1px solid var(--line); }
.pill.scheduled { color:var(--brand); border-color:var(--brand); }
.pill.full_monty { color:var(--warn); border-color:var(--warn); }
.pill.interactive { color:var(--ink-2); }
.workers-only { color:var(--warn); font-size:11px; margin-left:6px; }
.cost-caveat { color:var(--warn); font-size:12.5px; margin:0 0 16px; max-width:70ch; }
.drow > td { padding:0 !important; background:var(--surface-2); }
.drow .inner { padding:8px 8px 14px 28px; }
.subtable { width:100%; border-collapse:collapse; font-size:12.5px; }
.subtable th { text-align:left; color:var(--ink-2); font-weight:500; padding:4px 8px; border-bottom:1px solid var(--line); }
.subtable td { padding:4px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
.subtable .depth1 td:first-child { padding-left:18px; }
.subtable .depth2 td:first-child { padding-left:34px; }
.subtable .num { text-align:right; font-variant-numeric:tabular-nums; }
.mono { font-family:var(--mono, ui-monospace, monospace); }
.muted { color:var(--ink-2); }
.ok { color:var(--ok, var(--brand)); }
.warn { color:var(--warn); }
.bad { color:var(--err); }
.empty { color:var(--ink-2); padding:30px; text-align:center; }
"""


_BODY = r"""
  <div class="filters">
    <div class="fg">
      <label class="tip" tabindex="0" data-tip="Keeps sessions whose top-level dispatch started inside the window (local MST).">Window</label>
      <select id="window">
        <option value="1">Today</option>
        <option value="7" selected>Last 7 days</option>
        <option value="30">Last 30 days</option>
        <option value="90">Last 90 days</option>
        <option value="custom">Custom range…</option>
      </select>
    </div>
    <div class="fg" id="act-range-fg" style="display:none">
      <label class="tip" tabindex="0" data-tip="Inclusive date range over each session's start date (MST). Leave a side blank for open-ended.">Custom range</label>
      <div class="cprange"><input type="date" id="act-from" class="dateinp"><span class="muted">&#8594;</span><input type="date" id="act-to" class="dateinp"></div>
    </div>
    <div class="fg">
      <label class="tip" tabindex="0" data-tip="Interactive = a co session you drove (agentctl spawns + captured Task subagents). Scheduled = overnight/launchd. Full-monty = a manual full drain.">Origin</label>
      <div class="chips" id="f-origin"></div>
    </div>
    <div class="fg">
      <label class="tip" tabindex="0" data-tip="The repo the session targeted.">Product</label>
      <div class="chips" id="f-product"></div>
    </div>
  </div>
  <div class="statcards" id="cards"></div>
  <div class="cost-caveat" id="costCaveat" style="display:none"></div>
  <div class="data-table-wrap"><table class="data-table" id="tbl"><thead></thead><tbody></tbody></table></div>
  <div class="empty" id="empty" style="display:none">No sessions match these filters.</div>
"""


_SCRIPT = r"""<script>
const SNAP = __DATA__;
const SESSIONS = SNAP.sessions || [];
const state = { window:7, product:new Set(), origin:new Set() };

// America/Phoenix, not America/Denver. Convivy standardizes on Mountain
// STANDARD Time — a fixed UTC-7 year-round — and Denver shifts to UTC-6 every
// summer, so the two disagree by an hour for half the year while both calling
// themselves "Mountain". Keep this in step with orient_time.MST on the Python
// side; a page that picks its own zone is how the site ends up telling two
// different times.
const TZ = "America/Phoenix";
const fmtUSD = (n) => "$" + (n||0).toFixed(2);
function fmtDur(s){
  if(s===null||s===undefined) return "—";
  s = Math.round(s);
  const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), sec=s%60;
  if(h) return h+"h"+String(m).padStart(2,"0")+"m";
  if(m) return m+"m"+String(sec).padStart(2,"0")+"s";
  return sec+"s";
}
function localDT(iso){
  if(!iso) return "—";
  try { return new Date(iso).toLocaleString("en-US",{timeZone:TZ,month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}); }
  catch(e){ return iso; }
}
function localTime(iso){
  if(!iso) return "—";
  try { return new Date(iso).toLocaleTimeString("en-US",{timeZone:TZ,hour:"numeric",minute:"2-digit",second:"2-digit"}); }
  catch(e){ return iso; }
}
function localDate(iso){
  if(!iso) return null;
  try { return new Date(iso).toLocaleDateString("en-CA",{timeZone:TZ}); } // YYYY-MM-DD
  catch(e){ return null; }
}
const esc = (s) => String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

const uniq = (key) => [...new Set(SESSIONS.map(f=>f[key]))].filter(Boolean).sort();

function withinWindow(s){
  const d = localDate(s.started_at);
  if(!d) return false;
  if(state.window==="custom"){
    const from=document.getElementById("act-from").value, to=document.getElementById("act-to").value;
    return (!from||d>=from)&&(!to||d<=to);
  }
  const days = +state.window;
  const cutoff = new Date(Date.now() - days*86400000);
  return new Date(s.started_at) >= cutoff;
}
function passFilters(s){
  if(state.product.size && !state.product.has(s.product)) return false;
  if(state.origin.size && !state.origin.has(s.origin)) return false;
  return true;
}
// Sort by last activity (most-recent node end, else start), not launch time, so a
// still-open session — whose latest worker just finished — floats to the top where
// it's expected, instead of sinking below sessions that launched later (the operator 2026-06-30).
function lastActivity(s){ return s.ended_at || s.started_at || ""; }
function rows(){
  return SESSIONS.filter(s=>withinWindow(s)&&passFilters(s))
                 .sort((a,b)=> String(lastActivity(b)).localeCompare(String(lastActivity(a))));
}

function chip(container, key, val){
  const sel = state[key].has(val);
  return '<button type="button" class="chip'+(sel?" on":"")+'" data-k="'+key+'" data-v="'+esc(val)+'">'+esc(val)+'</button>';
}
function renderChips(){
  document.getElementById("f-origin").innerHTML = uniq("origin").map(v=>chip(null,"origin",v)).join("");
  document.getElementById("f-product").innerHTML = uniq("product").map(v=>chip(null,"product",v)).join("");
  document.querySelectorAll(".chip").forEach(b=>b.onclick=()=>{
    const k=b.dataset.k, v=b.dataset.v;
    state[k].has(v)?state[k].delete(v):state[k].add(v);
    b.classList.toggle("on"); sync();
  });
}

function renderCards(rs){
  const total = rs.reduce((a,s)=>a+(s.total_usd||0),0);
  const workers = rs.reduce((a,s)=>a+(s.worker_count||0),0);
  const cards = [
    ["Sessions", rs.length],
    ["Total cost", fmtUSD(total)],
    ["Workers spawned", workers],
    ["Avg / session", fmtUSD(rs.length?total/rs.length:0)],
  ];
  document.getElementById("cards").innerHTML = cards.map(([k,v])=>
    '<div class="card"><div class="k">'+k+'</div><div class="v">'+v+'</div></div>').join("");

  // Visible without hovering — the "workers only" marker on a row is a tooltip a
  // reader has to find. This caveat sits under the totals it affects instead.
  const wo = rs.filter(s=>s.main_loop_captured===false).length;
  const caveat = document.getElementById("costCaveat");
  if(wo){
    caveat.style.display = "";
    caveat.textContent = wo+" of "+rs.length+" session"+(rs.length===1?"":"s")+" here "
      +(wo===1?"is an":"are")+" interactive co session"+(wo===1?"":"s")+" whose own main-loop cost "
      +"isn't captured yet (an execution-layer/Keel telemetry gap — only its spawned workers are "
      +"counted), so Total cost and Avg / session above undercount "+(wo===1?"it":"those rows")+".";
  } else {
    caveat.style.display = "none";
  }
}

const HEAD = ["", "Started (MST)","Origin","Product","Session / task","Cost","Duration","Workers"];

// The ten exit_status values agentctl can write (VALID_EXIT_STATUSES,
// agent-platform lib/agent-platform/telemetry.py), classed by what they mean
// rather than by whether `agentctl wait` exited 0. Each class is a plain
// object (not backed by Object.prototype) so a status named "toString" or
// "constructor" can't resolve through the prototype chain and render as a
// false match — Object.create(null) has no inherited properties at all.
// Add the next status to its class here, not as a new branch below.
const HEALTHY_EXIT_LABELS = Object.assign(Object.create(null), {
  completed: "completed",
  committed_not_pushed: "committed, not pushed",
  committed_local_only: "committed, no remote",
  // the resume-watchdog re-spawned the orchestrator after it died mid-drain;
  // terminal, not an error, and the 05:00 healthcheck treats it as fine.
  superseded: "superseded",
});
// primary_mutated: the primary-checkout guard fired, but the run's own work
// is usually real and the guard's blame is often wrong (it flagged three
// healthy runs with separate worktrees on 2026-09-13,
// issue_01M2EPEZMG8EWABTE8QSD57C8C) — neither healthy nor failed.
const ATTENTION_EXIT_LABELS = Object.assign(Object.create(null), {
  primary_mutated: "primary checkout changed",
});
const FAILED_EXIT_LABELS = Object.assign(Object.create(null), {
  errored: "errored",
  killed: "killed",
  budget_refused: "budget refused",
  capacity_refused: "capacity refused",
});
function renderTable(rs){
  const thead = document.querySelector("#tbl thead");
  const tbody = document.querySelector("#tbl tbody");
  thead.innerHTML = "<tr>"+HEAD.map(h=>'<th>'+h+'</th>').join("")+"</tr>";
  document.getElementById("empty").style.display = rs.length?"none":"block";
  tbody.innerHTML = rs.map((s,i)=>{
    const opill = '<span class="pill '+esc(s.origin)+'">'+esc(s.origin)+'</span>';
    const wo = s.main_loop_captured===false ? '<span class="workers-only" title="A co session’s own main-loop cost is not yet captured; this is its workers only.">workers only</span>' : "";
    const label = esc(s.label||s.task||s.agent);
    const main =
      '<tr class="srow" data-i="'+i+'">'
      + '<td data-label=""><span class="twist">&#9656;</span></td>'
      + '<td data-label="Started">'+esc(localDT(s.started_at))+'</td>'
      + '<td data-label="Origin">'+opill+'</td>'
      + '<td data-label="Product">'+esc(s.product)+'</td>'
      + '<td data-label="Session"><span class="mono muted">'+esc(s.agent)+'</span> '+label+wo+'</td>'
      + '<td class="num" data-label="Cost">'+fmtUSD(s.total_usd)+'</td>'
      + '<td class="num" data-label="Duration">'+fmtDur(s.duration_s)+'</td>'
      + '<td class="num" data-label="Workers">'+(s.worker_count||0)+'</td>'
      + '</tr>';
    const detail =
      '<tr class="drow" data-for="'+i+'" style="display:none"><td colspan="'+HEAD.length+'"><div class="inner">'
      + '<div class="muted">Loading workers…</div></div></td></tr>';
    return main + detail;
  }).join("");
  document.querySelectorAll(".srow").forEach(r=>r.onclick=()=>{
    const i=r.dataset.i;
    const d=document.querySelector('.drow[data-for="'+i+'"]');
    const open = d.style.display!=="none";
    d.style.display = open?"none":"table-row";
    r.classList.toggle("open", !open);
    if(!open) loadNodes(rs[i], d.querySelector(".inner"));
  });
}

// The embedded rows carry no dispatch tree, so it is fetched when a row first
// opens and kept on the session for the next open
// (issue_01M388E7WD8C83T5430YS88EJG).
function loadNodes(s, inner){
  if(s.nodes){ inner.innerHTML = subTable(s.nodes); return; }
  fetch("/activity/nodes?id="+encodeURIComponent(s.session_id),{credentials:"same-origin"})
    .then(r=>{ if(!r.ok) throw new Error("HTTP "+r.status); return r.json(); })
    .then(j=>{ s.nodes = j.nodes||[]; inner.innerHTML = subTable(s.nodes); })
    .catch(e=>{ inner.innerHTML = '<div class="muted">Couldn’t load workers ('+esc(e.message)+').</div>'; });
}

function subTable(list){
  // Latest activity first within an expanded session, so the most recent worker is at
  // the top (the operator 2026-06-30). Depth indentation still conveys the dispatch tree.
  const nodes = (list||[]).slice()
    .sort((a,b)=> String(b.started_at||"").localeCompare(String(a.started_at||"")));
  if(!nodes.length) return '<div class="muted">No captured workers.</div>';
  const cols = ["Spawned","Agent","Task","Model","Dur","Cost","Turns","Outcome"];
  const head = "<tr>"+cols.map(c=>'<th'+(["Dur","Cost","Turns"].includes(c)?' class="num"':"")+'>'+c+'</th>').join("")+"</tr>";
  const body = nodes.map(n=>{
    const status = n.exit_status;
    let oc;
    if (status === "running") {
      oc = '<span class="muted">running</span>';
    } else if (Object.hasOwn(HEALTHY_EXIT_LABELS, status)) {
      oc = '<span class="ok">'+HEALTHY_EXIT_LABELS[status]+'</span>';
    } else if (Object.hasOwn(ATTENTION_EXIT_LABELS, status)) {
      oc = '<span class="warn">'+ATTENTION_EXIT_LABELS[status]+'</span>';
    } else if (Object.hasOwn(FAILED_EXIT_LABELS, status)) {
      oc = '<span class="bad">'+FAILED_EXIT_LABELS[status]+'</span>';
    } else {
      oc = '<span class="bad">'+esc(status)+'</span>';
    }
    const prs = (n.prs_opened&&n.prs_opened.length)?' <span class="muted">('+n.prs_opened.length+' PR)</span>':"";
    return '<tr class="depth'+(n.depth||0)+'">'
      + '<td class="mono">'+esc(localTime(n.started_at))+'</td>'
      + '<td>'+esc(n.agent)+'</td>'
      + '<td>'+esc(n.label||n.task||"")+'</td>'
      + '<td class="mono muted">'+esc(n.model)+'</td>'
      + '<td class="num">'+fmtDur(n.duration_s)+'</td>'
      + '<td class="num">'+fmtUSD(n.usd)+'</td>'
      + '<td class="num">'+(n.turns==null?"—":n.turns)+'</td>'
      + '<td>'+oc+prs+'</td>'
      + '</tr>';
  }).join("");
  return '<table class="subtable"><thead>'+head+'</thead><tbody>'+body+'</tbody></table>';
}

function sync(){
  const rs = rows();
  renderCards(rs);
  renderTable(rs);
  try {
    window._ds.writeUrlState({window:state.window,
      product:[...state.product].join(","), origin:[...state.origin].join(",")}, true);
  } catch(e){}
}

function init(){
  // restore from URL
  try {
    const u = window._ds.readUrlState();
    if(u.window){ state.window=u.window; }
    if(u.product){ u.product.split(",").filter(Boolean).forEach(v=>state.product.add(v)); }
    if(u.origin){ u.origin.split(",").filter(Boolean).forEach(v=>state.origin.add(v)); }
  } catch(e){}
  const wsel = document.getElementById("window");
  wsel.value = String(state.window);
  document.getElementById("act-range-fg").style.display = state.window==="custom"?"flex":"none";
  wsel.onchange = ()=>{
    state.window = wsel.value;
    document.getElementById("act-range-fg").style.display = state.window==="custom"?"flex":"none";
    sync();
  };
  ["act-from","act-to"].forEach(id=>document.getElementById(id).onchange=sync);
  renderChips();
  sync();
}
init();
</script>"""


if __name__ == "__main__":
    build()
