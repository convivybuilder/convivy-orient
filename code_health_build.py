#!/usr/bin/env python3.11
"""Code-health dashboard generator for convivy-orient (ARCH-110 follow-up).

A sibling to dashboard_build.py (cost+quality). Where that one reads JSONL
ledgers, this harvests per-repo engineering-health metrics live from `gh` + the
local git checkouts, then emits a self-contained `site/health.html` sliceable by
repo / kind / metric, client-side.

Metrics per repo (v1, all cheap + uniform across every repo — no per-repo deps):
  - CI: pass-rate over recent main runs, last conclusion + date
  - PRs: open count, oldest-open age, merged in the last 30d (throughput)
  - reverts in the last 30d (a revert ~ a regression that shipped)
  - test count (a `def test_` grep heuristic — approximate, dependency-free)
Coverage is deliberately deferred (it needs running coverage in each repo with
its deps — the "CI emits a health artifact" follow-up).

Served full-tier only (same gate as /dashboard) — see server.py's /health route.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import alert_policy_store
import crew_registry
import design_system as ds
import orient_time
from paths import STATE_ROOT

OUT = STATE_ROOT / "site"
CONVIVY = Path.home() / "Convivy"
# Persisted snapshot of the harvested rows, in the shared agent-runs data dir
# (alongside the scorecard ledgers) so the metric-alerts evaluator reads it
# without re-running the gh harvest.
SNAPSHOT = CONVIVY / "agent-runs" / "scorecard" / "health.json"
# Append-only daily history so the dashboard can trend each metric over time
# (the snapshot above is point-in-time and gets overwritten every build, so it
# can't carry a trend on its own). One slim record per repo per day; re-running
# the build on the same day overwrites that day's rows (last run wins).
HISTORY = CONVIVY / "agent-runs" / "scorecard" / "health-history.jsonl"

# Metrics carried into the daily history (the trendable subset of a harvest row).
# `coverage` is included though it's still None today (deferred per ARCH-111) —
# the trend slot is pre-wired so the sparkline fills in the day coverage data
# starts flowing, no template change needed.
TREND_METRICS = ("ci_pass_rate", "test_count", "coverage", "open_prs",
                 "oldest_open_days", "merged_recent", "reverts_recent")

# (repo, kind). kind drives the product/internal filter.
REPOS = [
    ("vivy", "product"),
    ("constance", "product"),
    ("sous-chef", "product"),
    ("selector", "product"),
    ("agent-platform", "internal"),
    ("knowledge", "internal"),
    ("convivy-orient", "internal"),
    ("hollis", "internal"),
    ("convivy-app-kit", "internal"),
]

CI_WINDOW = 20      # recent main runs to compute the pass-rate over
WINDOW_DAYS = 30    # reverts + merged-throughput window
# A revert is `git revert`'s subject: `Revert "<original subject>"`. Match the
# SUBJECT only (mirrors the velocity producer's _REVERT_RE) — see count_reverts.
_REVERT_RE = re.compile(r"^Revert\b")
# .claude holds agent worktrees (full repo copies → duplicate test files); worktrees
# likewise. Skipping them is what keeps the count to the canonical tree.
_SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git", ".ruff_cache",
              ".pytest_cache", "dist", "build", ".claude", "worktrees", "site-packages"}


# --- pure: metric derivation (testable) --------------------------------------


def _parse_iso(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def ci_health(runs: list[dict]) -> dict:
    """{pass_rate, last, last_date} from `gh run list` records. pass-rate counts
    only decisive runs (success vs failure) — cancelled/skipped/superseded runs
    are excluded from the denominator so a cancelled run doesn't read as a failure.
    `last` is the most recent decisive conclusion."""
    decisive = [r for r in runs if r.get("status") == "completed"
                and r.get("conclusion") in ("success", "failure")]
    if not decisive:
        return {"pass_rate": None, "last": None, "last_date": None}
    decisive.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
    passed = sum(1 for r in decisive if r["conclusion"] == "success")
    return {"pass_rate": passed / len(decisive),
            "last": decisive[0]["conclusion"], "last_date": (decisive[0].get("createdAt") or "")[:10]}


def pr_metrics(open_prs: list[dict], merged_prs: list[dict], now: datetime) -> dict:
    """{open, oldest_open_days, merged_recent} from gh pr lists."""
    ages = [(now - d).days for d in (_parse_iso(p.get("createdAt")) for p in open_prs) if d]
    cutoff = now - timedelta(days=WINDOW_DAYS)
    merged_recent = sum(1 for p in merged_prs
                        if (_parse_iso(p.get("mergedAt")) or now) >= cutoff)
    return {"open": len(open_prs),
            "oldest_open_days": max(ages) if ages else None,
            "merged_recent": merged_recent}


def parse_coverage_pct(text: str) -> float | None:
    """Coverage fraction (0..1) from a coverage artifact's JSON. Accepts two
    shapes so the artifact contract stays tool-agnostic:
      - coverage.py native: {"totals": {"percent_covered": 87.3, ...}}
      - simple contract:     {"pct": 87.3}   (for non-pytest stacks later)
    The number is a 0..100 percentage; returned as a 0..1 fraction to match the
    dashboard's fmtPct (which multiplies by 100). None on any malformation."""
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    totals = d.get("totals")
    if isinstance(totals, dict) and "percent_covered" in totals:
        pct = totals["percent_covered"]
    elif "pct" in d:
        pct = d["pct"]
    else:
        return None
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return None
    if pct < 0 or pct > 100:
        return None
    return round(pct / 100.0, 4)


# --- threshold status (reused by alerts_build.py's /alerts evaluator) -------
#
# `thresholds` mirrors the coloring on this page's own table (_BODY's COLS
# `color` callback for ci_pass_rate, whose good_min/warn_min are interpolated
# from the same source at render time) exactly — same numbers, same tiers —
# so the Dashboards alert feed and this page can never disagree about what
# counts as in-policy. None on a missing value: an alert evaluator must not
# read "no data" as "healthy".
#
# `thresholds` defaults to `alert_policy_store.DEFAULTS`, never `.get()` —
# this function stays pure so a test can call it with one argument and get
# the shipped default, with no control_db touched. A live caller
# (`render()`'s own callers) fetches the current policy once and passes it in.


def ci_status(rate: float | None, thresholds: dict | None = None) -> str | None:
    """good/warn/bad for a CI pass-rate. Applied to `ci_pass_rate_main`
    (ARCH-179's alert basis — merged-code health, not the noisier all-branch
    rate)."""
    if rate is None:
        return None
    t = thresholds or alert_policy_store.DEFAULTS["code_health.ci_pass_rate"]
    if rate >= t["good_min"]:
        return "good"
    if rate >= t["warn_min"]:
        return "warn"
    return "bad"


def oldest_pr_status(days: int | None) -> str | None:
    """good/warn/bad for the oldest open PR's age. Mirrors the `oldest_open_days`
    column's `color` callback above (amber past 7 days, red past 14)."""
    if days is None:
        return None
    if days > 14:
        return "bad"
    if days > 7:
        return "warn"
    return "good"


def reverts_status(n: int | None) -> str | None:
    """good/bad for the revert count. Mirrors the `reverts_recent` column's
    `color` callback above (any revert in the window is flagged)."""
    if n is None:
        return None
    return "bad" if n > 0 else "good"


def history_record(row: dict, date_str: str) -> dict:
    """Project a full harvest row to a slim dated history record (date + repo +
    the trendable metrics only)."""
    rec = {"date": date_str, "repo": row.get("repo")}
    for m in TREND_METRICS:
        rec[m] = row.get(m)
    return rec


def merge_history(existing: list[dict], today_rows: list[dict], date_str: str) -> list[dict]:
    """Append today's per-repo records to the accumulated history, replacing any
    prior records carrying today's date (so re-running the build on the same day
    overwrites that day's rows — last run wins, idempotent). Sorted by (date, repo)."""
    kept = [r for r in existing if r.get("date") != date_str]
    kept += [history_record(r, date_str) for r in today_rows]
    return sorted(kept, key=lambda r: (r.get("date") or "", r.get("repo") or ""))


def count_tests(repo_path: Path) -> int | None:
    """Approximate test count: `def test_` across test files, deps-free. None if
    the checkout is missing."""
    if not repo_path.is_dir():
        return None
    total = 0
    for p in repo_path.rglob("*.py"):
        if _SKIP_DIRS.intersection(p.parts):
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            try:
                total += sum(1 for line in p.read_text(errors="ignore").splitlines()
                             if re.match(r"\s*def test_", line))
            except OSError:
                continue
    return total


# --- impure: gh + git --------------------------------------------------------


def _gh_json(args: list[str]):
    try:
        cp = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
        if cp.returncode != 0 or not cp.stdout.strip():
            return None
        return json.loads(cp.stdout)
    except (OSError, json.JSONDecodeError):
        return None


def count_reverts(subjects: list[str]) -> int:
    """Count commit SUBJECTS that are reverts (`git revert` writes `Revert "..."`).

    Subject-only, NOT `git log --grep` over the whole message (ARCH-180): `--grep`'s
    `^` anchors to any line in the message, so a wrapped body line like "reverts to
    sonnet" or "reverts >= 2 / oldest PR > 14d" counted as a revert — every nonzero
    code-health revert count on 2026-06-17 was such a false positive, none a real
    revert. `^Revert\\b` on the subject matches `git revert`'s output and the velocity
    producer's convention; it won't match prose "reverts"/"Reverting"."""
    return sum(1 for s in subjects if _REVERT_RE.match(s))


def reverts_recent(repo_path: Path) -> int | None:
    """Revert commits in the local checkout's history over the window. None if the
    checkout is missing (count is approximate — the checkout may trail origin)."""
    if not (repo_path / ".git").exists():
        return None
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo_path), "log", f"--since={WINDOW_DAYS} days ago",
             "--format=%s"],
            capture_output=True, text=True, check=False)
        if cp.returncode != 0:
            return None
        return count_reverts(cp.stdout.splitlines())
    except OSError:
        return None


def coverage_pct(repo_full: str) -> float | None:
    """Latest CI coverage fraction (0..1) for a repo, read from the newest
    non-expired `coverage` artifact its CI uploaded. None when no artifact exists
    (the repo hasn't opted in / non-Python) or on ANY failure — coverage is
    additive, so a slow or missing fetch must never break the harvest."""
    resp = _gh_json(["api", f"repos/{repo_full}/actions/artifacts?per_page=100"])
    arts = resp.get("artifacts", []) if isinstance(resp, dict) else []
    cands = [a for a in arts if a.get("name") == "coverage" and not a.get("expired")]
    cands.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    if not cands:
        return None
    run_id = (cands[0].get("workflow_run") or {}).get("id")
    if not run_id:
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            cp = subprocess.run(
                ["gh", "run", "download", str(run_id), "-R", repo_full,
                 "-n", "coverage", "-D", td],
                capture_output=True, text=True, check=False)
            if cp.returncode != 0:
                return None
            tdp = Path(td)
            named = tdp / "coverage.json"
            if named.exists():
                return parse_coverage_pct(named.read_text())
            for p in tdp.glob("*.json"):   # fall back to any json in the artifact
                v = parse_coverage_pct(p.read_text())
                if v is not None:
                    return v
    except OSError:
        return None
    return None


def harvest(now: datetime) -> list[dict]:
    rows = []
    for repo, kind in REPOS:
        full = f"convivy/{repo}"
        # by workflow, NOT --branch main: the products run CI on PR branches
        # (pull_request), so a main-only filter would show them as having no CI.
        # This all-branch rate is the dashboard's `ci_pass_rate` column.
        runs = _gh_json(["run", "list", "--repo", full, "--workflow", "ci.yml", "-L",
                         str(CI_WINDOW), "--json", "conclusion,status,createdAt,event"]) or []
        # A SECOND, main-only harvest (ARCH-179): the all-branch rate sinks on a
        # busy night of agent PRs that fail→fix→merge (each pre-merge red is CI
        # working, caught before merge — not a health regression). The
        # code-health ALERTS gate on this main-only view instead, so they fire
        # only when *merged* code is unhealthy. A dedicated `--branch main` call
        # (not a filter on the all-branch sample) gets a full CI_WINDOW of main
        # runs, since main runs are sparse relative to PR runs.
        main_runs = _gh_json(["run", "list", "--repo", full, "--workflow", "ci.yml",
                              "--branch", "main", "-L", str(CI_WINDOW), "--json",
                              "conclusion,status,createdAt,event"]) or []
        open_prs = _gh_json(["pr", "list", "--repo", full, "--state", "open", "-L", "100",
                             "--json", "number,createdAt"]) or []
        # -L 300: busy products merge >100/window; a smaller cap would floor the
        # throughput number misleadingly.
        merged_prs = _gh_json(["pr", "list", "--repo", full, "--state", "merged", "-L", "300",
                               "--json", "number,mergedAt"]) or []
        ci = ci_health(runs)
        ci_main = ci_health(main_runs)
        prm = pr_metrics(open_prs, merged_prs, now)
        path = CONVIVY / repo
        rows.append({
            "repo": repo, "kind": kind,
            "ci_pass_rate": ci["pass_rate"], "ci_last": ci["last"], "ci_last_date": ci["last_date"],
            # main-only view — the alert basis (ARCH-179); None when a repo has
            # no push-to-main ci.yml runs, which leaves its CI alerts silent.
            "ci_pass_rate_main": ci_main["pass_rate"], "ci_last_main": ci_main["last"],
            "open_prs": prm["open"], "oldest_open_days": prm["oldest_open_days"],
            "merged_recent": prm["merged_recent"],
            "reverts_recent": reverts_recent(path),
            "test_count": count_tests(path),
            "coverage": coverage_pct(full),  # from the repo's CI coverage artifact (None until opted in)
        })
    return rows


def load_history(path: Path = HISTORY) -> list[dict]:
    """The accumulated daily history (one slim record per repo per day). [] if
    none yet — the first build seeds it."""
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


def load_snapshot(path: Path = SNAPSHOT) -> tuple[str, list[dict]]:
    """The last harvested per-repo rows + their harvest timestamp, from the
    persisted snapshot. ("", []) if none yet — the scheduled build seeds it. Live
    render reads this instead of re-harvesting: harvest() runs gh/git per repo,
    far too heavy for a per-request render (ARCH-182)."""
    if not path.exists():
        return "", []
    try:
        snap = json.loads(path.read_text())
        return snap.get("asof", ""), snap.get("repos", [])
    except (json.JSONDecodeError, OSError):
        return "", []


# com.convivy.code-health.plist runs this repo's own harvest daily at 07:45
# MST. 30h tolerates a late-running harvest without hiding a feed that's gone
# dark for the better part of two days.
_STALLED_AFTER_HOURS = 30
_EXPECTED_CADENCE = "daily (07:45 MST)"


def freshness(asof: str) -> ds.Freshness:
    """This dashboard's own freshness verdict for /health's harvest — read
    verbatim by /health's own page and by its /overview card
    (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment 902)."""
    text = orient_time.fmt(asof) or "no freshness reported"
    hours = orient_time.hours_since(asof)
    stalled = hours is not None and hours > _STALLED_AFTER_HOURS
    return ds.Freshness(
        text=text, stalled=stalled, cadence=_EXPECTED_CADENCE,
        reason="The daily gh/git harvest may not have run.",
    )


def live_html(scope: frozenset[str]) -> str:
    """Render /health from the last persisted harvest at request time (ARCH-182),
    restricted to the viewer's project scope (E19·3,
    issue_01M35K5EAZD85FBYXEBQVRKVZV): every row and history record for a repo
    the registry lists under no project in `scope` is dropped before `render()` ever sees it, so no out-of-scope
    repo name reaches the page, its inline JSON, or its trend table. An
    administrator's scope is the Library's full project list, so this filter
    removes nothing for them — the same rows render as before.

    The harvest (gh/git) stays the scheduled producer; this only re-reads its
    snapshot + history, so the page picks up a new harvest the moment it lands.
    asof reflects the harvest time, not the request, since the data is producer-fresh.

    Fetches the live CI threshold fresh on every call (no cache) so a write
    made from this same page's own form reaches both the table's coloring and
    the write form's displayed values on the very next request."""
    asof, rows = load_snapshot()
    repos = crew_registry.repos_in_scope(scope)
    rows = [r for r in rows if r.get("repo") in repos]
    history = [h for h in load_history() if h.get("repo") in repos]
    thresholds = alert_policy_store.get("code_health.ci_pass_rate")
    return render(rows, history, fresh=freshness(asof), thresholds=thresholds)


_PAGE_CSS = r"""
/* ---- Page head --------------------------------------------------------- */
.page-head { margin:24px 0 0; }
.page-head h1 { font-size:22px; font-weight:700; letter-spacing:-.01em; color:var(--ink); }
.page-sub { font-size:13px; color:var(--ink-2); margin-top:4px; margin-bottom:16px; }
.page-sub a { color:var(--brand-ink); text-decoration:none; }
.page-sub a:hover { text-decoration:underline; }

/* ---- Controls row (toolbar — renamed from .bar to avoid .bar-track/.bar-fill collision) */
.controls-row { display:flex; justify-content:space-between; align-items:center;
                gap:12px; flex-wrap:wrap; margin-bottom:16px; }
.controls-row .filters { margin-bottom:0; }

/* ---- Kind filter chips ------------------------------------------------- */
.filters { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }
.chip { padding:4px 12px; border-radius:20px; border:1px solid var(--line);
        background:var(--surface-2); color:var(--ink-2); cursor:pointer; font-size:12px;
        user-select:none; }
.chip[aria-pressed="true"] {
  background:var(--surface); color:var(--brand-ink); box-shadow:var(--shadow); font-weight:650; border-color:var(--line); }
.chip:focus-visible { outline:2px solid var(--brand); outline-offset:2px; }

/* ---- View segmented toggle (tablist) ----------------------------------- */
.seg { display:inline-flex; border:1px solid var(--line); border-radius:var(--radius);
       overflow:hidden; background:var(--surface-2); padding:2px; gap:2px; }
.vbtn { padding:5px 13px; font-size:13px; font-weight:500; color:var(--ink-2);
        background:transparent; border:none; border-radius:calc(var(--radius) - 2px); cursor:pointer; }
.vbtn[aria-selected="true"] {
  background:var(--surface); color:var(--brand-ink); box-shadow:var(--shadow); font-weight:650; }
.vbtn:hover:not([aria-selected="true"]) { background:var(--surface); color:var(--ink); }
.vbtn:focus-visible { outline:2px solid var(--brand); outline-offset:2px; }

/* ---- StatCards grid ---------------------------------------------------- */
.statcards { display:grid; grid-template-columns:repeat(auto-fill,minmax(130px,1fr));
             gap:12px; margin-bottom:16px; }

/* ---- Table decorations ------------------------------------------------- */
.data-table th { cursor:pointer; }
.data-table th, .data-table td { text-align:right; white-space:nowrap; }
.data-table th:first-child, .data-table td:first-child { text-align:left; }
td.k { font-weight:600; }
.data-table tbody tr:hover td { background:var(--brand-wash); }
.pill { font-size:11px; padding:1px 7px; border-radius:20px; border:1px solid var(--line);
        color:var(--ink-2); }
.dot { display:inline-block; width:8px; height:8px; border-radius:50%;
       margin-right:5px; vertical-align:middle; }

/* ---- Color signals ----------------------------------------------------- */
.good { color:var(--ok); }
.warn { color:var(--warn); }
.bad { color:var(--err); }
.muted { color:var(--ink-2); }
.empty { color:var(--ink-2); padding:30px; text-align:center; }

/* ---- Tooltip right-align override -------------------------------------- */
.tipr::after { left:auto; right:0; transform:none; }

/* ---- Coverage-gap marker (visible without a hover) --------------------- */
.cov-gap { color:var(--ink-2); font-weight:700; }
.cov-legend { color:var(--ink-2); font-size:12px; margin:8px 0 0; }

/* ---- Trend sparklines (kept bespoke — no ds equivalent) ---------------- */
table.trend th, table.trend td { text-align:center; }
table.trend td.k, table.trend th:first-child { text-align:left; }
.sparkwrap { display:inline-flex; align-items:center; gap:7px; }
svg.spark { display:block; }
.sparlabel { font-variant-numeric:tabular-nums; font-size:12px; }
"""

_BODY = r"""<div class="page-head">
  <h1>Code health <span class="pill">__NREPOS__ repos · __NTESTS__ tests · __NDAYS__d tracked</span></h1>
  <p class="page-sub">CI / PR throughput / reverts / tests per repo (ARCH-110). Windows = __WINDOW__d. As of __ASOF__.
    Hover any metric label for what it measures and what events feed it.</p>
</div>
__ALERT_FORM__
<div class="controls-row">
  <div class="filters" id="kinds"></div>
  <div class="seg" id="view" role="tablist" aria-label="Code health view">
    <button type="button" class="vbtn" data-v="table" aria-selected="true" role="tab">Table</button>
    <button type="button" class="vbtn" data-v="trends" aria-selected="false" role="tab">Trends</button>
  </div>
</div>
<div class="statcards" id="cards"></div>
<div class="data-table-wrap"><table class="data-table" id="tbl"><thead></thead><tbody></tbody></table></div>
<p class="cov-legend" id="covLegend" hidden>&dagger; Coverage CI step not wired for this repo.</p>
<div id="trendwrap" style="display:none"></div>
<script>
const ROWS = __DATA__;
const HISTORY = __HISTORY__;   // slim per-repo per-day records — the trend source
const state = { kind:new Set(), view:"table" };
document.getElementById("covLegend").hidden = !ROWS.some(r=>r.coverage==null);

function pass(f){ return !state.kind.size || state.kind.has(f.kind); }
function fmtPct(p){ return p==null? "—" : (p*100).toFixed(0)+"%"; }
function clsRate(p,g,w){ if(p==null) return ""; if(p>=g) return "good"; if(p>=w) return "warn"; return "bad"; }

// tip = what the metric is + which events feed it (rendered as a hover tooltip
// on the column header; no double quotes inside — they sit in an HTML attribute)
const TIPS = {
  repo:"One row per repo, harvested by the daily 07:45 build from gh and the local checkout. Dot = the most recent decisive CI conclusion (green success, red failure). Pill = product vs internal.",
  ci_pass_rate:"Successes ÷ the last up-to-20 decisive runs of the repo's ci.yml workflow, any branch or event (gh run list). Cancelled, skipped and in-progress runs count neither way. Green ≥__CI_GOOD_PCT__%, amber ≥__CI_WARN_PCT__%.",
  ci_last:"Conclusion of the most recent decisive (success-or-failure) ci.yml run, with its date in the harvest.",
  test_count:"def test_ functions counted across test_*.py / *_test.py files in the local checkout — a dependency-free approximation. Worktrees, venvs and .claude copies are skipped so each test counts once.",
  coverage:"Total line coverage from the newest coverage artifact the repo's own CI uploaded (pytest --cov, ARCH-114).",
  open_prs:"PRs currently open on GitHub.",
  oldest_open_days:"Age in days of the oldest open PR — a review-backlog proxy. Amber past 7 days, red past 14.",
  merged_recent:"PRs merged in the last "+__WINDOW__+" days — merge throughput.",
  reverts_recent:"Commits whose subject starts with Revert in the local checkout over the last "+__WINDOW__+" days. Each one approximates a regression that shipped. The local checkout may trail origin slightly.",
};
const COLS = [
  {k:"repo", t:"Repo", tip:TIPS.repo, get:r=>r.repo, cell:r=>{
     const c = r.ci_last==="success"?"ok":(r.ci_last==="failure"?"err":"ink-3");
     return `<td data-label="Repo" class="k"><span class="dot" style="background:var(--${c})"></span>${r.repo} <span class="pill">${r.kind}</span></td>`; }},
  {k:"ci_pass_rate", t:"CI pass%", tip:TIPS.ci_pass_rate, get:r=>r.ci_pass_rate, fmt:fmtPct, color:p=>clsRate(p,__CI_GOOD__,__CI_WARN__)},
  {k:"ci_last", t:"Last CI", tip:TIPS.ci_last, get:r=>r.ci_last||"—",
     fmt:v=>v, color:v=>v==="success"?"good":(v==="failure"?"bad":"muted")},
  {k:"test_count", t:"Tests", tip:TIPS.test_count, get:r=>r.test_count, fmt:v=>v==null?"—":v},
  {k:"coverage", t:"Cov%", tip:TIPS.coverage, get:r=>r.coverage,
     fmt:v=>v==null?'— <sup class="cov-gap" aria-hidden="true">&dagger;</sup>':fmtPct(v)},
  {k:"open_prs", t:"Open PRs", tip:TIPS.open_prs, get:r=>r.open_prs, fmt:v=>v},
  {k:"oldest_open_days", t:"Oldest PR (d)", tip:TIPS.oldest_open_days, get:r=>r.oldest_open_days,
     fmt:v=>v==null?"—":v, color:v=>v==null?"":(v>14?"bad":v>7?"warn":"")},
  {k:"merged_recent", t:"Merged/"+__WINDOW__+"d", tip:TIPS.merged_recent, get:r=>r.merged_recent, fmt:v=>v},
  {k:"reverts_recent", t:"Reverts/"+__WINDOW__+"d", tip:TIPS.reverts_recent, get:r=>r.reverts_recent,
     fmt:v=>v==null?"—":v, color:v=>v==null?"":(v>0?"bad":"good")},
];
let sortKey="repo", sortDir=1;

// metrics that get a sparkline in the trend view (order = column order)
const TRENDS = [
  {k:"ci_pass_rate",     t:"CI pass%",                pct:true},
  {k:"coverage",         t:"Cov%",                    pct:true},
  {k:"test_count",       t:"Tests"},
  {k:"open_prs",         t:"Open PRs"},
  {k:"oldest_open_days", t:"Oldest PR (d)"},
  {k:"merged_recent",    t:"Merged/"+__WINDOW__+"d"},
  {k:"reverts_recent",   t:"Reverts/"+__WINDOW__+"d"},
];
TRENDS.forEach(m=>{ m.tip = "Daily value from health-history.jsonl (one record per repo per day, appended by the 07:45 build). " + TIPS[m.k]; });

function renderCards(rows){
  const okCI = rows.filter(r=>r.ci_last==="success").length;
  const failCI = rows.filter(r=>r.ci_last==="failure").length;
  const tests = rows.reduce((a,r)=>a+(r.test_count||0),0);
  const reverts = rows.reduce((a,r)=>a+(r.reverts_recent||0),0);
  const cards=[
    ["Repos",rows.length,"","Repos matching the current product/internal filter."],
    ["CI green",okCI,"good","Repos whose most recent decisive ci.yml run succeeded."],
    ["CI red",failCI,failCI?"bad":"muted","Repos whose most recent decisive ci.yml run failed."],
    ["Tests",tests,"",TIPS.test_count],
    ["Reverts/"+__WINDOW__+"d",reverts,reverts?"bad":"good",TIPS.reverts_recent]
  ];
  document.getElementById("cards").innerHTML = cards.map(([l,v,c,tip])=>
    `<article class="statcard"><div class="statcard-value num ${c}">${v}</div><div class="statcard-label">`
    +(tip?`<span class="tip" data-tip="${tip}">${l}</span>`:l)+`</div></article>`).join("");
}

function renderTable(){
  const rows = ROWS.filter(pass);
  const col = COLS.find(c=>c.k===sortKey);
  rows.sort((a,b)=>{ let av=col.get(a), bv=col.get(b);
    av=(av==null)?(sortDir<0?-Infinity:Infinity):av; bv=(bv==null)?(sortDir<0?-Infinity:Infinity):bv;
    if(typeof av==="string") return sortDir*String(av).localeCompare(String(bv));
    return sortDir*(av-bv); });
  document.querySelector("#tbl thead").innerHTML = "<tr>"+COLS.map((c,i)=>
    `<th data-k="${c.k}"${c.tip?` class="tip${i>=COLS.length-2?" tipr":""}" data-tip="${c.tip}"`:""}>`
    +`${c.t}${sortKey===c.k?(sortDir<0?" ▾":" ▴"):""}</th>`).join("")+"</tr>";
  document.querySelector("#tbl tbody").innerHTML = rows.map(r=>"<tr>"+COLS.map(c=>{
    if(c.cell) return c.cell(r);
    let raw=c.get(r), txt=c.fmt?c.fmt(raw):raw, color=c.color?c.color(raw):"";
    return `<td data-label="${c.t}" class="${color}">${txt}</td>`;
  }).join("")+"</tr>").join("");
}

// inline-SVG sparkline over a value series (nulls = gaps, kept positionally so
// time spacing stays even). Returns the line + a dot on the latest point + a
// value label. No charting lib — keeps the page self-contained.
function fmtTrendVal(v,pct){
  if(v==null) return "—";
  return pct ? (v*100).toFixed(0)+"%" : (Number.isInteger(v)? String(v): v.toFixed(2));
}
function spark(values,pct){
  const w=104,h=26,pad=3;
  const pts = values.map((v,i)=>[i,v]).filter(([,v])=>v!=null && !isNaN(v));
  if(pts.length===0) return `<span class="muted">—</span>`;
  const last = pts[pts.length-1][1];
  const lbl = fmtTrendVal(last,pct);
  if(pts.length===1) return `<span class="sparlabel muted">${lbl}</span>`;
  const ys = pts.map(([,v])=>v);
  const mn=Math.min(...ys), mx=Math.max(...ys), span=(mx-mn)||1, n=values.length;
  const X=i=> pad + (n<2?0:(i/(n-1))*(w-2*pad));
  const Y=v=> (h-pad) - ((v-mn)/span)*(h-2*pad);
  let d=""; pts.forEach(([i,v],k)=>{ d += (k?"L":"M")+X(i).toFixed(1)+" "+Y(v).toFixed(1)+" "; });
  const [li,lv]=pts[pts.length-1];
  const trend = ys[ys.length-1]-ys[0];   // first→last direction, for the line tint
  const col = trend>0? "var(--ok)" : trend<0? "var(--err)" : "var(--brand)";
  return `<span class="sparkwrap"><svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">`
    + `<path d="${d}" fill="none" stroke="${col}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>`
    + `<circle cx="${X(li).toFixed(1)}" cy="${Y(lv).toFixed(1)}" r="2.2" fill="${col}"/></svg>`
    + `<span class="sparlabel">${lbl}</span></span>`;
}

function renderTrends(){
  const wrap = document.getElementById("trendwrap");
  const dates = [...new Set(HISTORY.map(h=>h.date))].sort();
  if(dates.length < 2){
    wrap.innerHTML = `<div class="empty">Trends accumulate daily — need ≥2 days of history to draw a line `
      + `(currently ${dates.length} day${dates.length===1?"":"s"}). The daily 07:45 build appends one record `
      + `per repo; check back tomorrow.</div>`;
    return;
  }
  const repos = [...new Set(HISTORY.map(h=>h.repo))]
    .filter(repo=>{ const r=ROWS.find(x=>x.repo===repo); return !r || pass(r); })
    .sort();
  const lut = {};
  for(const h of HISTORY){ (lut[h.repo]=lut[h.repo]||{})[h.date]=h; }
  const head = "<tr><th>Repo</th>"+TRENDS.map((m,i)=>
    `<th class="tip${i>=TRENDS.length-2?" tipr":""}" data-tip="${m.tip}">${m.t}</th>`).join("")+"</tr>";
  const body = repos.map(repo=>{
    const cells = TRENDS.map(m=>{
      const series = dates.map(d=> lut[repo][d]? lut[repo][d][m.k] : null);
      return `<td>${spark(series,m.pct)}</td>`;
    }).join("");
    return `<tr><td class="k">${repo}</td>${cells}</tr>`;
  }).join("");
  wrap.innerHTML =
    `<div class="muted" style="font-size:12px;margin-bottom:12px">Each line = the metric over the last ${dates.length} `
    + `tracked days (${dates[0]} → ${dates[dates.length-1]}); dot = latest; green/red = up/down vs first day. `
    + `Cov% fills in once per-repo coverage is wired.</div>`
    + `<div class="data-table-wrap"><table class="data-table trend"><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
}

function draw(){
  const rows = ROWS.filter(pass);
  renderCards(rows);
  const t = state.view==="trends";
  document.getElementById("tbl").closest(".data-table-wrap").style.display = t? "none":"";
  document.getElementById("trendwrap").style.display = t? "":"none";
  if(t) renderTrends(); else renderTable();
}

const kinds=[...new Set(ROWS.map(r=>r.kind))].sort();
document.getElementById("kinds").innerHTML =
  kinds.map(k=>`<button type="button" class="chip" data-k="${k}" aria-pressed="false">${k}</button>`).join("");
document.getElementById("kinds").onclick=e=>{const c=e.target.closest(".chip"); if(!c)return;
  const k=c.dataset.k; state.kind.has(k)?state.kind.delete(k):state.kind.add(k);
  c.setAttribute('aria-pressed', state.kind.has(k)?'true':'false');
  draw(); _ds.writeUrlState(currentStateToParams(),false);};
document.getElementById("view").onclick=e=>{const b=e.target.closest(".vbtn"); if(!b)return;
  state.view=b.dataset.v;
  document.querySelectorAll("#view .vbtn").forEach(x=>x.setAttribute("aria-selected", x===b?"true":"false"));
  draw(); _ds.writeUrlState(currentStateToParams(),false);};
document.querySelector("#tbl thead").addEventListener("click",e=>{
  const th=e.target.closest("th"); if(!th)return;
  const k=th.dataset.k; if(k===sortKey) sortDir*=-1; else {sortKey=k; sortDir=1;}
  renderTable(); _ds.writeUrlState(currentStateToParams(),false);});
// URL state: serialize current filter/sort/view to URL params (defaults omitted).
function currentStateToParams(){
  var p={};
  var kl=[...state.kind].sort().join(",");
  if(kl) p.kind=kl;
  if(state.view!=="table") p.view=state.view;
  if(sortKey!=="repo") p.sort=sortKey;
  if(sortDir!==1) p.dir=String(sortDir);
  return p;
}
// URL state: apply a params object to live state + reflect chips/tabs.
// Never calls draw() — caller is responsible.
function applyUrlState(s){
  state.kind=new Set(s.kind?s.kind.split(",").filter(function(k){return k;}):[] );
  state.view=(s.view==="trends")?"trends":"table";
  // Validate sort against the known columns: an unknown key from a crafted/stale
  // URL would make COLS.find() return undefined and crash renderTable.
  sortKey=COLS.some(function(c){return c.k===s.sort;})?s.sort:"repo";
  sortDir=s.dir==="-1"?-1:1;
  document.querySelectorAll("#kinds .chip").forEach(function(c){
    var on=state.kind.has(c.dataset.k);
    c.setAttribute("aria-pressed",on?"true":"false");
  });
  document.querySelectorAll("#view .vbtn").forEach(function(b){
    b.setAttribute("aria-selected",b.dataset.v===state.view?"true":"false");
  });
}
applyUrlState(_ds.readUrlState());
_ds.writeUrlState(currentStateToParams(),true);
draw();
_ds.onUrlState(function(s){ applyUrlState(s); draw(); });
</script>"""


def render(rows: list[dict], history: list[dict], fresh: ds.Freshness | None = None,
          thresholds: dict | None = None) -> str:
    """`thresholds` defaults to `alert_policy_store.DEFAULTS` — pure, like
    `ci_status` — so a test calling this with no fourth argument (as every
    existing caller does) renders against the shipped default rather than
    touching control_db. `live_html()` is the one caller that fetches the
    live value and threads it through."""
    fresh = fresh or ds.Freshness(text=orient_time.now_label(), stalled=False)
    t = thresholds or alert_policy_store.DEFAULTS["code_health.ci_pass_rate"]
    total_tests = sum(r["test_count"] or 0 for r in rows)
    n_days = len({h.get("date") for h in history})
    data_json = json.dumps(rows, separators=(",", ":")).replace("</", "<\\/")
    history_json = json.dumps(history, separators=(",", ":")).replace("</", "<\\/")
    alert_form = ds.alert_threshold_form_html(
        key="code_health.ci_pass_rate", title="CI pass-rate alert threshold",
        fields=[
            ("good_min", "Good ≥", t["good_min"], "0.01"),
            ("warn_min", "Warn ≥", t["warn_min"], "0.01"),
        ])
    body = (_BODY
            .replace("__DATA__", data_json)
            .replace("__HISTORY__", history_json)
            .replace("__ASOF__", escape(fresh.text))
            .replace("__NREPOS__", str(len(rows)))
            .replace("__WINDOW__", str(WINDOW_DAYS))
            .replace("__NDAYS__", str(n_days))
            .replace("__NTESTS__", str(total_tests))
            .replace("__ALERT_FORM__", alert_form)
            .replace("__CI_GOOD__", json.dumps(t["good_min"]).replace("</", "<\\/"))
            .replace("__CI_WARN__", json.dumps(t["warn_min"]).replace("</", "<\\/"))
            .replace("__CI_GOOD_PCT__", str(round(t["good_min"] * 100)))
            .replace("__CI_WARN_PCT__", str(round(t["warn_min"] * 100))))
    banner = ds.stalled_banner_html("Code-health harvest", fresh)
    return ds.page_shell(
        title="code health",
        nav_html=ds.nav_component("/health", "full"),
        body=banner + body,
        extra_css=_PAGE_CSS,
    )


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    rows = harvest(now)
    # History is keyed on the LOCAL calendar day (matching the page's local `asof`
    # and the 07:45-local daily cadence — one record per repo per local day), not
    # UTC, so an evening rebuild and the next-morning scheduled run don't collide.
    local_day = datetime.now().astimezone().date().isoformat()
    history = merge_history(load_history(), rows, local_day)
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(
        {"asof": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "repos": rows}, indent=2))
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text("\n".join(json.dumps(h, separators=(",", ":")) for h in history) + "\n")
    # The static fallback is served (ARCH-182) to ANY viewer whose live render
    # throws, before scope is known to this file at all — a per-repo row here
    # would be real project data with no scope check on the way out (E19·3,
    # issue_01M35K5EAZD85FBYXEBQVRKVZV). Deliberately empty rather than the
    # last harvest, same discipline as issue_build.main().
    (OUT / "health.html").write_text(
        ds.page_shell(title="code health", nav_html=ds.nav_component("/health", "full"),
                     body=ds.empty_state("Live view unavailable — reload in a moment."),
                     extra_css=_PAGE_CSS),
        encoding="utf-8")
    print(f"Wrote site/health.html (static fallback, empty) + {SNAPSHOT} ({len(rows)} repos) + "
          f"{HISTORY.name} ({len(history)} records / {len({h['date'] for h in history})} days).")


if __name__ == "__main__":
    sys.exit(build())
