#!/usr/bin/env python3.11
"""warrant_build.py — the /warrant dashboard: did agent-merged work deserve to
exist? (ARCH-309). The spurious-work axis, sibling to /security. Reads the rollup
that agent-platform `warrant-scorecard` writes to
~/Convivy/agent-runs/scorecard/warrant-report.json and emits a self-contained
site/warrant.html. Full-tier only (server.py FULL_ONLY).

A hallucinated-but-correct PR (an agent invents a need from data, builds it, it
merges) scores positively on every other axis — merged, survives, +velocity, no
security findings — so it's invisible unless measured here. The signal is the
revert, classified by reason: `unwarranted` (the change shouldn't have existed —
the headline spurious-work rate) vs `bug` (a quality-escape) vs others."""
from __future__ import annotations

import json
from html import escape
from pathlib import Path

import crew_registry
import design_system as ds
import orient_time
from paths import STATE_ROOT

OUT = STATE_ROOT / "site"
DATA_PATH = Path.home() / "Convivy" / "agent-runs" / "scorecard" / "warrant-report.json"


def load() -> dict:
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"generated_at": "", "window_days": None, "overall": {}, "by_model": {}, "by_repo": {}}


# agent-platform's the warrant-scorecard job's plist runs this harvest daily at
# 07:50 MST. 30h tolerates a late-running harvest without hiding a feed
# that's gone dark for the better part of two days.
_STALLED_AFTER_HOURS = 30
_EXPECTED_CADENCE = "daily (07:50 MST)"


def freshness(data: dict) -> ds.Freshness:
    """This dashboard's own freshness verdict for /warrant's scorecard — read
    verbatim by /warrant's own page and by its /overview card
    (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment 902)."""
    gen = data.get("generated_at")
    text = orient_time.fmt(gen) or "no freshness reported"
    hours = orient_time.hours_since(gen)
    stalled = hours is not None and hours > _STALLED_AFTER_HOURS
    return ds.Freshness(
        text=text, stalled=stalled, cadence=_EXPECTED_CADENCE,
        reason="The daily warrant-scorecard harvest may not have run.",
    )


_PAGE_CSS = r"""
.page-head { margin:24px 0 0; }
.page-head h1 { font-size:22px; font-weight:700; letter-spacing:-.01em; color:var(--ink); }
.page-sub { font-size:13px; color:var(--ink-2); margin-top:4px; margin-bottom:16px; max-width:66ch; }
.statcards { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:16px 0 6px; }
@media (max-width:640px){ .statcards { grid-template-columns:repeat(2,1fr); } }
@media (max-width:360px){ .statcards { grid-template-columns:1fr; } }
.sec-h2 { font-size:14px; font-weight:650; color:var(--ink); margin:26px 0 8px; }
.data-table th, .data-table td { text-align:right; white-space:nowrap; }
.data-table th:first-child, .data-table td:first-child { text-align:left; }
td.k { font-weight:600; }
.data-table tbody tr:hover td { background:var(--brand-wash); }
.muted { color:var(--ink-2); }
.good { color:var(--ok); font-weight:600; }
.warn { color:var(--warn); font-weight:600; }
.bad  { color:var(--err); font-weight:600; }
.notes { color:var(--ink-2); font-size:12px; line-height:1.65; margin:22px 0 0;
         padding:12px 14px 12px 26px; border-left:2px solid var(--brand);
         background:var(--surface); border-radius:4px; list-style:none; }
.notes li { margin-bottom:5px; }
.notes b { color:var(--ink); font-weight:650; }
.notes code { font-variant-numeric:tabular-nums; }
.clean { color:var(--ink-2); padding:20px; border:1px dashed var(--line); border-radius:8px;
         background:var(--surface); margin:8px 0; }
.empty { color:var(--ink-2); padding:36px; text-align:center; }
"""


def _num(x, digits=0):
    if x is None:
        return "—"
    return f"{x:,.{digits}f}" if isinstance(x, (int, float)) else escape(str(x))


def _rate_cls(rate: float) -> str:
    # No policy threshold set (measure-first). Zero clean; any spurious work flagged.
    return "good" if rate <= 0 else ("warn" if rate < 2 else "bad")


def _rows(bucket: dict[str, dict], first_col: str) -> str:
    out = []
    for name in sorted(bucket, key=lambda k: (bucket[k].get("unwarranted_rate", 0),
                                              bucket[k].get("revert_rate", 0)), reverse=True):
        m = bucket[name]
        ur = m.get("unwarranted_rate", 0)
        out.append(
            "<tr>"
            f'<td data-label="{escape(first_col)}" class="k">{escape(name)}</td>'
            f'<td data-label="Merged">{_num(m.get("merged_prs"))}</td>'
            f'<td data-label="Reverted">{_num(m.get("reverted"))} <span class="muted">({_num(m.get("revert_rate"),1)}%)</span></td>'
            f'<td data-label="Unwarranted"><span class="{_rate_cls(ur)}">{_num(m.get("unwarranted"))} ({_num(ur,1)}%)</span></td>'
            f'<td data-label="Bug-reverts">{_num(m.get("bug_reverts"))}</td>'
            "</tr>"
        )
    return "".join(out)


def _table(bucket: dict[str, dict], first_col: str) -> str:
    if not bucket:
        return '<div class="empty">No data in the window yet.</div>'
    heads = [first_col, "Merged", "Reverted", "Unwarranted", "Bug-reverts"]
    ths = "".join(f"<th>{escape(h)}</th>" for h in heads)
    return ('<div class="data-table-wrap"><table class="data-table"><thead><tr>'
            + ths + "</tr></thead><tbody>" + _rows(bucket, first_col) + "</tbody></table></div>")


def render(data: dict, scope: frozenset[str] = frozenset(), whole_fleet: bool = True) -> str:
    """`scope`/`whole_fleet` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M):
    `by_repo` is keyed by repo name, so a row the registry lists under no
    project in `scope` is dropped before its table is built. `overall` and
    `by_model` carry no repo dimension this file can filter them by, so
    they render only for a whole-fleet viewer; a scoped viewer sees the
    (now-filtered) "By repo" table and nothing else. The defaults
    (`whole_fleet=True`) keep every existing caller unchanged."""
    fresh = freshness(data)
    win = data.get("window_days")
    o = data.get("overall") or {}
    win_label = f"trailing {win} days" if win else "all recorded"
    by_repo = data.get("by_repo") or {}
    if not whole_fleet:
        repos = crew_registry.repos_in_scope(scope)
        by_repo = {name: m for name, m in by_repo.items() if name in repos}

    if whole_fleet and not o.get("merged_prs"):
        body = ('<div class="page-head"><h1>Warrant</h1>'
                f'<p class="page-sub">Did agent-merged work deserve to exist? '
                f'As of {escape(orient_time.now_label())}.</p></div>'
                '<div class="empty">No warrant-scorecard data yet — the '
                '<code>warrant-scorecard</code> harvester has not run.</div>')
        return ds.page_shell(title="warrant", nav_html=ds.nav_component("/warrant", "full"),
                             body=body, extra_css=_PAGE_CSS)
    if not whole_fleet and not by_repo:
        body = ('<div class="page-head"><h1>Warrant</h1>'
                f'<p class="page-sub">Did agent-merged work deserve to exist? '
                f'As of {escape(orient_time.now_label())}.</p></div>'
                '<div class="empty">No warrant-scorecard data yet for a project in your scope.</div>')
        return ds.page_shell(title="warrant", nav_html=ds.nav_component("/warrant", "full"),
                             body=body, extra_css=_PAGE_CSS)

    head = (
        '<div class="page-head"><h1>Warrant</h1>'
        f'<p class="page-sub">Did agent-merged work deserve to exist? The <b>unwarranted-merge rate</b> — '
        f'agent PRs later reverted because the change shouldn\'t have been built (a hallucinated premise), '
        f'not because of a bug — over the {escape(win_label)}. As of {escape(fresh.text)}.</p></div>'
        + ds.stalled_banner_html("Warrant scorecard", fresh)
    )
    fleet_only = ""
    if whole_fleet:
        cards = "".join([
            ds.stat_card("Unwarranted rate", f'{_num(o.get("unwarranted_rate"), 1)}%'),
            ds.stat_card("Reverted", f'{_num(o.get("reverted"))} ({_num(o.get("revert_rate"), 1)}%)'),
            ds.stat_card("Merged agent PRs", _num(o.get("merged_prs"))),
            ds.stat_card("Bug-reverts",
                        f'{_num(o.get("bug_reverts"))} ({_num(o.get("bug_revert_rate"), 1)}%)'),
        ])
        clean_banner = ""
        if not o.get("reverted"):
            clean_banner = ('<div class="clean">No reverts recorded in this window — nothing has been '
                            'flagged as unwarranted or reverted for a bug yet. The rate populates as revert '
                            'PRs land carrying a <code>Revert-reason:</code> tag.</div>')
        fleet_only = (
            f'<div class="statcards">{cards}</div>' + clean_banner
            + '<div class="sec-h2">By producer model</div>' + _table(data.get("by_model") or {}, "Model")
        )
    body = (
        head
        + fleet_only
        + '<div class="sec-h2">By repo</div>' + _table(by_repo, "Repo")
        + '<ul class="notes">'
        '<li>A hallucinated-but-correct PR scores <b>positively</b> on every other axis — merged, survives '
        '7 days, +velocity, zero security findings — so it\'s invisible unless measured here. Code review '
        'can\'t catch it: the defect is the premise, not the code.</li>'
        '<li><b>Unwarranted</b> = reverted because the change shouldn\'t have existed (the spurious-work '
        'rate, the headline). <b>Bug-reverts</b> = reverted because it broke something (a quality-escape, '
        'complementing the survival metric). The two need opposite fixes: unwarranted → better upstream '
        'task-grounding; bug → tighter review/CI.</li>'
        '<li>The signal is the revert, classified by a <code>Revert-reason:</code> tag on the revert PR '
        '(<code>unwarranted | bug | superseded | other</code>). A detected revert with no tag reads '
        '<code>unclassified</code> until a human labels it — never silently dropped.</li>'
        '<li>The rate colouring is a soft read (zero clean, above flagged), <b>not a policy threshold</b> — '
        'that gets set from the accumulated baseline once there\'s enough to measure.</li>'
        '</ul>'
    )
    return ds.page_shell(title="warrant", nav_html=ds.nav_component("/warrant", "full"),
                         body=body, extra_css=_PAGE_CSS)


def live_html(scope: frozenset[str] = frozenset(), whole_fleet: bool = True) -> str:
    """Render /warrant from the current warrant-report.json at request time
    (ARCH-182), restricted to `scope` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M).
    Cheap: one JSON read. `warrant-scorecard` stays the data producer."""
    return render(load(), scope, whole_fleet)


def main() -> None:
    """Seed the static fallback, deliberately empty (E19·4,
    issue_01M35K5JQ7Z7MHYZGWRSB5F85M) — this used to write the current, real,
    unscoped report, which a scoped viewer would see on any live-render
    failure (the same leak class 26bb079 fixed for crews/signals/engineering/
    roster)."""
    OUT.mkdir(exist_ok=True)
    fallback = ds.page_shell(
        title="warrant", nav_html=ds.nav_component("/warrant", "full"),
        body=ds.empty_state("Live view unavailable — reload in a moment."),
        extra_css=_PAGE_CSS)
    (OUT / "warrant.html").write_text(fallback, encoding="utf-8")
    print("Wrote site/warrant.html (empty static fallback)")


if __name__ == "__main__":
    main()
