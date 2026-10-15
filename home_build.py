#!/usr/bin/env python3.11
"""Home page builder for convivy-orient (ARCH-216 Phase 2).

Adaptive, tier-aware landing page:
- Full tier  → ops Home: hero + 3 StatCards, quick-links, activity feed, recent docs.
- Contractor → Reading Room launcher: hero + search, start-here links, and
  what's-new feed, all pointing into the member's Reading Room (ARCH-357).

live_html(tier, label) renders on every request; build_static() writes the
static fallback files to site/home.full.html and site/home.contractor.html.
"""
from __future__ import annotations

import pathlib
import re
from datetime import date, datetime, timezone
from html import escape
from urllib.parse import quote

import alerts_build
import code_health_build
import crew_registry
import design_system as ds
import library_client
import orient_time
import waiting_build
import waypoints
from paths import STATE_ROOT

OUT = STATE_ROOT / "site"

# Contractor visibility is the per-doc `contractor` audience marker (ARCH-228
# Phase 5), the same default-deny rule build.py uses — a doc surfaces to a
# contractor only if its audience opts in. No local blocklist to drift against.

# Data paths (same sources used by the individual dashboard builders).
_HEALTH_SNAPSHOT = pathlib.Path.home() / "Convivy" / "agent-runs" / "scorecard" / "health.json"
# Nothing in this module reads this path any more (`_kb_doc_count`/`_recent_docs` read via
# `library_client`'s reference readers, issue_01M1MFJA52QF2Q2K52MYJ65ZSX).
# The human site shows team-audience documents and nothing else, the rule
# `build.load_docs`/`kb_index.team_docs`/`reference_doc_build`'s own
# `_AUDIENCE` already apply. Named here rather than passed as a literal so
# the one place this page decides who it serves is greppable.
_AUDIENCE = "team"


# ---------------------------------------------------------------------------
# Data loaders — each returns a plain dict; each is wrapped in try/except so a
# single broken source never 500s the Home page.
# ---------------------------------------------------------------------------


def _spend_as_of() -> str:
    """The day and time this figure was rendered, e.g. "Mon Sep 28 · as of
    2:35 PM".

    Named on the figure because a bare "today" read the same on a tab left open
    since the night before (the operator, 2026-09-28: $750 on the computer, $158 on the
    phone, the first being Sunday's). The figure is a live per-request read now
    (CQ·154, issue_01M4ZBBQK0KFH9YQ16MNK75HJT, ARCH-182) rather than a
    cron-refreshed snapshot, so the render's own clock time IS the figure's
    age — there is no separate "as of" to fall back from."""
    now = orient_time.now()
    clock = now.strftime("%I:%M %p").lstrip("0")
    return f"{now:%a %b} {now.day} · as of {clock}"


def _today_window() -> tuple[datetime, datetime]:
    """Midnight MST to now, converted to the naive-UTC convention every
    `library_client` timestamp uses: the calendar day Home's agent-run spend
    covers, matching the day `_spend_as_of` names."""
    now_mst = orient_time.now()
    midnight_mst = now_mst.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight_mst.astimezone(timezone.utc).replace(tzinfo=None),
           now_mst.astimezone(timezone.utc).replace(tzinfo=None))


def _today_spend(whole_fleet: bool = True) -> dict | None:
    """Today's total finished-run spend, summed over every project in the
    Library's cost rollup (CQ·154, issue_01M4ZBBQK0KFH9YQ16MNK75HJT), or
    `None` for a scoped (non-whole-fleet) viewer — rule 1, no fleet total to
    a scoped viewer (E19·3, issue_01M35K5EAZD85FBYXEBQVRKVZV). Reads
    `library_client.cost_rollup_by_project`, the same rollup the crew view
    and the dashboards read, so this figure cannot disagree with theirs.

    `None` is also what a scoped viewer always got before this read a
    per-project source — the gate hasn't changed, only what feeds the number
    behind it. Catches a Library failure and degrades to "unavailable"
    rather than raising, the same belt every loader in this module wears
    (module docstring) so one broken source never 500s the whole page —
    `library_client` itself still raises; this is the layer that decides
    what a render does with that."""
    if not whole_fleet:
        return None
    try:
        since, until = _today_window()
        rollup = library_client.cost_rollup_by_project(library_client.list_finished_runs(since, until))
        total = sum(row["cost_usd"] for row in rollup.values())
        return {"value": f"${total:,.2f}", "delta": _spend_as_of()}
    except Exception:
        return {"value": "—", "delta": "unavailable"}


def _ci_health(scope: frozenset[str] = frozenset(), whole_fleet: bool = True) -> dict:
    """CI pass-rate from the persisted health snapshot, restricted to
    `scope` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) the same way
    `code_health_build.live_html` restricts /health's own rows — a repo the
    registry lists under no project in `scope` never enters the average.
    `whole_fleet` (default True, for every pre-existing caller) skips that
    filter entirely rather than resolving it from an always-empty `scope`."""
    try:
        _asof, rows = code_health_build.load_snapshot()
        if not whole_fleet:
            repos = crew_registry.repos_in_scope(scope)
            rows = [r for r in rows if r.get("repo") in repos]
        rates = [r["ci_pass_rate"] for r in rows
                 if isinstance(r.get("ci_pass_rate"), (int, float))]
        if not rates:
            return {"value": "—", "delta": "no CI data"}
        avg = sum(rates) / len(rates)
        return {"value": f"{avg * 100:.1f}%", "delta": "avg CI pass rate"}
    except Exception:
        return {"value": "—", "delta": "unavailable"}


def _velocity_stat(scope: frozenset[str] = frozenset(), whole_fleet: bool = True) -> dict:
    """Waypoints passed in the trailing 7 days, total across every IN-SCOPE
    product's live Course — pace toward each crew's bead (dashboards epic
    issue_01M2F3Q1B8J9E0WC5J1F1CMWA1), computed fresh from the Library on
    every render. The sub-line carries position — passed of total, across
    every live Course — since the 7-day headline alone doesn't say how far
    along a Course is. Replaces the old merged-PR headline: PR throughput is
    no longer this repo's pace signal.

    Restricted to `scope` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) the same
    way `velocity_build`'s own waypoints section is: a Course whose `product`
    is outside `scope` is dropped before `waypoints.course_progress` ever
    sees it. `whole_fleet` (default True, for every pre-existing caller)
    skips that filter entirely rather than resolving it from an always-empty
    `scope`.

    `waypoints.fetch_courses()` raises on a Library outage rather than
    returning `[]`, so that case is caught here and rendered "unavailable" —
    never a false "0 passed", which would read identically to a Course with
    every entry still open.
    """
    try:
        courses = waypoints.fetch_courses()
        if not whole_fleet:
            courses = [c for c in courses if c.product in scope]
        rows = waypoints.course_progress(courses)
        window_passed = waypoints.total_within(rows, 7)
        passed = sum(r.passed for r in rows)
        total = sum(r.total for r in rows)
        delta = f"{passed} of {total} passed" if rows else "no live Courses"
        return {"value": f"{window_passed} passed", "delta": delta}
    except Exception:
        return {"value": "—", "delta": "unavailable"}


def _kb_doc_count(scope: frozenset[str] = frozenset(), whole_fleet: bool = True) -> str:
    """KB doc count via the Library's reference reader, for the Home footer
    and the "Knowledge base" quick-card pill. Restricted to `scope` (E19·4,
    issue_01M35K5JQ7Z7MHYZGWRSB5F85M) the same way `engineering_build.
    live_html`'s own doc list is — a doc whose project is outside `scope`
    is dropped before it is counted.

    **Distinguishes a Library outage from a genuinely empty corpus.** A
    Library outage must not read the same as "no docs indexed" — that
    collapse is this repo's own recurring defect
    (issue_01M0TKX6DW2E1TXGN9QT6K61J5) — so an unreachable Library gets its
    own text rather than falling into the same "no docs indexed" bucket a
    truly empty corpus uses."""
    try:
        docs = library_client.list_reference_documents(audience=_AUDIENCE)
        if not whole_fleet:
            docs = [d for d in docs if d.project in scope]
        doc_count = len(docs)
        return f"{doc_count} docs indexed" if doc_count else "no docs indexed"
    except Exception as exc:
        return f"Library unreachable ({type(exc).__name__})"


def _active_alerts_count(scope: frozenset[str] = frozenset(), whole_fleet: bool = False) -> int:
    """Count of currently-crossed Dashboards policy thresholds, for the Home
    Alerts tile's pill. `alerts_build.gather()` already isolates each source
    dashboard's own read (a bad snapshot degrades to "no alerts from that
    source" rather than raising); the try/except here is Home's own belt on
    top, so a bug in `gather()` itself still can't take the whole page down.
    `scope` and `whole_fleet` make it count what `/alerts` shows this viewer
    (E19·3, issue_01M35K5EAZD85FBYXEBQVRKVZV)."""
    try:
        return len(alerts_build.gather(scope, whole_fleet))
    except Exception:
        return 0


def _recent_docs(n: int = 5, contractor_only: bool = False,
                 scope: frozenset[str] = frozenset(), whole_fleet: bool = True) -> list[dict]:
    """Most-recently-reviewed KB docs, via `library_client.list_reference_documents`
    (which already applies the `_AUDIENCE` team gate — issue_01M08YBCDTEFJ6N9S798ERF89P).

    The contractor branch applies the same default-deny rule as build.py: a doc
    surfaces in the "What's new in docs" feed only if its audience includes
    `contractor`, so no team-only doc can leak into the contractor home page.

    **`scope`/`whole_fleet` restrict the full-tier (non-`contractor_only`)
    branch** (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): a doc whose project
    is outside `scope` is dropped before its id, title or link can reach
    this feed — the id carries the project slug (`alpha/reference/doc-1`),
    so an unfiltered feed would disclose an out-of-scope project through the
    link alone. The contractor branch is untouched: contractor visibility is
    governed by the `audience` marker, not project scope (see this module's
    docstring).

    **Raises on a Library failure rather than swallowing it to `[]`.** An
    empty list here would render identically to "no docs indexed", which is
    this repo's own recurring defect (issue_01M0TKX6DW2E1TXGN9QT6K61J5). The
    callers (`_full_body`/`_contractor_body`) catch it and render
    `ds.error_state` instead of `ds.empty_state`, so the two cases stay
    visibly distinct on the page."""
    docs = [d for d in library_client.list_reference_documents(audience=_AUDIENCE)
            if d.last_reviewed]
    if not contractor_only and not whole_fleet:
        docs = [d for d in docs if d.project in scope]
    docs.sort(key=lambda d: d.last_reviewed, reverse=True)
    result = []
    for d in docs:
        if contractor_only and "contractor" not in (d.audience or []):
            continue
        result.append({"id": d.id, "kind": d.kind, "title": escape(d.title),
                       "reviewed": d.last_reviewed or ""})
        if len(result) >= n:
            break
    return result


# ---------------------------------------------------------------------------
# Greeting helper
# ---------------------------------------------------------------------------


def _first_name(label: str) -> str:
    """Derive a display first name from a label string.

    If label looks like an email address, take the local part before '@', then
    the first token split on '.', '_', or '+', and title-case it. Otherwise
    take the first whitespace-delimited token.  Returns "" when label is empty.
    """
    if not label:
        return ""
    # Strip the " · full team view" / " · contractor view" suffix added by server.py.
    label = re.split(r"\s*[·•]\s*", label)[0].strip()
    if "@" in label:
        local = label.split("@")[0]
        token = re.split(r"[._+]", local)[0]
        return token.title() if token else ""
    return label.split()[0] if label.split() else ""


# ---------------------------------------------------------------------------
# Icon mapping
# ---------------------------------------------------------------------------

_KIND_ICON = {
    "decision": "📄",
    "arch": "📐",
    "runbook": "📘",
    "reference": "📖",
    "spec": "📋",
    "overview": "🏛️",
    "brand": "🎨",
    "agent": "🤖",
    "skill": "⚡",
    "architecture": "🗂️",
}

_DOT_CLASS = {
    "arch": "brand",
    "decision": "brand",
    "ok": "ok",
    "warn": "warn",
}


def _doc_icon(kind: str) -> str:
    return _KIND_ICON.get(kind, "📄")


def _doc_tag(kind: str) -> str:
    labels = {
        "decision": "ADR", "arch": "ARCH", "runbook": "Runbook",
        "reference": "Ref", "spec": "Spec", "overview": "Overview",
        "brand": "Brand", "agent": "Agent", "skill": "Skill",
        "architecture": "Architecture",
    }
    return labels.get(kind, kind.title())


def _relative_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        d = date.fromisoformat(iso[:10])
        today = date.today()
        diff = (today - d).days
        if diff == 0:
            return "Today"
        if diff == 1:
            return "Yesterday"
        if diff < 7:
            return f"{diff} days ago"
        return iso[:10]
    except ValueError:
        return iso[:10]


# ---------------------------------------------------------------------------
# Full-tier Home body
# ---------------------------------------------------------------------------


def _hero_stat(label: str, val: str, delta: str, href: str) -> str:
    """One hero stat, as a link to the dashboard the number comes from.

    **Every stat is clickable, and `href` has no default on purpose.** These
    were `<div>`s until 2026-09-18, when the operator reported that a number they wanted
    to look into led nowhere. A stat that reports a figure and offers no way to
    the page behind it is a dead end on the one screen that exists to point
    somewhere. Requiring the argument means a stat added later cannot quietly
    reintroduce that: there is nothing to forget to pass.
    """
    return (
        f'<a class="hero-stat" href="{escape(href, quote=True)}">'
        f'<div class="label">{escape(label)}</div>'
        f'<div class="val mono">{val}</div>'
        f'<div class="delta">{delta}</div>'
        f'</a>'
    )


def _full_body(viewer: str | None = None, scope: frozenset[str] = frozenset(),
               whole_fleet: bool = False) -> str:
    spend = _today_spend(whole_fleet)
    health = _ci_health(scope, whole_fleet)
    velocity = _velocity_stat(scope, whole_fleet)
    kb_docs = _kb_doc_count(scope, whole_fleet)
    try:
        docs = _recent_docs(5, scope=scope, whole_fleet=whole_fleet)
        docs_error = None
    except Exception as exc:
        docs = []
        docs_error = f"The Library could not be reached ({type(exc).__name__}). Try again shortly."
    alerts_count = _active_alerts_count(scope, whole_fleet)
    alerts_pill = f"{alerts_count} active" if alerts_count else "All clear"
    # The "waiting on you" strip (ARCH-362 Phase 2) — FULL TIER ONLY, since it is
    # reached only from _full_body. It sits above the hero stats: the stats say how
    # things are going, the strip says what needs the operator now, and the second question
    # is the one they open the page to answer.
    waiting = waiting_build.strip_html(viewer=viewer, scope=scope, whole_fleet=whole_fleet)

    now_mst = orient_time.now()
    day_str = now_mst.strftime("%A · %B %-d, %Y · %-I:%M %p MST")

    # Spend is `None` for a scoped (non-whole-fleet) viewer (see
    # `_today_spend`'s docstring) — left out of the hero grid entirely rather
    # than shown with a placeholder value, per the rule every unfilterable
    # fleet-wide figure in this epic follows.
    hero_stats = "".join(filter(None, [
        # Each destination is the page the figure is computed on, not a generic
        # index: /overview would make the reader hunt for the number they just
        # clicked.
        _hero_stat("Today's Agent-Run Spend", spend["value"], spend["delta"], "/spending")
        if spend else None,
        _hero_stat("CI Health", health["value"], health["delta"], "/health"),
        _hero_stat("Waypoints Passed (7d)", velocity["value"], velocity["delta"],
                   "/velocity"),
    ]))

    spend_pill = f'<div class="qmeta"><span class="pill">{spend["value"]} on agent runs today</span></div>' \
        if spend else ""
    quick_links = f"""
<div class="quick" style="margin-top:0">
  <a class="qcard" href="/overview">
    <span class="qarrow" aria-hidden="true">→</span>
    <div class="qicon" aria-hidden="true">📊</div>
    <h3>Dashboards</h3>
    <p>LLM spend, cost-performance, velocity, code health, and KB retrieval.</p>
    {spend_pill}
  </a>
  <a class="qcard" href="/room">
    <span class="qarrow" aria-hidden="true">→</span>
    <div class="qicon" aria-hidden="true">📖</div>
    <h3>Reading Room</h3>
    <p>Your reading, curated for you — orientation that lasts your whole tenure.</p>
    <div class="qmeta"><span class="pill">Personalized</span></div>
  </a>
  <a class="qcard" href="/reference">
    <span class="qarrow" aria-hidden="true">→</span>
    <div class="qicon" aria-hidden="true">🧠</div>
    <h3>Knowledge base</h3>
    <p>Company and product docs, browsable and searchable.</p>
    <div class="qmeta"><span class="pill">{kb_docs}</span></div>
  </a>
  <a class="qcard" href="/chart-house">
    <span class="qarrow" aria-hidden="true">→</span>
    <div class="qicon" aria-hidden="true">🛠</div>
    <h3>Chart House</h3>
    <p>Every project at once — open work and a cost rollup — plus the issue
       queue, the merge queue, and the roster.</p>
    <div class="qmeta"><span class="pill">Issue queue</span></div>
  </a>
  <a class="qcard" href="/alerts">
    <span class="qarrow" aria-hidden="true">→</span>
    <div class="qicon" aria-hidden="true">🚨</div>
    <h3>Alerts</h3>
    <p>Dashboards metrics that have crossed their own policy threshold.</p>
    <div class="qmeta"><span class="pill">{alerts_pill}</span></div>
  </a>
</div>"""

    if docs:
        doc_items = []
        for d in docs:
            # Link each doc to its own page (issue_01M08YBCDTEFJ6N9S798ERF89P
            # step 3) rather than the KB browse drawer (/reference#doc=<id>) — a
            # bare /orientation link lost the doc once already (the operator
            # 2026-07-16), and the drawer link cost every click the whole
            # 10 MB /reference bundle before it could open. quote() with
            # safe="/" keeps the id's own hierarchical slashes readable in
            # the URL while still escaping anything else.
            doc_items.append(
                f'<a class="doc-item" href="/reference/doc/{quote(d["id"], safe="/")}">'
                f'<div class="doc-ico" aria-hidden="true">{_doc_icon(d["kind"])}</div>'
                f'<div class="doc-body">'
                f'<div class="dt">{d["title"]}</div>'
                f'<div class="dm">{_relative_date(d["reviewed"])}</div>'
                f'</div>'
                f'<span class="doc-tag">{_doc_tag(d["kind"])}</span>'
                f'</a>'
            )
        docs_html = "".join(doc_items)
    elif docs_error:
        docs_html = ds.error_state(docs_error)
    else:
        docs_html = ds.empty_state("No docs indexed yet.")

    return f"""<header class="hero">
  <div class="hero-inner">
    <div class="hero-head">
      <div>
        <div class="hero-eyebrow">{day_str}</div>
        <h1>__GREETING_FULL__</h1>
        <p class="hero-sub">Here is where things stand.</p>
      </div>
      {ds.ship_mark()}
    </div>
    {waiting}
    <div class="hero-stats">
      {hero_stats}
    </div>
  </div>
</header>

<section class="home-section">
  <div class="eyebrow">Jump back in</div>
  {quick_links}
</section>

<section class="home-section">
  <div class="panel">
    <div class="panel-head">
      <h3>Recent docs</h3>
      <a class="panel-more" href="/reference">Browse →</a>
    </div>
    {docs_html}
  </div>
</section>

<div class="home-foot">
  <span>Convivy substrate · team.convivy.com</span>
  <span class="mono">{kb_docs} · team site</span>
</div>"""


# ---------------------------------------------------------------------------
# Contractor Home body
# ---------------------------------------------------------------------------


def _contractor_body() -> str:
    now_mst = orient_time.now()
    day_str = now_mst.strftime("%A · %B %-d, %Y · %-I:%M %p MST")
    try:
        docs = _recent_docs(5, contractor_only=True)
        docs_error = None
    except Exception as exc:
        docs = []
        docs_error = f"The Library could not be reached ({type(exc).__name__}). Try again shortly."

    pinned = [
        {
            "icon": "🧭",
            "title": "Onboarding — your first week",
            "subtitle": "Start here",
            "tag": "Guide",
        },
        {
            "icon": "🏛️",
            "title": "Convivy — company overview",
            "subtitle": "Edited this week",
            "tag": "Overview",
        },
        {
            "icon": "📦",
            "title": "Products at a glance",
            "subtitle": "Vivy · Constance · Sous Chef",
            "tag": "Products",
        },
        {
            "icon": "🎨",
            "title": "Brand kit & assets",
            "subtitle": "Logo, palette, type",
            "tag": "Brand",
        },
        {
            "icon": "✍️",
            "title": "Write Better — the writing standard",
            "subtitle": "How we write here",
            "tag": "Guide",
        },
    ]

    if docs:
        feed_items = []
        for d in docs:
            feed_items.append(
                f'<div class="feed-item">'
                f'<span class="feed-dot muted" aria-hidden="true"></span>'
                f'<div class="feed-body">'
                f'<div class="t"><b>{d["title"]}</b></div>'
                f'<div class="m">{_doc_tag(d["kind"])} · {_relative_date(d["reviewed"])}</div>'
                f'</div>'
                f'</div>'
            )
        feed_html = "".join(feed_items)
    elif docs_error:
        feed_html = ds.error_state(docs_error)
    else:
        feed_html = ds.empty_state("No recent docs.")

    pinned_html = "".join(
        f'<a class="doc-item" href="/room">'
        f'<div class="doc-ico" aria-hidden="true">{p["icon"]}</div>'
        f'<div class="doc-body">'
        f'<div class="dt">{p["title"]}</div>'
        f'<div class="dm">{p["subtitle"]}</div>'
        f'</div>'
        f'<span class="doc-tag">{p["tag"]}</span>'
        f'</a>'
        for p in pinned
    )

    return f"""<header class="hero">
  <div class="hero-inner">
    <div class="hero-head">
      <div>
        <div class="hero-eyebrow">{day_str}</div>
        <h1>__GREETING_CONTRACTOR__</h1>
        <p class="hero-sub">Everything you need to find your way around Convivy — the company, the products, and how we work.</p>
      </div>
      {ds.ship_mark()}
    </div>
    <div class="hero-search">
      <span class="ico" aria-hidden="true">🔍</span>
      <label for="docSearch" class="sr-only">Search your Reading Room</label>
      <input id="docSearch" type="search" placeholder="Search docs — brand, getting started, products…" autocomplete="off">
    </div>
  </div>
</header>

<section class="home-section">
  <div class="eyebrow">Start here</div>
  <div class="quick">
    <a class="qcard" href="/room">
      <span class="qarrow" aria-hidden="true">→</span>
      <div class="qicon" aria-hidden="true">🧭</div>
      <h3>Getting started</h3>
      <p>Your first week: tools, access, who's who, and how we communicate.</p>
      <div class="qmeta"><span class="pill">5 docs</span> ~15 min read</div>
    </a>
    <a class="qcard" href="/room">
      <span class="qarrow" aria-hidden="true">→</span>
      <div class="qicon" aria-hidden="true">🏛️</div>
      <h3>Company overview</h3>
      <p>What Convivy builds, the premise, and how the team is organized.</p>
      <div class="qmeta"><span class="pill">Overview</span> updated this week</div>
    </a>
    <a class="qcard" href="/room">
      <span class="qarrow" aria-hidden="true">→</span>
      <div class="qicon" aria-hidden="true">📦</div>
      <h3>Products</h3>
      <p>Vivy, Constance, and Sous Chef — what each one is and who it's for.</p>
      <div class="qmeta"><span class="pill">3 products</span> overviews</div>
    </a>
    <a class="qcard" href="/room">
      <span class="qarrow" aria-hidden="true">→</span>
      <div class="qicon" aria-hidden="true">🎨</div>
      <h3>Brand &amp; voice</h3>
      <p>Logo, palette, typography, and the Write Better writing standard.</p>
      <div class="qmeta"><span class="pill">Brand kit</span> + style guide</div>
    </a>
  </div>
</section>

<section class="home-section">
  <div class="cols">
    <div class="panel">
      <div class="panel-head">
        <h3>What's new in docs</h3>
        <a class="panel-more" href="/room">Browse all →</a>
      </div>
      <div class="feed">
        {feed_html}
      </div>
    </div>
    <div class="panel">
      <div class="panel-head">
        <h3>Pinned for you</h3>
        <a class="panel-more" href="/room">All docs →</a>
      </div>
      {pinned_html}
    </div>
  </div>
</section>

<div class="home-foot">
  <span>Convivy · team.convivy.com</span>
  <span class="mono">Reading Room</span>
</div>"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def live_html(tier: str = "full", label: str = "", email: str | None = None, *,
              scope: frozenset[str] = frozenset(), whole_fleet: bool = False) -> str:
    """Render the adaptive Home at request time.

    Greetings use the caller-supplied label to show a first name ("Welcome back,
    the operator."). The nav avatar chip uses __WHOAMI__ so server.py can substitute the
    full label at serve time. Pass label="" for static builds — the greeting
    falls back to a name-less form.

    `scope` and `whole_fleet` reach the two alert counts (E19·3,
    issue_01M35K5EAZD85FBYXEBQVRKVZV); server.py passes the viewer's. The
    defaults count no alerts.
    """
    nav_html = ds.nav_component("/", tier)
    body = _full_body(email, scope, whole_fleet) if tier == "full" else _contractor_body()
    html = ds.page_shell(
        title="",
        nav_html=nav_html,
        body=body,
        # Only the full-tier body renders the strip, but the rules are harmless on
        # the contractor page and shipping one stylesheet keeps the two homes from
        # diverging into per-tier CSS, which is how a tier-specific render bug hides.
        extra_css=waiting_build.WAITING_CSS,
        auto_refresh_seconds=ds.SPEND_REFRESH_SECONDS,
    )
    # Substitute the per-hero greeting placeholders now (before server.py touches it),
    # so the hero shows a real first name while the nav chip (__WHOAMI__) stays for
    # server.py to fill with the full label.
    first = escape(_first_name(label))
    if first:
        full_greeting = f"Welcome back, {first}."
        contractor_greeting = f"Welcome, {first}."
    else:
        full_greeting = "Welcome back."
        contractor_greeting = "Welcome."
    html = html.replace("__GREETING_FULL__", full_greeting, 1)
    html = html.replace("__GREETING_CONTRACTOR__", contractor_greeting, 1)
    return html


def build_static() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for tier in ("full", "contractor"):
        html = live_html(tier)
        name = f"home.{tier}.html"
        (OUT / name).write_text(html, encoding="utf-8")
        print(f"Wrote site/{name}")


if __name__ == "__main__":
    build_static()
