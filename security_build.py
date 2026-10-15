#!/usr/bin/env python3.11
"""security_build.py — the /security dashboard: the security-review catch record for
agent-generated PRs (ARCH-306). Reads the rollup that agent-platform
`security-scorecard` writes to ~/Convivy/agent-runs/scorecard/security-report.json
and emits a self-contained site/security.html. Full-tier only (server.py FULL_ONLY).

The premise it measures (overnight team, 2026-07-02): security-reviewer is a
compensating control for a documented structural deficit in agent-written code, so
the control has to be *measured* — the per-unit rate at which security findings
surface on agent PRs — not assumed. This surfaces that rate, split by the producer
model (does cheaper-model code carry more vulns per KLOC?) and by repo."""
from __future__ import annotations

import json
from html import escape
from pathlib import Path

import crew_registry
import design_system as ds
import orient_time
from paths import STATE_ROOT

OUT = STATE_ROOT / "site"
DATA_PATH = Path.home() / "Convivy" / "agent-runs" / "scorecard" / "security-report.json"


def load() -> dict:
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"generated_at": "", "window_days": None, "overall": {}, "by_model": {}, "by_repo": {}}


# agent-platform's the security-scorecard job's plist runs this harvest daily
# at 07:45 MST. 30h tolerates a late-running harvest without hiding a feed
# that's gone dark for the better part of two days.
_STALLED_AFTER_HOURS = 30
_EXPECTED_CADENCE = "daily (07:45 MST)"


def freshness(data: dict) -> ds.Freshness:
    """This dashboard's own freshness verdict for /security's scorecard —
    read verbatim by /security's own page and by its /overview card
    (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment 902)."""
    gen = data.get("generated_at")
    text = orient_time.fmt(gen) or "no freshness reported"
    hours = orient_time.hours_since(gen)
    stalled = hours is not None and hours > _STALLED_AFTER_HOURS
    return ds.Freshness(
        text=text, stalled=stalled, cadence=_EXPECTED_CADENCE,
        reason="The daily security-scorecard harvest may not have run.",
    )


_PAGE_CSS = r"""
.page-head { margin:24px 0 0; }
.page-head h1 { font-size:22px; font-weight:700; letter-spacing:-.01em; color:var(--ink); }
.page-sub { font-size:13px; color:var(--ink-2); margin-top:4px; margin-bottom:16px; max-width:64ch; }

.statcards { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:16px 0 6px; }
@media (max-width:640px){ .statcards { grid-template-columns:repeat(2,1fr); } }
@media (max-width:360px){ .statcards { grid-template-columns:1fr; } }

.sec-h2 { font-size:14px; font-weight:650; color:var(--ink); margin:26px 0 8px; }

.data-table th, .data-table td { text-align:right; white-space:nowrap; }
.data-table th:first-child, .data-table td:first-child { text-align:left; }
td.k { font-weight:600; }
.data-table tbody tr:hover td { background:var(--brand-wash); }
tr.total td { border-top:2px solid var(--line); font-weight:700; background:var(--surface-2); }

.muted { color:var(--ink-2); }
.good { color:var(--ok); font-weight:600; }
.warn { color:var(--warn); font-weight:600; }
.bad  { color:var(--err); font-weight:600; }

.notes { color:var(--ink-2); font-size:12px; line-height:1.65; margin:22px 0 0;
         padding:12px 14px 12px 26px; border-left:2px solid var(--brand);
         background:var(--surface); border-radius:4px; }
.notes b { color:var(--ink); font-weight:650; }
.notes code { font-variant-numeric:tabular-nums; }
.empty { color:var(--ink-2); padding:36px; text-align:center; }
"""


def _num(x, digits=0) -> str:
    if x is None:
        return "—"
    if digits == 0:
        return f"{x:,.0f}" if isinstance(x, (int, float)) else escape(str(x))
    return f"{x:,.{digits}f}"


def _blk_cls(rate: float) -> str:
    """Signal class for a blocking-per-KLOC rate. No policy threshold is set yet
    (that's the baseline-then-decide step), so this is a soft read only: zero is
    clean, anything above is flagged for attention, not failure."""
    if rate <= 0:
        return "good"
    if rate < 0.5:
        return "warn"
    return "bad"


def _rate_cell(rate: float) -> str:
    return f'<span class="{_blk_cls(rate)}">{_num(rate, 2)}</span>'


def _rollup_rows(bucket: dict[str, dict], label_key: str) -> str:
    rows = []
    # order by blocking-per-KLOC desc so the highest-risk producer/repo reads first
    for name in sorted(bucket, key=lambda k: bucket[k].get("blocking_per_kloc", 0), reverse=True):
        m = bucket[name]
        rows.append(
            "<tr>"
            f'<td data-label="{escape(label_key)}" class="k">{escape(name)}</td>'
            f'<td data-label="PRs">{_num(m.get("prs_reviewed"))}</td>'
            f'<td data-label="Changed LOC">{_num(m.get("changed_loc"))}</td>'
            f'<td data-label="Findings">{_num(m.get("total_findings"))}</td>'
            f'<td data-label="Blocking">{_num(m.get("blocking_findings"))}</td>'
            f'<td data-label="Blk / KLOC">{_rate_cell(m.get("blocking_per_kloc", 0))}</td>'
            f'<td data-label="% w/ blocking">{_num(m.get("pct_prs_with_blocking"), 1)}%</td>'
            "</tr>"
        )
    return "".join(rows)


def _table(bucket: dict[str, dict], first_col: str) -> str:
    if not bucket:
        return '<div class="empty">No data in the window yet.</div>'
    heads = [first_col, "PRs", "Changed LOC", "Findings", "Blocking", "Blk / KLOC", "% w/ blocking"]
    ths = "".join(f"<th>{escape(h)}</th>" for h in heads)
    return ('<div class="data-table-wrap"><table class="data-table"><thead><tr>'
            + ths + "</tr></thead><tbody>" + _rollup_rows(bucket, first_col)
            + "</tbody></table></div>")


def render(data: dict, scope: frozenset[str] = frozenset(), whole_fleet: bool = True) -> str:
    """`scope`/`whole_fleet` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M):
    `by_repo` is keyed by repo name, so a row the registry lists under no
    project in `scope` is dropped before its table is built. `overall` and
    `by_model` carry no repo dimension at all — no PR-level breakdown this
    file can filter them by — so they render only for a whole-fleet viewer;
    a scoped viewer sees the (now-filtered) "By repo" table and nothing
    else. The defaults (`whole_fleet=True`) keep every existing caller
    (the static builder, direct callers in tests) unchanged."""
    fresh = freshness(data)
    win = data.get("window_days")
    o = data.get("overall") or {}
    by_repo = data.get("by_repo") or {}
    if not whole_fleet:
        repos = crew_registry.repos_in_scope(scope)
        by_repo = {name: m for name, m in by_repo.items() if name in repos}

    if whole_fleet and not o.get("prs_reviewed"):
        body = (
            '<div class="page-head"><h1>Security-success</h1>'
            f'<p class="page-sub">Security-review catch record for agent PRs. '
            f'As of {escape(orient_time.now_label())}.</p></div>'
            '<div class="empty">No security-scorecard data yet — the '
            '<code>security-scorecard</code> harvester has not run.</div>'
        )
        return ds.page_shell(title="security-success",
                             nav_html=ds.nav_component("/security", "full"),
                             body=body, extra_css=_PAGE_CSS)
    if not whole_fleet and not by_repo:
        body = (
            '<div class="page-head"><h1>Security-success</h1>'
            f'<p class="page-sub">Security-review catch record for agent PRs. '
            f'As of {escape(orient_time.now_label())}.</p></div>'
            '<div class="empty">No security-scorecard data yet for a project in your scope.</div>'
        )
        return ds.page_shell(title="security-success",
                             nav_html=ds.nav_component("/security", "full"),
                             body=body, extra_css=_PAGE_CSS)

    win_label = f"trailing {win} days" if win else "all recorded"
    head = (
        '<div class="page-head"><h1>Security-success</h1>'
        f'<p class="page-sub">The per-unit rate at which <b>security-reviewer</b> surfaces findings on '
        f'agent-generated PRs — the measured catch record of the compensating control, {escape(win_label)}. '
        f'As of {escape(fresh.text)}.</p></div>'
        + ds.stalled_banner_html("Security scorecard", fresh)
    )
    fleet_only = ""
    if whole_fleet:
        cards = "".join([
            ds.stat_card("Blocking / KLOC", _num(o.get("blocking_per_kloc"), 2)),
            ds.stat_card("PRs w/ a blocking vuln", f'{_num(o.get("pct_prs_with_blocking"), 1)}%'),
            ds.stat_card("Agent PRs reviewed", _num(o.get("prs_reviewed"))),
            ds.stat_card("Internal escapes", _num(o.get("internal_escapes"))),
        ])
        fleet_only = (
            f'<div class="statcards">{cards}</div>'
            + ds.awaiting_state(
                shows='"Internal escapes" is a proxy — blocking findings caught late, not '
                      'confirmed damage in production.',
                needs="a look-back feed (a security revert or incident tied to its PR) that "
                      "the security-scorecard harvester does not build yet",
                owner="keel", verified="2026-08-26",
                # render() already holds the value that would falsify this claim: once
                # the harvester starts writing `overall.escaped_to_prod`, this panel
                # must say so at that render rather than waiting up to 14 days for the
                # `verified` stamp to go stale.
                contradicted_by=(
                    None if o.get("escaped_to_prod") is None else
                    f"escaped_to_prod is now {o['escaped_to_prod']!r} in the "
                    "harvester's own report — the look-back feed has landed. The "
                    "claim is wrong, not the capability."))
            + '<div class="sec-h2">By producer model</div>'
            + _table(data.get("by_model") or {}, "Model")
        )
    body = (
        head
        + fleet_only
        + '<div class="sec-h2">By repo</div>'
        + _table(by_repo, "Repo")
        + '<ul class="notes" style="list-style:none;padding-left:26px">'
        '<li><b>Findings per KLOC</b> counts security-reviewer findings (blocking + non-blocking) '
        'against the PR\'s changed lines (additions + deletions), so a small and a large PR compare fairly. '
        '<b>Blocking</b> = a real vulnerability that should not ship as-is; <b>non-blocking</b> = a security '
        'concern that\'s real risk but not a hard vuln.</li>'
        '<li><b>% PRs with a blocking vuln</b> is the share of reviewed agent PRs that carried at least one '
        'blocking security finding — the headline "how often does agent code ship a caught vulnerability" rate.</li>'
        '<li><b>Internal escapes</b> are blocking findings that first surfaced on review round ≥2 — the '
        'earlier round missed them. This is the measurable proxy for "escaped to later".</li>'
        '<li>The rate colouring is a soft read (zero clean, above flagged), <b>not a policy threshold</b> — '
        'the threshold gets set from this baseline once enough is measured.</li>'
        '<li>Verdicts and finding counts are parsed from the stamped ARCH-033 review comments '
        '(<code>agentctl review-post</code>), so each is authentic and pinned to the exact commit reviewed.</li>'
        '</ul>'
    )
    return ds.page_shell(title="security-success",
                         nav_html=ds.nav_component("/security", "full"),
                         body=body, extra_css=_PAGE_CSS)


def live_html(scope: frozenset[str] = frozenset(), whole_fleet: bool = True) -> str:
    """Render /security from the current security-report.json at request
    time (ARCH-182), restricted to `scope` (E19·4,
    issue_01M35K5JQ7Z7MHYZGWRSB5F85M). Cheap: one JSON read.
    `security-scorecard` stays the data producer."""
    return render(load(), scope, whole_fleet)


def main() -> None:
    """Seed the static fallback, deliberately empty (E19·4,
    issue_01M35K5JQ7Z7MHYZGWRSB5F85M) — this used to write the current, real,
    unscoped report, which a scoped viewer would see on any live-render
    failure (the same leak class 26bb079 fixed for crews/signals/engineering/
    roster)."""
    OUT.mkdir(exist_ok=True)
    fallback = ds.page_shell(
        title="security-success", nav_html=ds.nav_component("/security", "full"),
        body=ds.empty_state("Live view unavailable — reload in a moment."),
        extra_css=_PAGE_CSS)
    (OUT / "security.html").write_text(fallback, encoding="utf-8")
    print("Wrote site/security.html (empty static fallback)")


if __name__ == "__main__":
    main()
