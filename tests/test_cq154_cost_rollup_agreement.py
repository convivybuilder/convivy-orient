"""Cross-surface cost agreement for CQ·154 (issue_01M4ZBBQK0KFH9YQ16MNK75HJT):
the crew view and Chart House both read
`library_client.cost_rollup_by_project` over `library_client.list_finished_runs`,
so a fixture with differing per-project costs must show the identical figure
on each — the Definition of Done's own language. Every surface here is
exercised through its real `live_html`/loader, down to one mocked Library
function, rather than a stub of the rollup itself, so a change that reintroduces
a second cost path would show up here even if it agreed with itself.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

import chart_house_build as ch
import crews_build as cr
import library_client as lc


def _finished_run(product, cost_usd):
    return lc.FinishedRun(
        signal_id=f"{product}-{cost_usd}", product=product, run="run-1", persona=None,
        repo=product, work_item=None, model="claude-sonnet-5",
        cost=lc.FinishedRunCost(usd=cost_usd, tokens=None, by_model=None),
        outcome="merged", produced_at=None, ingested_at=dt.datetime(2026, 10, 15))


@pytest.fixture(autouse=True)
def _fixed_runs(monkeypatch):
    """Two projects with DIFFERENT costs, present for every test in this
    file regardless of which surface or scope it renders — the point of the
    scoped test below is that SCOPE hides a project, never absent data."""
    runs = [_finished_run("alpha", 10.0), _finished_run("alpha", 5.0),
           _finished_run("beta", 25.0)]
    monkeypatch.setattr(lc, "list_finished_runs", lambda since, until=None: runs)


@pytest.fixture(autouse=True)
def _registry(tmp_path, monkeypatch):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({
        "alpha": {"crew": "alpha", "lifecycle": "active", "repos": ["alpha"]},
        "beta": {"crew": "beta", "lifecycle": "active", "repos": ["beta"]},
    }))
    monkeypatch.setenv("PROJECTS_JSON", str(path))


@pytest.fixture(autouse=True)
def _no_other_chart_house_or_crews_sources(monkeypatch):
    """Every Chart House / Crews data source this fixture doesn't care
    about, defaulted to its own real "nothing here" shape (matching
    test_chart_house_build.py's and test_crews_build.py's own autouse
    fixtures) so a test below need not know these calls exist."""
    monkeypatch.setattr(lc, "list_reference_documents", lambda **k: [])
    monkeypatch.setattr(lc, "list_issue_items", lambda: [])
    monkeypatch.setattr(lc, "list_signals", lambda **k: [])
    monkeypatch.setattr(ch.merge_queue_build, "cached_open_prs", lambda: None)
    monkeypatch.setattr(cr.library_client, "list_leases",
                        lambda **k: lc.LeaseListResult(leases=[], policy=None))
    monkeypatch.setattr(cr.library_client, "list_lease_refusals", lambda **k: [])
    monkeypatch.setattr(cr.library_client, "list_live_runs",
                        lambda: lc.LiveRunsResult(runs=[], available=True, reason=None,
                                                  generated_at=None, stale_after_seconds=None,
                                                  count=0))


def _course(product):
    entry = lc.CourseEntry(issue_id=f"{product}-i1", state="open", maturity=None,
                           title="ship it", specification=None)
    return lc.CourseRecord(
        id=f"course-{product}", product=product, title="Ship it", status="live",
        version=1, entries=[entry], created_by="ops-mcp", created_at=None,
        updated_by="ops-mcp", updated_at=None)


def _mock_courses(monkeypatch, products):
    courses = [_course(p) for p in products]
    monkeypatch.setattr(lc, "list_courses", lambda **k: courses)
    monkeypatch.setattr(lc, "get_course", lambda cid: next(c for c in courses if c.id == cid))


def _mock_roster(monkeypatch, slugs):
    roster = {"projects": [{"slug": s, "label": s.title(), "repo": f"/{s}", "lanes": []}
                           for s in slugs], "autonomous": []}
    monkeypatch.setattr(cr.roster_build, "load_roster", lambda: roster)


def test_crew_view_and_chart_house_agree_on_differing_project_costs(monkeypatch):
    _mock_courses(monkeypatch, ["alpha", "beta"])
    _mock_roster(monkeypatch, ["alpha", "beta"])
    scope = frozenset({"alpha", "beta"})

    crew_html = cr.live_html(scope, True)
    chart_html = ch.live_html(scope)

    # alpha: 10.0 + 5.0 = 15.00; beta: 25.00 — the crew view's per-project
    # cost equals the sum of that project's finished-run records.
    assert "$15.00" in crew_html
    assert "$25.00" in crew_html
    assert "$15.00" in chart_html
    assert "$25.00" in chart_html


def test_a_viewer_scoped_to_one_project_sees_only_that_cost(monkeypatch):
    _mock_courses(monkeypatch, ["alpha", "beta"])
    _mock_roster(monkeypatch, ["alpha", "beta"])
    scope = frozenset({"alpha"})

    crew_html = cr.live_html(scope, False)
    chart_html = ch.live_html(scope)

    assert "$15.00" in crew_html
    assert "Beta" not in crew_html
    assert "$25.00" not in chart_html
    assert "Beta" not in chart_html
