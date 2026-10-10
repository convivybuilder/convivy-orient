"""Tests for the Chart House base page (/chart-house), redesigned per
issue_01M15C4G6YJHH73KJCHGK7B0J9's approved mockup: one row per product's
live Course, drawn at true scale.

Both data sources are monkeypatched: cost reads local files via
`dashboard_build.normalize()`, and Courses/bead ids read `library_client`,
which every test here fakes rather than calling the network.
"""
from __future__ import annotations

import datetime as dt

import pytest

import chart_house_build as ch
import library_client


def _course(course_id, product, title, *, version=1, entries):
    return library_client.CourseRecord(
        id=course_id, product=product, title=title, status="live", version=version,
        entries=entries, created_by="ops-mcp", created_at=None,
        updated_by="ops-mcp", updated_at=None)


def _entry(issue_id, state, maturity=None, title=None, specification=None):
    return library_client.CourseEntry(issue_id=issue_id, state=state, maturity=maturity,
                                      title=title or issue_id, specification=specification)


def _bead_doc(project, doc_id=None):
    return library_client.ReferenceDocument(
        id=doc_id or f"{project}/bead/{project}", source_ref=f"projects/{project}/bead/{project}.md",
        kind="bead", project=project, title=f"{project} bead", status="current",
        topics=[], audience=["team"], last_reviewed=None, updated_at=None)


@pytest.fixture(autouse=True)
def _no_beads(monkeypatch):
    """Default to "no bead docs" so a test that doesn't care about linking
    need not know the bead lookup exists."""
    monkeypatch.setattr(library_client, "list_reference_documents", lambda **k: [])


@pytest.fixture(autouse=True)
def _no_proposed_epics(monkeypatch):
    """Default to "nothing awaiting approval" so a test that doesn't care about
    the Approve control need not know the item read exists
    (issue_01M32CB6A9T04NVYPARGDT2GWQ).

    Defaulted rather than left live for the reason every stub in this file is:
    `_proposed_epics` is UNCAUGHT on a Library failure by design, so without a
    default every Chart House test would depend on a reachable Library."""
    monkeypatch.setattr(library_client, "list_issue_items", lambda: [])


@pytest.fixture(autouse=True)
def _no_prs_or_merges(monkeypatch):
    """Default the PR ribbon's two sources to "feed cold, no merges" — the
    real default a never-visited merge-queue cache actually has — so a test
    that doesn't care about the ribbon need not know these calls exist."""
    monkeypatch.setattr(library_client, "list_signals", lambda **k: [])
    monkeypatch.setattr(ch.merge_queue_build, "cached_open_prs", lambda: None)


def _pr(repo, number, group, ci, *, conflicting=False):
    return {"repo": repo, "number": number, "group": group, "ci": ci,
            "conflicting": conflicting, "draft": group == "draft",
            "cleared": group == "cleared"}


def _fact(product, cost_usd, date):
    return {"product": product, "cost_usd": cost_usd, "date": date}


# --- _cost_rollup / _cost_for (local file reads via dashboard_build) ------


def test_cost_rollup_sums_per_product_within_the_window(monkeypatch):
    today = dt.datetime.now(dt.timezone.utc).date()
    in_window = today.isoformat()
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [
        _fact("vivy", 1.5, in_window),
        _fact("vivy", 2.5, in_window),
        _fact("constance", 3.0, in_window),
    ])
    rollup = ch._cost_rollup()
    assert rollup["vivy"] == {"cost_usd": 4.0, "runs": 2}
    assert rollup["constance"] == {"cost_usd": 3.0, "runs": 1}


def test_cost_rollup_excludes_rows_outside_the_window(monkeypatch):
    today = dt.datetime.now(dt.timezone.utc).date()
    old = (today - dt.timedelta(days=ch.WINDOW_DAYS + 5)).isoformat()
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [_fact("vivy", 9.0, old)])
    assert ch._cost_rollup() == {}


def test_cost_for_matches_the_hollis_canonicalization():
    rollup = {"Hollis": {"cost_usd": 5.0, "runs": 1}}
    assert ch._cost_for("hollis", rollup) == {"cost_usd": 5.0, "runs": 1}


def test_cost_for_sums_selector_and_selector_backend():
    rollup = {"selector": {"cost_usd": 2.0, "runs": 1},
              "selector-backend": {"cost_usd": 18.03, "runs": 20}}
    assert ch._cost_for("selector", rollup) == {"cost_usd": 20.03, "runs": 21}


def test_cost_for_is_none_not_zero_when_none_of_the_aliases_appear():
    # The property that matters: "no data this window" must stay distinguishable
    # from "$0.00" so the page never claims a false zero.
    assert ch._cost_for("selector", {}) is None
    assert ch._cost_for("no-such-project", {}) is None


# --- _step_class (the track cell classification) ---------------------------


def test_step_class_done_beats_maturity():
    # A terminal issue is done regardless of what its maturity field says —
    # maturity is set at proposal/approval time and never cleared on resolution.
    assert ch._step_class(_entry("i1", "fixed", maturity="proposed")) == "d"
    assert ch._step_class(_entry("i1", "wontfix")) == "d"
    assert ch._step_class(_entry("i1", "duplicate")) == "d"


def test_step_class_approved():
    assert ch._step_class(_entry("i1", "open", maturity="approved")) == "a"


def test_step_class_proposed():
    assert ch._step_class(_entry("i1", "open", maturity="proposed")) == "p"


def test_step_class_unspecified_when_maturity_is_unset_or_unresolved():
    assert ch._step_class(_entry("i1", "open", maturity=None)) == "u"
    assert ch._step_class(_entry("i1", None, maturity=None)) == "u"  # get_course raced away


# --- _awaits_signoff / _proposed_without_spec (issue_01M1FN5GDQGHR1PXY4AG9QTBWX) --


def test_awaits_signoff_requires_both_proposed_maturity_and_a_specification():
    assert ch._awaits_signoff(
        _entry("i1", "open", maturity="proposed", specification="written")) is True


def test_awaits_signoff_is_false_when_proposed_with_no_specification():
    assert ch._awaits_signoff(_entry("i1", "open", maturity="proposed")) is False


def test_awaits_signoff_is_false_when_specification_is_whitespace_only():
    assert ch._awaits_signoff(
        _entry("i1", "open", maturity="proposed", specification="   \n\t  ")) is False


def test_awaits_signoff_is_false_when_a_specification_exists_but_maturity_is_unset():
    # A spec with no visible maturity cannot be told from an unrelated field
    # being filled in early — issue_build.py draws the same distinction for
    # rendering an epic's own page.
    assert ch._awaits_signoff(
        _entry("i1", "open", maturity=None, specification="written")) is False


def test_proposed_without_spec_is_the_complement_within_proposed_entries():
    assert ch._proposed_without_spec(_entry("i1", "open", maturity="proposed")) is True
    assert ch._proposed_without_spec(
        _entry("i1", "open", maturity="proposed", specification="written")) is False
    assert ch._proposed_without_spec(_entry("i1", "open", maturity="approved")) is False


# --- _course_rows: one row per live Course, steps classified into 4 states -


def test_course_rows_classifies_every_step_and_counts_each_class(monkeypatch):
    # Plants a course whose steps span all four maturity states, mirroring
    # the live bosun Course the approved mockup renders: 1 done, 1 approved,
    # 1 proposed, 1 unspecified.
    course = _course("course_1", "bosun", "Make dispatch accountable", version=9, entries=[
        _entry("i1", "fixed"),
        _entry("i2", "open", maturity="approved"),
        _entry("i3", "open", maturity="proposed"),
        _entry("i4", "open", maturity=None),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    rows = ch._course_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["steps"] == ["d", "a", "p", "u"]
    assert row["total"] == 4
    assert row["done"] == 1
    assert row["approved"] == 1
    assert row["proposed"] == 1
    assert row["unspecified"] == 1
    # i3 is proposed but carries no specification, so it counts as waiting on
    # the crew, not as one of the operator's sign-offs.
    assert row["awaiting_signoff"] == 0
    assert row["proposed_unspecified"] == 1


def test_course_rows_falls_back_to_the_list_row_if_get_course_races_it_away(monkeypatch):
    """A course deleted between `list_courses` and `get_course` answers None;
    the row still renders (steps read "u", conservatively) rather than
    dropping the product from the page."""
    course = _course("course_1", "vivy", "Ship it", entries=[_entry("issue_1", None)])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: None)
    rows = ch._course_rows()
    assert rows[0]["total"] == 1
    assert rows[0]["steps"] == ["u"]


def _boom(*a, **k):
    raise ConnectionError("Library unreachable")


def test_course_rows_raises_when_list_courses_fails(monkeypatch):
    """No independent section is left to spare — a Library outage on the
    page's only data source must propagate so server.py's stale-static
    fallback takes over, rather than a half-rendered page."""
    monkeypatch.setattr(library_client, "list_courses", _boom)
    with pytest.raises(ConnectionError):
        ch._course_rows()


def test_one_courses_detail_failing_degrades_that_row_and_not_the_others(monkeypatch, capsys):
    good = _course("course_1", "vivy", "Ship it",
                   entries=[_entry("issue_1", "fixed"), _entry("issue_2", "open")])
    # The list-route fallback entry carries no state/maturity, matching what
    # list_courses() actually serves — a real fallback race, not an entry
    # that happens to already carry the detail route's resolution.
    bad = _course("course_2", "selector", "Make playlists good",
                  entries=[_entry("issue_3", None)])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [good, bad])

    def flaky(cid):
        if cid == "course_2":
            raise ConnectionError("Library unreachable")
        return good

    monkeypatch.setattr(library_client, "get_course", flaky)
    rows = ch._course_rows()
    assert len(rows) == 2                         # neither row is dropped
    by_product = {r["product"]: r for r in rows}
    assert by_product["vivy"]["done"] == 1        # the healthy row is unaffected
    # the degraded row falls back to the list record (steps unresolved -> "u")
    assert by_product["selector"]["title"] == "Make playlists good"
    assert by_product["selector"]["steps"] == ["u"]
    assert "course_2" in capsys.readouterr().err   # and the swallowed failure is not silent


def test_a_swallowed_exception_cannot_forge_a_log_line(monkeypatch, capsys):
    course = _course("course_1", "vivy", "Ship it", entries=[_entry("issue_1", "open")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])

    def boom(cid):
        raise ConnectionError("down\nFORGED 200 GET /admin\tinjected")

    monkeypatch.setattr(library_client, "get_course", boom)
    ch._course_rows()
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "\t" not in err
    assert "FORGED 200 GET /admin injected" in err


def test_log_swallowed_calls_the_shared_sanitiser(monkeypatch, capsys):
    # Pinned so a future edit that re-inlines the old copy fails here instead
    # of silently drifting from server.py's copy of the same rule
    # (issue_01M0VBZRCA15QSVYFMEF8MZ8N0).
    monkeypatch.setattr(ch, "clean_log_field", lambda v: "REPLACED")
    ch._log_swallowed("thing", ValueError("boom"))
    assert capsys.readouterr().err.strip() == "REPLACED"


# --- _bead_href / _bead_ids (the project name link) -------------------------


def test_bead_href_resolves_a_direct_project_match():
    ids = {"convivy-orient": "convivy-orient/bead/convivy-orient"}
    assert ch._bead_href("convivy-orient", ids) == "/reference/doc/convivy-orient/bead/convivy-orient"


def test_bead_href_resolves_through_the_bosun_alias():
    ids = {"agent-platform": "agent-platform/bead/agent-platform"}
    assert ch._bead_href("bosun", ids) == "/reference/doc/agent-platform/bead/agent-platform"


def test_bead_href_resolves_through_the_convivy_deck_alias():
    ids = {"deck": "deck/bead/deck"}
    assert ch._bead_href("convivy-deck", ids) == "/reference/doc/deck/bead/deck"


def test_bead_href_resolves_shipwright_to_the_product_bead_it_shares():
    """shipwright keeps NO bead of its own, by the operator's decision of 2026-09-05
    recorded in §5 of `company/bead/convivy-builder`: a crew built to work an
    existing destination would otherwise describe the same endpoint twice and
    drift from it. So it works the product bead, which is filed under the
    `company` project. Before this alias the row rendered unlinked with "no
    bead on file ... yet", which states the opposite of a settled decision, and
    Deck resolved the same crew correctly while this page did not."""
    ids = {"company": "company/bead/convivy-builder"}
    assert ch._bead_href("shipwright", ids) == "/reference/doc/company/bead/convivy-builder"


def test_bead_href_is_none_when_the_project_has_no_bead():
    """A project absent from the lookup and from the alias table. This is the
    "nothing found" case, distinct from shipwright's "shares another bead"
    above — the caller renders this one unlinked."""
    assert ch._bead_href("some-crew-with-no-bead", {}) is None


def test_bead_href_url_encodes_a_doc_id_needing_it():
    ids = {"convivy-orient": "convivy-orient/bead/weird?id#1 name"}
    assert ch._bead_href("convivy-orient", ids) == \
        "/reference/doc/convivy-orient/bead/weird%3Fid%231%20name"


def test_bead_href_leaves_a_hierarchical_ids_slashes_unencoded():
    # A slash-encoding quote() (the default safe="") would break every real
    # bead link, since ids legitimately carry hierarchical slashes.
    ids = {"convivy-orient": "convivy-orient/bead/convivy-orient"}
    assert ch._bead_href("convivy-orient", ids) == \
        "/reference/doc/convivy-orient/bead/convivy-orient"


def test_bead_ids_reads_only_kind_bead_keyed_by_project(monkeypatch):
    docs = [
        _bead_doc("convivy-orient"),
        library_client.ReferenceDocument(
            id="convivy-orient/decision/x", source_ref="x", kind="decision",
            project="convivy-orient", title="not a bead", status="current",
            topics=[], audience=["team"], last_reviewed=None, updated_at=None),
    ]
    monkeypatch.setattr(library_client, "list_reference_documents", lambda **k: docs)
    ids = ch._bead_ids()
    assert ids == {"convivy-orient": "convivy-orient/bead/convivy-orient"}


# --- _state_chip -------------------------------------------------------------


def test_state_chip_awaiting_signoff_outranks_everything():
    assert ch._state_chip(done=5, approved=5, awaiting_signoff=1, proposed_unspecified=0,
                          cost={"cost_usd": 1, "runs": 1}) == ("chip-warn", "awaits your sign-off")


def test_state_chip_underway_when_something_is_done():
    assert ch._state_chip(done=1, approved=0, awaiting_signoff=0, proposed_unspecified=0,
                          cost=None) == ("chip-ok", "underway")


def test_state_chip_charted_not_started():
    assert ch._state_chip(done=0, approved=3, awaiting_signoff=0, proposed_unspecified=0,
                          cost=None) == ("chip-muted", "charted, not started")


def test_state_chip_proposed_unspecified_is_not_told_it_awaits_the_operator():
    # issue_01M1FN5GDQGHR1PXY4AG9QTBWX, round two: a step merely proposed
    # with no specification written yet must not produce the "awaits your
    # sign-off" chip — that step is blocked on the crew, not on the operator.
    assert ch._state_chip(done=0, approved=0, awaiting_signoff=0, proposed_unspecified=4,
                          cost=None) == ("chip-muted", "proposed, no spec yet")


def test_state_chip_running_uncharted_when_nothing_is_charted_but_spend_exists():
    cost = {"cost_usd": 900.0, "runs": 300}
    assert ch._state_chip(done=0, approved=0, awaiting_signoff=0, proposed_unspecified=0,
                          cost=cost) == ("chip-err", "running uncharted")


def test_state_chip_not_yet_specified_when_nothing_is_charted_and_no_spend():
    assert ch._state_chip(done=0, approved=0, awaiting_signoff=0, proposed_unspecified=0,
                          cost=None) == ("chip-muted", "not yet specified")


# --- live_html: the three load-bearing behaviours ---------------------------


def test_live_html_links_a_project_with_a_bead_and_leaves_one_without_unlinked(monkeypatch):
    """Plants both cases in one render: a project with a bead doc gets an
    href; vivy, with none, renders unlinked with a stated reason. A test
    covering only the happy path would pass while vivy's row silently 404s."""
    with_bead = _course("course_1", "convivy-orient", "Orient's route", entries=[
        _entry("i1", "fixed")])
    without_bead = _course("course_2", "vivy", "Make the numbers act on", entries=[
        _entry("i2", "open")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [with_bead, without_bead])
    monkeypatch.setattr(library_client, "get_course", lambda cid:
                        {"course_1": with_bead, "course_2": without_bead}[cid])
    monkeypatch.setattr(library_client, "list_reference_documents",
                        lambda **k: [_bead_doc("convivy-orient")])
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    html = ch.live_html()
    assert 'href="/reference/doc/convivy-orient/bead/convivy-orient"' in html
    # vivy carries no href to any /reference/doc/ URL at all...
    assert "/reference/doc/vivy" not in html
    # ...and the row visibly says why, rather than looking like any other
    # unlinked-by-accident row.
    assert "no bead on file" in html
    assert "name-unlinked" in html


def test_live_html_never_renders_unattributed_cost_as_a_dollar_amount(monkeypatch):
    """Plants a product with no ledger rows this window and asserts the
    output contains no `$0.00` for it — the failure mode the redesign exists
    to prevent (issue_01M15C50ESYBPTDWV5FHZZXVH5)."""
    course = _course("course_1", "convivy-deck", "Make Deck run", entries=[_entry("i1", "open")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])  # no ledger rows at all
    html = ch.live_html()
    assert "$0.00" not in html
    assert "no attributed cost" in html


def test_live_html_draws_one_track_cell_per_step_with_the_right_maturity_class(monkeypatch):
    """A course whose steps span all four states — the whole point of the
    redesign: a reader tells "executing" from "blocked on the operator" from the
    track alone, never from a single collapsed percentage."""
    course = _course("course_1", "bosun", "Make dispatch accountable", version=9, entries=[
        _entry("i1", "fixed"),
        _entry("i2", "open", maturity="approved"),
        _entry("i3", "open", maturity="proposed"),
        _entry("i4", "open", maturity=None),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    html = ch.live_html()
    assert html.count('<span class="sg d"></span>') == 1
    assert html.count('<span class="sg a"></span>') == 1
    assert html.count('<span class="sg p"></span>') == 1
    assert html.count('<span class="sg u"></span>') == 1


# --- live_html: the rest of the page --------------------------------------


def test_live_html_renders_with_no_live_courses(monkeypatch):
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [])
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    html = ch.live_html()
    assert "Chart House" in html
    assert "No live Courses." in html


def test_live_html_raises_when_the_library_is_down(monkeypatch):
    """No static fallback lives inside this module — server.py's is what
    catches this, matching every other full-tier dynamic page."""
    monkeypatch.setattr(library_client, "list_courses", _boom)
    with pytest.raises(ConnectionError):
        ch.live_html()


def test_live_html_shows_pr_and_cost_figures_when_present(monkeypatch):
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    course = _course("course_1", "vivy", "Ship it", entries=[_entry("i1", "open")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [_fact("vivy", 12.5, today)])
    html = ch.live_html()
    assert "$12.50" in html
    assert "1 run<" in html


def test_live_html_uses_one_display_name_per_product(monkeypatch):
    """The defect the redesign closes alongside the missing link: "bosun"
    somewhere and "Agent Platform" somewhere else, neither of which is what
    the operator calls it. One name, everywhere on the page."""
    course = _course("course_1", "bosun", "Make dispatch accountable", entries=[
        _entry("i1", "open", maturity="proposed")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    html = ch.live_html()
    assert ">Bosun<" in html
    assert "Agent Platform" not in html


def test_live_html_escapes_a_course_version_containing_markup(monkeypatch):
    """`version` is a Library-supplied int only by type, never coerced
    (library_client.py) — plant markup in it and confirm the row escapes it
    like every other Library-sourced value on the page, rather than emitting
    it as live HTML."""
    course = _course("course_1", "vivy", "Ship it", version="<script>boom</script>",
                     entries=[_entry("i1", "open")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    html = ch.live_html()
    assert "<script>boom</script>" not in html
    assert "&lt;script&gt;boom&lt;/script&gt;" in html


def test_live_html_signals_a_product_awaiting_sign_off(monkeypatch):
    # A genuine sign-off wait: proposed AND a specification actually written.
    course = _course("course_1", "bosun", "Make dispatch accountable", entries=[
        _entry("i1", "open", maturity="proposed", specification="do the thing")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    html = ch.live_html()
    assert "awaiting your sign-off" in html
    assert "awaits your sign-off" in html  # the row's own chip label


def test_live_html_counts_only_proposed_entries_that_carry_a_specification(monkeypatch):
    """The exact shape from issue_01M1FN5GDQGHR1PXY4AG9QTBWX: 5 proposed
    entries on a live Course, only 1 of which carries a specification. The
    page told the operator "5 specs awaiting your sign-off" when 4 of them didn't
    exist; it must say 1, and must say who the other 4 are blocked on.

    Round two of the same issue: the banner isn't the only place this claim
    is made. The row's own chip and counts text sit two lines below it and
    must agree with it for the SAME data, not just avoid the raw "5" in
    isolation — a passing banner check that leaves the chip and counts still
    reading "5 proposed, awaiting you" is what shipped in round one."""
    course = _course("course_1", "bosun", "Make dispatch accountable", entries=[
        _entry("i1", "open", maturity="proposed", specification="the one real spec"),
        _entry("i2", "open", maturity="proposed"),
        _entry("i3", "open", maturity="proposed"),
        _entry("i4", "open", maturity="proposed"),
        _entry("i5", "open", maturity="proposed"),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    html = ch.live_html()
    # the banner. The phrase is now wrapped in a link to the product's own
    # Course section (issue_01M17W7DRZ3FJNRNYCAQFWHS1F), so assert the count
    # and the wording separately rather than as one contiguous string — the
    # claim under test here is which NUMBER the page reports, not its markup.
    assert "1 spec awaiting your sign-off</a>" in html
    assert "5 spec" not in html
    assert "has 4 steps proposed with no specification written yet" in html
    assert "waiting on the crew, not on you" in html
    # the row's chip: awaiting_signoff is 1, so the chip is honest that the operator
    # is genuinely blocking something — but never "5 proposed"
    assert "awaits your sign-off" in html
    # the row's counts text: the two clauses stay split, never recombined
    assert "1 spec awaiting you" in html
    assert "4 proposed, no spec yet" in html
    assert "5 proposed, awaiting you" not in html


def test_live_html_row_never_claims_the_operator_is_blocking_a_purely_unspecified_proposal(monkeypatch):
    """No entry here has a specification, so nobody is genuinely waiting on
    the operator. The chip and counts text must say so — never "awaits your
    sign-off" or "awaiting you" for a row where every proposed step is
    unspecified — while the banner still tells the crew the steps exist."""
    course = _course("course_1", "vivy", "Make the numbers act on", entries=[
        _entry("i1", "open", maturity="proposed"),
        _entry("i2", "open", maturity="proposed"),
        _entry("i3", "open", maturity="proposed"),
        _entry("i4", "open", maturity="proposed"),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    html = ch.live_html()
    assert "awaits your sign-off" not in html
    assert "awaiting you" not in html
    assert '<span class="chip chip-warn">' not in html  # the CSS rule itself is always present
    assert "proposed, no spec yet" in html
    assert "has 4 steps proposed with no specification written yet" in html
    assert "waiting on the crew, not on you" in html


def test_live_html_does_not_count_a_specification_with_unset_maturity_as_awaiting_signoff(
        monkeypatch):
    """A specification can be written ahead of maturity being set (the same
    early-fill case `issue_build.py` handles on the epic's own page). It must
    not be counted as awaiting the operator, and must not be counted as proposed-and-
    unspecified either — it isn't proposed at all yet."""
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "open", maturity=None, specification="written ahead of maturity")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    html = ch.live_html()
    assert "awaiting your sign-off" not in html
    assert "proposed with no specification" not in html


def test_live_html_treats_a_whitespace_only_specification_as_absent(monkeypatch):
    course = _course("course_1", "bosun", "Make dispatch accountable", entries=[
        _entry("i1", "open", maturity="proposed", specification="   \n\t  ")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    html = ch.live_html()
    assert "awaiting your sign-off" not in html
    assert "has 1 step proposed with no specification written yet" in html
    # the row itself agrees: no sign-off chip, and the counts text names the
    # step as blocked on the crew rather than recombining it into "awaiting you"
    assert "awaits your sign-off" not in html
    assert "1 proposed, no spec yet" in html


def test_awaiting_sign_off_text_is_a_link_to_the_products_own_course_section(monkeypatch):
    """issue_01M17W7DRZ3FJNRNYCAQFWHS1F: every place this page tells the operator a
    sign-off is wanted has to say where to look, not just that it's wanted.
    All three renderings — the row chip, the row's counts line, and the
    fleet-level signals line — link to the SAME product-scoped anchor on
    /engineering/course, where the proposed steps themselves live, rather
    than a page that only re-counts the same number.

    The entry carries a SPECIFICATION deliberately. `issue_01M1FN5GDQGHR1PXY4AG9QTBWX`
    split "proposed" into what actually awaits the operator (proposed WITH a spec) and
    what is waiting on the crew (proposed with none), and only the first is
    theirs to act on — so only the first is linked. A proposed entry with no
    specification renders no link at all, which
    `test_live_html_row_never_claims_the_operator_is_blocking_a_purely_unspecified_proposal`
    pins from the other side."""
    course = _course("course_1", "bosun", "Make dispatch accountable", entries=[
        _entry("i1", "open", maturity="proposed", specification="the one real spec")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    html = ch.live_html()
    href = ch.issue_build.course_anchor_href("bosun")
    assert href == "/engineering/course#course-bosun"
    assert f'<a class="chip chip-warn" href="{href}">awaits your sign-off</a>' in html
    assert f'<a href="{href}">1 spec awaiting you</a>' in html
    assert f'<a href="{href}">1 spec awaiting your sign-off</a>' in html
    # and that's a real, full-tier route registered against the Course
    # renderer — not a string that merely looks like one.
    import server
    routes = server._full_only_routes()
    base = href.split("#")[0]
    assert base in routes
    assert routes[base][1] is ch.issue_build.live_html_course


def test_an_unspecified_proposal_gets_no_sign_off_link(monkeypatch):
    """The link follows the sign-off claim rather than the raw proposed count.
    A proposed step with no specification is the crew's to write, so the page
    must neither claim the operator is blocking it nor hand them an anchor to it."""
    course = _course("course_1", "bosun", "Make dispatch accountable", entries=[
        _entry("i1", "open", maturity="proposed")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    html = ch.live_html()
    assert "awaiting you" not in html
    assert "/engineering/course#course-bosun" not in html
    assert "1 proposed, no spec yet" in html


def test_no_proposed_steps_renders_no_sign_off_link(monkeypatch):
    """A row with nothing pending must not carry a dangling course-review
    link — the chip only becomes an <a> when `_state_chip` actually returns
    chip-warn, which requires a proposed step to exist."""
    course = _course("course_1", "bosun", "Make dispatch accountable", entries=[
        _entry("i1", "fixed")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    html = ch.live_html()
    assert "awaits your sign-off" not in html
    assert "awaiting your sign-off" not in html
    assert "/engineering/course#course-bosun" not in html
    assert '<span class="chip chip-ok">underway</span>' in html


def test_live_html_flags_running_uncharted_with_an_anomaly_note(monkeypatch):
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    course = _course("course_1", "vivy", "Make the numbers act on", version=1,
                     entries=[_entry("i1", "open", maturity=None)])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [_fact("vivy", 903.49, today)])
    html = ch.live_html()
    assert "running uncharted" in html
    assert "row-anomaly" in html
    assert "never been specified" in html


# --- the PR ribbon (round two, the operator's follow-up 2026-08-28) -------------------


def _signal(product, produced_at):
    return library_client.SignalRecord(
        id="s", source="github", stream=None, kind="changelog", actor="a",
        ingested_by=None, product=product, body="", refs={}, meta={},
        privacy_class="internal", produced_at=produced_at, ingested_at=produced_at)


def test_merge_counts_by_repo_counts_within_the_window_only(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    old = now - dt.timedelta(days=ch.MERGE_WINDOW_DAYS + 1)
    monkeypatch.setattr(library_client, "list_signals", lambda **k: [
        _signal("library", now), _signal("library", now), _signal("library", old),
        _signal("vivy", now),
    ])
    assert ch._merge_counts_by_repo() == {"library": 2, "vivy": 1}


def test_merge_counts_by_repo_filters_on_github_changelog_signals(monkeypatch):
    captured = {}

    def fake(**k):
        captured.update(k)
        return []

    monkeypatch.setattr(library_client, "list_signals", fake)
    ch._merge_counts_by_repo()
    assert captured["source"] == ["github"]
    assert captured["kind"] == ["changelog"]


def test_ribbon_says_unavailable_when_the_feed_is_cold_not_zero_open():
    """`cached_open_prs()` returning None means "cannot say," not "none open"
    — the failure mode this whole feature exists to prevent. Merge counts are
    planted nonzero so a passing assertion can't be trivially true because
    every count in sight happens to be zero."""
    html = ch._ribbon_html("library", None, {"library": 5})
    assert "PR feed unavailable" in html
    assert "0" not in html


def test_ribbon_renders_no_prs_open_muted_with_no_link_or_dot():
    html = ch._ribbon_html("library", [], {"library": 20})
    assert "no PRs open" in html
    assert "<a " not in html
    assert "rdot" not in html
    assert "20 merged, 7 days" in html


def test_ribbon_renders_one_open_pr_with_a_dot_state_and_the_merge_queue_link():
    prs = [_pr("library", 1, "cleared", "passing")]
    html = ch._ribbon_html("library", prs, {"library": 20})
    assert 'href="/engineering/merge-queue"' in html
    assert "1 ready to merge" in html
    assert "rdot ok" in html


def test_ribbon_renders_each_of_the_four_dot_states():
    """Not just the two states today's live PRs happen to exercise (draft,
    ready) — a state that first appears months from now must already have a
    styled class waiting for it."""
    cases = [
        ([_pr("library", 1, "draft", "passing")], "draft", "1 draft — CI passing"),
        ([_pr("library", 1, "in-cycle", "pending")], "warn", "1 CI running"),
        ([_pr("library", 1, "blocked", "failing")], "err", "1 blocked — CI failing"),
        ([_pr("library", 1, "cleared", "passing")], "ok", "1 ready to merge"),
    ]
    for prs, dot, text in cases:
        html = ch._ribbon_html("library", prs, {})
        assert f'rdot {dot}"' in html, html
        assert text in html, html


def test_ribbon_sums_open_prs_and_merges_across_the_selector_repo_alias():
    """selector spans two real GitHub repos (selector, selector-backend) —
    the same pair _COST_ALIASES sums for the run ledger."""
    prs = [_pr("selector", 1, "draft", "passing"),
           _pr("selector-backend", 2, "blocked", "failing")]
    html = ch._ribbon_html("selector", prs, {"selector": 3, "selector-backend": 7})
    assert "10 merged, 7 days" in html
    assert "rdot err" in html  # the worse bucket, across both aliased repos, wins
    assert "1 blocked — CI failing" in html


def test_ribbon_resolves_bosun_through_the_agent_platform_repo_alias():
    prs = [_pr("agent-platform", 464, "blocked", "failing")]
    html = ch._ribbon_html("bosun", prs, {"agent-platform": 38})
    assert "38 merged, 7 days" in html
    assert "rdot err" in html


def test_live_html_never_renders_a_false_zero_when_the_pr_feed_is_cold(monkeypatch):
    """The None-handling wired through live_html(), not just the isolated
    _ribbon_html unit above."""
    course = _course("course_1", "vivy", "Ship it", entries=[_entry("i1", "open")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    monkeypatch.setattr(ch.merge_queue_build, "cached_open_prs", lambda: None)
    today = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(library_client, "list_signals", lambda **k: [_signal("vivy", today)])
    html = ch.live_html()
    assert "PR feed unavailable" in html
    # a real, nonzero merge count still renders — the assertion above isn't
    # passing merely because everything on the row happens to be zero
    assert "1 merged, 7 days" in html


def test_live_html_wires_the_open_pr_ribbon_per_row(monkeypatch):
    course = _course("course_1", "vivy", "Ship it", entries=[_entry("i1", "open")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(ch.dashboard_build, "normalize", lambda: [])
    monkeypatch.setattr(ch.merge_queue_build, "cached_open_prs",
                        lambda: [_pr("vivy", 1, "cleared", "passing")])
    html = ch.live_html()
    assert "1 ready to merge" in html
    assert 'href="/engineering/merge-queue"' in html


# --- the operator's approve control (issue_01M32CB6A9T04NVYPARGDT2GWQ) -------
#
# **The operator, 2026-09-21:** "all epics stay as proposed, and are displayed as
# proposed, until the operator presses the approve button in either Deck or
# Orient." Approving also puts the epic on its product's Course.
#
# The section exists because the Course rows above cannot carry the control:
# they render step counts, naming no individual epic, and an epic not yet on
# any Course produces no step at all — which after the Library's change is
# every newly filed one, exactly the set that most needs approving.


def _item(issue_id, *, product="vivy", maturity="proposed", itype="epic",
          state="open", title=None, specification="spec text"):
    import datetime as dt
    return library_client.IssueRecord(
        id=issue_id, url=f"/engineering/issues/{issue_id}", product=product,
        title=title or issue_id, body="", type=itype, state=state, severity="high",
        needs="agent", maturity=maturity, parent_id=None, topics=[], refs={},
        specification=specification, definition_of_done="dod", resolution=None,
        resolved_by=None, outcome_note=None, claimed_by=None, claimed_at=None,
        claim_expires_at=None, claim_expired=False, resolved_at=None,
        created_by="ops-mcp", created_by_kind="agent",
        created_at=dt.datetime(2026, 9, 21), run_id=None, park_reason=None,
        unpark_condition=None, parked_at=None, blocked=False, prerequisites=[])


def _seed_items(monkeypatch, items):
    """The approve section's own source, plus an empty Course list so these
    tests exercise `live_html` without also standing up a Course — the two are
    independent by design, which is the point of the section."""
    monkeypatch.setattr(library_client, "list_issue_items", lambda: items)
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [])
    monkeypatch.setattr(library_client, "get_course", lambda cid: None)


def test_a_proposed_epic_gets_an_approve_control(monkeypatch):
    _seed_items(monkeypatch, [_item("issue_a", title="Needs your nod")])
    html = ch.live_html()
    assert 'data-approve="issue_a"' in html
    assert "Needs your nod" in html
    assert "1 epic awaiting your approval" in html


def test_an_approved_epic_gets_no_control(monkeypatch):
    """The control is for `proposed` and nothing else — an approved epic is
    already on its Course, and offering the button again would invite a press
    the Library would have to decide about."""
    _seed_items(monkeypatch, [_item("issue_done", maturity="approved")])
    html = ch.live_html()
    assert "data-approve=" not in html
    assert "awaiting your approval" not in html


def test_a_terminal_epic_is_not_awaiting_approval(monkeypatch):
    """State beats maturity, the same priority `_step_class` applies: nothing
    clears `maturity` when an epic resolves, so a fixed epic still reads
    `proposed`. Two of the four live proposed epics were in exactly that state
    when this was written."""
    _seed_items(monkeypatch, [
        _item("issue_fixed", state="fixed"),
        _item("issue_dup", state="duplicate"),
        _item("issue_live"),
    ])
    html = ch.live_html()
    assert 'data-approve="issue_live"' in html
    assert 'data-approve="issue_fixed"' not in html
    assert 'data-approve="issue_dup"' not in html


def test_a_non_epic_is_never_offered_for_approval(monkeypatch):
    _seed_items(monkeypatch, [_item("issue_bug", itype="bug")])
    assert "data-approve=" not in ch.live_html()


def test_an_epic_with_no_specification_is_flagged_but_keeps_its_control(monkeypatch):
    """**The refusal case the definition of done names.** The Library refuses
    an approval for an epic carrying no specification, and that refusal is what
    the page must be able to show. Disabling the button would put the refusal
    path behind a state nobody can reach — so the epic is flagged and the
    control stays live."""
    _seed_items(monkeypatch, [_item("issue_nospec", specification="   ")])
    html = ch.live_html()
    assert "no specification yet" in html
    assert 'data-approve="issue_nospec"' in html


def test_the_control_is_gated_on_the_viewers_role(monkeypatch):
    """`__CAN_APPROVE_EPIC__` is server.py's job to substitute from the
    viewer's role; this page's job is to emit the placeholder and the gated
    section. A contractor gets `false` and the CSS hides the whole section —
    and `approve_api` refuses them regardless, which is the actual wall."""
    _seed_items(monkeypatch, [_item("issue_a")])
    html = ch.live_html()
    assert 'data-can-approve="__CAN_APPROVE_EPIC__"' in html
    # The hiding rule has to exist, or `false` would render the control anyway.
    assert '.approvals[data-can-approve="false"] { display: none; }' in html


def test_the_note_can_render_a_refusal_where_the_control_is(monkeypatch):
    """"A refusal from the Library renders where the control is, naming what
    was refused." The note element is inside the row, so it cannot land
    somewhere else on the page, and the script fills it from the server's own
    wording rather than a message of its own."""
    _seed_items(monkeypatch, [_item("issue_a")])
    html = ch.live_html()
    row = html[html.index('data-epic="issue_a"'):]
    row = row[:row.index("</div>", row.index("ap-note"))]
    assert 'class="ap-note"' in row, "the note belongs inside the epic's own row"
    assert "res.body && res.body.error" in html


def test_the_page_renders_when_nothing_awaits_approval(monkeypatch):
    """No section at all rather than an empty heading — the same choice
    `_signals_html` makes when it has no lines."""
    _seed_items(monkeypatch, [])
    html = ch.live_html()
    assert "awaiting your approval" not in html
    assert "Chart House" in html


def test_a_library_failure_takes_the_page_down_rather_than_hiding_the_queue(monkeypatch):
    """**Uncaught on purpose**, like every other read on this page. A page that
    silently showed no epics awaiting approval would tell the operator there is nothing
    to approve, which is the one wrong answer this section must never give."""
    def boom():
        raise RuntimeError("Library unreachable")

    # The Courses stubbed to succeed, so the raise under test is the ITEM read
    # and not `_course_rows` failing first — which would raise too, and the
    # test would pass without the behaviour it names ever running.
    _seed_items(monkeypatch, [])
    monkeypatch.setattr(library_client, "list_issue_items", boom)
    with pytest.raises(RuntimeError, match="Library unreachable"):
        ch.live_html()


# --- an epic put forward with no maturity set (reviewer, PR #316) ------------
# `POST /items` has never accepted `maturity`, so an epic is created with none
# and something later sets it. An epic whose specification was written without
# that step is invisible to a `== "proposed"` filter — and approving is now the
# only way anything reaches `approved`, so nobody could ever move it. Measured
# on 2026-09-21: of 93 non-terminal epics, 18 carried no maturity and two of
# those carried a full specification.


def test_an_unset_maturity_epic_with_a_specification_is_awaiting_approval(monkeypatch):
    """The stranded case. Two live epics were in exactly this state when the
    filter was widened."""
    _seed_items(monkeypatch, [_item("issue_stranded", maturity=None,
                                    specification="a real specification")])
    html = ch.live_html()
    assert 'data-approve="issue_stranded"' in html


def test_an_unset_maturity_epic_with_nothing_written_is_not_listed(monkeypatch):
    """Put forward by nobody. Listing the other 16 would add rows the operator cannot
    act on, because the Library refuses an approval with no specification.

    **That is the whole reason, and this docstring used to give a second one
    that was false** — that the Courses they sit on report them through the
    signals line. They do not: `_proposed_without_spec` requires
    `maturity == "proposed"` exactly and never sees an unset-maturity entry, so
    nothing about one reaches `_signals_html`. The claim was doubly wrong here,
    since `_seed_items` leaves `list_courses` empty and this epic sits on no
    Course at all. Corrected in `_proposed_epics`'s own docstring first and
    left standing here for a round — when a false claim is fixed, every copy of
    it has to be found, not the one a reviewer pointed at."""
    _seed_items(monkeypatch, [_item("issue_blank", maturity=None, specification=None)])
    html = ch.live_html()
    assert "data-approve=" not in html
    assert "awaiting your approval" not in html


def test_a_whitespace_only_specification_is_not_a_specification(monkeypatch):
    """The same reading `_awaits_signoff` already applies two functions up: a
    whitespace-only specification is not something to sign off on either."""
    _seed_items(monkeypatch, [_item("issue_ws", maturity=None, specification="   \n  ")])
    assert "data-approve=" not in ch.live_html()


def test_a_proposed_epic_with_no_specification_is_still_listed(monkeypatch):
    """The asymmetry is deliberate: the crew claimed this one is ready, so a
    missing specification on it is an anomaly worth seeing rather than hiding.
    It is flagged, and pressing Approve surfaces the Library's own refusal."""
    _seed_items(monkeypatch, [_item("issue_claimed", maturity="proposed",
                                    specification=None)])
    html = ch.live_html()
    assert 'data-approve="issue_claimed"' in html
    assert "no specification yet" in html


def test_an_approved_epic_is_never_listed_whatever_it_has_written(monkeypatch):
    """The positive control for the widening: `_put_forward` must not let an
    already-approved epic back in through the specification branch."""
    _seed_items(monkeypatch, [_item("issue_done", maturity="approved",
                                    specification="a real specification")])
    assert "data-approve=" not in ch.live_html()


def test_an_unset_maturity_step_reaches_no_signal_line(monkeypatch):
    """**Pins the claim `_proposed_epics`'s docstring makes about the 16 epics
    it excludes**, because an earlier draft of that comment asserted the
    opposite and a reviewer caught it.

    `_proposed_without_spec` requires `maturity == "proposed"` exactly, so an
    unset-maturity entry never reaches it and never reaches `_signals_html`.
    It lands in `_step_class`'s "u" bucket instead — a track segment and a
    per-row count, and only when the epic is on a live Course.

    Asserted on the rendered page rather than by calling the predicate, so it
    covers the whole path a reader would have to trace by hand."""
    course = _course("c1", "vivy", "Vivy Course", entries=[
        _entry("issue_unset", "open", maturity=None, specification=None),
    ])
    monkeypatch.setattr(library_client, "list_issue_items", lambda: [])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    html = ch.live_html()
    assert ch._proposed_without_spec(_entry("i", "open", maturity=None)) is False
    assert ch._step_class(_entry("i", "open", maturity=None)) == "u"
    assert "proposed with no specification written yet" not in html, (
        "an unset-maturity step must not be reported as a proposed one")
