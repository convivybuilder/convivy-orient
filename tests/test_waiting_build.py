"""Tests for the Home "waiting on you" strip (ARCH-362 Phase 2).

The two properties worth defending here are not about the numbers. They are:

  1. The strip must never make Home slow — it reads the merge-queue cache
     opportunistically and must NEVER trigger the `gh` fan-out.
  2. The strip must never take Home down — a broken source degrades to one
     missing number, not a 500 on the landing page.

Both are the kind of thing that looks fine in review and only shows up in
production, so they are pinned directly rather than inferred from the render.
"""
from __future__ import annotations

import datetime as dt

import pytest

import control_db
import library_fake
import waiting_build as W


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A seeded temp control DB behind the Library fake.

    The vault tiles read through `library_client` now (Phase 3 §5 slice 3), so
    the tests that care about the numbers seed `control_db` the way every other
    builder test does and let the fake serve it back in the Library's own response
    shape (library_fake's docstring).
    """
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    library_fake.patch(monkeypatch)
    yield


@pytest.fixture
def no_library():
    """**THE STRIP NOW MAKES NO LIBRARY CALL AT ALL**, so this fixture asserts
    that rather than refusing one (issue_01M32CB6A9T04NVYPARGDT2GWQ).

    `list_unplanned_epics` was the last `library_client` call in
    `waiting_build.py`; it went with the tile, because approving an epic now
    places it on a Course in the same transaction and the Library removed the
    route. Corrections went earlier, and Alerts reads `alerts_build.gather()`,
    which reads local snapshots.

    Kept as a fixture, and kept on the tests that used it, because "no Library
    call" is the property those isolation tests were written around. A stub
    that quietly did nothing would let a Library call creep back in unnoticed;
    this fails the moment one does."""
    assert not hasattr(W, "library_client"), (
        "waiting_build imports library_client again — the strip is supposed to "
        "make no Library call. Restore the isolation this fixture used to give.")
    yield


# --- it must never fetch ------------------------------------------------------


def test_cleared_prs_returns_none_on_a_cold_cache_and_never_fetches(monkeypatch):
    """The whole reason `cached_open_prs` exists. Home is the most-requested page
    and the merge queue fans out one `gh` subprocess per repo at 20s timeout each,
    so a cold-cache fetch here would put a multi-second network call on the
    landing page. A missing number is the correct failure."""
    called = []
    monkeypatch.setattr(W.merge_queue_build, "cached_open_prs", lambda: None)
    monkeypatch.setattr(W.merge_queue_build, "_fetch_open_prs",
                        lambda *a, **k: called.append(1) or [])
    count, sub = W._cleared_prs()
    assert count is None
    assert not called, "the strip triggered a live gh fan-out — this is the slow-Home regression"


def test_gather_never_calls_the_live_fetch(no_library, monkeypatch):
    """Belt and braces at the gather() level, since a future edit could reach for
    _fetch_open_prs directly rather than through _cleared_prs."""
    called = []
    monkeypatch.setattr(W.merge_queue_build, "_fetch_open_prs",
                        lambda *a, **k: called.append(1) or [])
    monkeypatch.setattr(W.merge_queue_build, "cached_open_prs", lambda: None)
    W.gather()
    assert not called


def test_cleared_prs_counts_only_cleared_rows(monkeypatch):
    monkeypatch.setattr(W.merge_queue_build, "cached_open_prs",
                        lambda: [{"cleared": True}, {"cleared": False}, {"cleared": True}])
    count, sub = W._cleared_prs()
    assert count == 2 and "CI green" in sub


# --- a broken source must not take Home down ----------------------------------


def test_a_broken_vault_source_degrades_to_none_rather_than_raising(no_library,
                                                                    monkeypatch):
    """This renders on Home. A strip that can raise is strictly worse than no
    strip, so every source is isolated."""
    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(W.merge_queue_build, "cached_open_prs", boom)
    monkeypatch.setattr(W.alerts_build, "gather", boom)
    tiles = W.gather()
    # Two tiles since the unplanned-epics one went
    # (issue_01M32CB6A9T04NVYPARGDT2GWQ). Asserted against the real length
    # rather than a literal, so removing or adding a tile does not quietly
    # narrow what "every source is isolated" covers.
    assert len(tiles) == 2
    assert [t["count"] for t in tiles] == [None] * len(tiles)
    assert all(t["sub"] == "unavailable" for t in tiles)


def test_strip_html_survives_a_total_source_failure(no_library, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("everything is broken")

    monkeypatch.setattr(W.merge_queue_build, "cached_open_prs", boom)
    monkeypatch.setattr(W.alerts_build, "gather", boom)
    html = W.strip_html()
    assert 'aria-label="Waiting on you"' in html
    # One em dash per tile, counted off the real tile count for the reason the
    # sibling test above gives.
    assert html.count("—") == len(W.gather())


def test_strip_html_is_self_sealing_even_if_gather_itself_raises(monkeypatch):
    """gather() isolates its sources, so today nothing in strip_html can raise —
    but that is a property of the current body, not of the function. _full_body()
    calls strip_html() with no try/except of its own, so anything escaping here
    500s the LANDING PAGE. Break gather() outright and require Home-safe output.

    Empty is the correct degradation: it must not fall back to the all-clear line,
    which would assert "nothing is waiting on you" on the strength of a crash.
    """
    def boom(*a, **k):
        raise RuntimeError("gather itself is broken")

    monkeypatch.setattr(W, "gather", boom)
    html = W.strip_html()
    assert html == ""
    assert "Nothing is waiting" not in html


# --- unknown is not the same as clear -----------------------------------------


def test_unknown_counts_do_not_render_the_all_clear(monkeypatch):
    """The one failure that would make the strip untrustworthy: claiming
    "nothing is waiting on you" when we simply could not find out. An unknown
    count must never be treated as a zero."""
    monkeypatch.setattr(W, "gather", lambda now=None, viewer=None: [
        {"label": "Decisions", "href": "/d", "count": 0, "sub": ""},
        {"label": "Issues", "href": "/i", "count": 0, "sub": ""},
        {"label": "Ready to merge", "href": "/m", "count": None, "sub": "tap to load"},
        {"label": "Alerts", "href": "/a", "count": 0, "sub": ""},
    ])
    html = W.strip_html()
    assert "Nothing is waiting on you" not in html
    # The all-clear branch carries the same aria-label, so pin the non-clear
    # branch by its own markup rather than by the accessible name.
    assert '<section class="waiting" ' in html
    assert 'class="wtiles"' in html


def test_all_zero_renders_the_all_clear(monkeypatch):
    monkeypatch.setattr(W, "gather", lambda now=None, viewer=None: [
        {"label": x, "href": "/x", "count": 0, "sub": ""}
        for x in ("Decisions", "Issues", "Ready to merge", "Alerts")
    ])
    html = W.strip_html()
    assert "Nothing is waiting on you" in html


def test_a_zero_tile_is_rendered_not_hidden(monkeypatch):
    """A zero tile is muted, not dropped. Hiding it would change the strip's
    shape under the operator, and "Decisions: 0" is information — it says the queue was
    checked and is empty, which an absent tile does not say."""
    monkeypatch.setattr(W, "gather", lambda now=None, viewer=None: [
        {"label": "Decisions", "href": "/d", "count": 0, "sub": ""},
        {"label": "Issues", "href": "/i", "count": 3, "sub": "oldest 2d"},
        {"label": "Ready to merge", "href": "/m", "count": 0, "sub": ""},
        {"label": "Alerts", "href": "/a", "count": 0, "sub": ""},
    ])
    html = W.strip_html()
    assert html.count("wtile") >= 4
    assert "Decisions" in html and "zero" in html


# --- escaping -----------------------------------------------------------------


def test_tile_fields_are_escaped():
    html = W._tile_html({"label": '<img src=x onerror=alert(1)>', "href": '/x"onclick="evil',
                         "count": 1, "sub": "<script>"})
    assert "<img" not in html and "<script>" not in html
    assert 'onclick="evil' not in html


def test_tile_fields_are_escaped_on_the_hot_path_too(monkeypatch):
    """The hot path adds an aria-label that interpolates the label into an HTML
    ATTRIBUTE — a boundary the plain path does not have. The escaping test above
    never reached it, because its tile has no `urgent` key."""
    html = W._tile_html({"label": 'A" onmouseover="evil', "href": "/a",
                         "count": 3, "sub": "unacknowledged", "urgent": True})
    assert "wtile hot" in html
    assert 'onmouseover="evil' not in html
    assert "&quot;" in html


def test_a_bool_count_never_goes_hot():
    """bool subclasses int, so isinstance(True, int) is True — a count of True
    would have gone hot and rendered the word "True" as the number. No
    production path produces one, but the guard is described as load-bearing."""
    html = W._tile_html({"label": "Alerts", "href": "/a", "count": True,
                         "sub": "", "urgent": True})
    assert "hot" not in html


# --- tier boundary ------------------------------------------------------------


def test_only_the_full_tier_home_renders_the_strip():
    """The strip is the operator's cockpit: pending decisions, aging issues and unmerged
    PRs are internal operational state a contractor must not see.

    It is reached only from `_full_body`, but the CSS ships on both homes (one
    stylesheet, so the two tiers cannot drift into per-tier CSS). So assert on
    the rendered ELEMENT, not on a class name — the naive `"wtile" in html`
    check passes on the contractor page purely because of the stylesheet.
    """
    import home_build

    contractor = home_build.live_html("contractor", "")
    full = home_build.live_html("full", "The operator")
    assert '<section class="waiting' in full
    assert '<section class="waiting' not in contractor
    # ...and the tiles' destinations must not leak into the contractor page either.
    assert "/engineering/decisions" not in contractor


# --- age formatting -----------------------------------------------------------


def test_age_short_buckets():
    from datetime import timedelta

    from models import utcnow
    now = utcnow()
    assert W._age_short(now - timedelta(minutes=5), now) == "5m"
    assert W._age_short(now - timedelta(hours=3), now) == "3h"
    assert W._age_short(now - timedelta(days=2), now) == "2d"
    assert W._age_short(None, now) == "—"


# --- the urgent (hot) tile ----------------------------------------------------
#
# The operator: "it would be nice if it stood out when a new unacknowledged alert is
# present." They had 14 unacknowledged alerts sitting in the strip and missed
# them, because a 14 tile and a 0 tile were styled identically. An alert is the
# fallback for a push notification they did not catch in time, so "something fired
# and you have not seen it" has to read at a glance.


def _tiles(alerts_count, alerts_sub="unacknowledged", urgent=True):
    return [
        {"label": "Decisions", "href": "/d", "count": 0, "sub": ""},
        {"label": "Issues", "href": "/i", "count": 0, "sub": ""},
        {"label": "Ready to merge", "href": "/m", "count": 0, "sub": ""},
        {"label": "Alerts", "href": "/a", "count": alerts_count,
         "sub": alerts_sub, "urgent": urgent},
    ]


def test_a_non_zero_alerts_tile_goes_hot(monkeypatch):
    monkeypatch.setattr(W, "gather", lambda now=None, viewer=None: _tiles(14))
    html = W.strip_html()
    assert "wtile hot" in html


def test_a_zero_alerts_tile_does_not_go_hot(monkeypatch):
    monkeypatch.setattr(W, "gather", lambda now=None, viewer=None: _tiles(0, ""))
    html = W.strip_html()
    assert "hot" not in html


def test_an_unknown_alerts_count_does_not_go_hot(monkeypatch):
    """The same rule the all-clear follows, in the other direction: "we could
    not read the queue" is not "you have alerts". Crying wolf on missing data is
    how the strip stops being believed."""
    monkeypatch.setattr(W, "gather",
                        lambda now=None, viewer=None: _tiles(None, "unavailable"))
    html = W.strip_html()
    assert "hot" not in html
    assert "—" in html


def test_a_non_urgent_tile_never_goes_hot(monkeypatch):
    """Only tiles that opt in are styled. Decisions and Issues are deliberately
    left plain for now — this change was about alerts, not a restyle of the
    whole strip."""
    monkeypatch.setattr(W, "gather", lambda now=None, viewer=None: [
        {"label": "Decisions", "href": "/d", "count": 9, "sub": "oldest 2d"},
        {"label": "Alerts", "href": "/a", "count": 0, "sub": "", "urgent": True},
    ])
    html = W.strip_html()
    assert "hot" not in html


def test_hot_state_is_not_conveyed_by_colour_alone(monkeypatch):
    """A colour-only signal is invisible to a screen reader and to anyone who
    cannot distinguish it. The count, the sub-label, and the accessible name all
    carry the same state."""
    monkeypatch.setattr(W, "gather", lambda now=None, viewer=None: _tiles(14))
    html = W.strip_html()
    assert "needing attention" in html
    assert "unacknowledged" in html
    assert ">14<" in html


def _seed_issue(**kw):
    base = dict(id=control_db.new_issue_id(), product="vivy", title="t", body="b",
                type="bug", state="open", created_by="agent")
    base.update(kw)
    library_fake.seed_issue(**base)
    return base["id"]


# --- Corrections is gone (issue_01M2F3QFTVZEFVTF0QJ5C7W7NC) -------------------
#
# The Library retired the `awaiting-decision` state crews used to leave a
# correction in for the operator; corrections are now worked without the operator, so a
# tile that reported "N need you" against that state was reporting operator
# work that no longer exists. `/engineering/corrections` still shows what is
# open — this only removes the strip's claim that some of it waits on the operator.


def test_strip_has_no_corrections_tile_even_with_open_corrections(db):
    """Plant the old tile back (`_vault_tile("Corrections", ...)` in `gather()`)
    and this fails: with corrections open, the old tile read "3 active, none
    need you" and would appear here."""
    for _ in range(3):
        _seed_issue(state="open")
    tiles = {t["label"] for t in W.gather()}
    assert "Corrections" not in tiles
    html = W.strip_html()
    assert "Corrections" not in html
    assert "need you" not in html


# --- the unplanned-epics tile is GONE (issue_01M32CB6A9T04NVYPARGDT2GWQ) -----
# It counted approved epics on no live Course, from `GET /courses/unplanned`.
# Approving now places an epic on its Course in the same Library transaction,
# so the state cannot occur, the Library removed the route, and the tile went
# with it. Its five tests went too; what replaces them is the assertion below
# that the tile is not rendered, plus the fixture's check that the strip makes
# no Library call at all.


def test_the_strip_carries_no_unplanned_epics_tile(no_library):
    """A tile reading a route that 404s rendered "unavailable" forever between
    the Library's landing and this one. Asserted on the RENDERED LABELS rather
    than on the absence of a function, so re-adding the tile under any name
    that mentions it fails here."""
    labels = [t["label"] for t in W.gather()]
    assert labels == ["Ready to merge", "Alerts"], labels
    assert not any("nplanned" in ln for ln in labels)


# --- the alerts tile, now sourced from alerts_build.gather() -----------------
# (issue_01M2F4MGD7FMZ9PWM21MY70AAR) — the tile used to count recent
# alert-*kind* signals in the store, a different number from what /alerts and
# the Home quick-link pill both show (policy-threshold crossings from
# `alerts_build.gather()`), so one page carried two disagreeing alert counts.


def _seed_alert(body, age, **kw):
    base = dict(id=control_db.new_signal_id(), source="healthcheck", kind="alert",
                actor="cron", ingested_by="cron", product="vivy", body=body,
                ingested_at=control_db.utcnow() - age)
    base.update(kw)
    library_fake.seed_signal(**base)
    return base["id"]


def _fake_threshold_alerts(n, severity="bad"):
    return [{"category": "Code Health", "metric": "CI pass rate (main)",
             "subject": f"repo{i}", "severity": severity, "value": "50%",
             "detail": "d", "href": "/health"} for i in range(n)]


def test_alerts_tile_counts_alerts_build_gather_not_signal_store_rows(db, monkeypatch):
    """Seed a different number of alert-kind signals than threshold alerts, so
    the two sources can't agree by coincidence. Plant the old
    `_unacked_alerts`-style wiring back into `gather()` and this fails, since
    that would count the 3 signals instead of the 5 threshold alerts."""
    for i in range(3):
        _seed_alert(f"noise {i}", dt.timedelta(hours=1))
    monkeypatch.setattr(W.alerts_build, "gather", lambda: _fake_threshold_alerts(5))

    assert W._active_alerts(W.utcnow())[0] == 5
    tiles = {t["label"]: t for t in W.gather()}
    assert tiles["Alerts"]["count"] == 5
    assert tiles["Alerts"]["href"] == "/alerts"


def test_alerts_tile_is_unavailable_when_alerts_build_gather_raises(monkeypatch):
    """A source that fails to load must read as unknown, never as zero — the
    same self-sealing contract every other tile holds (`strip_html`'s
    docstring)."""
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(W.alerts_build, "gather", boom)
    tiles = {t["label"]: t for t in W.gather()}
    assert tiles["Alerts"]["count"] is None
    assert tiles["Alerts"]["sub"] == "unavailable"


def test_alerts_tile_is_zero_when_no_threshold_is_crossed(monkeypatch):
    monkeypatch.setattr(W.alerts_build, "gather", lambda: [])
    assert W._active_alerts(W.utcnow()) == (0, "")
# --- the warmer must not undo the never-fetch-on-Home property ----------------


def test_warmer_runs_the_fetch_off_the_event_loop():
    """The warm loop must hand the fetch to a thread, never await it inline.

    The fetch is blocking subprocess work — one `gh` per repo at 20s timeout. Run
    on the event loop it would stall EVERY request for its duration, which is the
    exact slowness the cache-only tile exists to avoid; the warmer would then have
    reintroduced the problem it was written to fix, on a 150s timer.

    Asserted against the source rather than by timing, because a timing test for
    this is flaky and the property is structural. Known trade-off: wrapping the
    call in a helper would keep the runtime property while making this assertion
    pass vacuously, so if that refactor happens this test has to move with it.
    """
    import inspect

    import server

    src = inspect.getsource(server._warm_merge_queue_cache)
    assert "asyncio.to_thread" in src, (
        "the warmer must dispatch the blocking fetch via asyncio.to_thread")
    assert "await merge_queue_build._fetch_open_prs" not in src


def test_warmer_survives_a_failing_fetch(monkeypatch):
    """A warm miss degrades the tile to cold; it must never take the server down.

    Cold is what Home ships today, so a dead warmer is a no-op rather than a
    regression — but only if the exception is swallowed and the loop keeps
    turning. Asserted by observing the task is still alive after a raising fetch,
    rather than by reading the source for an except clause.
    """
    import asyncio
    import contextlib

    import server

    calls = []

    def _boom(*_a, **_k):
        calls.append(1)
        raise RuntimeError("gh exploded")

    monkeypatch.setattr(server.merge_queue_build, "_fetch_open_prs", _boom)
    monkeypatch.setattr(server, "_WARM_EVERY", 0.01)

    async def _run():
        task = asyncio.create_task(server._warm_merge_queue_cache())
        await asyncio.sleep(0.15)
        alive = not task.done()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return alive

    alive = asyncio.run(_run())
    assert calls, "the warmer never called the fetch"
    assert alive, "a raising fetch killed the warm loop instead of being swallowed"


def test_lifespan_does_not_warm_unless_serving(monkeypatch):
    """TestClient runs the lifespan too, so the warmer must be OFF by default.

    Without this gate every TestClient test starts the warm loop, which fans out
    one real `gh` subprocess per repo at 20s timeout each — it hung this repo's
    suite at 34% when the warmer was first written. The gate is what keeps a
    serving concern out of every other test in the project.
    """
    import asyncio

    import server

    started = []
    monkeypatch.setattr(server, "_warm_merge_queue_cache",
                        lambda: started.append(1) or asyncio.sleep(0))

    async def _run(env):
        monkeypatch.delenv(server._WARM_ENV, raising=False)
        if env is not None:
            monkeypatch.setenv(server._WARM_ENV, env)
        async with server._lifespan(server.app):
            pass

    asyncio.run(_run(None))
    assert not started, "the warmer started with no env set — TestClient would fan out gh"
    asyncio.run(_run("0"))
    assert not started, "the warmer started when explicitly disabled"
    asyncio.run(_run("1"))
    assert started, "the warmer did not start when serving"


def test_warmer_backs_off_on_repeated_failure(monkeypatch):
    """Repeated failure must widen the interval, not hammer it.

    The fetch is all GraphQL (`gh search prs` plus one `gh pr list` per repo) and
    that budget is exhaustible — a heavy review day burned the full 5000/hour
    allowance on 2026-07-20. Retrying every 150s through a rate-limit window
    spends budget the interactive work needs and logs a line each time, while
    achieving nothing: the window is an hour wide.
    """
    import asyncio
    import contextlib

    import server

    sleeps = []

    async def _fake_sleep(d):
        sleeps.append(d)
        if len(sleeps) >= 4:
            raise asyncio.CancelledError

    def _boom(*_a, **_k):
        raise RuntimeError("rate limit")

    monkeypatch.setattr(server.merge_queue_build, "_fetch_open_prs", _boom)
    monkeypatch.setattr(server.asyncio, "sleep", _fake_sleep)

    async def _run():
        with contextlib.suppress(asyncio.CancelledError):
            await server._warm_merge_queue_cache()

    asyncio.run(_run())
    assert sleeps == sorted(sleeps) and sleeps[0] < sleeps[-1], (
        f"the interval did not widen under repeated failure: {sleeps}")
    assert max(sleeps) <= server._WARM_MAX_BACKOFF


def test_warmer_resets_the_interval_after_a_success(monkeypatch):
    """A recovered fetch returns to the normal cadence rather than staying slow."""
    import asyncio
    import contextlib

    import server

    sleeps = []
    outcomes = [RuntimeError("x"), RuntimeError("x"), None, None]

    async def _fake_sleep(d):
        sleeps.append(d)
        if len(sleeps) >= len(outcomes):
            raise asyncio.CancelledError

    def _fetch(*_a, **_k):
        r = outcomes[len(sleeps)]
        if isinstance(r, Exception):
            raise r

    monkeypatch.setattr(server.merge_queue_build, "_fetch_open_prs", _fetch)
    monkeypatch.setattr(server.asyncio, "sleep", _fake_sleep)

    async def _run():
        with contextlib.suppress(asyncio.CancelledError):
            await server._warm_merge_queue_cache()

    asyncio.run(_run())
    assert sleeps[-1] == server._WARM_EVERY, (
        f"a success did not reset the interval: {sleeps}")


def test_the_strip_has_no_visible_heading(monkeypatch):
    """The `<h2>Waiting on you</h2>` is gone and the `aria-label` is not.

    the operator, 2026-09-18: "the tiles on the front page are under a label 'waiting
    for you' which should be removed". The heading sat at the top of
    `section.waiting`, directly above `.wtiles` — so the tiles it labelled were
    this strip's own. `home_build`'s hero-stats are the next sibling of the
    whole section, below that row, and were never under the heading.

    Asserted on the markup rather than on the words, because the accessible
    name still contains them: the two assertions above this file used to read
    `"Waiting on you" in html`, which the `aria-label` satisfies on its own, so
    they passed whether the heading was rendered or not.
    """
    monkeypatch.setattr(W, "gather", lambda *a, **k: [
        {"label": "Alerts", "href": "/alerts", "count": 3, "sub": ""},
    ])
    html = W.strip_html()
    assert "<h2" not in html
    assert "wtitle" not in html
    assert "wtitle" not in W.WAITING_CSS
    assert 'aria-label="Waiting on you"' in html
