#!/usr/bin/env python3.11
"""tech_debt_build.py — the /tech-debt dashboard.

**Tech debt is a project's corrections queue**: the open Library work items
that belong to no epic. A product's queue is where the Library keeps work that
has no plan behind it (library/specs/one-home-per-work-item), so how much is
open in it, and how old, is what that project still owes and nobody has
scheduled. The page this replaces counted open GitHub issues; those stopped
existing when work moved to the Library's queue, which is why the route 404'd
between its retirement (issue_01M2HY3K2SWV10VGZFRWJD34RS) and this rebuild.

Read live per request through `library_client.list_issue_items` — nothing is
cached or stored in Orient's control database, so a data change shows on the
next load. A Library failure raises rather than rendering an empty page:
server.py serves the last static build instead, because "no open corrections"
is indistinguishable from an empty queue to a reader.

`issue_build._is_correction` and `_queue_parents` are imported rather than
restated. /engineering/corrections lists these same items, and two definitions
of "a correction" would drift until the dashboard's count and the page's own
disagreed with no way to tell which was right.

Full-tier only, gated in server.py on the Cloudflare Access identity and
scoped to the viewer's own projects: a project outside `scope` contributes no
row, no count and no name.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape

import design_system as ds
import issue_build
import library_client
from models import utcnow
from paths import STATE_ROOT

SITE = STATE_ROOT / "site"

# A severity bucket per value the Library's work items carry. "unset" is not a
# fourth rank: it holds None, the literal "unset" some rows carry, and any
# value this module does not recognise. Folding those together rather than
# ranking them is `issue_build._SEVERITY_RANK`'s own rule — inventing a
# severity for an unrecognised one would read a value nobody chose as one
# somebody did.
SEVERITY_KEYS = ("high", "medium", "low", "unset")
_SEVERITY_LABEL = {"high": "High", "medium": "Medium", "low": "Low",
                   "unset": "Unset/other"}

# Age bands by UPPER bound in whole days, ascending, the last open-ended — so
# every non-negative age lands in exactly one band and `age_band` needs no
# fallback that could answer with a value outside this table.
AGE_BANDS = (
    ("under 7 days", 6),
    ("7-30 days", 30),
    ("31-90 days", 90),
    ("over 90 days", None),
)


@dataclass(frozen=True)
class ProjectDebt:
    """One project's open corrections. `oldest` is the item itself, named on
    the page; `oldest_age_days` is its age, so a caller that only needs the
    number does not re-subtract dates."""

    product: str
    total: int
    by_severity: dict[str, int]
    by_band: dict[str, int]
    oldest: "library_client.IssueRecord | None"
    oldest_age_days: int | None


def severity_of(item) -> str:
    """`item`'s severity bucket — one of `SEVERITY_KEYS`."""
    return item.severity if item.severity in ("high", "medium", "low") else "unset"


def age_days(created_at, now) -> int:
    """Whole days since `created_at`, clamped at zero. Both are naive UTC, the
    store's convention (models.utcnow). The clamp is `issue_build._age`'s own:
    an item written by an agent host whose clock runs slightly ahead would
    otherwise carry a negative age and fall outside every band."""
    return max(0, (now - created_at).days)


def age_band(days: int) -> str:
    """The label of the band `days` falls in."""
    for label, upper in AGE_BANDS:
        if upper is None or days <= upper:
            return label
    raise AssertionError("AGE_BANDS must end with an open-ended band")


def corrections(items) -> list:
    """The active corrections-queue items among `items`, in
    `issue_build._active_in_lane`'s order.

    `issue_build._epic_and_correction_groups` is the Corrections page's own
    partition, called rather than reimplemented: it applies the lane split
    (a `needs=operator` blocker is not a correction), the queue-parent lookup
    (`library_client.corrections_queue_ids`, never a `cq_` prefix), and the
    fallback that keeps an item whose parent names no item at all on the page
    instead of dropping it. A second copy of those rules would drift from the
    page this dashboard counts, and the disagreement would be invisible.

    "Active" is the Library's own open set (open, claimed, pr-open); a
    resolved item is not debt. The cross-product relation the page reads with
    `related` is left out here — it joins an item to an epic's children, and
    an item already on the page as a correction stays one."""
    _, _, rows = issue_build._epic_and_correction_groups(items)
    return rows


def summarise(items, scope: frozenset[str], now=None) -> list[ProjectDebt]:
    """One row per in-scope project, largest queue first.

    `items` is the Library's whole list; the scope filter runs first, so an
    out-of-scope project is never grouped, counted, or named, and its queue is
    never looked up. A project in `scope` with no open correction keeps a row
    of zeros — an empty queue is a fact worth showing, and dropping the row
    would make it look like a project Orient does not know about.

    A scope holding "*" is the Library's answer for an administrator, so the
    projects are then the ones the Library's items name."""
    now = now or utcnow()
    visible = [it for it in items if issue_build._in_scope(it.product, scope)]
    projects = {p for p in scope if p != "*"} | {it.product for it in visible}

    grouped: dict[str, list] = {}
    for it in corrections(visible):
        grouped.setdefault(it.product, []).append(it)

    rows = []
    for product in projects:
        open_items = grouped.get(product, [])
        by_severity = dict.fromkeys(SEVERITY_KEYS, 0)
        by_band = {label: 0 for label, _ in AGE_BANDS}
        for it in open_items:
            by_severity[severity_of(it)] += 1
            by_band[age_band(age_days(it.created_at, now))] += 1
        oldest = min(open_items, key=lambda it: (it.created_at, it.id), default=None)
        rows.append(ProjectDebt(
            product=product, total=len(open_items), by_severity=by_severity,
            by_band=by_band, oldest=oldest,
            oldest_age_days=age_days(oldest.created_at, now) if oldest else None))
    rows.sort(key=lambda r: (-r.total, r.product))
    return rows


def scoped_rows(scope: frozenset[str], now=None) -> list[ProjectDebt]:
    """`summarise` over the Library's live item list. Raises on a Library
    failure, like every read in `library_client` — server.py's stale-static
    fallback is the degrade, not an empty page."""
    return summarise(library_client.list_issue_items(), scope, now)


_PAGE_CSS = r"""
.page-head { margin:24px 0 0; }
.page-head h1 { font-size:22px; font-weight:700; letter-spacing:-.01em; color:var(--ink); }
.page-sub { font-size:13px; color:var(--ink-2); margin-top:4px; margin-bottom:16px; }
.page-head h2 { font-size:17px; font-weight:700; letter-spacing:-.01em; color:var(--ink);
                margin:26px 0 10px; }
.statcards { margin-bottom:8px; }
.data-table th, .data-table td { text-align:right; white-space:nowrap; }
.data-table th:first-child, .data-table td:first-child { text-align:left; }
.data-table td.k { font-weight:600; }
.data-table td a { color:inherit; text-decoration:none; border-bottom:1px dotted var(--line); }
.data-table td a:hover { color:var(--brand-ink); border-bottom-color:var(--brand-ink); }
.data-table td.none { color:var(--ink-3); }
.sev-high { color:var(--err); font-weight:650; }
.sev-medium { color:var(--warn); font-weight:650; }
.sev-low, .sev-unset { color:var(--ink-2); }
"""


def _count_cell(value: int) -> str:
    """A count cell; a zero reads as quiet rather than as a small finding."""
    return f'<td class="none">{value}</td>' if value == 0 else f"<td>{value}</td>"


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    return ('<div class="data-table-wrap"><table class="data-table">'
            f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>")


def _severity_table(rows: list[ProjectDebt]) -> str:
    body = [
        f'<tr><td class="k">{escape(r.product)}</td>'
        + "".join(f'<td class="sev-{key}">{r.by_severity[key]}</td>'
                  for key in SEVERITY_KEYS)
        + _count_cell(r.total) + "</tr>"
        for r in rows
    ]
    return _table(["Project"] + [_SEVERITY_LABEL[k] for k in SEVERITY_KEYS] + ["Open"], body)


def _age_table(rows: list[ProjectDebt]) -> str:
    labels = [label for label, _ in AGE_BANDS]
    body = [
        f'<tr><td class="k">{escape(r.product)}</td>'
        + "".join(_count_cell(r.by_band[label]) for label in labels)
        + _count_cell(r.total) + "</tr>"
        for r in rows
    ]
    return _table(["Project"] + labels + ["Open"], body)


def _oldest_table(rows: list[ProjectDebt]) -> str:
    body = []
    for r in rows:
        if r.oldest is None:
            body.append(f'<tr><td class="k">{escape(r.product)}</td>'
                        '<td class="none">&mdash;</td><td class="none">0</td>'
                        '<td class="none">&mdash;</td></tr>')
            continue
        item = r.oldest
        body.append(
            f'<tr><td class="k">{escape(r.product)}</td>'
            f'<td><a href="/engineering/issues/{escape(item.id, quote=True)}">'
            f'{escape(item.title)}</a></td>'
            f'<td>{r.oldest_age_days}d</td>'
            f'<td class="sev-{severity_of(item)}">'
            f'{escape(_SEVERITY_LABEL[severity_of(item)])}</td></tr>')
    return _table(["Project", "Oldest open correction", "Age", "Severity"], body)


def render(rows: list[ProjectDebt]) -> str:
    total = sum(r.total for r in rows)
    by_severity = {k: sum(r.by_severity[k] for r in rows) for k in SEVERITY_KEYS}
    oldest = max((r.oldest_age_days for r in rows if r.oldest_age_days is not None),
                 default=None)
    cards = "".join([
        ds.stat_card("Open corrections", str(total),
                     f"{len(rows)} project{'' if len(rows) == 1 else 's'} in scope"),
        ds.stat_card("High severity", str(by_severity["high"]),
                     f"{by_severity['medium']} medium · {by_severity['low']} low"),
        ds.stat_card("Oldest", f"{oldest}d" if oldest is not None else "—",
                     "since the oldest item was created"),
    ])
    head = (
        '<div class="page-head">'
        f'<h1>Tech debt <span class="pill">{total} open</span></h1>'
        '<p class="page-sub">Open corrections-queue items per project: work with no epic '
        'behind it, read live from the Library on this request, so the figures are as of the '
        'render time in the footer. Age is days since the item was created.</p>'
        f'<div class="statcards">{cards}</div>'
    )
    if not rows:
        return (head + ds.empty_state(
            "No open corrections in the projects you can see.") + "</div>")
    return (
        head
        + "<h2>By severity</h2>" + _severity_table(rows)
        + "<h2>By age</h2>" + _age_table(rows)
        + "<h2>Oldest open correction per project</h2>" + _oldest_table(rows)
        + "</div>"
    )


def live_html(scope: frozenset[str] = frozenset()) -> str:
    """Render /tech-debt at request time, restricted to the viewer's project
    scope. The default empty scope renders an empty page, for the static
    fallback's own render path; no full-tier request reaches it, because an
    empty scope gets server.py's dedicated empty-scope page."""
    return ds.page_shell(
        title="tech debt",
        nav_html=ds.nav_component("/tech-debt", "full"),
        body=render(scoped_rows(scope)),
        extra_css=_PAGE_CSS,
    )


def build() -> None:
    """Write the static fallback server.py serves when a live render throws.

    Empty by construction, not the last figures: the fallback is served to any
    viewer whose render failed, before their scope is known to this module at
    all, so a per-project row written here would be real project data with no
    scope check on the way out. Same discipline as `issue_build.main` and
    `code_health_build.build`."""
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "tech-debt.html").write_text(
        ds.page_shell(title="tech debt", nav_html=ds.nav_component("/tech-debt", "full"),
                      body=ds.empty_state("Live view unavailable — reload in a moment."),
                      extra_css=_PAGE_CSS),
        encoding="utf-8")
    print("Wrote site/tech-debt.html (static fallback, empty)")


if __name__ == "__main__":
    build()
