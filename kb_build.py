#!/usr/bin/env python3.11
"""kb_build.py — KB retrieval observability dashboard (ARCH-121).

Renders site/kb.html: how effectively the knowledge index serves agents. Three
panels:

  1. Retrieval quality — recall@k + MRR from the kb-eval golden-set harness,
     overall and broken down by query style (keyword vs natural-language) and
     target doc kind, plus the current misses. This panel refreshes the eval by
     running `kb-eval` (its own knowledge venv) before reading kb-eval.json.
  2. Live queries — volume, null-result and hit-limit (truncation) rates,
     latency, the orchestrated-vs-interactive split, and the top null-result
     queries (the searches worth fixing) from ~/.knowledge-index/kb-queries.jsonl.
  3. Individuation coverage — per-profile docs baked into .claude/ vs matched
     but left to live query, from kb-materializations.jsonl.
  4. KB write audit — the weekly `kb-audit` stream from the Library: a digest
     week reads clean, an alert week names the commits that bypassed both
     sanctioned write paths, and a window with no signal says the control did
     not run.

Served full-tier only (server.py /kb route, same gate as /dashboard + /health).
Daily launchd com.convivy.kb-observability. The running server reads the file
per request, so no restart. Empty logs render as "no traffic yet" — the eval
panel always has data.
"""
import datetime as dt
import json
import re
import subprocess
import sys
from html import escape
from pathlib import Path

import alert_policy_store
import design_system as ds
import library_client
import orient_time
import paths
from paths import STATE_ROOT

OUT = STATE_ROOT / "site"
HOME = Path.home()
KB_EVAL_JSON = HOME / "Convivy" / "agent-runs" / "scorecard" / "kb-eval.json"
QUERY_LOG = HOME / ".knowledge-index" / "kb-queries.jsonl"
MAT_LOG = HOME / ".knowledge-index" / "kb-materializations.jsonl"

# Resolved through paths.resolve_tool, not `HOME / ".local" / "bin" /
# "kb-eval"` — that literal was the DEVELOPER symlink farm into the
# agent-platform working checkout, reachable from this deploy-pinned launchd
# job (com.convivy.kb-observability), which is what
# issue_01M1DKJYHSH9FT1SBP9068MTT1 fixes.
KB_EVAL_BIN = paths.resolve_tool("kb-eval")


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def refresh_eval() -> bool:
    """Re-run the golden-set eval (its own knowledge venv) so kb-eval.json is
    current before we render. Best-effort: on failure we render the last run,
    but a non-zero exit or a raised exception is logged to stderr rather than
    discarded, since a silently swallowed failure here is the same defect one
    layer up (issue_01M1S3YSKJV3GCB8ZN4MQ7W16N).

    Returns whether THIS call refreshed KB_EVAL_JSON (its mtime moved), not
    whether the file exists — a stale file left over from before the harness
    started failing exists on disk either way, so only the mtime comparison
    tells a dead eval apart from a healthy one."""
    if not KB_EVAL_BIN:
        return False
    before = _eval_json_mtime()
    try:
        result = subprocess.run([KB_EVAL_BIN], capture_output=True, timeout=180, check=False)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            if len(stderr) > 500:
                stderr = stderr[:500] + " (truncated)"
            print(f"kb-eval exited {result.returncode}: {stderr}", file=sys.stderr)
    except Exception as exc:
        print(f"kb-eval raised {exc!r}", file=sys.stderr)
    after = _eval_json_mtime()
    return after is not None and after != before


def _eval_json_mtime() -> float | None:
    """KB_EVAL_JSON's mtime, or None if it doesn't exist yet. Isolated from
    refresh_eval's own try/except so a stat failure (permission change, the
    scorecard directory going away) logs its own line instead of being
    mistaken for a subprocess failure — and, like the subprocess failure,
    must never propagate: build() has no handler of its own."""
    try:
        return KB_EVAL_JSON.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError as exc:
        print(f"kb-eval.json stat failed: {exc!r}", file=sys.stderr)
        return None


# This repo's own com.convivy.kb-observability.plist re-runs kb-eval and
# regenerates this page's eval panel daily at 07:48 MST. 30h tolerates a
# late-running harvest without hiding a feed that's gone dark for the better
# part of two days.
_STALLED_AFTER_HOURS = 30
_EXPECTED_CADENCE = "daily (07:48 MST)"


def freshness(ev: dict | None) -> ds.Freshness:
    """This dashboard's own freshness verdict for /kb's golden-set eval —
    read verbatim by /kb's own page and by its /overview card
    (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment 902). Judges only the eval
    panel: the live-query and individuation panels read their logs
    directly at request time, so they're current by construction and carry
    no staleness of their own to report."""
    if not ev:
        return ds.Freshness(text="no freshness reported", stalled=None)
    gen = ev.get("generated")
    text = orient_time.fmt(gen, assume_utc=True) or "no freshness reported"
    hours = orient_time.hours_since(gen, assume_utc=True)
    stalled = hours is not None and hours > _STALLED_AFTER_HOURS
    return ds.Freshness(
        text=text, stalled=stalled, cadence=_EXPECTED_CADENCE,
        reason="The daily kb-eval refresh may not have run.",
    )


def load_eval() -> dict | None:
    if KB_EVAL_JSON.exists():
        try:
            return json.loads(KB_EVAL_JSON.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _pctile(vals: list[float], p: float):
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    f = int(k)
    return s[f] if f + 1 >= len(s) else s[f] + (s[f + 1] - s[f]) * (k - f)


def query_stats(rows: list[dict]) -> dict:
    searches = [r for r in rows if r.get("tool") == "kb_search"]
    n_s = len(searches)
    empty = sum(1 for r in searches if r.get("empty"))
    hitlimit = sum(1 for r in searches if r.get("hit_limit"))
    lat = [r["latency_ms"] for r in rows if isinstance(r.get("latency_ms"), (int, float))]
    orchestrated = sum(1 for r in rows if r.get("run_id"))
    nulls: dict[str, int] = {}
    for r in searches:
        if r.get("empty") and r.get("query"):
            nulls[r["query"]] = nulls.get(r["query"], 0) + 1
    return {
        "total": len(rows),
        "searches": n_s,
        "gets": sum(1 for r in rows if r.get("tool") == "kb_get"),
        "topics": sum(1 for r in rows if r.get("tool") == "kb_list_topics"),
        "empty_rate": (empty / n_s) if n_s else None,
        "hitlimit_rate": (hitlimit / n_s) if n_s else None,
        "p50": _pctile(lat, 0.5),
        "p95": _pctile(lat, 0.95),
        "orchestrated": orchestrated,
        "interactive": len(rows) - orchestrated,
        "top_nulls": sorted(nulls.items(), key=lambda kv: -kv[1])[:10],
        "first": min((r.get("ts") for r in rows if r.get("ts")), default=None),
        "last": max((r.get("ts") for r in rows if r.get("ts")), default=None),
    }


def mat_stats(rows: list[dict]) -> dict:
    by_outcome: dict[str, int] = {}
    for r in rows:
        by_outcome[r.get("outcome", "?")] = by_outcome.get(r.get("outcome", "?"), 0) + 1
    latest: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("project"), r.get("profile"))
        if key not in latest or r.get("ts", "") > latest[key].get("ts", ""):
            latest[key] = r
    profiles = []
    for (proj, prof), r in sorted(latest.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        agents, skills, total = r.get("agents", 0), r.get("skills", 0), r.get("total_docs", 0)
        profiles.append({
            "project": proj, "profile": prof, "agents": agents, "skills": skills,
            "total_docs": total, "individuated": agents + skills,
            "live_only": max(total - (agents + skills), 0),
        })
    return {"total": len(rows), "by_outcome": by_outcome, "profiles": profiles}


# --- rendering ---

# What each metric is + which events feed it (hover tooltips). Keyed for reuse
# across cards and table headers; escaped at render time.
TIPS = {
    # panel sources
    "eval_panel": "Hand-authored golden set (agent-platform eval/kb-golden.jsonl) run through the real kb_search against the live index. Refreshed by running kb-eval at each build; written to kb-eval.json.",
    "query_panel": "Every mcp-kb tool call the server logs to ~/.knowledge-index/kb-queries.jsonl, across interactive co sessions and dispatched agents.",
    "mat_panel": "knowledge-ctx activations logged to ~/.knowledge-index/kb-materializations.jsonl. One row per project/profile; the latest activation wins.",
    "audit_panel": "The weekly KB write audit (agent-platform kb_audit), read from the Library's kb-audit stream. A digest is a clean week; an alert names the commits that reached main by neither the cage nor a PR merge.",
    # retrieval quality
    "recall1": "Share of golden queries whose expected doc comes back as the top search result.",
    "recall5": "Share of golden queries whose expected doc lands in the top 5 results.",
    "mrr": "Mean reciprocal rank of the expected doc across golden queries: 1.0 = always first, 0.5 = typically second.",
    "cases": "Hand-authored query → expected-doc pairs in the golden set; grows as real null-result queries get promoted into it.",
    "style": "How the golden query is phrased — terse keyword vs a natural-language question, the two retrieval modes agents actually use.",
    "kind": "The expected doc's kind in the KB schema (adr, runbook, reference, …) — shows which doc types retrieval favors.",
    "n": "Golden cases in this bucket.",
    "misses": "Golden queries whose expected doc missed the top k — the concrete retrieval gaps to fix with an alias, a title change, or an index fix.",
    # live queries
    "total": "All logged mcp-kb calls: kb_search + kb_get + kb_list_topics.",
    "searches": "kb_search calls only — the denominator for the rate cards.",
    "empty_rate": "Share of searches that returned zero hits; each one is an agent that found nothing. Green ≤5%, amber ≤20%.",
    "hitlimit_rate": "Share of searches that filled their result cap — relevant docs may have been cut off below the limit.",
    "p50": "Median latency across logged tool calls.",
    "p95": "95th-percentile latency across logged tool calls.",
    "orch": "Calls carrying an agentctl run id (dispatched agents) vs calls without one (interactive co sessions).",
    "nulls": "The most-repeated searches that returned nothing — the best candidates for a new doc, an alias, or a query fix.",
    # individuation
    "profile": "The project/profile a session activated on start via knowledge-ctx.",
    "agents": "Agent docs written into the repo's .claude/agents/ at activation.",
    "skills": "Skill docs written into the repo's .claude/skills/ at activation.",
    "baked": "agents + skills — docs physically individuated into .claude/, loaded with zero query latency.",
    "live_only": "Docs the profile matched but left to live MCP query (kb_search / kb_get) instead of baking in.",
    "matched": "Everything the profile matched: baked in + live-only.",
}


def _tip(label: str, tip: str, right: bool = False) -> str:
    """Wrap a label in a hover-tooltip span (right-anchored for edge columns)."""
    cls = "tip tipr" if right else "tip"
    return f'<span class="{cls}" data-tip="{escape(tip)}">{escape(label)}</span>'


def _pct(x) -> str:
    return f"{x * 100:.0f}%" if isinstance(x, (int, float)) else "—"


def cls_recall(x, thresholds: dict | None = None) -> str:
    """good/warn/bad for a recall or MRR value. Reused by alerts_build.py — the
    Dashboards-alert evaluator for /alerts — as the KB Retrieval policy threshold,
    so this and the KB page can never disagree about what counts as on-target.

    `thresholds` defaults to `alert_policy_store.DEFAULTS` — pure, so a test
    calling this with one argument (every existing caller) gets the shipped
    default with no control_db touched. `render()`'s live caller fetches the
    current policy once and threads it through every call in one render."""
    if not isinstance(x, (int, float)):
        return "muted"
    t = thresholds or alert_policy_store.DEFAULTS["kb.recall"]
    return "good" if x >= t["good_min"] else "warn" if x >= t["warn_min"] else "bad"


def status_recall(x, thresholds: dict | None = None) -> str:
    """Short non-color qualifier for a recall value (WCAG 1.4.1 supplement).
    Pairs with cls_recall so the status isn't carried by color alone — takes
    the same `thresholds` argument so the two can never disagree about what
    counts as on-target."""
    if not isinstance(x, (int, float)):
        return ""
    t = thresholds or alert_policy_store.DEFAULTS["kb.recall"]
    return "✓ on target" if x >= t["good_min"] else "△ below target"


def empty_rate_status(rate) -> str | None:
    """good/warn/bad for a live-query null-result rate. Mirrors the thresholds
    the inline "null-result rate" StatCard below colors by (bad > 20%, warn >
    5%) — reused by alerts_build.py so the two can't drift apart.

    Returns None on a missing rate (no searches logged yet), unlike the StatCard
    below which treats it as 0% for display. An alerts feed must not read "no
    traffic logged yet" as "the metric is healthy" — the same unknown-is-not-good
    distinction dashboard_build.overage_status draws."""
    if not isinstance(rate, (int, float)):
        return None
    if rate > 0.2:
        return "bad"
    if rate > 0.05:
        return "warn"
    return "good"


def _card(value: str, label: str, cls: str = "", tip: str | None = None,
          status: str = "") -> str:
    lbl = _tip(label, tip) if tip else escape(label)
    cls_attr = f" {cls}" if cls else ""
    delta_html = (f'<div class="statcard-delta">{escape(status)}</div>'
                  if status else "")
    return (f'<article class="statcard">'
            f'<div class="statcard-value num{cls_attr}">{value}</div>'
            f'<div class="statcard-label">{lbl}</div>'
            f'{delta_html}'
            f'</article>')


_PAGE_CSS = r"""
/* ---- Page head --------------------------------------------------------- */
.page-head { margin:24px 0 0; }
.page-head h1 { font-size:22px; font-weight:700; letter-spacing:-.01em; color:var(--ink); }
.page-sub { font-size:13px; color:var(--ink-2); margin-top:4px; margin-bottom:20px; }

/* ---- Sections ---------------------------------------------------------- */
section { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius);
          padding:16px 18px; margin-bottom:18px; box-shadow:var(--shadow); }
section h2 { font-size:15px; font-weight:650; margin:0 0 12px; color:var(--ink); letter-spacing:-.01em; }
section h3 { font-size:12px; font-weight:650; color:var(--ink-2); text-transform:uppercase;
             letter-spacing:.04em; margin:18px 0 8px; }

/* ---- StatCards --------------------------------------------------------- */
.cards { display:flex; flex-wrap:wrap; gap:12px; margin-bottom:12px; }

/* ---- Color signals ----------------------------------------------------- */
.good { color:var(--ok); }
.warn { color:var(--warn); }
.bad { color:var(--err); }
.muted { color:var(--ink-2); }

/* ---- Pill -------------------------------------------------------------- */
.pill { font-size:11px; padding:1px 8px; border-radius:20px; background:var(--surface-2);
        border:1px solid var(--line); color:var(--ink-2); font-weight:400; margin-left:6px; }

/* ---- Sub text ---------------------------------------------------------- */
.sub { color:var(--ink-2); font-size:12px; margin-top:8px; }

/* ---- code -------------------------------------------------------------- */
code { background:var(--surface-2); border:1px solid var(--line); border-radius:4px;
       padding:1px 5px; font-family:var(--mono); font-size:12px; }

/* ---- Table decorations ------------------------------------------------- */
.data-table tbody tr:hover td { background:var(--brand-wash); }
.data-table td.k { font-weight:600; text-align:left; }
.data-table th:first-child, .data-table td:first-child { text-align:left; }
.data-table th, .data-table td { text-align:right; white-space:nowrap; }

/* ---- Tooltip right-align override -------------------------------------- */
.tipr::after { left:auto; right:0; transform:none; }
"""


# The audit's own window: `launch-weekly-kb-audit` runs `--since 8.days`, a day
# of overlap so a run that slips a day still lands inside it. A newest signal
# older than this means the weekly control did not run.
_AUDIT_WINDOW = dt.timedelta(days=8)

# The stored signal body has its line breaks flattened to single spaces, so the
# commit list is parsed from the text as one line. A commit starts at a short
# sha and a date; its detail runs to the next commit or the end of the block.
_BYPASS_BLOCK_END = re.compile(r"\s(?:Each of these|WRITTEN ON ATTESTATION)")
_COMMIT_START = re.compile(r"(?<![0-9A-Za-z])([0-9a-f]{7,40})\s+(\d{4}-\d{2}-\d{2})\s+")


def load_audit() -> list[library_client.SignalRecord] | None:
    """The `kb-audit` stream, or None when the Library cannot be read. None is
    rendered as "unavailable"; an empty list would read as a clean audit, which
    is the one thing a failed read must never look like."""
    try:
        return library_client.list_signals(stream=["kb-audit"])
    except (OSError, ValueError, RuntimeError) as e:
        print(f"kb audit: could not read the kb-audit stream ({type(e).__name__})",
              file=sys.stderr)
        return None


def _audit_when(sig: library_client.SignalRecord) -> dt.datetime:
    return sig.produced_at or sig.ingested_at


def _bypass_commits(body: str) -> list[tuple[str, str, str]]:
    """The commits an alert names under its BYPASSED header, as (sha, date,
    detail). The block ends at the sentence that follows the list, or at the
    attested section."""
    start = body.find("BYPASSED BOTH SANCTIONED PATHS")
    if start < 0:
        return []
    block = body[start:]
    end = _BYPASS_BLOCK_END.search(block)
    if end:
        block = block[:end.start()]
    starts = list(_COMMIT_START.finditer(block))
    commits = []
    for i, m in enumerate(starts):
        stop = starts[i + 1].start() if i + 1 < len(starts) else len(block)
        commits.append((m.group(1), m.group(2), block[m.end():stop].strip()))
    return commits


def _audit_panel(rows: list[library_client.SignalRecord] | None, now: dt.datetime) -> str:
    """Panel 4. Branches on `kind` within the newest signal, never on `digest`
    alone: anything that is not a digest is shown as findings, so a kind this
    code does not know is never shown as clean."""
    head = f'<h2>{_tip("KB write audit", TIPS["audit_panel"])}</h2>'
    if rows is None:
        return (f'<section>{head}'
                + ds.error_state("The audit results are unavailable: the Library did "
                                 "not answer, so no week can be read as clean.")
                + '</section>')

    latest = max(rows, key=_audit_when, default=None)
    if latest is None or _audit_when(latest) < now - _AUDIT_WINDOW:
        if latest is None:
            last = "No audit has ever been captured."
        else:
            last = (f"The last audit ran "
                    f"{escape(orient_time.fmt(_audit_when(latest).isoformat(), assume_utc=True))}.")
        return (f'<section>{head}'
                + ds.empty_state("The weekly control did not run in the last 8 days, so "
                                 "this week is unknown, not clean. " + last)
                + '</section>')

    stamp = (f'<p class="sub">Last audit ran '
             f'{escape(orient_time.fmt(_audit_when(latest).isoformat(), assume_utc=True))}.</p>')
    if latest.kind == "digest":
        body = ('<p>Clean: no commit reached main by neither the cage nor a PR merge.</p>'
                f'<pre style="white-space:pre-wrap">{escape(latest.body)}</pre>')
    else:
        commits = _bypass_commits(latest.body)
        if commits:
            commit_rows = "".join(
                f'<tr><td data-label="Commit" class="k">{escape(sha)}</td>'
                f'<td data-label="Date">{escape(date)}</td>'
                f'<td data-label="Author and subject" style="text-align:left">{escape(detail)}</td></tr>'
                for sha, date, detail in commits)
            body = (f'<p><strong>Findings:</strong> these commits reached main by neither the '
                    f'cage nor a PR merge, so no review or provenance covers them.</p>'
                    f'<div class="data-table-wrap"><table class="data-table"><thead><tr>'
                    f'<th>commit</th><th>date</th><th style="text-align:left">author and subject</th>'
                    f'</tr></thead><tbody>{commit_rows}</tbody></table></div>')
        else:
            body = f'<pre style="white-space:pre-wrap">{escape(latest.body)}</pre>'
    return f'<section>{head}{stamp}{body}</section>'


def render(ev: dict | None, q: dict, m: dict, thresholds: dict | None = None,
           audit: list[library_client.SignalRecord] | None = None,
           whole_fleet: bool = True) -> str:
    """`thresholds` defaults to `alert_policy_store.DEFAULTS` — pure, so a
    test calling this with three arguments (every existing caller) renders
    against the shipped default with no control_db touched. `live_html()`
    fetches the current policy once and threads it through this whole render,
    so every recall/MRR cell on the page and the write form's own displayed
    values agree with each other.

    `audit` is `load_audit()`'s result. The default None renders panel 4 as
    unavailable, so a caller that omits the stream cannot show a clean audit
    by omission.

    **`whole_fleet` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) gates panels 1,
    2 and 4** (retrieval quality, live queries, the KB write audit): each is
    a corpus-wide or repo-wide figure with no per-project breakdown this file
    can filter it by — the golden-set eval runs over the whole KB, the query
    log has no product field, and the write audit is about commits to the
    shared `convivy/knowledge` repo, not a product repo. Panel 3
    (individuation coverage) is filtered to `scope` by its caller
    (`live_html`/`build`) before it ever reaches here — `m`'s `profiles` are
    already scoped, so this function renders them unconditionally. The
    default `whole_fleet=True` keeps every existing caller unchanged."""
    asof = orient_time.now_label()
    t = thresholds or alert_policy_store.DEFAULTS["kb.recall"]
    scoped_note = ds.empty_state(
        "Fleet-wide; shown to a whole-fleet scope only.")

    # Panel 1 — retrieval quality.
    if not whole_fleet:
        eval_panel = f'<section><h2>Retrieval quality</h2>{scoped_note}</section>'
    elif ev:
        o = ev["overall"]
        k = ev["k"]
        rk = f"recall@{k}"
        tip_rk = f"Share of golden queries whose expected doc lands in the top {k} — the eval cutoff."
        cards = "".join([
            _card(_pct(o.get("recall@1")), "recall@1", cls_recall(o.get("recall@1"), t), TIPS["recall1"],
                  status_recall(o.get("recall@1"), t)),
            _card(_pct(o.get("recall@5")), "recall@5", cls_recall(o.get("recall@5"), t), TIPS["recall5"],
                  status_recall(o.get("recall@5"), t)),
            _card(_pct(o.get(rk)), rk, cls_recall(o.get(rk), t), tip_rk,
                  status_recall(o.get(rk), t)),
            _card(f'{o.get("mrr", 0):.2f}', "MRR", cls_recall(o.get("mrr"), t), TIPS["mrr"],
                  status_recall(o.get("mrr"), t)),
            # golden cases count appears in the panel header badge; no StatCard needed.
        ])
        style_rows = "".join(
            f'<tr><td data-label="Style" class="k">{escape(s)}</td>'
            f'<td data-label="n">{b.get("n","")}</td>'
            f'<td data-label="r@1" class="{cls_recall(b.get("recall@1"), t)}">{_pct(b.get("recall@1"))}</td>'
            f'<td data-label="r@5" class="{cls_recall(b.get("recall@5"), t)}">{_pct(b.get("recall@5"))}</td>'
            f'<td data-label="{rk}" class="{cls_recall(b.get(rk), t)}">{_pct(b.get(rk))}</td>'
            f'<td data-label="MRR">{b.get("mrr","—")}</td></tr>'
            for s, b in ev.get("by_style", {}).items() if b.get("n"))
        kind_rows = "".join(
            f'<tr><td data-label="Kind" class="k">{escape(kd)}</td>'
            f'<td data-label="n">{b.get("n","")}</td>'
            f'<td data-label="{rk}" class="{cls_recall(b.get(rk), t)}">{_pct(b.get(rk))}</td>'
            f'<td data-label="MRR">{b.get("mrr","—")}</td></tr>'
            for kd, b in ev.get("by_kind", {}).items() if b.get("n"))
        miss_rows = "".join(
            f'<tr><td data-label="Style" class="k">{escape(mi.get("style",""))}</td>'
            f'<td data-label="Query" style="text-align:left">{escape(mi.get("query") or "")}</td>'
            f'<td data-label="Expected" style="text-align:left" class="muted">{escape(", ".join(mi.get("expect", [])))}</td></tr>'
            for mi in ev.get("misses", []))
        n_miss = len(ev.get("misses", []))
        miss_block = (
            f'<h3>{_tip(f"misses ({n_miss})", TIPS["misses"])}</h3>'
            f'<div class="data-table-wrap"><table class="data-table"><thead><tr>'
            f'<th>{_tip("style", TIPS["style"])}</th>'
            f'<th style="text-align:left">query</th>'
            f'<th style="text-align:left">expected</th>'
            f'</tr></thead><tbody>{miss_rows}</tbody></table></div>'
        ) if ev.get("misses") else '<p class="muted">No misses — every golden query lands in top-k.</p>'
        gen = escape(orient_time.fmt(ev.get("generated", ""), assume_utc=True))
        eval_panel = f"""
  <section>
    <h2>{_tip("Retrieval quality", TIPS["eval_panel"])} <span class="pill">golden set · {escape(str(ev.get('cases','')))} cases · k={k}</span></h2>
    <div class="cards">{cards}</div>
    <div class="cols">
      <div><h3>by query style</h3>
        <div class="data-table-wrap"><table class="data-table"><thead><tr>
        <th>{_tip("style", TIPS["style"])}</th><th>{_tip("n", TIPS["n"])}</th>
        <th>{_tip("r@1", TIPS["recall1"])}</th><th>{_tip("r@5", TIPS["recall5"])}</th>
        <th>{_tip(rk, tip_rk)}</th><th>{_tip("MRR", TIPS["mrr"], right=True)}</th>
        </tr></thead><tbody>{style_rows}</tbody></table></div></div>
      <div><h3>by doc kind</h3>
        <div class="data-table-wrap"><table class="data-table"><thead><tr>
        <th>{_tip("kind", TIPS["kind"])}</th><th>{_tip("n", TIPS["n"])}</th>
        <th>{_tip(rk, tip_rk)}</th><th>{_tip("MRR", TIPS["mrr"], right=True)}</th>
        </tr></thead><tbody>{kind_rows}</tbody></table></div></div>
    </div>
    {miss_block}
    <p class="sub">eval generated {gen}</p>
  </section>"""
    else:
        eval_panel = '<section><h2>Retrieval quality</h2><p class="muted">No kb-eval.json yet — run <code>kb-eval</code>.</p></section>'

    # Panel 2 — live queries.
    if not whole_fleet:
        q_panel = f'<section><h2>Live queries</h2>{scoped_note}</section>'
    elif q["total"]:
        span = ""
        if q["first"] and q["last"]:
            span = f'{q["first"][:10]} → {q["last"][:10]}'
        qcards = "".join([
            _card(str(q["total"]), "tool calls", "", TIPS["total"]),
            _card(str(q["searches"]), "searches", "", TIPS["searches"]),
            _card(_pct(q["empty_rate"]), "null-result rate",
                  "bad" if (q["empty_rate"] or 0) > 0.2 else "warn" if (q["empty_rate"] or 0) > 0.05 else "good",
                  TIPS["empty_rate"]),
            _card(_pct(q["hitlimit_rate"]), "hit-limit rate",
                  "warn" if (q["hitlimit_rate"] or 0) > 0.2 else "good", TIPS["hitlimit_rate"]),
            _card(f'{q["p50"]:.1f}ms' if q["p50"] is not None else "—", "latency p50", "", TIPS["p50"]),
            _card(f'{q["p95"]:.1f}ms' if q["p95"] is not None else "—", "latency p95", "", TIPS["p95"]),
            _card(f'{q["orchestrated"]} / {q["interactive"]}', "orchestrated / interactive", "", TIPS["orch"]),
        ])
        null_rows = "".join(
            f'<tr><td data-label="Query" style="text-align:left">{escape(query)}</td>'
            f'<td data-label="Count">{count}</td></tr>'
            for query, count in q["top_nulls"])
        null_block = (
            f'<h3>{_tip("top null-result queries", TIPS["nulls"])}</h3>'
            f'<div class="data-table-wrap"><table class="data-table"><thead><tr>'
            f'<th style="text-align:left">query</th><th>count</th>'
            f'</tr></thead><tbody>{null_rows}</tbody></table></div>'
        ) if q["top_nulls"] else '<p class="muted">No null-result searches logged.</p>'
        q_panel = f"""
  <section>
    <h2>{_tip("Live queries", TIPS["query_panel"])} <span class="pill">{escape(span)}</span></h2>
    <div class="cards">{qcards}</div>
    {null_block}
  </section>"""
    else:
        q_panel = ('<section><h2>Live queries</h2><p class="muted">No query traffic logged yet. '
                   'kb-queries.jsonl fills as co sessions cycle onto the instrumented server.</p></section>')

    # Panel 3 — individuation coverage.
    if m["total"]:
        outcomes = " · ".join(f"{escape(k)} {v}" for k, v in sorted(m["by_outcome"].items()))
        prof_rows = "".join(
            f'<tr><td data-label="Profile" class="k">{escape(p["project"] or "")}/{escape(p["profile"] or "")}</td>'
            f'<td data-label="Agents">{p["agents"]}</td>'
            f'<td data-label="Skills">{p["skills"]}</td>'
            f'<td data-label="Baked in">{p["individuated"]}</td>'
            f'<td data-label="Live-only" class="muted">{p["live_only"]}</td>'
            f'<td data-label="Matched">{p["total_docs"]}</td></tr>'
            for p in m["profiles"])
        m_panel = f"""
  <section>
    <h2>{_tip("Individuation coverage", TIPS["mat_panel"])} <span class="pill">{m['total']} activations · {escape(outcomes)}</span></h2>
    <div class="data-table-wrap"><table class="data-table"><thead><tr>
      <th>{_tip("profile", TIPS["profile"])}</th><th>{_tip("agents", TIPS["agents"])}</th>
      <th>{_tip("skills", TIPS["skills"])}</th><th>{_tip("baked in", TIPS["baked"])}</th>
      <th>{_tip("live-only", TIPS["live_only"], right=True)}</th>
      <th>{_tip("matched", TIPS["matched"], right=True)}</th>
    </tr></thead><tbody>{prof_rows}</tbody></table></div>
    <p class="sub">"baked in" = agents+skills written to .claude/; "live-only" = docs the profile matched but left to MCP query.</p>
  </section>"""
    else:
        m_panel = ('<section><h2>Individuation coverage</h2><p class="muted">No individuations logged yet. '
                   'kb-materializations.jsonl fills as <code>knowledge-ctx use</code> runs on session start.</p></section>')

    if whole_fleet:
        audit_panel = _audit_panel(audit, dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))
    else:
        audit_panel = f'<section><h2>KB write audit</h2>{scoped_note}</section>'
    banner = ds.stalled_banner_html("KB golden-set eval", freshness(ev))
    alert_form = ds.alert_threshold_form_html(
        key="kb.recall", title="Recall/MRR alert threshold",
        fields=[
            ("good_min", "Good ≥", t["good_min"], "0.01"),
            ("warn_min", "Warn ≥", t["warn_min"], "0.01"),
        ])
    body = (
        f'<div class="page-head">'
        f'<h1>KB retrieval observability</h1>'
        f'<p class="page-sub">how effectively the knowledge index serves agents · built {escape(asof)} · ARCH-121'
        f' · hover any metric label for what it measures and what events feed it</p>'
        f'</div>'
        f'{banner}'
        f'{alert_form}'
        f'{eval_panel}'
        f'{q_panel}'
        f'{m_panel}'
        f'{audit_panel}'
    )
    return ds.page_shell(
        title="KB retrieval",
        nav_html=ds.nav_component("/kb", "full"),
        body=body,
        extra_css=_PAGE_CSS,
    )


def load_query_stats() -> dict:
    """query_stats() over the current live query log. Reused by alerts_build.py's
    /alerts evaluator so it reads the exact same aggregation this page renders
    from, not a re-implementation of it."""
    return query_stats(_read_jsonl(QUERY_LOG))


def live_html(scope: frozenset[str] = frozenset(), whole_fleet: bool = True) -> str:
    """Render /kb from current data at request time (ARCH-182). Cheap: reads the
    last eval JSON + the live query and individuation logs — so the query/latency
    stats are truly current, not frozen at the daily build. refresh_eval() (the
    heavy golden-set re-run) stays in the scheduled build(); build() also writes
    the static file as a fallback.

    Fetches the live recall/MRR threshold fresh on every call (no cache) so a
    write made from this same page's own form reaches every colored cell and
    the write form's displayed values on the very next request.

    `scope`/`whole_fleet` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): the
    materializations rows are filtered to `scope` (each row carries a
    `project` field) before `mat_stats` ever sums them; `render`'s own
    `whole_fleet` gate hides the three corpus-/repo-wide panels it can't
    filter at all."""
    thresholds = alert_policy_store.get("kb.recall")
    mat_rows = _read_jsonl(MAT_LOG)
    if not whole_fleet:
        mat_rows = [r for r in mat_rows if r.get("project") in scope]
    return render(load_eval(), load_query_stats(), mat_stats(mat_rows), thresholds,
                  audit=load_audit(), whole_fleet=whole_fleet)


def build() -> None:
    """Refreshes the golden-set eval (the heavy, scheduled part of this job),
    then writes the STATIC FALLBACK deliberately empty (E19·4,
    issue_01M35K5JQ7Z7MHYZGWRSB5F85M) — this used to write the current, real,
    unscoped report (including every project's individuation-coverage row),
    which a scoped viewer would see on any live-render failure (the same leak
    class 26bb079 fixed for crews/signals/engineering/roster). `refresh_eval`
    still runs on every scheduled build so the golden-set numbers the live
    page reads stay current; only the file this writes is now empty."""
    OUT.mkdir(parents=True, exist_ok=True)
    refreshed = refresh_eval()
    fallback = ds.page_shell(
        title="KB retrieval", nav_html=ds.nav_component("/kb", "full"),
        body=ds.empty_state("Live view unavailable — reload in a moment."),
        extra_css=_PAGE_CSS)
    (OUT / "kb.html").write_text(fallback, encoding="utf-8")
    print(f"Wrote site/kb.html (empty static fallback, eval_refreshed="
          f"{'yes' if refreshed else 'no'}).")


if __name__ == "__main__":
    build()
