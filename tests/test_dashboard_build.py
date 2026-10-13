"""Tests for dashboard_build.py — the cost+quality dashboard's data layer.

The risk: the normalizer flattens three heterogeneous ledgers into one fact
shape, and a wrong provider/billing/week derivation or a botched judge join would
silently mis-slice the dashboard. The client-side pivot is exercised by the live
page; these pin the Python normalization.
"""
import json
import re

import dashboard_build as db


def test_provider_of():
    assert db.provider_of("deepseek/deepseek-chat") == "deepseek"
    assert db.provider_of("claude-sonnet-4-6") == "anthropic"
    assert db.provider_of(None) == "—"
    assert db.provider_of("some-other") == "other"


def test_canon_product_reports_the_three_nyh_projects_separately():
    """ARCH-426 amendment: the NYH repos are three projects, each reporting for
    itself. This table MIRRORS agent-platform's cost_join._NYH_DISPLAY — if the two
    drift, the scorecard and /activity disagree about the same run."""
    assert db._canon_product("nyh-arch-tools") == "NYH Arch Tools"
    assert db._canon_product("nyh-etc") == "East Texas Circle"
    assert db._canon_product("nyh-park-villa") == "Park Villa"
    assert db._canon_product("nyh-architecture") == "NYH Arch Tools"
    # a fourth property must appear as itself, not be swallowed by a prefix match
    assert db._canon_product("nyh-somewhere-new") == "nyh-somewhere-new"
    # the Hollis fold is unchanged — three tags across the ARCH-290 rename, one row
    assert db._canon_product("hollis") == "Hollis"
    assert db._canon_product("personal-assistant") == "Hollis"
    assert db._canon_product("pa") == "Hollis"
    assert db._canon_product("vivy") == "vivy"
    assert db._canon_product(None) is None


def test_billing_of():
    assert db.billing_of("provider_api:deepseek") == "metered"
    assert db.billing_of("subscription_credit") == "subscription"
    assert db.billing_of(None) == "—"


def test_week_is_monday_anchored():
    # 2026-06-03 is a Wednesday → week start Monday 2026-06-01
    assert db._week("2026-06-03") == "2026-06-01"
    assert db._week("2026-06-01") == "2026-06-01"  # already Monday
    assert db._week(None) == ""


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_normalize_joins_judge_and_maps_dimensions(tmp_path, monkeypatch):
    sc = tmp_path / "scorecard.jsonl"
    pa = tmp_path / "personal-assistant.jsonl"
    jg = tmp_path / "judge.jsonl"
    _write(sc, [{
        "run_id": "run-1", "date": "2026-06-03", "agent": "bug-fixer",
        "model": "deepseek/deepseek-chat", "billing_source": "provider_api:deepseek",
        "repo": "vivy", "cost_usd": 0.02, "pr_state": "MERGED", "merged": True,
        "survived": True, "ci_first_pass": True, "pr_number": 5,
    }])
    _write(pa, [{
        "run_id": "personal-assistant#9", "date": "2026-06-02", "agent": "personal-assistant",
        "model": None, "repo": "personal-assistant", "cost_usd": None, "pr_state": "MERGED",
        "merged": True, "survived": None, "ci_first_pass": True, "pr_number": 9,
    }])
    _write(jg, [{"run_id": "run-1", "review_thoroughness": 4, "fix_quality": 5}])
    monkeypatch.setattr(db, "SCORECARD", sc)
    monkeypatch.setattr(db, "PA_LEDGER", pa)
    monkeypatch.setattr(db, "JUDGE", jg)

    facts = db.normalize()
    assert len(facts) == 2
    f = next(x for x in facts if x["pr_number"] == 5)
    assert f["product"] == "vivy" and f["provider"] == "deepseek" and f["billing"] == "metered"
    assert f["week"] == "2026-06-01" and f["resolved"] is True
    assert f["review_score"] == 4 and f["fix_score"] == 5  # judge joined by run_id

    pa_fact = next(x for x in facts if x["product"] == "Hollis")
    assert pa_fact["provider"] == "—" and pa_fact["cost_usd"] is None  # PA: no model/cost
    assert pa_fact["source"] == "github-pr"


def test_normalize_tolerates_missing_ledgers(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "SCORECARD", tmp_path / "absent.jsonl")
    monkeypatch.setattr(db, "PA_LEDGER", tmp_path / "absent2.jsonl")
    monkeypatch.setattr(db, "JUDGE", tmp_path / "absent3.jsonl")
    assert db.normalize() == []


def test_render_is_self_contained_and_substitutes(tmp_path, monkeypatch):
    facts = [{"product": "vivy", "agent": "coder", "model": "claude-sonnet-4-6",
              "provider": "anthropic", "billing": "subscription", "date": "2026-06-03",
              "week": "2026-06-01", "cost_usd": 0.1, "resolved": True, "merged": True,
              "survived": None, "ci_first_pass": True, "review_score": None,
              "fix_score": None, "pr_number": 1, "source": "run"}]
    html = db.cost_performance_render(facts)
    assert "__DATA__" not in html and "__ASOF__" not in html and "__NFACTS__" not in html
    assert "const FACTS = [" in html
    # the embedded data round-trips
    blob = html.split("const FACTS = ", 1)[1].split(";\n", 1)[0]
    assert json.loads(blob)[0]["product"] == "vivy"


def test_render_has_metric_tooltips():
    html = db.cost_performance_render([])
    # every metric label carries hover copy; spot-check the load-bearing claims
    for needle in ("const TIPS", 'data-tip="', "7-day survival window",
                   "scorecard.jsonl", "judge.jsonl", "per-run telemetry"):
        assert needle in html, needle
    # the tooltip CSS ships inside the page (stays self-contained)
    assert ".tip::after" in html and "attr(data-tip)" in html
    # API-rate and Billed split tooltips present
    assert "TIPS.apiRate" in html and "TIPS.billed" in html
    assert "API list price" in html and "flat plan" in html


def test_cost_panel_no_snapshot(tmp_path, monkeypatch):
    # missing latest.json → graceful placeholder, no crash
    monkeypatch.setattr(db, "COST_DIR", tmp_path)
    html = db.cost_panel_html()
    assert "Spend — all sessions" in html and "No cost snapshot yet" in html


def test_cost_panel_tooltip_does_not_point_at_slack(tmp_path, monkeypatch):
    # The operator stopped reading Slack 2026-07-21; the tooltip used to end "also
    # posted to #agent-cost", which reaches nobody.
    monkeypatch.setattr(db, "COST_DIR", tmp_path)
    (tmp_path / "latest.json").write_text(json.dumps({
        "date": "2026-06-11", "generated_at": "2026-06-11T15:52:00",
        "total": 1.0, "anthropic_total": 1.0, "gateway_total": 0, "apps_total": 0, "fable": 0,
    }))
    html = db.cost_panel_html()
    assert "#agent-cost" not in html
    assert "Refreshed every 4h by" in html   # the rest of the tooltip is intact


def test_cost_panel_embeds_by_model_history_and_controls(tmp_path, monkeypatch):
    # The panel is now client-rendered (ARCH-168): it embeds the by-model history
    # as JSON plus a window/view selector, and the browser does the windowed
    # aggregation. Assert the embedded data + controls, not server-rendered $.
    monkeypatch.setattr(db, "COST_DIR", tmp_path)
    (tmp_path / "latest.json").write_text(json.dumps({
        "date": "2026-06-11", "generated_at": "2026-06-11T15:52:00",
        "total": 469.61, "anthropic_total": 469.48, "gateway_total": 0.06,
        "apps_total": 0.07, "fable": 318.17,
        "by_model": [{"model": "claude-fable-5", "cost": 318.17, "kind": "est"}],
    }))
    # history rows carrying by_model are used directly (no fallback to latest)
    (tmp_path / "history.jsonl").write_text(
        json.dumps({"date": "2026-06-10", "total": 12.0, "anthropic_total": 12.0,
                    "gateway_total": 0, "apps_total": 0, "fable": 0,
                    "by_model": [{"model": "claude-opus-4-8", "cost": 12.0, "kind": "est"}]}) + "\n"
        + json.dumps({"date": "2026-06-11", "total": 469.61, "anthropic_total": 469.48,
                      "gateway_total": 0.06, "apps_total": 0.07, "fable": 318.17,
                      "by_model": [{"model": "claude-fable-5", "cost": 318.17, "kind": "est"},
                                   {"model": "claude-opus-4-8", "cost": 151.31, "kind": "est"}]}) + "\n"
    )
    html = db.cost_panel_html()
    # window + view controls, default 1d (the 1d button is the pressed default;
    # the client also initializes `let win=1` and applyUrlState falls back to 1d)
    assert 'id="cp-window"' in html and 'data-d="1"' in html and 'data-d="90"' in html
    assert 'data-d="1" aria-pressed="true"' in html
    assert 'data-d="7" aria-pressed="false"' in html
    assert "let win=1," in html
    assert 'data-v="breakdown"' in html and 'data-v="trends"' in html
    # embedded history carries both days' by-model detail (Opus surfaces here)
    assert "claude-opus-4-8" in html and "claude-fable-5" in html
    assert "469.61" in html and "318.17" in html
    # header frames the windowing + that Opus only surfaces in this view
    assert "Opus" in html and "window" in html.lower()
    # cost-per-day average renders beside the headline so windows compare (1d vs 30d);
    # divides by ndays/rows.length (days actually covered), shown only when >1 day
    assert "/day avg" in html and "a.tot.total/ndays" in html
    # API-rate and Billed split: both StatCard labels present; trends legend labeled
    assert "API-rate" in html and "Billed" in html
    assert "Daily API-rate total" in html
    # Billed card shows % of API-rate delta
    assert "% of API-rate" in html


def test_cost_panel_has_day_stepper_on_1d(tmp_path, monkeypatch):
    # On the 1d/today preset the panel shows prev/next arrows to step through
    # COST_HIST one calendar day at a time (client-side; the full history is
    # already embedded). The day-nav ships hidden in the static HTML; applyUrlState
    # reveals it on load now that 1d is the default (and on any later 1d click).
    monkeypatch.setattr(db, "COST_DIR", tmp_path)
    (tmp_path / "latest.json").write_text(json.dumps({
        "date": "2026-06-20", "generated_at": "2026-06-20T15:52:00",
        "total": 9.0, "anthropic_total": 9.0, "gateway_total": 0,
        "apps_total": 0, "fable": 0,
        "by_model": [{"model": "claude-opus-4-8", "cost": 9.0, "kind": "est"}],
    }))
    (tmp_path / "history.jsonl").write_text(
        json.dumps({"date": "2026-06-19", "total": 5.0, "anthropic_total": 5.0,
                    "gateway_total": 0, "apps_total": 0, "fable": 0,
                    "by_model": [{"model": "claude-opus-4-8", "cost": 5.0, "kind": "est"}]}) + "\n"
        + json.dumps({"date": "2026-06-20", "total": 9.0, "anthropic_total": 9.0,
                      "gateway_total": 0, "apps_total": 0, "fable": 0,
                      "by_model": [{"model": "claude-opus-4-8", "cost": 9.0, "kind": "est"}]}) + "\n"
    )
    html = db.cost_panel_html()
    # the day-nav UI exists, hidden until 1d is picked
    assert 'id="cp-daynav"' in html and 'display:none' in html
    assert 'id="cp-prev"' in html and 'id="cp-next"' in html and 'id="cp-daylabel"' in html
    # stepping logic: a dayIdx into COST_HIST, used by rowsForWin on the 1d window
    assert "dayIdx" in html
    assert "if(win===1){ const r=COST_HIST[dayIdx]; return r?[r]:[]; }" in html
    # picking 1d resets to the newest day and reveals the nav; the arrows are bounded
    assert 'if(win===1){ dayIdx=COST_HIST.length-1;' in html
    assert "if(dayIdx>0){ dayIdx--;" in html and "if(dayIdx<COST_HIST.length-1){ dayIdx++;" in html


def test_cost_panel_falls_back_to_latest_when_history_lacks_by_model(tmp_path, monkeypatch):
    # pre-ARCH-168 history rows hold totals but no by_model; the panel falls back
    # to a one-day series from latest.json so the breakdown still has detail.
    monkeypatch.setattr(db, "COST_DIR", tmp_path)
    (tmp_path / "latest.json").write_text(json.dumps({
        "date": "2026-06-11", "generated_at": "2026-06-11T15:52:00",
        "total": 9.0, "anthropic_total": 8.0, "gateway_total": 1.0,
        "apps_total": 0.0, "fable": 0.0,
        "by_model": [{"model": "claude-opus-4-8", "cost": 8.0, "kind": "est"}],
    }))
    (tmp_path / "history.jsonl").write_text(
        json.dumps({"date": "2026-06-10", "total": 5.0, "fable": 0}) + "\n")  # no by_model
    html = db.cost_panel_html()
    assert "claude-opus-4-8" in html  # came from the latest.json fallback


def _make_history_jsonl(tmp_path, rows):
    (tmp_path / "history.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )


def test_spending_billed_split_subscription_is_zero(tmp_path, monkeypatch):
    """A subscription row (kind=est) must show $0 Billed; a gateway row (kind=actual)
    shows its cost as Billed. Billed total = gateway_total + apps_total."""
    monkeypatch.setattr(db, "COST_DIR", tmp_path)
    (tmp_path / "latest.json").write_text(json.dumps({
        "date": "2026-06-21", "generated_at": "2026-06-21T12:00:00",
        "total": 10.13, "anthropic_total": 10.0, "gateway_total": 0.10,
        "apps_total": 0.03, "fable": 0.0,
        "by_model": [],
    }))
    _make_history_jsonl(tmp_path, [{
        "date": "2026-06-21",
        "total": 10.13, "anthropic_total": 10.0, "gateway_total": 0.10,
        "apps_total": 0.03, "fable": 0.0,
        "by_model": [
            {"model": "claude-opus-4-8", "cost": 10.0, "kind": "est"},    # subscription → $0 billed
            {"model": "deepseek/deepseek-chat", "cost": 0.10, "kind": "actual"},  # gateway → billed
            {"model": "app/sous-chef", "cost": 0.03, "kind": "api"},      # direct API → billed
        ],
    }])
    html = db.cost_panel_html()
    # API-rate and Billed StatCard labels present
    assert "API-rate" in html and "Billed" in html
    # The JS agg code uses e.billed which is 0 for "est" and m.cost for "actual"/"api"
    assert 'if(m.kind==="actual"||m.kind==="api") e.billed+=m.cost;' in html
    # billedTotal = gateway_total + apps_total (not anthropic_total)
    assert "billedTotal=tot.gateway_total+tot.apps_total" in html
    # Both cost columns in the breakdown table
    assert 'data-label="API-rate"' in html and 'data-label="Billed"' in html
    # Totals footer row present
    assert 'class="tfoot"' in html


def test_spending_api_rate_vs_billed_labels_on_two_columns(tmp_path, monkeypatch):
    """The breakdown table emits two cost columns (API-rate, Billed) and a footer total."""
    monkeypatch.setattr(db, "COST_DIR", tmp_path)
    (tmp_path / "latest.json").write_text(json.dumps({
        "date": "2026-06-21", "generated_at": "2026-06-21T12:00:00",
        "total": 5.0, "anthropic_total": 4.9, "gateway_total": 0.1,
        "apps_total": 0.0, "fable": 0.0, "by_model": [],
    }))
    _make_history_jsonl(tmp_path, [{
        "date": "2026-06-21",
        "total": 5.0, "anthropic_total": 4.9, "gateway_total": 0.1, "apps_total": 0.0, "fable": 0.0,
        "by_model": [
            {"model": "claude-opus-4-8", "cost": 4.9, "kind": "est"},
            {"model": "deepseek/deepseek-chat", "cost": 0.1, "kind": "actual"},
        ],
    }])
    html = db.cost_panel_html()
    assert 'data-label="API-rate"' in html
    assert 'data-label="Billed"' in html
    # model-sub shows source context (subscription / via gateway / direct API)
    assert "subscription" in html and "via gateway" in html
    # Trends legend is labeled "API-rate"
    assert "Daily API-rate total" in html


def test_cost_perf_chips_are_buttons_with_aria_pressed():
    html = db.cost_performance_render([])
    # Filter chips must be <button> elements so keyboard users can activate them.
    assert '<button type="button" class="chip"' in html
    assert 'aria-pressed="false"' in html
    # Old span-only chips must be gone.
    assert '<span class="chip"' not in html


def test_cp_view_tabs_have_aria_controls():
    html = db.cost_performance_render([])
    assert 'aria-controls="tbl"' in html
    assert 'aria-controls="trendwrap"' in html


def test_spending_tab_has_aria_controls_and_tabpanel(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "COST_DIR", tmp_path)
    (tmp_path / "latest.json").write_text('{"date":"2026-06-21","generated_at":"2026-06-21T12:00:00","total":0,"anthropic_total":0,"gateway_total":0,"apps_total":0,"fable":0,"by_model":[]}')
    (tmp_path / "history.jsonl").write_text('{"date":"2026-06-21","total":0,"anthropic_total":0,"gateway_total":0,"apps_total":0,"fable":0,"by_model":[]}\n')
    html = db.cost_panel_html()
    assert 'aria-controls="cp-body"' in html
    assert 'role="tabpanel"' in html
    assert 'aria-labelledby="cp-tab-breakdown"' in html


def test_cost_perf_th_has_tabindex(tmp_path, monkeypatch):
    html = db.cost_performance_render([])
    # Sortable table headers need tabindex="0" for keyboard sort activation.
    assert 'tabindex="0"' in html


def test_cost_per_outcome_has_exclude_and_rollup(monkeypatch):
    """ARCH-245: the cost-per-outcome view lets the viewer exclude a single run
    (e.g. a one-off runaway) and recomputes a by-model/agent cost-per-PR rollup
    live, so one outlier doesn't condemn a model."""
    snap = {"outcome_count": 1, "generated_at": "2026-06-24T00:00:00Z", "outcomes": [
        {"agent": "refactorer", "model": "moonshot/kimi-k2.7-code", "product": "vivy",
         "pr_number": 1, "pr_url": "", "date": "2026-06-24T00:00:00Z",
         "chain_usd": 23.49, "node_usd": 23.49, "node_run_id": "run-AAA"}]}
    monkeypatch.setattr(db, "_load_outcomes", lambda: snap)
    html = db.cost_performance_render([])
    # the per-run exclude control + restore affordance
    assert 'class="oc-ex' in html and 'data-rid="' in html
    assert 'st.excluded' in html and 'id="oc-clear-ex"' in html
    # the recomputing rollup + its group-by control
    assert 'id="oc-rollup"' in html and 'id="oc-groupby"' in html
    assert 'Avg $/PR' in html
    assert 'function rollup(' in html and 'function kept(' in html
    # the synthetic run's stable id is embedded so it can be excluded client-side
    assert 'run-AAA' in html


def test_cost_performance_billed_split(tmp_path, monkeypatch):
    """Cost-performance aggregate: metered billing rows contribute to billedUsd;
    subscription and '—' rows contribute $0. Two-column table headers present."""
    monkeypatch.setattr(db, "SCORECARD", tmp_path / "absent.jsonl")
    monkeypatch.setattr(db, "PA_LEDGER", tmp_path / "absent2.jsonl")
    monkeypatch.setattr(db, "JUDGE", tmp_path / "absent3.jsonl")

    # metered fact (gateway deepseek)
    metered_fact = {
        "product": "vivy", "agent": "coder", "model": "deepseek/deepseek-chat",
        "provider": "deepseek", "billing": "metered", "date": "2026-06-21",
        "week": "2026-06-16", "cost_usd": 0.05, "resolved": True, "merged": True,
        "survived": None, "ci_first_pass": True, "review_score": None,
        "fix_score": None, "pr_number": 10, "source": "run",
    }
    # subscription fact (Anthropic claude)
    subscription_fact = {
        "product": "vivy", "agent": "reviewer", "model": "claude-sonnet-4-6",
        "provider": "anthropic", "billing": "subscription", "date": "2026-06-21",
        "week": "2026-06-16", "cost_usd": 0.20, "resolved": True, "merged": True,
        "survived": None, "ci_first_pass": True, "review_score": None,
        "fix_score": None, "pr_number": 11, "source": "run",
    }

    html = db.cost_performance_render([metered_fact, subscription_fact])

    # Both column headers present in the table
    assert "API-rate $" in html and "Billed $" in html
    # StatCard labels present
    assert "API-rate $" in html and "Billed $" in html
    # The JS aggregate code accumulates billedUsd only for metered rows
    assert 'if(f.billing==="metered") g.billedUsd+=f.cost_usd;' in html
    # TIPS entries for the split
    assert "TIPS.apiRate" in html and "TIPS.billed" in html
    # Trends panels include both API-rate $ and Billed $ sparklines
    assert '"API-rate $"' in html and '"Billed $"' in html


# --- the subscription-quota line on /spending -------------------------------
#
# Every dollar on that page is an API-rate ESTIMATE against subscription usage
# that is mostly inside the plan, so none of them says whether money left the
# account. This line is the part that does, and its three states matter more
# than its wording.

def test_overage_line_says_not_reported_when_the_day_is_silent():
    """The state that must NOT read as reassurance. A day with no runs and a day
    whose runs never emitted a rate-limit event look identical from here, and
    neither is evidence of having stayed inside the plan."""
    html = db.overage_line_html({})
    assert "<b>not reported</b>" in html
    # The tooltip says the words "inside the plan" while REFUSING that reading,
    # so assert on the visible label rather than the whole string.
    assert "<b>inside the plan</b>" not in html
    assert "<b>in overage</b>" not in html


def test_overage_line_reports_a_day_inside_the_plan():
    html = db.overage_line_html(
        {"overage": {"runs_reporting": 40, "runs_in_overage": 0,
                     "peak_utilization": 0.42, "worst_status": "allowed"}})
    assert "inside the plan" in html and "40 runs" in html
    assert "42% full" in html
    assert "good" in html


def test_overage_line_reports_a_day_in_overage_with_the_count():
    """The fraction is the useful number: one run in overage out of forty is a
    different day from thirty."""
    html = db.overage_line_html(
        {"overage": {"runs_reporting": 40, "runs_in_overage": 7,
                     "peak_utilization": 1.0, "worst_status": "rejected"}})
    assert "in overage" in html and "7 of 40" in html
    assert "requests were turned away" in html
    assert "bad" in html


def test_overage_line_says_the_figure_is_account_wide():
    """A per-account flag sitting beside per-run dollar figures invites being read
    as per-run attribution. The label has to refuse that reading out loud."""
    html = db.overage_line_html(
        {"overage": {"runs_reporting": 1, "runs_in_overage": 1}})
    assert "account-wide" in html
    assert "not per-run" in html


def test_overage_line_survives_a_malformed_rollup():
    """It renders from a JSON snapshot written by another process; a bad value
    must not take the whole spending page down."""
    html = db.overage_line_html(
        {"overage": {"runs_reporting": "x", "runs_in_overage": None,
                     "peak_utilization": "not-a-number"}})
    assert "Subscription quota" in html


def test_a_rollup_with_zero_reporting_runs_is_not_reported_not_reassurance():
    """The three-state rule's own loophole. `overage_rollup` returns {} at zero, so
    this shape should never arrive — but a malformed or future producer could send
    it, and "inside the plan — 0 runs reported" in green is exactly the
    reassurance the rule exists to withhold (reviewer, #150)."""
    for shape in ({"runs_reporting": 0, "runs_in_overage": 0},
                  {"runs_reporting": 0, "runs_in_overage": 0, "peak_utilization": 0.0},
                  {"runs_reporting": "0"},
                  {"runs_in_overage": 3}):          # no runs_reporting at all
        html = db.overage_line_html({"overage": shape})
        assert "<b>not reported</b>" in html, shape
        assert "<b>inside the plan</b>" not in html, shape


# --- overage_status (reused by alerts_build.py's /alerts evaluator) ----------


def test_overage_status_matches_the_overage_line_html_verdict():
    ov = {"runs_reporting": 40, "runs_in_overage": 0}
    assert db.overage_status(ov) == "good"
    assert "inside the plan" in db.overage_line_html({"overage": ov})

    ov = {"runs_reporting": 40, "runs_in_overage": 7}
    assert db.overage_status(ov) == "bad"
    assert "in overage" in db.overage_line_html({"overage": ov})


def test_overage_status_not_reported_is_none_not_good():
    assert db.overage_status(None) is None
    assert db.overage_status({}) is None
    assert db.overage_status({"runs_reporting": 0, "runs_in_overage": 0}) is None


def test_load_latest_snapshot_missing_and_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "COST_DIR", tmp_path)
    assert db.load_latest_snapshot() == {}
    (tmp_path / "latest.json").write_text("not json")
    assert db.load_latest_snapshot() == {}
    (tmp_path / "latest.json").write_text(json.dumps({"total": 1.5}))
    assert db.load_latest_snapshot() == {"total": 1.5}


# --- spending_freshness (owns the /spending <-> /overview stalled agreement) -


def test_spending_freshness_stalled_past_30h():
    fresh = db.spending_freshness({"generated_at": "2020-01-01T00:00:00Z"})
    assert fresh.stalled is True
    assert fresh.cadence == "daily (07:40 MST)"


def test_spending_freshness_not_stalled_when_recent():
    import orient_time
    fresh = db.spending_freshness({"generated_at": orient_time.now().isoformat()})
    assert fresh.stalled is False


def test_spending_render_shows_stalled_banner_for_an_old_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "COST_DIR", tmp_path)
    (tmp_path / "latest.json").write_text(json.dumps({"generated_at": "2020-01-01T00:00:00Z", "total": 1.5}))
    html = db.spending_render()
    assert 'class="stalled-banner"' in html


def test_spending_render_no_stalled_banner_for_a_fresh_snapshot(tmp_path, monkeypatch):
    import orient_time
    monkeypatch.setattr(db, "COST_DIR", tmp_path)
    (tmp_path / "latest.json").write_text(
        json.dumps({"generated_at": orient_time.now().isoformat(), "total": 1.5}))
    html = db.spending_render()
    assert 'class="stalled-banner"' not in html


# --- cost_performance_freshness (deliberately never stalled) -----------------


def test_cost_performance_freshness_never_stalled_even_when_the_only_date_is_old():
    """Checked: the field this dashboard ages against (`date`) is calendar-day
    granularity at the source and event-driven (a merged PR), so an old date
    means "nothing merged lately," not "the harvest stopped" — see
    `cost_performance_freshness`'s docstring. No threshold ever fires."""
    fresh = db.cost_performance_freshness([{"date": "2020-01-01"}])
    assert fresh.stalled is None
    assert fresh.cadence is None
    assert "2020-01-01" in fresh.text


def test_cost_performance_render_never_shows_a_stalled_banner():
    html = db.cost_performance_render([{"product": "vivy", "agent": "coder", "model": "—",
                                        "provider": "—", "billing": "—", "date": "2020-01-01",
                                        "week": "2019-12-30", "cost_usd": None, "resolved": True,
                                        "merged": True, "survived": None, "ci_first_pass": None,
                                        "review_score": None, "fix_score": None,
                                        "pr_number": 1, "source": "run"}])
    assert 'class="stalled-banner"' not in html
    assert "2020-01-01" in html


# --- the cost-detail layout (2026-09-24) ------------------------------------
# The operator reported /spending "quite broken". Rendered in headless Chromium: the Fable
# flag took a flex column of its own and squeezed the table beside it, and a
# phone drew two share bars per row because nothing hid either one.


def _css_rule(css, selector):
    import re
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"no rule for {selector}"
    return m.group(1)


def test_cost_detail_stacks_rather_than_sitting_in_a_flex_row():
    rule = _css_rule(db._SPENDING_CSS, ".costgrid")
    assert "display:block" in rule and "flex" not in rule
    assert "width:100%" in _css_rule(db._SPENDING_CSS, ".costtbl")


def test_exactly_one_share_bar_shows_at_each_width():
    css = db._SPENDING_CSS
    assert "display:none" in _css_rule(css, ".costtbl .m-share")
    assert ("@media (max-width:640px) {\n"
            "  .costtbl .bar-track { display:none; }\n"
            "  .costtbl .m-share { display:flex; }\n"
            "}") in css


def test_the_totals_row_labels_its_amounts_for_the_stacked_phone_card():
    import inspect
    src = inspect.getsource(db)
    assert '<td data-label="API-rate" class="num" style="font-weight:600">\'+fmt(a.tot.total)' in src
    assert '<td data-label="Billed" class="num" style="font-weight:600">\'+fmt(a.billedTotal)' in src


# --- recorded LLM spend from the Library's ledger (issue_01M38TF7S5RXVKB1JG6P4VT9JW)

import datetime as _dt  # noqa: E402

import library_client  # noqa: E402


def _le(n, *, product="vivy", usd=1.0, model="claude-sonnet-5", agent="coder",
        item_id=None, source="gateway", day="2026-09-27", voided=False):
    return library_client.LedgerEntry(
        id=f"le_{n}", ledger_id="led_1", day=day, usd=usd,
        at=_dt.datetime(2026, 9, 27, 12), source=source, product=product,
        run_id=f"run-{n}", agent=agent, model=model, item_id=item_id,
        voided_at=_dt.datetime(2026, 9, 27, 13) if voided else None)


_FIXTURE = [
    _le(1, usd=2.50, model="claude-opus-5-5", item_id="issue_a"),
    _le(2, usd=0.75, item_id="issue_b"),
    _le(3, usd=1.10, agent="reviewer", day="2026-09-26"),
    _le(4, usd=9.99, voided=True),
    _le(5, product="selector", usd=4.00, source="app"),
]
_EPIC_OF = {"issue_a": "epic_x", "issue_b": "epic_x"}


def test_a_projects_total_and_each_groups_total_equal_their_entries_sum():
    """Definition of done: the project total equals the sum of that project's
    live entries, and every group under every dimension sums to the same
    total. A voided entry is counted separately and left out, as the
    Library's own ledger total leaves it out."""
    groups = db.ledger_groups(_FIXTURE, _EPIC_OF)
    for product in ("vivy", "selector"):
        live = [e for e in _FIXTURE if e.product == product and e.voided_at is None]
        g = groups[product]
        assert round(g["total"], 6) == round(sum(e.usd for e in live), 6)
        assert g["count"] == len(live)
        for dim, _label in db._LEDGER_DIMENSIONS:
            assert round(sum(t for _, t, _ in g["groups"][dim]), 6) == round(g["total"], 6), dim
    assert groups["vivy"]["voided"] == 1
    assert dict((v, t) for v, t, _ in groups["vivy"]["groups"]["epic"]) == {
        "epic_x": 3.25, "(none)": 1.10}


def test_a_page_missing_one_page_of_entries_would_fail_the_total_check(monkeypatch):
    """The total check above only guards anything if a short read changes the
    answer. Drop the last entry, as a reader that stopped one page early
    would, and the rendered total no longer equals the full set's sum."""
    full = db.ledger_groups(_FIXTURE, _EPIC_OF)["vivy"]["total"]
    short = db.ledger_groups(_FIXTURE[:2], _EPIC_OF)["vivy"]["total"]
    assert short != full
    html = db.ledger_section_html(_FIXTURE, [], _EPIC_OF)
    assert f"${full:,.2f}" in html


def test_an_empty_ledger_says_nothing_is_recorded_rather_than_zero():
    html = db.ledger_section_html([], [], {})
    assert "No spend recorded yet." in html
    assert "$0.00" not in html


def test_a_projects_ledger_shows_todays_total_against_its_hard_cap():
    lg = library_client.Ledger(scope_kind="project", scope_id="vivy", day="2026-09-27",
                               total_usd=3.5, warn_usd=None, soft_cap_usd=None,
                               hard_cap_usd=10.0, per_run_cap_usd=None, state="ok")
    html = db.ledger_section_html(_FIXTURE, [lg], _EPIC_OF)
    assert "$3.50 today (2026-09-27) of $10.00 hard cap" in html
    assert "1 voided, not counted" in html


# --- the per-project ledger-caps form (issue_01M4BH8KSSMC8XBKB2C2CX9QTF) -----


def test_the_caps_form_prefills_all_four_values_from_the_ledger():
    lg = library_client.Ledger(scope_kind="project", scope_id="vivy", day="2026-09-27",
                               total_usd=3.5, warn_usd=1.0, soft_cap_usd=2.0,
                               hard_cap_usd=10.0, per_run_cap_usd=0.5, state="ok")
    html = db.ledger_section_html(_FIXTURE, [lg], _EPIC_OF)
    block = html.split('data-project="vivy"')[1].split("</div>")[0]
    assert 'data-field="warn_usd" value="1.0"' in block
    assert 'data-field="soft_cap_usd" value="2.0"' in block
    assert 'data-field="hard_cap_usd" value="10.0"' in block
    assert 'data-field="per_run_cap_usd" value="0.5"' in block


def test_the_caps_form_prefills_blank_fields_when_the_project_has_no_ledger():
    """`selector` carries entries in `_FIXTURE` but no `Ledger` is passed for
    it — the shape a project with no caps set shows."""
    html = db.ledger_section_html(_FIXTURE, [], _EPIC_OF)
    block = html.split('data-project="selector"')[1].split("</div>")[0]
    for field in ("warn_usd", "soft_cap_usd", "hard_cap_usd", "per_run_cap_usd"):
        assert f'data-field="{field}" value=""' in block


def test_the_caps_write_script_is_emitted_once_for_every_project_on_the_page():
    """Two projects, one script — a copy per project would attach the save
    handler again on every row already rendered (ds.ledger_caps_form_html's
    own docstring)."""
    html = db.ledger_section_html(_FIXTURE, [], _EPIC_OF)
    assert len({"vivy", "selector"} & set(re.findall(r'data-project="(\w+)"', html))) == 2
    assert html.count('document.querySelectorAll(".ledger-caps")') == 1


def test_ledger_values_are_escaped():
    html = db.ledger_section_html([_le(1, product="<p>", model="<script>x</script>")], [], {})
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
    assert 'data-product="&lt;p&gt;"' in html


def test_a_long_dimension_folds_its_tail_into_one_row_that_keeps_the_sum():
    many = [_le(i, day=f"2026-09-{i:02d}", usd=1.0) for i in range(1, 21)]
    html = db.ledger_section_html(many, [], {})
    assert "8 more" in html and "$8.00" in html


def test_the_ledger_groups_a_correction_under_corrections_not_its_queue_id(monkeypatch):
    """A correction's parent is null or its project's corrections queue
    (library/specs/one-home-per-work-item); either way its spend groups under
    "Corrections", and an epic's child still groups under the epic."""
    entries = [_le(1, item_id="issue_null"), _le(2, item_id="issue_queued"),
               _le(3, item_id="issue_child"), _le(4)]
    parents = {"issue_null": None, "issue_queued": "cq_vivy", "issue_child": "epic_x"}

    def rec(i, type_, parent):
        return library_client.IssueRecord(
            id=i, url="", product="vivy", title="", body="", type=type_, state="open",
            severity=None, needs=None, maturity=None, parent_id=parent, topics=[], refs={},
            specification=None, definition_of_done=None, resolution=None, resolved_by=None,
            outcome_note=None, claimed_by=None, claimed_at=None, claim_expires_at=None,
            claim_expired=False, resolved_at=None, created_by="a", created_by_kind=None,
            created_at=_dt.datetime(2026, 9, 1), run_id=None, park_reason=None,
            unpark_condition=None, parked_at=None, blocked=False, prerequisites=[])

    items = [rec(i, "task", p) for i, p in parents.items()] + [rec("epic_x", "epic", None)]
    asked = []
    monkeypatch.setattr(library_client, "list_ledger_entries", lambda **k: entries)
    monkeypatch.setattr(library_client, "list_ledgers", lambda: [])
    monkeypatch.setattr(library_client, "list_issue_items", lambda: items)
    monkeypatch.setattr(library_client, "corrections_queue_ids",
                        lambda ids: asked.append(set(ids)) or {i for i in ids if i == "cq_vivy"})
    html = db._ledger_section()
    epic_rows = dict(re.findall(r"<tr><td>([^<]+)</td><td class=\"num\">(\d+)</td>",
                                html.split("<h4>Epic</h4>")[1].split("</table>")[0]))
    assert epic_rows == {"Corrections": "2", "epic_x": "1", "(none)": "1"}
    assert "cq_vivy" not in html
    assert asked == [{"cq_vivy"}]


def test_the_live_page_reads_the_ledger_and_the_static_build_does_not(tmp_path, monkeypatch):
    """The scheduled `build()` writes the fallback page, and a fallback that
    needs the Library cannot stand in when the Library is down, so only the
    live render reads the ledger."""
    monkeypatch.setattr(db, "COST_DIR", tmp_path)
    (tmp_path / "latest.json").write_text(json.dumps({"generated_at": "2026-09-27T00:00:00Z", "total": 1.5}))
    monkeypatch.setattr(db.alert_policy_store, "get", lambda _k: None)
    monkeypatch.setattr(library_client, "list_ledger_entries", lambda **k: _FIXTURE)
    monkeypatch.setattr(library_client, "list_ledgers", lambda: [])
    items = [library_client.IssueRecord(
        id=i, url="", product="vivy", title="", body="", type="task", state="open",
        severity=None, needs=None, maturity=None, parent_id="epic_x", topics=[], refs={},
        specification=None, definition_of_done=None, resolution=None, resolved_by=None,
        outcome_note=None, claimed_by=None, claimed_at=None, claim_expires_at=None,
        claim_expired=False, resolved_at=None, created_by="a", created_by_kind=None,
        created_at=_dt.datetime(2026, 9, 1), run_id=None, park_reason=None,
        unpark_condition=None, parked_at=None, blocked=False, prerequisites=[])
        for i in ("issue_a", "issue_b")]
    monkeypatch.setattr(library_client, "list_issue_items", lambda: items)
    monkeypatch.setattr(library_client, "corrections_queue_ids", lambda ids: set())
    live = db.spending_live_html()
    assert "Recorded LLM spend by project" in live and "epic_x" in live

    def refuse(**_k):
        raise AssertionError("the static build read the Library")

    monkeypatch.setattr(library_client, "list_ledger_entries", refuse)
    assert "Recorded LLM spend by project" not in db.spending_render()
