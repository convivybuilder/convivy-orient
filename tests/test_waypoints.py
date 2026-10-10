"""Tests for waypoints.py — the shared waypoint-pace computation behind the
front page's hero stat, /overview's velocity card, and /velocity's headline
(dashboards epic issue_01M2F3Q1B8J9E0WC5J1F1CMWA1, item
issue_01M2F3QV06RFYTQSFS0MJ4F7PF).

`library_client` is monkeypatched throughout, never called live, mirroring
`test_chart_house_build.py`'s own fixture pattern for Courses (this module's
`is_passed` is the terminal test `chart_house_build._step_class` now calls
too, so a passed waypoint and a "d" step must agree)."""
from __future__ import annotations

import datetime as dt

import pytest

import library_client
import waypoints
from models import utcnow


def _course(course_id, product, title, *, version=1, entries):
    return library_client.CourseRecord(
        id=course_id, product=product, title=title, status="live", version=version,
        entries=entries, created_by="ops-mcp", created_at=None,
        updated_by="ops-mcp", updated_at=None)


def _entry(issue_id, state, title=None, resolved_at=None):
    return library_client.CourseEntry(issue_id=issue_id, state=state, title=title or issue_id,
                                      resolved_at=resolved_at)


def _boom(*a, **k):
    raise ConnectionError("Library unreachable")


# --- is_passed ---------------------------------------------------------


def test_is_passed_true_for_every_terminal_state():
    for state in ("fixed", "wontfix", "duplicate", "redirected"):
        assert waypoints.is_passed(_entry("i1", state))


def test_is_passed_false_for_open_states_and_unresolved_entries():
    assert not waypoints.is_passed(_entry("i1", "open"))
    assert not waypoints.is_passed(_entry("i1", "pr-open"))
    assert not waypoints.is_passed(_entry("i1", None))  # get_course raced away


# --- course_progress: mixed Course --------------------------------------


def test_mixed_course_reports_passed_in_flight_and_remaining(monkeypatch):
    course = _course("course_1", "bosun", "Make dispatch accountable", entries=[
        _entry("i1", "fixed", title="Ship the queue"),
        _entry("i2", "wontfix", title="Old plan"),
        _entry("i3", "open", title="Wire the dashboard"),
        _entry("i4", "open", title="Write the runbook"),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    rows = waypoints.course_progress()
    assert len(rows) == 1
    row = rows[0]
    assert row.product == "bosun"
    assert row.total == 4
    assert row.passed == 2
    assert row.remaining == 2
    # first non-terminal entry in Course order, not just any open one
    assert row.in_flight == "Wire the dashboard"


def test_course_with_no_terminal_entries_reports_zero_passed(monkeypatch):
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "open", title="Step one"),
        _entry("i2", "pr-open", title="Step two"),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    rows = waypoints.course_progress()
    assert rows[0].passed == 0
    assert rows[0].remaining == 2
    assert rows[0].in_flight == "Step one"


def test_fully_passed_course_has_no_entry_in_flight(monkeypatch):
    course = _course("course_1", "vivy", "Ship it", entries=[_entry("i1", "fixed")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    rows = waypoints.course_progress()
    assert rows[0].passed == 1
    assert rows[0].remaining == 0
    assert rows[0].in_flight is None


def test_product_with_no_live_course_is_simply_absent(monkeypatch):
    """A product with no live Course is shown as having none, not as an
    error — course_progress() only ever iterates what list_courses returns,
    so an absent product produces no row rather than a placeholder or a
    crash."""
    course = _course("course_1", "vivy", "Ship it", entries=[_entry("i1", "fixed")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    rows = waypoints.course_progress()
    assert [r.product for r in rows] == ["vivy"]  # "constance" etc. simply don't appear


def test_course_progress_raises_when_list_courses_fails(monkeypatch):
    """A Library outage must propagate — callers render "unavailable" on the
    exception, never a false 0 (library_client's own rule, restated here)."""
    monkeypatch.setattr(library_client, "list_courses", _boom)
    with pytest.raises(ConnectionError):
        waypoints.course_progress()


def test_one_courses_detail_failing_degrades_that_row_and_not_the_others(monkeypatch, capsys):
    good = _course("course_1", "vivy", "Ship it", entries=[_entry("i1", "fixed")])
    bad = _course("course_2", "selector", "Make playlists good", entries=[_entry("i2", None)])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [good, bad])

    def flaky(cid):
        if cid == "course_2":
            raise ConnectionError("Library unreachable")
        return good

    monkeypatch.setattr(library_client, "get_course", flaky)
    rows = waypoints.course_progress()
    assert len(rows) == 2
    by_product = {r.product: r for r in rows}
    assert by_product["vivy"].passed == 1
    assert by_product["selector"].passed == 0  # degraded to the list record's unresolved state
    assert "course_2" in capsys.readouterr().err


# --- windowed pace (dashboards epic issue_01M2F3Q1B8J9E0WC5J1F1CMWA1) -------


def test_passed_entry_resolved_3_days_ago_counts_in_every_window(monkeypatch):
    now = utcnow()
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "fixed", resolved_at=now - dt.timedelta(days=3)),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    row = waypoints.course_progress()[0]
    assert row.passed_within[7] == 1
    assert row.passed_within[30] == 1
    assert row.passed_within[90] == 1


def test_passed_entry_resolved_20_days_ago_counts_in_30_and_90_only(monkeypatch):
    now = utcnow()
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "fixed", resolved_at=now - dt.timedelta(days=20)),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    row = waypoints.course_progress()[0]
    assert row.passed_within[7] == 0
    assert row.passed_within[30] == 1
    assert row.passed_within[90] == 1


def test_passed_entry_with_no_resolved_at_counts_in_no_window_but_still_passed(monkeypatch):
    course = _course("course_1", "vivy", "Ship it", entries=[_entry("i1", "fixed")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    row = waypoints.course_progress()[0]
    assert row.passed == 1  # still counts toward position
    assert row.passed_within[7] == 0
    assert row.passed_within[30] == 0
    assert row.passed_within[90] == 0


def test_open_entry_never_counts_in_any_window_however_old_its_resolved_at(monkeypatch):
    """An open entry with a `resolved_at` set (a reopened issue, say) must
    never count as passed in any window — `is_passed` gates every window
    check, `resolved_at` alone is not enough."""
    now = utcnow()
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "open", resolved_at=now - dt.timedelta(days=1)),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    row = waypoints.course_progress()[0]
    assert row.passed == 0
    assert row.passed_within[7] == 0
    assert row.passed_within[30] == 0
    assert row.passed_within[90] == 0


def test_window_includes_its_exact_edge_and_excludes_just_past_it():
    """Round-1 review on #294: every other window test sits well inside or well
    outside a window, so flipping `age <= timedelta(days=days)` to `<` would pass.
    Pin both sides of the 7-day edge with a fixed `now`."""
    now = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.timezone.utc)
    at_edge = _entry("i1", "fixed", resolved_at=now - dt.timedelta(days=7))
    past_edge = _entry("i2", "fixed", resolved_at=now - dt.timedelta(days=7, seconds=1))
    assert waypoints._passed_within(at_edge, 7, now)
    assert not waypoints._passed_within(past_edge, 7, now)
    assert waypoints._passed_within(past_edge, 30, now)


def test_a_resolved_at_in_the_future_counts_in_no_window():
    """Clock skew between this host and the Library can put `resolved_at` after
    `now`; the `timedelta(0) <= age` bound keeps that out of every window. Flip it
    to `<`, or drop it, and a skewed entry resolved one second ahead would count."""
    now = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.timezone.utc)
    ahead = _entry("i1", "fixed", resolved_at=now + dt.timedelta(seconds=1))
    exactly_now = _entry("i2", "fixed", resolved_at=now)
    for days in waypoints.WINDOWS:
        assert not waypoints._passed_within(ahead, days, now)
        assert waypoints._passed_within(exactly_now, days, now)


def test_total_within_sums_across_products(monkeypatch):
    now = utcnow()
    c1 = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "fixed", resolved_at=now - dt.timedelta(days=2)),
    ])
    c2 = _course("course_2", "bosun", "Make dispatch accountable", entries=[
        _entry("i2", "fixed", resolved_at=now - dt.timedelta(days=5)),
        _entry("i3", "fixed", resolved_at=now - dt.timedelta(days=40)),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [c1, c2])
    monkeypatch.setattr(library_client, "get_course", lambda cid: {"course_1": c1, "course_2": c2}[cid])

    rows = waypoints.course_progress()
    assert waypoints.total_within(rows, 7) == 2  # the two entries within 7 days
    assert waypoints.total_within(rows, 30) == 2
    assert waypoints.total_within(rows, 90) == 3


# --- fetch_courses / course_progress(courses=...) sharing one read --------


def test_fetch_courses_raises_when_list_courses_fails(monkeypatch):
    monkeypatch.setattr(library_client, "list_courses", _boom)
    with pytest.raises(ConnectionError):
        waypoints.fetch_courses()


def test_course_progress_accepts_a_prefetched_course_list_without_refetching(monkeypatch):
    """The /velocity page fetches once and hands the same courses to both
    `course_progress` and `weekly_pace` — `course_progress(courses)` must not
    call `list_courses`/`get_course` again."""
    course = _course("course_1", "vivy", "Ship it", entries=[_entry("i1", "fixed")])
    monkeypatch.setattr(library_client, "list_courses", _boom)
    monkeypatch.setattr(library_client, "get_course", _boom)

    rows = waypoints.course_progress([course])
    assert rows[0].product == "vivy"
    assert rows[0].passed == 1


# --- weekly_pace (issue_01M2HXZKHKZFVZK9K1MJ5KTKTA) ------------------------


_NOW = dt.datetime(2026, 9, 15, 12, 0)  # naive UTC, this codebase's convention


def test_weekly_pace_fills_a_quiet_week_with_zero_rather_than_a_gap():
    """Entries land 3 and 1 weeks ago; nothing happened 2 weeks ago. That
    middle week must still appear, at 0 — not be skipped."""
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "fixed", resolved_at=_NOW - dt.timedelta(days=21)),
        _entry("i2", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
    ])
    weekly = waypoints.weekly_pace([course], now=_NOW)
    weeks = [w.week for w in weekly]
    assert len(weeks) == len(set(weeks))  # no duplicate/missing week
    totals_by_week = {w.week: w.total for w in weekly}
    gap_week = waypoints._iso_week(_NOW - dt.timedelta(days=14))
    assert gap_week in totals_by_week
    assert totals_by_week[gap_week] == 0


def test_weekly_pace_marks_only_the_current_week_partial():
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
        _entry("i2", "fixed", resolved_at=_NOW),
    ])
    weekly = waypoints.weekly_pace([course], now=_NOW)
    partial_weeks = [w.week for w in weekly if w.partial]
    assert partial_weeks == [waypoints._iso_week(_NOW)]


def test_weekly_pace_splits_builder_crews_from_products_and_sums_to_total():
    """Builder's crews (library here) collapse to one "Builder" group,
    distinct from a product's own name (vivy) — and the groups still add up
    to that week's total, per the item's definition of done."""
    builder_course = _course("course_1", "library", "Ship the queue", entries=[
        _entry("i1", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
        _entry("i2", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
    ])
    product_course = _course("course_2", "vivy", "Ship it", entries=[
        _entry("i3", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
    ])
    weekly = waypoints.weekly_pace([builder_course, product_course], now=_NOW)
    week = next(w for w in weekly if w.total)
    assert week.by_group == {"Builder": 2, "vivy": 1}
    assert sum(week.by_group.values()) == week.total == 3


def test_weekly_pace_excludes_open_and_unresolved_entries():
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "open", resolved_at=_NOW - dt.timedelta(days=7)),  # not passed
        _entry("i2", "fixed"),  # passed, no resolved_at
    ])
    weekly = waypoints.weekly_pace([course], now=_NOW)
    assert weekly == []


def test_weekly_pace_returns_empty_list_with_no_passed_and_resolved_entries():
    course = _course("course_1", "vivy", "Ship it", entries=[_entry("i1", "open")])
    assert waypoints.weekly_pace([course], now=_NOW) == []


def test_weekly_pace_ignores_a_resolved_at_in_the_future():
    """Clock skew between this host and the Library could otherwise put a
    week past "now" into the series, breaking the one-partial-week rule."""
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
        _entry("i2", "fixed", resolved_at=_NOW + dt.timedelta(days=7)),
    ])
    weekly = waypoints.weekly_pace([course], now=_NOW)
    assert all(w.week <= waypoints._iso_week(_NOW) for w in weekly)
    assert sum(w.total for w in weekly) == 1


# --- unresolved_passed_count ------------------------------------------------


def test_unresolved_passed_count_counts_passed_entries_with_no_resolved_at():
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "fixed", resolved_at=_NOW),  # resolved — not counted
        _entry("i2", "fixed"),                    # passed, no resolved_at
        _entry("i3", "open"),                     # not passed at all
    ])
    assert waypoints.unresolved_passed_count([course]) == 1
