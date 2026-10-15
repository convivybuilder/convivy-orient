"""Loader unit tests for home_build and overview_build.

Covers the bugs caught in the PR-16 review:
  2. Overview Cost-Performance card shows $/merged, not CI pass rate.
  3. _recent_docs(contractor_only=True) is default-deny on the `contractor`
     audience marker (ARCH-228 Phase 5).
  5. _first_name() derives a first name from an email label.

And the dashboards epic's velocity item (issue_01M2F3QV06RFYTQSFS0MJ4F7PF):
  1. `home_build._velocity_stat()` headlines waypoints passed, computed live
     from `waypoints.course_progress()` (mocked `library_client`, never a
     live call) — replacing the old merged-PR-count headline.

KB-backed tests seed `library_fake` rather than a real sqlite index
(issue_01M1MFJA52QF2Q2K52MYJ65ZSX) — `_kb_doc_count`/`_recent_docs` read
through `library_client.list_reference_documents` now, not
`~/.knowledge-index/index.db`.
"""
import datetime as dt
import json
import pathlib

import crew_registry
import home_build
import design_system as ds
import library_client
import library_fake
import overview_build
from models import utcnow


def _course(course_id, product, title, *, version=1, entries):
    return library_client.CourseRecord(
        id=course_id, product=product, title=title, status="live", version=version,
        entries=entries, created_by="ops-mcp", created_at=None,
        updated_by="ops-mcp", updated_at=None)


def _entry(issue_id, state, title=None, resolved_at=None):
    return library_client.CourseEntry(issue_id=issue_id, state=state, title=title or issue_id,
                                      resolved_at=resolved_at)


# ---------------------------------------------------------------------------
# Bug 1 (superseded) — velocity headlines waypoints passed, not merged PRs.
# Now windowed (dashboards epic issue_01M2F3Q1B8J9E0WC5J1F1CMWA1, item
# issue_01M2F3QV06RFYTQSFS0MJ4F7PF): the headline is waypoints passed in the
# trailing 7 days, and the sub-line carries all-time position.
# ---------------------------------------------------------------------------


def test_velocity_stat_headlines_waypoints_passed_within_7_days(monkeypatch):
    now = utcnow()
    course = _course("course_1", "bosun", "Make dispatch accountable", entries=[
        _entry("i1", "fixed", resolved_at=now - dt.timedelta(days=3)),
        _entry("i2", "wontfix", resolved_at=now - dt.timedelta(days=20)),
        _entry("i3", "open"),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    result = home_build._velocity_stat()
    assert result["value"] == "1 passed"
    assert result["delta"] == "2 of 3 passed"


def test_velocity_stat_reports_zero_when_no_terminal_entries(monkeypatch):
    course = _course("course_1", "vivy", "Ship it", entries=[_entry("i1", "open")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    result = home_build._velocity_stat()
    assert result["value"] == "0 passed"


def test_velocity_stat_reports_no_live_courses_rather_than_zero(monkeypatch):
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [])
    result = home_build._velocity_stat()
    assert result["value"] == "0 passed"
    assert result["delta"] == "no live Courses"


def test_velocity_stat_reads_unavailable_on_library_outage(monkeypatch):
    def boom(**k):
        raise ConnectionError("Library unreachable")

    monkeypatch.setattr(library_client, "list_courses", boom)
    result = home_build._velocity_stat()
    assert result["value"] == "—"
    assert result["delta"] == "unavailable"


def test_old_prs_merged_headline_is_gone(monkeypatch):
    """Plants the old merged-PR-count behaviour and confirms it no longer
    drives the headline — a velocity.json with a large prs_merged count must
    not appear in `_velocity_stat()`'s value now that it reads Courses."""
    now = utcnow()
    course = _course("course_1", "vivy", "Ship it",
                     entries=[_entry("i1", "fixed", resolved_at=now - dt.timedelta(days=1))])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    result = home_build._velocity_stat()
    assert "PR" not in result["value"]
    assert result["value"] == "1 passed"


def test_velocity_stat_a_passed_entry_with_no_resolved_at_is_not_in_the_7d_window(monkeypatch):
    """A passed entry with no `resolved_at` (a degraded `get_course` detail
    call, per `waypoints`'s own docstring) still counts in the sub-line's
    position but not in the 7-day headline — there's no transition time to
    test against the window, and guessing one would misreport a pace this
    codebase can't actually measure."""
    course = _course("course_1", "vivy", "Ship it", entries=[_entry("i1", "fixed")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    result = home_build._velocity_stat()
    assert result["value"] == "0 passed"
    assert result["delta"] == "1 of 1 passed"


# ---------------------------------------------------------------------------
# Bug 2 — Overview Cost-Performance is NOT the CI pass rate
# ---------------------------------------------------------------------------


def test_overview_cost_performance_differs_from_ci_health(tmp_path, monkeypatch):
    """The Cost-Performance card must show a cost/efficiency signal, not CI health.

    We set up a scorecard with one merged PR and a known cost so we can assert
    the computed $/merged value, then confirm it differs from the CI pass rate
    that the Code Health card shows.
    """
    import dashboard_build as db

    sc = tmp_path / "scorecard.jsonl"
    sc.write_text(json.dumps({
        "run_id": "r1", "date": "2026-06-21", "agent": "coder",
        "model": "claude-sonnet-4-6", "billing_source": "subscription",
        "repo": "vivy", "cost_usd": 0.50, "pr_state": "MERGED", "merged": True,
        "survived": True, "ci_first_pass": True, "pr_number": 42,
    }) + "\n")
    monkeypatch.setattr(db, "SCORECARD", sc)
    monkeypatch.setattr(db, "PA_LEDGER", tmp_path / "absent.jsonl")
    monkeypatch.setattr(db, "JUDGE", tmp_path / "absent2.jsonl")

    cp = overview_build._cost_per_merged_pr(db.normalize())
    # One merged PR at $0.50 → $/merged should be "$0.50"
    assert cp["value"] == "$0.50", f"Expected '$0.50', got {cp['value']!r}"
    assert "merged PR" in cp["delta"]

    # The CI pass rate from home_build reads health.json (through
    # code_health_build.load_snapshot, since E19·4's scoping of /health —
    # issue_01M35K5JQ7Z7MHYZGWRSB5F85M), not the scorecard. We just need to
    # confirm the two values differ — stub the snapshot load so
    # home_build._ci_health(whole_fleet=True) returns a known percentage,
    # and compare.
    import code_health_build
    monkeypatch.setattr(code_health_build, "load_snapshot",
                        lambda: ("2026-06-21T00:00:00Z", [{"repo": "vivy", "ci_pass_rate": 0.80}]))

    health = home_build._ci_health()
    assert health["value"] != cp["value"], (
        "Bug 2: Cost-Performance card value must differ from CI pass rate. "
        f"Both returned {cp['value']!r}."
    )


def test_overview_cost_performance_graceful_on_empty_scorecard(tmp_path, monkeypatch):
    import dashboard_build as db

    monkeypatch.setattr(db, "SCORECARD", tmp_path / "absent.jsonl")
    monkeypatch.setattr(db, "PA_LEDGER", tmp_path / "absent2.jsonl")
    monkeypatch.setattr(db, "JUDGE", tmp_path / "absent3.jsonl")

    cp = overview_build._cost_per_merged_pr(db.normalize())
    assert cp["value"] == "—"


# ---------------------------------------------------------------------------
# ARCH-228 Phase 5 — contractor _recent_docs is default-deny: a doc surfaces in
# the contractor feed only if its audience includes "contractor" (the old
# kind/title/project blocklist is gone).
# ---------------------------------------------------------------------------


def test_contractor_recent_docs_excludes_untagged(monkeypatch):
    """Default-deny: a team-only doc (no `contractor` marker) must not surface in
    the contractor feed; a [team, contractor] doc must."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="shared/overview", title="Company overview",
                                    audience=["team", "contractor"], last_reviewed="2026-06-20")
    # team-only — not opted in for contractors; must be filtered out.
    library_fake.seed_reference_doc(id="shared/people", title="People",
                                    audience=["team"], last_reviewed="2026-06-19")

    results = home_build._recent_docs(n=10, contractor_only=True)
    ids = {d["id"] for d in results}

    assert "shared/people" not in ids, (
        "default-deny: a team-only doc must not surface in the contractor feed."
    )
    assert "shared/overview" in ids


def test_contractor_recent_docs_marker_not_kind_or_title(monkeypatch):
    """The marker is the only axis: a contractor-tagged runbook IS allowed, and an
    untagged overview is NOT — the old kind/title/project blocklist is gone."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="vivy/runbooks/deploy", kind="runbook",
                                    title="Deploy runbook", audience=["team", "contractor"],
                                    last_reviewed="2026-06-20")
    library_fake.seed_reference_doc(id="shared/overview/secret", kind="overview",
                                    title="Company overview", audience=["team"],
                                    last_reviewed="2026-06-19")

    results = home_build._recent_docs(n=10, contractor_only=True)
    ids = {d["id"] for d in results}

    assert "vivy/runbooks/deploy" in ids, "a contractor-tagged runbook is allowed"
    assert "shared/overview/secret" not in ids, "an untagged overview is denied"


def test_contractor_recent_docs_requires_team(monkeypatch):
    """The team gate still applies: a doc not on the orientation site (no `team`)
    is excluded even if it carries `contractor`."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="shared/ok", title="Visible",
                                    audience=["team", "contractor"], last_reviewed="2026-06-20")
    library_fake.seed_reference_doc(id="internal/secret", title="Internal",
                                    audience=["contractor"], last_reviewed="2026-06-19")

    results = home_build._recent_docs(n=10, contractor_only=True)
    ids = {d["id"] for d in results}

    assert "internal/secret" not in ids, "the team gate still applies"
    assert "shared/ok" in ids


def test_contractor_recent_docs_returns_populated_list(monkeypatch):
    """The contractor feed returns docs when contractor-tagged docs exist."""
    library_fake.patch(monkeypatch)
    for i in range(5):
        library_fake.seed_reference_doc(id=f"shared/doc-{i}", title=f"Doc {i}",
                                        audience=["team", "contractor"],
                                        last_reviewed=f"2026-06-{10 + i:02d}")

    results = home_build._recent_docs(n=5, contractor_only=True)
    assert len(results) >= 1, (
        "the contractor 'What's new in docs' panel must return docs when "
        "contractor-tagged docs exist in the index."
    )


# ---------------------------------------------------------------------------
# A Library failure must not read like an empty corpus (this repo's own
# recurring defect, issue_01M0TKX6DW2E1TXGN9QT6K61J5) — `_recent_docs` raises
# and the two bodies render `ds.error_state`, distinct from `ds.empty_state`.
# ---------------------------------------------------------------------------


def test_recent_docs_raises_on_a_library_failure(monkeypatch):
    """`_recent_docs` must not swallow a Library failure to `[]` — that is
    exactly the collapse that made an unreachable index indistinguishable
    from a genuinely empty one."""
    import library_client
    import pytest

    def _boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_reference_documents", _boom)
    with pytest.raises(RuntimeError):
        home_build._recent_docs(5)


def test_full_home_shows_error_state_not_empty_state_on_library_failure(monkeypatch):
    """The full-tier Recent Docs panel renders `error-state`, not
    `empty-state`, when the Library call raises — a reader must be able to
    tell "the Library is down" from "nobody has written anything yet"."""
    import library_client

    def _boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_reference_documents", _boom)
    html = home_build._full_body()
    assert 'class="error-state"' in html
    assert 'class="empty-state"' not in html
    assert "RuntimeError" in html


def test_contractor_home_shows_error_state_not_empty_state_on_library_failure(monkeypatch):
    import library_client

    def _boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_reference_documents", _boom)
    html = home_build._contractor_body()
    assert 'class="error-state"' in html
    assert 'class="empty-state"' not in html


def test_full_home_shows_empty_state_when_the_library_has_no_docs(monkeypatch):
    """The other side of the pair above — a successful call that returns
    nothing still renders `empty-state`, not `error-state`. Without this test,
    a change that made every Library outcome render `error-state` would pass
    the failure test above just as well."""
    library_fake.patch(monkeypatch)
    html = home_build._full_body()
    assert 'class="empty-state"' in html
    assert 'class="error-state"' not in html


def test_kb_doc_count_says_library_unreachable_not_no_docs_indexed(monkeypatch):
    """The Home footer and the Knowledge-base quick-link pill both read
    `_kb_doc_count()` — it must say the Library is unreachable, not the text
    it uses for a Library that answered with zero documents, or the two
    outcomes look the same everywhere this value is shown."""
    import library_client

    def _boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_reference_documents", _boom)
    assert "Library unreachable" in home_build._kb_doc_count()
    assert "no docs indexed" not in home_build._kb_doc_count()

    library_fake.patch(monkeypatch)
    assert home_build._kb_doc_count() == "no docs indexed"


# ---------------------------------------------------------------------------
# Recent activity — Home's ARCH-log panel is retired (the operator, 2026-08-26; the log
# itself froze at ARCH-441, 2026-08-03). `_recent_activity`, the thin wrapper
# over `engineering_build.arch_docs()` that fed it, is gone with the panel;
# arch_docs()'s own parsing (title/date fallback, the superseded-blockquote
# skip, caching) is tested directly in test_engineering_build.py, which is
# unaffected by this — Home never had its own parse to test.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bug 5 — _first_name derives a first name from an email label
# ---------------------------------------------------------------------------


def test_first_name_from_email():
    assert home_build._first_name("operator@example.com") == "Operator"


def test_first_name_from_email_with_suffix():
    # server.py appends " · full team view" to the label
    assert home_build._first_name("operator@example.com · full team view") == "Operator"


def test_first_name_from_email_dotted_local():
    assert home_build._first_name("maya.smith@acme.example") == "Maya"


def test_first_name_from_plain_name():
    assert home_build._first_name("Dana Smith") == "Dana"


def test_first_name_empty_returns_empty():
    assert home_build._first_name("") == ""


def test_live_html_full_shows_first_name_in_greeting():
    html = home_build.live_html("full", "operator@example.com · full team view")
    assert "Welcome back, Operator." in html, (
        "Bug 5: the full-tier hero must show 'Welcome back, Operator.' when label is "
        "'operator@example.com · full team view'."
    )
    # The nav chip should still carry __WHOAMI__ for server.py to fill
    assert "__WHOAMI__" in html


def test_live_html_contractor_shows_first_name_in_greeting():
    html = home_build.live_html("contractor", "maya@acme.example · contractor view")
    assert "Welcome, Maya." in html, (
        "Bug 5: the contractor hero must show 'Welcome, Maya.' for 'maya@acme.example'."
    )


def test_live_html_empty_label_falls_back_to_nameless_greeting():
    html_full = home_build.live_html("full", "")
    assert "Welcome back." in html_full

    html_co = home_build.live_html("contractor", "")
    assert "Welcome." in html_co


# ---------------------------------------------------------------------------
# Smoke render — live_html must not raise and must return non-empty HTML
# ---------------------------------------------------------------------------


def test_live_html_full_does_not_raise():
    html = home_build.live_html("full", "operator@example.com · full team view")
    assert html and "<html" in html.lower()


def test_live_html_contractor_does_not_raise():
    html = home_build.live_html("contractor", "maya@acme.example · contractor view")
    assert html and "<html" in html.lower()


def test_overview_live_html_does_not_raise():
    html = overview_build.live_html()
    assert html and "<html" in html.lower()


def test_overview_live_html_has_hero_band():
    html = overview_build.live_html()
    assert 'class="hero"' in html, (
        "Bug 8: the Overview page must include the hero gradient band."
    )
    assert "Dashboards" in html
    assert "Overview" in html


# --- Recent-Docs deep links (the operator 2026-07-16: items linked a generic /orientation) --


def test_full_home_recent_docs_link_to_the_documents_own_page(monkeypatch):
    """Each full-tier Recent Docs item links the document's own page
    (/reference/doc/<id>, issue_01M08YBCDTEFJ6N9S798ERF89P step 3) rather than
    the KB browse drawer (/reference#doc=<id>), which cost every click the whole
    /reference bundle before the drawer could open. The id's own slashes stay
    literal in the URL; anything else would be percent-encoded."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="constance/reference/spine-brief",
                                    title="Spine brief", audience=["team"],
                                    last_reviewed="2026-07-16")

    html = home_build.live_html("full", "t", scope=frozenset({"proj"}), whole_fleet=True)
    assert 'href="/reference/doc/constance/reference/spine-brief"' in html
    assert "/reference#doc=" not in html
    # no full-tier doc item may still carry the old generic /orientation href
    assert 'class="doc-item" href="/orientation"' not in html


def test_browse_js_handles_doc_hash():
    """The KB browse carries the #doc= deep-link handler the home items target."""
    import engineering_build
    html = engineering_build.render([], frozenset(), True)
    assert "#doc=(.+)" in html and "openFromHash" in html and "hashchange" in html


def test_browse_deep_link_deferred_past_drawer_init():
    """The initial (page-load) openFromHash must fire AFTER the drawer is wired
    (on DOMContentLoaded), or the drawer never opens and the deep link dumps the
    viewer on /reference top-level (the operator 2026-07-17). It must be gated on window
    'load' / readyState, never called bare-synchronously at parse time."""
    import re
    import engineering_build
    html = engineering_build.render([], frozenset(), True)
    assert "addEventListener('load', openFromHash)" in html
    # no bare synchronous `openFromHash();` call (only inside the readyState guard)
    assert not re.search(r"(?<![.'])\n\s*openFromHash\(\);", html)


def test_home_promotes_the_issue_queue_not_the_roster():
    """The fourth quick-card pointed at Roster, the same page the operator needs least and
    the same one the rail used to land on. Home is the highest-traffic page, so
    it was the second place teaching Roster-first (2026-07-19 IA review).

    Home reaches the work queue through the Chart House quick-card, which names
    the issue queue in its own copy. The "waiting on you" strip no longer links
    `/engineering/corrections`: its Corrections tile was removed with the
    `awaiting-decision` state (issue_01M2F3QFTVZEFVTF0QJ5C7W7NC), because crews
    work corrections without the operator, so an open correction is not
    something waiting on the operator."""
    html = home_build._full_body()
    assert 'href="/chart-house"' in html
    assert "<h3>Chart House</h3>" in html
    assert 'href="/engineering/roster"' not in html


# ---------------------------------------------------------------------------
# The grouped Overview index (nav-reshape spec §4, issue_01M0TKX6DW2E1TXGN9QT6K61J5)
# ---------------------------------------------------------------------------


def test_build_body_has_three_group_headings():
    html = overview_build._build_body()
    assert '<h2 class="overview-group-title">Money</h2>' in html
    assert '<h2 class="overview-group-title">Delivery</h2>' in html
    assert '<h2 class="overview-group-title">Health</h2>' in html
    # Alerts is a needs-me signal, not a browsable dashboard — no card for it,
    # it keeps its pinned nav entry and a Home tile instead.
    assert 'href="/alerts"' not in html


def test_velocity_section_shows_the_same_number_as_the_front_page(monkeypatch):
    """/overview's velocity card and the front page's hero stat both headline
    waypoints passed in the trailing 7 days, and must agree for the SAME
    viewer scope — they call the same `home_build._velocity_stat()`
    (dashboards epic issue_01M2F3Q1B8J9E0WC5J1F1CMWA1). Scoped to "bosun"
    explicitly (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) rather than relying
    on either function's default, now that both are scope-aware."""
    now = utcnow()
    course = _course("course_1", "bosun", "Make dispatch accountable", entries=[
        _entry("i1", "fixed", resolved_at=now - dt.timedelta(days=1)), _entry("i2", "open"),
    ])
    # An out-of-scope Course with two passes of its own: an unfiltered count
    # reads "3 passed", so the scope filter has to run for "1 passed" to hold.
    other = _course("course_2", "vivy", "Ship taste maps", entries=[
        _entry("i3", "fixed", resolved_at=now - dt.timedelta(days=1)),
        _entry("i4", "fixed", resolved_at=now - dt.timedelta(days=2)),
    ])
    courses = {"course_1": course, "course_2": other}
    monkeypatch.setattr(library_client, "list_courses", lambda **k: list(courses.values()))
    monkeypatch.setattr(library_client, "get_course", lambda cid: courses[cid])

    scope = frozenset({"bosun"})
    card_html = overview_build._velocity_section(scope, False)
    hero_html = home_build._full_body(None, scope, False)
    assert "1 passed" in card_html
    assert "1 passed" in hero_html
    assert "1 of 2 passed" in card_html


def test_activity_stalled_when_producer_generated_at_is_old(tmp_path, monkeypatch):
    """A snapshot older than the 15-minute producer's own cadence renders the
    stalled banner ABOVE the stat, never instead of it."""
    import activity_build

    p = tmp_path / "sessions.json"
    p.write_text(json.dumps({
        "generated_at": "2020-01-01T00:00:00+00:00",
        "session_count": 3, "sessions": [],
    }))
    monkeypatch.setattr(activity_build, "ACTIVITY_FILE", p)
    html = overview_build._activity_section()
    assert 'class="stalled-banner"' in html
    assert "3 sessions" in html


def test_spending_stalled_when_the_daily_harvest_snapshot_is_old(monkeypatch):
    """The 30-hour daily-harvest threshold, forced over. Untested branches are
    how an inverted comparison or a wrong constant ships silently in a section
    whose entire premise is that it must not lie about freshness (PR #204
    review)."""
    import dashboard_build
    monkeypatch.setattr(dashboard_build, "load_latest_snapshot",
                        lambda: {"generated_at": "2020-01-01T00:00:00+00:00"})
    monkeypatch.setattr(home_build, "_today_spend",
                        lambda whole_fleet: {"value": "$12.00", "delta": "today"})
    html = overview_build._spending_section()
    assert 'class="stalled-banner"' in html
    assert "$12.00" in html            # banner ABOVE the stat, never instead


def test_spending_not_stalled_when_the_snapshot_is_fresh(monkeypatch):
    """The other side of the same boundary — without this the test above
    passes even if the comparison is inverted."""
    import dashboard_build
    import orient_time
    fresh = orient_time.now().isoformat()
    monkeypatch.setattr(dashboard_build, "load_latest_snapshot",
                        lambda: {"generated_at": fresh})
    monkeypatch.setattr(home_build, "_today_spend",
                        lambda whole_fleet: {"value": "$12.00", "delta": "today"})
    html = overview_build._spending_section()
    assert 'class="stalled-banner"' not in html
    assert "$12.00" in html


def test_code_health_stalled_when_the_daily_harvest_snapshot_is_old(monkeypatch):
    """Shares `_DAILY_HARVEST_STALLED_AFTER_HOURS` with spending, and shared
    constants are exactly what a single test lets drift."""
    import code_health_build
    monkeypatch.setattr(code_health_build, "load_snapshot",
                        lambda: ("2020-01-01T00:00:00+00:00", [{"repo": "x"}]))
    monkeypatch.setattr(home_build, "_ci_health",
                        lambda scope, whole_fleet: {"value": "97.8%", "delta": "avg CI pass rate"})
    html = overview_build._code_health_section()
    assert 'class="stalled-banner"' in html
    assert "97.8%" in html


def test_code_health_not_stalled_when_the_snapshot_is_fresh(monkeypatch):
    import code_health_build
    import orient_time
    monkeypatch.setattr(code_health_build, "load_snapshot",
                        lambda: (orient_time.now().isoformat(), [{"repo": "x"}]))
    monkeypatch.setattr(home_build, "_ci_health",
                        lambda scope, whole_fleet: {"value": "97.8%", "delta": "avg CI pass rate"})
    html = overview_build._code_health_section()
    assert 'class="stalled-banner"' not in html


def test_e2e_stalled_when_the_newest_ci_signal_is_old(monkeypatch):
    """The card this whole epic opened on. A green "1/1 passing" with a
    six-day-old feed is exactly the reading the banner exists to contradict,
    so the over-threshold branch needs its own test rather than being reached
    only incidentally through the disabled-flag case."""
    import datetime as dt
    import library_client

    class _Sig:
        product = "vivy"
        stream = "e2e-stage-smoke"
        kind = "test-result"
        meta = {"status": "pass"}
        # NAIVE utc, which is what library_client._iso guarantees and what
        # models.utcnow returns — the whole datastore's convention. A
        # tz-aware fixture here would be testing a shape the client cannot
        # produce, and would fail on the subtraction rather than on the branch.
        produced_at = dt.datetime(2020, 1, 1)
        ingested_at = produced_at

    monkeypatch.setattr(library_client, "list_signals", lambda **k: [_Sig()])
    html = overview_build._e2e_section()
    assert 'class="stalled-banner"' in html
    assert "1/1 passing" in html       # the healthy-looking number is still there


def test_e2e_not_stalled_when_the_newest_ci_signal_is_fresh(monkeypatch):
    """The pair the stalled test needs, and the one I left off.

    Spending and code-health each got both sides on the argument that without
    the pair an inverted comparison passes the stalled assertion just as well.
    E2E got only the old-signal side, so nothing would have failed if its
    check regressed to always-on — on the card this module's own docstring
    calls the one this epic opened on (PR #204 review)."""
    import datetime as dt
    import library_client
    from models import utcnow

    fresh = utcnow() - dt.timedelta(hours=1)

    class _Sig:
        product = "vivy"
        stream = "e2e-stage-smoke"
        kind = "test-result"
        meta = {"status": "pass"}
        produced_at = fresh
        ingested_at = fresh

    monkeypatch.setattr(library_client, "list_signals", lambda **k: [_Sig()])
    html = overview_build._e2e_section()
    assert 'class="stalled-banner"' not in html
    assert "1/1 passing" in html


def test_the_stalled_card_detection_is_pinned_to_what_stalled_state_emits():
    """`_overview_card` decides whether to drop the aria-label by sniffing a
    literal substring out of the section's HTML, which couples that decision
    to `design_system.stalled_state`'s exact markup (PR #204 review).

    The coupling is left in place — this module builds raw HTML strings
    throughout — but it is pinned here rather than left silent, because the
    failure mode is that a markup tweak in design_system quietly restores the
    accessibility bug this round fixed. If stalled_state stops emitting that
    class, this fails loudly instead."""
    banner = ds.stalled_state(feed="CI test results", newest="2026-08-18, 6d ago",
                              expected_cadence="daily", why="The feed is down.")
    card = overview_build._overview_card("/engineering/e2e", "E2E",
                                         "Suites Passing", banner)
    assert "aria-label" not in card, (
        "stalled_state's markup no longer matches what _overview_card sniffs "
        "for, so a stalled card would keep the aria-label that hides its "
        "warning from screen readers")


def test_a_stalled_card_drops_its_aria_label_so_the_warning_is_announced(
        monkeypatch):
    """An `aria-label` on the anchor overrides accessible-name computation for
    the whole subtree, so the banner text — the one thing this section exists
    to surface — was excluded from what a screen reader announces, and
    `role="status"` does not rescue content present at initial render (PR #204
    review). A stalled card drops the override; a healthy one keeps it."""
    healthy = overview_build._overview_card(
        "/spending", "Spending", "Today's LLM Spend", "<div>fine</div>")
    assert 'aria-label="Spending dashboard"' in healthy

    stalled = overview_build._overview_card(
        "/engineering/e2e", "E2E", "Suites Passing",
        '<div class="stalled-banner" role="status"><p>Newest result 6d ago.</p></div>')
    assert "aria-label" not in stalled
    assert "Newest result 6d ago." in stalled


def test_one_failing_loader_does_not_take_down_the_page(monkeypatch):
    """Graceful degradation, generalized from five cards to ten: a broken
    loader renders its own error card and the rest of the page — hero, other
    groups, other cards — still renders."""
    import security_build

    def _boom():
        raise RuntimeError("security-report.json is corrupt")

    monkeypatch.setattr(security_build, "load", _boom)
    # whole_fleet=True: Security is a `fleet_only` card (E19·4,
    # issue_01M35K5JQ7Z7MHYZGWRSB5F85M) and would not render at all for a
    # scoped viewer, which this test is not exercising.
    html = overview_build.live_html(frozenset(), True)
    assert "<html" in html.lower()
    assert 'class="error-state"' in html
    assert "Could not load Security data." in html
    # a sibling card in a different group still rendered normally
    assert "Spending" in html


# ---------------------------------------------------------------------------
# The invariant this whole clause exists for: for a given snapshot, the
# /overview card and the dashboard's own page reach the SAME stalled verdict
# — the failure mode was the index warning of a dead feed while the
# destination page rendered calmly (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment
# 902). Each pair below feeds one stale snapshot to both readers.
# ---------------------------------------------------------------------------


def test_spending_card_and_page_agree_when_stalled(monkeypatch):
    import dashboard_build
    monkeypatch.setattr(dashboard_build, "load_latest_snapshot",
                        lambda: {"generated_at": "2020-01-01T00:00:00Z"})
    monkeypatch.setattr(home_build, "_today_spend",
                        lambda whole_fleet: {"value": "$12.00", "delta": "today"})
    card_html = overview_build._spending_section()
    page_html = dashboard_build.spending_render()
    assert 'class="stalled-banner"' in card_html
    assert 'class="stalled-banner"' in page_html


def test_code_health_card_and_page_agree_when_stalled(monkeypatch):
    import code_health_build
    monkeypatch.setattr(code_health_build, "load_snapshot",
                        lambda: ("2020-01-01T00:00:00Z", [{"repo": "x", "test_count": 1}]))
    monkeypatch.setattr(home_build, "_ci_health",
                        lambda scope, whole_fleet: {"value": "97.8%", "delta": "avg CI pass rate"})
    # whole_fleet=True on the card: the card's own default already is, but
    # the page's `scope={"x"}` must actually cover repo "x" in the registry
    # for its own filter not to drop it — monkeypatched here (E19·4,
    # issue_01M35K5JQ7Z7MHYZGWRSB5F85M) rather than relying on this
    # machine's real ~/Convivy/agent-platform/config/projects.json, which
    # has no project named "x".
    monkeypatch.setattr(crew_registry, "repos_in_scope", lambda scope: frozenset({"x"}))
    card_html = overview_build._code_health_section()
    page_html = code_health_build.live_html(frozenset({"x"}))
    assert 'class="stalled-banner"' in card_html
    assert 'class="stalled-banner"' in page_html


def test_activity_card_and_page_agree_when_stalled(monkeypatch):
    import activity_build
    snap = {"generated_at": "2020-01-01T00:00:00Z", "session_count": 3, "sessions": []}
    monkeypatch.setattr(activity_build, "_load_snapshot", lambda: snap)
    card_html = overview_build._activity_section()
    page_html = activity_build.live_html()
    assert 'class="stalled-banner"' in card_html
    assert 'class="stalled-banner"' in page_html


def test_velocity_page_shows_stalled_banner_for_delivery_activity_when_rollup_is_old(monkeypatch):
    """/velocity's secondary "Delivery activity" panel still watches the
    local rollup's own freshness — unlike the waypoints headline above it,
    which is computed live from the Library on every render and carries no
    producer cadence to judge staleness against."""
    import velocity_build
    data = {"windows": {"7": {"total": {"prs_merged": 4}}}, "generated_at": "2020-01-01T00:00:00"}
    monkeypatch.setattr(velocity_build, "load", lambda: {**data, "history": []})
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [])
    page_html = velocity_build.live_html(frozenset(), True)
    assert 'class="stalled-banner"' in page_html


def test_velocity_card_carries_no_stalled_banner_since_it_is_computed_live(monkeypatch):
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [])
    card_html = overview_build._velocity_section()
    assert 'class="stalled-banner"' not in card_html


def test_warrant_card_and_page_agree_when_stalled(monkeypatch):
    import warrant_build
    data = {"generated_at": "2020-01-01T00:00:00Z", "window_days": 30,
            "overall": {"merged_prs": 10, "unwarranted_rate": 1.0}, "by_model": {}, "by_repo": {}}
    monkeypatch.setattr(warrant_build, "load", lambda: data)
    card_html = overview_build._warrant_section()
    page_html = warrant_build.live_html()
    assert 'class="stalled-banner"' in card_html
    assert 'class="stalled-banner"' in page_html


def test_security_card_and_page_agree_when_stalled(monkeypatch):
    import security_build
    data = {"generated_at": "2020-01-01T00:00:00Z", "window_days": 30,
            "overall": {"prs_reviewed": 10, "blocking_per_kloc": 0.1}, "by_model": {}, "by_repo": {}}
    monkeypatch.setattr(security_build, "load", lambda: data)
    card_html = overview_build._security_section()
    page_html = security_build.live_html()
    assert 'class="stalled-banner"' in card_html
    assert 'class="stalled-banner"' in page_html


def test_kb_card_and_page_agree_when_stalled(monkeypatch):
    import kb_build
    ev = {"generated_at": None, "generated": "2020-01-01T00:00:00Z", "overall": {}, "k": 10,
          "cases": 1, "by_style": {}, "by_kind": {}, "misses": []}
    monkeypatch.setattr(kb_build, "load_eval", lambda: ev)
    monkeypatch.setattr(kb_build, "load_query_stats", lambda: kb_build.query_stats([]))
    card_html = overview_build._kb_section()
    page_html = kb_build.live_html()
    assert 'class="stalled-banner"' in card_html
    assert 'class="stalled-banner"' in page_html


def test_e2e_card_and_page_agree_when_stalled(monkeypatch):
    import datetime as dt
    import library_client
    from models import utcnow

    stale = utcnow() - dt.timedelta(hours=200)

    class _Sig:
        product = "vivy"
        stream = "e2e-stage-smoke"
        kind = "test-result"
        meta = {"status": "pass"}
        body = ""
        produced_at = stale
        ingested_at = stale

    monkeypatch.setattr(library_client, "list_signals", lambda **k: [_Sig()])
    card_html = overview_build._e2e_section()
    import engineering_e2e_build
    page_html = engineering_e2e_build.live_html()
    assert 'class="stalled-banner"' in card_html
    assert 'class="stalled-banner"' in page_html


def test_e2e_card_and_page_agree_on_count_over_100_signals(monkeypatch):
    """The overview card used to scan cap=100 CI signals while the e2e page
    scanned engineering_e2e_build._SCAN_LIMIT (400) over the same feed, so the
    two could disagree on the passing count once there were more than 100
    distinct suites (issue_01M0Z8C4MC3X8KV12PS4Y84DVD). Plant 150 distinct
    (product, stream) suites, newest-first, with the 100 newest passing and
    the 50 oldest failing — a 100-row window sees "100/100 passing", a
    400-row window sees "100/150 passing". With fewer than 100 signals both
    windows would agree regardless of the cap, proving nothing."""
    import datetime as dt
    import library_client
    from models import utcnow

    now = utcnow()

    def _sig(i: int) -> library_client.SignalRecord:
        status = "pass" if i < 100 else "fail"
        return library_client.SignalRecord(
            id=f"sig{i}", source="ci", stream=f"suite-{i}", kind="test-result",
            actor="ci", ingested_by=None, product=f"product-{i}", body="",
            refs={}, meta={"status": status}, privacy_class="internal",
            produced_at=now - dt.timedelta(minutes=i), ingested_at=now)

    signals = [_sig(i) for i in range(150)]

    def _fake_list_signals(**kw):
        cap = kw.get("cap")
        return signals[:cap] if cap is not None else signals

    monkeypatch.setattr(library_client, "list_signals", _fake_list_signals)

    card_html = overview_build._e2e_section()
    import engineering_e2e_build
    page_html = engineering_e2e_build.live_html()

    page_total = page_html.count('<article class="tile ')
    page_passing = page_html.count('<article class="tile pass"')
    assert page_total == 150, f"expected the page to see all 150 suites, got {page_total}"
    assert page_passing == 100

    assert f"{page_passing}/{page_total} passing" in card_html, (
        "the overview card's scan cap has drifted from engineering_e2e_build's "
        f"again: card_html={card_html!r}")


# ---------------------------------------------------------------------------
# The /kb card carries the golden-set eval's own recall (the operator, 2026-09-05)
# ---------------------------------------------------------------------------


def test_kb_card_shows_the_golden_set_recall_not_a_freshness_timestamp(monkeypatch):
    """**Every other card on /overview names a measured quantity**, and this one
    named a staleness property until the KB Freshness stat was removed. It now
    shows what /kb exists to measure. The recall here grades `bad` under
    `kb_build.cls_recall`, which is the case worth rendering: a card showing a
    doc count said nothing about retrieval failing its own threshold."""
    import kb_build

    monkeypatch.setattr(kb_build, "load_eval", lambda: {
        "generated": "2026-09-05T07:48:00",
        "overall": {"n": 44, "recall@1": 0.364, "recall@5": 0.5, "mrr": 0.429},
    })
    html = overview_build._kb_section()
    assert "50%" in html
    assert "recall@5 · 44 cases" in html


def test_kb_card_only_empty_states_when_no_eval_has_ever_run(monkeypatch):
    """**A degraded eval must not render as an absent one.** `freshness`
    separates them — `stalled=None` when nothing was ever written, and a real
    verdict once a run has landed — and only the first reaches the empty
    state. An eval that ran but carries no `recall@5` keeps its freshness
    line and shows an em dash, because empty-stating it would drop the one
    signal saying the feed is degraded. That collapse is this page's own
    recurring defect."""
    import kb_build

    # Patch the PATH, not `load_eval` — `load_eval` returns None rather than
    # {} for an absent file, and a stub returning {} never exercises that.
    # An earlier version of this test stubbed it to `dict` and passed against
    # code that crashed on the real path.
    monkeypatch.setattr(kb_build, "KB_EVAL_JSON",
                        pathlib.Path("/nonexistent/kb-eval.json"))
    assert kb_build.load_eval() is None, "the stub must reproduce the real None"
    assert 'class="empty-state"' in overview_build._kb_section()

    monkeypatch.setattr(kb_build, "load_eval", lambda: {"overall": {"n": 44}})
    html = overview_build._kb_section()
    assert 'class="empty-state"' not in html
    assert "no recall in the latest eval" in html


# --- the spend figure names its day, and the spend pages reload (the operator, 2026-09-28)


def test_the_spend_figure_names_the_render_day_and_time(monkeypatch):
    """A bare "today" read the same on a tab left open since the night
    before, which showed Sunday's $750 beside Monday's $158 on another
    device. The figure is a live per-request read now (CQ·154,
    issue_01M4ZBBQK0KFH9YQ16MNK75HJT), so the day and clock named are the
    render's own rather than a snapshot's."""
    import orient_time
    fixed = dt.datetime(2026, 9, 28, 14, 35, 6, tzinfo=orient_time.MST)
    monkeypatch.setattr(orient_time, "now", lambda: fixed)
    monkeypatch.setattr(library_client, "list_finished_runs", lambda since, until=None: [])
    monkeypatch.setattr(library_client, "cost_rollup_by_project",
                        lambda runs: {"vivy": {"cost_usd": 158.71, "runs": 3}})
    assert home_build._today_spend() == {"value": "$158.71",
                                         "delta": "Mon Sep 28 · as of 2:35 PM"}


def test_the_spend_figure_sums_every_project_in_the_rollup(monkeypatch):
    monkeypatch.setattr(library_client, "list_finished_runs", lambda since, until=None: [])
    monkeypatch.setattr(library_client, "cost_rollup_by_project", lambda runs: {
        "vivy": {"cost_usd": 1.5, "runs": 1}, "constance": {"cost_usd": 2.5, "runs": 2}})
    assert home_build._today_spend()["value"] == "$4.00"


def test_the_spend_figure_degrades_to_unavailable_on_a_library_outage(monkeypatch):
    """Matches every other loader in this module (its own docstring): one
    broken source degrades, it never 500s the whole Home page."""
    def boom(since, until=None):
        raise ConnectionError("Library unreachable")

    monkeypatch.setattr(library_client, "list_finished_runs", boom)
    assert home_build._today_spend() == {"value": "—", "delta": "unavailable"}


def test_the_spend_figure_is_none_for_a_scoped_viewer(monkeypatch):
    """Rule 1 (E19·3, issue_01M35K5EAZD85FBYXEBQVRKVZV): no fleet total to a
    scoped viewer. Asserted with the Library call itself unmocked, so a
    regression that moved the gate past the read would be a hard failure
    here, not a quietly-passing stub."""
    assert home_build._today_spend(whole_fleet=False) is None


def test_the_pages_showing_todays_spend_reload_every_fifteen_minutes(monkeypatch):
    import dashboard_build
    monkeypatch.setattr(home_build, "_today_spend",
                        lambda whole_fleet: {"value": "$1.00", "delta": "today"})
    monkeypatch.setattr(dashboard_build, "load_latest_snapshot", lambda: {})
    marker = f'data-seconds="{15 * 60}"'
    assert marker in dashboard_build.spending_render()
    assert marker in overview_build.live_html()
    assert marker in home_build.live_html("full", "")


# --- Ship mark in the hero (the operator 2026-10-09) ---------------------------


def _hero(html: str) -> str:
    start = html.index('<header class="hero">')
    return html[start:html.index("</header>", start)]


def test_both_home_heroes_carry_the_same_aria_hidden_ship_mark(monkeypatch):
    """The Convivy Builder ship sits in each home hero, hidden from assistive
    tech because it is decorative. Both tiers get the same markup, so the mark
    adds nothing tier-specific to either payload."""
    library_fake.patch(monkeypatch)
    marks = []
    for tier in ("full", "contractor"):
        hero = _hero(home_build.live_html(tier, "t"))
        assert hero.count('class="hero-mark"') == 1, tier
        svg = hero[hero.index("<svg"):hero.index("</svg>") + len("</svg>")]
        assert 'class="hero-mark"' in svg and 'aria-hidden="true"' in svg, tier
        marks.append(svg)
    assert marks[0] == marks[1] == ds.ship_mark()


def test_full_home_alerts_pill_counts_the_viewers_alerts(monkeypatch):
    """Home's Alerts pill counts what `/alerts` shows this viewer (E19·3,
    issue_01M35K5EAZD85FBYXEBQVRKVZV): the scope and whole-fleet flag reach
    `alerts_build.gather`, and its result sets the pill. With no scope, the
    pill counts nothing."""
    library_fake.patch(monkeypatch)
    seen = []

    def gather(scope, whole_fleet):
        seen.append((scope, whole_fleet))
        if not scope:
            return []
        return [{"severity": "warn", "category": "Code Health", "subject": "alpha-repo"},
                {"severity": "bad", "category": "Code Health", "subject": "alpha-repo"}]

    monkeypatch.setattr(home_build.alerts_build, "gather", gather)

    html = home_build._full_body(None, frozenset({"alpha"}), True)
    assert "2 active" in html
    assert (frozenset({"alpha"}), True) in seen

    seen.clear()
    html = home_build._full_body()
    assert "2 active" not in html and "All clear" in html
    assert seen and all(call == (frozenset(), False) for call in seen)


# --- build_static: the static fallback must never carry real Home data -----


def test_build_static_seeds_an_empty_fallback_not_real_data(tmp_path, monkeypatch):
    """home.full.html/home.contractor.html are served to ANY viewer's failed
    render, scoped or not (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): a
    snapshot naming a real doc or a real spend figure would leak to a
    scoped viewer. `live_html`'s own defaults (empty scope, not
    whole-fleet) already make this empty by construction — pinned here so
    a future default change is caught."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="realproj/reference/real-doc", title="REAL-DOC-TITLE",
                                    project="realproj", last_reviewed="2026-01-01")
    monkeypatch.setattr(home_build, "OUT", tmp_path)
    home_build.build_static()
    for name in ("home.full.html", "home.contractor.html"):
        html = (tmp_path / name).read_text(encoding="utf-8")
        assert "REAL-DOC-TITLE" not in html
        assert "realproj" not in html


# --- E19·4: Home's CI rate and /overview's e2e card count only the viewer's
# own projects, checked against a second, out-of-scope project ---------------


def test_ci_health_averages_only_in_scope_repos(tmp_path, monkeypatch):
    """`agent-platform` is `bosun`'s repo and `deck-repo` is `convivy-deck`'s.
    An unfiltered average reads 50.0%; the bosun viewer's reads 100.0%."""
    import code_health_build
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"bosun": {"crew": "bosun", "repos": ["agent-platform"]},
                               "convivy-deck": {"crew": "deck", "repos": ["deck-repo"]}}))
    monkeypatch.setattr(crew_registry, "REGISTRY", reg)
    monkeypatch.setattr(code_health_build, "load_snapshot", lambda: (
        "2026-10-13T00:00:00Z",
        [{"repo": "agent-platform", "ci_pass_rate": 1.0},
         {"repo": "deck-repo", "ci_pass_rate": 0.0}]))

    assert home_build._ci_health(frozenset({"bosun"}), False)["value"] == "100.0%"
    assert home_build._ci_health(frozenset(), True)["value"] == "50.0%"


def test_e2e_section_counts_only_in_scope_suites(monkeypatch):
    """A failing `constance` suite must neither appear in the scoped viewer's
    count nor drag their "passing" figure: unfiltered, the card reads 1/2."""
    import datetime as dt

    fresh = utcnow() - dt.timedelta(hours=1)

    def sig(product, status):
        class _Sig:
            stream = "e2e-stage-smoke"
            kind = "test-result"
            meta = {"status": status}
            produced_at = fresh
            ingested_at = fresh
        _Sig.product = product
        return _Sig()

    monkeypatch.setattr(library_client, "list_signals",
                        lambda **k: [sig("vivy", "pass"), sig("constance", "fail")])
    scoped = overview_build._e2e_section(frozenset({"vivy"}), False)
    assert "1/1 passing" in scoped
    assert "1 suites" in scoped
    assert "1/2 passing" in overview_build._e2e_section(frozenset(), True)


def test_kb_doc_count_counts_only_in_scope_documents(monkeypatch):
    """Home's knowledge-base pill: unfiltered it reads 3 docs; the vivy
    viewer's reads 1."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="vivy/a", title="A", project="vivy")
    library_fake.seed_reference_doc(id="deck/b", title="B", project="convivy-deck")
    library_fake.seed_reference_doc(id="deck/c", title="C", project="convivy-deck")

    assert home_build._kb_doc_count(frozenset({"vivy"}), False) == "1 docs indexed"
    assert home_build._kb_doc_count(frozenset(), True) == "3 docs indexed"


def _two_repo_registry(tmp_path, monkeypatch):
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"bosun": {"crew": "bosun", "repos": ["agent-platform"]},
                               "convivy-deck": {"crew": "deck", "repos": ["deck-repo"]}}))
    monkeypatch.setattr(crew_registry, "REGISTRY", reg)
    return frozenset({"bosun"})


def test_cost_performance_section_counts_only_in_scope_repos(tmp_path, monkeypatch):
    """One merged PR per repo, $0.50 in scope and $9.50 out: unfiltered the
    card reads $5.00 over 2 PRs, the bosun viewer's $0.50 over 1."""
    import dashboard_build as db
    scope = _two_repo_registry(tmp_path, monkeypatch)
    sc = tmp_path / "scorecard.jsonl"
    rows = [dict(run_id=f"r{i}", date="2026-10-13", agent="coder", model="claude-sonnet-5",
                 billing_source="subscription", repo=repo, cost_usd=cost, pr_state="MERGED",
                 merged=True, survived=True, ci_first_pass=True, pr_number=i)
            for i, (repo, cost) in enumerate([("agent-platform", 0.50), ("deck-repo", 9.50)])]
    sc.write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(db, "SCORECARD", sc)
    monkeypatch.setattr(db, "PA_LEDGER", tmp_path / "absent.jsonl")
    monkeypatch.setattr(db, "JUDGE", tmp_path / "absent2.jsonl")

    scoped = overview_build._cost_performance_section(scope, False)
    assert "$0.50" in scoped
    assert "1 total" in scoped
    assert "$5.00" in overview_build._cost_performance_section(frozenset(), True)


def test_activity_section_counts_only_in_scope_sessions(tmp_path, monkeypatch):
    """Sessions carry a repo as `product`. Unfiltered: 3 sessions; the bosun
    viewer's: 1, with the product-less session kept only for the whole fleet."""
    import activity_build
    scope = _two_repo_registry(tmp_path, monkeypatch)
    snap = {"generated_at": utcnow().isoformat() + "+00:00", "session_count": 3,
            "sessions": [{"product": "agent-platform"}, {"product": "deck-repo"},
                         {"product": None}]}
    monkeypatch.setattr(activity_build, "_load_snapshot", lambda: snap)

    assert "1 sessions" in overview_build._activity_section(scope, False)
    assert "3 sessions" in overview_build._activity_section(frozenset(), True)
