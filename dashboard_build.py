#!/usr/bin/env python3.11
"""Cost dashboards generator for convivy-orient (ARCH-107 / ARCH-191 follow-up).

Normalizes the scorecard ecosystem's append-only ledgers into one flat fact
record per PR/run, then emits two self-contained, standalone pages that slice
by product / agent / model / provider / time entirely client-side (the same grain
as the KB bundles: embed the data, pivot in the browser, no query backend):

  - `site/spending.html`         (/spending)        TOTAL spend, every session
  - `site/cost-performance.html` (/cost-performance) dev-crew cost-vs-quality

The split (ARCH-191) gives each its own page + its own date picker so either can
be screenshotted whole. Each picker carries a Custom date-range option.

Sources (all under ~/Convivy/agent-runs/scorecard/):
  - scorecard.jsonl          run-based cost+quality (vivy/constance/sous-chef)
  - hollis.jsonl             GitHub-only Hollis quality (no per-agent cost)
  - judge.jsonl              sampled LLM-judge scores (joined by run_id)

This is the DEV-crew cost+quality view. Hollis *runtime* LLM cost
(hollis/data/llm-spend.jsonl) is a separate, data-local feed — a
deliberate follow-up, not folded in here.

Served full-tier only (cost/perf is sensitive) — see server.py's routes.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from html import escape
from pathlib import Path

import alert_policy_store
import design_system as ds
import library_client
import orient_time
from control_db import ISSUE_TYPE_EPIC
from paths import STATE_ROOT

OUT = STATE_ROOT / "site"
SCORECARD_DIR = Path.home() / "Convivy" / "agent-runs" / "scorecard"

SCORECARD = SCORECARD_DIR / "scorecard.jsonl"
PA_LEDGER = SCORECARD_DIR / "hollis.jsonl"
JUDGE = SCORECARD_DIR / "judge.jsonl"


# Telemetry string → display for the three NYH projects (ARCH-426 amendment).
# Mirrors agent-platform cost_join._NYH_DISPLAY; the two must agree or the
# scorecard and /activity disagree about the same run. An explicit table rather
# than the `nyh-*` prefix match it replaces: a prefix would silently swallow a
# fourth property into whichever branch it hit, and a new building should appear as
# itself or not at all. `nyh-architecture` is the bare umbrella — one historical co
# session from 2026-06-08, before the per-repo sessions existed.
_NYH_DISPLAY = {
    "nyh-arch-tools": "NYH Arch Tools",
    "nyh-etc": "East Texas Circle",
    "nyh-park-villa": "Park Villa",
    "nyh-architecture": "NYH Arch Tools",
}


def _canon_product(repo):
    """Fold a raw repo string to its canonical product bucket, mirroring
    agent-platform cost_join._canonical_product. Hollis (formerly personal-assistant,
    ARCH-290) is tagged three ways across the rename — "hollis", "personal-assistant",
    "pa" — so unify those into one row rather than slivers.

    The NYH repos go the other way: they are THREE PROJECTS, each reporting for
    itself (ARCH-426 amendment, the operator 2026-07-31). They were folded into one "NYH
    Studio" bucket while the umbrella directory was the only thing holding them
    together; NYH Studio survives as the product all three build."""
    if not repo:
        return None
    if repo in _NYH_DISPLAY:
        return _NYH_DISPLAY[repo]
    if repo in ("hollis", "personal-assistant", "pa"):
        return "Hollis"
    return repo

# Cost panel feed (ARCH-136): the agent-cost-report producer writes these.
# Unlike the scorecard (autonomous dev-crew runs only), this captures TOTAL
# spend incl. interactive co sessions — the gap that hid the Fable burn.
COST_DIR = Path.home() / "Convivy" / "agent-runs" / "cost-report"

# Cost-per-outcome feed (View A, ARCH-191/239): the agent-activity-report producer
# writes outcomes.json — one row per opened PR, cost summed across its dispatch chain.
OUTCOMES_FILE = Path.home() / "Convivy" / "agent-runs" / "activity" / "outcomes.json"


def _load_outcomes() -> dict:
    try:
        return json.loads(OUTCOMES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"outcome_count": 0, "outcomes": [], "generated_at": None}


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def provider_of(model: str | None) -> str:
    """Provider from the model id: `deepseek/...` → deepseek, `claude-...` →
    anthropic. The model carries the provider; billing_source carries how it's
    paid (subscription vs metered)."""
    if not model:
        return "—"
    if "/" in model:
        return model.split("/", 1)[0]
    if model.startswith("claude"):
        return "anthropic"
    return "other"


def billing_of(billing_source: str | None) -> str:
    if not billing_source:
        return "—"
    if billing_source.startswith("provider_api"):
        return "metered"
    if "subscription" in billing_source:
        return "subscription"
    return billing_source


def _week(date_str: str | None) -> str:
    """Monday-anchored week start (ISO date) for the time grouping."""
    try:
        d = date.fromisoformat((date_str or "")[:10])
    except ValueError:
        return ""
    return (d - timedelta(days=d.weekday())).isoformat()


def normalize() -> list[dict]:
    """Flatten every ledger into a common fact record. One row per PR/run."""
    judge_by_run: dict = {}
    for j in _load(JUDGE):
        if j.get("run_id"):
            judge_by_run[j["run_id"]] = j

    facts = []

    def fact(r, *, source):
        run_id = r.get("run_id")
        j = judge_by_run.get(run_id, {})
        return {
            "product": _canon_product(r.get("repo")) or "—",
            "agent": r.get("agent") or "—",
            "model": r.get("model") or "—",
            "provider": provider_of(r.get("model")),
            "billing": billing_of(r.get("billing_source")),
            "date": (r.get("date") or "")[:10],
            "week": _week(r.get("date")),
            "cost_usd": r.get("cost_usd"),
            "resolved": r.get("pr_state") not in (None, "unresolved"),
            "merged": r.get("merged"),
            "survived": r.get("survived"),
            "ci_first_pass": r.get("ci_first_pass"),
            "review_score": j.get("review_thoroughness"),
            "fix_score": j.get("fix_quality"),
            "pr_number": r.get("pr_number"),
            "source": source,
        }

    for r in _load(SCORECARD):
        facts.append(fact(r, source="run"))
    for r in _load(PA_LEDGER):
        facts.append(fact(r, source="github-pr"))
    return facts


def cost_performance_freshness(facts: list[dict]) -> ds.Freshness:
    """This dashboard's own freshness verdict for /cost-performance — read
    verbatim by its own page and by its /overview card
    (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment 902).

    Deliberately `stalled=None`, always. Checked: `model-scorecard` (agent-
    platform's `the model-scorecard job's plist`) regenerates SCORECARD daily at
    07:30 MST, a real cadence — but the field this page ages against is each
    fact's `date`, which is calendar-day-only at the source (verified against
    scorecard.jsonl directly: `"date": "2026-08-25"`, no clock time). That
    value can't distinguish "the harvest didn't run" from "no PR merged that
    day," which is a normal, frequent outcome here (unlike a harvest that
    always writes a fresh timestamp regardless of what it finds). Any fixed
    threshold on it would fire on ordinary quiet stretches and train the
    reader to ignore it — worse than no judgment, per this clause's own
    instructions. So this shows the true data date and nothing more.
    """
    dates = [f["date"] for f in facts if f.get("date")]
    text = orient_time.fmt(max(dates)) if dates else "no freshness reported"
    return ds.Freshness(text=text, stalled=None)


def overage_line_html(snap: dict, thresholds: dict | None = None) -> str:
    """One line under the spend panel: was the SUBSCRIPTION actually in overage?

    Every dollar on this page is an API-RATE ESTIMATE against subscription usage
    that is mostly inside the plan, so a big number here has never meant money
    left the account. This line is the part that does, taken from the CLI's own
    rate-limit events (recorded per run, rolled up per day by agent-cost-report).

    THREE STATES, and the third is why this is not a boolean:
      not reported — no run that day carried the field. A day with no runs and a
                     day whose runs never emitted the event look identical, and
                     NEITHER is evidence of having stayed inside the plan.
      inside       — runs reported, none hit overage.
      in overage   — at least one run executed while extra usage was being drawn.

    ACCOUNT-WIDE, NOT PER-RUN. The quota belongs to the subscription, so this
    says the account was in overage while those runs executed — never that their
    dollars came out of credits. Sitting a per-account flag next to per-run cost
    figures invites exactly that misreading, so the label says so out loud.
    """
    ov = snap.get("overage") or {}
    if not ov:
        return _not_reported_html()
    return _reported_html(ov, thresholds)


def load_latest_snapshot() -> dict:
    """The current agent-cost-report snapshot (latest.json), or {} if missing or
    corrupt. Reused by alerts_build.py's /alerts evaluator so it reads the same
    file cost_panel_html() does rather than a second read path."""
    latest_p = COST_DIR / "latest.json"
    if not latest_p.exists():
        return {}
    try:
        return json.loads(latest_p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# agent-cost-report (agent-platform) refreshes latest.json's generated_at
# through the day; this repo's own daily regenerate at 07:40 MST
# (com.convivy.orient-dashboard.plist) is downstream of it. 30h tolerates a
# late-running harvest without hiding a feed that's gone dark for the better
# part of two days.
_SPEND_STALLED_AFTER_HOURS = 30
_SPEND_EXPECTED_CADENCE = "daily (07:40 MST)"


def spending_freshness(snap: dict) -> ds.Freshness:
    """This dashboard's own freshness verdict for /spending's snapshot — read
    verbatim by /spending's own page and by its /overview card
    (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment 902)."""
    gen = snap.get("generated_at")
    text = orient_time.fmt(gen) or "no freshness reported"
    hours = orient_time.hours_since(gen)
    stalled = hours is not None and hours > _SPEND_STALLED_AFTER_HOURS
    return ds.Freshness(
        text=text, stalled=stalled, cadence=_SPEND_EXPECTED_CADENCE,
        reason="The daily cost-report harvest may not have run.",
    )


def overage_status(ov: dict | None, thresholds: dict | None = None) -> str | None:
    """good/bad/None (not reported) for a day's subscription-overage record —
    the Spending policy threshold, reused by alerts_build.py's /alerts evaluator
    so the two can never disagree. None means no evidence either way (see
    overage_line_html's three-state note above); an alert evaluator must not
    read that as clean.

    `thresholds` defaults to `alert_policy_store.DEFAULTS` — pure, so a test
    calling this with one argument (every existing caller) gets the shipped
    default (`max_runs_in_overage: 0`, i.e. any run in overage is bad — the
    exact rule this function held before the threshold was settable) with no
    control_db touched."""
    if not ov:
        return None

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    n = _int(ov.get("runs_reporting"))
    if n <= 0:
        return None
    t = thresholds or alert_policy_store.DEFAULTS["spending.overage_runs"]
    return "bad" if _int(ov.get("runs_in_overage")) > t["max_runs_in_overage"] else "good"


def _not_reported_html() -> str:
    return ('<div class="overage-line muted">Subscription quota — '
                '<b>not reported</b> for this day. '
                '<span class="tip" tabindex="0" data-tip="No run that day recorded a '
                'rate-limit event. That is not the same as staying inside the plan — '
                'it means nothing was heard.">ⓘ</span></div>')


def _reported_html(ov: dict, thresholds: dict | None = None) -> str:
    # Coerced defensively: this dict is JSON another process wrote, and a bad
    # value must degrade this LINE rather than take the whole spending page down.
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    n = _int(ov.get("runs_reporting"))
    hit = _int(ov.get("runs_in_overage"))
    # Zero reporting runs is the NOT-REPORTED state wearing a rollup's clothes.
    # `overage_rollup` never emits it — it returns {} at zero — but a malformed or
    # future producer could, and "inside the plan — 0 runs reported" in green is
    # exactly the reassurance the three-state rule exists to withhold
    # (reviewer, #150). Zero knowledge renders as zero knowledge, whatever shape
    # it arrives in.
    if n <= 0:
        return _not_reported_html()
    peak = ov.get("peak_utilization")
    worst = ov.get("worst_status") or ""
    bits = []
    if peak is not None:
        try:
            bits.append(f"peak window {float(peak) * 100:.0f}% full")
        except (TypeError, ValueError):
            pass
    if worst == "rejected":
        bits.append("requests were turned away")
    elif worst == "allowed_warning":
        bits.append("crossed the warning threshold")
    detail = escape(" · ".join(bits))
    tip = ("Whether the SUBSCRIPTION was drawing extra usage while these runs ran. "
           "This is account-wide, not per-run: it does not say a particular run's "
           "cost came out of credits. The dollar figures above are API-rate "
           "estimates and cannot answer this question at all.")
    t = thresholds or alert_policy_store.DEFAULTS["spending.overage_runs"]
    cls = overage_status(ov, t)
    if hit > t["max_runs_in_overage"]:
        label = f"<b>in overage</b> — {hit} of {n} runs"
    else:
        label = f"<b>inside the plan</b> — {n} runs reported"
    tail = f" · {detail}" if detail else ""
    return (f'<div class="overage-line"><span class="muted">Subscription quota — </span>'
            f'<span class="{cls}">{label}</span>'
            f'<span class="muted">{tail}</span> '
            f'<span class="tip" tabindex="0" data-tip="{escape(tip)}">ⓘ</span></div>')


def cost_panel_html(thresholds: dict | None = None) -> str:
    """The Spending dashboard body: windowed 'spend — all sessions' (ARCH-136 /
    ARCH-168). Embeds the agent-cost-report 90-day by-model history + latest.json,
    then aggregates client-side over a selectable window (1/7/30/60/90d or a
    Custom date range, default 1d) plus a trends view. This is the interactive-
    inclusive cost view — the cost-performance scorecard only sees autonomous
    dev-crew runs, so a Fable/interactive spike is invisible there.

    `thresholds` defaults to `alert_policy_store.DEFAULTS` — pure, like
    `overage_status` — so a test calling this with no argument gets the
    shipped default with no control_db touched. `spending_live_html()` fetches
    the live value and threads it through.
    """
    t = thresholds or alert_policy_store.DEFAULTS["spending.overage_runs"]
    latest_p = COST_DIR / "latest.json"
    if not latest_p.exists():
        return ('<div class="costpanel"><div class="cph">Spend — all sessions</div>'
                '<div class="muted" style="padding:8px 0">No cost snapshot yet — '
                '<code>agent-cost-report</code> writes it every 4h.</div></div>')
    try:
        snap = json.loads(latest_p.read_text())
    except (json.JSONDecodeError, OSError):
        return '<div class="costpanel"><div class="cph">Spend — all sessions</div></div>'

    # 90-day by-model history feeds the windowed panel; latest.json supplies the
    # as-of cue + a one-day fallback. Both written together by agent-cost-report
    # (ARCH-168); pre-168 rows carry no by_model and degrade to totals-only.
    hist = []
    hp = COST_DIR / "history.jsonl"
    if hp.exists():
        for line in hp.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                hist.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    hist.sort(key=lambda r: r.get("date", ""))
    hist = hist[-90:]
    if not any(r.get("by_model") for r in hist):
        hist = [{
            "date": snap.get("date", ""),
            "total": snap.get("total", 0.0),
            "anthropic_total": snap.get("anthropic_total", 0.0),
            "gateway_total": snap.get("gateway_total", 0.0),
            "apps_total": snap.get("apps_total", 0.0),
            "fable": snap.get("fable", 0.0),
            "by_model": snap.get("by_model", []),
        }]
    hist_json = json.dumps(hist, separators=(",", ":")).replace("</", "<\\/")
    asof = escape(orient_time.fmt(snap.get("generated_at", "")))
    # Rendered from the SNAPSHOT's day, not the selected window: the quota rollup
    # is per-day and the window selector is client-side over history.jsonl, so a
    # 30d selection must not make this line silently describe one day of it.
    overage_html = overage_line_html(snap, t)
    alert_form = ds.alert_threshold_form_html(
        key="spending.overage_runs", title="Subscription-overage alert threshold",
        fields=[("max_runs_in_overage", "Alert past", t["max_runs_in_overage"], "1")])
    tip = ("TOTAL spend across the selected window, every session included — the view the "
           "cost-performance scorecard lacks (it only sees autonomous dev-crew runs). The by-model "
           "breakdown is the only place interactive co-session spend (Opus) surfaces. "
           "Anthropic models are a ccusage API-rate ESTIMATE from local transcripts "
           "(the subscription/credit consumers); gateway models (deepseek/kimi/gpt5) are "
           "ACTUAL provider cost from the spend log. Direct-API apps (PA, sous-chef) are "
           "real out-of-pocket spend from each app's own ledger. Refreshed every 4h by "
           "agent-cost-report.")
    head = f"""<div class="cp-title-row">
  <div class="cp-title-left">
    <span class="cph-inline">Spend — all sessions
      <span class="tip" tabindex="0" data-tip="{tip}">ⓘ</span>
    </span>
    <span class="muted cp-asof">as of {asof}</span>
  </div>
  <div class="cp-controls-right">
    <div class="seg" id="cp-window" role="group" aria-label="Spending window">
      <button type="button" class="vbtn" data-d="1" aria-pressed="true">1d</button>
      <button type="button" class="vbtn" data-d="7" aria-pressed="false">7d</button>
      <button type="button" class="vbtn" data-d="30" aria-pressed="false">30d</button>
      <button type="button" class="vbtn" data-d="60" aria-pressed="false">60d</button>
      <button type="button" class="vbtn" data-d="90" aria-pressed="false">90d</button>
      <button type="button" class="vbtn" data-d="custom" aria-pressed="false">Custom</button>
    </div>
    <div class="cprange" id="cp-range" style="display:none">
      <input type="date" id="cp-from" class="dateinp"><span class="muted">→</span><input type="date" id="cp-to" class="dateinp">
    </div>
    <div class="cprange" id="cp-daynav" style="display:none">
      <button type="button" class="vbtn" id="cp-prev" title="Previous day" aria-label="Previous day">◀</button>
      <span class="muted" id="cp-daylabel" style="min-width:150px;text-align:center;font-weight:600"></span>
      <button type="button" class="vbtn" id="cp-next" title="Next day" aria-label="Next day">▶</button>
    </div>
  </div>
</div>
<div class="spend-stats" id="cp-statcards" aria-label="Spending summary"></div>
{overage_html}
{alert_form}
<div class="costpanel">
  <div class="cost-detail-head">
    <h2>Cost detail</h2>
    <div class="seg" id="cp-view" role="tablist" aria-label="Spending view">
      <button type="button" class="vbtn" data-v="breakdown" aria-selected="true" role="tab" id="cp-tab-breakdown" aria-controls="cp-body">Breakdown</button>
      <button type="button" class="vbtn" data-v="trends" aria-selected="false" role="tab" id="cp-tab-trends" aria-controls="cp-body">Trends</button>
    </div>
  </div>
  <div class="costgrid" id="cp-body" role="tabpanel" aria-labelledby="cp-tab-breakdown"></div>
</div>
"""
    script = r"""<script>
const COST_HIST = __HIST__;
(function(){
  let win=1, view="breakdown", dayIdx=COST_HIST.length-1;
  const $ = id=>document.getElementById(id);
  const esc = s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const fmt = n=>"$"+n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
  function rowsForWin(){
    if(win==="custom"){
      const f=$("cp-from").value, t=$("cp-to").value;
      return COST_HIST.filter(r=>(!f||(r.date||"")>=f)&&(!t||(r.date||"")<=t));
    }
    if(win===1){ const r=COST_HIST[dayIdx]; return r?[r]:[]; }
    return COST_HIST.slice(-win);
  }
  // Day-stepper (shown only on the 1d preset): walk COST_HIST one calendar day
  // at a time. dayIdx is the index into COST_HIST of the viewed day; the newest
  // entry (length-1) is "today".
  function fmtDay(d){
    if(!d) return "";
    const dt=new Date(d+"T00:00:00");
    const s=dt.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"});
    return dayIdx>=COST_HIST.length-1 ? s+" (today)" : s;
  }
  function updateDayNav(){
    $("cp-daylabel").textContent=fmtDay((COST_HIST[dayIdx]||{}).date);
    const atStart=dayIdx<=0, atEnd=dayIdx>=COST_HIST.length-1;
    $("cp-prev").style.opacity=atStart?.3:1; $("cp-prev").style.pointerEvents=atStart?"none":"auto";
    $("cp-next").style.opacity=atEnd?.3:1;   $("cp-next").style.pointerEvents=atEnd?"none":"auto";
  }
  // API-rate vs Billed split:
  // - apiRate = every model's usage valued at API list price (the ccusage estimate, all kinds).
  // - billed  = actual out-of-pocket; subscription (kind "est") is covered by the flat plan and
  //             costs $0 here; only metered rows (kind "actual" = gateway, kind "api" = direct API)
  //             count. Confirmed: per-row billed sums to gateway_total + apps_total.
  function agg(rows){
    const models={}, tot={total:0,anthropic_total:0,gateway_total:0,apps_total:0,fable:0};
    for(const r of rows){
      for(const k in tot) tot[k]+=(r[k]||0);
      for(const m of (r.by_model||[])){
        const e=models[m.model]||(models[m.model]={model:m.model,cost:0,billed:0,kind:m.kind});
        e.cost+=m.cost;
        // metered rows (gateway=actual, direct-API=api) are real out-of-pocket; est=subscription=$0
        if(m.kind==="actual"||m.kind==="api") e.billed+=m.cost;
        e.kind=m.kind;
      }
    }
    const list=Object.values(models).filter(m=>m.cost>=0.005).sort((a,b)=>b.cost-a.cost);
    // billedTotal = gateway + direct-API (matches per-row billed sum exactly)
    const billedTotal=tot.gateway_total+tot.apps_total;
    return {tot,list,billedTotal};
  }
  function statCard(label,valueHtml,deltaHtml){
    return '<article class="statcard"><div class="statcard-label">'+esc(label)+'</div>'
      +'<div class="statcard-value num">'+valueHtml+'</div>'
      +(deltaHtml?'<div class="statcard-delta flat">'+deltaHtml+'</div>':'')
      +'</article>';
  }
  function drawStatCards(rows){
    const a=agg(rows);
    const ndays=rows.length;
    // Card 1: API-rate (the full model-list estimate at API list price)
    const apiRateCard=statCard("API-rate",fmt(a.tot.total),
      ndays>1?(fmt(a.tot.total/ndays)+"/day avg"):"");
    // Card 2: Billed (actual out-of-pocket; subscription usage = $0)
    const billedPct=a.tot.total>0?Math.round(100*a.billedTotal/a.tot.total):0;
    const billedCard=statCard("Billed",fmt(a.billedTotal),
      a.tot.total>0?(billedPct+"% of API-rate"):"");
    // Card 3: API-rate / day average
    const avgCard=statCard("API-rate / day",
      ndays>1?fmt(a.tot.total/ndays):"—","");
    // Card 4: Source Split (Anthropic subscription / Gateway / Direct API)
    const tot=a.tot.total||0.0001;
    const anPct=Math.round(100*a.tot.anthropic_total/tot);
    const gwPct=Math.round(100*a.tot.gateway_total/tot);
    const apPct=Math.round(100*a.tot.apps_total/tot);
    const splitCard='<article class="statcard"><div class="statcard-label">Source Split</div>'
      +'<div class="split-bars" aria-hidden="true">'
      +'<span style="width:'+anPct+'%;background:var(--brand)"></span>'
      +'<span style="width:'+gwPct+'%;background:var(--chart-2)"></span>'
      +'<span style="width:'+apPct+'%;background:var(--chart-3)"></span>'
      +'</div>'
      +'<div class="split-legend">'
      +'<span><i style="background:var(--brand)"></i>Anthropic '+anPct+'%</span>'
      +'<span><i style="background:var(--chart-2)"></i>Gateway '+gwPct+'%</span>'
      +'<span><i style="background:var(--chart-3)"></i>Direct '+apPct+'%</span>'
      +'</div></article>';
    $("cp-statcards").innerHTML=apiRateCard+billedCard+avgCard+splitCard;
  }
  function breakdown(rows){
    const a=agg(rows), mx=Math.max(...a.list.map(m=>m.cost),0.0001);
    const fab = a.tot.fable>0.005 ? ' · <span class="bad" style="font-weight:600">🚨 Fable '+fmt(a.tot.fable)+'</span>' : "";
    // kindLabel: shown as a sub-label under the model name so source is clear without a separate column
    const kindLabel=k=>({actual:"via gateway",api:"direct API"})[k]||"subscription";
    const trs = a.list.map(m=>{
      const isF=m.model.toLowerCase().includes("fable");
      const kl=kindLabel(m.kind);
      const bpct=Math.max(2,Math.round(100*m.cost/mx));
      return '<tr>'
        +'<td data-label="Model" class="k"><code>'+esc(m.model)+'</code>'+(isF?" ⚠️":"")
        +'<div class="model-sub muted">'+kl+'</div></td>'
        +'<td data-label="Share"><div class="bar-track" style="max-width:120px"><div class="bar-fill" style="width:'+bpct+'%"></div></div>'
        +'<div class="m-share" aria-hidden="true"><div class="m-share-track"><div class="m-share-fill" style="width:'+bpct+'%"></div></div></div></td>'
        +'<td data-label="API-rate" class="num'+(isF?' bad':'')+'">'+( isF?'✕ ':'')+fmt(m.cost)+'</td>'
        +'<td data-label="Billed" class="num">'+fmt(m.billed)+'</td>'
        +'</tr>';
    }).join("") || '<tr><td class="muted">no spend in window</td></tr>';
    // footer row: totals for both columns
    const footer='<tr class="tfoot">'
      +'<td colspan="2" class="muted" style="font-weight:600">Total</td>'
      +'<td data-label="API-rate" class="num" style="font-weight:600">'+fmt(a.tot.total)+'</td>'
      +'<td data-label="Billed" class="num" style="font-weight:600">'+fmt(a.billedTotal)+'</td>'
      +'</tr>';
    return (fab?'<div style="margin-bottom:6px">'+fab+'</div>':'')
      +'<div class="data-table-wrap"><table class="costtbl data-table">'
      +'<thead><tr><th scope="col">Model</th><th scope="col">Share</th>'
      +'<th scope="col" class="num">API-rate</th><th scope="col" class="num">Billed</th></tr></thead>'
      +'<tbody>'+trs+footer+'</tbody></table></div>';
  }
  function trends(rows){
    const hmx=Math.max(...rows.map(r=>r.total||0),0.0001);
    const bars=rows.map(r=>{
      const bh=Math.max(3,Math.round(90*(r.total||0)/hmx));
      const f=(r.fable||0)>0.005;
      return '<div class="histcol" title="'+esc(r.date)+': '+fmt(r.total||0)+(f?' (incl. Fable)':'')+'">'
        +'<div class="histbar'+(f?' bad':'')+'" style="height:'+bh+'px"></div>'
        +'<div class="histlbl">'+esc((r.date||'').slice(5))+'</div></div>';
    }).join("");
    return '<div style="width:100%"><div class="muted" style="font-size:12px;margin-bottom:8px">'
      +'Daily API-rate total · '+fmt(agg(rows).tot.total)+' over '+rows.length+' days'
      +(rows.length>1 ? ' · '+fmt(agg(rows).tot.total/rows.length)+'/day avg' : '')+'</div>'
      +'<div class="hist" style="height:110px">'+bars+'</div></div>';
  }
  function draw(){
    const rows=rowsForWin();
    drawStatCards(rows);
    $("cp-body").innerHTML = view==="trends" ? trends(rows) : breakdown(rows);
  }
  // URL state: serialize current control state to URL params (defaults omitted).
  function currentStateToParams(){
    var p={};
    if(win==="custom"){
      p.win="custom";
      var f=$("cp-from").value, t=$("cp-to").value;
      if(f) p.from=f;
      if(t) p.to=t;
    } else if(win!==1){
      p.win=String(win);
    }
    // 1d is the default (win omitted); add the day param only when stepped off
    // today, so a shared link to a past day round-trips.
    if(win===1){ var d=(COST_HIST[dayIdx]||{}).date; if(d && dayIdx<COST_HIST.length-1) p.day=d; }
    if(view!=="breakdown") p.view=view;
    return p;
  }
  // URL state: apply a params object to live state + reflect it in the UI.
  // Never calls draw() — caller is responsible.
  function applyUrlState(s){
    var rawWin=s.win;
    if(rawWin==="custom"){ win="custom"; }
    else if(rawWin==="7"){ win=7; }
    else if(rawWin==="30"){ win=30; }
    else if(rawWin==="60"){ win=60; }
    else if(rawWin==="90"){ win=90; }
    else{ win=1; }  // default (1d); a bare ?day=… with no win also lands here
    if(win===1){
      var dayDate=s.day;
      var found=COST_HIST.findIndex(function(r){ return r.date===dayDate; });
      dayIdx=found>=0?found:COST_HIST.length-1;
    }
    view=(s.view==="trends")?"trends":"breakdown";
    [].forEach.call($("cp-window").children,function(c){
      if(c.tagName!=="BUTTON") return;
      c.setAttribute("aria-pressed",c.dataset.d===String(win==="custom"?"custom":win)?"true":"false");
    });
    if(win==="custom"){
      $("cp-range").style.display="flex"; $("cp-daynav").style.display="none";
      if(s.from) $("cp-from").value=s.from;
      if(s.to) $("cp-to").value=s.to;
    } else if(win===1){
      $("cp-range").style.display="none"; $("cp-daynav").style.display="flex";
      updateDayNav();
    } else {
      $("cp-range").style.display="none"; $("cp-daynav").style.display="none";
    }
    [].forEach.call($("cp-view").children,function(c){
      if(c.tagName!=="BUTTON") return;
      var on=c.dataset.v===view;
      c.setAttribute("aria-selected",on?"true":"false");
      c.setAttribute("tabindex",on?"0":"-1");
    });
    $("cp-body").setAttribute("aria-labelledby","cp-tab-"+view);
  }
  $("cp-window").onclick=e=>{const b=e.target.closest(".vbtn");if(!b)return;
    const d=b.dataset.d;
    [].forEach.call($("cp-window").children,c=>{if(c.tagName==="BUTTON")c.setAttribute("aria-pressed",c===b?"true":"false");});
    if(d==="custom"){
      win="custom"; $("cp-range").style.display="flex"; $("cp-daynav").style.display="none";
      if(!$("cp-from").value){
        const last=(COST_HIST[COST_HIST.length-1]||{}).date||"";
        const first=(COST_HIST[Math.max(0,COST_HIST.length-30)]||{}).date||"";
        $("cp-from").value=first; $("cp-to").value=last;
      }
    } else {
      win=+d; $("cp-range").style.display="none";
      if(win===1){ dayIdx=COST_HIST.length-1; $("cp-daynav").style.display="flex"; updateDayNav(); }
      else { $("cp-daynav").style.display="none"; }
    }
    draw(); _ds.writeUrlState(currentStateToParams(),false);};
  $("cp-prev").onclick=()=>{ if(dayIdx>0){ dayIdx--; updateDayNav(); draw(); _ds.writeUrlState(currentStateToParams(),false); } };
  $("cp-next").onclick=()=>{ if(dayIdx<COST_HIST.length-1){ dayIdx++; updateDayNav(); draw(); _ds.writeUrlState(currentStateToParams(),false); } };
  $("cp-from").onchange=()=>{ draw(); _ds.writeUrlState(currentStateToParams(),false); };
  $("cp-to").onchange=()=>{ draw(); _ds.writeUrlState(currentStateToParams(),false); };
  $("cp-view").onclick=e=>{const b=e.target.closest(".vbtn");if(!b)return;
    activateCpTab(b); draw(); _ds.writeUrlState(currentStateToParams(),false);};
  $("cp-view").onkeydown=e=>{
    const btns=[].filter.call($("cp-view").children,c=>c.tagName==="BUTTON");
    const idx=btns.indexOf(e.target); if(idx===-1) return;
    if(e.key==="ArrowRight"||e.key==="ArrowLeft"){
      const next=btns[(idx+(e.key==="ArrowRight"?1:btns.length-1))%btns.length];
      next.focus(); activateCpTab(next); draw(); _ds.writeUrlState(currentStateToParams(),false); e.preventDefault();
    }
  };
  function activateCpTab(b){
    [].forEach.call($("cp-view").children,c=>{if(c.tagName==="BUTTON"){
      const on=c===b; c.setAttribute("aria-selected",on?"true":"false");
      c.setAttribute("tabindex",on?"0":"-1");
    }});
    $("cp-body").setAttribute("aria-labelledby","cp-tab-"+b.dataset.v);
    view=b.dataset.v;
  }
  applyUrlState(_ds.readUrlState());
  _ds.writeUrlState(currentStateToParams(),true);
  _ds.onUrlState(function(s){ applyUrlState(s); draw(); });
  draw();
})();
</script>"""
    return head + script.replace("__HIST__", hist_json)


# ---------------------------------------------------------------------------
# Spending dashboard page CSS (Atelier tokens; no old palette literals)
# ---------------------------------------------------------------------------

_SPENDING_CSS = r"""
/* ---- Page head --------------------------------------------------------- */
.page-head { margin:24px 0 0; }
.page-head h1 { font-size:22px; font-weight:700; letter-spacing:-.01em; color:var(--ink); }
.page-sub { font-size:13px; color:var(--ink-2); margin-top:4px; }
/* Brand-washed pill for the "all sessions" title chip */
.pill-brand { background:var(--brand-wash); color:var(--brand-ink); border-radius:20px;
              padding:2px 9px; font-weight:600; font-size:12px; }

/* ---- Title + controls strip (replaces the old bordered costpanel wrapper) */
.cp-title-row {
  display:flex; align-items:flex-start; justify-content:space-between;
  flex-wrap:wrap; gap:10px; margin:10px 0 12px;
}
.cp-title-left { display:flex; flex-direction:column; gap:3px; }
.cph-inline { font-size:14px; font-weight:650; color:var(--ink); }
.cp-asof { font-size:12px; }
.cp-controls-right { display:flex; flex-direction:column; gap:6px; align-items:flex-end; }
@media (max-width:640px) {
  .cp-title-row { flex-direction:column; }
  .cp-controls-right { align-items:flex-start; }
}

/* ---- Controls ---------------------------------------------------------- */
.cprange { display:flex; gap:6px; align-items:center; }
input.dateinp { background:var(--surface); color:var(--ink); border:1px solid var(--line);
                border-radius:6px; padding:5px 7px; font-size:13px; font-family:inherit; }

/* ---- Segmented control / view-toggle (B-1: raised-white-pill active state) */
.seg { display:inline-flex; border:1px solid var(--line); border-radius:var(--radius);
       overflow:hidden; background:var(--surface-2); padding:2px; gap:2px; }
.vbtn { padding:5px 13px; font-size:13px; font-weight:500; color:var(--ink-2);
        background:transparent; border:none; border-radius:calc(var(--radius) - 2px); cursor:pointer; }
.vbtn[aria-pressed="true"], .vbtn[aria-selected="true"] {
  background:var(--surface); color:var(--brand-ink); box-shadow:var(--shadow); font-weight:650; }
.vbtn:hover:not([aria-pressed="true"]):not([aria-selected="true"]) { background:var(--surface); color:var(--ink); }
.vbtn:focus-visible { outline:2px solid var(--brand); outline-offset:2px; }

/* ---- Hero StatCards grid (N-1) ----------------------------------------- */
.spend-stats { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:0 0 16px; }
@media (max-width:1024px) { .spend-stats { grid-template-columns:repeat(2,1fr); } }
@media (max-width:640px)  { .spend-stats { grid-template-columns:1fr; } }
/* Source-split card inner elements */
.split-bars { display:flex; gap:2px; margin-top:8px; height:7px; border-radius:4px; overflow:hidden; }
.split-bars span { display:block; height:100%; }
.split-legend { display:flex; gap:10px; margin-top:6px; font-size:11.5px; color:var(--ink-2);
                flex-wrap:wrap; }
.split-legend i { display:inline-block; width:7px; height:7px; border-radius:2px;
                  margin-right:4px; vertical-align:middle; }

/* ---- Cost panel -------------------------------------------------------- */
.costpanel { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
             padding:14px 16px; margin-bottom:16px; box-shadow:var(--shadow); }
.cph { font-size:14px; font-weight:650; margin-bottom:8px; color:var(--ink); }
/* Block, not a flex row: `breakdown()` returns the Fable flag and the table as
   siblings, and in a flex row the flag took a column of its own and the table
   shrank to its content width beside it. Stacked, the flag sits above a table
   that spans the card. */
.costgrid { display:block; min-height:120px; }
/* view-toggle section header row */
.cost-detail-head { display:flex; align-items:center; justify-content:space-between;
                    margin-bottom:12px; flex-wrap:wrap; gap:8px; }
.cost-detail-head h2 { font-size:15px; font-weight:650; color:var(--ink); letter-spacing:-.01em; }
.costtbl { width:100%; }
.costtbl td { padding:4px 10px 4px 0; border-bottom:none; color:var(--ink); font-variant-numeric:tabular-nums; }
.costtbl code { font-family:var(--mono); font-size:12.5px; }
.model-sub { font-size:10.5px; margin-top:2px; }

/* ---- Histogram --------------------------------------------------------- */
.hist { display:flex; gap:8px; align-items:flex-end; height:110px; }
.histcol { display:flex; flex-direction:column; align-items:center; gap:4px; cursor:default; }
.histbar { width:22px; border-radius:3px 3px 0 0; background:var(--brand); }
.histbar.bad { background:var(--err); }
.histlbl { font-size:10px; color:var(--ink-2); }

/* ---- Mobile share-bar track (N-6: use --line so it's visible on white cards) */
/* Each row carries TWO share bars: `.bar-track` for the desktop table and
   `.m-share` for the stacked phone card. Exactly one may show at each width.
   Nothing hid either until 2026-09-24, so a phone drew both. */
.costtbl .m-share { display:none; }
.m-share-track { flex:1; height:6px; background:var(--line); border-radius:4px; overflow:hidden; }
.m-share-fill { height:100%; background:var(--brand); border-radius:4px; }
@media (max-width:640px) {
  .costtbl .bar-track { display:none; }
  .costtbl .m-share { display:flex; }
}

/* ---- Subscription-quota line ------------------------------------------- */
.overage-line { font-size:12.5px; margin:0 0 14px; padding:8px 12px;
                background:var(--surface); border:1px solid var(--line);
                border-radius:var(--radius); box-shadow:var(--shadow); }

/* ---- Recorded LLM spend, from the Library's ledger --------------------- */
.ledger { margin:28px 0 0; }
.ledger-h { font-size:17px; font-weight:700; color:var(--ink); }
.ledger-project { background:var(--surface); border:1px solid var(--line);
                  border-radius:var(--radius); box-shadow:var(--shadow);
                  padding:14px 16px; margin:14px 0 0; }
.ledger-project h3 { font-size:15px; font-weight:700; margin:0; }
.ledger-total { font-family:var(--mono); color:var(--brand); margin-left:8px; }
.ledger-dims { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr));
               gap:12px; margin-top:10px; }
.ledger-dim h4 { font-size:11px; color:var(--ink-2); text-transform:uppercase;
                 letter-spacing:.04em; margin:0 0 4px; }
.ledger-dim td:first-child { overflow-wrap:anywhere; }

/* ---- Utility ----------------------------------------------------------- */
.muted { color:var(--ink-2); }
.good { color:var(--ok); }
.warn { color:var(--warn); }
.bad { color:var(--err); }
"""


# ---------------------------------------------------------------------------
# Spending dashboard (/spending)
# ---------------------------------------------------------------------------

# The dimensions a ledger entry carries, in the order the page shows them.
# Person and team are withheld by the Library, and there is no feature field.
_LEDGER_DIMENSIONS = (
    ("epic", "Epic"), ("source", "Source"), ("model", "Model"), ("agent", "Agent"),
    ("item_id", "Work item"), ("run_id", "Run"), ("day", "Day"),
)
_LEDGER_ROWS_SHOWN = 12


def _usd(v: float) -> str:
    return f"${v:,.2f}"


def ledger_groups(entries: list, epic_of: dict) -> dict:
    """Group live (unvoided) entries by project, then by each dimension.

    Returns `{product: {"total": usd, "count": n, "voided": n,
    "groups": {dim: [(value, usd, n), ...]}}}`, each group list sorted by
    spend. A voided entry is counted and left out of every total, matching the
    Library's own ledger total. `epic_of` maps a work item to its epic."""
    out: dict = {}
    for e in entries:
        p = out.setdefault(e.product or "(no project)",
                           {"total": 0.0, "count": 0, "voided": 0, "groups": {}})
        if e.voided_at is not None:
            p["voided"] += 1
            continue
        p["total"] += e.usd
        p["count"] += 1
        for dim, _label in _LEDGER_DIMENSIONS:
            value = epic_of.get(e.item_id) if dim == "epic" else getattr(e, dim)
            bucket = p["groups"].setdefault(dim, {}).setdefault(value or "(none)", [0.0, 0])
            bucket[0] += e.usd
            bucket[1] += 1
    for p in out.values():
        p["groups"] = {dim: sorted(((v, t, n) for v, (t, n) in g.items()),
                                   key=lambda r: (-r[1], r[0]))
                       for dim, g in p["groups"].items()}
    return out


def ledger_section_html(entries: list, ledgers: list, epic_of: dict) -> str:
    """The per-project spend section, from the Library's ledger. An empty
    ledger says nothing has been recorded rather than showing $0, since no app
    reports spend to it yet and $0 would read as nothing spent."""
    head = ('<h2 class="ledger-h">Recorded LLM spend by project</h2>'
            '<p class="page-sub">From the Library\'s spend ledger, every entry an app has '
            'reported, voided ones excluded. Person and team are not shown: the Library '
            'does not serve them.</p>')
    if not entries:
        return f'<section class="ledger">{head}{ds.empty_state("No spend recorded yet.")}</section>'
    caps = {lg.scope_id: lg for lg in ledgers if lg.scope_kind == "project"}
    parts = [head]
    groups = ledger_groups(entries, epic_of)
    for product in sorted(groups, key=lambda k: (-groups[k]["total"], k)):
        g = groups[product]
        meta = [f'{g["count"]} entries']
        if g["voided"]:
            meta.append(f'{g["voided"]} voided, not counted')
        lg = caps.get(product)
        if lg is not None:
            cap = f' of {_usd(lg.hard_cap_usd)} hard cap' if lg.hard_cap_usd is not None else ""
            meta.append(f'{_usd(lg.total_usd)} today ({escape(lg.day)}){cap}')
        caps_form = ds.ledger_caps_form_html(product, {
            "warn_usd": lg.warn_usd if lg else None,
            "soft_cap_usd": lg.soft_cap_usd if lg else None,
            "hard_cap_usd": lg.hard_cap_usd if lg else None,
            "per_run_cap_usd": lg.per_run_cap_usd if lg else None,
        })
        tables = []
        for dim, label in _LEDGER_DIMENSIONS:
            rows = g["groups"].get(dim, [])
            shown, rest = rows[:_LEDGER_ROWS_SHOWN], rows[_LEDGER_ROWS_SHOWN:]
            trs = "".join(
                f'<tr><td>{escape(str(v))}</td><td class="num">{n}</td>'
                f'<td class="num">{_usd(t)}</td></tr>' for v, t, n in shown)
            if rest:
                trs += (f'<tr class="muted"><td>{len(rest)} more</td>'
                        f'<td class="num">{sum(n for _, _, n in rest)}</td>'
                        f'<td class="num">{_usd(sum(t for _, t, _ in rest))}</td></tr>')
            tables.append(
                f'<div class="ledger-dim"><h4>{label}</h4>'
                '<div class="data-table-wrap"><table class="costtbl data-table">'
                f'<thead><tr><th scope="col">{label}</th><th scope="col" class="num">Entries</th>'
                f'<th scope="col" class="num">Spend</th></tr></thead><tbody>{trs}</tbody>'
                '</table></div></div>')
        parts.append(
            f'<div class="ledger-project" data-product="{escape(product, quote=True)}">'
            f'<h3>{escape(product)} <span class="ledger-total">{_usd(g["total"])}</span></h3>'
            f'<p class="muted">{" · ".join(meta)}</p>'
            f'{caps_form}'
            f'<div class="ledger-dims">{"".join(tables)}</div></div>')
    # ONE SCRIPT, NOT ONE PER PROJECT (ds.ledger_caps_form_html's own
    # docstring): every `.ledger-caps` section on the page shares it, so a
    # project's own Save button fires once per click whether the page holds
    # one project or twenty.
    parts.append(f'<script>{ds.LEDGER_CAPS_WRITE_JS}</script>')
    return f'<section class="ledger">{"".join(parts)}</section>'


LEDGER_CORRECTIONS = "Corrections"


def _ledger_section() -> str:
    """Read the ledger live, per request, and render the section. Work items
    are read only when an entry names one, to find each item's epic. A
    correction, whose parent is null or its project's corrections queue,
    groups under "Corrections" rather than a raw queue id."""
    entries = library_client.list_ledger_entries()
    epic_of: dict = {}
    if any(e.item_id for e in entries):
        items = library_client.list_issue_items()
        epic_ids = {it.id for it in items if it.type == ISSUE_TYPE_EPIC}
        queue_ids = library_client.corrections_queue_ids(
            {it.parent_id for it in items
             if it.type != ISSUE_TYPE_EPIC and it.parent_id and it.parent_id not in epic_ids})
        for item in items:
            if item.type == ISSUE_TYPE_EPIC:
                epic_of[item.id] = item.id
            elif not item.parent_id or item.parent_id in queue_ids:
                epic_of[item.id] = LEDGER_CORRECTIONS
            else:
                epic_of[item.id] = item.parent_id
    return ledger_section_html(entries, library_client.list_ledgers(), epic_of)


def spending_render(thresholds: dict | None = None, ledger_html: str = "") -> str:
    banner = ds.stalled_banner_html("Spend snapshot", spending_freshness(load_latest_snapshot()))
    body = (
        '<div class="page-head">'
        '<h1>Spending <span class="pill-brand">all sessions</span></h1>'
        '<p class="page-sub">TOTAL agent + app spend across every session, every model — interactive '
        '<code>co</code> sessions included (ARCH-136). Pick a window or a custom range; '
        'hover ⓘ for what each number means.</p></div>'
        + banner
        + cost_panel_html(thresholds)
        + ledger_html
    )
    return ds.page_shell(
        title="spending",
        nav_html=ds.nav_component("/spending", "full"),
        body=body,
        extra_css=_SPENDING_CSS,
        auto_refresh_seconds=ds.SPEND_REFRESH_SECONDS,
    )


def spending_live_html() -> str:
    """Render the Spending dashboard from current data at request time
    (ARCH-182). Fetches the live overage threshold fresh on every call (no
    cache) so a write made from this page's own form reaches both the
    overage-line coloring and the form's displayed value on the next request.

    The ledger section is read here and not in `spending_render`, because the
    scheduled `build()` also calls that to write the static fallback, and a
    fallback that needs the Library cannot stand in when the Library is down."""
    return spending_render(alert_policy_store.get("spending.overage_runs"),
                           ledger_html=_ledger_section())


# ---------------------------------------------------------------------------
# Cost-Performance dashboard page CSS (Atelier tokens; no old palette literals)
# ---------------------------------------------------------------------------

_COSTPERF_CSS = r"""
/* ---- Page head --------------------------------------------------------- */
.page-head { margin:24px 0 0; }
.page-head h1 { font-size:22px; font-weight:700; letter-spacing:-.01em; color:var(--ink); }
.page-sub { font-size:13px; color:var(--ink-2); margin-top:4px; }

/* ---- Filters ----------------------------------------------------------- */
.filters { display:flex; flex-wrap:wrap; gap:14px; align-items:flex-end;
           background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
           padding:14px 16px; margin-bottom:16px; box-shadow:var(--shadow); }
.fg { display:flex; flex-direction:column; gap:5px; }
.fg label { font-size:11px; color:var(--ink-2); text-transform:uppercase; letter-spacing:.04em; }
select { background:var(--surface); color:var(--ink); border:1px solid var(--line);
         border-radius:6px; padding:6px 8px; font-size:13px; font-family:inherit; }
.chips { display:flex; flex-wrap:wrap; gap:6px; padding:5px;
         background:var(--surface); border:1px solid var(--line); border-radius:6px; max-width:520px; }
.chip { padding:3px 9px; border-radius:20px; border:1px solid var(--line);
        background:var(--surface-2); color:var(--ink-2); cursor:pointer; font-size:12px;
        user-select:none; font-family:inherit; }
.chip.on, .chip[aria-pressed="true"] { background:var(--brand-wash); border-color:color-mix(in srgb, var(--brand) 40%, var(--line)); color:var(--brand-ink); font-weight:650; }
.chip:focus-visible { outline:2px solid var(--brand); outline-offset:2px; }
.cprange { display:flex; gap:6px; align-items:center; }
input.dateinp { background:var(--surface); color:var(--ink); border:1px solid var(--line);
                border-radius:6px; padding:5px 7px; font-size:13px; font-family:inherit; }

/* ---- Segmented view toggle (B-1: raised-white-pill active state) -------- */
.seg { display:inline-flex; border:1px solid var(--line); border-radius:var(--radius);
       overflow:hidden; background:var(--surface-2); padding:2px; gap:2px; }
.vbtn { padding:5px 13px; font-size:13px; font-weight:500; color:var(--ink-2);
        background:transparent; border:none; border-radius:calc(var(--radius) - 2px); cursor:pointer; }
.vbtn[aria-pressed="true"], .vbtn[aria-selected="true"] {
  background:var(--surface); color:var(--brand-ink); box-shadow:var(--shadow); font-weight:650; }
.vbtn:hover:not([aria-pressed="true"]):not([aria-selected="true"]) { background:var(--surface); color:var(--ink); }
.vbtn:focus-visible { outline:2px solid var(--brand); outline-offset:2px; }

/* ---- Summary statcards grid -------------------------------------------- */
.statcards { display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr));
             gap:12px; margin-bottom:16px; }

/* ---- Scope note -------------------------------------------------------- */
.scopenote { color:var(--ink-2); font-size:12px; line-height:1.5; margin:0 0 10px;
             padding:8px 10px; border-left:2px solid var(--brand);
             background:var(--brand-wash); border-radius:4px; }

/* ---- Table decorations ------------------------------------------------- */
th.tip { cursor:pointer; }
.tipr::after { left:auto; right:0; transform:none; }

/* ---- Trend sparklines -------------------------------------------------- */
.trendcards { display:flex; flex-wrap:wrap; gap:12px; }
.trendcard { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
             padding:12px 14px; min-width:170px; box-shadow:var(--shadow); }
.trendcard .l { font-size:11px; color:var(--ink-2); text-transform:uppercase; letter-spacing:.04em; }
.trendcard .tv { font-size:20px; font-weight:700; margin:2px 0 6px; font-variant-numeric:tabular-nums; }
svg.spark { display:block; }

/* ---- Color signals ----------------------------------------------------- */
.muted { color:var(--ink-2); }
.good { color:var(--ok); }
.warn { color:var(--warn); }
.bad { color:var(--err); }
.empty { color:var(--ink-2); padding:30px; text-align:center; }
"""


# ---------------------------------------------------------------------------
# Cost-Performance dashboard (/cost-performance)
# ---------------------------------------------------------------------------

_SCORECARD_BODY = r"""
  <div class="cph">Cost vs quality — coding agents</div>
  <div class="scopenote">Each row is an agent's <b>cost-vs-quality ratio on code-related tasks</b> — <code>coder</code>, <code>refactorer</code>, <code>bug-fixer</code>, plus Hollis PRs. These land as pull requests, so their quality is observable (merge&nbsp;%, 7-day survival, CI-first-pass); agents without that signal (reviewers, planners, the orchestrator) are out of scope. Autonomous dev-crew runs only — interactive <code>co</code> sessions (Opus) never enter here; for those see the <b>Spending</b> dashboard. Full definitions: the <a href="/">Cost vs quality metrics</a> reference on this site.</div>
  <div class="filters">
    <div class="fg"><label class="tip" tabindex="0" data-tip="Keeps records whose run/PR date falls inside the window. Each record carries the date its run or PR was logged to the scorecard ledger.">Window</label>
      <select id="window">
        <option value="7">Last 7 days</option>
        <option value="14">Last 14 days</option>
        <option value="30" selected>Last 30 days</option>
        <option value="9999">All time</option>
        <option value="custom">Custom range…</option>
      </select></div>
    <div class="fg" id="sc-range-fg" style="display:none">
      <label class="tip" tabindex="0" data-tip="Inclusive date range over each record's run/PR ledger date. Leave a side blank for open-ended.">Custom range</label>
      <div class="cprange"><input type="date" id="sc-from" class="dateinp"><span class="muted">→</span><input type="date" id="sc-to" class="dateinp"></div></div>
    <div class="fg"><label class="tip" tabindex="0" data-tip="The dimension each table row aggregates over: the agent that ran, the model that served it, the provider, the product repo, agent × model, or the Monday-anchored calendar week.">Group by</label>
      <select id="groupby">
        <option value="agent">Agent</option>
        <option value="model">Model</option>
        <option value="provider">Provider</option>
        <option value="product">Product</option>
        <option value="agent|model">Agent × Model</option>
        <option value="week">Week</option>
      </select></div>
    <div class="fg"><label class="tip" tabindex="0" data-tip="The repo the run or PR targeted. Click chips to filter; none selected = all.">Product</label><div class="chips" id="f-product"></div></div>
    <div class="fg"><label class="tip" tabindex="0" data-tip="Model provider, inferred from the model id (deepseek/… → deepseek, claude-* → anthropic). Click chips to filter.">Provider</label><div class="chips" id="f-provider"></div></div>
    <div class="fg"><label>View</label>
      <div class="seg" id="view" role="tablist" aria-label="Cost-performance view">
        <button type="button" class="vbtn" data-v="table" aria-selected="true" role="tab" id="view-tab-table" aria-controls="tbl" tabindex="0">Table</button>
        <button type="button" class="vbtn" data-v="trends" aria-selected="false" role="tab" id="view-tab-trends" aria-controls="trendwrap" tabindex="-1">Trends</button>
      </div></div>
  </div>
  <div class="statcards" id="cards"></div>
  <div class="scopenote" id="modelnote" style="display:none">This table covers <b>autonomous dev-crew runs + Hollis PRs only</b> — interactive <code>co</code> sessions (which run on Opus) never enter it. For total spend including interactive Opus, see the <b>Spending</b> dashboard. The <b>"—"</b> row is Hollis PRs, whose ledger records no model yet.</div>
  <div class="data-table-wrap"><table class="data-table" id="tbl" role="tabpanel" aria-labelledby="view-tab-table"><thead></thead><tbody></tbody></table></div>
  <div class="empty" id="empty" style="display:none">No records match these filters.</div>
  <div id="trendwrap" role="tabpanel" aria-labelledby="view-tab-trends" tabindex="0" style="display:none"></div>
"""

_SCORECARD_SCRIPT = r"""<script>
const FACTS = __DATA__;
const state = { window:30, groupby:"agent", product:new Set(), provider:new Set(), view:"table" };

const uniq = (key) => [...new Set(FACTS.map(f=>f[key]))].filter(Boolean).sort();
const todayISO = new Date().toISOString().slice(0,10);
function withinWindow(f){
  if(state.window==="custom"){
    if(!f.date) return false;
    const from=document.getElementById("sc-from").value, to=document.getElementById("sc-to").value;
    return (!from||f.date>=from)&&(!to||f.date<=to);
  }
  if(state.window>=9999) return true;
  if(!f.date) return false;
  const cutoff = new Date(Date.now()-state.window*86400000).toISOString().slice(0,10);
  return f.date >= cutoff;
}
function passFilters(f){
  if(!withinWindow(f)) return false;
  if(state.product.size && !state.product.has(f.product)) return false;
  if(state.provider.size && !state.provider.has(f.provider)) return false;
  return true;
}
function pct(n,d){ return d? (100*n/d) : null; }
function fmtPct(p){ return p==null? "—" : p.toFixed(0)+"%"; }
function cls(p, good, warn){ if(p==null) return ""; if(p>=good) return "good"; if(p>=warn) return "warn"; return "bad"; }

// API-rate vs Billed split (cost-performance):
// - apiRate (usd) = sum of all cost_usd for the group, regardless of billing source.
// - billed (billedUsd) = sum of cost_usd only for metered rows (billing "metered");
//   subscription rows are covered by the flat plan and contribute $0 here.
function aggregate(rows){
  const groups = {};
  for(const f of rows){
    const key = state.groupby.split("|").map(k=>f[k]??"—").join(" · ");
    const g = groups[key] || (groups[key]={key, runs:0, resolved:0, merged:0,
      ciRuns:0, ci:0, scored:0, survived:0, usd:0, billedUsd:0, hasCost:false,
      rev:0, revN:0, fix:0, fixN:0});
    g.runs++;
    if(f.cost_usd!=null){
      g.usd+=f.cost_usd; g.hasCost=true;
      // metered = real out-of-pocket; subscription and "—" = $0 billed
      if(f.billing==="metered") g.billedUsd+=f.cost_usd;
    }
    if(f.resolved) g.resolved++;
    if(f.merged) g.merged++;
    if(f.ci_first_pass!=null){ g.ciRuns++; if(f.ci_first_pass) g.ci++; }
    if(f.survived!=null){ g.scored++; if(f.survived) g.survived++; }
    if(f.review_score!=null){ g.rev+=f.review_score; g.revN++; }
    if(f.fix_score!=null){ g.fix+=f.fix_score; g.fixN++; }
  }
  return Object.values(groups);
}

// tip = what the metric is + which events feed it (rendered as a hover tooltip
// on the column header; no double quotes inside — they sit in an HTML attribute)
const TIPS = {
  runs:"Records in this group. One record = one scorecard event: an agentctl dev-crew run (vivy / constance / sous-chef, from scorecard.jsonl) or one Hollis GitHub PR (hollis.jsonl).",
  merge:"Merged PRs ÷ records whose PR state is known (open, merged or closed). Runs that produced no traceable PR sit outside the denominator. Green ≥90%, amber ≥70%.",
  surv:"Of merged PRs whose 7-day survival window has elapsed: the share not reverted and with no follow-up bug detected in-window (the ARCH-107 success bar). Fresh merges stay unscored until their window passes.",
  ci:"PRs whose FIRST pull-request CI run on the branch passed, ÷ those where that first run is known. A red first run that needed a fix-up push counts against it. Green ≥80%, amber ≥60%.",
  judge:"Mean LLM-judge scores on sampled runs (judge.jsonl, joined by run id), each 1–5: review thoroughness / fix quality.",
  apiRate:"API-rate cost of the group: every run valued at API list price, from per-run telemetry (cost_usd). This is the full estimate regardless of whether runs billed to the subscription or to metered usage.",
  billed:"Actual out-of-pocket cost: only metered runs (billing=metered, e.g. gateway or direct API). Subscription runs are covered by the flat plan and contribute $0 here.",
  perMerged:"API-rate $ ÷ merged PRs in the group: the estimated cost of one landed change at list price.",
};
const COLS = [
  {k:"key", t:"", num:false, get:g=>g.key},
  {k:"runs", t:"Runs", tip:TIPS.runs, get:g=>g.runs},
  {k:"merge", t:"Merge%", tip:TIPS.merge, get:g=>pct(g.merged,g.resolved), fmt:fmtPct, color:p=>cls(p,90,70)},
  {k:"surv", t:"Survived%", tip:TIPS.surv, get:g=>pct(g.survived,g.scored), fmt:fmtPct, color:p=>cls(p,90,70)},
  {k:"ci", t:"CI-1st%", tip:TIPS.ci, get:g=>pct(g.ci,g.ciRuns), fmt:fmtPct, color:p=>cls(p,80,60)},
  {k:"judge", t:"Judge r/f", tip:TIPS.judge, get:g=>g.revN||g.fixN? g : null,
     fmt:g=> g==null?"—":`${g.revN?(g.rev/g.revN).toFixed(1):"—"}/${g.fixN?(g.fix/g.fixN).toFixed(1):"—"}`},
  {k:"usd", t:"API-rate $", tip:TIPS.apiRate, get:g=>g.hasCost? g.usd : null, fmt:v=>v==null?"—":"$"+v.toFixed(4)},
  {k:"billedUsd", t:"Billed $", tip:TIPS.billed, get:g=>g.hasCost? g.billedUsd : null, fmt:v=>v==null?"—":"$"+v.toFixed(4)},
  {k:"perMerged", t:"$/merged", tip:TIPS.perMerged, get:g=>(g.hasCost&&g.merged)? g.usd/g.merged : null,
     fmt:v=>v==null?"—":"$"+v.toFixed(4)},
];
let sortKey="usd", sortDir=-1;

function renderCards(rows){
  const tot = aggregate(rows).reduce((a,g)=>{a.runs+=g.runs;a.resolved+=g.resolved;a.merged+=g.merged;
    a.scored+=g.scored;a.survived+=g.survived;a.ciRuns+=g.ciRuns;a.ci+=g.ci;a.usd+=g.usd;a.billedUsd+=g.billedUsd;return a;},
    {runs:0,resolved:0,merged:0,scored:0,survived:0,ciRuns:0,ci:0,usd:0,billedUsd:0});
  const billedPct=tot.usd>0?Math.round(100*tot.billedUsd/tot.usd):0;
  const cards = [
    ["Records", rows.length, "", TIPS.runs],
    ["Merge%", fmtPct(pct(tot.merged,tot.resolved)), cls(pct(tot.merged,tot.resolved),90,70), TIPS.merge],
    ["Survived%", fmtPct(pct(tot.survived,tot.scored)), cls(pct(tot.survived,tot.scored),90,70), TIPS.surv],
    ["CI-1st%", fmtPct(pct(tot.ci,tot.ciRuns)), cls(pct(tot.ci,tot.ciRuns),80,60), TIPS.ci],
    ["API-rate $", "$"+tot.usd.toFixed(4), "", TIPS.apiRate],
    ["Billed $", "$"+tot.billedUsd.toFixed(4)+(tot.usd>0?" ("+billedPct+"% of API-rate)":""), "", TIPS.billed],
  ];
  document.getElementById("cards").innerHTML = cards.map(([l,v,c,tip])=>{
    const glyph = c==="good"?"✓ ":c==="warn"?"△ ":c==="bad"?"✕ ":"";
    return `<article class="statcard"><div class="statcard-value num ${c}">${glyph}${v}</div><div class="statcard-label">`
    +(tip?`<span class="tip" tabindex="0" data-tip="${tip}">${l}</span>`:l)+`</div></article>`;
  }).join("");
}

function renderTable(rows){
  let groups = aggregate(rows);
  const col = COLS.find(c=>c.k===sortKey);
  groups.sort((a,b)=>{ let av=col.get(a), bv=col.get(b);
    av = (av==null||typeof av==="object")? (sortDir<0?-Infinity:Infinity):av;
    bv = (bv==null||typeof bv==="object")? (sortDir<0?-Infinity:Infinity):bv;
    if(typeof av==="string") return sortDir*av.localeCompare(bv);
    return sortDir*(av-bv); });

  const maxUsd = Math.max(1e-9,...groups.map(g=>g.hasCost?g.usd:0));
  document.querySelector("#tbl thead").innerHTML = "<tr>"+COLS.map((c,i)=>
    `<th data-k="${c.k}" tabindex="0"${c.tip?` class="tip${i>=COLS.length-2?" tipr":""}" data-tip="${c.tip}"`:""}>`
    +`${c.t}${sortKey===c.k?(sortDir<0?" ▾":" ▴"):""}</th>`).join("")+"</tr>";
  document.querySelector("#tbl tbody").innerHTML = groups.map(g=>"<tr>"+COLS.map(c=>{
    let raw=c.get(g), txt=c.fmt? c.fmt(raw): (raw==null?"—":raw);
    let color = c.color&&typeof raw==="number"? c.color(raw):"";
    if(c.k==="key"){
      const bpct = g.hasCost? Math.round(100*g.usd/maxUsd):0;
      return `<td data-label="Group" class="k">${g.key} ${bpct?'<div class="bar-track" style="max-width:80px;display:inline-block;vertical-align:middle"><div class="bar-fill" style="width:'+bpct+'%"></div></div>':""}</td>`;
    }
    const glyph = color==="good"?"✓ ":color==="warn"?"△ ":color==="bad"?"✕ ":"";
    return `<td data-label="${c.t}" class="${color}">${glyph}${txt}</td>`;
  }).join("")+"</tr>").join("");
  document.getElementById("tbl").style.display = groups.length? "":"none";
  document.getElementById("empty").style.display = groups.length? "none":"";
}

// inline-SVG sparkline (nulls = gaps, kept positionally so week spacing stays
// even); line tint = up/down vs the first week. No charting lib — self-contained.
function spark(values){
  const w=170,h=42,pad=4;
  const pts=values.map((v,i)=>[i,v]).filter(([,v])=>v!=null&&!isNaN(v));
  if(pts.length<2) return `<svg class="spark" width="${w}" height="${h}"></svg>`;
  const ys=pts.map(([,v])=>v), mn=Math.min(...ys), mx=Math.max(...ys), span=(mx-mn)||1, n=values.length;
  const X=i=> pad+(n<2?0:(i/(n-1))*(w-2*pad));
  const Y=v=> (h-pad)-((v-mn)/span)*(h-2*pad);
  let d=""; pts.forEach(([i,v],k)=>{ d+=(k?"L":"M")+X(i).toFixed(1)+" "+Y(v).toFixed(1)+" "; });
  const [li,lv]=pts[pts.length-1];
  const trend=ys[ys.length-1]-ys[0];
  const col = trend>0?"var(--ok)":trend<0?"var(--err)":"var(--brand)";
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">`
    +`<path d="${d}" fill="none" stroke="${col}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>`
    +`<circle cx="${X(li).toFixed(1)}" cy="${Y(lv).toFixed(1)}" r="2.5" fill="${col}"/></svg>`;
}

function renderTrends(rows){
  const wrap = document.getElementById("trendwrap");
  const weeks = [...new Set(rows.map(f=>f.week))].filter(Boolean).sort();
  if(weeks.length < 2){
    wrap.innerHTML = `<div class="empty">Weekly trends need ≥2 weeks of data in the current window `
      + `(have ${weeks.length}). Widen the window, or trends fill in as more weeks land.</div>`;
    return;
  }
  const agg={}; weeks.forEach(w=>agg[w]={runs:0,resolved:0,merged:0,scored:0,survived:0,ciRuns:0,ci:0,usd:0,billedUsd:0,hasCost:false});
  for(const f of rows){ if(!f.week || !(f.week in agg)) continue; const a=agg[f.week];
    a.runs++; if(f.cost_usd!=null){a.usd+=f.cost_usd; a.hasCost=true; if(f.billing==="metered") a.billedUsd+=f.cost_usd;}
    if(f.resolved)a.resolved++; if(f.merged)a.merged++;
    if(f.ci_first_pass!=null){a.ciRuns++; if(f.ci_first_pass)a.ci++;}
    if(f.survived!=null){a.scored++; if(f.survived)a.survived++;} }
  const S=weeks.map(w=>agg[w]);
  const panels=[
    {t:"Runs",        tip:TIPS.runs,      vals:S.map(a=>a.runs),                                     fmt:v=>v==null?"—":v},
    {t:"Merge%",      tip:TIPS.merge,     vals:S.map(a=>pct(a.merged,a.resolved)),                   fmt:fmtPct},
    {t:"Survived%",   tip:TIPS.surv,      vals:S.map(a=>pct(a.survived,a.scored)),                   fmt:fmtPct},
    {t:"CI-1st%",     tip:TIPS.ci,        vals:S.map(a=>pct(a.ci,a.ciRuns)),                         fmt:fmtPct},
    {t:"API-rate $",  tip:TIPS.apiRate,   vals:S.map(a=>a.hasCost?a.usd:null),                       fmt:v=>v==null?"—":"$"+v.toFixed(4)},
    {t:"Billed $",    tip:TIPS.billed,    vals:S.map(a=>a.hasCost?a.billedUsd:null),                 fmt:v=>v==null?"—":"$"+v.toFixed(4)},
    {t:"$/merged (API-rate)", tip:TIPS.perMerged, vals:S.map(a=>(a.hasCost&&a.merged)?a.usd/a.merged:null), fmt:v=>v==null?"—":"$"+v.toFixed(4)},
  ];
  wrap.innerHTML =
    `<div class="sub" style="margin-bottom:12px">Weekly trend over ${weeks.length} weeks `
    + `(${weeks[0]} → ${weeks[weeks.length-1]}), current filters applied; dot = latest; `
    + `green/red = up/down vs the first week.</div>`
    + `<div class="trendcards">`+panels.map(p=>{
        const last=[...p.vals].reverse().find(v=>v!=null);
        return `<div class="trendcard"><div class="l">`
          + (p.tip?`<span class="tip" tabindex="0" data-tip="Per Monday-anchored week: ${p.tip}">${p.t}</span>`:p.t)
          + `</div><div class="tv">${p.fmt(last==null?null:last)}</div>${spark(p.vals)}</div>`;
      }).join("")+`</div>`;
}

function draw(){
  const rows = FACTS.filter(passFilters);
  renderCards(rows);
  const t = state.view==="trends";
  document.getElementById("tbl").style.display = (t||!rows.length)? "none":"";
  document.getElementById("empty").style.display = (!t && !rows.length)? "":"none";
  document.getElementById("modelnote").style.display = (!t && state.groupby==="model")? "":"none";
  document.getElementById("trendwrap").style.display = t? "":"none";
  if(t) renderTrends(rows); else renderTable(rows);
}

function chipBox(id, key){
  const box=document.getElementById(id);
  box.innerHTML = uniq(key).map(v=>`<button type="button" class="chip" data-v="${v}" aria-pressed="false">${v}</button>`).join("");
  box.onclick = e=>{ const c=e.target.closest(".chip"); if(!c) return;
    const v=c.dataset.v, set=state[key]; set.has(v)?set.delete(v):set.add(v);
    const on=set.has(v); c.classList.toggle("on",on); c.setAttribute("aria-pressed",on?"true":"false");
    draw(); _ds.writeUrlState(currentStateToParams(),false); };
}
// URL state: serialize current control state to URL params (defaults omitted).
function currentStateToParams(){
  var p={};
  if(state.window!==30){
    p.win=state.window===9999?"all":String(state.window==="custom"?"custom":state.window);
    if(state.window==="custom"){
      var f=document.getElementById("sc-from").value, t=document.getElementById("sc-to").value;
      if(f) p.from=f;
      if(t) p.to=t;
    }
  }
  if(state.groupby!=="agent") p.group=state.groupby;
  var prod=[...state.product].sort().join(",");
  if(prod) p.product=prod;
  var prov=[...state.provider].sort().join(",");
  if(prov) p.provider=prov;
  if(state.view!=="table") p.view=state.view;
  if(sortKey!=="usd") p.sort=sortKey;
  if(sortDir!==-1) p.dir=String(sortDir);
  return p;
}
// URL state: apply a params object to live state + reflect selects/tabs.
// Never calls draw() — caller is responsible.
function applyUrlState(s){
  var rawWin=s.win;
  if(rawWin==="custom"){ state.window="custom"; }
  else if(rawWin==="all"){ state.window=9999; }
  else if(rawWin==="7"){ state.window=7; }
  else if(rawWin==="14"){ state.window=14; }
  else{ state.window=30; }
  if(state.window==="custom"){
    document.getElementById("sc-range-fg").style.display="";
    if(s.from) document.getElementById("sc-from").value=s.from;
    if(s.to) document.getElementById("sc-to").value=s.to;
  } else {
    document.getElementById("sc-range-fg").style.display="none";
  }
  var sel=document.getElementById("window");
  sel.value=state.window===9999?"9999":String(state.window==="custom"?"custom":state.window);
  state.groupby=s.group||"agent";
  document.getElementById("groupby").value=state.groupby;
  state.product=new Set(s.product?s.product.split(",").filter(Boolean):[]);
  state.provider=new Set(s.provider?s.provider.split(",").filter(Boolean):[]);
  state.view=(s.view==="trends")?"trends":"table";
  // Validate sort against the known columns: an unknown key from a crafted/stale
  // URL would make COLS.find() return undefined and crash renderTable.
  sortKey=COLS.some(function(c){return c.k===s.sort;})?s.sort:"usd";
  sortDir=s.dir==="1"?1:-1;
  document.querySelectorAll("#view .vbtn").forEach(function(x){
    var on=x.dataset.v===state.view;
    x.setAttribute("aria-selected",on?"true":"false");
    x.setAttribute("tabindex",on?"0":"-1");
  });
}
// Reflect product/provider Sets in chip DOM after applyUrlState.
function reflectChips(){
  document.querySelectorAll("#f-product .chip").forEach(function(c){
    var on=state.product.has(c.dataset.v);
    c.classList.toggle("on",on); c.setAttribute("aria-pressed",on?"true":"false");
  });
  document.querySelectorAll("#f-provider .chip").forEach(function(c){
    var on=state.provider.has(c.dataset.v);
    c.classList.toggle("on",on); c.setAttribute("aria-pressed",on?"true":"false");
  });
}
document.getElementById("window").onchange=e=>{
  const v=e.target.value, fg=document.getElementById("sc-range-fg");
  if(v==="custom"){
    state.window="custom"; fg.style.display="";
    const from=document.getElementById("sc-from"), to=document.getElementById("sc-to");
    if(!from.value){
      const dates=FACTS.map(f=>f.date).filter(Boolean).sort();
      to.value = dates[dates.length-1]||todayISO;
      from.value = new Date(Date.now()-30*86400000).toISOString().slice(0,10);
    }
  } else { state.window=+v; fg.style.display="none"; }
  draw(); _ds.writeUrlState(currentStateToParams(),false);};
document.getElementById("sc-from").onchange=()=>{ draw(); _ds.writeUrlState(currentStateToParams(),false); };
document.getElementById("sc-to").onchange=()=>{ draw(); _ds.writeUrlState(currentStateToParams(),false); };
document.getElementById("groupby").onchange=e=>{state.groupby=e.target.value; draw(); _ds.writeUrlState(currentStateToParams(),false);};
function activateViewTab(b){
  document.querySelectorAll("#view .vbtn").forEach(x=>{
    const on=x===b; x.setAttribute("aria-selected",on?"true":"false");
    x.setAttribute("tabindex",on?"0":"-1");
  });
  state.view=b.dataset.v;
}
document.getElementById("view").onclick=e=>{const b=e.target.closest(".vbtn"); if(!b)return;
  activateViewTab(b); draw(); _ds.writeUrlState(currentStateToParams(),false);};
document.getElementById("view").onkeydown=e=>{
  const btns=[...document.querySelectorAll("#view .vbtn")];
  const idx=btns.indexOf(e.target); if(idx===-1) return;
  if(e.key==="ArrowRight"||e.key==="ArrowLeft"){
    const next=btns[(idx+(e.key==="ArrowRight"?1:btns.length-1))%btns.length];
    next.focus(); activateViewTab(next); draw(); _ds.writeUrlState(currentStateToParams(),false); e.preventDefault();
  }
};
document.querySelector("#tbl thead").addEventListener("click", e=>{
  const th=e.target.closest("th"); if(!th) return;
  const k=th.dataset.k; if(k===sortKey) sortDir*=-1; else {sortKey=k; sortDir=-1;}
  renderTable(FACTS.filter(passFilters)); _ds.writeUrlState(currentStateToParams(),false);
});
document.querySelector("#tbl thead").addEventListener("keydown", e=>{
  if(e.key!=="Enter"&&e.key!==" ") return;
  const th=e.target.closest("th"); if(!th) return;
  e.preventDefault();
  const k=th.dataset.k; if(k===sortKey) sortDir*=-1; else {sortKey=k; sortDir=-1;}
  renderTable(FACTS.filter(passFilters)); _ds.writeUrlState(currentStateToParams(),false);
});
chipBox("f-product","product"); chipBox("f-provider","provider");
applyUrlState(_ds.readUrlState());
reflectChips();
_ds.writeUrlState(currentStateToParams(),true);
draw();
_ds.onUrlState(function(s){ applyUrlState(s); reflectChips(); draw(); });
</script>"""


# ---------------------------------------------------------------------------
# Cost per outcome (View A) — one PR per row, cost summed across its dispatch
# chain. Appended below the cost-vs-quality scorecard. Self-contained: its JS is
# wrapped in an IIFE so its identifiers never collide with the scorecard script
# that shares this page's global scope.
# ---------------------------------------------------------------------------

_OUTCOMES_BODY = r"""
  <style>
  .cph2 { font-size:13px; font-weight:650; margin:14px 0 6px; color:var(--ink); }
  .oc-ex { width:24px; height:24px; padding:0; line-height:1; border-radius:6px;
    border:1px solid var(--line); background:var(--surface-2); color:var(--ink-2);
    cursor:pointer; font-size:12px; }
  .oc-ex:hover { border-color:var(--brand); color:var(--brand-ink); }
  .oc-ex.on { background:var(--brand-wash); border-color:var(--brand); color:var(--brand-ink); }
  .oc-ex:focus-visible { outline:2px solid var(--brand); outline-offset:1px; }
  #oc-tbl td.oc-excell { width:32px; text-align:center; }
  #oc-tbl tr.oc-excluded-row td:not(.oc-excell) { opacity:.4; text-decoration:line-through; }
  .oc-linkbtn { background:none; border:none; padding:0; color:var(--brand-ink);
    font:inherit; cursor:pointer; text-decoration:underline; }
  #oc-rollup tbody tr:first-child td { font-weight:650; }
  </style>
  <hr class="section-rule">
  <div class="cph">Cost per outcome — dispatch-chain-joined</div>
  <div class="scopenote">Each row is a <b>pull request</b>, with the cost summed across the dispatch chain that produced it (the producing agent + anything it spawned). Complements the per-agent ratios above: this is the per-PR economics (ARCH-191/239). <b>Note:</b> reviewers/security-reviewers are dispatched as the orchestrator's siblings, so their cost sits in the session total (see the <a href="/activity">Activity</a> log), not folded into a PR here; and merge status isn't joined yet (this counts PRs <i>opened</i>).</div>
  <div class="filters">
    <div class="fg"><label>Window</label>
      <select id="oc-window">
        <option value="7">Last 7 days</option>
        <option value="30" selected>Last 30 days</option>
        <option value="90">Last 90 days</option>
        <option value="9999">All loaded</option>
      </select></div>
    <div class="fg"><label>Product</label><div class="chips" id="oc-product"></div></div>
  </div>
  <div class="statcards" id="oc-cards"></div>
  <div class="cph2">Cost by <span id="oc-groupby-label">model</span> — recomputes as you exclude runs below</div>
  <div class="filters">
    <div class="fg"><label>Group by</label>
      <select id="oc-groupby">
        <option value="model" selected>Model</option>
        <option value="agent">Agent</option>
        <option value="model_agent">Model + Agent</option>
      </select></div>
  </div>
  <div class="data-table-wrap"><table class="data-table" id="oc-rollup"><thead></thead><tbody></tbody></table></div>
  <div class="scopenote" id="oc-excluded" style="display:none"></div>
  <div class="scopenote" style="margin-top:6px">Per-PR detail. Click <b>✕</b> to exclude a run from the totals above (e.g. a one-off runaway, to see the model's economics without it); click <b>↩</b> to restore. Click any column header to sort; click again to reverse.</div>
  <div class="data-table-wrap"><table class="data-table" id="oc-tbl"><thead></thead><tbody></tbody></table></div>
  <div class="empty" id="oc-empty" style="display:none">No PRs in this window.</div>
"""

_OUTCOMES_SCRIPT = r"""<script>
(function(){
  const SNAP = __ODATA__;
  const ROWS = (SNAP.outcomes)||[];
  // `excluded` holds node_run_ids the viewer has temporarily pulled from the
  // aggregates (ARCH-245) — e.g. a one-off runaway, to see a model/agent's real
  // economics without it. Client-only, resets on reload; never mutates the feed.
  const st = { window:30, product:new Set(), excluded:new Set(), groupBy:"model" };
  let sortKey="chain_usd", sortDir=-1;   // default: most expensive chain first
  const fmtUSD = (n)=>"$"+(n||0).toFixed(2);
  const esc = (s)=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  // America/Phoenix, not America/Denver — Convivy is on fixed UTC-7 Mountain
  // STANDARD Time, and Denver shifts to UTC-6 in summer. Matches orient_time.MST.
  const localDate = (iso)=>{ try{ return new Date(iso).toLocaleDateString("en-CA",{timeZone:"America/Phoenix"}); }catch(e){ return (iso||"").slice(0,10); } };
  const uniqProducts = [...new Set(ROWS.map(r=>r.product))].filter(Boolean).sort();
  // Stable per-row id for exclusion: the producing node's run-id (unique per PR);
  // fall back to product#pr when a row predates node_run_id.
  const rid = (r)=> r.node_run_id || (String(r.product)+"#"+String(r.pr_number));

  // Every column is sortable both directions. `get` returns the sort value
  // (string → localeCompare, number → numeric); `cell` renders the HTML.
  const COLS = [
    {k:"pr",        t:"PR",               get:r=>+r.pr_number||0,
       cell:r=> r.pr_url ? '<a href="'+esc(r.pr_url)+'" target="_blank" rel="noopener">#'+esc(r.pr_number)+'</a>' : ("#"+esc(r.pr_number))},
    {k:"product",   t:"Product",          get:r=>r.product||"", cell:r=>esc(r.product)},
    {k:"agent",     t:"Agent",            get:r=>r.agent||"",   cell:r=>esc(r.agent)},
    {k:"model",     t:"Model",            get:r=>r.model||"",   cell:r=>esc(r.model), cls:"muted"},
    {k:"date",      t:"Date",             get:r=>r.date||"",    cell:r=>esc(localDate(r.date))},
    {k:"chain_usd", t:"Chain $",          get:r=>r.chain_usd||0, cell:r=>fmtUSD(r.chain_usd), num:true},
    {k:"node_usd",  t:"Producing node $", get:r=>r.node_usd||0,  cell:r=>fmtUSD(r.node_usd),  num:true},
  ];

  function within(r){
    if(st.window===9999) return true;
    const cutoff = new Date(Date.now() - (+st.window)*86400000);
    return new Date(r.date) >= cutoff;
  }
  function pass(r){ return (!st.product.size||st.product.has(r.product)) && within(r); }
  function isExcluded(r){ return st.excluded.has(rid(r)); }
  // Detail rows: every passing row (excluded ones stay visible, dimmed, so they
  // can be restored), sorted. Aggregates use kept() — passing minus excluded.
  function rows(){
    const rs = ROWS.filter(pass);
    const col = COLS.find(c=>c.k===sortKey) || COLS[5];
    rs.sort((a,b)=>{ let av=col.get(a), bv=col.get(b);
      if(typeof av==="string") return sortDir*av.localeCompare(bv);
      return sortDir*(av-bv); });
    return rs;
  }
  function kept(){ return ROWS.filter(r=>pass(r) && !isExcluded(r)); }
  function cards(){
    const ks = kept();
    const total = ks.reduce((a,r)=>a+(r.chain_usd||0),0);
    const c = [["PRs",ks.length],["Total chain cost",fmtUSD(total)],["Avg / PR",fmtUSD(ks.length?total/ks.length:0)]];
    document.getElementById("oc-cards").innerHTML = c.map(([k,v])=>'<div class="card"><div class="k">'+k+'</div><div class="v">'+v+'</div></div>').join("");
  }
  function groupKey(r){
    if(st.groupBy==="agent") return r.agent||"—";
    if(st.groupBy==="model_agent") return (r.model||"—")+"  ·  "+(r.agent||"—");
    return r.model||"—";
  }
  // Cost-per-outcome rollup by the chosen group, ranked by avg $/PR (the headline
  // "is this model cost-effective" number) — recomputes off kept() so excluding a
  // runaway immediately shows the group's economics without it.
  function rollup(){
    const m = new Map();
    kept().forEach(r=>{ const k=groupKey(r); const g=m.get(k)||{k,n:0,usd:0}; g.n++; g.usd+=(r.chain_usd||0); m.set(k,g); });
    const groups = [...m.values()].sort((a,b)=> (b.usd/b.n)-(a.usd/a.n));
    document.querySelector("#oc-rollup thead").innerHTML =
      '<tr><th>'+(st.groupBy==="agent"?"Agent":st.groupBy==="model_agent"?"Model · Agent":"Model")
      +'</th><th class="num">PRs</th><th class="num">Total $</th><th class="num">Avg $/PR</th></tr>';
    document.querySelector("#oc-rollup tbody").innerHTML = groups.length
      ? groups.map(g=>'<tr><td>'+esc(g.k)+'</td><td class="num">'+g.n+'</td><td class="num">'
          +fmtUSD(g.usd)+'</td><td class="num">'+fmtUSD(g.n?g.usd/g.n:0)+'</td></tr>').join("")
      : '<tr><td colspan="4" class="muted">No runs in scope (all excluded?)</td></tr>';
  }
  function table(){
    const rs = rows();
    document.querySelector("#oc-tbl thead").innerHTML = '<tr><th aria-label="exclude"></th>'+COLS.map(c=>
      '<th data-k="'+c.k+'" tabindex="0"'+(c.num?' class="num"':"")+'>'
      +c.t+(sortKey===c.k?(sortDir<0?" ▾":" ▴"):"")+'</th>').join("")+"</tr>";
    document.getElementById("oc-empty").style.display = rs.length?"none":"block";
    document.querySelector("#oc-tbl tbody").innerHTML = rs.map(r=>{
      const ex = isExcluded(r);
      const btn = '<button type="button" class="oc-ex'+(ex?" on":"")+'" data-rid="'+esc(rid(r))
        +'" aria-pressed="'+ex+'" title="'+(ex?"Restore this run to the totals":"Exclude this run from the totals")+'">'+(ex?"↩":"✕")+'</button>';
      return '<tr'+(ex?' class="oc-excluded-row"':"")+'><td class="oc-excell">'+btn+'</td>'+COLS.map(c=>
        '<td'+(c.num?' class="num"':(c.cls?' class="'+c.cls+'"':""))+' data-label="'+c.t+'">'+c.cell(r)+'</td>'
      ).join("")+"</tr>";
    }).join("");
  }
  function excludedNote(){
    // Count only excluded rows that are in the active window/product scope — those
    // are the ones actually pulled from the totals shown; an excluded row outside
    // the current filter doesn't affect these aggregates, so don't count it.
    const n = ROWS.filter(r=>pass(r) && isExcluded(r)).length;
    const el = document.getElementById("oc-excluded");
    if(!n){ el.style.display="none"; el.innerHTML=""; return; }
    el.style.display="block";
    el.innerHTML = "<b>"+n+"</b> run"+(n>1?"s":"")+" excluded from the totals above. "
      +'<button type="button" class="oc-linkbtn" id="oc-clear-ex">Restore all</button>';
    document.getElementById("oc-clear-ex").onclick = ()=>{ st.excluded.clear(); draw(); };
  }
  function draw(){ cards(); rollup(); table(); excludedNote(); }
  function onSort(th){ const k=th.dataset.k; if(!k) return;
    if(k===sortKey) sortDir*=-1; else { sortKey=k; sortDir=-1; } draw(); }
  function chips(){
    document.getElementById("oc-product").innerHTML = uniqProducts.map(v=>'<button type="button" class="chip" data-v="'+esc(v)+'">'+esc(v)+'</button>').join("");
    document.querySelectorAll("#oc-product .chip").forEach(b=>b.onclick=()=>{ const v=b.dataset.v; st.product.has(v)?st.product.delete(v):st.product.add(v); b.classList.toggle("on"); draw(); });
  }
  document.getElementById("oc-window").onchange = function(){ st.window=+this.value; draw(); };
  document.getElementById("oc-groupby").onchange = function(){
    st.groupBy=this.value;
    document.getElementById("oc-groupby-label").textContent = this.options[this.selectedIndex].text.toLowerCase();
    draw();
  };
  // Delegated exclude/restore toggle on the detail table.
  document.querySelector("#oc-tbl tbody").addEventListener("click", e=>{
    const b=e.target.closest(".oc-ex"); if(!b) return;
    const id=b.dataset.rid; st.excluded.has(id)?st.excluded.delete(id):st.excluded.add(id); draw();
  });
  document.querySelector("#oc-tbl thead").addEventListener("click", e=>{ const th=e.target.closest("th"); if(th) onSort(th); });
  document.querySelector("#oc-tbl thead").addEventListener("keydown", e=>{ if(e.key!=="Enter"&&e.key!==" ") return; const th=e.target.closest("th"); if(th){ e.preventDefault(); onSort(th); } });
  chips(); draw();
})();
</script>"""


def cost_performance_render(facts: list[dict]) -> str:
    # `</` → `<\/` so a `</script` in any embedded string can't close the <script>
    # block early (matches cost_panel_html's hist_json escaping; defense-in-depth —
    # the ledger fields are platform-written, but keep both embed paths consistent).
    data_json = json.dumps(facts, separators=(",", ":")).replace("</", "<\\/")
    fresh = cost_performance_freshness(facts)
    n_products = len({f["product"] for f in facts})
    n_models = len({f["model"] for f in facts if f["model"] != "—"})
    outcomes_json = json.dumps(_load_outcomes(), separators=(",", ":")).replace("</", "<\\/")
    body = (
        _SCORECARD_BODY + _SCORECARD_SCRIPT.replace("__DATA__", data_json)
        + _OUTCOMES_BODY + _OUTCOMES_SCRIPT.replace("__ODATA__", outcomes_json)
    )
    pill = f"{n_products} products · {n_models} models · {len(facts)} records"
    page_head = (
        f'<div class="page-head">'
        f'<h1>Agent cost-performance <span class="pill">{escape(pill)}</span></h1>'
        f'<p class="page-sub">Dev-crew success-vs-cost across the scorecard ledgers (ARCH-107). Newest '
        f'record: {escape(fresh.text)}. Pick a window or a custom range; hover any metric label for '
        f'what it measures and what events feed it.</p></div>'
    )
    # No stalled banner: cost_performance_freshness() never judges this feed
    # stalled (see its docstring for why) — stalled_banner_html renders "" for
    # stalled=None, so this is a no-op, kept for symmetry with every sibling.
    banner = ds.stalled_banner_html("Cost-performance ledger", fresh)
    return ds.page_shell(
        title="cost-performance",
        nav_html=ds.nav_component("/cost-performance", "full"),
        body=page_head + banner + body,
        extra_css=_COSTPERF_CSS,
    )


def cost_performance_live_html() -> str:
    """Render the Cost-Performance dashboard from current data at request time
    (ARCH-182). Cheap: reads the scorecard ledgers, no heavy producer — so the
    page reflects the latest scorecard the moment it lands, instead of being
    frozen at the once-daily static build. The scheduled build() still writes the
    static file as a fallback."""
    return cost_performance_render(normalize())


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "spending.html").write_text(
        spending_render(alert_policy_store.get("spending.overage_runs")), encoding="utf-8")
    facts = normalize()
    (OUT / "cost-performance.html").write_text(cost_performance_render(facts), encoding="utf-8")
    print(f"Wrote site/spending.html + site/cost-performance.html ({len(facts)} facts).")


if __name__ == "__main__":
    build()
