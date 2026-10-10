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


def _cost_per_merged_pr() -> dict:
    """Cost-per-merged-PR headline from the scorecard ledger (all-time window).

    Reads the same normalize() facts that the Cost-Performance dashboard uses
    and returns total metered cost ÷ merged PRs — the $/merged figure that is
    the dashboard's primary cost-efficiency signal.
    """
    try:
        facts = dashboard_build.normalize()
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


def _safe_section(fn, label: str) -> str:
    try:
        return fn()
    except Exception:
        return ds.error_state(f"Could not load {label} data.")


def _section(fresh: ds.Freshness, feed: str, value, delta) -> str:
    """A stat block for a card whose module-owned `Freshness` may be stalled
    — the shared shape every section below reduces to, so the banner-or-not
    decision is made in exactly one place on this page too."""
    banner = ds.stalled_banner_html(feed, fresh)
    return banner + _stat_block(value, delta, fresh.text)


def _spending_section() -> str:
    snap = dashboard_build.load_latest_snapshot()
    if not snap:
        return ds.empty_state("No spend snapshot yet.")
    spend = home_build._today_spend()
    fresh = dashboard_build.spending_freshness(snap)
    return _section(fresh, "Spend snapshot", spend["value"], spend["delta"])


def _cost_performance_section() -> str:
    facts = dashboard_build.normalize()
    if not facts:
        return ds.empty_state("No scorecard data yet.")
    cp = _cost_per_merged_pr()
    fresh = dashboard_build.cost_performance_freshness(facts)
    return _section(fresh, "Cost-performance ledger", cp["value"], cp["delta"])


def _activity_section() -> str:
    snap = activity_build._load_snapshot()
    count = snap.get("session_count", 0)
    if not count:
        return ds.empty_state("No session activity captured yet.")
    fresh = activity_build.freshness(snap)
    return _section(fresh, "Session activity", f"{count} sessions", "logged")


def _velocity_section() -> str:
    """Waypoints passed, the same number and the same
    `home_build._velocity_stat()` call the front page's hero stat uses, so
    the two surfaces cannot disagree (dashboards epic
    issue_01M2F3Q1B8J9E0WC5J1F1CMWA1). Computed live from the Library on
    every render, not from a local snapshot, so there is no freshness
    cadence to judge it against — a stalled banner does not apply here the
    way it does to the other cards on this page."""
    velocity = home_build._velocity_stat()
    return _stat_block(velocity["value"], velocity["delta"], "computed live from the Library")


def _warrant_section() -> str:
    data = warrant_build.load()
    o = data.get("overall") or {}
    if not o.get("merged_prs"):
        return ds.empty_state("No warrant-scorecard data yet.")
    value = f'{o.get("unwarranted_rate", 0):.1f}%'
    delta = f'{o.get("merged_prs")} merged PRs'
    fresh = warrant_build.freshness(data)
    return _section(fresh, "Warrant scorecard", value, delta)


def _code_health_section() -> str:
    asof, rows = code_health_build.load_snapshot()
    if not rows:
        return ds.empty_state("No code-health harvest yet.")
    health = home_build._ci_health()
    fresh = code_health_build.freshness(asof)
    return _section(fresh, "Code-health harvest", health["value"], health["delta"])


def _security_section() -> str:
    data = security_build.load()
    o = data.get("overall") or {}
    if not o.get("prs_reviewed"):
        return ds.empty_state("No security-scorecard data yet.")
    value = f'{o.get("blocking_per_kloc", 0):.2f}'
    delta = f'{o.get("prs_reviewed")} PRs reviewed'
    fresh = security_build.freshness(data)
    return _section(fresh, "Security scorecard", value, delta)


def _e2e_section() -> str:
    recent = library_client.list_signals(
        source=[engineering_e2e_build.CI_SOURCE],
        cap=engineering_e2e_build._SCAN_LIMIT)
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


def _kb_section() -> str:
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


# (href, dashboard name, headline label, section fn) per group. Alerts is
# deliberately absent — it is a needs-me signal, not a dashboard to browse,
# and keeps its pinned nav entry plus a Home tile instead (home_build).
_GROUPS = [
    ("Money", [
        ("/spending", "Spending", "Today's LLM Spend", _spending_section),
        ("/cost-performance", "Cost-Performance", "Cost per Merged PR", _cost_performance_section),
    ]),
    ("Delivery", [
        ("/activity", "Activity", "Sessions Logged", _activity_section),
        ("/velocity", "Velocity", "Waypoints Passed (7d)", _velocity_section),
        ("/warrant", "Warrant", "Unwarranted Rate", _warrant_section),
    ]),
    ("Health", [
        ("/health", "Code Health", "Avg CI Pass Rate", _code_health_section),
        ("/security", "Security", "Blocking / KLOC", _security_section),
        ("/engineering/e2e", "E2E", "Suites Passing", _e2e_section),
        ("/kb", "KB Retrieval", "Golden-Set Recall", _kb_section),
    ]),
]


def _build_body() -> str:
    now_label = orient_time.now_label()

    group_blocks = []
    for group_name, entries in _GROUPS:
        cards = "".join(
            _overview_card(href, dashboard, label, _safe_section(fn, dashboard))
            for href, dashboard, label, fn in entries
        )
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


def live_html() -> str:
    """Render the Dashboards Overview hub at request time (ARCH-182)."""
    nav_html = ds.nav_component("/overview", "full")
    body = _build_body()
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
