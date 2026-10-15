#!/usr/bin/env python3.11
"""velocity_build.py — the /velocity dashboard.

**Headline: waypoints passed**, computed live from the Library's Courses on
every render (dashboards epic issue_01M2F3Q1B8J9E0WC5J1F1CMWA1) — a waypoint
is an epic seen from its crew's Course, passed when the epic's state is
terminal (the operator, 2026-08-07). `waypoints.course_progress()` is the shared
computation `home_build`'s front-page hero stat also calls, so the two
surfaces cannot disagree. Nothing here is stored in Orient's control
database; a Library outage renders the waypoints section as unavailable
without taking the rest of the page down (`_waypoints_section`).

Pace is also shown over trailing 7/30/90-day windows, total and per product,
using each waypoint's `resolved_at` (`waypoints.WINDOWS`/`total_within`) —
see `waypoints`'s module docstring for what that's read from and the one
case (a `resolved_at`-less passed entry) it can't place in any window.

**Weekly trend** (`_weekly_pace_section`) — one mark per week from
`waypoints.weekly_pace()`, in the same SVG idiom as the Delivery index trend
below so the two read as peers rather than unrelated widgets
(issue_01M2HXZKHKZFVZK9K1MJ5KTKTA). A quiet week is a zero mark, not a gap;
the current week is ringed and labelled partial. Computed from the same
`fetch_courses()` read the headline cards above use — this section adds no
Library call of its own.

**Secondary: "Delivery activity"** — commits, merged PRs, code-health and
decision throughput, total + by product/bucket, over a 7/30/90-day window.
Reads the rollup that agent-platform `velocity-report` writes to
~/Convivy/agent-runs/velocity/velocity.json. The approved spec (dashboards
epic issue_01M2F3Q1B8J9E0WC5J1F1CMWA1) makes merged PRs a delivery-activity
signal rather than this page's pace-toward-the-bead headline, so its
composite score is labeled "Delivery index" here, not "velocity".

Emits a self-contained site/velocity.html. Full-tier only (server.py
FULL_ONLY)."""
from __future__ import annotations

import json
from html import escape
from pathlib import Path

import design_system as ds
import orient_time
import waypoints
from paths import STATE_ROOT

OUT = STATE_ROOT / "site"
DATA_PATH = Path.home() / "Convivy" / "agent-runs" / "velocity" / "velocity.json"
HISTORY_PATH = Path.home() / "Convivy" / "agent-runs" / "velocity" / "velocity-history.jsonl"


def _load_history() -> list[dict]:
    """The weekly delivery-index trend points (oldest→newest), or [] if none yet."""
    if not HISTORY_PATH.exists():
        return []
    rows = []
    for ln in HISTORY_PATH.read_text().splitlines():
        ln = ln.strip()
        if ln:
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    rows.sort(key=lambda r: r.get("week", ""))
    return rows


def load() -> dict:
    data = {"windows": {}, "bucket_order": [], "notes": [], "generated_at": ""}
    if DATA_PATH.exists():
        try:
            data = json.loads(DATA_PATH.read_text())
        except json.JSONDecodeError:
            pass
    data["history"] = _load_history()
    return data


# agent-platform's the velocity-report job's plist runs this harvest daily at
# 06:45 MST. 30h tolerates a late-running harvest without hiding a feed
# that's gone dark for the better part of two days.
_STALLED_AFTER_HOURS = 30
_EXPECTED_CADENCE = "daily (06:45 MST)"


def freshness(data: dict) -> ds.Freshness:
    """This dashboard's own freshness verdict for /velocity's report — read
    verbatim by /velocity's own page and by its /overview card
    (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment 902)."""
    gen = data.get("generated_at")
    text = orient_time.fmt(gen) or "no freshness reported"
    hours = orient_time.hours_since(gen)
    stalled = hours is not None and hours > _STALLED_AFTER_HOURS
    return ds.Freshness(
        text=text, stalled=stalled, cadence=_EXPECTED_CADENCE,
        reason="The daily velocity-report harvest may not have run.",
    )


def _waypoints_section(scope: frozenset[str]) -> str:
    """The page's headline: waypoints passed over the trailing 7/30/90-day
    windows, total and per product, plus each Course's position (passed of
    total), the entry in flight, what remains, and the weekly pace trend
    (`_weekly_pace_section`). Catches a Library failure locally, rather than
    letting it propagate, so a Library outage degrades only this section —
    the "Delivery activity" panel below reads a local file and has nothing
    to do with the Library being up.

    Restricted to `scope` (E19·3, issue_01M35K5EAZD85FBYXEBQVRKVZV): a Course
    whose `product` is outside the viewer's scope is dropped before any
    window total, table row, or weekly mark is computed from it — so the
    windowed totals are the scoped sum, not the fleet sum, for a scoped
    viewer. An administrator's scope is the Library's full project list, so
    nothing is dropped for them.

    One `fetch_courses()` read backs every figure here — the windowed cards,
    the per-course table, and the weekly trend below all derive from it, so
    this section costs the page the same one-`get_course`-per-Course budget
    it always has (issue_01M2HXZKHKZFVZK9K1MJ5KTKTA)."""
    try:
        courses = [c for c in waypoints.fetch_courses() if c.product in scope]
        rows = waypoints.course_progress(courses)
        weekly = waypoints.weekly_pace(courses)
        unresolved = waypoints.unresolved_passed_count(courses)
    except Exception:
        return ds.error_state("Could not load waypoint pace (the Library could not be reached).")

    total_passed = sum(r.passed for r in rows)
    total_all = sum(r.total for r in rows)
    position = f"{total_passed} of {total_all} passed" if rows else "no live Courses"
    cards = "".join(
        ds.stat_card(f"Waypoints passed ({days}d)", str(waypoints.total_within(rows, days)), position)
        for days in waypoints.WINDOWS
    )
    if rows:
        table_rows = "".join(
            f'<tr><td class="k">{escape(r.product)}</td>'
            f'<td>{escape(r.title)}</td>'
            f'<td>{r.passed} / {r.total}</td>'
            + "".join(f'<td>{r.passed_within[days]}</td>' for days in waypoints.WINDOWS)
            + f'<td>{escape(r.in_flight) if r.in_flight else "&mdash;"}</td>'
            f'<td>{r.remaining}</td></tr>'
            for r in rows
        )
        window_ths = "".join(f'<th>{days}d</th>' for days in waypoints.WINDOWS)
        table_html = (
            '<div class="data-table-wrap"><table class="data-table">'
            f'<thead><tr><th>Product</th><th>Course</th><th>Passed</th>{window_ths}'
            '<th>In flight</th><th>Remaining</th></tr></thead>'
            f'<tbody>{table_rows}</tbody></table></div>'
        )
    else:
        table_html = ds.empty_state("No live Courses yet.")
    return f'<div class="statcards">{cards}</div>{table_html}{_weekly_pace_section(weekly, unresolved)}'


def _weekly_pace_columns(weekly: list) -> list[str]:
    """Group columns for the weekly breakdown table, "Builder" first (the operator,
    2026-09-15) then each product alphabetically — only groups that actually
    appear in `weekly`, so a quiet product doesn't grow the table forever."""
    groups: set[str] = set()
    for w in weekly:
        groups.update(w.by_group)
    rest = sorted(g for g in groups if g != "Builder")
    return (["Builder"] if "Builder" in groups else []) + rest


def _weekly_pace_svg(weekly: list) -> str:
    """The weekly waypoints-passed trend, in the same SVG idiom as the
    Delivery index trend below (`trendSvg` in `_BODY_SCRIPT`) so the two
    read as peers — a line+area chart with one dot per week, the current
    week's dot ringed and labelled "partial" so its often-lower count isn't
    read as a fall in pace."""
    if len(weekly) < 2:
        return f'<div class="viewnote">Trend builds one point per week — {len(weekly)} so far.</div>'
    W, H = 600, 140
    L, R, T, B = 30, 590, 12, 110
    n = len(weekly)
    scale = max((w.total for w in weekly), default=0) or 1

    def x(i: int) -> float:
        return L + (R - L) * (i / (n - 1))

    def y(v: float) -> float:
        return B - (v / scale) * (B - T)

    ticks = sorted({0, scale // 2, scale})
    grid = "".join(
        f'<line class="vtrend-grid" x1="{L}" y1="{y(v):.1f}" x2="{R}" y2="{y(v):.1f}"/>'
        f'<text class="vtrend-glabel" x="{L - 4}" y="{y(v) + 3:.1f}" text-anchor="end">{v}</text>'
        for v in ticks
    )
    line = " ".join(f'{"M" if i == 0 else "L"}{x(i):.1f} {y(w.total):.1f}' for i, w in enumerate(weekly))
    area = (f'M{x(0):.1f} {B} '
            + " ".join(f'L{x(i):.1f} {y(w.total):.1f}' for i, w in enumerate(weekly))
            + f' L{x(n - 1):.1f} {B} Z')
    dots = "".join(
        f'<circle class="vtrend-dot{" last" if i == n - 1 else ""}{" partial" if w.partial else ""}" '
        f'cx="{x(i):.1f}" cy="{y(w.total):.1f}" r="{3.5 if i == n - 1 else 2.5}">'
        f'<title>{escape(w.week)}: {w.total} passed'
        + (" (partial week)" if w.partial else "")
        # `g` is a Course's `product` straight off the Library (or the literal
        # "Builder"), free text this client never restricts to a known slug — so
        # it is escaped here exactly as it is in the breakdown table's header
        # below, which renders the same `by_group` keys.
        + (" — " + ", ".join(f"{escape(g)}: {c}" for g, c in sorted(w.by_group.items()))
           if w.by_group else "")
        + '</title></circle>'
        for i, w in enumerate(weekly)
    )
    label_idxs = sorted({0, (n - 1) // 2, n - 1})
    xlabels = "".join(
        f'<text class="vtrend-xlabel" x="{x(i):.1f}" y="{B + 16}" '
        f'text-anchor="{"start" if i == 0 else ("end" if i == n - 1 else "middle")}">'
        f'{escape(weekly[i].week[5:])}</text>'
        for i in label_idxs
    )
    partial_label = ""
    if weekly[-1].partial:
        partial_label = (
            f'<text class="vtrend-glabel" x="{x(n - 1):.1f}" y="{y(weekly[-1].total) - 8:.1f}" '
            'text-anchor="end">partial</text>'
        )
    return (
        f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet" role="img" '
        'aria-label="Weekly waypoints-passed trend">'
        + grid + f'<path class="vtrend-area" d="{area}"/><path class="vtrend-line" d="{line}"/>'
        + dots + xlabels + partial_label + '</svg>'
    )


def _weekly_pace_table(weekly: list) -> str:
    """The weekly breakdown behind the chart above — Builder's own crews
    against each product, per week, adding to that week's total column."""
    columns = _weekly_pace_columns(weekly)
    if not columns:
        return ""
    head = "<th>Week</th>" + "".join(f"<th>{escape(g)}</th>" for g in columns) + "<th>Total</th>"
    rows = "".join(
        '<tr>'
        f'<td class="k">{escape(w.week)}{" (partial)" if w.partial else ""}</td>'
        + "".join(f'<td>{w.by_group.get(g, 0)}</td>' for g in columns)
        + f'<td>{w.total}</td></tr>'
        for w in weekly
    )
    return f'<div class="data-table-wrap"><table class="data-table"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>'


def _weekly_pace_section(weekly: list, unresolved: int) -> str:
    """/velocity's weekly waypoint-pace chart — a peer of the Delivery index
    trend below, computed from the same Library read the headline cards
    above use (`_waypoints_section`), never a second one
    (issue_01M2HXZKHKZFVZK9K1MJ5KTKTA)."""
    if not weekly:
        body = ds.empty_state("No weekly waypoint history yet.")
    else:
        body = _weekly_pace_svg(weekly) + _weekly_pace_table(weekly)
    note = ""
    if unresolved:
        plural, verb = ("s", "carry") if unresolved != 1 else ("", "carries")
        note = (f'<p class="viewnote">{unresolved} passed waypoint{plural} {verb} no resolved '
                'time — counted in the total above, in no week below.</p>')
    return (
        '<div class="vindex-trend wk-trend">'
        '<h3>Weekly trend · waypoints passed</h3>'
        + body + note +
        '</div>'
    )


_PAGE_CSS = r"""
/* ---- Page head --------------------------------------------------------- */
.page-head { margin:24px 0 0; }
.page-head h1 { font-size:22px; font-weight:700; letter-spacing:-.01em; color:var(--ink); }
.page-head h2 { font-size:17px; font-weight:700; letter-spacing:-.01em; color:var(--ink);
                margin-top:28px; padding-top:20px; border-top:1px solid var(--line); }
.page-sub { font-size:13px; color:var(--ink-2); margin-top:4px; margin-bottom:16px; }

/* ---- Window picker (segmented control) --------------------------------- */
.seg { display:inline-flex; border:1px solid var(--line); border-radius:var(--radius);
       overflow:hidden; background:var(--surface-2); padding:2px; gap:2px; margin-bottom:16px; }
.vbtn { padding:5px 13px; font-size:13px; font-weight:500; color:var(--ink-2);
        background:transparent; border:none; border-radius:calc(var(--radius) - 2px); cursor:pointer; }
.vbtn[aria-pressed="true"] {
  background:var(--surface); color:var(--brand-ink); box-shadow:var(--shadow); font-weight:650; }
.vbtn:hover:not([aria-pressed="true"]) { background:var(--surface); color:var(--ink); }
.vbtn:focus-visible { outline:2px solid var(--brand); outline-offset:2px; }

/* ---- View note --------------------------------------------------------- */
.viewnote { color:var(--ink-2); font-size:12px; margin:0 0 12px; }

/* ---- StatCards grid ---------------------------------------------------- */
.statcards { display:grid; grid-template-columns:repeat(4,1fr);
             gap:12px; margin-bottom:18px; }
@media (max-width:1024px) {
  .statcards { grid-template-columns:repeat(4,1fr); }
}
@media (max-width:640px) {
  .statcards { grid-template-columns:repeat(2,1fr); }
  .statcards > :last-child:nth-child(odd) { grid-column: 1 / -1; }
}
@media (max-width:360px) {
  .statcards { grid-template-columns:1fr; }
}

/* ---- Table decorations ------------------------------------------------- */
.data-table th, .data-table td { text-align:right; white-space:nowrap; }
.data-table th:first-child, .data-table td:first-child { text-align:left; }
td.k { font-weight:600; }
.data-table tbody tr:hover td { background:var(--brand-wash); }
tr.total td { border-top:2px solid var(--line); font-weight:700; background:var(--surface-2); }
tr.bkt td { background:var(--surface-2); font-weight:600; }
tr.sub-r td:first-child { color:var(--ink-2); }

/* ---- Color signals ----------------------------------------------------- */
.muted { color:var(--ink-2); }
.good { color:var(--ok); }
.warn { color:var(--warn); }
.bad { color:var(--err); }

/* ---- Notes ------------------------------------------------------------- */
.notes { color:var(--ink-2); font-size:12px; line-height:1.6; margin:18px 0 0;
         padding:10px 12px 10px 26px; border-left:2px solid var(--brand);
         background:var(--surface); border-radius:4px; }
.empty { color:var(--ink-2); padding:30px; text-align:center; }

/* ---- Tooltip right-align override -------------------------------------- */
.tipr::after { left:auto; right:0; transform:none; }

/* ---- Delivery index hero + trend --------------------------------------- */
.vindex { display:grid; grid-template-columns:auto 1fr; gap:20px; align-items:stretch;
          border:1px solid var(--line); border-radius:var(--radius); background:var(--surface);
          padding:16px 18px; margin-bottom:18px; }
@media (max-width:640px){ .vindex { grid-template-columns:1fr; } }
.vindex-score { display:flex; flex-direction:column; justify-content:center;
                min-width:150px; padding-right:20px; border-right:1px solid var(--line); }
@media (max-width:640px){ .vindex-score { border-right:none; border-bottom:1px solid var(--line);
                          padding-right:0; padding-bottom:12px; } }
.vindex-num { font-size:44px; font-weight:750; letter-spacing:-.02em; line-height:1;
              font-variant-numeric:tabular-nums; }
.vindex-num .of { font-size:18px; font-weight:600; color:var(--ink-2); margin-left:2px; }
.vindex-label { font-size:12px; color:var(--ink-2); margin-top:6px; }
.vindex-delta { font-size:12px; font-weight:600; margin-top:4px; }
.vindex-comps { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
.vchip { font-size:11px; padding:2px 7px; border-radius:20px; background:var(--surface-2);
         border:1px solid var(--line); color:var(--ink-2); white-space:nowrap; }
.vchip b { color:var(--ink); font-weight:650; font-variant-numeric:tabular-nums; }
.vindex-trend { display:flex; flex-direction:column; min-width:0; }
.vindex-trend h3 { font-size:12px; font-weight:600; color:var(--ink-2); margin:0 0 2px; }
.vindex-trend svg { width:100%; height:auto; max-height:170px; display:block; overflow:visible; }
.vtrend-line { fill:none; stroke:var(--brand); stroke-width:2; }
.vtrend-area { fill:var(--brand); opacity:.07; }
.vtrend-dot { fill:var(--surface); stroke:var(--brand); stroke-width:1.5; }
.vtrend-dot.last { fill:var(--brand); }
.vtrend-dot.stale { stroke:var(--warn); stroke-dasharray:2 1.5; }
.vtrend-dot.partial { stroke-dasharray:2 1.5; }
.vtrend-grid { stroke:var(--line); stroke-width:1; }
.vtrend-glabel { fill:var(--ink-2); font-size:9px; }
.vtrend-xlabel { fill:var(--ink-2); font-size:9px; }
.vscore-good { color:var(--ok); } .vscore-mid { color:var(--brand-ink); } .vscore-low { color:var(--err); }
td.idx { font-variant-numeric:tabular-nums; font-weight:650; }

/* ---- Weekly waypoint-pace trend (peer of the Delivery index trend) ---- */
.wk-trend { margin-bottom:18px; }
.wk-trend .data-table-wrap { margin-top:10px; }
"""

_BODY_SCRIPT = r"""<script>
const V = __DATA__;
(function(){
  let win = "30";
  const $ = id => document.getElementById(id);
  const esc = s => String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const n = x => (x==null?0:x).toLocaleString();
  const signed = x => (x>=0?"+":"") + n(x);
  const pct = x => x==null ? '<span class="muted">—</span>' : x.toFixed(0)+'%';
  const cfr = x => { if(x==null) return '<span class="muted">—</span>';
    const c=x>=0.15?'bad':x>0?'warn':'good';
    const g=c==='good'?'✓ ':c==='warn'?'△ ':'✕ ';
    return '<span class="'+c+'">'+g+(x*100).toFixed(0)+'%</span>'; };
  // delivery index (0–100): score class + a compact table cell.
  const scoreCls = s => s==null?'muted':(s>=70?'vscore-good':s>=45?'vscore-mid':'vscore-low');
  const idxCell = vi => { const s=(vi||{}).score;
    return s==null ? '<span class="muted">—</span>'
                   : '<span class="'+scoreCls(s)+'">'+s.toFixed(0)+'</span>'; };
  const COMP_LABEL = {throughput:'Throughput', survival:'Survival',
    ci_first_pass:'CI 1st-pass', features:'Features', stability:'Stability'};
  const TIPS = {
    Bucket: "A product or engineering grouping; its repos roll up here. ADRs attribute to the ADR's project bucket, not the knowledge repo.",
    Commits: "Commits on origin/main authored within the window (a daily git fetch keeps it current).",
    Merged: "Pull requests merged within the window (GitHub). Repos without a GitHub remote are skipped.",
    Features: "Merged PRs whose title carries a conventional feat: prefix. Every product's crew tags feature PRs feat: (the machine-global PR-title convention), so this is the features-shipped signal — earlier it undercounted any product that hadn't adopted the convention.",
    "Net LOC": "Lines added minus removed in the window, excluding lockfiles, generated, and vendored files. A weak signal — read it alongside the rest.",
    Reverts: "Commits whose subject reads as a revert/regression in the window — the numerator of change-failure rate.",
    "Chg-fail": "Change-failure rate: reverts ÷ PRs merged. Lower is better.",
    "CI 1st": "Share of runs whose CI passed on the first pull_request run (from the model-scorecard ledgers). Higher is better.",
    Survived: "Share of merged PRs whose 7-day window has elapsed that were not reverted and had no follow-up bug. Higher is better.",
    ADRs: "Architecture Decision Records minted in the window; for Engineering-internal it also shows ARCH entries added to substrate/decisions.md.",
    "PRs merged": "Pull requests merged within the window across all buckets.",
    ARCH: "ARCH decision entries added to substrate/decisions.md in the window.",
    "Lines of code": "Current tracked lines of code — the codebase's size now (lockfiles, generated, and vendored files excluded). Not a windowed delta. A weak signal — one input among several.",
    "Commits (total)": "All-time commits on origin/main.",
    "PRs (total)": "All-time merged pull requests (GitHub).",
    "Bucket / repo": "Each bucket with its repos broken out. ADRs attribute to the ADR's project, not the knowledge repo.",
    Index: "Delivery index (0–100) for this window: a weighted blend of throughput (35%), 7-day survival (25%), CI first-pass (20%), features shipped (10%), and stability, i.e. 1−change-failure (10%). Throughput and features are scored against a frozen per-bucket baseline (at baseline → 50, 2× → 100); the percentages are used directly. A component with no data drops out and the rest re-weight. Buckets with no measurable signal show —.",
  };
  const tipAttr = k => TIPS[k] ? ' class="tip" tabindex="0" data-tip="'+esc(TIPS[k]||'')+'"' : '';
  function makeStatCard(label, value) {
    return '<article class="statcard"><div class="statcard-value num">'+value+'</div>'
      +'<div class="statcard-label"><span'+tipAttr(label)+'>'+label+'</span></div></article>';
  }
  function cell(v, label){ return '<td data-label="'+label+'">'+v+'</td>'; }
  // A bucket rolls up several repos; V.alltime carries the repo list for every
  // view (windows.<w>.buckets.<b> does not), so a count needs no new data.
  // Silent for 0/1 repos — a count adds nothing when there's no aggregation
  // to disclose, and "(1 repos)" would just be wrong.
  function repoCountLabel(name){
    const bb = ((V.alltime||{}).buckets||{})[name];
    const repos = bb && bb.repos;
    if(!repos || repos.length < 2) return '';
    return ' <span class="muted">('+repos.length+' repos)</span>';
  }
  function row(name, m, isTotal){
    const netLocLabel = 'Net LOC ('+win+'d)';
    return '<tr'+(isTotal?' class="total"':'')+'>'
      + '<td data-label="Bucket" class="k">'+esc(name)+(isTotal?'':repoCountLabel(name))+'</td>'
      + '<td data-label="Index" class="idx">'+idxCell(m.velocity_index)+'</td>'
      + cell(n(m.commits), "Commits")
      + cell(n(m.prs_merged), "Merged")
      + cell(n(m.features), "Features")
      + cell(signed(m.loc_net), netLocLabel)
      + cell(m.reverts ? '<span class="warn">△ '+n(m.reverts)+'</span>' : '0', "Reverts")
      + cell(cfr(m.change_failure_rate), "Chg-fail")
      + cell(pct(m.ci_first_pass_pct), "CI 1st")
      + cell(pct(m.survival_pct), "Survived")
      + cell(n(m.adrs) + (m.arch ? ' <span class="muted">/ '+n(m.arch)+' ARCH</span>' : ''), "ADRs")
      + '</tr>';
  }
  function drawAlltime(){
    const at = V.alltime;
    if(!at){ $('table').innerHTML = '<div class="empty">No all-time data yet — the rollup hasn\'t run.</div>'; $('cards').innerHTML=''; return; }
    const t = at.total;
    const statCards = [
      ['Lines of code',n(t.loc_now)],
      ['Commits (total)',n(t.commits_total)],
      ['PRs (total)',n(t.prs_total)]
    ];
    $('cards').innerHTML = statCards.map(c=>makeStatCard(c[0].replace(' (total)',''), c[1])).join('');
    const cols = ['Bucket / repo','Lines of code','Commits (total)','PRs (total)'];
    // "Lines of code" here is the codebase's current size (loc_now), not a
    // windowed delta — say so in the header, not only the tooltip, since
    // that's the column a reader compares against the windowed table's Net LOC.
    const headerText = c => c==='Lines of code' ? 'LOC (current)' : c.replace(' (total)','');
    const ths = cols.map((c,i)=>'<th tabindex="0" class="tip'+(i>=cols.length-2?' tipr':'')+'" data-tip="'+esc(TIPS[c]||'')+'">'+headerText(c)+' </th>').join('');
    let html = '<div class="data-table-wrap"><table class="data-table"><thead><tr>'+ths+'</tr></thead><tbody>';
    for(const b of V.bucket_order){
      const bb = at.buckets[b]; if(!bb) continue;
      html += '<tr class="bkt"><td data-label="Bucket / repo" class="k">'+esc(b)+'</td>'
        +'<td data-label="LOC (current)">'+n(bb.loc_now)+'</td>'
        +'<td data-label="Commits">'+n(bb.commits_total)+'</td>'
        +'<td data-label="PRs">'+n(bb.prs_total)+'</td></tr>';
      for(const r of bb.repos){
        const flags = (r.no_local?' <span class="muted">· not cloned</span>':'')+(r.no_gh?' <span class="muted">· no GitHub</span>':'');
        html += '<tr class="sub-r"><td data-label="Repo">&nbsp;&nbsp;'+esc(r.repo)+flags+'</td>'
          +'<td data-label="LOC (current)">'+n(r.loc_now)+'</td>'
          +'<td data-label="Commits">'+n(r.commits_total)+'</td>'
          +'<td data-label="PRs">'+n(r.prs_total)+'</td></tr>';
      }
    }
    html += '<tr class="total"><td data-label="Bucket / repo" class="k">— Convivy total —</td>'
      +'<td data-label="LOC (current)">'+n(t.loc_now)+'</td>'
      +'<td data-label="Commits">'+n(t.commits_total)+'</td>'
      +'<td data-label="PRs">'+n(t.prs_total)+'</td></tr>';
    $('table').innerHTML = html + '</tbody></table></div>';
  }
  function trendSvg(hist){
    const pts = (hist||[]).filter(h => h.total!=null);
    if(pts.length < 2)
      return '<div class="viewnote">Trend builds one point per week — '
        + pts.length + ' so far.</div>';
    const W=600,H=140, L=30,R=590,T=12,B=110;
    const x = i => L + (R-L)*(i/(pts.length-1));
    const y = s => B - (s/100)*(B-T);
    let grid='';
    [0,25,50,75,100].forEach(v=>{ const yy=y(v).toFixed(1);
      grid += '<line class="vtrend-grid" x1="'+L+'" y1="'+yy+'" x2="'+R+'" y2="'+yy+'"/>'
        + '<text class="vtrend-glabel" x="'+(L-4)+'" y="'+(y(v)+3).toFixed(1)+'" text-anchor="end">'+v+'</text>';
    });
    const line = pts.map((p,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(p.total).toFixed(1)).join(' ');
    const area = 'M'+x(0).toFixed(1)+' '+B+' '
      + pts.map((p,i)=>'L'+x(i).toFixed(1)+' '+y(p.total).toFixed(1)).join(' ')
      + ' L'+x(pts.length-1).toFixed(1)+' '+B+' Z';
    let dots='';
    pts.forEach((p,i)=>{ const last=i===pts.length-1;
      dots += '<circle class="vtrend-dot'+(last?' last':'')+(p.pr_stale?' stale':'')
        + '" cx="'+x(i).toFixed(1)+'" cy="'+y(p.total).toFixed(1)+'" r="'+(last?3.5:2.5)+'">'
        + '<title>'+esc(p.week)+': '+p.total.toFixed(1)+(p.pr_stale?' (held — stale fetch)':'')+'</title></circle>';
    });
    let xl='';
    [...new Set([0, Math.floor((pts.length-1)/2), pts.length-1])].forEach(i=>{
      const anchor = i===0?'start':(i===pts.length-1?'end':'middle');
      xl += '<text class="vtrend-xlabel" x="'+x(i).toFixed(1)+'" y="'+(B+16)
        + '" text-anchor="'+anchor+'">'+esc((pts[i].week||'').slice(5))+'</text>';
    });
    return '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="xMidYMid meet" role="img" '
      + 'aria-label="Weekly delivery index trend">'
      + grid + '<path class="vtrend-area" d="'+area+'"/><path class="vtrend-line" d="'+line+'"/>'
      + dots + xl + '</svg>';
  }
  function renderVIndex(){
    const host = $('vindex'); if(!host) return;
    const vi = (((V.windows||{})['30']||{}).total||{}).velocity_index || {};
    const hist = V.history || [];
    const score = vi.score;
    const scoreHtml = score==null
      ? '<span class="muted">—</span>'
      : '<span class="'+scoreCls(score)+'">'+score.toFixed(0)+'</span><span class="of">/100</span>';
    let deltaHtml='';
    if(hist.length>=2){
      const cur=hist[hist.length-1].total, prev=hist[hist.length-2].total;
      if(cur!=null && prev!=null){ const d=cur-prev;
        const c=d>0?'vscore-good':(d<0?'vscore-low':'muted');
        const a=d>0?'▲':(d<0?'▼':'—');
        deltaHtml='<div class="vindex-delta '+c+'">'+a+' '+(d>0?'+':'')+d.toFixed(1)+' vs last week</div>';
      }
    }
    const comps = vi.components || {};
    const chips = Object.keys(comps)
      .sort((a,b)=>comps[b].weight-comps[a].weight)
      .map(k=>'<span class="vchip">'+esc(COMP_LABEL[k]||k)+' <b>'+Math.round(comps[k].value ?? 0)+'</b></span>')
      .join('');
    host.innerHTML =
      '<div class="vindex-score">'
      + '<div class="vindex-num">'+scoreHtml+'</div>'
      + '<div class="vindex-label"><span class="tip" tabindex="0" data-tip="'+esc(TIPS.Index||'')
      + '">Delivery index · 30-day</span></div>'
      + deltaHtml
      + (chips?'<div class="vindex-comps">'+chips+'</div>':'')
      + '</div>'
      + '<div class="vindex-trend"><h3>Weekly trend · trailing 30-day index</h3>'
      + trendSvg(hist) + '</div>';
  }
  function draw(){
    $('viewnote').textContent = win==='all'
      ? 'Cumulative across all history — codebase size, commits, and merged PRs ever, per repo.'
      : 'Activity in the trailing '+win+' days.';
    if(win==='all') return drawAlltime();
    const w = V.windows[win];
    if(!w){ $('table').innerHTML = '<div class="empty">No velocity data yet — the rollup hasn\'t run.</div>'; $('cards').innerHTML=''; return; }
    const t = w.total;
    const statCards = [
      ['Commits',n(t.commits)],['PRs merged',n(t.prs_merged)],['Features',n(t.features)],
      ['Net LOC',signed(t.loc_net)],['Reverts',n(t.reverts)],['ADRs',n(t.adrs)],['ARCH',n(t.arch)]
    ];
    $('cards').innerHTML = statCards.map(c=>makeStatCard(c[0],c[1])).join('');
    const cols = ['Bucket','Index','Commits','Merged','Features','Net LOC','Reverts','Chg-fail','CI 1st','Survived','ADRs'];
    // "Net LOC" carries its window in the header itself — a reader switching
    // from All-time's current-size column to this one sees it's a different
    // quantity (a windowed delta) without needing the tooltip.
    const headerText = c => c==='Net LOC' ? c+' ('+win+'d)' : c;
    const ths = cols.map((c,i)=>'<th class="tip'+(i>=cols.length-2?' tipr':'')+'" data-tip="'+esc(TIPS[c]||'')+'">'+headerText(c)+'</th>').join('');
    let html = '<div class="data-table-wrap"><table class="data-table"><thead><tr>'+ths+'</tr></thead><tbody>';
    for(const b of V.bucket_order) html += row(b, w.buckets[b]||{}, false);
    html += row('— Convivy total —', t, true);
    $('table').innerHTML = html + '</tbody></table></div>';
  }
  $('win').onclick = e => {
    const b = e.target.closest('[data-w]'); if(!b) return;
    win = b.dataset.w;
    [].forEach.call($('win').querySelectorAll('button'), c => c.setAttribute('aria-pressed', c===b?'true':'false'));
    draw(); _ds.writeUrlState(currentStateToParams(),false);
  };
  // URL state: serialize current win to URL params (default 30 omitted).
  function currentStateToParams(){
    var p={};
    if(win!=="30") p.win=win;
    return p;
  }
  // URL state: apply a params object to live win + reflect button state.
  // Never calls draw() — caller is responsible.
  function applyUrlState(s){
    var w=s.win;
    if(w==="7"||w==="90"||w==="all") win=w; else win="30";
    [].forEach.call($('win').querySelectorAll('button'),function(c){
      c.setAttribute('aria-pressed',c.dataset.w===win?'true':'false');
    });
  }
  applyUrlState(_ds.readUrlState());
  _ds.writeUrlState(currentStateToParams(),true);
  renderVIndex();
  draw();
  _ds.onUrlState(function(s){ applyUrlState(s); draw(); });
})();
</script>"""


# The two magnitudes differ by 5x and are quoted separately on purpose. Stating one
# figure for "LOC" would repeat the conflation this note sits next to: the reader who
# read 186K all-time against 982K over 30 days as one measure was reading two columns
# that answer different questions (issue_01M1S2PXE0SDKHJDBBZ5W5TZRT).
#
# The "before" figures are fixed history and safe to quote. The "after" ones are NOT
# pinned here, because they moved again within a day: the 2026-09-06 history repair
# below took 30-day net LOC from 59,313 to 50,981. Quote the ratio as measured on a
# named date instead, or this note needs editing every time the data changes.
_SERIES_BREAK_NOTE = (
    "LOC figures step down on 2026-09-05 and are not comparable across that date. The "
    "producer changed method (convivy/agent-platform#489 — markdown no longer counted, "
    "each merge commit counted once). Measured on 2026-09-06 for the Library bucket, "
    "current size fell to about a third of its former value while 30-day net LOC fell "
    "by more than sixteen times, from 188,635 and 982,011 respectively — the windowed "
    "column moved about five times further than the all-time one. Neither is a "
    "regression."
)

# Direction matters and the first draft had it backwards. The graft carried the repo's
# whole prior content as ONE 2026-08-23 commit of +131,120 insertions, and that date sat
# inside both trailing windows — so every line ever added before it counted as added in
# the last 30 days. Real history spreads those across May to September and most fall
# outside the window. So the truncation UNDER-reported commits and OVER-reported LOC at
# the same time; saying "conservative" would be wrong about the headline number.
_HISTORY_REPAIR_NOTE = (
    "History-based figures for the Library bucket changed again on 2026-09-06, when the "
    "local knowledge checkout was un-shallowed from 337 commits to 2,118 and history "
    "before 2026-08-23 became visible. Commit counts rose and windowed LOC fell: over 30 "
    "days, 708 commits to 1,284 and net LOC 59,313 to 50,981; over 90 days, 708 to 2,275 "
    "and 59,313 to 58,385. The truncation had been collapsing the repo's entire prior "
    "content into a single commit dated inside both windows, which inflated windowed LOC "
    "while hiding the commits that produced it."
)


def render(data: dict, scope: frozenset[str], whole_fleet: bool) -> str:
    """`whole_fleet` gates the whole "Delivery activity" section below: its
    buckets (`data["bucket_order"]`) come from the velocity-report producer's
    own engineering-grouping taxonomy, keyed by names like "Library" and
    "Bosun" that are Builder's own crews, not a Library project — and others
    ("Placard", "Archy", "Convivy general") that are features with no project
    of their own at all (E19·3, issue_01M35K5EAZD85FBYXEBQVRKVZV). Neither
    reconciles with the viewer's project-slug `scope`, so there is no safe way
    to filter this section's buckets, its fleet-wide Delivery-index composite
    score, or its weekly trend history (which carries no per-bucket
    breakdown) down to a scoped viewer's own projects — see the PR body for
    why this is left out rather than guessed at. A viewer whose scope covers
    every registry project still sees it unchanged."""
    fresh = freshness(data)
    notes = "".join(
        f"<li>{escape(n)}</li>" for n in [_SERIES_BREAK_NOTE, _HISTORY_REPAIR_NOTE, *data.get("notes", [])]
    )
    waypoints_html = _waypoints_section(scope)
    if not whole_fleet:
        return ds.page_shell(
            title="engineering velocity",
            nav_html=ds.nav_component("/velocity", "full"),
            body=(
                '<div class="page-head">'
                '<h1>Engineering velocity</h1>'
                '<p class="page-sub">Pace toward each crew\'s bead — waypoints passed, the entry in '
                'flight, and what remains, per product and in total.</p>'
                '</div>'
                + waypoints_html
            ),
            extra_css=_PAGE_CSS,
        )
    data_json = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    script = _BODY_SCRIPT.replace("__DATA__", data_json)
    body = (
        '<div class="page-head">'
        '<h1>Engineering velocity</h1>'
        '<p class="page-sub">Pace toward each crew\'s bead — waypoints passed, the entry in '
        'flight, and what remains, per product and in total.</p>'
        '</div>'
        + waypoints_html
        + '<div class="page-head">'
        '<h2>Delivery activity</h2>'
        f'<p class="page-sub">Commits, merged PRs, volume, stability &amp; decisions across '
        f'the org — activity, not pace toward the bead. As of {escape(fresh.text)}.</p>'
        '</div>'
        + ds.stalled_banner_html("Delivery activity report", fresh)
        + '<div class="seg" id="win" role="group" aria-label="Velocity window">'
        '<button type="button" class="vbtn" data-w="7" aria-pressed="false">7d</button>'
        '<button type="button" class="vbtn" data-w="30" aria-pressed="true">30d</button>'
        '<button type="button" class="vbtn" data-w="90" aria-pressed="false">90d</button>'
        '<button type="button" class="vbtn" data-w="all" aria-pressed="false">All-time</button>'
        '</div>'
        '<div class="vindex" id="vindex"></div>'
        '<div class="viewnote" id="viewnote"></div>'
        '<div class="statcards" id="cards"></div>'
        '<div id="table"></div>'
        f'<ul class="notes">{notes}</ul>'
        + script
    )
    return ds.page_shell(
        title="engineering velocity",
        nav_html=ds.nav_component("/velocity", "full"),
        body=body,
        extra_css=_PAGE_CSS,
    )


def live_html(scope: frozenset[str], whole_fleet: bool) -> str:
    """Render /velocity at request time (ARCH-182): one read of velocity.json for
    the delivery panel, plus a live Library read for waypoint pace
    (`waypoints.course_progress()`, one `get_course` per live Course). The
    `velocity-report` job stays the producer of velocity.json; main() still
    writes the static file as a fallback.

    `scope`/`whole_fleet`: E19·3, issue_01M35K5EAZD85FBYXEBQVRKVZV — see
    `render`'s docstring for what each gates."""
    return render(load(), scope, whole_fleet)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    # The static fallback is served (ARCH-182) to ANY viewer whose live render
    # throws, before scope is known to this file at all — the old unconditional
    # render(load()) named every Course's product and every Delivery-activity
    # bucket with no scope check on the way out (E19·3,
    # issue_01M35K5EAZD85FBYXEBQVRKVZV). Deliberately empty rather than that,
    # same discipline as issue_build.main().
    (OUT / "velocity.html").write_text(
        ds.page_shell(title="engineering velocity", nav_html=ds.nav_component("/velocity", "full"),
                     body=ds.empty_state("Live view unavailable — reload in a moment."),
                     extra_css=_PAGE_CSS),
        encoding="utf-8")
    print("Wrote site/velocity.html (static fallback, empty)")


if __name__ == "__main__":
    main()
