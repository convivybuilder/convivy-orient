"""Tests for security_build.py — the /security dashboard's render layer (ARCH-306).

The aggregation lives in agent-platform `security-scorecard`; orient only reads its
rollup and renders. These pin the render: the rate-signal thresholds, the
highest-risk-first ordering, the empty state, and that the honest caveats (no policy
threshold yet; escaped-to-prod not wired, surfaced as an `awaiting_state` panel rather
than footnote prose) are actually on the page.
"""
import orient_time
import security_build as sb


def _report():
    return {
        "generated_at": "2026-07-02T12:00:00Z",
        "window_days": 30,
        "overall": {
            "prs_reviewed": 133, "changed_loc": 43792,
            "blocking_findings": 7, "non_blocking_findings": 25, "total_findings": 32,
            "findings_per_pr": 0.24, "blocking_per_pr": 0.05,
            "findings_per_kloc": 0.73, "blocking_per_kloc": 0.16,
            "pct_prs_with_blocking": 3.8, "pct_prs_with_any_finding": 15.8,
            "internal_escapes": 4,
        },
        "by_model": {
            "claude-sonnet-4-6": {"prs_reviewed": 108, "changed_loc": 40457,
                                  "blocking_findings": 4, "non_blocking_findings": 22,
                                  "total_findings": 26, "blocking_per_kloc": 0.10,
                                  "pct_prs_with_blocking": 2.8},
            "deepseek/deepseek-chat": {"prs_reviewed": 24, "changed_loc": 2897,
                                       "blocking_findings": 3, "non_blocking_findings": 3,
                                       "total_findings": 6, "blocking_per_kloc": 1.04,
                                       "pct_prs_with_blocking": 8.3},
        },
        "by_repo": {
            "vivy": {"prs_reviewed": 88, "changed_loc": 30570, "total_findings": 17,
                     "blocking_findings": 2, "blocking_per_kloc": 0.07, "pct_prs_with_blocking": 2.3},
            "constance": {"prs_reviewed": 23, "changed_loc": 5380, "total_findings": 8,
                          "blocking_findings": 3, "blocking_per_kloc": 0.56, "pct_prs_with_blocking": 8.7},
        },
    }


def test_blk_cls_thresholds():
    assert sb._blk_cls(0) == "good"        # zero is clean
    assert sb._blk_cls(0.16) == "warn"     # above zero, under the soft band
    assert sb._blk_cls(1.04) == "bad"      # elevated


def test_render_has_stat_cards_and_headline_rate():
    html = sb.render(_report())
    assert html.count('class="statcard"') == 4
    assert "Blocking / KLOC" in html
    assert "PRs w/ a blocking vuln" in html
    assert "Internal escapes" in html


def test_render_by_model_ordered_highest_risk_first():
    html = sb.render(_report())
    # deepseek (1.04 blk/KLOC) must render before sonnet (0.10) — highest risk reads first
    assert html.index("deepseek/deepseek-chat") < html.index("claude-sonnet-4-6")


def test_render_carries_honest_caveats():
    html = sb.render(_report())
    assert "not a policy threshold" in html          # threshold-pending, deliberate
    assert "review-post" in html                     # provenance of the numbers


def test_render_escaped_to_prod_is_an_awaiting_state_panel_owned_by_the_keel():
    """The look-back feed for "Internal escapes" doesn't exist anywhere yet — not
    a Library route, not something Orient can write a client for — because
    agent-platform's security-scorecard harvester writes `escaped_to_prod: null`
    with its own docstring saying the feed "we don't have yet" (verified against
    that harvester's source, not assumed). That's Keel-owned execution-layer
    work, so this must not become an owner="orient" claim — the exact
    misattribution this component exists to catch
    (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comments 902/903)."""
    html = sb.render(_report())
    assert 'class="awaiting-state' in html
    assert "WAITING ON THE KEEL" in html
    assert "WAITING ON ORIENT" not in html


def _fix_now(monkeypatch, iso_date: str):
    import datetime as dt
    fixed = dt.datetime(*map(int, iso_date.split("-")), tzinfo=orient_time.MST)
    monkeypatch.setattr(orient_time, "now", lambda: fixed)


def test_render_escaped_to_prod_panel_holds_while_recently_verified(monkeypatch):
    """Pins the direction the panel currently claims: verified 2026-08-26,
    so a render shortly after that date must read as holding, not stale."""
    _fix_now(monkeypatch, "2026-08-27")
    html = sb.render(_report())
    assert 'class="awaiting-state holds"' in html
    assert 'class="awaiting-state stale"' not in html


def test_render_escaped_to_prod_panel_goes_stale_if_left_unverified(monkeypatch):
    """The failure this pins against: a claim that was true on the day it was
    written and never re-checked again. If nobody re-verifies this panel's
    2026-08-26 claim for over `_AWAITING_STALE_AFTER_DAYS`, the page must say
    so rather than keep reading as current forever — asserting only the
    panel's current (holding) state, as the previous test does, would pass
    identically whether the staleness path were wired or missing."""
    _fix_now(monkeypatch, "2026-09-15")   # 20 days past 2026-08-26
    html = sb.render(_report())
    assert 'class="awaiting-state stale"' in html
    assert 'class="awaiting-state holds"' not in html


def test_render_escaped_to_prod_panel_self_corrects_once_the_feed_lands():
    """The panel's "does not exist yet" claim would go stale silently for up
    to 14 days if it only had the `verified` stamp to go on — but `render()`
    already holds the value (`overall.escaped_to_prod`) that falsifies the
    claim the moment agent-platform's harvester starts writing it. This must
    flip to contradicted at that render, not wait on the stamp date, even
    though `verified` here is today's date and would otherwise read holds."""
    report = _report()
    report["overall"]["escaped_to_prod"] = 2
    html = sb.render(report)
    assert 'class="awaiting-state contradicted"' in html
    assert 'class="awaiting-state holds"' not in html
    assert "escaped_to_prod is now 2" in html
    assert "the look-back feed has landed" in html


def test_render_no_longer_admits_the_escaped_to_prod_gap_as_a_footnote():
    """The old admission lived inside the `<ul class="notes">` list; it must move
    into the panel rather than being duplicated in both places."""
    html = sb.render(_report())
    notes_start = html.index('<ul class="notes"')
    notes_html = html[notes_start:html.index("</ul>", notes_start)]
    assert "is not yet wired" not in notes_html
    assert "escaped-to-prod" not in notes_html and "escaped to prod" not in notes_html


def test_render_empty_state():
    html = sb.render({"overall": {}, "by_model": {}, "by_repo": {}})
    assert "has not run" in html
    assert 'class="statcard"' not in html            # no cards when there's no data


# --- freshness (owns the /security <-> /overview stalled agreement) --------


def test_freshness_stalled_past_30h():
    fresh = sb.freshness({"generated_at": "2020-01-01T00:00:00Z"})
    assert fresh.stalled is True
    assert fresh.cadence == "daily (07:45 MST)"


def test_freshness_not_stalled_when_recent():
    import orient_time
    fresh = sb.freshness({"generated_at": orient_time.now().isoformat()})
    assert fresh.stalled is False


def test_render_shows_stalled_banner_for_an_old_report():
    report = _report()
    report["generated_at"] = "2020-01-01T00:00:00Z"
    html = sb.render(report)
    assert 'class="stalled-banner"' in html


def test_render_no_stalled_banner_for_a_fresh_report():
    import orient_time
    report = _report()
    report["generated_at"] = orient_time.now().isoformat()
    html = sb.render(report)
    assert 'class="stalled-banner"' not in html
