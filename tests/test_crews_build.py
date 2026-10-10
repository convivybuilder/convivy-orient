"""Tests for the Crews page (/crews, issue_01M08YM8MTNSTAEG1NV7E2GYRV).

All four data sources are monkeypatched: this page never talks to GitHub
itself (unlike project_build/merge_queue_build), it reads the Library live
(issue items, leases — via library_client) and local files (cost, via
chart_house_build's rollup) and roster_build's own in-repo data. Same
seam-testing shape as test_chart_house_build.py.

The sessions/runs section (issue_01M15C5W1GFGC81CW7YNCB0CP9) reads local
files under `~/Convivy/agent-runs` directly — the `_empty_agent_runs`
autouse fixture below points `cr.AGENT_RUNS_ROOT` at a private, empty tmp
directory for every test in this file, so nothing here can read (or flake
against) the real corpus on this machine. Tests that need run/session data
build fixture directories under that same root explicitly.
"""
from __future__ import annotations

import datetime as dt
import json
import re

import pytest

import crews_build as cr
import library_client as lc


@pytest.fixture(autouse=True)
def _empty_agent_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, "AGENT_RUNS_ROOT", tmp_path / "agent-runs")


def _write_run(root, date, run_id, *, agent="orient-coder", task="a task", label=None,
               started_at="2026-08-29T12:00:00Z", ended_at="2026-08-29T12:30:00Z",
               repo="convivy-orient", model_resolved="claude-sonnet-5",
               co_session="sess-1", cost_usd=1.0, outcome=None, no_cost=False,
               no_outcome=False):
    """One fixture run directory: meta.json always, cost.json/outcome.json
    unless explicitly withheld (`no_cost`/`no_outcome`) — matching how an
    in-flight run has neither file yet."""
    d = root / date / run_id
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "run_id": run_id, "agent": agent, "task": task, "label": label,
        "started_at": started_at, "ended_at": ended_at, "repo": repo,
        "model_resolved": model_resolved, "co_session": co_session,
    }))
    if not no_cost and ended_at is not None:
        (d / "cost.json").write_text(json.dumps({"usd": cost_usd}))
    if not no_outcome and ended_at is not None:
        (d / "outcome.json").write_text(json.dumps(outcome or {
            "exit_status": "completed", "files_modified": [], "prs_opened": [],
        }))
    return d


def _write_session(root, date, session_id, *, started_at="2026-08-29T11:55:00Z",
                   heartbeat_at="2026-08-29T12:35:00Z", cwd=None, model=""):
    d = root / date / f"co-{session_id}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "kind": "co-session", "session_id": session_id, "started_at": started_at,
        "heartbeat_at": heartbeat_at,
        "cwd": cwd or str(cr.CONVIVY_ROOT / "convivy-orient"),
        "product": "convivy-orient", "model": model,
        "transcript_path": "/x.jsonl", "ppid": 1,
    }))
    return d


def _item(id_, product, state, pr_url=None, prerequisites=None):
    prereqs = prerequisites or []
    return lc.IssueRecord(
        id=id_, url=f"/engineering/issues/{id_}", product=product, title="t",
        body="", type="bug", state=state, severity="medium", needs="agent",
        maturity=None, parent_id=None, topics=[], refs={"pr_url": pr_url} if pr_url else {},
        specification=None, definition_of_done=None, resolution=None, resolved_by=None,
        outcome_note=None, claimed_by=None, claimed_at=None, claim_expires_at=None,
        claim_expired=False, resolved_at=None, created_by="agent", created_by_kind="agent",
        created_at=None, run_id=None, park_reason=None, unpark_condition=None, parked_at=None,
        blocked=bool(prereqs), prerequisites=prereqs)


def _lease(id_, root_id, *, dead=False, expires_at=None, heartbeat_at=None, taken_at=None):
    return lc.LeaseRecord(
        id=id_, root_id=root_id, root_url=f"/engineering/issues/{root_id}",
        taken_for="build it", holder="coder", run_id="run-1", taken_at=taken_at,
        heartbeat_at=heartbeat_at, expires_at=expires_at, dead=dead, released_at=None,
        release_reason=None, released_by=None, note=None)


def _refusal(id_, product, *, requester="session-B", holder="session-A",
             root="issue_root", blocks_by="ancestor", at=None, blocked=False,
             wanted="issue_wanted"):
    return lc.LeaseRefusalRecord(
        id=id_, at=at, requested_item_id=wanted, product=product,
        requester=requester, actor="ops-mcp", blocks_by=blocks_by,
        blocking_lease_id=None if blocked else "lease_1",
        blocking_root_id=None if blocked else root,
        blocking_holder=None if blocked else holder, blocked=blocked)


# --- _work_by_crew ---------------------------------------------------------


def test_work_by_crew_counts_only_open_and_claimed():
    items = [_item("i1", "vivy", "open"), _item("i2", "vivy", "claimed"),
             _item("i3", "vivy", "fixed"), _item("i4", "vivy", "wontfix")]
    work = cr._work_by_crew(items)
    assert work["vivy"]["active"] == 2


def test_work_by_crew_counts_in_review_as_active_with_a_linked_pr():
    items = [_item("i1", "vivy", "open", pr_url="https://github.com/convivy/vivy/pull/1"),
             _item("i2", "vivy", "open")]
    work = cr._work_by_crew(items)
    assert work["vivy"] == {"active": 2, "in_review": 1}


def test_work_by_crew_groups_by_product():
    items = [_item("i1", "vivy", "open"), _item("i2", "constance", "claimed")]
    work = cr._work_by_crew(items)
    assert work["vivy"]["active"] == 1
    assert work["constance"]["active"] == 1


# --- _leases_by_crew (joined via root_id, since a lease carries no product) --


def test_leases_by_crew_joins_through_the_root_item():
    # No expires_at on this fixture lease -> the None guard in _lease_state,
    # which reads as ordinary "alive" rather than crashing or reading urgent.
    items = [_item("i1", "vivy", "claimed")]
    leases = [_lease("l1", "i1")]
    out = cr._leases_by_crew(leases, items)
    assert out["vivy"] == {"alive": 1, "near_expiry": [], "dead": 0}


def test_leases_by_crew_splits_alive_near_expiry_and_dead():
    now = dt.datetime(2026, 9, 1, 12, 0, 0)
    items = [_item("i1", "vivy", "claimed"), _item("i2", "vivy", "claimed"),
             _item("i3", "vivy", "claimed")]
    leases = [
        _lease("l1", "i1", dead=False, expires_at=now + dt.timedelta(hours=4),
              heartbeat_at=now),
        _lease("l2", "i2", dead=False, expires_at=now + dt.timedelta(minutes=4),
              heartbeat_at=now - dt.timedelta(minutes=30)),
        _lease("l3", "i3", dead=True),
    ]
    out = cr._leases_by_crew(leases, items, now=now)
    assert out["vivy"]["alive"] == 1
    assert out["vivy"]["dead"] == 1
    assert out["vivy"]["near_expiry"] == [pytest.approx(240.0)]


def test_leases_by_crew_drops_a_lease_whose_root_item_is_unknown():
    # An issue outside this credential's scope, or one from a store this
    # page didn't fetch — must not crash or misattribute to a bogus key.
    leases = [_lease("l1", "issue-not-in-items")]
    out = cr._leases_by_crew(leases, [])
    assert out == {}


def test_leases_by_crew_warns_once_per_render_when_policy_is_missing(monkeypatch):
    # A missing policy makes every short-runway lease read as near_expiry
    # (issue_01M1F1F8M4TZ0FN917DGB1Q62Q's fail-toward-warning tradeoff) —
    # that must not happen silently, and must not flood one warning per
    # lease when a render holds several. Every lease carries an expires_at
    # so `_lease_state`'s own (unrelated) "no expires_at" warning can't
    # inflate the count this test is checking.
    warnings = []
    monkeypatch.setattr(cr._logger, "warning", lambda *a, **k: warnings.append(a))
    now = dt.datetime(2026, 9, 1, 12, 0, 0)
    items = [_item("i1", "vivy", "claimed"), _item("i2", "vivy", "claimed"),
             _item("i3", "vivy", "claimed")]
    leases = [_lease("l1", "i1", expires_at=now + dt.timedelta(hours=4)),
              _lease("l2", "i2", expires_at=now + dt.timedelta(hours=4)),
              _lease("l3", "i3", expires_at=now + dt.timedelta(hours=4))]
    cr._leases_by_crew(leases, items, policy=None, now=now)
    assert len(warnings) == 1


def test_leases_by_crew_does_not_warn_when_policy_is_served(monkeypatch):
    warnings = []
    monkeypatch.setattr(cr._logger, "warning", lambda *a, **k: warnings.append(a))
    now = dt.datetime(2026, 9, 1, 12, 0, 0)
    items = [_item("i1", "vivy", "claimed")]
    leases = [_lease("l1", "i1", expires_at=now + dt.timedelta(hours=4))]
    policy = lc.LeasePolicy(grace_seconds=14400, max_lease_seconds=86400)
    cr._leases_by_crew(leases, items, policy=policy, now=now)
    assert warnings == []


# --- _lease_state (issue_01M1F1F8M4TZ0FN917DGB1Q62Q: near-expiry, before it --
# --- expires, with time remaining) ------------------------------------------


def test_lease_state_dead_lease_is_dead_regardless_of_clock():
    # A dead lease is dead even if its expires_at reads as comfortably future
    # — the Library's own dead flag always wins.
    now = dt.datetime(2026, 9, 1, 12, 0, 0)
    lease = _lease("l1", "i1", dead=True, expires_at=now + dt.timedelta(hours=4))
    assert cr._lease_state(lease, now) == ("dead", None)


def test_lease_state_alive_when_comfortably_before_expiry():
    now = dt.datetime(2026, 9, 1, 12, 0, 0)
    lease = _lease("l1", "i1", dead=False, expires_at=now + dt.timedelta(hours=4),
                   heartbeat_at=now)
    assert cr._lease_state(lease, now) == ("alive", None)


def test_lease_state_near_expiry_when_close_and_heartbeat_is_also_stale():
    # The failure the epic was filed to catch: nobody has checked in and the
    # runway is short — surfaced BEFORE the lease actually dies.
    now = dt.datetime(2026, 9, 1, 12, 0, 0)
    lease = _lease("l1", "i1", dead=False, expires_at=now + dt.timedelta(minutes=4),
                   heartbeat_at=now - dt.timedelta(minutes=30))
    state, remaining = cr._lease_state(lease, now)
    assert state == "near_expiry"
    assert remaining == pytest.approx(240.0)


def test_lease_state_is_not_near_expiry_when_the_heartbeat_is_fresh():
    """The false alarm the issue calls out by name: a healthy, actively
    heartbeated crew riding a short GRACE window must not read as at risk
    just because its absolute expiry happens to be close.

    Reachable, not hypothetical: `MIN_GRACE` is one minute, so grace tuned to
    five minutes is a legal policy, and a lease taken well within the last
    24h max-lease window has its expiry set entirely by that short grace —
    the grace arm binds (`_grace_arm_binds`), and the fix's arm-detection
    must recognize that rather than warning on every short runway."""
    now = dt.datetime(2026, 9, 1, 12, 0, 0)
    policy = lc.LeasePolicy(grace_seconds=5 * 60, max_lease_seconds=24 * 3600)
    heartbeat_at = now - dt.timedelta(minutes=1)
    lease = _lease("l1", "i1", dead=False,
                   expires_at=heartbeat_at + dt.timedelta(seconds=policy.grace_seconds),
                   heartbeat_at=heartbeat_at, taken_at=now - dt.timedelta(hours=1))
    assert cr._lease_state(lease, now, policy) == ("alive", None)


def test_lease_state_near_expiry_on_the_max_lease_arm_despite_a_fresh_heartbeat():
    """The case the old guard had backwards. A lease riding its hard
    MAX_LEASE cap, not the grace window, with a heartbeat five seconds old:
    the cap does not move no matter how alive the holder is, so the short
    runway here is unambiguous and must not be suppressed.

    Against the pre-fix guard (heartbeat freshness alone) this lease reads
    "alive" — wrong, since the lease dies on schedule regardless."""
    now = dt.datetime(2026, 9, 1, 12, 0, 0)
    policy = lc.LeasePolicy(grace_seconds=4 * 3600, max_lease_seconds=24 * 3600)
    taken_at = now - dt.timedelta(hours=24) + dt.timedelta(minutes=4)
    heartbeat_at = now - dt.timedelta(seconds=5)
    lease = _lease("l1", "i1", dead=False,
                   expires_at=taken_at + dt.timedelta(seconds=policy.max_lease_seconds),
                   heartbeat_at=heartbeat_at, taken_at=taken_at)
    state, remaining = cr._lease_state(lease, now, policy)
    assert state == "near_expiry"
    assert remaining == pytest.approx(240.0, abs=1)


def test_lease_state_warns_rather_than_suppresses_when_policy_is_unavailable():
    """Degrading honestly (per the fix's own requirement): if the Library's
    response ever omits `policy`, `_lease_state` must not fall back to the
    pre-fix, backwards heartbeat-only suppression. A short runway with no
    policy to judge the arm against reads as near_expiry, not alive."""
    now = dt.datetime(2026, 9, 1, 12, 0, 0)
    lease = _lease("l1", "i1", dead=False, expires_at=now + dt.timedelta(minutes=4),
                   heartbeat_at=now - dt.timedelta(seconds=5),
                   taken_at=now - dt.timedelta(hours=1))
    state, remaining = cr._lease_state(lease, now, None)
    assert state == "near_expiry"
    assert remaining == pytest.approx(240.0, abs=1)


def test_lease_state_guards_a_live_lease_with_no_expires_at():
    """`expires_at` is `| None` on LeaseRecord. A live lease reaching this
    with no expiry must not crash and must not read as urgent for a TTL this
    page cannot see."""
    now = dt.datetime(2026, 9, 1, 12, 0, 0)
    lease = _lease("l1", "i1", dead=False, expires_at=None, heartbeat_at=now)
    assert cr._lease_state(lease, now) == ("alive", None)


# --- _roster_counts (reuses roster_build.load_roster(), never reimplements) -


def test_roster_counts_sums_agents_across_lanes(monkeypatch):
    fake_roster = {"projects": [
        {"slug": "vivy", "label": "Vivy", "repo": "/x",
         "lanes": [{"name": "daytime", "label": "Daytime", "agents": [{}, {}]},
                   {"name": "overnight", "label": "Overnight", "agents": [{}]}]},
    ], "autonomous": []}
    monkeypatch.setattr(cr.roster_build, "load_roster", lambda: fake_roster)
    counts = cr._roster_counts()
    assert counts == {"vivy": {"label": "Vivy", "agents": 3}}


# --- crew-name override (ADR-0042): this page is about crews, not repos ----


def test_roster_counts_renders_agent_platform_as_bosun(monkeypatch):
    """The operator, reading /crews, 2026-09-02: "the 'agent-platform' ... line" needs
    to say Bosun — ADR-0042's own crew table names this mapping."""
    fake_roster = {"projects": [
        {"slug": "agent-platform", "label": "Agent Platform", "repo": "/x", "lanes": []},
    ], "autonomous": []}
    monkeypatch.setattr(cr.roster_build, "load_roster", lambda: fake_roster)
    counts = cr._roster_counts()
    assert counts["agent-platform"]["label"] == "Bosun"


def test_roster_counts_leaves_an_unmapped_crews_label_alone(monkeypatch):
    fake_roster = {"projects": [
        {"slug": "selector-backend", "label": "Selector Backend", "repo": "/x", "lanes": []},
    ], "autonomous": []}
    monkeypatch.setattr(cr.roster_build, "load_roster", lambda: fake_roster)
    counts = cr._roster_counts()
    assert counts["selector-backend"]["label"] == "Selector Backend"


def test_live_html_renders_bosun_rather_than_agent_platform(monkeypatch):
    roster = {"projects": [
        {"slug": "agent-platform", "label": "Agent Platform", "repo": "/x", "lanes": []},
    ], "autonomous": []}
    _patch_sources(monkeypatch, roster=roster)
    html = cr.live_html()
    assert ">Bosun<" in html
    assert "Agent Platform" not in html


# --- _refusals_by_crew (needs no join — a refusal names its own product) ---


def test_refusals_by_crew_groups_by_the_rows_own_product():
    out = cr._refusals_by_crew([_refusal("r1", "vivy"), _refusal("r2", "vivy"),
                                _refusal("r3", "keel")])
    assert out == {"vivy": 2, "keel": 1}


def test_refusals_by_crew_is_empty_rather_than_zeroed_when_there_are_none():
    assert cr._refusals_by_crew([]) == {}


# --- live_html: renders, links known projects, names the deferred gaps ----


def _live_runs_result(runs=None, *, available=True, reason=None, generated_at=None):
    return lc.LiveRunsResult(runs=runs or [], available=available, reason=reason,
                             generated_at=generated_at, stale_after_seconds=None,
                             count=len(runs or []))


def _patch_sources(monkeypatch, *, roster=None, items=None, leases=None, policy=None,
                   cost=None, refusals=None, live_runs=None):
    monkeypatch.setattr(cr.roster_build, "load_roster",
                        lambda: roster or {"projects": [], "autonomous": []})
    monkeypatch.setattr(cr.library_client, "list_issue_items", lambda: items or [])
    monkeypatch.setattr(cr.library_client, "list_leases",
                        lambda **k: lc.LeaseListResult(leases=leases or [], policy=policy))
    monkeypatch.setattr(cr.library_client, "list_lease_refusals",
                        lambda **k: refusals or [])
    monkeypatch.setattr(cr.library_client, "list_live_runs",
                        lambda: live_runs if live_runs is not None else _live_runs_result())
    monkeypatch.setattr(cr.ch, "_cost_rollup", lambda: cost or {})


def test_live_html_renders_a_row_per_roster_crew(monkeypatch):
    roster = {"projects": [
        {"slug": "vivy", "label": "Vivy", "repo": "/x", "lanes": []},
        {"slug": "constance", "label": "Constance", "repo": "/y", "lanes": []},
    ], "autonomous": []}
    _patch_sources(monkeypatch, roster=roster)
    html = cr.live_html()
    assert "Vivy" in html
    assert "Constance" in html


def test_live_html_links_a_crew_that_is_also_a_known_project(monkeypatch):
    roster = {"projects": [{"slug": "vivy", "label": "Vivy", "repo": "/x", "lanes": []}],
             "autonomous": []}
    _patch_sources(monkeypatch, roster=roster)
    html = cr.live_html()
    assert 'href="/projects/vivy"' in html


def test_live_html_does_not_link_a_crew_with_no_project_page(monkeypatch):
    # e.g. "selector-backend" — a real dispatchable crew per roster discovery,
    # but not one of project_build.KNOWN_PROJECTS' names.
    roster = {"projects": [{"slug": "selector-backend", "label": "Selector Backend",
                           "repo": "/x", "lanes": []}], "autonomous": []}
    _patch_sources(monkeypatch, roster=roster)
    html = cr.live_html()
    assert 'href="/projects/selector-backend"' not in html
    assert "Selector Backend" in html


def test_live_html_defers_nothing_and_names_no_missing_library_route(monkeypatch):
    """Every gap this page once deferred is closed or has moved elsewhere.
    Refusals shipped 2026-08-22, live Bosun state shipped in #246, and Course
    progress turned out to belong on the Chart House. Naming any of them as
    missing would be the page lying about the Library — the exact failure this
    epic's thread records twice over."""
    _patch_sources(monkeypatch)
    html = cr.live_html()
    assert "Bosun" in html
    assert "lease refusals (neither has a Library route" not in html
    # The RENDERED element, not the string: `.awaiting-state` is a rule in the
    # shared stylesheet every page ships, so a bare substring check for it can
    # never fail and would pin nothing.
    assert '<div class="awaiting-state' not in html


def test_live_html_no_longer_blames_the_library_for_course_progress(monkeypatch):
    """The old lede sentence — "Course progress (no Library read path
    exists)" — was wrong (issue_01M0TKX6DW2E1TXGN9QT6K61J5's reconciliation):
    `GET /courses` is live and served by the Library today."""
    _patch_sources(monkeypatch)
    html = cr.live_html()
    assert "no Library read path exists" not in html
    assert "no route yet" not in html


def test_live_html_does_not_claim_orient_lacks_a_courses_client(monkeypatch):
    """The panel this replaces said Orient needed "a client for GET /courses"
    and was false the whole time it was up: `library_client.list_courses` and
    `get_course` exist and both `chart_house_build` and `issue_build` consume
    them. Its `probe=("/courses", 200)` asked the Library whether it served the
    route — the half nobody doubted — so it passed at every render while the
    claim it stamped was wrong.

    **The `probe_route` stub below covers no branch that exists today** —
    `crews_build` no longer calls `probe_route` anywhere, because the panel
    that called it is gone. It is a forward-looking guard: if the panel is
    ever reintroduced, it renders under the unreachable-Library branch as well
    as the probed one, and this test fails either way. Said plainly rather
    than left to read as current coverage, since not overclaiming what a check
    verifies is the whole point of the change it guards.

    Course progress itself is NOT missing and is deliberately not built here
    (the operator, 2026-09-03): `/chart-house` already renders every live Course, and a
    Course's `product` key is not this page's crew slug — `bosun` versus
    `agent-platform` — so joining Courses here would silently drop the Bosun
    until someone copied `_BEAD_PROJECT_ALIASES`."""
    monkeypatch.setattr(cr.library_client, "probe_route", lambda path, **k: None)
    _patch_sources(monkeypatch)
    html = cr.live_html()
    assert "a client for GET /courses" not in html
    assert "Course progress" not in html
    assert '<div class="awaiting-state' not in html


def test_live_html_no_longer_claims_bosun_state_is_awaiting_orient(monkeypatch):
    """`GET /runs` now has a client (`library_client.list_live_runs`) and a
    real render — the Bosun panel must never again claim to be waiting on
    Orient, which is exactly what it said before this was built."""
    _patch_sources(monkeypatch)
    html = cr.live_html()
    assert "WAITING ON ORIENT — a client for GET /runs" not in html


def test_live_html_shows_a_live_run_when_the_bosun_register_has_one(monkeypatch):
    run = lc.LiveRunRecord(run_id="run-live-1", agent="deck-coder", task="ship the thing",
                           started_at=dt.datetime(2026, 9, 2, 20, 0), repo="agent-platform",
                           worktree="/x", pid=123, parent_run=None, holds=[])
    _patch_sources(monkeypatch, live_runs=_live_runs_result([run]))
    html = cr.live_html()
    assert "deck-coder" in html
    assert "ship the thing" in html


def test_live_html_names_the_reason_when_the_bosun_register_is_degraded(monkeypatch):
    """`available: False` is the Library successfully reporting its OWN
    upstream didn't answer — this must say so, never render as "no runs",
    which would be indistinguishable from the healthy empty case."""
    _patch_sources(monkeypatch, live_runs=_live_runs_result(
        available=False, reason="bosun unreachable"))
    html = cr.live_html()
    assert "bosun unreachable" in html
    assert "No runs on foot right now." not in html


def test_live_html_says_so_when_no_runs_are_on_foot(monkeypatch):
    _patch_sources(monkeypatch, live_runs=_live_runs_result())
    html = cr.live_html()
    assert "No runs on foot right now." in html


def test_live_html_in_flight_column_counts_a_crews_live_runs(monkeypatch):
    roster = _roster_one_crew(slug="vivy", label="Vivy")
    run = lc.LiveRunRecord(run_id="run-live-1", agent="a", task="t",
                           started_at=dt.datetime(2026, 9, 2, 20, 0), repo="vivy",
                           worktree="/x", pid=1, parent_run=None, holds=[])
    _patch_sources(monkeypatch, roster=roster, live_runs=_live_runs_result([run]))
    html = cr.live_html()
    assert "1 running" in html


def test_live_html_in_flight_column_is_em_dash_with_no_live_runs(monkeypatch):
    roster = _roster_one_crew(slug="vivy", label="Vivy")
    _patch_sources(monkeypatch, roster=roster)
    html = cr.live_html()
    assert "running</span>" not in html


def test_live_html_renders_a_collision_as_who_was_blocked_by_whom(monkeypatch):
    """The epic wants refusals so a collision between two crews is visible at
    all. A count cannot show that, so the page names both sides and the subtree
    they met on.

    Deliberately not "two crews wanting the same thing" — the epic's own
    framing, and one this row cannot support (reviewer, PR #200). The page
    shows who met where; what each of them was after is not on the wire."""
    _patch_sources(monkeypatch, refusals=[
        _refusal("ref1", "keel", requester="session-B", holder="session-A",
                 root="issue_epic1")])
    html = cr.live_html()
    assert "session-B" in html
    assert "session-A" in html
    assert "issue_epic1" in html


def test_the_caption_says_where_the_fence_sat_and_claims_nothing_more(
        monkeypatch):
    """**Two versions each claimed more than the row supports** (reviewer,
    PR #200, both rounds), so this pins the retreat rather than a distinction.

    Version one read `blocks_by` as "an epic fenced a sibling". Wrong:
    `live_conflicts` walks `[root_id, *ancestor_ids(root_id)]` and tags a lease
    on the requested item itself `"ancestor"` too.

    Version two compared the blocking root to the requested item and called
    equality a race for the same work. Wrong for the case it most needed to be
    right about: `root_id_for` makes a LEAF lease its PARENT, so two crews
    racing for the identical leaf under one epic store different ids and never
    compare equal.

    What survives is true of both: the caption says where the FENCE sat.
    Whether the other crew wanted the same item is not on this row, and
    `issue_01M0RCF408K7TBE0N9VP224MGA` asks the Library for it rather than
    guessing here."""
    _patch_sources(monkeypatch, refusals=[
        _refusal("ref1", "keel", root="issue_same", wanted="issue_same")])
    itself = cr.live_html()
    assert "the item itself" in itself
    assert "fences what it asked for" not in itself

    _patch_sources(monkeypatch, refusals=[
        _refusal("ref1", "keel", root="issue_epic", wanted="issue_child")])
    above = cr.live_html()
    assert "fences what it asked for" in above
    assert "the item itself" not in above


def test_a_race_for_one_leaf_is_not_captioned_as_the_item_itself(monkeypatch):
    """**The case version two got wrong, pinned so version four cannot.** Two
    crews both want leaf L under epic E. The first checks out L, which leases E
    because `root_id_for` makes a leaf lease its parent. The second is refused,
    storing `requested_item_id = L` and `blocking_root_id = E`.

    The fence sat on E, so the caption must say that. It must NOT say "the item
    itself", which was the reading that made a genuine race look like an epic
    fencing a sibling — and it must not claim the reverse either, because the
    row cannot tell this apart from E fencing a DIFFERENT leaf."""
    _patch_sources(monkeypatch, refusals=[
        _refusal("ref1", "keel", root="issue_epic_E", wanted="issue_leaf_L")])
    html = cr.live_html()
    assert "the item itself" not in html
    assert "fences what it asked for" in html
    assert "issue_epic_E" in html


def test_a_descendant_collision_is_captioned_as_one(monkeypatch):
    """The enum's other real value. The first version's test exercised this
    branch with `blocks_by="item"`, which the Library never emits — it passed
    because the code only special-cased `"ancestor"`, so it proved nothing about
    the value the code actually meets (reviewer, PR #200)."""
    _patch_sources(monkeypatch, refusals=[
        _refusal("ref1", "keel", blocks_by="descendant",
                 root="issue_child", wanted="issue_parent")])
    assert "sits under what it asked for" in cr.live_html()


def test_a_withheld_blocker_says_so_rather_than_rendering_three_blanks(
        monkeypatch):
    """**The Library omits the blocker when its subtree is out of the reading
    credential's scope**, sending `blocked: true` instead so the row still reads
    as "something held this" rather than "nothing did". Rendering that through
    the normal template gives "blocked by — on —", which reads as a parsing
    fault rather than a privacy boundary (reviewer, PR #200).

    Orient's own credential is `*`-scoped so this shape does not arrive today.
    That is a fact about one token, not a promise of the contract, and it is
    exactly the kind of thing that stops being true quietly."""
    _patch_sources(monkeypatch, refusals=[
        _refusal("ref1", "keel", blocked=True, wanted="issue_asked")])
    html = cr.live_html()
    assert "outside this view" in html
    assert "issue_asked" in html
    # The em-dash placeholder is what the blank-rendering bug looked like.
    assert "blocked by — on —" not in html


def test_the_collisions_section_says_it_is_unwindowed(monkeypatch):
    """It sits beside a rolling cost window, so the asymmetry reads as a bug to
    anyone who has not read the PR that defended it (reviewer, PR #200)."""
    _patch_sources(monkeypatch)
    assert "all time" in cr.live_html()


def test_live_html_says_so_plainly_when_no_crew_has_been_refused(monkeypatch):
    """The common case, and the one a bare empty div renders as a missing
    feature. One refusal exists across the whole live store, so this is what
    the section shows almost always."""
    _patch_sources(monkeypatch)
    assert "No crew has been refused a lease." in cr.live_html()


def test_a_refusal_with_no_product_is_still_shown_even_though_it_counts_nowhere(
        monkeypatch):
    """`product` is nullable on the row. Counting a null under a placeholder
    crew would invent an attribution the row does not carry; dropping it from
    the list would hide a real collision. So it counts nowhere and shows."""
    _patch_sources(monkeypatch, refusals=[
        _refusal("ref1", None, requester="orphan-session")])
    html = cr.live_html()
    assert "orphan-session" in html
    assert cr._refusals_by_crew([_refusal("ref1", None)]) == {}


def test_live_html_never_throws_on_every_source_empty(monkeypatch):
    _patch_sources(monkeypatch)
    html = cr.live_html()
    assert "Crews" in html


def test_live_html_shows_leases_as_alive_and_dead_not_just_a_total(monkeypatch):
    roster = {"projects": [{"slug": "vivy", "label": "Vivy", "repo": "/x", "lanes": []}],
             "autonomous": []}
    items = [_item("i1", "vivy", "claimed"), _item("i2", "vivy", "claimed")]
    leases = [_lease("l1", "i1", dead=False), _lease("l2", "i2", dead=True)]
    _patch_sources(monkeypatch, roster=roster, items=items, leases=leases)
    html = cr.live_html()
    assert "1 alive" in html
    assert "1 dead" in html


def test_live_html_marks_a_lease_near_expiry_before_it_expires_with_time_remaining(
        monkeypatch):
    """Nothing on the live store is near expiry right now — every real lease
    measured while filing this issue was already dead — so this is the only
    way to see the marking exist at all: construct one and render it through
    the real path, `live_html()`, rather than asserting on `_lease_state`
    alone."""
    roster = _roster_one_crew(slug="vivy", label="Vivy")
    items = [_item("i1", "vivy", "claimed")]
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    leases = [_lease("l1", "i1", dead=False, expires_at=now + dt.timedelta(minutes=4),
                     heartbeat_at=now - dt.timedelta(minutes=30))]
    _patch_sources(monkeypatch, roster=roster, items=items, leases=leases)
    html = cr.live_html()
    assert "near expiry" in html
    # The quoted attribute, not a bare substring: the stylesheet defines
    # `.cr-lease-near` regardless of whether any row uses it, so a bare
    # substring check would pass even with the marking never rendered.
    assert 'class="cr-lease-near"' in html
    assert re.search(r"1 near expiry \(\d+m\d{2}s\)", html)


def test_live_html_marks_a_lease_near_expiry_on_the_max_lease_arm_end_to_end(
        monkeypatch):
    """The full `live_html()` path for the case the finding named: a lease
    pinned against MAX_LEASE with a fresh heartbeat, and the served policy
    that lets `_lease_state` tell that arm apart from grace. Without the
    served `policy` threaded through, this renders "alive" — wrong."""
    roster = _roster_one_crew(slug="vivy", label="Vivy")
    items = [_item("i1", "vivy", "claimed")]
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    policy = lc.LeasePolicy(grace_seconds=4 * 3600, max_lease_seconds=24 * 3600)
    taken_at = now - dt.timedelta(hours=24) + dt.timedelta(minutes=4)
    leases = [_lease("l1", "i1", dead=False,
                     expires_at=taken_at + dt.timedelta(seconds=policy.max_lease_seconds),
                     heartbeat_at=now - dt.timedelta(seconds=5), taken_at=taken_at)]
    _patch_sources(monkeypatch, roster=roster, items=items, leases=leases, policy=policy)
    html = cr.live_html()
    assert "near expiry" in html
    assert 'class="cr-lease-near"' in html


def test_live_html_renders_near_expiry_alive_and_dead_as_three_distinct_states(
        monkeypatch):
    roster = _roster_one_crew(slug="vivy", label="Vivy")
    items = [_item("i1", "vivy", "claimed"), _item("i2", "vivy", "claimed"),
             _item("i3", "vivy", "claimed")]
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    leases = [
        _lease("l1", "i1", dead=False, expires_at=now + dt.timedelta(minutes=4),
              heartbeat_at=now - dt.timedelta(minutes=30)),  # near expiry
        _lease("l2", "i2", dead=False, expires_at=now + dt.timedelta(hours=4),
              heartbeat_at=now),                              # alive
        _lease("l3", "i3", dead=True),                        # dead
    ]
    _patch_sources(monkeypatch, roster=roster, items=items, leases=leases)
    html = cr.live_html()
    # Quoted attributes, not bare substrings — see the comment on the test
    # above for why a bare `"cr-lease-near" in html` check is a false
    # positive (the stylesheet always defines the class).
    assert 'class="cr-lease-near"' in html
    assert 'class="cr-lease-alive"' in html
    assert 'class="cr-lease-dead"' in html


# --- the assumption the Library's disclosure bound rests on -------------------


def test_crews_is_full_tier_only_because_the_disclosure_bound_depends_on_it():
    """**The Library bounds this disclosure on an assumption THIS page has to
    keep** (security-reviewer, PR #200).

    `GET /lease-refusals`'s docstring accepts that a refusal names the holder it
    collided with because "the row is already scoped to products the reader can
    see." That scoping is applied to ORIENT'S credential, which carries
    `products: ["*"]` — so the client receives every crew's refusals and the
    bound is actually enforced one layer up, by this site's own tier gate.

    Nothing in `crews_build.py` re-derives per-viewer scope; the page trusts the
    binary tier gate to be the only audience distinction that will ever exist.
    Add a narrower full-ish tier later and the Library's comment silently stops
    being true here, in a second codebase nobody would think to reread. This
    test is where that assumption is written down."""
    import server as srv
    assert "/crews" in srv._full_only_routes()


def test_a_hostile_requester_name_cannot_break_out_of_the_collision_line(
        monkeypatch):
    """`requester`, `blocking_holder` and `actor` are CALLER-SUPPLIED at refusal
    time — a session names its own owner — so they are the untrusted input in
    this diff, unlike the ids and timestamps the Library generates.

    Escaping is correct today; the point is to pin it against a later refactor
    of `_collision`, which builds its HTML by concatenation
    (security-reviewer, PR #200)."""
    nasty = '<script>alert(1)</script>'
    _patch_sources(monkeypatch, refusals=[
        _refusal("r1", "vivy", requester=nasty, holder=nasty, root=nasty)])
    html = cr.live_html()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# --- sessions and runs (issue_01M15C5W1GFGC81CW7YNCB0CP9) -------------------
#
# _empty_agent_runs (module-level autouse fixture) already points
# cr.AGENT_RUNS_ROOT at a private tmp_path root for every test below.


def _today():
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


# --- _date_dirs: bounded to exactly the window asked for --------------------


def test_date_dirs_only_opens_days_inside_the_window(tmp_path):
    root = tmp_path / "agent-runs"
    since = dt.date(2026, 8, 27)
    until = dt.date(2026, 8, 29)
    for d in ("2026-08-26", "2026-08-27", "2026-08-28", "2026-08-29", "2026-08-30"):
        (root / d).mkdir(parents=True)
    dirs = cr._date_dirs(since, until)
    assert [p.name for p in dirs] == ["2026-08-27", "2026-08-28", "2026-08-29"]


def test_date_dirs_skips_a_day_with_no_directory_on_disk(tmp_path):
    root = tmp_path / "agent-runs"
    (root / "2026-08-27").mkdir(parents=True)
    # 2026-08-28 deliberately absent
    (root / "2026-08-29").mkdir(parents=True)
    dirs = cr._date_dirs(dt.date(2026, 8, 27), dt.date(2026, 8, 29))
    assert [p.name for p in dirs] == ["2026-08-27", "2026-08-29"]


# --- resolve_window -----------------------------------------------------


def test_resolve_window_defaults_to_the_recent_slice_with_no_query():
    since, until, is_custom = cr.resolve_window(None)
    today = dt.datetime.now(dt.timezone.utc).date()
    assert until == today
    assert since == today - dt.timedelta(days=cr.RUNS_DEFAULT_WINDOW_DAYS - 1)
    assert is_custom is False


def test_resolve_window_honors_an_explicit_from_and_to():
    since, until, is_custom = cr.resolve_window({"from": "2026-08-01", "to": "2026-08-05"})
    assert (since, until, is_custom) == (dt.date(2026, 8, 1), dt.date(2026, 8, 5), True)


def test_resolve_window_swaps_a_backwards_range():
    since, until, _ = cr.resolve_window({"from": "2026-08-05", "to": "2026-08-01"})
    assert since == dt.date(2026, 8, 1)
    assert until == dt.date(2026, 8, 5)


def test_resolve_window_clamps_a_hand_typed_range_to_the_max(monkeypatch):
    since, until, _ = cr.resolve_window({"from": "2020-01-01", "to": "2026-08-29"})
    assert until == dt.date(2026, 8, 29)
    assert (until - since).days == cr.RUNS_MAX_WINDOW_DAYS


def test_resolve_window_ignores_unparseable_dates():
    # An unparseable `from` behaves as if it were never given — the default
    # recent slice, not a crash and not a bogus range.
    since, until, is_custom = cr.resolve_window({"from": "not-a-date"})
    today = dt.datetime.now(dt.timezone.utc).date()
    assert until == today
    assert since == today - dt.timedelta(days=cr.RUNS_DEFAULT_WINDOW_DAYS - 1)
    assert is_custom is False


# --- boundary dates: date.min/date.max must clamp, never raise OverflowError -


def test_resolve_window_does_not_raise_on_date_min_as_to():
    # `until - timedelta(days=N)` used to underflow past date.min here.
    since, until, is_custom = cr.resolve_window({"to": "0001-01-01"})
    assert until == dt.date.min
    assert since == dt.date.min
    assert is_custom is True


def test_resolve_window_does_not_raise_on_date_max_as_to():
    since, until, is_custom = cr.resolve_window({"to": "9999-12-31"})
    assert until == dt.date.max
    assert is_custom is True


def test_date_dirs_does_not_raise_when_until_is_date_max():
    # The loop's `d += timedelta(days=1)` used to overflow past date.max on
    # the final iteration.
    dirs = cr._date_dirs(dt.date.max, dt.date.max)
    assert dirs == []


def test_runs_html_does_not_500_on_boundary_to_dates(monkeypatch):
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    html_min = cr.runs_html({"product": "convivy-orient", "to": "0001-01-01"})
    assert "Crew runs" in html_min
    html_max = cr.runs_html({"product": "convivy-orient", "to": "9999-12-31"})
    assert "Crew runs" in html_max


# --- duration / clipping helpers ---------------------------------------


def test_elapsed_seconds_between_two_iso_timestamps():
    secs = cr._elapsed_seconds("2026-08-29T12:00:00Z", "2026-08-29T12:30:00Z")
    assert secs == 1800.0


def test_elapsed_seconds_is_none_on_unparseable_input():
    assert cr._elapsed_seconds(None, "2026-08-29T12:30:00Z") is None


def test_fmt_duration_shapes():
    assert cr._fmt_duration(None) == "—"
    assert cr._fmt_duration(45) == "45s"
    assert cr._fmt_duration(125) == "2m05s"
    assert cr._fmt_duration(3725) == "1h02m"


def test_clip_leaves_a_short_label_alone():
    assert cr._clip("short label") == "short label"


def test_clip_shortens_a_paragraph_and_never_sets_the_row_height():
    task = "word " * 50
    clipped = cr._clip(task, n=40)
    assert len(clipped) <= 40
    assert clipped.endswith("…")


def test_clip_of_falsy_input_is_none():
    assert cr._clip(None) is None
    assert cr._clip("") is None


def test_fmt_ago_shapes():
    assert cr._fmt_ago(None) == "—"
    assert cr._fmt_ago(30) == "just now"
    assert cr._fmt_ago(240) == "4m"
    assert cr._fmt_ago(2 * 3600 + 13 * 60) == "2h13m"
    assert cr._fmt_ago(3 * 86400 + 100) == "3d"


# --- _clippable: click-to-expand, in place of a plain "…" that threw the ---
# --- rest of the string away (the operator, reading /crews, 2026-09-02) -------------


def test_clippable_leaves_short_text_as_plain_escaped_text():
    assert cr._clippable("short") == "short"
    assert "<details" not in cr._clippable("short")


def test_clippable_wraps_long_text_in_a_details_the_reader_can_open():
    text = "word " * 50
    html = cr._clippable(text, n=40)
    assert "<details class=\"cr-clip\">" in html
    assert "<summary>" in html
    assert text in html  # the full text survives, not just the clipped summary


def test_clippable_escapes_a_hostile_task_string():
    nasty = "<script>alert(1)</script> " * 10
    html = cr._clippable(nasty, n=20)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_clippable_of_falsy_input_is_an_em_dash():
    assert cr._clippable(None) == "—"
    assert cr._clippable("") == "—"


# --- _session_crew_and_role: cwd is what separates the two populations ------


def test_session_crew_and_role_dispatched_agent_worktree():
    cwd = str(cr.WORKTREES_ROOT / "convivy-orient.orient-coder-do-a-thing")
    assert cr._session_crew_and_role(cwd) == ("convivy-orient", "dispatched")


def test_session_crew_and_role_human_primary_checkout():
    cwd = str(cr.CONVIVY_ROOT / "vivy")
    assert cr._session_crew_and_role(cwd) == ("vivy", "human")


def test_session_crew_and_role_neither_shape():
    assert cr._session_crew_and_role("/") == (None, "other")
    assert cr._session_crew_and_role(None) == (None, "other")


# --- _outcome_summary ----------------------------------------------------


def test_outcome_summary_in_flight_is_running_never_unknown():
    assert cr._outcome_summary(None, in_flight=True) == "running"
    assert cr._outcome_summary({"exit_status": "completed"}, in_flight=True) == "running"


def test_outcome_summary_names_the_gap_when_outcome_json_never_arrived():
    assert cr._outcome_summary(None, in_flight=False) == "no outcome recorded"


def test_outcome_summary_reports_status_and_counts():
    out = cr._outcome_summary(
        {"exit_status": "completed", "files_modified": ["a.py", "b.py"], "prs_opened": [1]},
        in_flight=False)
    assert out == "completed · 1 PR · 2 files"


@pytest.mark.parametrize("status,label", [
    # healthy
    ("completed", "completed"),
    ("committed_not_pushed", "committed, not pushed"),
    ("committed_local_only", "committed, no remote"),
    ("superseded", "superseded"),
    # attention
    ("primary_mutated", "primary checkout changed"),
    # failed
    ("errored", "errored"),
    ("killed", "killed"),
    ("budget_refused", "budget refused"),
    ("capacity_refused", "capacity refused"),
])
def test_outcome_summary_renders_every_exit_status_readably(status, label):
    """All ten of VALID_EXIT_STATUSES (agent-platform telemetry.py) — `running`
    is covered separately above since it never reaches _EXIT_LABELS."""
    out = cr._outcome_summary({"exit_status": status}, in_flight=False)
    assert out == label


def test_outcome_summary_falls_back_to_the_raw_name_for_an_unknown_status():
    out = cr._outcome_summary({"exit_status": "some_future_status"}, in_flight=False)
    assert out == "some_future_status"


# --- _run_view: the six fields, plus in-flight handling ---------------------


def test_run_view_of_a_finished_run(tmp_path):
    root = tmp_path / "agent-runs"
    d = _write_run(root, "2026-08-29", "run-x1", agent="sel-coder", label="ship-it",
                   started_at="2026-08-29T12:00:00Z", ended_at="2026-08-29T12:30:00Z",
                   cost_usd=4.5)
    run = {"meta": json.loads((d / "meta.json").read_text()),
          "cost": json.loads((d / "cost.json").read_text()),
          "outcome": json.loads((d / "outcome.json").read_text())}
    rv = cr._run_view(run, {})
    assert rv["agent"] == "sel-coder"
    assert rv["label"] == "ship-it"
    assert rv["in_flight"] is False
    assert rv["duration_s"] == 1800.0
    assert rv["model"] == "claude-sonnet-5"
    assert rv["cost_usd"] == 4.5
    assert rv["cost_known"] is True
    assert rv["outcome_text"] == "completed"


def test_run_view_of_an_in_flight_run_has_no_cost_or_outcome_yet(tmp_path):
    root = tmp_path / "agent-runs"
    d = _write_run(root, "2026-08-29", "run-x2", ended_at=None, no_cost=True, no_outcome=True)
    run = {"meta": json.loads((d / "meta.json").read_text()), "cost": None, "outcome": None}
    rv = cr._run_view(run, {})
    assert rv["in_flight"] is True
    assert rv["cost_known"] is False
    assert rv["outcome_text"] == "running"
    assert rv["duration_s"] is not None  # elapsed-so-far, not None


def test_run_view_carries_task_and_label_raw_and_unclipped():
    """Clipping and the task-over-label display preference are
    `_dispatched_for_html`'s job now, not `_run_view`'s — see the tests
    below for that behavior end to end."""
    run = {"meta": {"run_id": "r1", "agent": "coder", "task": "x" * 200, "label": None,
                    "started_at": None, "ended_at": None, "repo": "vivy",
                    "model_resolved": "m", "co_session": None},
          "cost": None, "outcome": None}
    rv = cr._run_view(run, {})
    assert rv["task"] == "x" * 200
    assert rv["label"] is None


# --- _dispatched_for_html: the task wins over the label ("dispatched for" --
# --- used to show a terse dispatcher slug like "rev-170" even when the -----
# --- readable task sentence was also available; counted against the live --
# --- corpus 2026-09-01..2026-09-02: label is never the only source) --------


def test_dispatched_for_shows_the_task_as_primary_when_both_exist():
    rv = {"task": "Round-3 security re-review of PR #475", "label": "rev-170"}
    html = cr._dispatched_for_html(rv)
    assert "Round-3 security re-review of PR #475" in html
    assert "rev-170" in html  # kept as the secondary handle, not thrown away


def test_dispatched_for_falls_back_to_the_label_when_there_is_no_task():
    rv = {"task": None, "label": "rev-170"}
    html = cr._dispatched_for_html(rv)
    assert "rev-170" in html


def test_dispatched_for_is_an_em_dash_when_neither_exists():
    assert "—" in cr._dispatched_for_html({"task": None, "label": None})


def test_run_view_joins_to_its_session_when_the_session_resolves():
    session = {"cwd": str(cr.CONVIVY_ROOT / "vivy"), "started_at": "x", "model": "m"}
    run = {"meta": {"run_id": "r1", "agent": "a", "task": "t", "label": None,
                    "started_at": None, "ended_at": None, "repo": "vivy",
                    "model_resolved": "m", "co_session": "sess-9"},
          "cost": None, "outcome": None}
    rv = cr._run_view(run, {"sess-9": session})
    assert rv["session"] is session
    assert rv["session_id"] == "sess-9"


def test_run_view_session_is_none_when_the_join_does_not_resolve():
    run = {"meta": {"run_id": "r1", "agent": "a", "task": "t", "label": None,
                    "started_at": None, "ended_at": None, "repo": "vivy",
                    "model_resolved": "m", "co_session": "sess-missing"},
          "cost": None, "outcome": None}
    rv = cr._run_view(run, {})
    assert rv["session"] is None
    assert rv["session_id"] == "sess-missing"


# --- _group_runs_by_crew: repo is authoritative, newest first --------------


def test_group_runs_by_crew_uses_the_runs_own_repo_field(tmp_path):
    root = tmp_path / "agent-runs"
    _write_run(root, "2026-08-29", "run-a", repo="vivy",
              started_at="2026-08-29T10:00:00Z", ended_at="2026-08-29T10:10:00Z")
    _write_run(root, "2026-08-29", "run-b", repo="selector",
              started_at="2026-08-29T11:00:00Z", ended_at="2026-08-29T11:10:00Z")
    date_dirs = cr._date_dirs(dt.date(2026, 8, 29), dt.date(2026, 8, 29))
    runs = cr._load_run_records(date_dirs)
    grouped = cr._group_runs_by_crew(runs, {})
    assert set(grouped) == {"vivy", "selector"}
    assert len(grouped["vivy"]) == 1


def test_group_runs_by_crew_sorts_newest_first(tmp_path):
    root = tmp_path / "agent-runs"
    _write_run(root, "2026-08-29", "run-early", repo="vivy",
              started_at="2026-08-29T09:00:00Z", ended_at="2026-08-29T09:10:00Z")
    _write_run(root, "2026-08-29", "run-late", repo="vivy",
              started_at="2026-08-29T15:00:00Z", ended_at="2026-08-29T15:10:00Z")
    date_dirs = cr._date_dirs(dt.date(2026, 8, 29), dt.date(2026, 8, 29))
    runs = cr._load_run_records(date_dirs)
    grouped = cr._group_runs_by_crew(runs, {})
    assert [rv["run_id"] for rv in grouped["vivy"]] == ["run-late", "run-early"]


def test_group_runs_by_crew_drops_a_run_with_no_repo(tmp_path):
    root = tmp_path / "agent-runs"
    d = root / "2026-08-29" / "run-norepo"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "run_id": "run-norepo", "agent": "a", "task": "t", "label": None,
        "started_at": "2026-08-29T10:00:00Z", "ended_at": "2026-08-29T10:10:00Z",
        "repo": None, "model_resolved": "m", "co_session": None,
    }))
    date_dirs = cr._date_dirs(dt.date(2026, 8, 29), dt.date(2026, 8, 29))
    runs = cr._load_run_records(date_dirs)
    grouped = cr._group_runs_by_crew(runs, {})
    assert grouped == {}


# --- _last_activity_by_crew: the max of three signals, never a liveness ----
# --- check (that's `heartbeat_at`'s wrong use — see the function's own -----
# --- docstring) --------------------------------------------------------------


def test_last_activity_by_crew_a_live_run_counts_as_activity_now():
    now = cr.orient_time.to_mst("2026-09-02T20:00:00Z")
    out = cr._last_activity_by_crew(
        ["vivy"], live_runs_by_crew={"vivy": [object()]}, runs_by_crew={},
        sessions_by_crew={}, now=now)
    assert out["vivy"] == now


def test_last_activity_by_crew_uses_the_newest_local_run_record():
    now = cr.orient_time.to_mst("2026-09-02T20:00:00Z")
    runs_by_crew = {"vivy": [
        {"started_at": "2026-09-02T10:00:00Z", "ended_at": "2026-09-02T10:30:00Z"},
        {"started_at": "2026-09-02T09:00:00Z", "ended_at": "2026-09-02T09:10:00Z"},
    ]}
    out = cr._last_activity_by_crew(["vivy"], live_runs_by_crew={}, runs_by_crew=runs_by_crew,
                                    sessions_by_crew={}, now=now)
    assert out["vivy"] == cr.orient_time.to_mst("2026-09-02T10:30:00Z")


def test_last_activity_by_crew_uses_the_newest_session_heartbeat():
    now = cr.orient_time.to_mst("2026-09-02T20:00:00Z")
    sessions_by_crew = {"vivy": [{"heartbeat_at": "2026-09-02T11:00:00Z"},
                                 {"heartbeat_at": "2026-09-02T08:00:00Z"}]}
    out = cr._last_activity_by_crew(["vivy"], live_runs_by_crew={}, runs_by_crew={},
                                    sessions_by_crew=sessions_by_crew, now=now)
    assert out["vivy"] == cr.orient_time.to_mst("2026-09-02T11:00:00Z")


def test_last_activity_by_crew_is_none_for_a_crew_with_no_signal_at_all():
    now = cr.orient_time.to_mst("2026-09-02T20:00:00Z")
    out = cr._last_activity_by_crew(["vivy"], live_runs_by_crew={}, runs_by_crew={},
                                    sessions_by_crew={}, now=now)
    assert out["vivy"] is None


# --- live_html: default order is most-recent-activity-first, not -----------
# --- alphabetical, and click-to-sort headers are rendered -------------------


def test_live_html_sorts_most_recent_activity_first_by_default(tmp_path, monkeypatch):
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-1", repo="vivy", started_at=f"{today}T10:00:00Z",
              ended_at=f"{today}T10:10:00Z")
    roster = {"projects": [
        {"slug": "vivy", "label": "Vivy", "repo": "/x", "lanes": []},
        {"slug": "quiet", "label": "Quiet", "repo": "/y", "lanes": []},
    ], "autonomous": []}
    _patch_sources(monkeypatch, roster=roster)
    html = cr.live_html()
    assert html.index(">Vivy<") < html.index(">Quiet<")


def test_live_html_a_crew_with_no_activity_signal_sorts_last(tmp_path, monkeypatch):
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-1", repo="active", started_at=f"{today}T10:00:00Z",
              ended_at=f"{today}T10:10:00Z")
    roster = {"projects": [
        {"slug": "quiet", "label": "Quiet", "repo": "/y", "lanes": []},
        {"slug": "active", "label": "Active", "repo": "/x", "lanes": []},
    ], "autonomous": []}
    _patch_sources(monkeypatch, roster=roster)
    html = cr.live_html()
    assert html.index(">Active<") < html.index(">Quiet<")


def test_live_html_renders_click_to_sort_column_headers(monkeypatch):
    _patch_sources(monkeypatch)
    html = cr.live_html()
    assert 'class="cr-sort-btn' in html
    assert 'data-col="0"' in html
    assert 'data-default-dir="desc"' in html  # "Last activity" is the default
    assert "cr-group" in html
    assert "sortGroups" in html  # the click-to-sort JS is actually embedded


# --- live_html: the per-crew runs panel -------------------------------------


def _roster_one_crew(slug="convivy-orient", label="Convivy Orient"):
    return {"projects": [{"slug": slug, "label": label, "repo": "/x", "lanes": []}],
           "autonomous": []}


def test_live_html_shows_a_runs_panel_for_a_crew_with_recent_runs(tmp_path, monkeypatch):
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-1", agent="orient-coder", label="ship-crews-page",
              repo="convivy-orient", started_at=f"{today}T10:00:00Z",
              ended_at=f"{today}T10:20:00Z")
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    html = cr.live_html()
    assert "orient-coder" in html
    assert "ship-crews-page" in html
    assert "cr-runs" in html


def test_live_html_runs_panel_has_column_headers(tmp_path, monkeypatch):
    """The operator, reading /crews, 2026-09-02: "the expanded table needs column
    headers" — `runs_html`'s standalone page already had them; the per-crew
    panel on /crews itself did not."""
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-1", repo="convivy-orient",
              started_at=f"{today}T10:00:00Z", ended_at=f"{today}T10:20:00Z")
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    html = cr.live_html()
    assert '<div class="cr-run cr-run-head">' in html
    assert "<span class=\"cr-run-when\">Started</span>" in html


def test_live_html_shows_no_runs_panel_for_a_crew_with_none_in_window(monkeypatch):
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    html = cr.live_html()
    assert 'class="cr-runs"' not in html


def test_live_html_renders_an_in_flight_run_as_running(tmp_path, monkeypatch):
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-live", repo="convivy-orient",
              started_at=f"{today}T10:00:00Z", ended_at=None, no_cost=True, no_outcome=True)
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    html = cr.live_html()
    assert "running" in html
    assert "in progress" in html  # the cost cell, honest about not being $0


def test_live_html_names_an_unresolved_session_rather_than_dropping_the_run(
        tmp_path, monkeypatch):
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-orphan", repo="convivy-orient", co_session="ghost-session",
              started_at=f"{today}T10:00:00Z", ended_at=f"{today}T10:10:00Z")
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    html = cr.live_html()
    assert "run-orphan" not in html  # run_id itself isn't rendered, but the row must be
    assert "session not recorded" in html


def test_live_html_names_a_session_with_no_recorded_model(tmp_path, monkeypatch):
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-1", repo="convivy-orient", co_session="sess-1",
              started_at=f"{today}T10:00:00Z", ended_at=f"{today}T10:10:00Z")
    _write_session(root, today, "sess-1", model="")
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    html = cr.live_html()
    assert "model not recorded" in html


def test_live_html_caps_the_panel_and_links_to_view_all(tmp_path, monkeypatch):
    root = tmp_path / "agent-runs"
    today = _today()
    for i in range(cr.RUNS_PANEL_LIMIT + 3):
        _write_run(root, today, f"run-{i:03d}", repo="convivy-orient", co_session=f"sess-{i}",
                  started_at=f"{today}T{i % 20:02d}:00:00Z", ended_at=f"{today}T{i % 20:02d}:10:00Z")
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    html = cr.live_html()
    assert "3 more in this window" in html
    assert "/crews/runs?product=convivy-orient" in html


def test_live_html_escapes_a_hostile_run_label(tmp_path, monkeypatch):
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-1", repo="convivy-orient",
              label="<script>alert(1)</script>",
              started_at=f"{today}T10:00:00Z", ended_at=f"{today}T10:10:00Z")
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    html = cr.live_html()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# --- runs_html: the custom-window click-through -----------------------------


def test_runs_html_with_no_product_asks_the_reader_to_pick_a_crew(monkeypatch):
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    html = cr.runs_html({})
    assert "Pick a crew" in html


def test_runs_html_with_an_unknown_product_also_asks_the_reader_to_pick_a_crew(monkeypatch):
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    html = cr.runs_html({"product": "not-a-real-crew"})
    assert "Pick a crew" in html


def test_runs_html_lists_a_crews_runs_over_the_default_window(tmp_path, monkeypatch):
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-1", agent="orient-coder", label="a-task",
              repo="convivy-orient", started_at=f"{today}T10:00:00Z",
              ended_at=f"{today}T10:10:00Z")
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    html = cr.runs_html({"product": "convivy-orient"})
    assert "orient-coder" in html
    assert "a-task" in html


def test_runs_html_honors_an_explicit_custom_range(tmp_path, monkeypatch):
    root = tmp_path / "agent-runs"
    _write_run(root, "2026-08-01", "run-old", agent="orient-coder", label="archived-task",
              repo="convivy-orient", started_at="2026-08-01T10:00:00Z",
              ended_at="2026-08-01T10:10:00Z")
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    # Outside the default window, so it should be absent with no range given...
    assert "archived-task" not in cr.runs_html({"product": "convivy-orient"})
    # ...and present once the range covers it.
    html = cr.runs_html({"product": "convivy-orient", "from": "2026-08-01", "to": "2026-08-01"})
    assert "archived-task" in html


def test_runs_html_says_so_when_a_crew_has_no_runs_in_the_chosen_window(monkeypatch):
    # slug "convivy-orient" carries the ADR-0042 crew-name override
    # ("Orient"), so this is what the roster's own label resolves to
    # regardless of the fixture's own (unused) `label=` default.
    _patch_sources(monkeypatch, roster=_roster_one_crew())
    html = cr.runs_html({"product": "convivy-orient"})
    assert "No runs for Orient in this window." in html


# --- _read_json: a syntactically valid but wrong-shaped record is skipped, --
# --- not propagated as an AttributeError on the next .get() ----------------


def test_read_json_returns_none_for_a_list(tmp_path):
    p = tmp_path / "meta.json"
    p.write_text("[1, 2, 3]")
    assert cr._read_json(p) is None


def test_read_json_returns_none_for_a_bare_null(tmp_path):
    p = tmp_path / "meta.json"
    p.write_text("null")
    assert cr._read_json(p) is None


def test_read_json_returns_none_for_a_bare_string(tmp_path):
    p = tmp_path / "meta.json"
    p.write_text('"oops"')
    assert cr._read_json(p) is None


def test_read_json_still_returns_a_dict_when_shaped_right(tmp_path):
    p = tmp_path / "meta.json"
    p.write_text(json.dumps({"a": 1}))
    assert cr._read_json(p) == {"a": 1}


def test_a_run_with_a_list_shaped_meta_is_skipped_and_other_crews_still_render(
        tmp_path, monkeypatch):
    """The finding: one bad record used to break the render for every crew's
    panel via AttributeError, because `_load_run_records` builds one flat
    list across ALL crews before grouping by repo. Pinned with the bad record
    under one crew's run and the asserted-rendering run under a DIFFERENT
    crew, so this only passes if per-crew isolation actually holds — a
    single-crew fixture would pass even if a bad record anywhere still broke
    every panel, since there'd be no second panel to prove survived."""
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-good", agent="orient-coder", label="good-task",
              repo="selector", started_at=f"{today}T10:00:00Z",
              ended_at=f"{today}T10:10:00Z")
    bad_dir = root / today / "run-bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "meta.json").write_text("[1, 2, 3]")
    roster = {"projects": [
        {"slug": "vivy", "label": "Vivy", "repo": "/x", "lanes": []},
        {"slug": "selector", "label": "Selector", "repo": "/y", "lanes": []},
    ], "autonomous": []}
    _patch_sources(monkeypatch, roster=roster)
    html = cr.live_html()
    assert "good-task" in html
    assert "orient-coder" in html


def test_a_run_with_list_shaped_exit_status_does_not_crash_other_crews(tmp_path, monkeypatch):
    """Round-2 finding: the wrong-shape guard closed the OUTER json-value
    check but not a wrong-typed FIELD inside an otherwise-valid dict.
    `exit_status` as a list is unhashable and crashes `_EXIT_LABELS.get` in
    `_outcome_summary`, one flat list deep across every crew — same
    two-crew shape as above so a regression that loses per-crew isolation
    for this nested case is actually caught."""
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-good", agent="orient-coder", label="good-task",
              repo="selector", started_at=f"{today}T10:00:00Z",
              ended_at=f"{today}T10:10:00Z")
    _write_run(root, today, "run-bad-status", agent="orient-coder", label="bad-status-task",
              repo="vivy", started_at=f"{today}T09:00:00Z",
              ended_at=f"{today}T09:10:00Z",
              outcome={"exit_status": ["x"], "files_modified": [], "prs_opened": []})
    roster = {"projects": [
        {"slug": "vivy", "label": "Vivy", "repo": "/x", "lanes": []},
        {"slug": "selector", "label": "Selector", "repo": "/y", "lanes": []},
    ], "autonomous": []}
    _patch_sources(monkeypatch, roster=roster)
    html = cr.live_html()
    assert "good-task" in html


def test_a_run_with_int_shaped_prs_opened_does_not_crash_other_crews(tmp_path, monkeypatch):
    """`prs_opened`/`files_modified` as a non-list crashes `len(...)` in
    `_outcome_summary` — same failure class and same two-crew isolation
    check as the exit_status case above."""
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-good", agent="orient-coder", label="good-task",
              repo="selector", started_at=f"{today}T10:00:00Z",
              ended_at=f"{today}T10:10:00Z")
    _write_run(root, today, "run-bad-prs", agent="orient-coder", label="bad-prs-task",
              repo="vivy", started_at=f"{today}T09:00:00Z",
              ended_at=f"{today}T09:10:00Z",
              outcome={"exit_status": "completed", "files_modified": [], "prs_opened": 3})
    roster = {"projects": [
        {"slug": "vivy", "label": "Vivy", "repo": "/x", "lanes": []},
        {"slug": "selector", "label": "Selector", "repo": "/y", "lanes": []},
    ], "autonomous": []}
    _patch_sources(monkeypatch, roster=roster)
    html = cr.live_html()
    assert "good-task" in html


def test_a_run_with_int_shaped_agent_does_not_crash_other_crews(tmp_path, monkeypatch):
    """`agent`/`label`/`model_resolved` as a non-string crashes `escape(...)`
    in `_run_row_html`, since `meta.get("agent") or "—"` only falls back on
    FALSY values, never on wrong types. Same two-crew isolation check."""
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-good", agent="orient-coder", label="good-task",
              repo="selector", started_at=f"{today}T10:00:00Z",
              ended_at=f"{today}T10:10:00Z")
    bad_dir = root / today / "run-bad-agent"
    bad_dir.mkdir(parents=True)
    (bad_dir / "meta.json").write_text(json.dumps({
        "run_id": "run-bad-agent", "agent": 7, "task": "t", "label": None,
        "started_at": f"{today}T09:00:00Z", "ended_at": f"{today}T09:10:00Z",
        "repo": "vivy", "model_resolved": "m", "co_session": None,
    }))
    roster = {"projects": [
        {"slug": "vivy", "label": "Vivy", "repo": "/x", "lanes": []},
        {"slug": "selector", "label": "Selector", "repo": "/y", "lanes": []},
    ], "autonomous": []}
    _patch_sources(monkeypatch, roster=roster)
    html = cr.live_html()
    assert "good-task" in html


def test_a_session_with_a_null_shaped_meta_is_skipped_and_others_still_render(
        tmp_path, monkeypatch):
    root = tmp_path / "agent-runs"
    today = _today()
    _write_run(root, today, "run-1", repo="convivy-orient", co_session="sess-good",
              started_at=f"{today}T10:00:00Z", ended_at=f"{today}T10:10:00Z")
    _write_session(root, today, "sess-good")
    bad_session_dir = root / today / "co-sess-bad"
    bad_session_dir.mkdir(parents=True)
    (bad_session_dir / "meta.json").write_text("null")
    date_dirs = cr._date_dirs(dt.date.fromisoformat(today), dt.date.fromisoformat(today))
    sessions = cr._load_session_records(date_dirs)
    assert "sess-good" in sessions
    assert len(sessions) == 1


def test_a_session_with_list_shaped_session_id_does_not_crash_the_load(tmp_path):
    """Round-2 finding: `session_id` as a list is unhashable, which crashed
    `out[meta["session_id"]] = meta` — the preceding `meta.get("session_id")`
    truthiness check doesn't imply hashability. A wrong-typed session_id is
    now normalized to None (falsy) and the record is skipped, same as a
    missing session_id, rather than propagating a TypeError."""
    root = tmp_path / "agent-runs"
    today = _today()
    _write_session(root, today, "sess-good")
    bad_session_dir = root / today / "co-sess-bad"
    bad_session_dir.mkdir(parents=True)
    (bad_session_dir / "meta.json").write_text(json.dumps({
        "kind": "co-session", "session_id": ["not", "a", "string"],
        "started_at": f"{today}T09:00:00Z", "heartbeat_at": f"{today}T09:05:00Z",
        "cwd": str(cr.CONVIVY_ROOT / "convivy-orient"), "product": "convivy-orient",
        "model": "", "transcript_path": "/x.jsonl", "ppid": 1,
    }))
    date_dirs = cr._date_dirs(dt.date.fromisoformat(today), dt.date.fromisoformat(today))
    sessions = cr._load_session_records(date_dirs)
    assert "sess-good" in sessions
    assert len(sessions) == 1
