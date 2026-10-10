"""Tests for activity_build.py — the /activity dashboard's freshness banner.

The session-list rendering itself has no dedicated test file (covered via
server tests); this pins the one thing added here: /activity discloses its
own feed's staleness the same way /overview's Activity card judges it
(issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment 902).
"""
import json
import shutil
import subprocess

import pytest

import activity_build as ab


def _snap(generated_at, count=3):
    return {"generated_at": generated_at, "session_count": count, "sessions": []}


def test_freshness_stalled_past_2h():
    fresh = ab.freshness(_snap("2020-01-01T00:00:00Z"))
    assert fresh.stalled is True
    assert fresh.cadence == "every 15 minutes"


def test_freshness_not_stalled_when_recent():
    import orient_time
    fresh = ab.freshness(_snap(orient_time.now().isoformat()))
    assert fresh.stalled is False


def test_render_shows_stalled_banner_for_an_old_snapshot():
    html = ab._render(_snap("2020-01-01T00:00:00Z"))
    assert 'class="stalled-banner"' in html


def test_render_no_stalled_banner_for_a_fresh_snapshot():
    import orient_time
    html = ab._render(_snap(orient_time.now().isoformat()))
    assert 'class="stalled-banner"' not in html


# --- the workers-only cost caveat (comment 903: hover-only isn't visible enough) ---


def test_render_has_a_visible_cost_caveat_container():
    """The "workers only" marker on a row was previously a tooltip, findable
    only by hovering. A reader who never hovers must still learn that a
    session's own main-loop cost isn't captured and that the totals above
    undercount it — so the page needs a container that isn't gated behind
    `title=`."""
    html = ab._render(_snap("2026-08-26T00:00:00Z"))
    assert 'id="costCaveat"' in html
    assert "main_loop_captured" in html
    assert "execution-layer" in html or "Keel" in html


def test_cost_caveat_keys_off_the_data_rather_than_always_showing():
    """An always-visible caveat would be as misleading as the hover-only one
    was quiet — it must show only when the current window actually has a
    workers-only row, and hide otherwise."""
    html = ab._render(_snap("2026-08-26T00:00:00Z"))
    assert 'caveat.style.display = "none"' in html
    assert 'caveat.style.display = ""' in html


# --- per-node outcome class (agent-platform#568: three healthy exit_status
# values, not one) --------------------------------------------------------
# These run the embedded client-side subTable() for real in node, rather
# than grepping ab._render()'s output for a string that a Python-side change
# could produce either way — the logic under test is entirely inside the
# JS, so a string-only assertion could pass against unrelated text.


def _run_activity_js(driver_js, nodes):
    """Execute activity_build's _SCRIPT for real in node against a minimal
    DOM stub, then run `driver_js` against it, and return the JSON object it
    prints. Skips (not fails) when node is absent, matching
    test_velocity_build.py's _run_velocity_js."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not available to exercise the activity page JS")
    snap = {
        "generated_at": "2026-09-01T00:10:00Z",
        "session_count": 1,
        "sessions": [{
            "started_at": "2026-09-01T00:00:00Z",
            "ended_at": "2026-09-01T00:10:00Z",
            "origin": "dispatched",
            "product": "orient",
            "agent": "co",
            "label": "test session",
            "total_usd": 1.0,
            "duration_s": 600,
            "worker_count": len(nodes),
            "main_loop_captured": True,
            "nodes": nodes,
        }],
    }
    dom_stub = """
function makeEl(id) {
  return {
    id, _html: '', value: '', dataset: {}, style: {}, onchange: null,
    get innerHTML(){ return this._html; },
    set innerHTML(v){ this._html = v; },
    querySelectorAll(){ return []; },
    querySelector(){ return null; },
    setAttribute(){},
    addEventListener(){},
  };
}
const elements = {};
function el(id) { if (!elements[id]) elements[id] = makeEl(id); return elements[id]; }
global.document = {
  getElementById(id) { return el(id); },
  querySelector(sel) {
    if (sel === "#tbl thead") return el("tblThead");
    if (sel === "#tbl tbody") return el("tblTbody");
    return null;
  },
  querySelectorAll(){ return []; },
};
global.window = { _ds: { readUrlState: () => ({}), writeUrlState: () => {}, onUrlState: () => {} } };
"""
    script = (
        ab._SCRIPT.replace("__DATA__", json.dumps(snap))
        .removeprefix("<script>")
        .removesuffix("</script>")
    )
    full = f"'use strict';\n{dom_stub}\n{script}\n{driver_js}\n"
    r = subprocess.run([node_bin, "-e", full], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"activity page JS harness failed:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def _node(exit_status):
    return {
        "exit_status": exit_status, "started_at": "2026-09-01T00:05:00Z",
        "agent": "orient-coder", "label": "a worker", "model": "claude-sonnet-5",
        "duration_s": 60, "usd": 0.5, "turns": 3, "depth": 0, "prs_opened": [],
    }


_HEALTHY_STATUSES = ["completed", "committed_not_pushed", "committed_local_only", "superseded"]
_ATTENTION_STATUSES = ["primary_mutated"]
_FAILED_STATUSES = ["errored", "killed", "budget_refused", "capacity_refused"]
_STATUSES = _HEALTHY_STATUSES + _ATTENTION_STATUSES + _FAILED_STATUSES + ["running"]


def _sub_table_html(statuses):
    result = _run_activity_js(
        r"""console.log(JSON.stringify({ html: subTable(SNAP.sessions[0].nodes) }));""",
        [_node(s) for s in statuses],
    )
    return result["html"]


def _sub_table_classes():
    return _sub_table_html(_STATUSES)


def test_healthy_exit_statuses_render_ok():
    """All ten of VALID_EXIT_STATUSES (agent-platform telemetry.py) are
    covered by name below; these four are the healthy class."""
    html = _sub_table_classes()
    assert '<span class="ok">completed</span>' in html
    assert '<span class="ok">committed, not pushed</span>' in html
    assert '<span class="ok">committed, no remote</span>' in html
    assert '<span class="ok">superseded</span>' in html


def test_primary_mutated_renders_attention_not_healthy_or_failed():
    html = _sub_table_classes()
    assert '<span class="warn">primary checkout changed</span>' in html


def test_running_stays_muted_and_failures_render_bad_with_readable_labels():
    html = _sub_table_classes()
    assert '<span class="muted">running</span>' in html
    assert '<span class="bad">errored</span>' in html
    assert '<span class="bad">killed</span>' in html
    assert '<span class="bad">budget refused</span>' in html
    assert '<span class="bad">capacity refused</span>' in html


def test_unknown_exit_status_renders_bad_with_the_raw_escaped_name():
    html = _sub_table_html(["some_future_status"])
    assert '<span class="bad">some_future_status</span>' in html


def test_toString_exit_status_does_not_match_through_the_prototype_chain():
    """A status literally named `toString` must not resolve through
    Object.prototype.toString and render as a false healthy/attention/failed
    match — every *_EXIT_LABELS lookup must check an own property
    (Object.hasOwn, or a Map, or Object.create(null)), not a bare truthy
    `dict[status]` read."""
    html = _sub_table_html(["toString"])
    assert '<span class="bad">toString</span>' in html
    assert '<span class="ok">' not in html
    assert '<span class="warn">' not in html
    assert "[native code]" not in html
    assert "function" not in html


def test_committed_not_pushed_used_to_render_bad_before_the_fix():
    """Plant the old two-branch behaviour (only `completed` is healthy) and
    confirm it would have rendered committed_not_pushed as `bad`, so this
    test would have caught the original defect."""
    html = _run_activity_js(
        r"""
const oldStyleOc = n =>
  n.exit_status === "completed" ? '<span class="ok">completed</span>'
  : (n.exit_status === "running" ? '<span class="muted">running</span>'
  : '<span class="bad">' + esc(n.exit_status) + '</span>');
console.log(JSON.stringify({ html: oldStyleOc({exit_status: "committed_not_pushed"}) }));
""",
        [_node("completed")],
    )
    assert html["html"] == '<span class="bad">committed_not_pushed</span>'


# --- thin rows: the page embeds no dispatch tree (issue_01M388E7WD8C83T5430YS88EJG)


def _big_snapshot(sessions=3000, nodes_each=40):
    node = {"exit_status": "completed", "started_at": "2026-09-01T00:05:00Z",
            "agent": "orient-coder", "label": "a worker doing a thing " * 4,
            "model": "claude-sonnet-5", "duration_s": 60, "usd": 0.5, "turns": 3,
            "depth": 1, "prs_opened": [], "task": "a long task description " * 20}
    return {"generated_at": "2026-09-01T00:10:00Z", "session_count": sessions,
            "sessions": [{"session_id": f"s-{i}", "started_at": "2026-09-01T00:00:00Z",
                          "ended_at": "2026-09-01T00:10:00Z", "origin": "dispatched",
                          "product": "orient", "agent": "co", "label": f"session {i}",
                          "total_usd": 1.0, "duration_s": 600, "worker_count": nodes_each,
                          "main_loop_captured": True, "nodes": [node] * nodes_each}
                         for i in range(sessions)]}


def test_the_page_stays_under_its_byte_budget_on_a_snapshot_far_past_one_page():
    """3,000 sessions of 40 workers each is about 50 times the page's budget
    when the trees are embedded; the rows alone must stay under 1.5 MB."""
    snap = _big_snapshot()
    full = len(json.dumps(snap))
    html = ab._render(snap)
    assert full > 30_000_000, "the fixture must be far larger than the budget"
    assert len(html.encode("utf-8")) < 1_500_000, len(html)


def test_thin_drops_only_the_tree():
    snap = _big_snapshot(sessions=2, nodes_each=3)
    out = ab.thin(snap)
    assert all("nodes" not in s for s in out["sessions"])
    assert [set(s) | {"nodes"} for s in out["sessions"]] == [set(s) for s in snap["sessions"]]
    assert out["generated_at"] == snap["generated_at"]
    assert all("nodes" in s for s in snap["sessions"]), "the input is not mutated"


def test_session_nodes_finds_one_tree_and_answers_none_for_an_unknown_id(tmp_path, monkeypatch):
    snap = _big_snapshot(sessions=3, nodes_each=2)
    snap["sessions"][1]["nodes"] = [{"agent": "the-one"}]
    f = tmp_path / "sessions.json"
    f.write_text(json.dumps(snap))
    monkeypatch.setattr(ab, "ACTIVITY_FILE", f)
    assert ab.session_nodes("s-1") == [{"agent": "the-one"}]
    assert ab.session_nodes("nope") is None
    assert ab.session_nodes("") is None


def test_expanding_a_row_fetches_its_tree_once_and_renders_it():
    """Runs the page's own loadNodes against a stub fetch: the first open
    asks /activity/nodes for that session, renders the answer, and a second
    open reuses it without asking again."""
    result = _run_activity_js(r"""
const calls = [];
global.fetch = (url) => { calls.push(url); return Promise.resolve({ ok: true,
  json: () => Promise.resolve({ nodes: [{ exit_status: "completed", agent: "fetched-agent",
    started_at: "2026-09-01T00:05:00Z", depth: 0, prs_opened: [] }] }) }); };
const s = { session_id: "a b/c" };
const inner = { innerHTML: "" };
loadNodes(s, inner);
setTimeout(() => { const first = inner.innerHTML; loadNodes(s, inner);
  console.log(JSON.stringify({ calls, first, second: inner.innerHTML })); }, 20);
""", [])
    assert result["calls"] == ["/activity/nodes?id=a%20b%2Fc"]
    assert "fetched-agent" in result["first"]
    assert result["second"] == result["first"]


def test_a_failed_fetch_says_so_in_the_row():
    result = _run_activity_js(r"""
global.fetch = () => Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
const inner = { innerHTML: "" };
loadNodes({ session_id: "x" }, inner);
setTimeout(() => console.log(JSON.stringify({ html: inner.innerHTML })), 20);
""", [])
    assert "load workers" in result["html"] and "HTTP 404" in result["html"]
