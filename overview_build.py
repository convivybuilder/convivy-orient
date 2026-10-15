#!/usr/bin/env python3.11
"""Dashboards Overview hub for convivy-orient (/overview, ARCH-216 Phase 2;
grouped-index rebuild: nav-reshape spec §4, issue_01M0TKX6DW2E1TXGN9QT6K61J5).

Full-tier only. A card per dashboard (all twelve except Overview itself and
Alerts, which stays a pinned nav-only signal and gets its own Home tile
instead — see home_build), grouped under three headings: Money, Delivery,
Health. Each card carries the dashboard's name, a headline number where one is
cheaply available, and a freshness stamp — this index is what makes a stalled
feed visible across every dashboard at once, the way /engineering/e2e's
stalled_state made it visible on one.

Graceful degradation is a hard rule, generalized from five cards to ten: a
broken or slow loader renders its own card as empty/stalled/error, it never
hangs or 500s the page. Every per-dashboard `_..._section()` function is
wrapped by `_safe_section`, which is the single place an unexpected exception
is caught.
"""
from __future__ import annotations

import json
import pathlib
from html import escape

import activity_build
import code_health_build
import crew_registry
import dashboard_build
import design_system as ds
import engineering_e2e_build
import home_build
import kb_build
import library_client
import orient_time
import security_build
import warrant_build
from models import utcnow
from paths import STATE_ROOT

OUT = STATE_ROOT / "site"

# Every card's freshness judgment is owned by that dashboard's own build
# module (a `freshness()` function returning a `design_system.Freshness`),
# not by this file — so /overview and the page a card links to read the same
# verdict off the same value instead of computing it twice
# (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment 902). See each module's own
# `freshness()` for its cadence and how it was derived; see
# `dashboard_build.cost_performance_freshness` for the one dashboard that
# deliberately renders no judgment, and why.

_FRESHNESS_UNKNOWN = "no freshness reported"


def _stat_block(value, delta, freshness_label: str) -> str:
    return (
        f'<div class="overview-card-value num">{escape(str(value))}</div>'
        f'<div class="overview-card-delta">{escape(str(delta))}</div>'
        f'<div class="overview-card-freshness">{escape(freshness_label)}</div>'
    )


def _read_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _cost_per_merged_pr(facts: list[dict]) -> dict:
    """Cost-per-merged-PR headline from already-scoped scorecard facts (the
    all-time window) — the $/merged figure that is the Cost-Performance
    dashboard's primary cost-efficiency signal."""
    try:
        total_usd = sum(f["cost_usd"] for f in facts if f.get("cost_usd") is not None)
        merged = sum(1 for f in facts if f.get("merged"))
        if merged == 0:
            return {"value": "—", "delta": "no merged PRs yet"}
        per_pr = total_usd / merged
        return {"value": f"${per_pr:.2f}", "delta": f"per merged PR · {merged} total"}
    except Exception:
        return {"value": "—", "delta": "unavailable"}


# ---------------------------------------------------------------------------
# Per-dashboard sections — each returns the HTML that goes between a card's
# label and its "Dashboard →" footer: either a stat block (+ a stalled banner
# above it, where the cadence is known and exceeded) or one of design_system's
# empty/error states. `_safe_section` is the only exception boundary; a
# section function is free to let an unexpected error propagate to it.
# ---------------------------------------------------------------------------


def _safe_section(fn, label: str, scope: frozenset[str], whole_fleet: bool) -> str:
    try:
        return fn(scope, whole_fleet)
    except Exception:
        return ds.error_state(f"Could not load {label} data.")


def _section(fresh: ds.Freshness, feed: str, value, delta) -> str:
    """A stat block for a card whose module-owned `Freshness` may be stalled
    — the shared shape every section below reduces to, so the banner-or-not
    decision is made in exactly one place on this page too."""
    banner = ds.stalled_banner_html(feed, fresh)
    return banner + _stat_block(value, delta, fresh.text)


def _spending_section(_scope=frozenset(), _whole_fleet=True) -> str:
    """FLEET-ONLY (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): the account-wide
    spend snapshot carries no `product` field at all (`home_build.
    _today_spend`'s own docstring), so there is no way to recompute it over a
    subset of projects. `_build_body` only ever calls this for a whole-fleet
    viewer — see `_GROUPS`'s `fleet_only` flag — so `_scope`/`_whole_fleet`
    are accepted for interface symmetry with every other section and unused."""
    snap = dashboard_build.load_latest_snapshot()
    if not snap:
        return ds.empty_state("No spend snapshot yet.")
    spend = home_build._today_spend(True)
    fresh = dashboard_build.spending_freshness(snap)
    return _section(fresh, "Spend snapshot", spend["value"], spend["delta"])


def _cost_performance_section(scope=frozenset(), whole_fleet=True) -> str:
    """Restricted to `scope` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) the
    same way `dashboard_build.cost_performance_live_html` restricts
    /cost-performance's own facts."""
    repos = None if whole_fleet else crew_registry.repos_in_scope(scope)
    facts = dashboard_build.normalize(repos)
    if not facts:
        return ds.empty_state("No scorecard data yet.")
    cp = _cost_per_merged_pr(facts)
    fresh = dashboard_build.cost_performance_freshness(facts)
    return _section(fresh, "Cost-performance ledger", cp["value"], cp["delta"])


def _activity_section(scope=frozenset(), whole_fleet=True) -> str:
    """Restricted to `scope` the same way /activity's own sessions are
    (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): a session whose product is
    outside `scope` is dropped before it is counted."""
    snap = activity_build.scoped_snapshot(scope, whole_fleet)
    count = snap.get("session_count", 0)
    if not count:
        return ds.empty_state("No session activity captured yet.")
    fresh = activity_build.freshness(snap)
    return _section(fresh, "Session activity", f"{count} sessions", "logged")


def _velocity_section(scope=frozenset(), whole_fleet=True) -> str:
    """Waypoints passed, the same number and the same
    `home_build._velocity_stat()` call the front page's hero stat uses, so
    the two surfaces cannot disagree (dashboards epic
    issue_01M2F3Q1B8J9E0WC5J1F1CMWA1), restricted to `scope` the same way
    (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M). Computed live from the
    Library on every render, not from a local snapshot, so there is no
    freshness cadence to judge it against — a stalled banner does not apply
    here the way it does to the other cards on this page."""
    velocity = home_build._velocity_stat(scope, whole_fleet)
    return _stat_block(velocity["value"], velocity["delta"], "computed live from the Library")


def _warrant_section(_scope=frozenset(), _whole_fleet=True) -> str:
    """FLEET-ONLY (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): the overall
    unwarranted-rate headline aggregates across every repo with no way to
    recompute it over a subset — warrant-report.json's `by_repo` bucket is
    filtered on /warrant's own page, but this card shows `overall`, not
    `by_repo`. `_build_body` only calls this for a whole-fleet viewer."""
    data = warrant_build.load()
    o = data.get("overall") or {}
    if not o.get("merged_prs"):
        return ds.empty_state("No warrant-scorecard data yet.")
    value = f'{o.get("unwarranted_rate", 0):.1f}%'
    delta = f'{o.get("merged_prs")} merged PRs'
    fresh = warrant_build.freshness(data)
    return _section(fresh, "Warrant scorecard", value, delta)


def _code_health_section(scope=frozenset(), whole_fleet=True) -> str:
    """Restricted to `scope` the same way `/health`'s own rows are (E19·3,
    issue_01M35K5EAZD85FBYXEBQVRKVZV), via the same helper Home's hero stat
    uses."""
    asof, rows = code_health_build.load_snapshot()
    if not whole_fleet:
        repos = crew_registry.repos_in_scope(scope)
        rows = [r for r in rows if r.get("repo") in repos]
    if not rows:
        return ds.empty_state("No code-health harvest yet.")
    health = home_build._ci_health(scope, whole_fleet)
    fresh = code_health_build.freshness(asof)
    return _section(fresh, "Code-health harvest", health["value"], health["delta"])


def _security_section(_scope=frozenset(), _whole_fleet=True) -> str:
    """FLEET-ONLY (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): same reasoning
    as `_warrant_section` — `overall`, not the filterable `by_repo` bucket.
    `_build_body` only calls this for a whole-fleet viewer."""
    data = security_build.load()
    o = data.get("overall") or {}
    if not o.get("prs_reviewed"):
        return ds.empty_state("No security-scorecard data yet.")
    value = f'{o.get("blocking_per_kloc", 0):.2f}'
    delta = f'{o.get("prs_reviewed")} PRs reviewed'
    fresh = security_build.freshness(data)
    return _section(fresh, "Security scorecard", value, delta)


def _e2e_section(scope=frozenset(), whole_fleet=True) -> str:
    """Restricted to `scope` the same way `/engineering/e2e`'s own tiles are
    (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M)."""
    recent = library_client.list_signals(
        source=[engineering_e2e_build.CI_SOURCE],
        cap=engineering_e2e_build._SCAN_LIMIT)
    if not whole_fleet:
        recent = [s for s in recent if s.product in scope]
    if not recent:
        return ds.empty_state("No CI results captured yet.")
    now = utcnow()
    latest: dict[tuple, library_client.SignalRecord] = {}
    for sig in recent:
        key = (sig.product, sig.stream or sig.kind)
        if key not in latest:
            latest[key] = sig
    total = len(latest)
    passing = sum(1 for s in latest.values()
                  if (s.meta or {}).get("status") == "pass")
    value = f"{passing}/{total} passing"
    delta = f"{total} suites"
    fresh = engineering_e2e_build.freshness(recent, now)
    return _section(fresh, "CI test results", value, delta)


def _kb_section(_scope=frozenset(), _whole_fleet=True) -> str:
    """The golden-set eval's own recall, which is what /kb exists to measure.

    **This card used to show KB freshness — a doc count and a timestamp — and
    that was the only label on this page naming a staleness property rather
    than a measured quantity** (the operator, 2026-09-05). Every sibling card names a
    number, so this one does too, and the number is the one that can be wrong:
    recall@5 was 0.500 when this changed, which `cls_recall` grades `bad`, and
    no overview surface said so.

    `cls_recall` is deliberately the same function `alerts_build` thresholds
    the KB Retrieval alert on, so this card, the /kb page and the alert cannot
    disagree about what counts as on-target.

    **AN EVAL THAT RAN BUT CARRIES NO RECALL MUST STILL SHOW ITS STALLED
    BANNER.** Only a total absence of eval data reaches the empty state, which
    `freshness` distinguishes for us: it reports `stalled=None` when nothing
    has ever been written and `stalled=True` with a real reason when a run
    landed without usable numbers. Empty-stating the second case would drop
    the staleness signal and render a degraded feed as an absent one — the
    collapse this page exists to prevent.

    **FLEET-ONLY** (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): the golden-set
    eval runs over the whole KB corpus, with no per-project recall to show
    instead. `_build_body` only calls this for a whole-fleet viewer.
    """
    # `load_eval` returns None, not {}, when the file is absent or unparseable
    # — its own `dict | None`, and the case this branch exists for. Coalescing
    # here rather than trusting the call: without it `.get` raises, and
    # `_safe_section` turns the intended "no eval yet" empty state into "Could
    # not load KB Retrieval data", rendering an absent feed as a broken one.
    ev = kb_build.load_eval() or {}
    fresh = kb_build.freshness(ev)
    overall = ev.get("overall") or {}
    recall = overall.get("recall@5")
    # bool is an int subclass, so the isinstance guard admits True/False and
    # would format it as 100%/0%.
    if isinstance(recall, bool):
        recall = None
    if fresh.stalled is None and not isinstance(recall, (int, float)):
        return ds.empty_state("No golden-set eval yet.")
    if not isinstance(recall, (int, float)):
        return _section(fresh, "KB golden-set eval", "—", "no recall in the latest eval")
    cases = overall.get("n")
    delta = f"recall@5 · {cases} cases" if cases else "recall@5"
    return _section(fresh, "KB golden-set eval", f"{recall:.0%}", delta)


# ---------------------------------------------------------------------------


def _overview_card(href: str, dashboard: str, label: str, inner_html: str) -> str:
    """One dashboard's card.

    **A card carrying a stalled banner gets NO `aria-label`, deliberately.**
    An `aria-label` on the anchor overrides accessible-name computation for
    the entire subtree, so the banner's "Newest result … 6d ago" text — the
    one thing this whole section exists to surface — was excluded from what a
    screen reader announces for the link. `role="status"` does not rescue it
    either: the banner is present at initial render rather than injected
    afterwards, and assistive tech does not retroactively announce live-region
    content that was already in the DOM (PR #204 review).

    Dropping the override makes the accessible name the card's own text, which
    is wordier and includes the warning. That trade only applies to the cards
    that have something extra to say; a healthy card keeps the tidy name."""
    stalled = 'class="stalled-banner"' in inner_html
    aria = "" if stalled else f' aria-label="{escape(dashboard, quote=True)} dashboard"'
    return (
        f'<a class="overview-card" href="{href}"{aria}>'
        f'<div class="overview-card-label">{label}</div>'
        f'{inner_html}'
        f'<div class="overview-card-name">{dashboard} →</div>'
        f'</a>'
    )


# (href, dashboard name, headline label, section fn, fleet_only) per group.
# Alerts is deliberately absent — it is a needs-me signal, not a dashboard to
# browse, and keeps its pinned nav entry plus a Home tile instead (home_build).
#
# `fleet_only` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): the card's headline
# figure aggregates across every repo with no `product` dimension to filter
# on (Spending's account-wide snapshot, Warrant's and Security's `overall`
# bucket, KB's golden-set recall over the whole corpus) — `_build_body` drops
# that card from the grid entirely for a scoped, non-whole-fleet viewer,
# rather than show a fleet figure or an empty husk of a card.
_GROUPS = [
    ("Money", [
        ("/spending", "Spending", "Today's LLM Spend", _spending_section, True),
        ("/cost-performance", "Cost-Performance", "Cost per Merged PR",
         _cost_performance_section, False),
    ]),
    ("Delivery", [
        ("/activity", "Activity", "Sessions Logged", _activity_section, False),
        ("/velocity", "Velocity", "Waypoints Passed (7d)", _velocity_section, False),
        ("/warrant", "Warrant", "Unwarranted Rate", _warrant_section, True),
    ]),
    ("Health", [
        ("/health", "Code Health", "Avg CI Pass Rate", _code_health_section, False),
        ("/security", "Security", "Blocking / KLOC", _security_section, True),
        ("/engineering/e2e", "E2E", "Suites Passing", _e2e_section, False),
        ("/kb", "KB Retrieval", "Golden-Set Recall", _kb_section, True),
    ]),
]


def _build_body(scope: frozenset[str] = frozenset(), whole_fleet: bool = True) -> str:
    now_label = orient_time.now_label()

    group_blocks = []
    for group_name, entries in _GROUPS:
        visible = [e for e in entries if whole_fleet or not e[4]]
        cards = "".join(
            _overview_card(href, dashboard, label, _safe_section(fn, dashboard, scope, whole_fleet))
            for href, dashboard, label, fn, _fleet_only in visible
        )
        if not cards:
            continue
        group_blocks.append(
            f'<div class="overview-group">'
            f'<h2 class="overview-group-title">{escape(group_name)}</h2>'
            f'<div class="overview-grid">{cards}</div>'
            f'</div>'
        )

    return f"""<header class="hero">
  <div class="hero-inner">
    <div class="hero-eyebrow">Dashboards</div>
    <h1>Overview</h1>
    <p class="hero-sub">As of {now_label}. Each card links into the full dashboard and says when its data last updated.</p>
  </div>
</header>
{"".join(group_blocks)}
<div class="home-foot">
  <span>Convivy substrate · team.convivy.com</span>
  <span class="mono">All dashboards</span>
</div>"""


def live_html(scope: frozenset[str] = frozenset(), whole_fleet: bool = False) -> str:
    """Render the Dashboards Overview hub at request time (ARCH-182),
    restricted to `scope` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): every
    card's own headline figure is filtered the same way its full dashboard
    is, and a card whose figure cannot be filtered at all is left off the
    grid for a scoped viewer — see `_GROUPS`'s `fleet_only` flag. The
    defaults render an empty-scope grid, for the static fallback."""
    nav_html = ds.nav_component("/overview", "full")
    body = _build_body(scope, whole_fleet)
    return ds.page_shell(
        title="Dashboards Overview",
        nav_html=nav_html,
        body=body,
        auto_refresh_seconds=ds.SPEND_REFRESH_SECONDS,
    )


def build_static() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    html = live_html()
    (OUT / "overview.html").write_text(html, encoding="utf-8")
    print("Wrote site/overview.html")


if __name__ == "__main__":
    build_static()
