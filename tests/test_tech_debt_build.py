"""CQ·164 (issue_01M57NS4ZPTBGG1QCMXX5YCPXF): /tech-debt reports each project's
open corrections from the Library's queue.

The figures are asserted against `library_fake`'s stub of
`library_client.list_issue_items`, never a live Library, and with a fixed `now`
so an age band cannot move under the test. The scoping tests render the page as
a viewer holding one project while a second project's items sit in the same
store — the shape tests/test_scope_route_walk.py renders the route in.
"""
from __future__ import annotations

import datetime as dt
import re

import pytest

import issue_build
import library_client
import library_fake
import tech_debt_build as tdb

NOW = dt.datetime(2026, 10, 18, 12, 0, 0)
ALPHA = frozenset({"alpha"})
WHOLE_FLEET = frozenset({"alpha", "beta"})


@pytest.fixture
def store(monkeypatch):
    library_fake.patch(monkeypatch)
    # Seeds are dated from NOW, so the page must measure age from NOW too;
    # read from the real clock, an item seeded 40 days back reads 41 days
    # once a day has passed since NOW.
    monkeypatch.setattr(tdb, "utcnow", lambda: NOW)
    return library_fake


def _seed(product, item_id, *, days=1, title=None, **fields):
    return library_fake.seed_issue(
        id=item_id, product=product, title=title or f"{item_id} title",
        created_at=NOW - dt.timedelta(days=days), **fields)


def _rows(scope=ALPHA, now=NOW):
    return tdb.summarise(library_client.list_issue_items(), scope, now)


# --- which items are tech debt ----------------------------------------------


def test_a_queue_item_and_an_unparented_item_both_count(store):
    """The Library is moving every correction's parent from null to its
    product's queue id, so both readings must hold (issue_build._is_correction's
    own docstring)."""
    library_fake.seed_corrections_queue("alpha")
    _seed("alpha", "issue_queue_parented", parent_id="alpha")
    _seed("alpha", "issue_unparented")
    rows = _rows()
    assert [r.total for r in rows] == [2]


def test_an_epics_child_is_the_epics_work_not_tech_debt(store):
    library_fake.seed_corrections_queue("alpha")
    library_fake.seed_issue(id="issue_epic", product="alpha", title="Epic",
                            type="epic", state="open")
    _seed("alpha", "issue_child", parent_id="issue_epic")
    _seed("alpha", "issue_correction", parent_id="alpha")
    assert _rows()[0].total == 1


def test_a_blocker_is_not_tech_debt(store):
    """A blocker is work only the operator can do; `lane_of` routes it away
    from the corrections queue before this page sees it, exactly as
    /engineering/corrections does."""
    library_fake.seed_corrections_queue("alpha")
    _seed("alpha", "issue_blocker", parent_id="alpha", needs="operator")
    _seed("alpha", "issue_correction", parent_id="alpha")
    assert _rows()[0].total == 1


def test_a_resolved_item_is_not_open_debt(store):
    library_fake.seed_corrections_queue("alpha")
    _seed("alpha", "issue_fixed", parent_id="alpha", state="fixed")
    _seed("alpha", "issue_open", parent_id="alpha", state="open")
    assert _rows()[0].total == 1


@pytest.mark.parametrize("state", ["claimed", "pr-open"])
def test_claimed_and_pr_open_items_are_still_open(store, state):
    library_fake.seed_corrections_queue("alpha")
    _seed("alpha", "issue_live", parent_id="alpha", state=state)
    assert _rows()[0].total == 1


def test_an_item_whose_parent_names_nothing_is_still_counted(store):
    """The Corrections page keeps an item whose parent names no item at all on
    the page rather than dropping it, and the dashboard must count what that
    page shows. Reading the two rules apart is how they drifted before this
    module used the page's own partition."""
    _seed("alpha", "issue_dangling", parent_id="issue_does_not_exist")
    assert _rows()[0].total == 1


def test_the_count_matches_the_corrections_pages_own_partition(store):
    """The dashboard and /engineering/corrections must agree about what a
    correction is, which is why one imports the other's classification rather
    than restating it."""
    library_fake.seed_corrections_queue("alpha")
    _seed("alpha", "issue_a1", parent_id="alpha")
    _seed("alpha", "issue_a2")
    library_fake.seed_issue(id="issue_epic", product="alpha", title="Epic",
                            type="epic", state="open")
    _seed("alpha", "issue_a3", parent_id="issue_epic")
    items = library_client.list_issue_items()
    _, _, corrections = issue_build._epic_and_correction_groups(items)
    assert _rows()[0].total == len(corrections) == 2


# --- the figures -------------------------------------------------------------


def test_severity_is_counted_per_project(store):
    _seed("alpha", "issue_high", severity="high")
    _seed("alpha", "issue_high_2", severity="high")
    _seed("alpha", "issue_medium", severity="medium")
    _seed("alpha", "issue_low", severity="low")
    _seed("alpha", "issue_unset", severity=None)
    _seed("alpha", "issue_unknown", severity="critical")
    row = _rows()[0]
    assert row.by_severity == {"high": 2, "medium": 1, "low": 1, "unset": 2}
    assert row.total == 6


@pytest.mark.parametrize("days,band", [
    (0, "under 7 days"), (6, "under 7 days"),
    (7, "7-30 days"), (30, "7-30 days"),
    (31, "31-90 days"), (90, "31-90 days"),
    (91, "over 90 days"), (400, "over 90 days"),
])
def test_age_bands_meet_at_their_edges(store, days, band):
    _seed("alpha", "issue_aged", days=days)
    assert _rows()[0].by_band[band] == 1


def test_a_future_timestamp_falls_in_the_youngest_band(store):
    """A clock running slightly ahead must not push an item outside every
    band — the clamp `issue_build._age` already applies to its own ages."""
    _seed("alpha", "issue_skewed", days=-2)
    assert _rows()[0].by_band["under 7 days"] == 1


def test_the_oldest_item_is_named_with_its_age(store):
    _seed("alpha", "issue_young", days=3)
    _seed("alpha", "issue_old", days=120, title="The oldest correction")
    row = _rows()[0]
    assert row.oldest.id == "issue_old"
    assert row.oldest_age_days == 120


def test_a_project_with_an_empty_queue_keeps_a_row_of_zeros(store):
    _seed("beta", "issue_beta")
    rows = tdb.summarise(library_client.list_issue_items(), ALPHA, NOW)
    assert [(r.product, r.total, r.oldest) for r in rows] == [("alpha", 0, None)]


def test_projects_are_ordered_worst_first(store):
    for i in range(3):
        _seed("alpha", f"issue_alpha_{i}")
    _seed("beta", "issue_beta")
    rows = tdb.summarise(library_client.list_issue_items(), WHOLE_FLEET, NOW)
    assert [r.product for r in rows] == ["alpha", "beta"]


# --- the rendered page -------------------------------------------------------


def _html(scope=ALPHA):
    return tdb.live_html(scope)


def test_the_page_reports_the_figures_and_names_the_oldest(store):
    _seed("alpha", "issue_high", days=40, severity="high", title="Fix the pump")
    _seed("alpha", "issue_low", days=2, severity="low")
    html = _html()
    assert "2 open" in html
    assert "Fix the pump" in html
    assert "/engineering/issues/issue_high" in html
    assert "40d" in html


def test_the_page_carries_its_last_live_render_time(store):
    _seed("alpha", "issue_one")
    html = _html()
    assert 'class="render-stamp"' in html
    assert re.search(r'data-rendered-at="\d{4}-\d{2}-\d{2} \d{2}:\d{2} MST"', html), (
        "a live page must say when it rendered")


def test_an_empty_queue_renders_a_zero_rather_than_an_empty_page(store):
    """A project with nothing open still gets its row, so an empty queue reads
    as a fact about that project rather than as a project the page lost."""
    html = _html()
    assert "0 open" in html
    assert ">alpha<" in html
    assert "Oldest open correction per project" in html


def test_a_viewer_sees_no_project_outside_their_scope(store):
    _seed("alpha", "issue_alpha", title="Alpha correction")
    _seed("beta", "issue_beta", title="Beta correction")
    html = _html(ALPHA)
    assert "Alpha correction" in html
    assert "beta" not in html
    assert "issue_beta" not in html


def test_a_whole_fleet_viewer_sees_the_second_project(store):
    """The positive control for the test above: the same fixture rendered for
    a viewer holding both projects does carry the second one, so the absence
    asserted there is the scope filter and not a page that never showed it."""
    _seed("alpha", "issue_alpha", title="Alpha correction")
    _seed("beta", "issue_beta", title="Beta correction")
    html = _html(WHOLE_FLEET)
    assert "Beta correction" in html
    assert "beta" in html


def test_an_administrator_scope_takes_its_projects_from_the_library(store):
    """`{"*"}` is the Library's own answer for an administrator, not a flag
    this module branches on, so the project rows come from the items."""
    _seed("alpha", "issue_alpha")
    _seed("beta", "issue_beta")
    rows = tdb.summarise(library_client.list_issue_items(), frozenset({"*"}), NOW)
    assert [r.product for r in rows] == ["alpha", "beta"]


def test_a_library_failure_raises_rather_than_rendering_an_empty_queue(store, monkeypatch):
    """An empty page and a Library outage look identical to a reader, so this
    module lets the failure through and server.py serves the last static
    build."""
    def _boom():
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_issue_items", _boom)
    with pytest.raises(RuntimeError):
        tdb.live_html(ALPHA)
