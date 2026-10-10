"""Tests for velocity_build.py — the /velocity dashboard page."""
import datetime as dt
import json
import shutil
import subprocess
from html import escape

import pytest

import library_client
import velocity_build as vb
import waypoints
from models import utcnow


@pytest.fixture(autouse=True)
def _no_live_courses(monkeypatch):
    """Default to no live Courses, so a test that doesn't care about
    waypoint pace need not know /velocity's headline now also reads the
    Library (dashboards epic issue_01M2F3Q1B8J9E0WC5J1F1CMWA1) — same
    discipline `test_chart_house_build.py`'s own autouse fixtures use."""
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [])


def _course(course_id, product, title, *, version=1, entries):
    return library_client.CourseRecord(
        id=course_id, product=product, title=title, status="live", version=version,
        entries=entries, created_by="ops-mcp", created_at=None,
        updated_by="ops-mcp", updated_at=None)


def _entry(issue_id, state, title=None, resolved_at=None):
    return library_client.CourseEntry(issue_id=issue_id, state=state, title=title or issue_id,
                                      resolved_at=resolved_at)


def _sample():
    return {
        "generated_at": "2026-06-15T20:00:00",
        "bucket_order": ["Vivy", "Engineering-internal"],
        "notes": ["LOC is a weak signal.", "young-repo caveat here"],
        "alltime": {
            "total": {"loc_now": 5000, "commits_total": 100, "prs_total": 40},
            "buckets": {
                "Vivy": {"loc_now": 3000, "commits_total": 60, "prs_total": 25,
                         "repos": [{"repo": "vivy", "loc_now": 3000, "commits_total": 60,
                                    "prs_total": 25, "no_local": False, "no_gh": False}]},
                "Engineering-internal": {"loc_now": 2000, "commits_total": 40, "prs_total": 15, "repos": []}},
        },
        "windows": {
            "7": {"total": {"commits": 5, "prs_merged": 3, "features": 1, "loc_net": 100,
                            "reverts": 0, "adrs": 0, "arch": 0,
                            "change_failure_rate": 0.0, "ci_first_pass_pct": 100.0, "survival_pct": None},
                  "buckets": {"Vivy": {"commits": 5, "prs_merged": 3, "features": 1, "loc_net": 100,
                                       "reverts": 0, "adrs": 0, "arch": 0,
                                       "change_failure_rate": 0.0, "ci_first_pass_pct": 100.0, "survival_pct": None},
                              "Engineering-internal": {"commits": 0, "prs_merged": 0, "features": 0,
                                       "loc_net": 0, "reverts": 0, "adrs": 0, "arch": 2,
                                       "change_failure_rate": None, "ci_first_pass_pct": None, "survival_pct": None}}},
            "30": {"total": {"commits": 9, "prs_merged": 9, "features": 2, "loc_net": 999,
                             "reverts": 1, "adrs": 4, "arch": 7,
                             "change_failure_rate": 0.111, "ci_first_pass_pct": 80.0, "survival_pct": 90.0},
                   "buckets": {"Vivy": {}, "Engineering-internal": {}}},
            "90": {"total": {}, "buckets": {}},
        },
    }


def test_render_embeds_data_nav_asof_and_notes(tmp_path, monkeypatch):
    p = tmp_path / "velocity.json"
    p.write_text(json.dumps(_sample()))
    monkeypatch.setattr(vb, "DATA_PATH", p)
    html = vb.render(vb.load())
    # MST-labeled asof from generated_at (ARCH-171), window selector, nav link
    assert "MST" in html and 'data-w="7"' in html and 'data-w="90"' in html
    assert ">Velocity<" in html  # its own nav link, highlighted
    # embedded data + both buckets reachable client-side
    assert "Engineering-internal" in html and "Vivy" in html
    # notes rendered as list items
    assert "weak signal" in html and "young-repo caveat" in html
    # all-time tab + its data embedded for the client-side view
    assert 'data-w="all"' in html and "alltime" in html and "loc_now" in html


def test_nav_is_styled(tmp_path, monkeypatch):
    # After migrating to ds.nav_component, the old .topnav class is gone.
    # The Atelier nav renders class="atelier-nav" with aria-current="page"
    # on the Velocity sub-strip link, and ds tokens (e.g. --brand) replace
    # the old per-page --grad literal.
    p = tmp_path / "velocity.json"
    p.write_text(json.dumps(_sample()))
    monkeypatch.setattr(vb, "DATA_PATH", p)
    html = vb.render(vb.load())
    assert 'class="atelier-nav"' in html          # ds nav rendered
    assert 'aria-current="page"' in html          # active link marked
    assert ">Velocity<" in html                   # the sub-strip link is there
    assert "--brand" in html                       # ds token (not old --grad literal)


def test_load_missing_file_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(vb, "DATA_PATH", tmp_path / "nope.json")
    data = vb.load()
    assert data["windows"] == {} and data["bucket_order"] == []
    # render still produces a page (the JS shows an empty-state)
    assert "<html" in vb.render(data)


# --- freshness (owns the /velocity <-> /overview stalled agreement) ---------


def test_freshness_stalled_past_30h():
    fresh = vb.freshness({"generated_at": "2020-01-01T00:00:00"})
    assert fresh.stalled is True
    assert fresh.cadence == "daily (06:45 MST)"


def test_freshness_not_stalled_when_recent():
    import orient_time
    fresh = vb.freshness({"generated_at": orient_time.now().replace(tzinfo=None).isoformat()})
    assert fresh.stalled is False


def test_render_shows_stalled_banner_for_an_old_report():
    data = _sample()
    data["generated_at"] = "2020-01-01T00:00:00"
    html = vb.render(data)
    assert 'class="stalled-banner"' in html


def test_render_no_stalled_banner_for_a_fresh_report():
    import orient_time
    data = _sample()
    data["generated_at"] = orient_time.now().replace(tzinfo=None).isoformat()
    html = vb.render(data)
    assert 'class="stalled-banner"' not in html


# --- header measure + repo-count disclosure (issue_01M1S2PXE0SDKHJDBBZ5W5TZRT) ----
# These run the actual embedded client-side script in node against a minimal
# DOM stub and read back the HTML it builds, rather than grepping vb.render()'s
# output for a string that a Python-side change could produce either way — the
# fix here is entirely inside _BODY_SCRIPT, so a string-only assertion on
# vb.render() output could pass against unrelated text.


def _velocity_js_data():
    return {
        "generated_at": "2026-09-06T06:45:00",
        "bucket_order": ["Library", "Orient", "Ghost"],
        "notes": [],
        "history": [],
        "alltime": {
            "total": {"loc_now": 59312, "commits_total": 708, "prs_total": 415},
            "buckets": {
                "Library": {
                    "loc_now": 59312, "commits_total": 708, "prs_total": 415,
                    "repos": [
                        {"repo": "library", "loc_now": 49377, "commits_total": 371,
                         "prs_total": 90, "no_local": False, "no_gh": False},
                        {"repo": "knowledge", "loc_now": 9935, "commits_total": 337,
                         "prs_total": 325, "no_local": False, "no_gh": False},
                    ],
                },
                "Orient": {
                    "loc_now": 8000, "commits_total": 50, "prs_total": 20,
                    "repos": [{"repo": "convivy-orient", "loc_now": 8000, "commits_total": 50,
                               "prs_total": 20, "no_local": False, "no_gh": False}],
                },
                # "Ghost" is deliberately absent here — a bucket windows carries
                # that alltime has no entry for at all (repoCountLabel must not
                # throw on a missing bucket, only skip the count).
            },
        },
        "windows": {
            "30": {
                "total": {"commits": 758, "prs_merged": 235, "features": 72, "loc_net": 59313,
                          "reverts": 1, "adrs": 0, "arch": 0, "change_failure_rate": 0.004,
                          "ci_first_pass_pct": None, "survival_pct": 100.0},
                "buckets": {
                    "Library": {"commits": 708, "prs_merged": 235, "features": 72, "loc_net": 59313,
                                "reverts": 1, "adrs": 0, "arch": 0, "change_failure_rate": 0.004,
                                "ci_first_pass_pct": None, "survival_pct": 100.0},
                    "Orient": {"commits": 30, "prs_merged": 10, "features": 2, "loc_net": 500,
                               "reverts": 0, "adrs": 0, "arch": 0, "change_failure_rate": 0.0,
                               "ci_first_pass_pct": 100.0, "survival_pct": 100.0},
                    "Ghost": {"commits": 1, "prs_merged": 0, "features": 0, "loc_net": 10,
                              "reverts": 0, "adrs": 0, "arch": 0, "change_failure_rate": None,
                              "ci_first_pass_pct": None, "survival_pct": None},
                },
            },
        },
    }


def _run_velocity_js(data, driver_js):
    """Execute vb._BODY_SCRIPT for real in node against a minimal DOM stub,
    run `driver_js` against it, and return the JSON object it prints. Skips
    (not fails) when node is absent, matching test_engineering_build.py's
    _run_browse_js."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to exercise the velocity page JS")
    dom_stub = """
function makeEl(id) {
  return {
    id, _html: '', dataset: {},
    get innerHTML(){ return this._html; },
    set innerHTML(v){ this._html = v; },
    get textContent(){ return this._text !== undefined ? this._text : this._html; },
    set textContent(v){ this._text = v; },
    querySelectorAll(){ return []; },
    querySelector(){ return null; },
    setAttribute(){},
    addEventListener(){},
  };
}
const elements = {};
function el(id) { if (!elements[id]) elements[id] = makeEl(id); return elements[id]; }
global.document = { getElementById(id) { return el(id); } };
global._ds = { readUrlState: () => ({}), writeUrlState: () => {}, onUrlState: () => {} };
"""
    script = (
        vb._BODY_SCRIPT.replace("__DATA__", json.dumps(data))
        .removeprefix("<script>")
        .removesuffix("</script>")
    )
    full = f"'use strict';\n{dom_stub}\n{script}\n{driver_js}\n"
    r = subprocess.run([node, "-e", full], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"velocity page JS harness failed:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_windowed_table_header_states_net_loc_is_windowed():
    """draw()'s LOC column must say it's a windowed net figure, not just
    'Net LOC' — the header a reader compares against All-time's column,
    per issue_01M1S2PXE0SDKHJDBBZ5W5TZRT."""
    result = _run_velocity_js(_velocity_js_data(), r"""
console.log(JSON.stringify({ tableHtml: el('table').innerHTML }));
""")
    assert "Net LOC (30d)" in result["tableHtml"]


def test_alltime_table_header_states_loc_is_current():
    """drawAlltime()'s LOC column must say it's the codebase's current size,
    not the bare 'Lines of code' that reads as comparable to the windowed
    table's Net LOC column."""
    result = _run_velocity_js(_velocity_js_data(), r"""
document.getElementById('win').onclick({ target: { closest: () => ({ dataset: { w: 'all' } }) } });
console.log(JSON.stringify({ tableHtml: el('table').innerHTML }));
""")
    assert "LOC (current)" in result["tableHtml"]
    assert "Net LOC" not in result["tableHtml"]


def test_windowed_bucket_row_discloses_repo_count_for_multi_repo_bucket():
    """Library's windowed row must disclose that it aggregates 2 repos —
    V.alltime carries the repo list even while the windowed view is showing,
    per the issue's 'needs no new data' claim."""
    result = _run_velocity_js(_velocity_js_data(), r"""
console.log(JSON.stringify({ tableHtml: el('table').innerHTML }));
""")
    assert "Library" in result["tableHtml"]
    assert "(2 repos)" in result["tableHtml"]


def test_windowed_bucket_row_omits_repo_count_for_single_repo_bucket():
    """Orient has exactly one repo — no count should be printed (no data
    is being aggregated away, so a count would be silly noise, and a naive
    singular/plural fix could still emit '(1 repos)')."""
    result = _run_velocity_js(_velocity_js_data(), r"""
console.log(JSON.stringify({ tableHtml: el('table').innerHTML }));
""")
    assert "(1 repo)" not in result["tableHtml"]
    assert "(1 repos)" not in result["tableHtml"]


def test_windowed_bucket_row_omits_repo_count_when_alltime_has_no_entry():
    """Ghost is in windows.30.buckets but has no entry in alltime.buckets at
    all — repoCountLabel must not throw (the harness would report a nonzero
    exit) and must print no count for it."""
    result = _run_velocity_js(_velocity_js_data(), r"""
console.log(JSON.stringify({ tableHtml: el('table').innerHTML }));
""")
    assert "Ghost" in result["tableHtml"]
    ghost_row = result["tableHtml"].split("Ghost")[1][:60]
    assert "repo" not in ghost_row


def test_render_puts_both_page_notes_ahead_of_the_producers_own(tmp_path, monkeypatch):
    """The two page notes are module constants rather than producer data, so
    nothing else asserts they reach the page at all. They carry the only
    explanation a reader gets for the 2026-09-05 LOC step-change and for the
    Library bucket's identical 30/90-day figures, so a refactor that stopped
    prepending them would silently leave both anomalies unexplained."""
    p = tmp_path / "velocity.json"
    p.write_text(json.dumps(_sample()))
    monkeypatch.setattr(vb, "DATA_PATH", p)
    html = vb.render(vb.load())

    assert "step down on 2026-09-05" in html
    assert "un-shallowed from 337 commits to 2,118" in html
    # ahead of the producer's own notes, so the series break is read first
    assert html.index("step down on 2026-09-05") < html.index("weak signal")
    assert html.index("un-shallowed from 337") < html.index("weak signal")
    # rendered as list items through the same escape() path as producer notes.
    # Compared against the ESCAPED text, not the raw constant: the shallow-history
    # note carries apostrophes, and asserting on the raw string passes only for a
    # note that happens to contain no escapable character — which is how a test
    # like this quietly stops covering the escaping it claims to check.
    assert f"<li>{escape(vb._SERIES_BREAK_NOTE)}</li>" in html
    assert f"<li>{escape(vb._HISTORY_REPAIR_NOTE)}</li>" in html
    assert "&#x27;" in escape(vb._HISTORY_REPAIR_NOTE)  # the escaping is exercised


# --- waypoints headline (dashboards epic issue_01M2F3Q1B8J9E0WC5J1F1CMWA1) --


def test_waypoints_section_headlines_passed_count_and_per_course_progress(monkeypatch):
    course = _course("course_1", "bosun", "Make dispatch accountable", entries=[
        _entry("i1", "fixed", title="Ship the queue"),
        _entry("i2", "open", title="Wire the dashboard"),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    html = vb._waypoints_section()
    assert "1" in html and "Waypoints passed" in html
    assert "bosun" in html
    assert "Make dispatch accountable" in html
    assert "Wire the dashboard" in html  # the entry in flight, named


def test_waypoints_section_shows_windowed_pace_per_window(monkeypatch):
    """Plants the old "windowed rate unavailable" behaviour and confirms it's
    gone — /velocity now computes and shows pace for each of 7/30/90 days,
    total and per product (dashboards epic issue_01M2F3Q1B8J9E0WC5J1F1CMWA1)."""
    now = utcnow()
    course = _course("course_1", "bosun", "Make dispatch accountable", entries=[
        _entry("i1", "fixed", resolved_at=now - dt.timedelta(days=3)),
        _entry("i2", "fixed", resolved_at=now - dt.timedelta(days=20)),
        _entry("i3", "open"),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    html = vb._waypoints_section()
    assert "not shown" not in html and "unavailable" not in html
    assert "Waypoints passed (7d)" in html
    assert "Waypoints passed (30d)" in html
    assert "Waypoints passed (90d)" in html


def test_waypoints_section_renders_no_live_courses_without_error():
    html = vb._waypoints_section()  # autouse fixture already sets list_courses -> []
    assert "No live Courses" in html


def test_waypoints_section_degrades_on_library_outage(monkeypatch):
    def boom(**k):
        raise ConnectionError("Library unreachable")

    monkeypatch.setattr(library_client, "list_courses", boom)
    html = vb._waypoints_section()
    assert 'class="error-state"' in html


def test_render_puts_waypoints_headline_ahead_of_delivery_activity(monkeypatch):
    course = _course("course_1", "vivy", "Ship it", entries=[_entry("i1", "fixed")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)

    html = vb.render(vb.load())
    assert "Waypoints passed" in html
    assert "Delivery activity" in html
    assert html.index("Waypoints passed") < html.index("Delivery activity")


def test_waypoints_section_degrades_on_a_get_course_wide_failure_too(monkeypatch):
    """`fetch_courses` (now called directly by `_waypoints_section`) must
    still render "unavailable" when `list_courses` itself fails — the same
    failure mode `test_waypoints_section_degrades_on_library_outage` covers,
    re-asserted after the refactor that introduced the shared fetch."""
    def boom(**k):
        raise ConnectionError("Library unreachable")

    monkeypatch.setattr(library_client, "list_courses", boom)
    html = vb._waypoints_section()
    assert 'class="error-state"' in html
    assert "Weekly trend" not in html  # nothing below the failure renders either


# --- weekly waypoint-pace trend (issue_01M2HXZKHKZFVZK9K1MJ5KTKTA) ---------


_NOW = dt.datetime(2026, 9, 15, 12, 0)  # naive UTC, this codebase's convention


def test_weekly_pace_chart_renders_from_live_courses_mixing_terminal_and_open(monkeypatch):
    """Definition of done: the weekly chart renders from live Library reads
    over Courses mixing terminal and open epics with resolved times spread
    across several weeks."""
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "fixed", resolved_at=_NOW - dt.timedelta(days=21)),
        _entry("i2", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
        _entry("i3", "open"),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(waypoints, "utcnow", lambda: _NOW)

    html = vb._waypoints_section()
    assert "Weekly trend" in html
    assert "<svg" in html


def test_weekly_pace_chart_shows_a_quiet_week_as_zero_not_a_gap(monkeypatch):
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "fixed", resolved_at=_NOW - dt.timedelta(days=21)),
        _entry("i2", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(waypoints, "utcnow", lambda: _NOW)

    html = vb._waypoints_section()
    gap_week = waypoints._iso_week(_NOW - dt.timedelta(days=14))
    assert f">{gap_week}<" in html  # the breakdown table's zero row is present
    # its Total cell reads 0, not an absent row
    row = html.split(f">{gap_week}<")[1][:200]
    assert "<td>0</td>" in row


def test_weekly_pace_chart_marks_the_current_week_partial(monkeypatch):
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
        _entry("i2", "fixed", resolved_at=_NOW),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(waypoints, "utcnow", lambda: _NOW)

    html = vb._waypoints_section()
    assert "(partial)" in html
    assert 'partial"' in html


def test_weekly_pace_chart_degrades_on_library_outage(monkeypatch):
    def boom(**k):
        raise ConnectionError("Library unreachable")

    monkeypatch.setattr(library_client, "list_courses", boom)
    html = vb._waypoints_section()
    assert 'class="error-state"' in html
    assert "<svg" not in html


def test_weekly_pace_breakdown_splits_builder_from_products_and_sums(monkeypatch):
    builder = _course("course_1", "library", "Ship the queue", entries=[
        _entry("i1", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
        _entry("i2", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
    ])
    product = _course("course_2", "vivy", "Ship it", entries=[
        _entry("i3", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [builder, product])
    monkeypatch.setattr(library_client, "get_course",
                         lambda cid: {"course_1": builder, "course_2": product}[cid])
    monkeypatch.setattr(waypoints, "utcnow", lambda: _NOW)

    html = vb._waypoints_section()
    assert ">Builder<" in html
    assert ">vivy<" in html
    week = waypoints._iso_week(_NOW - dt.timedelta(days=7))
    row = html.split(f">{week}<")[1].split("</tr>")[0]
    cells = [c.split("<")[0] for c in row.split("<td>")[1:]]
    assert cells == ["2", "1", "3"]  # Builder, vivy, Total — adds up


def test_weekly_pace_unresolved_passed_entries_counted_and_disclosed(monkeypatch):
    """Definition of done: passed entries with no resolved time count in the
    total, in no week, and their count is shown on the page."""
    course = _course("course_1", "vivy", "Ship it", entries=[
        _entry("i1", "fixed"),  # passed, no resolved_at
        _entry("i2", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(waypoints, "utcnow", lambda: _NOW)

    html = vb._waypoints_section()
    assert "2 of 2 passed" in html  # both count toward the position/total
    assert "1 passed waypoint carries no resolved time" in html


def test_old_prs_merged_headline_no_longer_drives_the_page_headline(tmp_path, monkeypatch):
    """Plants the old behaviour (a velocity.json with a large prs_merged
    count) and confirms it no longer appears as the page's headline — only
    inside the secondary Delivery activity panel."""
    p = tmp_path / "velocity.json"
    p.write_text(json.dumps(_sample()))
    monkeypatch.setattr(vb, "DATA_PATH", p)
    html = vb.render(vb.load())
    assert "<h1>Engineering velocity</h1>" in html
    assert "Waypoints passed" in html
    # the old headline framing ("N PRs" as the top figure) is gone — merged-PR
    # counts now surface only inside the Delivery activity JS data blob below.
    assert html.index("Waypoints passed") < html.index("Delivery activity")


def test_weekly_pace_tooltip_escapes_a_product_name_carrying_markup(monkeypatch):
    """A Course's `product` is free text from the Library — nothing in
    `library_client` restricts it to a known slug — and it reaches the weekly
    chart's per-dot `<title>` through `by_group`. Unescaped, a product named
    `</title><script>…` breaks out of the SVG text node and runs in the browser
    of any full-tier viewer of /velocity (security-reviewer, PR #297, run
    run-73fa09c8). The breakdown table's header escaped the same keys three
    lines away; the tooltip did not."""
    hostile = '</title><script>alert(1)</script>'
    course = _course("course_1", hostile, "Ship it", entries=[
        _entry("i1", "fixed", resolved_at=_NOW - dt.timedelta(days=7)),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    monkeypatch.setattr(waypoints, "utcnow", lambda: _NOW)

    html = vb._waypoints_section()

    assert "<script>alert(1)</script>" not in html
    assert "&lt;/title&gt;&lt;script&gt;" in html
