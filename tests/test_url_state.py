"""Tests for URL-persisted view state (ARCH-216 Phase 4, PR 3).

Asserts the shared helper API in design_system.URL_STATE_JS and the per-page
embedding in each dashboard. JS behavior is verified at the markup layer: we
confirm the helper primitives and param names are present in each page's output.
"""
import json

import design_system as ds
import dashboard_build as db
import velocity_build as vb
import code_health_build as ch


# ---------------------------------------------------------------------------
# Shared helper: URL_STATE_JS
# ---------------------------------------------------------------------------


def test_url_state_js_exists_and_nonempty():
    assert hasattr(ds, "URL_STATE_JS")
    assert len(ds.URL_STATE_JS) > 100


def test_url_state_js_has_read_write_on():
    js = ds.URL_STATE_JS
    assert "readUrlState" in js
    assert "writeUrlState" in js
    assert "onUrlState" in js


def test_url_state_js_assigns_window_ds():
    assert "window._ds" in ds.URL_STATE_JS


def test_url_state_js_uses_history_api():
    js = ds.URL_STATE_JS
    assert "history.replaceState" in js
    assert "history.pushState" in js


def test_url_state_js_uses_url_search_params():
    assert "URLSearchParams" in ds.URL_STATE_JS


def test_url_state_js_subscribes_to_popstate():
    assert "popstate" in ds.URL_STATE_JS


def test_page_shell_embeds_url_state_js():
    html = ds.page_shell(title="T", nav_html="", body="")
    assert "readUrlState" in html
    assert "writeUrlState" in html
    assert "onUrlState" in html
    assert "window._ds" in html


# ---------------------------------------------------------------------------
# Spending dashboard
# ---------------------------------------------------------------------------


def _spending_html(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "COST_DIR", tmp_path)
    (tmp_path / "latest.json").write_text(json.dumps({
        "date": "2026-06-20", "generated_at": "2026-06-20T12:00:00",
        "total": 5.0, "anthropic_total": 5.0, "gateway_total": 0.0,
        "apps_total": 0.0, "fable": 0.0,
        "by_model": [{"model": "claude-opus-4-8", "cost": 5.0, "kind": "est"}],
    }))
    (tmp_path / "history.jsonl").write_text(
        json.dumps({"date": "2026-06-20", "total": 5.0, "anthropic_total": 5.0,
                    "gateway_total": 0.0, "apps_total": 0.0, "fable": 0.0,
                    "by_model": [{"model": "claude-opus-4-8", "cost": 5.0, "kind": "est"}]}) + "\n"
    )
    return db.cost_panel_html()


def test_spending_embeds_url_state_functions(tmp_path, monkeypatch):
    html = _spending_html(tmp_path, monkeypatch)
    assert "currentStateToParams" in html
    assert "applyUrlState" in html
    assert "_ds.readUrlState" in html
    assert "_ds.onUrlState" in html


def test_spending_url_param_names(tmp_path, monkeypatch):
    html = _spending_html(tmp_path, monkeypatch)
    assert "p.win" in html
    assert "p.day" in html
    assert "p.view" in html


def test_spending_writes_url_on_handlers(tmp_path, monkeypatch):
    html = _spending_html(tmp_path, monkeypatch)
    assert "_ds.writeUrlState(currentStateToParams(),false)" in html


def test_spending_init_and_subscribe(tmp_path, monkeypatch):
    html = _spending_html(tmp_path, monkeypatch)
    assert "_ds.writeUrlState(currentStateToParams(),true)" in html
    assert "_ds.onUrlState(function(s)" in html


# ---------------------------------------------------------------------------
# Cost-Performance dashboard
# ---------------------------------------------------------------------------


def test_cost_perf_embeds_url_state_functions():
    html = db.cost_performance_render([])
    assert "currentStateToParams" in html
    assert "applyUrlState" in html
    assert "_ds.readUrlState" in html
    assert "_ds.onUrlState" in html


def test_cost_perf_url_param_names():
    html = db.cost_performance_render([])
    for name in ("p.win", "p.group", "p.product", "p.provider", "p.view", "p.sort", "p.dir"):
        assert name in html, f"Missing param: {name}"


def test_cost_perf_writes_url_on_handlers():
    html = db.cost_performance_render([])
    assert "_ds.writeUrlState(currentStateToParams(),false)" in html


def test_cost_perf_init_normalize_subscribe():
    html = db.cost_performance_render([])
    assert "_ds.writeUrlState(currentStateToParams(),true)" in html
    assert "_ds.onUrlState(function(s)" in html


def test_cost_perf_reflect_chips_function():
    html = db.cost_performance_render([])
    assert "reflectChips" in html


# ---------------------------------------------------------------------------
# Velocity dashboard
# ---------------------------------------------------------------------------


def _velocity_html(tmp_path, monkeypatch):
    p = tmp_path / "velocity.json"
    p.write_text(json.dumps({
        "generated_at": "2026-06-20T12:00:00",
        "bucket_order": ["Vivy"],
        "notes": [],
        "windows": {"30": {"total": {"commits": 5, "prs_merged": 3, "features": 1,
                                      "loc_net": 100, "reverts": 0, "adrs": 0, "arch": 0,
                                      "change_failure_rate": 0.0, "ci_first_pass_pct": 100.0,
                                      "survival_pct": None},
                            "buckets": {"Vivy": {"commits": 5, "prs_merged": 3, "features": 1,
                                                  "loc_net": 100, "reverts": 0, "adrs": 0, "arch": 0,
                                                  "change_failure_rate": 0.0, "ci_first_pass_pct": 100.0,
                                                  "survival_pct": None}}}},
    }))
    monkeypatch.setattr(vb, "DATA_PATH", p)
    return vb.render(vb.load())


def test_velocity_embeds_url_state_functions(tmp_path, monkeypatch):
    html = _velocity_html(tmp_path, monkeypatch)
    assert "currentStateToParams" in html
    assert "applyUrlState" in html
    assert "_ds.readUrlState" in html
    assert "_ds.onUrlState" in html


def test_velocity_url_param_names(tmp_path, monkeypatch):
    html = _velocity_html(tmp_path, monkeypatch)
    assert "p.win" in html


def test_velocity_writes_url_on_handler(tmp_path, monkeypatch):
    html = _velocity_html(tmp_path, monkeypatch)
    assert "_ds.writeUrlState(currentStateToParams(),false)" in html


def test_velocity_init_normalize_subscribe(tmp_path, monkeypatch):
    html = _velocity_html(tmp_path, monkeypatch)
    assert "_ds.writeUrlState(currentStateToParams(),true)" in html
    assert "_ds.onUrlState(function(s)" in html


# ---------------------------------------------------------------------------
# Health dashboard
# ---------------------------------------------------------------------------


def _health_row(repo="vivy"):
    return {"repo": repo, "kind": "product", "ci_pass_rate": 0.9, "ci_last": "success",
            "ci_last_date": "2026-06-20", "ci_pass_rate_main": 1.0, "ci_last_main": "success",
            "open_prs": 2, "oldest_open_days": 3, "merged_recent": 5,
            "reverts_recent": 0, "test_count": 50, "coverage": None}


def test_health_embeds_url_state_functions():
    html = ch.render([_health_row()], [])
    assert "currentStateToParams" in html
    assert "applyUrlState" in html
    assert "_ds.readUrlState" in html
    assert "_ds.onUrlState" in html


def test_health_url_param_names():
    html = ch.render([_health_row()], [])
    for name in ("p.kind", "p.view", "p.sort", "p.dir"):
        assert name in html, f"Missing param: {name}"


def test_health_writes_url_on_handlers():
    html = ch.render([_health_row()], [])
    assert "_ds.writeUrlState(currentStateToParams(),false)" in html


def test_health_init_normalize_subscribe():
    html = ch.render([_health_row()], [])
    assert "_ds.writeUrlState(currentStateToParams(),true)" in html
    assert "_ds.onUrlState(function(s)" in html
