#!/usr/bin/env python3.11
"""Waypoint pace — shared by home_build/overview_build's velocity stat and
velocity_build's /velocity page, so the two surfaces can't disagree about
what "passed" means (dashboards epic issue_01M2F3Q1B8J9E0WC5J1F1CMWA1, item
issue_01M2F3QV06RFYTQSFS0MJ4F7PF).

A waypoint is an epic seen from its crew's Course, passed when the epic's
state is terminal (the operator, 2026-08-07). `chart_house_build._step_class` already
classifies a Course step by that same terminal test; `is_passed` below is
that test, factored out here so chart_house_build and this module share one
definition of "passed" rather than keeping two that could drift.

Nothing here is stored in Orient's control database — `course_progress()`
reads Courses fresh from `library_client` (the Library) on every call, and
raises on a `list_courses` failure rather than degrading to an empty list, the
same rule every other `library_client` read in this codebase follows: a
caller must be able to tell a Library outage from a genuinely empty Course,
so it is this module's callers that render "unavailable" on the exception,
never this module.

**Windowed pace (waypoints passed in the last 7/30/90 days)** uses each
entry's `resolved_at` — `library_client.CourseEntry.resolved_at`, resolved by
`get_course` from the same `store.item_dict` payload `IssueRecord.resolved_at`
already parses (the Library just wasn't asked for it here before). A passed
entry with `resolved_at` of `None` — a `get_course` detail call that failed
and fell back to its unresolved `list_courses` record — still counts as
passed for `total`/`passed`/`remaining`, but counts in no window: there is no
transition time to test against one, and guessing one would misreport a
pace this module can't actually measure.

**Weekly pace** (`weekly_pace`) buckets the same passed-and-resolved entries
by ISO week for /velocity's weekly chart (issue_01M2HXZKHKZFVZK9K1MJ5KTKTA).
It shares `fetch_courses()`'s single Library round trip with `course_progress`
— a caller needing both computes them from one fetch, not two, so the chart
costs the page no additional Library calls. A week between the earliest
passed-and-resolved entry and the current week is always emitted, zero when
nothing passed, so a quiet week reads as a flat mark rather than a gap in the
line. The current week is marked `partial`: it is still in progress, so its
often-lower count must not read as a fall in pace. Each week's `by_group`
splits Builder's own crews (`BUILDER_CREWS` — Steward, Library, Bosun,
Orient, Deck, Shipwright; the operator, 2026-09-15) from the products built with
Builder, so the platform's own pace reads beside its products' rather than
folded into one number."""
from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field

from control_db import ISSUE_TERMINAL
from logutil import clean_log_field
from models import utcnow

import library_client

# The three windows every caller of this module reports pace over (home page
# hero + /overview card show WINDOWS[0]; /velocity shows all three).
WINDOWS = (7, 30, 90)


def is_passed(entry: library_client.CourseEntry) -> bool:
    """A waypoint is passed once its epic's state is terminal — the same
    test `chart_house_build._step_class` uses for a step's "done" track
    colour. An entry with no `state` resolved (a `get_course` detail call
    that failed and fell back to its `list_courses` record) reads as not
    passed, the conservative side of that failure."""
    return entry.state in ISSUE_TERMINAL


def _passed_within(entry: library_client.CourseEntry, days: int, now: dt.datetime) -> bool:
    """Whether `entry` is a waypoint passed within the trailing `days` —
    passed, with a resolved time no older than `days` (and not in the
    future, which a clock skew between this host and the Library could
    otherwise count). An entry with no `resolved_at` is passed but
    unattributable to any window (see module docstring)."""
    if not is_passed(entry) or entry.resolved_at is None:
        return False
    age = now - entry.resolved_at
    return dt.timedelta(0) <= age <= dt.timedelta(days=days)


# Builder's own crews (the operator, 2026-09-15: "we can also define Builder by its
# crews, and your list of crews would be correct for it"). The product's three
# parts are the Keel — Library and Bosun — plus Orient and Deck; the steward
# and shipwright are adjunct helpers, the steward user-facing and shipwright
# helping deliver. Whether the steward is also a *part* is unsettled and
# nothing here depends on it: this set groups CREWS, which is the axis the operator
# confirmed. Everything else in a Course's `product` is a product built with
# Builder (vivy, selector, placard, ...), not named here since that set grows
# without this module changing.
BUILDER_CREWS = frozenset({
    "steward", "library", "bosun", "convivy-orient", "convivy-deck", "shipwright",
})


def _group_for(product: str) -> str:
    """Collapse Builder's six crews to one "Builder" line in a weekly
    breakdown — the operator, 2026-09-15: label the group "Builder" rather than
    naming six crews in a legend. A product keeps its own name."""
    return "Builder" if product in BUILDER_CREWS else product


@dataclass
class CourseProgress:
    """One product's live Course, reduced to waypoint pace."""

    product: str
    title: str
    total: int
    passed: int
    remaining: int
    in_flight: str | None  # title (falling back to id) of the first open entry
    # Waypoints passed within each of WINDOWS' trailing windows, keyed by
    # window length in days — e.g. {7: 1, 30: 3, 90: 3}.
    passed_within: dict[int, int] = field(default_factory=dict)


def total_within(rows: list[CourseProgress], days: int) -> int:
    """Waypoints passed within the trailing `days`, summed across every
    product in `rows` — the total `home_build`'s hero stat and /velocity's
    headline both show for a given window."""
    return sum(r.passed_within.get(days, 0) for r in rows)


def _log_swallowed(what: str, ex: BaseException) -> None:
    """Print a swallowed per-course failure to stderr — same discipline as
    `chart_house_build._log_swallowed`, which this mirrors rather than
    imports, since chart_house_build is a page builder and this module is
    beneath it in the dependency order (chart_house_build imports this
    module's `is_passed`, not the other way around)."""
    try:
        print(clean_log_field(f"waypoints: {what} — {type(ex).__name__}: {ex}"),
              file=sys.stderr)
    except Exception:
        pass


def _progress(course: library_client.CourseRecord, now: dt.datetime) -> CourseProgress:
    entries = course.entries
    passed = sum(1 for e in entries if is_passed(e))
    in_flight = next((e.title or e.issue_id for e in entries if not is_passed(e)), None)
    passed_within = {days: sum(1 for e in entries if _passed_within(e, days, now))
                      for days in WINDOWS}
    return CourseProgress(
        product=course.product, title=course.title, total=len(entries),
        passed=passed, remaining=len(entries) - passed, in_flight=in_flight,
        passed_within=passed_within,
    )


def fetch_courses() -> list[library_client.CourseRecord]:
    """Every live Course, product-sorted, entries resolved via `get_course`
    where possible — the one Library round trip behind both `course_progress`
    and `weekly_pace`, so a caller needing both (the /velocity page) fetches
    once rather than twice.

    Raises on a `list_courses` failure, per this module's own docstring. A
    single course's `get_course` detail failure degrades to that course's
    `list_courses` record instead — `chart_house_build._course_rows`'s own
    discipline, so one slow or broken course cannot take the whole read down
    with it. A degraded course's entries carry no `resolved_at` either, so
    they contribute to `passed`/`total` but to no window and no week."""
    courses = sorted(library_client.list_courses(status="live"), key=lambda c: c.product)
    out = []
    for course in courses:
        try:
            detail = library_client.get_course(course.id) or course
        except Exception as ex:
            _log_swallowed(f"course {course.id} detail unavailable", ex)
            detail = course
        out.append(detail)
    return out


def course_progress(courses: list[library_client.CourseRecord] | None = None) -> list[CourseProgress]:
    """One `CourseProgress` per live Course, product-sorted. A product with
    no live Course simply has no entry in this list — not an error, and not
    rendered as a zero.

    Pass `courses` (from `fetch_courses`) to compute this from an already-
    fetched read; omitted, this fetches fresh itself — every existing caller
    (the front page hero, /overview's card, /project pages) relies on that
    no-argument shape and takes it unchanged.

    Raises on a `list_courses` failure, per this module's own docstring
    (via `fetch_courses` when `courses` is omitted)."""
    now = utcnow()
    if courses is None:
        courses = fetch_courses()
    return [_progress(course, now) for course in courses]


def unresolved_passed_count(courses: list[library_client.CourseRecord]) -> int:
    """Passed entries with no `resolved_at` — a degraded `get_course` detail
    call, or an issue resolved before the Library carried the field. Counted
    in `course_progress`'s `passed` total, in no `weekly_pace` week; a caller
    shows this count so that omission reads as accounted-for, not lost."""
    return sum(1 for course in courses for entry in course.entries
               if is_passed(entry) and entry.resolved_at is None)


def _iso_week(ts: dt.datetime) -> str:
    """`ts`'s ISO week as e.g. "2026-W34" — the key `weekly_pace` buckets
    on, matching the format the operator's measured series in the item comments
    already uses."""
    iso = ts.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _week_range(start: str, end: str) -> list[str]:
    """Every ISO week from `start` to `end` inclusive, oldest first — the
    scaffold `weekly_pace` fills with zeros so a quiet week is a mark, not a
    gap. Walks by calendar date (not by week-number arithmetic) so a run
    that crosses a year boundary — week 52 or 53 into week 1 — lands right."""
    def parse(week: str) -> tuple[int, int]:
        year, wk = week.split("-W")
        return int(year), int(wk)

    y0, w0 = parse(start)
    y1, w1 = parse(end)
    day = dt.date.fromisocalendar(y0, w0, 1)
    last_day = dt.date.fromisocalendar(y1, w1, 1)
    weeks = []
    while day <= last_day:
        iso = day.isocalendar()
        weeks.append(f"{iso.year}-W{iso.week:02d}")
        day += dt.timedelta(days=7)
    return weeks


@dataclass
class WeeklyPace:
    """One week's waypoint pace: the total passed that week and the same
    total split by `_group_for` group. `partial` marks the current,
    still-in-progress week — see the module docstring."""

    week: str
    total: int
    partial: bool
    by_group: dict[str, int]


def weekly_pace(courses: list[library_client.CourseRecord],
                 now: dt.datetime | None = None) -> list[WeeklyPace]:
    """One `WeeklyPace` per ISO week from the earliest passed-and-resolved
    waypoint through the current week, inclusive. `courses` should be
    `fetch_courses()`'s result — this makes no Library call of its own.

    Returns `[]` when no passed entry carries a `resolved_at` (no live
    Course yet, or every passed entry degraded — see `fetch_courses`); a
    caller renders that the same way it renders no live Courses at all."""
    if now is None:
        now = utcnow()
    by_week: dict[str, dict[str, int]] = {}
    for course in courses:
        group = _group_for(course.product)
        for entry in course.entries:
            if not is_passed(entry) or entry.resolved_at is None:
                continue
            if entry.resolved_at > now:
                continue  # clock skew — see _passed_within's own guard
            week = _iso_week(entry.resolved_at)
            bucket = by_week.setdefault(week, {})
            bucket[group] = bucket.get(group, 0) + 1
    if not by_week:
        return []
    current_week = _iso_week(now)
    earliest_week = min(by_week)
    all_weeks = _week_range(earliest_week, max(earliest_week, current_week))
    return [
        WeeklyPace(week=week, total=sum(by_week.get(week, {}).values()),
                   partial=(week == current_week), by_group=by_week.get(week, {}))
        for week in all_weeks
    ]
