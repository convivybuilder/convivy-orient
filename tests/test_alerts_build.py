"""Tests for alerts_build.py — the /alerts dashboard (epic issue_01M0EHRNW5HVJCAXZHYQCBZ3YW).

Pure aggregation only; no live gh/API calls needed (every check reads
already-loaded snapshot data, mirroring how each source dashboard's own
live_html() reads its snapshot). These pin: each category's threshold is the
one its source page already colors by, severity-only alerts fire (good never
does), missing/None values never fire, and render()'s empty/non-empty states.
"""
import alerts_build as ab
import code_health_build as ch
import dashboard_build as db
import kb_build as kb


# --- code_health_alerts -------------------------------------------------------


def test_code_health_alerts_fires_only_on_warn_or_bad():
    rows = [
        {"repo": "healthy", "ci_pass_rate_main": 0.95, "oldest_open_days": 2, "reverts_recent": 0},
        {"repo": "sick", "ci_pass_rate_main": 0.5, "oldest_open_days": 20, "reverts_recent": 1},
    ]
    alerts = ab.code_health_alerts(rows)
    subjects = {(a["subject"], a["metric"]) for a in alerts}
    assert ("healthy", "CI pass rate (main)") not in subjects
    assert ("sick", "CI pass rate (main)") in subjects
    assert ("sick", "Oldest open PR") in subjects
    assert ("sick", "Reverts") in subjects
    for a in alerts:
        assert a["category"] == "Code Health" and a["href"] == "/health"


def test_code_health_alerts_none_values_never_fire():
    rows = [{"repo": "unknown", "ci_pass_rate_main": None, "oldest_open_days": None,
             "reverts_recent": None}]
    assert ab.code_health_alerts(rows) == []


def test_code_health_alerts_uses_the_main_only_rate_not_all_branch():
    # ARCH-179's alert basis is ci_pass_rate_main, not the noisier all-branch rate.
    rows = [{"repo": "busy", "ci_pass_rate": 0.2, "ci_pass_rate_main": 1.0,
             "oldest_open_days": None, "reverts_recent": None}]
    assert ab.code_health_alerts(rows) == []


# --- spending_alert --------------------------------------------------------------


def test_spending_alert_fires_only_when_in_overage():
    assert ab.spending_alert({"overage": {"runs_reporting": 5, "runs_in_overage": 0}}) is None
    a = ab.spending_alert({"overage": {"runs_reporting": 5, "runs_in_overage": 2}})
    assert a is not None and a["category"] == "Spending" and a["severity"] == "bad"
    assert a["href"] == "/spending"


def test_spending_alert_not_reported_is_not_an_alert():
    # zero reporting runs is "no evidence", not "clean" — must not fire either way.
    assert ab.spending_alert({"overage": {"runs_reporting": 0, "runs_in_overage": 0}}) is None
    assert ab.spending_alert({}) is None
    assert ab.spending_alert(None) is None


# --- kb_alerts -------------------------------------------------------------------


def test_kb_alerts_recall_and_mrr_thresholds():
    ev = {"overall": {"recall@1": 0.5, "mrr": 0.9}}
    q = {"empty_rate": None}
    alerts = ab.kb_alerts(ev, q)
    metrics = {a["metric"]: a for a in alerts}
    assert "recall@1" in metrics and metrics["recall@1"]["severity"] == "bad"  # 0.5 < 0.6
    assert "MRR" not in metrics                                                # 0.9 >= 0.85, good
    for a in alerts:
        assert a["category"] == "KB Retrieval" and a["href"] == "/kb"


def test_kb_alerts_no_eval_yet_skips_recall_checks():
    assert ab.kb_alerts(None, {"empty_rate": None}) == []


def test_kb_alerts_empty_rate_threshold():
    alerts = ab.kb_alerts(None, {"empty_rate": 0.3})
    assert len(alerts) == 1 and alerts[0]["metric"] == "Null-result rate"
    assert alerts[0]["severity"] == "bad"


# --- reused-threshold contract: alerts_build calls the source page's own status fn


def test_code_health_thresholds_are_the_source_pages_own_function():
    assert ch.ci_status(0.95) == "good" and ch.ci_status(0.5) == "bad"
    assert ab.code_health_alerts(
        [{"repo": "r", "ci_pass_rate_main": 0.5, "oldest_open_days": None, "reverts_recent": None}]
    )[0]["severity"] == ch.ci_status(0.5)


def test_spending_threshold_is_the_source_pages_own_function():
    ov = {"runs_reporting": 3, "runs_in_overage": 1}
    assert db.overage_status(ov) == "bad"
    assert ab.spending_alert({"overage": ov})["severity"] == db.overage_status(ov)


def test_kb_threshold_is_the_source_pages_own_function():
    assert kb.cls_recall(0.5) == "bad"
    a = ab.kb_alerts({"overall": {"recall@1": 0.5}}, {"empty_rate": None})[0]
    assert a["severity"] == kb.cls_recall(0.5)


# --- gather() isolation ---------------------------------------------------------


def test_gather_isolates_a_broken_source(monkeypatch):
    """One source raising must not blank the whole page — mirrors
    waiting_build.gather's per-tile isolation."""
    def boom():
        raise RuntimeError("snapshot corrupt")
    monkeypatch.setattr(ch, "load_snapshot", boom)
    monkeypatch.setattr(db, "load_latest_snapshot", lambda: {})
    monkeypatch.setattr(kb, "load_eval", lambda: None)
    monkeypatch.setattr(kb, "load_query_stats", lambda: {"empty_rate": None})
    assert ab.gather() == []


def test_gather_sorts_bad_before_warn():
    rows = [
        {"repo": "warny", "ci_pass_rate_main": 0.8, "oldest_open_days": None, "reverts_recent": None},
        {"repo": "baddy", "ci_pass_rate_main": 0.1, "oldest_open_days": None, "reverts_recent": None},
    ]
    alerts = ab.code_health_alerts(rows)
    alerts.sort(key=lambda a: (ab._SEVERITY_ORDER.get(a["severity"], 9), a["category"], a["subject"]))
    assert alerts[0]["severity"] == "bad" and alerts[1]["severity"] == "warn"


# --- render() ----------------------------------------------------------------


def test_render_empty_state():
    html = ab.render([])
    assert "No alerts" in html
    assert 'class="statcard"' not in html


def test_render_shows_cards_and_rows():
    alerts = [ab._alert(category="Code Health", metric="CI pass rate (main)",
                        subject="vivy", severity="bad", value="50%",
                        detail="vivy's CI is bad.", href="/health")]
    html = ab.render(alerts)
    assert html.count('class="statcard"') == 3
    assert "vivy" in html and "CI pass rate (main)" in html
    assert 'href="/health"' in html


def test_render_notes_why_security_and_warrant_are_excluded():
    alerts = [ab._alert(category="Spending", metric="Subscription overage",
                        subject="account-wide", severity="bad", value="1 of 3 runs",
                        detail="in overage", href="/spending")]
    html = ab.render(alerts)
    assert "Security" in html and "Warrant" in html   # named in the caveat text
    assert "not a policy threshold" in html


def test_alerts_has_no_tech_debt_category_or_source(monkeypatch):
    """The tech-debt dashboard was retired (issue_01M2HY3K2SWV10VGZFRWJD34RS):
    its source counted open GitHub issues, which the Library queue replaced,
    so /alerts must carry neither a Tech Debt category nor a tech-debt
    freshness row, in either render state."""
    assert not hasattr(ab, "tech_debt_alerts")
    monkeypatch.setattr(ch, "load_snapshot", lambda: ("2020-01-01T00:00:00Z", [{"repo": "x"}]))
    monkeypatch.setattr(db, "load_latest_snapshot", lambda: {})
    monkeypatch.setattr(kb, "load_eval", lambda: None)
    banners = ab.freshness_banners()
    assert "Tech" not in banners and "tech-debt" not in banners.lower()
    for alerts in ([], [ab._alert(category="Spending", metric="Subscription overage",
                                  subject="account-wide", severity="bad",
                                  value="1 of 3 runs", detail="in overage",
                                  href="/spending")]):
        html = ab.render(alerts, banners=banners)
        assert "Tech Debt" not in html and "/tech-debt" not in html


# --- freshness_banners() (this page shares its three sources' feeds) --------


def test_freshness_banners_fires_when_a_source_is_stalled(monkeypatch):
    monkeypatch.setattr(ch, "load_snapshot", lambda: ("2020-01-01T00:00:00Z", [{"repo": "x"}]))
    monkeypatch.setattr(db, "load_latest_snapshot", lambda: {})
    monkeypatch.setattr(kb, "load_eval", lambda: None)
    assert 'class="stalled-banner"' in ab.freshness_banners()


def test_freshness_banners_silent_when_every_source_is_fresh(monkeypatch):
    import orient_time
    fresh = orient_time.now().isoformat()
    monkeypatch.setattr(ch, "load_snapshot", lambda: (fresh, [{"repo": "x"}]))
    monkeypatch.setattr(db, "load_latest_snapshot", lambda: {"generated_at": fresh})
    monkeypatch.setattr(kb, "load_eval", lambda: {"generated": fresh})
    assert ab.freshness_banners() == ""


def test_freshness_banners_isolates_a_broken_source(monkeypatch):
    """One source raising must not blank the banner strip — mirrors gather()'s
    per-source isolation."""
    def boom():
        raise RuntimeError("snapshot corrupt")
    monkeypatch.setattr(ch, "load_snapshot", boom)
    monkeypatch.setattr(db, "load_latest_snapshot", lambda: {})
    monkeypatch.setattr(kb, "load_eval", lambda: None)
    assert ab.freshness_banners() == ""


def test_render_embeds_freshness_banners():
    html = ab.render([], banners='<div class="stalled-banner">boo</div>')
    assert 'class="stalled-banner"' in html
