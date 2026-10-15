"""Render tests for /engineering/e2e (ARCH-353 Phase A).

Load-bearing: the latest tile per (product, suite) is genuinely the latest
(newest-first scan, first occurrence wins), a malformed/missing status
degrades to "unknown" rather than blanking the page, the run link honors the
same http(s)-only allowlist every renderer uses, untrusted CI-runner text is
escaped, and the source=ci scan reaches past a single Library page (MAX_PAGE)
when the live scan window (_SCAN_LIMIT=400) needs to.
"""
from __future__ import annotations

import datetime as dt

import pytest

import control_db
import engineering_e2e_build as e2e
import library_client
import library_fake


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    # engineering_e2e_build now reads through library_client rather than
    # control_db directly; this fake serves the Library's exact response shape
    # from the same temp DB every test already seeds (library_fake's docstring).
    library_fake.patch(monkeypatch)
    yield


def _mk(**kw):
    base = dict(id=control_db.new_signal_id(), source="ci", kind="test-result",
                actor="ci-bot", ingested_by="ci-bot", product="vivy",
                stream="e2e-stage-smoke", body="", meta={"status": "pass"},
                privacy_class="internal")
    base.update(kw)
    return base


def _seed(*sigs):
    for sig in sigs:
        library_fake.seed_signal(**sig)


def test_empty_db_renders_empty_state(db):
    html = e2e.live_html()
    assert "No CI results captured yet" in html


def test_latest_tile_is_the_newest_run_for_the_suite(db):
    old = dt.datetime(2026, 7, 1)
    new = dt.datetime(2026, 7, 16)
    _seed(_mk(meta={"status": "fail"}, body="OLD-RUN", ingested_at=old),
          _mk(meta={"status": "pass"}, body="NEW-RUN", ingested_at=new))
    html = e2e.live_html()
    assert "NEW-RUN" in html
    assert "OLD-RUN" not in html   # only the history row, not a tile, holds the old run
    assert '<div class="verdict">PASS</div>' in html


def test_distinct_suites_each_get_their_own_tile(db):
    _seed(_mk(stream="e2e-stage-smoke", body="SMOKE-RUN"),
          _mk(stream="e2e-full", body="FULL-RUN"),
          _mk(product="constance", stream="e2e-stage-smoke", body="CONSTANCE-RUN"))
    html = e2e.live_html()
    assert "SMOKE-RUN" in html and "FULL-RUN" in html and "CONSTANCE-RUN" in html


def test_malformed_status_degrades_to_unknown(db):
    _seed(_mk(meta={"status": "explosion"}, body="WEIRD-STATUS"))
    html = e2e.live_html()
    assert '<div class="verdict">UNKNOWN</div>' in html
    assert 'class="tile unknown"' in html


def test_missing_meta_does_not_blank_the_page(db):
    _seed(_mk(meta={}, body="NO-META"))
    html = e2e.live_html()
    assert "NO-META" in html
    assert '<div class="verdict">UNKNOWN</div>' in html


def test_run_link_allows_http_and_rejects_non_http(db):
    _seed(_mk(stream="linked", meta={"status": "pass", "run_url": "https://ci.example/run/1",
                                     "run_number": 42}),
          _mk(stream="unlinked", meta={"status": "pass",
                                       "run_url": "javascript:alert(1)", "run_number": 7}))
    html = e2e.live_html()
    assert 'href="https://ci.example/run/1"' in html
    assert 'href="javascript:' not in html
    assert "Run #7" in html   # the label still renders even though it does not link


def test_untrusted_ci_text_is_escaped(db):
    _seed(_mk(product='<img src=x onerror=1>', stream='<svg onload=1>',
              body="<script>alert(1)</script>",
              meta={"status": "pass", "jobs": {"<b>job</b>": "success"}}))
    html = e2e.live_html()
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x onerror=1>" not in html
    assert "<svg onload=1>" not in html
    assert "<b>job</b>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_jobs_chips_render_ok_and_bad(db):
    _seed(_mk(meta={"status": "fail", "jobs": {"unit": "success", "lint": "failure"}}))
    html = e2e.live_html()
    assert 'class="job ok">unit</span>' in html
    assert 'class="job bad">lint</span>' in html


def test_history_lists_newest_first_up_to_the_limit(db):
    # One shared suite, so there is exactly one tile (the latest run) and every
    # other row's only chance to appear is the history section — isolating what
    # this test is actually about.
    base = dt.datetime(2026, 7, 20)
    total = e2e._HISTORY_LIMIT + 5
    sigs = [_mk(meta={"status": "pass", "run_number": i},
               ingested_at=base - dt.timedelta(minutes=i))
            for i in range(total)]   # i=0 is newest
    _seed(*sigs)
    html = e2e.live_html()
    for i in range(e2e._HISTORY_LIMIT):
        assert f"Run #{i}" in html
    for i in range(e2e._HISTORY_LIMIT, total):
        assert f"Run #{i}" not in html


def test_static_fallback_is_empty_state(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e, "SITE", tmp_path / "site")
    e2e.build_static()
    out = (tmp_path / "site" / "engineering-e2e.html").read_text()
    assert "Live view unavailable" in out


def test_route_registered_full_tier():
    """/engineering/e2e moved out of `_full_only_routes()` into
    `server.SCOPED_ROUTES` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): that
    dispatcher calls its renderer with no arguments, and this page now needs
    the viewer's scope. `tests/test_server.py`'s
    `_MOVED_OUT_OF_FULL_ONLY_ROUTES` carries the full-tier/nav/contractor-leak
    coverage this move must not lose."""
    import server
    assert "/engineering/e2e" in server.SCOPED_ROUTES
    assert "/engineering/e2e" not in server._full_only_routes()


def test_nav_marks_current_page(db):
    import design_system as ds
    html = ds.nav_component("/engineering/e2e", "full")
    # E2E moved into the Dashboards area (epic issue_01M0EHQHQ7M39K811XKX1BAG98)
    # and left the sub-strip in the 2026-08-24 trim to the operator's pinned five
    # (Overview/Spending/Velocity/Security/Alerts) — it keeps its route and
    # full-tier gating, still resolving to the Dashboards primary, but has no
    # sub-strip link of its own now that the grouped Overview index
    # (nav-reshape spec §4) carries it instead.
    assert 'aria-label="Dashboards navigation"' in html
    assert 'href="/overview" aria-current="page"' in html
    assert 'href="/engineering/e2e"' not in html


# --- the scan window reaches past a single Library page ----------------------
#
# MAX_PAGE clamps a single `GET /signals` response to 200 rows; _SCAN_LIMIT is
# 400, twice that. A regression that capped the scan at (or shrank it to)
# MAX_PAGE would silently drop the tile for any suite whose only signals are
# older than the 200 most recent CI rows — the exact failure this pins.


def test_scan_reaches_past_a_single_library_page(db):
    base = dt.datetime(2026, 8, 1, 12, 0, 0)
    # 200 recent "main" runs, newest first, all older signals pushed further back
    recent = [_mk(stream="main", body=f"MAIN-{i}",
                  ingested_at=base - dt.timedelta(minutes=i))
              for i in range(200)]
    # a suite whose only signal sits strictly PAST the 200-row mark
    buried = _mk(stream="RARE-SUITE-BEYOND-200", body="BURIED-RUN",
                 ingested_at=base - dt.timedelta(minutes=225))
    _seed(*recent, buried)
    html = e2e.live_html()
    assert "RARE-SUITE-BEYOND-200" in html, (
        "a signal past the first 200 rows (MAX_PAGE) never reached the page — "
        "the scan window is not actually 400")


def test_list_signals_cap_exceeds_max_page(db):
    """The client call itself: engineering_e2e_build must ask for more than
    MAX_PAGE (200) rows, or the fake (and, over the wire, the real Library's
    pagination) would never be exercised past a single page."""
    assert e2e._SCAN_LIMIT > library_client.MAX_PAGE


# --- the stalled banner: a live feed that went quiet, said plainly ----------
#
# The worst case the epic's reconciliation found: this page renders whatever
# the Library holds and says nothing when the feed behind it dies, so six
# days of silence looked identical to a quiet week.


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def test_stalled_banner_fires_past_the_cadence_threshold(db):
    stale_at = _now() - dt.timedelta(hours=e2e._STALLED_AFTER_HOURS + 5)
    _seed(_mk(meta={"status": "pass"}, body="OLD-BUT-STILL-A-TILE",
              ingested_at=stale_at))
    html = e2e.live_html()
    assert 'class="stalled-banner"' in html
    assert 'role="status"' in html
    assert "This feed expects a result daily." in html


def test_stalled_banner_does_not_fire_within_the_cadence_threshold(db):
    fresh_at = _now() - dt.timedelta(hours=e2e._STALLED_AFTER_HOURS - 5)
    _seed(_mk(meta={"status": "pass"}, body="RECENT-RUN", ingested_at=fresh_at))
    html = e2e.live_html()
    assert 'class="stalled-banner"' not in html


def test_stalled_banner_renders_above_the_tiles_which_keep_rendering(db):
    """A banner above real data, never instead of it — the tiles must still
    render underneath."""
    stale_at = _now() - dt.timedelta(hours=e2e._STALLED_AFTER_HOURS + 5)
    _seed(_mk(meta={"status": "pass"}, body="STILL-A-TILE", ingested_at=stale_at))
    html = e2e.live_html()
    assert html.index('class="stalled-banner"') < html.index("STILL-A-TILE")
    assert '<div class="verdict">PASS</div>' in html


def test_stalled_banner_prefers_produced_at_over_ingested_at(db):
    """A backfilled row can land (ingested) recently while describing a run
    from long ago (produced) — the run time is what staleness is about."""
    stale_produced = _now() - dt.timedelta(hours=e2e._STALLED_AFTER_HOURS + 10)
    _seed(_mk(meta={"status": "pass"}, body="BACKFILLED",
              produced_at=stale_produced, ingested_at=_now()))
    html = e2e.live_html()
    assert 'class="stalled-banner"' in html


def test_static_fallback_has_no_stalled_banner(tmp_path, monkeypatch):
    """The static fallback has no signals at all, so there is nothing to be
    stale about — this stays correct after the banner is added."""
    monkeypatch.setattr(e2e, "SITE", tmp_path / "site")
    e2e.build_static()
    out = (tmp_path / "site" / "engineering-e2e.html").read_text()
    assert 'class="stalled-banner"' not in out


def test_rendered_page_never_names_the_retired_ingest_route(db):
    """`/ingest/signal` was removed 2026-08-19 and answers 404, but this page
    went on telling readers to POST results to it for thirteen days
    (issue_01M1F1G643WE93P0C0R2R9T63V).

    The check is against the RENDERED OUTPUT, not the source, and that is the
    whole point of it: a retirement note in a comment SHOULD name the route,
    so grepping the file would fail on the correct fix and pass on a rewrite
    that only moved the copy. Only what the reader actually sees can settle
    it. Following those stale instructions reproduced the original outage
    exactly — `curl` exits 0 against a 404, so the wiring looked successful
    and nothing arrived."""
    _seed(_mk(meta={"status": "pass"}, body="ANY-RESULT"))
    assert "/ingest/signal" not in e2e.live_html()


def test_the_empty_state_also_never_names_the_retired_route(db):
    """The tiles-empty branch carries its own copy, so it is a second place
    the retired route can survive — and the one a reader hits precisely when
    no results are arriving, which is when they are most likely to go wiring
    something up."""
    assert "/ingest/signal" not in e2e.live_html()


# --- the fetch is ordered by the clock the page displays, not storage order -
#
# issue_01M1F4KN3XW0670TTWMM7PP8C1: push-written signals carry only
# ingested_at; poller-written ones carry produced_at. The renderers display
# `produced_at or ingested_at`, but the fetch used to inherit the Library's
# default order (ingested_at DESC) — sorting by one clock and dating by
# another. A backfill that shares one ingested_at across many produced_at
# values made this visible: 12 rows out of 46 displayed a date newer than the
# row above them.


def test_recent_runs_are_ordered_by_the_clock_they_display(db, monkeypatch):
    """Reproduces the exact shape that found the bug: 14 backfilled rows that
    all share ONE ingested_at (the moment the backfill pass wrote them) but
    span 14 days of produced_at (the actual run times). Sorting by storage
    time — the Library's default — makes their order within that batch
    unrelated to run time; the measured page showed 12 of these 14 rows out
    of order. Sorting by produced_at fixes this population; a separate test
    (test_ordering_survives_past_the_scan_cap) covers the cap interaction."""
    backfilled = [_mk(stream="poller", body=f"POLLER-{i}",
                      produced_at=dt.datetime(2026, 8, 19) + dt.timedelta(days=i),
                      ingested_at=dt.datetime(2026, 8, 31, 17, 31, 25))
                  for i in range(14)]
    _seed(*backfilled)

    real_list_signals = library_client.list_signals
    calls = []

    def spy(**kw):
        result = real_list_signals(**kw)
        calls.append((kw, result))
        return result

    monkeypatch.setattr(library_client, "list_signals", spy)
    e2e.live_html()

    assert calls, "live_html never called library_client.list_signals"
    kw, recent = calls[0]
    assert kw.get("order") == "produced_at", (
        "the fetch must ask the Library to sort by the clock the page "
        "displays, not inherit storage order and sort client-side")
    whens = [sig.produced_at or sig.ingested_at for sig in recent]
    inversions = sum(1 for a, b in zip(whens, whens[1:]) if b > a)
    assert inversions == 0, f"{inversions} rows displayed a date newer than the row above them"


def test_ordering_survives_past_the_scan_cap(db):
    """The client-side-sort fix looks identical to the correct fix under
    46 signals, which is exactly what makes it the wrong fix: it sorts
    only the top `_SCAN_LIMIT` rows BY STORAGE TIME, so a truly-latest run
    whose storage time is old (a backfilled row) never enters the fetched
    set at all — no amount of re-sorting after the fetch can recover it.
    This seeds 401 rows, one past `_SCAN_LIMIT` (400): a run whose
    produced_at is the newest of all of them but whose ingested_at is the
    oldest of all of them. It survives only if the fetch itself is ordered
    by produced_at before the cap is applied."""
    base = dt.datetime(2026, 8, 1, 12, 0, 0)
    filler = [_mk(stream="filler", body=f"FILLER-{i}",
                  ingested_at=base - dt.timedelta(minutes=i))
              for i in range(e2e._SCAN_LIMIT)]
    buried = _mk(stream="BURIED-SUITE", body="BURIED-BUT-LATEST-RUN",
                produced_at=base + dt.timedelta(days=365),
                ingested_at=base - dt.timedelta(days=365))
    _seed(*filler, buried)
    html = e2e.live_html()
    assert "BURIED-BUT-LATEST-RUN" in html, (
        "a row past the scan cap, latest by produced_at but oldest by "
        "ingested_at, never reached the page — the fetch is sorting the "
        "wrong window")


def test_latest_tile_is_max_by_clock_not_first_in_fetch_order(db, monkeypatch):
    """Pins the tile choice to an explicit max over `produced_at or
    ingested_at`, independent of the order `list_signals` returns rows in.
    The fetch is ordered by produced_at today, so first-occurrence and max
    agree — this proves the tile stays correct even if that ordering ever
    regresses, which would otherwise silently pin a stale run as \"latest\"
    with no visual difference from a correct tile."""
    now = dt.datetime(2026, 9, 1, 12, 0, 0)
    older = library_client.SignalRecord(
        id="s-older", source="ci", stream="suite", kind="test-result",
        actor="ci-bot", ingested_by="ci-bot", product="vivy", body="OLDER-RUN",
        refs={}, meta={"status": "fail"}, privacy_class="internal",
        produced_at=now - dt.timedelta(days=1), ingested_at=now - dt.timedelta(days=1))
    newer = library_client.SignalRecord(
        id="s-newer", source="ci", stream="suite", kind="test-result",
        actor="ci-bot", ingested_by="ci-bot", product="vivy", body="NEWER-RUN",
        refs={}, meta={"status": "pass"}, privacy_class="internal",
        produced_at=now, ingested_at=now)
    # Deliberately out of produced_at order — newer sorts LAST here, so a
    # first-occurrence pick would choose OLDER-RUN's FAIL tile instead.
    monkeypatch.setattr(library_client, "list_signals", lambda **kw: [older, newer])
    html = e2e.live_html()
    assert "NEWER-RUN" in html
    assert '<div class="verdict">PASS</div>' in html


# --- the copy explains why the page is vivy-only, not just scoped that way --
#
# The operator's ruling on this issue: /engineering/e2e is a staging-smoke page, not a
# fleet CI dashboard, so it stays scoped to vivy. The one thing missing was a
# reason a reader can find at the moment of confusion.


def test_copy_explains_why_only_vivy_is_tracked(db):
    html = e2e.live_html()
    assert "only end-to-end suite on the fleet" in html
    assert "per-PR CI" in html


def test_when_floors_an_undateable_row_so_the_tile_max_cannot_raise(db):
    """`live_html` picks the latest tile with `when > latest_when[key]`.
    First-occurrence never compared two rows; a max does, and `None > None`
    is a TypeError — which inside `live_html` drops the page to its static
    fallback, breaking this module's stated promise that "a malformed post
    must never blank the page".

    `SignalRecord.ingested_at` is annotated as a plain datetime, but nothing
    enforces that at runtime: `library_client._iso` returns None for an empty
    or null value. Whether the real Library can serve that WAS established,
    and it comes out the other way: the Library declares `ingested_at DATETIME
    NOT NULL` in its baseline migration and server-stamps the value on write,
    never taking it from the caller, so the shape is schema-impossible today.
    The floor is kept as cheap insurance against the annotation's promise not
    holding at runtime, not as a fix for an observed failure. An earlier draft
    of this docstring said the question was "not established" when it was two
    greps away — recorded because that is the same lapse this file's history
    is full of.

    Tested at `_when` rather than through the page on purpose:
    `library_fake.seed_signal` does `ingested_at or utcnow()`, so a seeded row
    ALWAYS has a timestamp and a page-level test of this cannot fail no matter
    what `live_html` does. A test that passes whether or not the code is
    correct is worse than no test, so this one asserts the property where it
    actually lives."""
    class _Undateable:
        produced_at = None
        ingested_at = None

    floored = e2e._when(_Undateable())
    assert floored == dt.datetime.min
    # the point of the floor: it stays comparable, and never wins a max
    assert floored < dt.datetime(2026, 1, 1)
