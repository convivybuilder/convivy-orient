"""Tests for the merge-queue index (/engineering/merge-queue, ARCH-359 step 3).

The GitHub fetch is pure I/O (subprocess), so these test the pure layer — the
CI rollup, the classify grouping, the reason line — and the render (cleared-first
ordering, XSS-escaping, empty state) against a monkeypatched fetch. No `gh` runs.
"""
from __future__ import annotations

import merge_queue_build as m


# --- CI rollup ---------------------------------------------------------------


def test_ci_status_passing_checkrun():
    assert m._ci_status([{"status": "COMPLETED", "conclusion": "SUCCESS"}]) == "passing"


def test_ci_status_passing_status_context():
    assert m._ci_status([{"state": "SUCCESS"}]) == "passing"


def test_ci_status_failing_wins():
    assert m._ci_status([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]) == "failing"


def test_ci_status_failing_wins_regardless_of_order():
    # a FAILURE after a PENDING must still read failing (priority, not array order)
    assert m._ci_status([{"status": "IN_PROGRESS"}, {"conclusion": "FAILURE"}]) == "failing"
    assert m._ci_status([{"state": "PENDING"}, {"state": "FAILURE"}]) == "failing"


def test_ci_status_pending():
    assert m._ci_status([{"status": "IN_PROGRESS", "conclusion": ""}]) == "pending"
    assert m._ci_status([{"state": "PENDING"}]) == "pending"


def test_ci_status_none_when_empty():
    assert m._ci_status([]) == "none"
    assert m._ci_status(None) == "none"


# --- classify ----------------------------------------------------------------


def _pr(**kw):
    base = dict(number=1, title="t", url="https://github.com/convivy/vivy/pull/1",
                isDraft=False, createdAt="2026-07-17T00:00:00Z", author={"login": "u"},
                reviewDecision="REVIEW_REQUIRED", mergeable="MERGEABLE",
                statusCheckRollup=[{"conclusion": "SUCCESS"}], repo="vivy")
    base.update(kw)
    return base


def test_classify_cleared_is_ci_green_no_conflict_not_draft():
    r = m._classify(_pr())
    assert r["cleared"] and r["group"] == "cleared"


def test_classify_cleared_ignores_review_decision():
    # our reviews are PR comments, not GitHub approvals, so REVIEW_REQUIRED still clears
    r = m._classify(_pr(reviewDecision="REVIEW_REQUIRED"))
    assert r["cleared"]


def test_classify_blocked_on_failing_ci():
    r = m._classify(_pr(statusCheckRollup=[{"conclusion": "FAILURE"}]))
    assert not r["cleared"] and r["group"] == "blocked"


def test_classify_blocked_on_conflict():
    r = m._classify(_pr(mergeable="CONFLICTING"))
    assert r["group"] == "blocked" and r["conflicting"]


def test_classify_draft_group():
    r = m._classify(_pr(isDraft=True))
    assert r["group"] == "draft" and not r["cleared"]


def test_classify_in_cycle_on_pending_ci():
    r = m._classify(_pr(statusCheckRollup=[{"status": "IN_PROGRESS"}]))
    assert r["group"] == "in-cycle"


def test_reason_notes_approved_as_bonus():
    assert "approved" in m._reason(m._classify(_pr(reviewDecision="APPROVED")))
    assert "approved" not in m._reason(m._classify(_pr(reviewDecision="REVIEW_REQUIRED")))


# --- render ------------------------------------------------------------------


def _rows(monkeypatch, prs):
    monkeypatch.setattr(m, "_fetch_open_prs", lambda force=False: [m._classify(p) for p in prs])


def test_fetch_sorts_most_recent_activity_first(monkeypatch):
    # _fetch_open_prs sorts by updated_at desc (the operator 2026-07-17: most-recent at top),
    # regardless of cleared/blocked status.
    monkeypatch.setattr(m, "_repos_with_open_prs", lambda: ["vivy"])
    monkeypatch.setattr(m, "_open_prs_for_repo", lambda repo: [
        _pr(number=1, title="OLD-ACTIVITY", updatedAt="2026-07-10T00:00:00Z"),
        _pr(number=2, title="NEW-ACTIVITY", updatedAt="2026-07-17T00:00:00Z",
            statusCheckRollup=[{"conclusion": "FAILURE"}]),  # blocked but most recent
        _pr(number=3, title="MID-ACTIVITY", updatedAt="2026-07-14T00:00:00Z"),
    ])
    m._cache = None
    rows = m._fetch_open_prs(force=True)
    titles = [r["title"] for r in rows]
    assert titles == ["NEW-ACTIVITY", "MID-ACTIVITY", "OLD-ACTIVITY"]


def test_row_shows_opened_and_updated_dates_in_mst(monkeypatch):
    """Dates render in MST (America/Phoenix, fixed UTC-7), not the UTC GitHub sends.

    These inputs are midnight UTC, which is 17:00 the PREVIOUS day in MST — so
    the correct render is Jul 9 / Jul 16. This test previously asserted Jul 10 /
    Jul 17, encoding the bug: every PR opened or merged after 17:00 MST was dated
    a day late, which is the window most of our merges land in.
    """
    _rows(monkeypatch, [_pr(createdAt="2026-07-10T00:00:00Z", updatedAt="2026-07-17T00:00:00Z")])
    html = m.live_html()
    assert "opened Jul 9" in html and "updated Jul 16" in html


def test_row_dates_unchanged_when_utc_and_mst_share_a_day(monkeypatch):
    """The other direction: a mid-day UTC timestamp is the same calendar day in
    MST, and must not be shifted. A fix that subtracted a day unconditionally
    would pass the test above and fail this one."""
    _rows(monkeypatch, [_pr(createdAt="2026-07-10T18:00:00Z", updatedAt="2026-07-17T18:00:00Z")])
    html = m.live_html()
    assert "opened Jul 10" in html and "updated Jul 17" in html


def test_render_escapes_title_and_author(monkeypatch):
    _rows(monkeypatch, [_pr(title="<script>alert(1)</script>", author={"login": "<b>x</b>"})])
    html = m.live_html()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html and "&lt;b&gt;x&lt;/b&gt;" in html


def test_render_rejects_non_http_pr_url(monkeypatch):
    _rows(monkeypatch, [_pr(url="javascript:alert(1)")])
    html = m.live_html()
    assert "javascript:alert(1)" not in html


def test_empty_state(monkeypatch):
    monkeypatch.setattr(m, "_fetch_open_prs", lambda force=False: [])
    assert "queue is clear" in m.live_html().lower()


def test_route_registered_full_tier():
    import server
    routes = server._full_only_routes()
    assert routes["/engineering/merge-queue"][1] is m.live_html


def test_nav_places_merge_queue_in_engineering():
    import design_system as ds
    html = ds.nav_component("/engineering/merge-queue", "full")
    assert 'href="/engineering/merge-queue"' in html
