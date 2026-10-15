"""Tests for warrant_build.py — the /warrant dashboard render (ARCH-309).

Aggregation lives in agent-platform `warrant-scorecard`; orient reads its rollup.
These pin the render: the rate signal, highest-risk-first ordering, the clean
banner when there are no reverts yet, the empty state, and that the framing (the
warrant-vs-quality distinction, the convention) is actually on the page.
"""
import warrant_build as wb


def _report(reverted=True):
    by_model = {
        "claude-sonnet-4-6": {"merged_prs": 192, "reverted": 1 if reverted else 0,
                              "revert_rate": 0.5 if reverted else 0.0,
                              "unwarranted": 1 if reverted else 0,
                              "unwarranted_rate": 0.5 if reverted else 0.0,
                              "bug_reverts": 0, "bug_revert_rate": 0.0,
                              "by_reason": {"unwarranted": 1} if reverted else {}},
        "deepseek/deepseek-chat": {"merged_prs": 101, "reverted": 0, "revert_rate": 0.0,
                                   "unwarranted": 0, "unwarranted_rate": 0.0,
                                   "bug_reverts": 0, "bug_revert_rate": 0.0, "by_reason": {}},
    }
    overall = {"merged_prs": 293, "reverted": 1 if reverted else 0,
               "revert_rate": 0.3 if reverted else 0.0,
               "unwarranted": 1 if reverted else 0, "unwarranted_rate": 0.3 if reverted else 0.0,
               "bug_reverts": 0, "bug_revert_rate": 0.0,
               "by_reason": {"unwarranted": 1} if reverted else {}}
    return {"generated_at": "2026-07-03T12:00:00Z", "window_days": 90,
            "overall": overall, "by_model": by_model, "by_repo": {}}


def test_rate_cls_thresholds():
    assert wb._rate_cls(0) == "good"
    assert wb._rate_cls(0.5) == "warn"
    assert wb._rate_cls(3) == "bad"


def test_render_has_four_cards_and_headline():
    html = wb.render(_report())
    assert html.count('class="statcard"') == 4
    assert "Unwarranted rate" in html
    assert "Merged agent PRs" in html


def test_render_orders_offender_first():
    html = wb.render(_report())
    # sonnet (0.5% unwarranted) must render before deepseek (0%)
    assert html.index("claude-sonnet-4-6") < html.index("deepseek/deepseek-chat")


def test_render_clean_banner_when_no_reverts():
    html = wb.render(_report(reverted=False))
    assert "No reverts recorded" in html
    assert "Revert-reason:" in html          # the convention is still explained


def test_render_carries_warrant_vs_quality_framing():
    html = wb.render(_report())
    assert "spurious-work" in html or "spurious" in html
    assert "not a policy threshold" in html
    assert "premise, not the code" in html


def test_render_empty_state():
    html = wb.render({"overall": {}, "by_model": {}, "by_repo": {}})
    assert "has not run" in html
    assert 'class="statcard"' not in html


# --- freshness (owns the /warrant <-> /overview stalled agreement) ----------


def test_freshness_stalled_past_30h():
    fresh = wb.freshness({"generated_at": "2020-01-01T00:00:00Z"})
    assert fresh.stalled is True
    assert fresh.cadence == "daily (07:50 MST)"


def test_freshness_not_stalled_when_recent():
    import orient_time
    fresh = wb.freshness({"generated_at": orient_time.now().isoformat()})
    assert fresh.stalled is False


def test_render_shows_stalled_banner_for_an_old_report():
    report = _report()
    report["generated_at"] = "2020-01-01T00:00:00Z"
    html = wb.render(report)
    assert 'class="stalled-banner"' in html


def test_render_no_stalled_banner_for_a_fresh_report():
    import orient_time
    report = _report()
    report["generated_at"] = orient_time.now().isoformat()
    html = wb.render(report)
    assert 'class="stalled-banner"' not in html


# --- scoping (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) ----------------------


def _report_with_repos():
    report = _report()
    report["by_repo"] = {
        "vivy": {"merged_prs": 10, "reverted": 0, "revert_rate": 0.0,
                 "unwarranted": 0, "unwarranted_rate": 0.0, "bug_reverts": 0},
        "constance": {"merged_prs": 5, "reverted": 1, "revert_rate": 20.0,
                      "unwarranted": 1, "unwarranted_rate": 20.0, "bug_reverts": 0},
    }
    return report


def test_scoped_viewer_sees_only_their_own_repo_in_by_repo(monkeypatch):
    import crew_registry
    monkeypatch.setattr(crew_registry, "repos_in_scope", lambda scope: frozenset({"vivy"}))
    html = wb.render(_report_with_repos(), frozenset({"some-project"}), False)
    assert "\">vivy<" in html
    assert "\">constance<" not in html


def test_scoped_viewer_never_sees_the_fleet_wide_overall_or_by_model(monkeypatch):
    import crew_registry
    monkeypatch.setattr(crew_registry, "repos_in_scope", lambda scope: frozenset({"vivy"}))
    html = wb.render(_report_with_repos(), frozenset({"some-project"}), False)
    assert "Unwarranted rate" not in html        # the overall statcard label
    assert "claude-sonnet-4-6" not in html       # a by_model key
    assert "deepseek/deepseek-chat" not in html


def test_whole_fleet_viewer_sees_everything_unfiltered(monkeypatch):
    import crew_registry
    monkeypatch.setattr(crew_registry, "repos_in_scope", lambda scope: frozenset({"vivy"}))
    html = wb.render(_report_with_repos(), frozenset(), True)
    assert "\">vivy<" in html and "\">constance<" in html
    assert "Unwarranted rate" in html


def test_scoped_viewer_with_no_in_scope_repo_data_gets_a_scoped_empty_state(monkeypatch):
    import crew_registry
    monkeypatch.setattr(crew_registry, "repos_in_scope", lambda scope: frozenset({"no-such-repo"}))
    html = wb.render(_report_with_repos(), frozenset({"some-project"}), False)
    assert "No warrant-scorecard data yet for a project in your scope" in html
    assert "\">vivy<" not in html and "\">constance<" not in html


# --- main: the static fallback must never carry real warrant data ---------


def test_main_seeds_an_empty_fallback_not_real_data(tmp_path, monkeypatch):
    """warrant.html is served to ANY viewer's failed render, scoped or not
    (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): a snapshot naming a real repo
    would leak to a scoped viewer."""
    monkeypatch.setattr(wb, "OUT", tmp_path)
    monkeypatch.setattr(wb, "load", lambda: _report_with_repos())
    wb.main()
    html = (tmp_path / "warrant.html").read_text(encoding="utf-8")
    assert "\">vivy<" not in html and "\">constance<" not in html
    assert "Live view unavailable" in html
