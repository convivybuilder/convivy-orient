"""Tests for code_health_build.py — the code-health dashboard's data layer.

The gh/git IO is impure (excluded); these pin the metric derivation, where a
wrong CI pass-rate or test count would mislead the dashboard. The CI-run shape
is the subtle one: products run CI on PR branches and produce cancelled runs.
"""
from datetime import datetime, timezone

import code_health_build as ch


# --- ci_health ---------------------------------------------------------------


def _run(concl, status="completed", date="2026-06-02T10:00:00Z"):
    return {"conclusion": concl, "status": status, "createdAt": date}


def test_ci_health_pass_rate_excludes_cancelled():
    runs = [
        _run("success", date="2026-06-02T12:00:00Z"),
        _run("failure", date="2026-06-02T11:00:00Z"),
        _run("success", date="2026-06-02T10:00:00Z"),
        _run("cancelled", date="2026-06-02T13:00:00Z"),  # superseded — not a failure
    ]
    h = ch.ci_health(runs)
    assert h["pass_rate"] == 2 / 3            # 2 success of 3 decisive (cancelled excluded)
    assert h["last"] == "success" and h["last_date"] == "2026-06-02"  # most recent DECISIVE


def test_ci_health_none_when_no_decisive_runs():
    h = ch.ci_health([_run("cancelled"), _run("skipped"), {"status": "in_progress"}])
    assert h == {"pass_rate": None, "last": None, "last_date": None}


def test_ci_health_empty():
    assert ch.ci_health([])["pass_rate"] is None


# --- harvest: all-branch vs main-only CI (ARCH-179) --------------------------


def test_harvest_emits_main_only_ci_alongside_all_branch(monkeypatch):
    """The bug this guards: a busy night of agent PRs failing→fixing→merging
    drags the all-branch `ci_pass_rate` below the alert floor while main is
    healthy. harvest() must emit a separate `ci_pass_rate_main`/`ci_last_main`
    from a `--branch main` harvest, so the alert can gate on merged-code health.
    """
    now = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
    # all-branch: 2 of 4 decisive green (50%) — PR-branch iteration noise.
    all_branch = [_run("failure", date="2026-06-17T11:00:00Z"),
                  _run("failure", date="2026-06-17T10:30:00Z"),
                  _run("success", date="2026-06-17T10:00:00Z"),
                  _run("success", date="2026-06-17T09:00:00Z")]
    # main-only: both green — merged code is healthy.
    main_only = [_run("success", date="2026-06-17T08:00:00Z"),
                 _run("success", date="2026-06-16T08:00:00Z")]

    def fake_gh_json(args):
        if args[0] == "run" and "--branch" in args:
            return main_only
        if args[0] == "run":
            return all_branch
        return []  # pr lists → empty

    monkeypatch.setattr(ch, "_gh_json", fake_gh_json)
    monkeypatch.setattr(ch, "reverts_recent", lambda p: 0)
    monkeypatch.setattr(ch, "count_tests", lambda p: 0)
    monkeypatch.setattr(ch, "coverage_pct", lambda full: None)
    monkeypatch.setattr(ch, "REPOS", [("sous-chef", "product")])

    row = ch.harvest(now)[0]
    assert row["ci_pass_rate"] == 0.5            # all-branch — dashboard, unchanged
    assert row["ci_pass_rate_main"] == 1.0       # main-only — the alert basis
    assert row["ci_last_main"] == "success"


# --- count_reverts (ARCH-180) ------------------------------------------------


def test_count_reverts_matches_revert_subjects_only():
    """Real reverts are `git revert`'s subject `Revert "..."`. Prose lines that
    merely start with "reverts"/"Reverting" are NOT reverts — the old whole-message
    `git log --grep=^Revert` matched those body lines (every nonzero 2026-06-17
    count was such a false positive)."""
    subjects = [
        'Revert "agentctl: attribute reviews to the real model"',  # real revert
        "iterm: wire cmd-click file-open into the co-* DynamicProfiles",  # false: body said "reverts on a reset"
        "agentctl: attribute reviews to the real model, not Claude",       # false: body said "reverts to sonnet"
        "metric-alerts: threshold alerts on cost/quality + code-health",   # false: body said "reverts >= 2"
        "Reverting is sometimes necessary",  # prose, not a git revert subject
    ]
    assert ch.count_reverts(subjects) == 1


def test_count_reverts_empty():
    assert ch.count_reverts([]) == 0


# --- pr_metrics --------------------------------------------------------------


def test_pr_metrics_open_age_and_recent_merges():
    now = datetime(2026, 6, 2, tzinfo=timezone.utc)
    open_prs = [
        {"number": 1, "createdAt": "2026-05-30T00:00:00Z"},   # 3d
        {"number": 2, "createdAt": "2026-05-01T00:00:00Z"},   # 32d (oldest)
    ]
    merged = [
        {"number": 3, "mergedAt": "2026-05-20T00:00:00Z"},    # within 30d
        {"number": 4, "mergedAt": "2026-04-01T00:00:00Z"},    # outside 30d
    ]
    m = ch.pr_metrics(open_prs, merged, now)
    assert m["open"] == 2 and m["oldest_open_days"] == 32 and m["merged_recent"] == 1


def test_pr_metrics_empty():
    now = datetime(2026, 6, 2, tzinfo=timezone.utc)
    m = ch.pr_metrics([], [], now)
    assert m == {"open": 0, "oldest_open_days": None, "merged_recent": 0}


# --- threshold status (reused by alerts_build.py's /alerts evaluator) --------


def test_ci_status_thresholds():
    assert ch.ci_status(0.95) == "good"
    assert ch.ci_status(0.9) == "good"     # boundary is inclusive
    assert ch.ci_status(0.8) == "warn"
    assert ch.ci_status(0.7) == "warn"     # boundary is inclusive
    assert ch.ci_status(0.5) == "bad"
    assert ch.ci_status(None) is None


def test_oldest_pr_status_thresholds():
    assert ch.oldest_pr_status(3) == "good"
    assert ch.oldest_pr_status(7) == "good"    # boundary is inclusive on the good side
    assert ch.oldest_pr_status(8) == "warn"
    assert ch.oldest_pr_status(14) == "warn"   # boundary is inclusive on the warn side
    assert ch.oldest_pr_status(15) == "bad"
    assert ch.oldest_pr_status(None) is None


def test_reverts_status():
    assert ch.reverts_status(0) == "good"
    assert ch.reverts_status(1) == "bad"
    assert ch.reverts_status(None) is None


# --- count_tests (the heuristic that must skip worktrees) ---------------------


def test_count_tests_counts_def_test_and_skips_worktrees(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_one():\n    pass\ndef test_two():\n    pass\n")
    (tmp_path / "mod_test.py").write_text("def test_three():\n    pass\n")
    (tmp_path / "helpers.py").write_text("def helper():\n    pass\n")  # not a test file
    # an agent worktree under .claude duplicates tests — must NOT be counted
    wt = tmp_path / ".claude" / "worktrees" / "x" / "tests"
    wt.mkdir(parents=True)
    (wt / "test_dupe.py").write_text("def test_dupe():\n    pass\ndef test_dupe2():\n    pass\n")
    assert ch.count_tests(tmp_path) == 3  # 2 + 1, the .claude copies excluded


def test_count_tests_none_for_missing_checkout(tmp_path):
    assert ch.count_tests(tmp_path / "does-not-exist") is None


# --- parse_coverage_pct (the coverage artifact contract) ---------------------


def test_parse_coverage_pct_coverage_py_native():
    # coverage.py's `coverage json` shape — uploaded as-is by Python repos
    text = '{"meta": {"version": "7.6"}, "totals": {"percent_covered": 87.34, "num_statements": 500}}'
    assert ch.parse_coverage_pct(text) == 0.8734


def test_parse_coverage_pct_simple_contract():
    # the tool-agnostic {"pct": N} shape for non-pytest stacks
    assert ch.parse_coverage_pct('{"pct": 72}') == 0.72


def test_parse_coverage_pct_full_and_zero():
    assert ch.parse_coverage_pct('{"pct": 100}') == 1.0
    assert ch.parse_coverage_pct('{"totals": {"percent_covered": 0}}') == 0.0


def test_parse_coverage_pct_rejects_malformed_or_out_of_range():
    for bad in ('', 'not json', '[]', '{}', '{"totals": {}}',
                '{"pct": "high"}', '{"pct": 150}', '{"pct": -3}', '{"other": 5}'):
        assert ch.parse_coverage_pct(bad) is None


# --- history accumulation (the trend data layer) -----------------------------


def _row(repo, **kw):
    base = {"repo": repo, "kind": "internal", "ci_pass_rate": 0.9, "ci_last": "success",
            "ci_last_date": "2026-06-02", "open_prs": 0, "oldest_open_days": None,
            "merged_recent": 6, "reverts_recent": 0, "test_count": 173, "coverage": None}
    base.update(kw)
    return base


def test_history_record_is_slim_and_carries_only_trend_metrics():
    rec = ch.history_record(_row("vivy", ci_pass_rate=0.8, test_count=42), "2026-06-02")
    assert rec["date"] == "2026-06-02" and rec["repo"] == "vivy"
    assert rec["ci_pass_rate"] == 0.8 and rec["test_count"] == 42
    assert set(rec) == {"date", "repo", *ch.TREND_METRICS}
    assert "ci_last" not in rec and "kind" not in rec   # non-trend fields dropped


def test_merge_history_appends_new_day():
    day1 = ch.merge_history([], [_row("vivy"), _row("constance")], "2026-06-01")
    day2 = ch.merge_history(day1, [_row("vivy"), _row("constance")], "2026-06-02")
    assert len(day2) == 4
    assert sorted({r["date"] for r in day2}) == ["2026-06-01", "2026-06-02"]


def test_merge_history_same_day_rerun_overwrites_not_duplicates():
    first = ch.merge_history([], [_row("vivy", test_count=10)], "2026-06-02")
    again = ch.merge_history(first, [_row("vivy", test_count=20)], "2026-06-02")
    assert len(again) == 1                       # idempotent — last run of the day wins
    assert again[0]["test_count"] == 20


def test_merge_history_sorted_by_date_then_repo():
    h = ch.merge_history(
        ch.merge_history([], [_row("vivy"), _row("agent-platform")], "2026-06-02"),
        [_row("constance")], "2026-06-01")
    assert [(r["date"], r["repo"]) for r in h] == [
        ("2026-06-01", "constance"),
        ("2026-06-02", "agent-platform"), ("2026-06-02", "vivy")]


# --- render ------------------------------------------------------------------


def test_render_self_contained(tmp_path):
    rows = [_row("agent-platform")]
    history = [ch.history_record(rows[0], "2026-06-01"),
               ch.history_record(rows[0], "2026-06-02")]
    html = ch.render(rows, history)
    assert "__DATA__" not in html and "__NREPOS__" not in html
    assert "__HISTORY__" not in html and "__NDAYS__" not in html
    assert "const ROWS = [" in html and "const HISTORY = [" in html
    assert "agent-platform" in html


def test_render_has_metric_tooltips():
    html = ch.render([_row("agent-platform")], [])
    # every metric label carries hover copy; spot-check the load-bearing claims
    for needle in ("const TIPS", 'data-tip="', "ci.yml", "def test_",
                   "coverage artifact", "health-history.jsonl"):
        assert needle in html, needle
    # the tooltip CSS ships inside the page (stays self-contained)
    assert ".tip::after" in html and "attr(data-tip)" in html


def test_coverage_dash_carries_its_meaning_at_the_point_of_use():
    """A "—" in the Cov% column previously explained itself only in a hover —
    the column-header tooltip and a per-cell `title` attribute, neither of
    which a reader scanning the table without hovering would ever see. The
    fix is a marker that's part of the cell's own text, plus a legend line
    that's on the page unconditionally, not behind a hover
    (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment 903)."""
    html = ch.render([_row("agent-platform")], [])
    assert 'class="cov-gap"' in html
    assert 'id="covLegend"' in html
    assert "Coverage CI step not wired for this repo" in html
    # the marker's visibility is driven by data at render time, not left to a hover
    assert 'document.getElementById("covLegend").hidden = !ROWS.some(r=>r.coverage==null);' in html


def test_coverage_dash_marker_scoped_to_the_null_branch():
    """The false-positive direction: rendering happens client-side (rows are
    serialized JSON, the fmt function is JS source shared by every row), so
    pytest can't observe a per-row DOM. What it CAN pin is that the marker
    sits only in the `v==null` branch of the ternary and the real-coverage
    branch renders through plain `fmtPct(v)` with no marker — asserting only
    that the marker string appears somewhere (as the test above does) would
    pass identically if it had leaked into the non-null branch too."""
    html = ch.render([_row("agent-platform")], [])
    assert ('fmt:v=>v==null?\'— <sup class="cov-gap" aria-hidden="true">'
            '&dagger;</sup>\':fmtPct(v)') in html


# --- freshness (owns the /health <-> /overview stalled agreement) -----------


def test_freshness_stalled_past_30h():
    fresh = ch.freshness("2020-01-01T00:00:00Z")
    assert fresh.stalled is True
    assert fresh.cadence == "daily (07:45 MST)"


def test_freshness_not_stalled_when_recent():
    import orient_time
    fresh = ch.freshness(orient_time.now().isoformat())
    assert fresh.stalled is False


def test_render_shows_stalled_banner_for_an_old_harvest():
    html = ch.render([_row("agent-platform")], [], fresh=ch.freshness("2020-01-01T00:00:00Z"))
    assert 'class="stalled-banner"' in html
    assert "agent-platform" in html  # the banner sits above the data, not instead of it


def test_render_no_stalled_banner_for_a_fresh_harvest():
    import orient_time
    html = ch.render([_row("agent-platform")], [], fresh=ch.freshness(orient_time.now().isoformat()))
    assert 'class="stalled-banner"' not in html


def test_nav_is_styled():
    """After migrating to ds.nav_component, the old topnav class is gone.
    The Atelier nav renders class="atelier-nav" with aria-current="page" on
    the Dashboards primary, and ds tokens (e.g. --brand) replace the old
    per-page --grad literal. Code Health left the Dashboards sub-strip in the
    2026-08-24 trim (the operator's pinned five: Overview/Spending/Velocity/Security/
    Alerts) — it keeps its route and full-tier gating, reachable through the
    grouped Overview index (nav-reshape spec §4), so it no longer has its own
    sub-strip link to assert on here."""
    html = ch.render([_row("agent-platform")], [])
    assert 'class="atelier-nav"' in html          # ds nav rendered
    assert 'href="/overview" aria-current="page"' in html  # Dashboards primary lit
    assert ">Code Health<" not in html             # trimmed from the sub-strip
    assert "--brand" in html                       # ds token present
    assert "topnav" not in html                    # old nav class absent
    assert "--grad" not in html or "var(--grad)" in html  # no old palette literal bare
