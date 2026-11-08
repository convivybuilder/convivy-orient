"""/model-comparison (CQ·156, issue_01M57NH489EASKMATJ8QFED5EC): finished
runs grouped by agent and model, read live from the Library's
`model_resolution` records through `library_client.list_finished_runs`.

The stub below carries two agents (coder, reviewer), two models
(claude-sonnet-5, claude-opus-5) and two projects (proj-a, proj-b) — the
DoD's own minimum — with figures chosen so a group's total cost, mean cost
and completed share are each a distinct, hand-checkable number."""
from __future__ import annotations

import datetime as dt

import model_comparison_build as mc
import library_client


def _run(product, persona, model_resolved, cost_usd, exit_status, *, produced_at=None):
    cost = (None if cost_usd is None else
            library_client.FinishedRunCost(usd=cost_usd, tokens=None, by_model=None))
    return library_client.FinishedRun(
        signal_id=f"res_{product}_{persona}_{model_resolved}_{exit_status}",
        product=product, run="run-x", persona=persona, repo=product, work_item=None,
        model="claude-sonnet-5", cost=cost,
        outcome={"exit_status": exit_status, "terminal_reason": None},
        produced_at=produced_at or dt.datetime(2026, 10, 10),
        ingested_at=produced_at or dt.datetime(2026, 10, 10),
        model_resolved=model_resolved, exit_status=exit_status)


# proj-a: 3 runs (2 coder, 1 reviewer; 2 claude-sonnet-5, 1 claude-opus-5)
# proj-b: 2 runs (1 coder, 1 reviewer; 1 claude-sonnet-5, 1 claude-opus-5, one priceless)
R1 = _run("proj-a", "coder", "claude-sonnet-5", 2.0, "completed")
R2 = _run("proj-a", "coder", "claude-opus-5", 4.0, "failed")
R3 = _run("proj-a", "reviewer", "claude-sonnet-5", 1.0, "completed")
R4 = _run("proj-b", "coder", "claude-sonnet-5", 3.0, "completed")
R5 = _run("proj-b", "reviewer", "claude-opus-5", None, "completed")
ALL_RUNS = [R1, R2, R3, R4, R5]


def _stub(monkeypatch, runs=ALL_RUNS):
    monkeypatch.setattr(library_client, "list_finished_runs", lambda since, until=None: runs)


# --- resolve_window -----------------------------------------------------


def test_resolve_window_defaults_to_the_trailing_window():
    since, until, is_custom = mc.resolve_window(None)
    today = dt.datetime.now(dt.timezone.utc).date()
    assert until == today
    assert since == today - dt.timedelta(days=mc.DEFAULT_WINDOW_DAYS - 1)
    assert is_custom is False


def test_resolve_window_honours_a_custom_range():
    since, until, is_custom = mc.resolve_window({"from": "2026-01-01", "to": "2026-01-10"})
    assert (since, until, is_custom) == (dt.date(2026, 1, 1), dt.date(2026, 1, 10), True)


def test_resolve_window_clamps_to_the_max():
    since, until, _ = mc.resolve_window({"from": "2026-01-01", "to": "2026-06-01"})
    assert (until - since).days == mc.MAX_WINDOW_DAYS


# --- grouping -------------------------------------------------------------


def test_grouping_by_persona_matches_the_stub(monkeypatch):
    groups = mc._group_rows([R1, R2, R3], "persona")
    by_label = {g.label: g for g in groups}
    assert set(by_label) == {"coder", "reviewer"}
    coder = by_label["coder"]
    assert coder.count == 2
    assert coder.total_cost == 6.0
    assert coder.mean_cost == 3.0
    assert coder.completed_share == 0.5
    reviewer = by_label["reviewer"]
    assert reviewer.count == 1
    assert reviewer.total_cost == 1.0
    assert reviewer.mean_cost == 1.0
    assert reviewer.completed_share == 1.0


def test_grouping_by_model_resolved_matches_the_stub():
    groups = mc._group_rows([R1, R2, R3], "model_resolved")
    by_label = {g.label: g for g in groups}
    assert set(by_label) == {"claude-sonnet-5", "claude-opus-5"}
    sonnet = by_label["claude-sonnet-5"]
    assert sonnet.count == 2
    assert sonnet.total_cost == 3.0
    assert sonnet.mean_cost == 1.5
    assert sonnet.completed_share == 1.0
    opus = by_label["claude-opus-5"]
    assert opus.count == 1
    assert opus.total_cost == 4.0
    assert opus.completed_share == 0.0


def test_grouping_by_both_pairs_agent_and_model():
    groups = mc._group_rows([R1, R2, R3], "both")
    labels = {g.label for g in groups}
    assert labels == {"coder · claude-sonnet-5", "coder · claude-opus-5",
                      "reviewer · claude-sonnet-5"}
    assert all(g.count == 1 for g in groups)


def test_a_run_with_unknown_cost_still_counts_toward_runs_but_not_the_mean():
    """R5 has no recorded cost — it must still be counted in the group's run
    total (the DoD's "number of runs"), while total/mean cost stay `None`
    rather than silently treating the unknown cost as zero."""
    groups = mc._group_rows([R4, R5], "persona")
    by_label = {g.label: g for g in groups}
    reviewer = by_label["reviewer"]
    assert reviewer.count == 1
    assert reviewer.total_cost is None
    assert reviewer.mean_cost is None
    assert reviewer.completed_share == 1.0


def test_a_small_group_is_not_hidden():
    """A one-run group still appears, carrying its own count — never
    suppressed for being small (the DoD's own wording)."""
    groups = mc._group_rows([R3], "persona")
    assert len(groups) == 1
    assert groups[0].count == 1


def test_groups_sort_largest_first_then_by_label():
    groups = mc._group_rows([R1, R2, R3, R4, R5], "persona")
    assert [g.label for g in groups] == ["coder", "reviewer"]
    assert groups[0].count == 3  # 3 coder runs (R1, R2, R4)


# --- scoping: applied before grouping, before the filter's own options ----


def test_scope_drops_an_out_of_scope_project_before_any_count(monkeypatch):
    _stub(monkeypatch)
    scope = frozenset({"proj-a"})
    html = mc.live_html(None, scope, False)
    assert "proj-b" not in html


def test_scope_drops_an_out_of_scope_project_from_the_filter_options(monkeypatch):
    _stub(monkeypatch)
    scope = frozenset({"proj-a"})
    html = mc.live_html(None, scope, False)
    assert '<option value="proj-a">proj-a</option>' in html
    assert 'value="proj-b"' not in html


def test_a_whole_fleet_viewer_sees_both_projects(monkeypatch):
    """The positive control: confirm "proj-b" DOES appear for a viewer whose
    scope covers it, so the scoped assertion above is not vacuous."""
    _stub(monkeypatch)
    scope = frozenset({"proj-a", "proj-b"})
    html = mc.live_html(None, scope, True)
    assert "proj-b" in html


def test_an_out_of_scope_project_query_param_matches_nothing(monkeypatch):
    """A hand-edited `?project=proj-b` outside `scope` narrows the
    already-scoped set to empty, rather than ever being checked against the
    full unfiltered list — there is no code path where it could reveal
    whether proj-b exists."""
    _stub(monkeypatch)
    scope = frozenset({"proj-a"})
    html = mc.live_html({"project": "proj-b"}, scope, False)
    assert "proj-b" not in html
    assert "No finished runs" in html


# --- the page itself --------------------------------------------------


def test_the_page_shows_group_counts_cost_and_completed_share(monkeypatch):
    _stub(monkeypatch, [R1, R2, R3])
    html = mc.live_html({"by": "persona"}, frozenset({"proj-a"}), False)
    assert "coder" in html
    assert "2 runs" in html
    assert "$6.00 total" in html
    assert "$3.00 mean" in html
    assert "50% completed" in html
    assert "reviewer" in html
    assert "1 run" in html
    assert "100% completed" in html


def test_a_group_opens_to_list_its_runs(monkeypatch):
    _stub(monkeypatch, [R1, R2, R3])
    html = mc.live_html({"by": "persona"}, frozenset({"proj-a"}), False)
    assert "<details" in html
    # the group's own runs are embedded, not fetched separately
    assert html.count("claude-sonnet-5") >= 2
    assert html.count("claude-opus-5") >= 1


def test_project_filter_narrows_the_runs(monkeypatch):
    """Selecting proj-b drops every proj-a RUN ROW. "proj-a" still appears
    once, as the filter dropdown's own option — the dropdown always lists
    every in-scope project regardless of which one is selected — so the
    assertion checks the row cell's exact markup, not the bare substring."""
    _stub(monkeypatch)
    html = mc.live_html({"project": "proj-b"}, frozenset({"proj-a", "proj-b"}), True)
    assert '>proj-a</td>' not in html
    assert '>proj-b</td>' in html


def test_date_range_outside_the_window_excludes_a_run(monkeypatch):
    old_run = _run("proj-a", "coder", "claude-sonnet-5", 1.0, "completed",
                   produced_at=dt.datetime(2020, 1, 1))

    def fake(since, until=None):
        return [r for r in [old_run] if since <= (r.produced_at or r.ingested_at) <= (until or since)]

    monkeypatch.setattr(library_client, "list_finished_runs", fake)
    html = mc.live_html({"from": "2026-01-01", "to": "2026-01-31"}, frozenset({"proj-a"}), False)
    assert "No finished runs" in html


def test_an_empty_result_renders_the_empty_state_not_a_crash(monkeypatch):
    _stub(monkeypatch, [])
    html = mc.live_html(None, frozenset({"proj-a"}), False)
    assert "No finished runs" in html


def test_a_library_failure_raises_rather_than_rendering_an_empty_queue(monkeypatch):
    """Orient is a client of the Library and must not turn an outage into a
    quiet "nothing ran" — the caller (server.py) decides what a raise
    becomes; this module must never swallow it."""
    def boom(since, until=None):
        raise RuntimeError("library unreachable")

    monkeypatch.setattr(library_client, "list_finished_runs", boom)
    try:
        mc.live_html(None, frozenset({"proj-a"}), False)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected the Library failure to propagate")
