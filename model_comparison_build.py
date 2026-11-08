#!/usr/bin/env python3.11
"""/model-comparison — finished runs grouped by agent and model (CQ·156,
issue_01M57NH489EASKMATJ8QFED5EC; child of E25, issue_01M52PBQYARTX1J45MMS6EFM4A,
"the operator sets each agent's model and compares results in Orient").

**Read live, on every request, from the Library's `model_resolution`
signals** (`library_client.list_finished_runs`) — the same typed read
`chart_house_build` and Home already share for finished-run cost, widened
here with the two fields this page adds (`library_client.FinishedRun.
model_resolved`, `.exit_status`). No TTL cache, no module-level dict: a run
that just finished is in the next render.

**Grouping is by `persona` ("agent"), by `model_resolved`, or both** — never
by `model`, the setting in force when a run was dispatched. A run whose
model differs from the current setting is counted under what it actually
ran on (the DoD's own wording), which is `model_resolved`, not `model`.

**Viewer scoping is applied before grouping, not after.** `scope` filters
`list_finished_runs`'s result first; the project filter's own option list is
built from what survives that filter, so a hand-edited `?project=` naming a
project outside `scope` can only narrow an already-scoped set to nothing —
there is no path from an out-of-scope project to a count, a filter option, or
a drill-down row.

**No group is hidden for being small.** A group of one run shows "1 run",
not a suppressed row — the DoD's own "a small group shows its run count
rather than being hidden."

**Filters are `by`, `project`, `from`/`to`, off the query string** — handled
as a literal route in server.py (like `/crews/runs` and
`/engineering/policy`), not through `_full_only_routes()`, for the same
reason both of those are: that dispatcher calls its renderer with zero
arguments. No static fallback, for the same reason `/engineering/policy`
has none — a stale copy of a cost/completion figure is the one thing this
page is built not to hold, so a render failure is a plain 500 instead.

Full-tier only (server.py gates /model-comparison); a contractor gets 404.
Linked from the Dashboards Overview grid (`overview_build`), not from the
pinned five-item subnav (`design_system._DASHBOARD_SUBNAV`) — see
`design_system._DASHBOARD_ONLY_PATHS`, the same place every other
grid-only dashboard is named so `_active_area` still resolves it to
/overview.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from html import escape

import design_system as ds
import library_client
import orient_time

# A custom `?from=`/`?to=` is clamped to this regardless of what a
# hand-edited URL asks for, so the Library read this drives stays bounded —
# the same discipline `crews_build.RUNS_MAX_WINDOW_DAYS` applies to its own
# window, widened here because a model/agent comparison wants more history
# than a crew's recent-runs drill-down does.
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 90

_UNKNOWN = "(unknown)"

# (query value, column label) — the three groupings the DoD names: "by
# agent (meta.persona), by model (meta.model_resolved), or both."
_BY_LABELS = (("persona", "Agent"), ("model_resolved", "Model"), ("both", "Agent & model"))
_BY_VALUES = frozenset(v for v, _ in _BY_LABELS)


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _sub_days_clamped(d: dt.date, days: int) -> dt.date:
    try:
        return d - dt.timedelta(days=days)
    except OverflowError:
        return dt.date.min


def resolve_window(query: dict | None) -> tuple[dt.date, dt.date, bool]:
    """(since, until, is_custom) from `from`/`to` query params, or the
    trailing DEFAULT_WINDOW_DAYS when neither is given — the same shape
    `crews_build.resolve_window` returns, with this page's own window
    constants."""
    query = query or {}
    today = dt.datetime.now(dt.timezone.utc).date()
    since = _parse_date(query.get("from"))
    until = _parse_date(query.get("to"))
    if since is None and until is None:
        return _sub_days_clamped(today, DEFAULT_WINDOW_DAYS - 1), today, False
    until = until or today
    since = since or _sub_days_clamped(until, DEFAULT_WINDOW_DAYS - 1)
    if since > until:
        since, until = until, since
    if (until - since).days > MAX_WINDOW_DAYS:
        since = _sub_days_clamped(until, MAX_WINDOW_DAYS)
    return since, until, True


def _group_key(run: library_client.FinishedRun, by: str) -> tuple[str, ...]:
    if by == "model_resolved":
        return (run.model_resolved or _UNKNOWN,)
    if by == "both":
        return (run.persona or _UNKNOWN, run.model_resolved or _UNKNOWN)
    return (run.persona or _UNKNOWN,)


@dataclass
class _Group:
    key: tuple[str, ...]
    runs: list[library_client.FinishedRun]

    @property
    def label(self) -> str:
        return " · ".join(self.key)

    @property
    def count(self) -> int:
        return len(self.runs)

    @property
    def _priced(self) -> list[library_client.FinishedRun]:
        return [r for r in self.runs
                if r.cost is not None and isinstance(r.cost.usd, (int, float))]

    @property
    def total_cost(self) -> float | None:
        """`None`, not `$0.00`, when no run in the group has a known cost —
        a real zero and "nothing priced" must not render alike (the
        cost.json-absent-is-meaningful convention `cost_rollup_by_project`
        already applies)."""
        priced = self._priced
        return sum(r.cost.usd for r in priced) if priced else None

    @property
    def mean_cost(self) -> float | None:
        """Divided by the PRICED count, not `self.count` — a run with no
        recorded cost still counts toward the group's run total (DoD: "the
        number of runs"), and folding it into this denominator too would
        silently deflate the mean every time one run's cost is unknown."""
        priced = self._priced
        return (sum(r.cost.usd for r in priced) / len(priced)) if priced else None

    @property
    def completed_share(self) -> float:
        return sum(1 for r in self.runs if r.exit_status == "completed") / self.count


def _group_rows(runs: list[library_client.FinishedRun], by: str) -> list[_Group]:
    buckets: dict[tuple[str, ...], list[library_client.FinishedRun]] = {}
    for r in runs:
        buckets.setdefault(_group_key(r, by), []).append(r)
    groups = [_Group(key=k, runs=v) for k, v in buckets.items()]
    # Largest sample first — the natural reading order for a comparison —
    # tie-broken by label so the order is deterministic.
    groups.sort(key=lambda g: (-g.count, g.label))
    return groups


def _fmt_cost(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "—"


def _fmt_pct(value: float) -> str:
    return f"{value:.0%}"


def _run_row_html(r: library_client.FinishedRun) -> str:
    when = orient_time.fmt(r.produced_at or r.ingested_at, assume_utc=True)
    cost = _fmt_cost(r.cost.usd if r.cost is not None else None)
    return (
        f'<tr><td data-label="Project">{escape(r.product)}</td>'
        f'<td data-label="Run">{escape(r.run or "—")}</td>'
        f'<td data-label="Agent">{escape(r.persona or "—")}</td>'
        f'<td data-label="Model">{escape(r.model_resolved or "—")}</td>'
        f'<td data-label="Cost">{escape(cost)}</td>'
        f'<td data-label="Outcome">{escape(r.exit_status or "—")}</td>'
        f'<td data-label="Produced">{escape(when or "—")}</td></tr>'
    )


_RUN_COLUMNS = ("Project", "Run", "Agent", "Model", "Cost", "Outcome", "Produced")


def _group_html(g: _Group) -> str:
    ths = "".join(f"<th>{escape(h)}</th>" for h in _RUN_COLUMNS)
    rows = "".join(
        _run_row_html(r) for r in
        sorted(g.runs, key=lambda r: r.produced_at or r.ingested_at, reverse=True))
    table = (f'<div class="data-table-wrap"><table class="data-table"><thead><tr>{ths}</tr>'
            f'</thead><tbody>{rows}</tbody></table></div>')
    return (
        f'<details class="mc-group">'
        f'<summary>'
        f'<span class="mc-group-name">{escape(g.label)}</span>'
        f'<span class="mc-group-count">{g.count} run{"s" if g.count != 1 else ""}</span>'
        f'<span class="mc-group-cost">{escape(_fmt_cost(g.total_cost))} total'
        f' · {escape(_fmt_cost(g.mean_cost))} mean</span>'
        f'<span class="mc-group-rate">{escape(_fmt_pct(g.completed_share))} completed</span>'
        f'</summary>'
        f'<div class="mc-group-body">{table}</div>'
        f'</details>'
    )


def _filters_form_html(by: str, project: str, project_options: list[str],
                       since: dt.date, until: dt.date) -> str:
    by_opts = "".join(
        f'<option value="{escape(v, quote=True)}"{" selected" if v == by else ""}>'
        f'{escape(label)}</option>'
        for v, label in _BY_LABELS)
    proj_opts = "".join(
        f'<option value="{escape(p, quote=True)}"{" selected" if p == project else ""}>'
        f'{escape(p)}</option>'
        for p in project_options)
    return (
        f'<form method="get" action="/model-comparison" class="mc-form">'
        f'<label>Group by<br><select name="by">{by_opts}</select></label>'
        f'<label>Project<br><select name="project">'
        f'<option value="">All ({len(project_options)} in scope)</option>{proj_opts}'
        f'</select></label>'
        f'<label>From<br><input type="date" name="from" '
        f'value="{escape(since.isoformat(), quote=True)}"></label>'
        f'<label>To<br><input type="date" name="to" '
        f'value="{escape(until.isoformat(), quote=True)}"></label>'
        f'<button type="submit">Apply</button></form>'
    )


def _build_body(query: dict | None, scope: frozenset[str], whole_fleet: bool) -> str:
    query = query or {}
    by = query["by"] if query.get("by") in _BY_VALUES else "persona"
    since, until, _ = resolve_window(query)
    project = (query.get("project") or "").strip()

    since_dt = dt.datetime.combine(since, dt.time.min)
    until_dt = dt.datetime.combine(until, dt.time.max)
    all_runs = library_client.list_finished_runs(since_dt, until_dt)

    # Scope first, then the option list, then the project filter — in that
    # order, so the dropdown can only ever offer a project this viewer can
    # already see, and a hand-edited `?project=` outside `scope` has nothing
    # left to match against.
    scoped_runs = [r for r in all_runs if r.product in scope]
    project_options = sorted({r.product for r in scoped_runs})
    runs = [r for r in scoped_runs if not project or r.product == project]

    groups = _group_rows(runs, by)

    scope_note = ("across every project you can see" if whole_fleet else
                  f"across {len(scope)} project{'s' if len(scope) != 1 else ''} in your scope")
    window_label = f"{since.isoformat()} to {until.isoformat()}"

    if not runs:
        results_html = ds.empty_state(f"No finished runs {scope_note}, {window_label}.")
    else:
        results_html = (
            f'<p class="mc-sub">{len(runs)} run{"s" if len(runs) != 1 else ""} '
            f'in {len(groups)} group{"s" if len(groups) != 1 else ""}, {window_label}.</p>'
            '<div class="mc-groups">' + "".join(_group_html(g) for g in groups) + '</div>'
        )

    return (
        '<div class="page-head"><h1>Model comparison</h1>'
        f'<p class="page-sub">Finished runs grouped by agent and model, {scope_note}. '
        'Read live from the Library on every request.</p></div>'
        + _filters_form_html(by, project, project_options, since, until)
        + results_html
    )


_PAGE_CSS = r"""
.page-head { margin:24px 0 0; }
.page-head h1 { font-size:22px; font-weight:700; letter-spacing:-.01em; color:var(--ink); }
.page-sub { font-size:13px; color:var(--ink-2); margin-top:4px; margin-bottom:16px; }
.mc-form { display:flex; flex-wrap:wrap; gap:12px; align-items:end; margin-bottom:16px; }
.mc-form label { font-size:12px; color:var(--ink-2); }
.mc-form select, .mc-form input, .mc-form button { display:block; margin-top:4px; }
.mc-sub { color:var(--ink-2); font-size:13px; margin-bottom:12px; }
.mc-groups { display:flex; flex-direction:column; gap:8px; }
.mc-group { border:1px solid var(--line); border-radius:var(--radius); background:var(--surface); }
.mc-group summary { cursor:pointer; padding:10px 14px; display:flex; flex-wrap:wrap;
                    gap:16px; align-items:baseline; list-style:none; }
.mc-group summary::-webkit-details-marker { display:none; }
.mc-group summary::before { content:"▸ "; color:var(--ink-3); }
.mc-group[open] summary::before { content:"▾ "; }
.mc-group-name { font-weight:650; color:var(--ink); }
.mc-group-count, .mc-group-cost, .mc-group-rate { color:var(--ink-2); font-size:13px;
                                                   font-family:var(--mono); }
.mc-group-body { padding:0 14px 12px; }
.data-table th, .data-table td { text-align:right; white-space:nowrap; }
.data-table th:first-child, .data-table td:first-child { text-align:left; }
.data-table tbody tr:hover td { background:var(--brand-wash); }
"""


def live_html(query: dict | None, scope: frozenset[str], whole_fleet: bool) -> str:
    """Render /model-comparison at request time. No static fallback — see
    the module docstring for why a render failure is a plain 500 instead."""
    body = _build_body(query, scope, whole_fleet)
    return ds.page_shell(title="Model comparison",
                         nav_html=ds.nav_component("/model-comparison", "full"),
                         body=body, extra_css=_PAGE_CSS)
