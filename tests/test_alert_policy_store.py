"""alert_policy_store.py — the dashboard alert-threshold store
(issue_01M17W6XGXVHNDWCFRTQ38Y2RK).

The load-bearing property this file exists to prove: the threshold Orient
writes and the threshold an evaluator reads are the SAME threshold. Every
"invariant" test below writes through `write()` and then observes an
evaluator's own verdict change for a fixed input — never reading the stored
row back, which would prove storage and nothing about enforcement.
"""
from __future__ import annotations

import pytest

import alert_policy_store
import control_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh control.db on a temp path, schema at head — matching
    test_blog_store.py's own fixture."""
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    yield


# --- get() / write() ----------------------------------------------------------


def test_get_returns_the_shipped_default_when_never_written(db):
    assert alert_policy_store.get("code_health.ci_pass_rate") == {
        "good_min": 0.9, "warn_min": 0.7}
    assert alert_policy_store.get("spending.overage_runs") == {
        "max_runs_in_overage": 0}
    assert alert_policy_store.get("kb.recall") == {"good_min": 0.85, "warn_min": 0.6}


def test_get_rejects_an_unknown_key(db):
    with pytest.raises(ValueError):
        alert_policy_store.get("not-a-real-key")


def test_write_then_get_returns_the_written_value(db):
    alert_policy_store.write(
        "code_health.ci_pass_rate", {"good_min": 0.95, "warn_min": 0.8},
        actor="operator@example.com")
    assert alert_policy_store.get("code_health.ci_pass_rate") == {
        "good_min": 0.95, "warn_min": 0.8}


def test_a_second_write_updates_the_same_row_rather_than_duplicating(db):
    alert_policy_store.write(
        "kb.recall", {"good_min": 0.9, "warn_min": 0.7}, actor="a@example.com")
    alert_policy_store.write(
        "kb.recall", {"good_min": 0.95, "warn_min": 0.75}, actor="b@example.com")
    assert alert_policy_store.get("kb.recall") == {"good_min": 0.95, "warn_min": 0.75}


def test_write_rejects_an_unknown_key(db):
    with pytest.raises(ValueError):
        alert_policy_store.write("not-a-real-key", {"x": 1}, actor="operator@example.com")


def test_write_rejects_a_missing_field(db):
    with pytest.raises(ValueError):
        alert_policy_store.write(
            "code_health.ci_pass_rate", {"good_min": 0.9}, actor="operator@example.com")


def test_write_rejects_an_unexpected_field(db):
    with pytest.raises(ValueError):
        alert_policy_store.write(
            "spending.overage_runs",
            {"max_runs_in_overage": 0, "extra": 1}, actor="operator@example.com")


def test_write_rejects_a_non_numeric_value(db):
    with pytest.raises(ValueError):
        alert_policy_store.write(
            "spending.overage_runs", {"max_runs_in_overage": "zero"},
            actor="operator@example.com")


def test_write_rejects_a_bool_value(db):
    """isinstance(True, int) is True in Python — a bool must not sneak through
    the numeric check (policy_api.py's own type-check-to-reject discipline)."""
    with pytest.raises(ValueError):
        alert_policy_store.write(
            "spending.overage_runs", {"max_runs_in_overage": True},
            actor="operator@example.com")


def test_write_rejects_warn_min_above_good_min(db):
    with pytest.raises(ValueError):
        alert_policy_store.write(
            "kb.recall", {"good_min": 0.5, "warn_min": 0.9}, actor="operator@example.com")


def test_write_rejects_good_min_above_one(db):
    with pytest.raises(ValueError):
        alert_policy_store.write(
            "kb.recall", {"good_min": 1.5, "warn_min": 0.6}, actor="operator@example.com")


def test_write_rejects_a_negative_run_count(db):
    with pytest.raises(ValueError):
        alert_policy_store.write(
            "spending.overage_runs", {"max_runs_in_overage": -1},
            actor="operator@example.com")


def test_write_rejects_a_non_whole_run_count(db):
    with pytest.raises(ValueError):
        alert_policy_store.write(
            "spending.overage_runs", {"max_runs_in_overage": 1.5},
            actor="operator@example.com")


def test_write_rejects_the_all_good_degenerate_rate_pair(db):
    """good_min=0 makes every rate (never negative) register as good, which
    silently disables the alert category rather than merely relaxing it."""
    with pytest.raises(ValueError):
        alert_policy_store.write(
            "kb.recall", {"good_min": 0, "warn_min": 0}, actor="operator@example.com")
    with pytest.raises(ValueError):
        alert_policy_store.write(
            "code_health.ci_pass_rate", {"good_min": 0, "warn_min": 0},
            actor="operator@example.com")


def test_write_rejects_a_non_finite_run_count(db):
    """json.loads accepts bare NaN/Infinity tokens, which must not reach the
    int() conversion as an unhandled ValueError/OverflowError (a clean 400,
    not a 500)."""
    with pytest.raises(ValueError):
        alert_policy_store.write(
            "spending.overage_runs", {"max_runs_in_overage": float("nan")},
            actor="operator@example.com")
    with pytest.raises(ValueError):
        alert_policy_store.write(
            "spending.overage_runs", {"max_runs_in_overage": float("inf")},
            actor="operator@example.com")


# --- the invariant: a write changes what an evaluator computes ----------------


def test_a_write_flips_code_healths_own_verdict_for_the_same_input(db):
    """Not a round-trip through get() — this calls the actual evaluator
    (code_health_build.ci_status) with the freshly-read threshold and shows
    its VERDICT for one fixed rate changes, which is the property that
    matters: a control that stores a value nothing reads changes nothing."""
    import code_health_build as ch

    rate = 0.85
    before = alert_policy_store.get("code_health.ci_pass_rate")
    assert ch.ci_status(rate, before) == "warn"  # under the shipped default

    alert_policy_store.write(
        "code_health.ci_pass_rate", {"good_min": 0.8, "warn_min": 0.5},
        actor="operator@example.com")

    after = alert_policy_store.get("code_health.ci_pass_rate")
    assert ch.ci_status(rate, after) == "good"  # same rate, lowered bar


def test_a_write_flips_whether_alerts_build_fires_for_the_same_snapshot(db):
    """One layer up: the actual /alerts evaluator (alerts_build.
    code_health_alerts) stops firing for a repo whose rate did not change,
    once the threshold it reads is lowered under it."""
    import alerts_build as ab

    rows = [{"repo": "r", "ci_pass_rate_main": 0.85,
            "oldest_open_days": None, "reverts_recent": None}]
    before = alert_policy_store.get("code_health.ci_pass_rate")
    assert ab.code_health_alerts(rows, before) != []

    alert_policy_store.write(
        "code_health.ci_pass_rate", {"good_min": 0.8, "warn_min": 0.5},
        actor="operator@example.com")

    after = alert_policy_store.get("code_health.ci_pass_rate")
    assert ab.code_health_alerts(rows, after) == []


def test_a_write_flips_spendings_own_verdict_for_the_same_run_count(db):
    import dashboard_build as db_mod

    ov = {"runs_reporting": 5, "runs_in_overage": 2}
    before = alert_policy_store.get("spending.overage_runs")
    assert db_mod.overage_status(ov, before) == "bad"  # any overage is bad by default

    alert_policy_store.write(
        "spending.overage_runs", {"max_runs_in_overage": 2}, actor="operator@example.com")

    after = alert_policy_store.get("spending.overage_runs")
    assert db_mod.overage_status(ov, after) == "good"  # 2 is now within the raised bar


def test_a_write_flips_kbs_own_verdict_for_the_same_recall_value(db):
    import kb_build as kb

    val = 0.7
    before = alert_policy_store.get("kb.recall")
    assert kb.cls_recall(val, before) == "warn"  # under the shipped default

    alert_policy_store.write(
        "kb.recall", {"good_min": 0.65, "warn_min": 0.4}, actor="operator@example.com")

    after = alert_policy_store.get("kb.recall")
    assert kb.cls_recall(val, after) == "good"  # same value, lowered bar


def test_a_write_flips_kbs_status_text_in_step_with_its_own_color(db):
    """status_recall is cls_recall's WCAG 1.4.1 text pairing — the two must
    never disagree about the same value after a write, or a cell renders
    green with "below target" text still attached."""
    import kb_build as kb

    val = 0.7
    before = alert_policy_store.get("kb.recall")
    assert kb.cls_recall(val, before) == "warn"
    assert kb.status_recall(val, before) == "△ below target"

    alert_policy_store.write(
        "kb.recall", {"good_min": 0.65, "warn_min": 0.4}, actor="operator@example.com")

    after = alert_policy_store.get("kb.recall")
    assert kb.cls_recall(val, after) == "good"
    assert kb.status_recall(val, after) == "✓ on target"
