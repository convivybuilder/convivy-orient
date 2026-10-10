"""Render tests for ops_reports_build.py (Slack-retirement Phase B; the
Reports/Notes split is ARCH-362 R1+R4). Both /engineering/reports and
/engineering/notes are retired now (Notes on 2026-08-26, the operator); /reference/
briefings, tested below in test_nav_places_briefings_in_archives and the
KB-reading tests further down, moved off this module's signal-reading
machinery the same day.

The stream→category routing and Slack-body escaping (`_category`, `_render`,
`_slack_mrkdwn_to_html` and friends) had no live caller as of 2026-08-26 — see
ops_reports_build's own module docstring — and were deleted along with their
unit tests the same day. Alerts moved to /alerts (alerts_build.py, epic
issue_01M0EHRNW5HVJCAXZHYQCBZ3YW) and is tested there, not here.
"""
from __future__ import annotations

import json

import pytest

import control_db
import library_client
import library_fake
import ops_reports_build as o


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    # ops_reports_build now reads through library_client rather than control_db
    # directly; this fake serves the Library's exact response shape from the
    # same temp DB every test already seeds (library_fake's docstring).
    library_fake.patch(monkeypatch)
    yield


def _mk(**kw):
    base = dict(id=control_db.new_signal_id(), source="slack", kind="notification",
                actor="platform", ingested_by="agent-platform", product="platform",
                body="body", privacy_class="internal", stream=None, refs={}, meta={},
                produced_at=None, ingested_at=control_db.utcnow())
    base.update(kw)
    return library_client.SignalRecord(**base)


def _seed(*sigs):
    for sig in sigs:
        library_fake.seed_signal(
            id=sig.id, source=sig.source, kind=sig.kind, actor=sig.actor,
            ingested_by=sig.ingested_by, product=sig.product, stream=sig.stream,
            refs=sig.refs, body=sig.body, meta=sig.meta,
            privacy_class=sig.privacy_class, produced_at=sig.produced_at,
            ingested_at=sig.ingested_at)


def test_routes_registered_full_tier():
    # /engineering/notes is retired (the operator, 2026-08-26): no route, no redirect,
    # a plain 404 like /engineering/reports and /memory/reports before it.
    import server
    routes = server._full_only_routes()
    assert "/engineering/notes" not in routes
    assert "/engineering/reports" not in routes
    assert "/memory/reports" not in routes
    # Alerts moved to /alerts under Dashboards (alerts_build.py) — no
    # /engineering/alerts route any more, served by redirect (test_server.py).
    assert "/engineering/alerts" not in routes


def test_nav_places_briefings_in_archives():
    # Briefings moved into Archives (nav-reshape epic
    # issue_01M0TKX6DW2E1TXGN9QT6K61J5 step 2, correcting the 2026-08-19
    # placement in the Reading Room). It rendered signal kind=briefing through
    # the same `ops_reports_build._render` helper Notes used at the time;
    # since 2026-08-26 it reads the KB index instead (briefings_live_html's
    # own docstring) and stays in Archives under the operator's memory-vs-canonical
    # rule. Notes, the ARCH log and Market were trimmed from this strip the
    # same day (the operator, 2026-08-26), so the strip is now Signals + Briefings only.
    import design_system as ds
    html = ds.nav_component("/reference/briefings", "full")
    assert 'href="/reference/briefings" aria-current="page"' in html
    assert 'aria-label="Archives navigation"' in html
    assert 'href="/archives"' in html
    assert 'href="/market"' not in html
    assert 'href="/engineering/notes"' not in html
    assert 'href="/reference/arch-log"' not in html
    assert 'href="/room"' not in html
    assert 'href="/engineering/roster"' not in html


def test_no_reports_or_notes_builder_survives(db):
    """reports_live_html and notes_live_html are GONE. The reports precedent is
    worth recording: the first draft claimed convivy-lab's project_mirror
    imported it, the way it genuinely did import briefs_build (ARCH-372). It
    did not — project_mirror imported ~12 builders and ops_reports_build was
    not among them. That claim was generalised from the briefs case without
    being checked, which is the ARCH-370 shape: an unverified premise carried
    forward because it resembled one already established. notes_live_html
    followed the same precedent when its page retired (the operator, 2026-08-26) —
    checked again rather than assumed. (convivy-lab was archived 2026-09-11;
    project_mirror.py no longer runs.)"""
    assert not hasattr(o, "reports_live_html")
    assert not hasattr(o, "notes_live_html")


# --- /reference/briefings reads the KB index, not the signal store (2026-08-26) --
#
# The producer (ai-briefing, launchd 03:00/04:00) moved from posting signals to
# publishing KB documents via the write cage; the page had kept reading the old
# store, so its newest item sat 39 days behind the KB. These pin the SOURCE —
# a page that merely renders "something" would pass against the dead signal
# path too (a previous version of this suite did exactly that; there is no
# surviving test asserting briefings reads signals, because that assertion
# would have encoded the bug).


def _seed_kb(monkeypatch, rows):
    """Put `rows` behind the Library's reference readers.

    Replaces the `index.db` this suite used to build in `tmp_path` and
    monkeypatch onto `ops_reports_build.DB` — that seam went away with the
    module's direct sqlite read (issue_01M1MWRXAZ5NRH10NP6Z9MBH0C). The rows
    are unchanged in shape apart from `audience`, which is a real list here
    rather than the JSON string the index column held."""
    library_fake.patch(monkeypatch)
    for row in rows:
        library_fake.seed_reference_doc(**row)


def _kb_row(**kw):
    d = {"id": "platform/briefs/x-2026-01-01", "project": "platform", "kind": "brief",
         "title": "T", "status": "current", "last_reviewed": "2026-01-01", "body": "Body.\n",
         "audience": ["team"]}
    d.update(kw)
    return d


def _payload(html: str) -> dict:
    start = html.index('<script id="data"')
    start = html.index(">", start) + 1
    end = html.index("</script>", start)
    return json.loads(html[start:end])


def test_briefings_ignores_the_signal_store(tmp_path, monkeypatch, db):
    """A 39-day-old signal in the old `ai-engineering-briefing` stream must not
    appear, and the render must not even touch library_client — the producer
    stopped posting there, it moved, and a page that fell back to a stale
    signal on an empty KB read would still fail a reader silently."""
    _seed_kb(monkeypatch, [])

    def _boom(*a, **kw):
        raise AssertionError("briefings_live_html must not call list_signals")
    monkeypatch.setattr(library_client, "list_signals", _boom)

    _seed(_mk(stream="ai-engineering-briefing", body="STALE-SIGNAL-BRIEFING"))
    html = o.briefings_live_html()
    assert "STALE-SIGNAL-BRIEFING" not in html


def test_briefings_shows_todays_kb_brief_not_a_stale_one(tmp_path, monkeypatch, db):
    _seed_kb(monkeypatch, [
        _kb_row(id="platform/briefs/old-2026-07-18", title="OLD-BRIEF-JUL-18",
                last_reviewed="2026-07-18"),
        _kb_row(id="platform/briefs/new-2026-08-26", title="FRESH-BRIEF-AUG-26",
                last_reviewed="2026-08-26"),
    ])
    html = o.briefings_live_html()
    payload = _payload(html)
    assert [b["title"] for b in payload["briefs"]] == ["FRESH-BRIEF-AUG-26", "OLD-BRIEF-JUL-18"]


def test_other_memory_kinds_surface_with_kind_label(tmp_path, monkeypatch, db):
    """The four kinds engineering_build.VISIBLE_KINDS drops as canonical-browse
    noise are memories under the operator's model and must be reachable here."""
    _seed_kb(monkeypatch, [
        _kb_row(id="vivy/meeting-notes/a", kind="meeting-notes", project="vivy",
                title="MEETING-NOTE-TITLE"),
        _kb_row(id="sous-chef/proposals/b", kind="proposal", project="sous-chef",
                title="PROPOSAL-TITLE", status="archived"),
        _kb_row(id="sous-chef/observations/c", kind="observation", project="sous-chef",
                title="OBSERVATION-TITLE"),
        _kb_row(id="constance/feature-requests/d", kind="feature-request",
                project="constance", title="FEATURE-REQUEST-TITLE", status="draft"),
    ])
    html = o.briefings_live_html()
    payload = _payload(html)
    titles = {e["title"]: e["kindLabel"] for e in payload["other"]}
    assert titles == {
        "MEETING-NOTE-TITLE": "Meeting notes",
        "PROPOSAL-TITLE": "Proposal",
        "OBSERVATION-TITLE": "Observation",
        "FEATURE-REQUEST-TITLE": "Feature request",
    }
    # archived/draft still show — a memory is what happened, not a current claim
    assert {e["title"] for e in payload["other"]} == set(titles)


@pytest.mark.parametrize("status", ["archived", "deprecated", "draft", "current"])
def test_a_memory_of_every_status_renders(tmp_path, monkeypatch, db, status):
    """**This is the guard on `_load_kb_docs`'s `status="all"`, and it is the one
    assertion that cannot be inferred from the live corpus.**

    The Library's listing default is current+draft, so a port that dropped the
    argument would still render correctly against production today — there are
    no non-current team-audience documents among this page's four kinds right
    now (measured 2026-09-10: 6 documents, all current). It would break the
    first time someone archived a proposal, which is precisely what this page is
    for under the operator's 2026-08-26 ruling that a memory is a record of what
    happened, not a claim someone still maintains.

    Parametrised per status rather than seeding all four in one render, so a
    failure names the status that vanished instead of reporting a set that does
    not match."""
    _seed_kb(monkeypatch, [
        _kb_row(id="sous-chef/proposals/x", kind="proposal", project="sous-chef",
                title="MEMORY-TITLE", status=status),
    ])
    payload = _payload(o.briefings_live_html())
    assert [e["title"] for e in payload["other"]] == ["MEMORY-TITLE"]


def test_a_render_lists_the_corpus_once_not_once_per_group(tmp_path, monkeypatch, db):
    """This page is served live per request, so the two memory groups share one
    listing pass. The first version of the port gave each group its own, which
    doubled the round trips on every render — the distinction the epic behind
    this migration sequenced its children around (a batch build absorbs round
    trips a request path cannot)."""
    _seed_kb(monkeypatch, [
        _kb_row(id="platform/briefs/a", kind="brief", title="BRIEF"),
        _kb_row(id="sous-chef/proposals/b", kind="proposal", title="PROPOSAL"),
    ])
    calls = []
    real = library_client.list_reference_documents
    monkeypatch.setattr(library_client, "list_reference_documents",
                        lambda **kw: (calls.append(kw), real(**kw))[1])
    payload = _payload(o.briefings_live_html())
    assert len(calls) == 1, f"corpus listed {len(calls)} times in one render"
    assert calls[0]["status"] == "all"
    # Both groups still populated from that single pass.
    assert [e["title"] for e in payload["briefs"]] == ["BRIEF"]
    assert [e["title"] for e in payload["other"]] == ["PROPOSAL"]


def test_a_doc_that_vanishes_between_the_listing_and_the_fetch_is_dropped(
        tmp_path, monkeypatch, db):
    """Entitlement or existence can change mid-render. Drop that row rather than
    raise, matching build.load_docs — one stale reference must not take out the
    whole page."""
    _seed_kb(monkeypatch, [
        _kb_row(id="platform/briefs/gone", kind="brief", title="VANISHED"),
        _kb_row(id="platform/briefs/ok", kind="brief", title="SURVIVES"),
    ])
    real = library_client.get_reference_document
    monkeypatch.setattr(
        library_client, "get_reference_document",
        lambda doc_id, **kw: None if doc_id.endswith("gone") else real(doc_id, **kw))
    payload = _payload(o.briefings_live_html())
    assert [e["title"] for e in payload["briefs"]] == ["SURVIVES"]


def test_a_doc_whose_body_is_none_is_dropped(tmp_path, monkeypatch, db):
    _seed_kb(monkeypatch, [
        _kb_row(id="platform/briefs/nobody", kind="brief", title="NO-BODY", body=None),
        _kb_row(id="platform/briefs/ok", kind="brief", title="HAS-BODY"),
    ])
    payload = _payload(o.briefings_live_html())
    assert [e["title"] for e in payload["briefs"]] == ["HAS-BODY"]


def test_the_memories_reader_opens_no_database(tmp_path, monkeypatch, db):
    """The point of the move (issue_01M1MWRXAZ5NRH10NP6Z9MBH0C): this module
    holds no handle on the knowledge index. Asserted structurally rather than by
    rendering, because a render proves only that THIS path did not open one."""
    import sqlite3 as _sqlite3
    monkeypatch.setattr(_sqlite3, "connect", lambda *a, **kw: pytest.fail(
        "ops_reports_build opened a database directly"))
    assert not hasattr(o, "DB")
    _seed_kb(monkeypatch, [_kb_row(id="platform/briefs/a", title="BRIEF")])
    assert "BRIEF" in o.briefings_live_html()


def test_legal_and_marketing_stay_excluded(tmp_path, monkeypatch, db):
    """The operator: legal and marketing read as canonical, not memory — considered for
    this page and deliberately left off, unlike the other four VISIBLE_KINDS
    exclusions."""
    _seed_kb(monkeypatch, [
        _kb_row(id="company/legal/a", kind="legal", title="LEGAL-DOC-TITLE"),
        _kb_row(id="company/marketing/b", kind="marketing", title="MARKETING-DOC-TITLE"),
    ])
    html = o.briefings_live_html()
    assert "LEGAL-DOC-TITLE" not in html
    assert "MARKETING-DOC-TITLE" not in html


def test_briefings_and_other_memories_are_product_filterable(tmp_path, monkeypatch, db):
    """Every Archives category must be filterable by product (spec)."""
    _seed_kb(monkeypatch, [
        _kb_row(id="platform/briefs/a-2026-08-01", project="platform", title="A"),
        _kb_row(id="company/briefs/b-2026-08-02", project="company", title="B"),
        _kb_row(id="vivy/meeting-notes/c", kind="meeting-notes", project="vivy", title="C"),
    ])
    html = o.briefings_live_html()
    payload = _payload(html)
    assert payload["projects"] == ["company", "platform", "vivy"]
    assert {b["project"] for b in payload["briefs"]} == {"platform", "company"}
    assert {e["project"] for e in payload["other"]} == {"vivy"}


def test_empty_states_use_the_shared_component(tmp_path, monkeypatch, db):
    """The per-list empty state is now rendered server-side through
    ds.empty_state() and shipped in the payload, not hand-rolled in
    _MEMORY_JS's renderList — pin the shared component's own markup shape
    rather than the page's prior bespoke `.empty` div."""
    import design_system as ds
    _seed_kb(monkeypatch, [])
    html = o.briefings_live_html()
    payload = _payload(html)
    assert payload["briefsEmptyHtml"] == ds.empty_state("No briefings for this project.")
    assert payload["otherEmptyHtml"] == ds.empty_state("Nothing captured for this project.")
    assert 'class="empty-state"' in payload["briefsEmptyHtml"]


# --- audience gate ------------------------------------------------------------
# _load_kb_docs() rendered every doc of these kinds with no audience filter at
# all, unlike build.load_docs()'s canonical browse — a role- or person-audienced
# doc (proposal, meeting-notes, observation, feature-request) rode along to
# every full-tier viewer. These pin the gate the way test_kb_write_api.py pins
# the write-side one: a stub or a "something rendered" assertion can't catch it,
# only an explicit non-team row that must NOT appear.

def test_team_audience_doc_of_each_kind_renders(tmp_path, monkeypatch, db):
    _seed_kb(monkeypatch, [
        _kb_row(id="platform/briefs/a", kind="brief", title="BRIEF-TEAM",
                audience=["team"]),
        _kb_row(id="vivy/meeting-notes/b", kind="meeting-notes", title="NOTE-TEAM",
                audience=["team"]),
        _kb_row(id="sous-chef/proposals/c", kind="proposal", title="PROPOSAL-TEAM",
                audience=["team", "architect"]),
        _kb_row(id="sous-chef/observations/d", kind="observation", title="OBS-TEAM",
                audience=["team"]),
        _kb_row(id="constance/feature-requests/e", kind="feature-request",
                title="FEATURE-TEAM", audience=["team"]),
    ])
    html = o.briefings_live_html()
    payload = _payload(html)
    titles = {e["title"] for e in (*payload["briefs"], *payload["other"])}
    assert titles == {"BRIEF-TEAM", "NOTE-TEAM", "PROPOSAL-TEAM", "OBS-TEAM", "FEATURE-TEAM"}


def test_non_team_audience_doc_is_excluded(tmp_path, monkeypatch, db):
    """A doc audienced at a role or a person, not the whole company, must not
    reach this full-tier page — same gate as build.load_docs()."""
    _seed_kb(monkeypatch, [
        _kb_row(id="platform/briefs/a", kind="brief", title="BRIEF-OPERATOR-ONLY",
                audience=["operator"]),
        _kb_row(id="vivy/meeting-notes/b", kind="meeting-notes", title="NOTE-ARCHITECT-ONLY",
                audience=["architect"]),
        _kb_row(id="sous-chef/proposals/c", kind="proposal", title="PROPOSAL-EMPTY-AUD",
                audience=[]),
    ])
    html = o.briefings_live_html()
    assert "BRIEF-OPERATOR-ONLY" not in html
    assert "NOTE-ARCHITECT-ONLY" not in html
    assert "PROPOSAL-EMPTY-AUD" not in html
    payload = _payload(html)
    assert payload["briefs"] == []
    assert payload["other"] == []


def test_null_and_malformed_audience_are_excluded_not_raised(tmp_path, monkeypatch, db):
    """A missing or malformed `audience` value must fail closed, not throw —
    this is a gate, so an unreadable value is not-team, not a crash."""
    _seed_kb(monkeypatch, [
        _kb_row(id="platform/briefs/a", kind="brief", title="BRIEF-NULL-AUD",
                audience=None),
        _kb_row(id="platform/briefs/b", kind="brief", title="BRIEF-MALFORMED-AUD",
                audience="not valid json"),
    ])
    html = o.briefings_live_html()  # must not raise
    assert "BRIEF-NULL-AUD" not in html
    assert "BRIEF-MALFORMED-AUD" not in html
