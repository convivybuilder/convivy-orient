"""The definition-of-done clause E19·4 (issue_01M35K5JQ7Z7MHYZGWRSB5F85M)
covers: "No project name outside a viewer's scope appears anywhere in what
they are served." Checked by rendering every route in server.py's own route
table as a scoped viewer with a second project present in the store, and
searching the rendered output for that project's slug, its repo name and an
item id from it.

**How every route is found, so a newly added one cannot dodge this walk.**
`_discovered_static_paths()` reads three sources, the same three server.py's
own comments point to when a route explains why it sits where it does:

  - `server._full_only_routes()` — the zero-argument dispatch table.
  - `server.SCOPED_DASHBOARDS` — the named tuple E19·3 pulled the per-scope
    dashboards into.
  - `server.SCOPED_ROUTES` — the named tuple E19·4 pulled nine more scoped
    pages into, dispatched through the same block as `SCOPED_DASHBOARDS`.
  - every literal `path == "/…"` and `path in ("/…", ...)` comparison in
    `handle_get`'s own source, found by regex rather than hand-listed — the
    shape every explicit block in that function uses to claim its route,
    including every one this PR adds. A route added in a fourth shape (a
    `path.startswith` prefix, which `handle_get` already uses for the
    dynamic per-id pages) is the one kind this scan cannot see on its own;
    `_DYNAMIC_PREFIXES` below names each of those by hand instead, and
    `test_every_handle_get_prefix_is_named` pins that list against the
    source the same way, so a fifth prefix added later fails loudly rather
    than rendering unchecked.

Every path this discovers is partitioned three ways, and the partition
itself is asserted (`test_every_discovered_route_is_accounted_for`) so a
newly added route must be filed into one of them before this file is
trusted again:

  - `SCOPED_AND_TESTED` — rendered below as a viewer scoped to "alpha" with
    a "beta" project also present in the store, and checked for "beta",
    "beta-repo" and a beta item id.
  - `_EXCLUDED` — a route this walk does not render, each with a reason.
    Most name an already-scoped page whose data this walk does not seed
    (a local snapshot file, the real KB index, a live `gh` call this file
    does not mock) rather than a gap in scoping itself; a few name a page
    E19·4 does not scope at all, which the PR body states plainly as left
    undone.
  - `_REDIRECTS` — a path that only ever 302s (server._MOVED, the retired
    aliases) or serves a static, pre-auth asset (`/sw.js`) — no project
    data to leak.
"""
from __future__ import annotations

import inspect
import json
import re

import pytest
from fastapi.testclient import TestClient

import access_identity as ident
import activity_build
import authz
import chart_house_build
import control_db
import dashboard_build
import merge_queue_build
import crew_registry
import library_client
import library_fake
import project_build
import roster_build
import server as srv

# --- the viewer this whole file renders as ----------------------------------

SCOPE = frozenset({"alpha"})       # holds "alpha" only, never "beta"
WHOLE_FLEET_SCOPE = frozenset({"alpha", "beta"})
EMAIL = "scoped-viewer@example.com"

# The second project's identifiers — every one of these must be absent from
# every SCOPED_AND_TESTED route's rendered body.
BETA_SLUG = "beta"
BETA_REPO = "beta-repo"
BETA_ITEM_ID = "issue_beta_001"
BETA_LEAK_MARKERS = (BETA_SLUG, BETA_REPO, BETA_ITEM_ID)

ALPHA_SLUG = "alpha"
ALPHA_REPO = "alpha-repo"
ALPHA_ITEM_ID = "issue_alpha_001"


# --- route discovery (so a new route cannot dodge this walk) ----------------


def _discovered_static_paths() -> set[str]:
    paths = set(srv._full_only_routes()) | set(srv.SCOPED_DASHBOARDS) | set(srv.SCOPED_ROUTES)
    source = inspect.getsource(srv.handle_get)
    paths.update(re.findall(r'path == "([^"]+)"', source))
    for tup in re.findall(r'path in \(([^)]*)\)', source):
        # Filtered to actual path-shaped matches: a tuple holding a bare `""`
        # alternative (`path in ("/", "", "/index.html")`) makes a naive
        # quoted-segment regex occasionally capture the literal text BETWEEN
        # two quote pairs instead of one quoted segment — harmless to drop,
        # since every real path here starts with "/".
        paths.update(p for p in re.findall(r'"([^"]+)"', tup) if p.startswith("/"))
    paths.update(srv._MOVED)
    paths.update(("/sw.js", srv.blog_posts_build.ROUTE, srv.blog_posts_build.ROUTE + "/"))
    return paths


# Dynamic prefixes `handle_get` matches with `path.startswith(...)` rather
# than an exact literal — `_discovered_static_paths`'s regex cannot see
# these, so they are named here by hand and pinned against the source by
# `test_every_handle_get_prefix_is_named` below, rather than trusted by
# inspection alone.
_DYNAMIC_PREFIXES = (
    srv._ISSUE_PREFIX, srv._SIGNAL_PREFIX, srv._OLD_SIGNAL_PREFIX,
    srv._REFERENCE_DOC_PREFIX, srv._PROJECT_PREFIX, srv._EDITOR_JS_PREFIX,
)


def test_every_handle_get_prefix_is_named():
    source = inspect.getsource(srv.handle_get)
    prefix_vars = re.findall(r'path\.startswith\((_[A-Z_]+)\)', source)
    named = {
        "_ISSUE_PREFIX": srv._ISSUE_PREFIX, "_SIGNAL_PREFIX": srv._SIGNAL_PREFIX,
        "_OLD_SIGNAL_PREFIX": srv._OLD_SIGNAL_PREFIX,
        "_REFERENCE_DOC_PREFIX": srv._REFERENCE_DOC_PREFIX,
        "_PROJECT_PREFIX": srv._PROJECT_PREFIX, "_EDITOR_JS_PREFIX": srv._EDITOR_JS_PREFIX,
    }
    missing = sorted(set(prefix_vars) - set(named))
    assert not missing, (
        f"handle_get matches a path prefix this walk's _DYNAMIC_PREFIXES list "
        f"does not know about: {missing} — add it there (or above, if it needs "
        f"its own seeded id) before trusting this file again")


# Routes this walk renders and checks for a "beta" leak, as a viewer scoped
# to "alpha" alone.
SCOPED_AND_TESTED = frozenset({
    "/engineering/issues", "/engineering/course", "/engineering/corrections",
    "/engineering/blockers", "/chart-house", "/crews", "/crews/runs",
    "/archives", "/archives/prs", "/archives/changelogs", "/reference",
    "/engineering/roster",
    # E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M:
    "/", "/cost-performance", "/activity",
    "/engineering/merge-queue", "/engineering/changelog", "/engineering/e2e",
    "/products/release-notes", "/reference/briefings",
})

# Routes this walk does not render, each with the reason — see the module
# docstring for the two shapes a reason takes.
_EXCLUDED = {
    # Already scoped (E19·3), reading a local snapshot file (a scorecard
    # ledger, velocity.json, a code-health harvest, an alert-policy
    # snapshot) this walk does not seed. Each has its own dedicated scope
    # test suite asserting the same invariant against its own fixtures.
    "/spending": "covered by tests/test_dashboard_build.py's own scope tests",
    "/velocity": "covered by tests/test_velocity_build.py's own scope tests",
    "/health": "covered by tests/test_code_health_build.py's own scope tests",
    "/alerts": "covered by tests/test_alerts_build.py's own scope tests",
    # E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M: partially scoped — the one
    # table or panel on each page that carries a repo/project dimension
    # (`by_repo` on /security and /warrant, the individuation-coverage panel
    # on /kb) IS filtered to scope now; the rest of each page (`overall`,
    # `by_model`, the golden-set eval, live queries, the KB write audit) has
    # no project dimension at all to filter — gated on `whole_fleet` instead
    # (see each builder's own `render`/`live_html` docstring for which).
    # Each reads a local snapshot file (security-report.json,
    # warrant-report.json, kb-eval.json / kb-queries.jsonl /
    # kb-materializations.jsonl) this walk does not seed, so the filtering
    # itself is proven by that builder's own dedicated scope tests instead.
    "/security": "partially scoped (by_repo); covered by tests/test_security_build.py's "
                "own scope tests, reading a local snapshot file this walk doesn't seed",
    "/warrant": "partially scoped (by_repo); covered by tests/test_warrant_build.py's "
               "own scope tests, reading a local snapshot file this walk doesn't seed",
    "/kb": "partially scoped (individuation coverage); covered by tests/test_kb_build.py's "
          "own scope tests, reading local snapshot files this walk doesn't seed",
    # A LOOKUP by an explicit agent/crew/model triple (its own `_SCOPE_NOTE`),
    # not a browsable directory this walk can seed two projects into and
    # check for a leak the way it checks a listing page. The one cross-project
    # LIST it embeds — the agent/crew suggestion datalist — is filtered to
    # scope (E19·4) and covered by its own dedicated test
    # (tests/test_policy_build.py::test_datalist_suggestions_are_restricted_to_scope).
    # The resolve result itself is not scope-gated — see policy_build.
    # live_html's own docstring for why that was considered and left out.
    "/engineering/policy": "a lookup, not a directory; its one cross-project list is "
                           "covered by tests/test_policy_build.py's own scope test",
    "/reference/arch-log": "renders only for a whole-fleet scope and answers anyone else "
                           "with the contractor's 404; covered by tests/test_server.py::"
                           "test_arch_log_renders_only_for_a_whole_fleet_scope",
    # /overview shows no project name at all — each card is a dashboard
    # label plus an aggregate number (e.g. "Cost per Merged PR: $0.50"),
    # never a project. Confirmed rather than assumed: this walk's own
    # positive control failed here first, with no "beta" marker reaching
    # the page even for a whole-fleet viewer. The scoping work is in each
    # underlying section function instead (E19·4,
    # issue_01M35K5JQ7Z7MHYZGWRSB5F85M — `_cost_performance_section`,
    # `_activity_section`, `_velocity_section`, `_code_health_section`,
    # `_e2e_section` filter to scope; `_spending_section`, `_warrant_
    # section`, `_security_section`, `_kb_section` are fleet-only and
    # omitted from the grid for a scoped viewer), proven by overview_
    # build's own section-level tests in tests/test_home_overview_build.py.
    "/overview": "shows no project name at all (card labels + aggregate numbers only); "
                "its sections are scoped individually, covered by "
                "tests/test_home_overview_build.py's own section tests",
    # Verified by reading the source (E19·4): neither module references a
    # `product`/`project` field anywhere, so there is no project-scoped data
    # for a test to exercise — unlike /room just below, no mechanism exists
    # here to name a dedicated test against.
    "/writing-desk": "personal blog-draft page, carries no project-scoped data "
                     "(verified: writing_desk_build.py has no product/project field)",
    "/writing-desk/": "personal blog-draft page, carries no project-scoped data "
                      "(verified: writing_desk_build.py has no product/project field)",
    srv.blog_posts_build.ROUTE: "personal posts, carries no project-scoped aggregate "
                                "(verified: blog_posts_build.py has no product/project field)",
    srv.blog_posts_build.ROUTE + "/": "personal posts, carries no project-scoped aggregate "
                                      "(verified: blog_posts_build.py has no product/project field)",
    "/room": "reads kb_index.entitled_docs against a control-DB session and the "
             "real KB index; covered by tests/test_reading_room.py's own scope tests",
    "/room/": "reads kb_index.entitled_docs against a control-DB session and the "
              "real KB index; covered by tests/test_reading_room.py's own scope tests",
    # A JSON API over a session id, not a page this walk renders and scans
    # for a leaked marker. Scoped the same way /activity's own sessions are
    # (E19·4): an out-of-scope session answers like an unknown id, covered
    # by its own dedicated tests (tests/test_activity_build.py::
    # test_session_nodes_finds_one_tree_and_answers_none_for_an_unknown_id,
    # tests/test_server.py::test_activity_nodes_serves_one_tree_to_the_full_tier_only).
    "/activity/nodes": "JSON API over a session id; covered by its own dedicated "
                      "out-of-scope tests, not a project-scoped page",
    "/dashboard": "redirects to /spending, no content of its own",
}

# Paths that only ever 302 (server._MOVED, and the retired-surface aliases
# handled by name/prefix just above it) or serve a static, pre-auth asset —
# no project data is ever rendered.
_REDIRECTS = frozenset(set(srv._MOVED) | {
    "/sw.js", "/knowledge", "/knowledge/", "/orientation", "/orientation/",
    "/engineering/briefs", "/memory/briefs", "/reference-docs",
    "/engineering/system-design", "/engineering/design",
})


def test_every_discovered_route_is_accounted_for():
    """A route this file has not filed into SCOPED_AND_TESTED, `_EXCLUDED` or
    `_REDIRECTS` fails here rather than rendering unchecked — the "cannot
    grow silently" requirement."""
    discovered = _discovered_static_paths()
    accounted = SCOPED_AND_TESTED | set(_EXCLUDED) | _REDIRECTS
    missing = sorted(discovered - accounted)
    assert not missing, (
        f"route(s) discovered in server.py that this walk has not filed "
        f"anywhere: {missing} — add each to SCOPED_AND_TESTED (and render it "
        f"below) or to _EXCLUDED (with a reason)")
    # And the reverse: nothing claimed here that server.py no longer has,
    # which is what would let a retired route's entry linger unnoticed.
    extra = sorted(accounted - discovered - {"__placeholder__"})
    assert not extra, (
        f"this walk still lists a route server.py no longer discovers: "
        f"{extra} — remove it")


# --- the fixture every rendered route below shares ---------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    library_fake.patch(monkeypatch)
    library_fake.seed_person_scope(EMAIL, SCOPE)

    # The registry crews_build/project_build resolve a viewer's scope
    # (project slugs: "alpha"/"beta") into the repo identity those two
    # pages actually key rows by ("alpha-repo"/"beta-repo") — the same
    # project-slug/repo-name split `crew_registry.repos_in_scope`'s own
    # docstring names for bosun/agent-platform.
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "alpha": {"crew": "alpha", "lifecycle": "active", "repos": [ALPHA_REPO]},
        "beta": {"crew": "beta", "lifecycle": "active", "repos": [BETA_REPO]},
    }))
    monkeypatch.setattr(crew_registry, "REGISTRY", registry)

    # crews_build's own data sources, none of which library_fake.patch covers
    # (it is not Orient's own vault data — see crews_build's module
    # docstring). Minimal non-empty roster so /crews has rows to filter.
    roster = {"projects": [
        {"slug": ALPHA_REPO, "label": "Alpha", "repo": "/x", "lanes": []},
        {"slug": BETA_REPO, "label": "Beta", "repo": "/y", "lanes": []},
    ], "autonomous": []}
    monkeypatch.setattr(roster_build, "load_roster", lambda: roster)
    monkeypatch.setattr(library_client, "list_leases",
                        lambda **k: library_client.LeaseListResult(leases=[], policy=None))
    monkeypatch.setattr(library_client, "list_lease_refusals", lambda **k: [])
    monkeypatch.setattr(library_client, "list_live_runs",
                        lambda: library_client.LiveRunsResult(
                            runs=[], available=True, reason=None, generated_at=None,
                            stale_after_seconds=None, count=0))
    monkeypatch.setattr(chart_house_build, "_cost_rollup", lambda: {})

    # project_build shells out to `gh`; stubbed per the same seam
    # tests/test_project_build.py's own `_stub_fetch` uses.
    monkeypatch.setattr(project_build, "_fetch", lambda name, force=False: {
        "repo_exists": True, "open_prs": [], "merged_prs": [], "commits": [],
        "bead": None, "bead_error": None,
    })

    # dashboard_build.normalize() (/cost-performance) reads these jsonl
    # ledgers from disk — one scratch scorecard row per project, each with
    # its own repo so the per-repo filter has something to drop.
    scorecard = tmp_path / "scorecard.jsonl"
    scorecard.write_text("\n".join(json.dumps(r) for r in [
        {"run_id": "r-alpha", "date": "2026-01-01", "agent": "coder", "model": "claude-sonnet-5",
         "billing_source": "subscription", "repo": ALPHA_REPO, "cost_usd": 1.0,
         "pr_state": "MERGED", "merged": True, "pr_number": 1},
        {"run_id": "r-beta", "date": "2026-01-01", "agent": "coder", "model": "claude-sonnet-5",
         "billing_source": "subscription", "repo": BETA_REPO, "cost_usd": 2.0,
         "pr_state": "MERGED", "merged": True, "pr_number": 2},
    ]) + "\n")
    monkeypatch.setattr(dashboard_build, "SCORECARD", scorecard)
    monkeypatch.setattr(dashboard_build, "PA_LEDGER", tmp_path / "absent-pa.jsonl")
    monkeypatch.setattr(dashboard_build, "JUDGE", tmp_path / "absent-judge.jsonl")

    # activity_build (/activity) reads its own session snapshot from disk —
    # one scratch session per project, keyed by repo (the field activity
    # sessions carry — "the repo the session targeted", this page's own
    # tooltip) the same way `crew_registry.repos_in_scope` resolves scope.
    sessions_file = tmp_path / "sessions.json"
    sessions_file.write_text(json.dumps({
        "generated_at": "2026-01-01T00:00:00Z", "session_count": 2,
        "sessions": [
            {"session_id": "s-alpha", "product": ALPHA_REPO, "origin": "interactive",
             "agent": "co", "label": "Alpha session", "total_usd": 1.0, "duration_s": 60,
             "worker_count": 0, "started_at": "2026-01-01T00:00:00Z", "nodes": []},
            {"session_id": "s-beta", "product": BETA_REPO, "origin": "interactive",
             "agent": "co", "label": "Beta session", "total_usd": 1.0, "duration_s": 60,
             "worker_count": 0, "started_at": "2026-01-01T00:00:00Z", "nodes": []},
        ],
    }))
    monkeypatch.setattr(activity_build, "ACTIVITY_FILE", sessions_file)

    # merge_queue_build (/engineering/merge-queue) shells out to `gh`;
    # stubbed to the two in-scope/out-of-scope rows `_seed_two_projects`
    # would otherwise have no way to inject into a live GitHub call.
    monkeypatch.setattr(merge_queue_build, "_fetch_open_prs", lambda force=False: [
        {"repo": ALPHA_REPO, "number": 1, "title": "Alpha PR", "url": "https://x/a1",
         "author": "a", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
         "decision": "", "ci": "passing", "mergeable": "", "draft": False,
         "conflicting": False, "changes": False, "cleared": True, "group": "cleared"},
        {"repo": BETA_REPO, "number": 2, "title": "Beta PR", "url": "https://x/b2",
         "author": "b", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
         "decision": "", "ci": "passing", "mergeable": "", "draft": False,
         "conflicting": False, "changes": False, "cleared": True, "group": "cleared"},
    ])

    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": [EMAIL, "admin@example.com"]}))
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(srv, "SITE", tmp_path / "site")
    monkeypatch.setattr(ident, "TIERS", tiers)
    monkeypatch.setattr(ident, "JWT_CONFIG", jwtc)
    monkeypatch.setattr(srv, "ACCESS_LOG", tmp_path / "access.log")
    monkeypatch.setattr(authz, "ROLES", tmp_path / "roles.json")
    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


def _seed_two_projects():
    """One item/doc/course per project, named so a leak is unmistakable in
    the assertion rather than a coincidental substring."""
    library_fake.seed_issue(id=ALPHA_ITEM_ID, product=ALPHA_SLUG, title="Alpha work",
                            state="open")
    library_fake.seed_issue(id=BETA_ITEM_ID, product=BETA_SLUG, title="Beta work",
                            state="open")
    # One blocker (needs=operator) per project, for /engineering/blockers —
    # `lane_of` routes an item here only when it needs the operator, which
    # neither item above does.
    library_fake.seed_issue(id="issue_alpha_blocker", product=ALPHA_SLUG,
                            title="Alpha blocker", state="open", needs="operator")
    library_fake.seed_issue(id="issue_beta_blocker", product=BETA_SLUG,
                            title="Beta blocker", state="open", needs="operator")
    library_fake.seed_corrections_queue(ALPHA_SLUG)
    library_fake.seed_corrections_queue(BETA_SLUG)
    library_fake.seed_course(id="course_alpha", product=ALPHA_SLUG, title="Alpha course",
                            entries=[{"issue_id": ALPHA_ITEM_ID, "state": "open"}])
    library_fake.seed_course(id="course_beta", product=BETA_SLUG, title="Beta course",
                             entries=[{"issue_id": BETA_ITEM_ID, "state": "open"}])
    library_fake.seed_signal(id="sig_alpha_001", source="github_review", kind="pr_comment",
                             actor="reviewer", ingested_by="github-capture",
                             product=ALPHA_SLUG, body="alpha signal body")
    library_fake.seed_signal(id="sig_beta_001", source="github_review", kind="pr_comment",
                             actor="reviewer", ingested_by="github-capture",
                             product=BETA_SLUG, body="beta signal body")
    library_fake.seed_signal(id="desc_alpha_001", source="github", kind="changelog",
                             actor="bot", ingested_by="github-capture", product=ALPHA_SLUG,
                             body="alpha merged", refs={"pr": 1, "url": "https://x/a1"})
    library_fake.seed_signal(id="desc_beta_001", source="github", kind="changelog",
                             actor="bot", ingested_by="github-capture", product=BETA_SLUG,
                             body="beta merged", refs={"pr": 2, "url": "https://x/b1"})
    # The AUTHORED changelog (/engineering/changelog reads source="co" only —
    # changelog_build.CHANGELOG_AUTHORED_SOURCES — distinct from the
    # github-derived raw-merge rows seeded above, which that page excludes).
    library_fake.seed_signal(id="chglog_alpha_001", source="co", kind="changelog",
                             actor="the-operator", ingested_by="co", product=ALPHA_SLUG,
                             body="Alpha entry", refs={"pr": 3, "url": "https://x/a3"})
    library_fake.seed_signal(id="chglog_beta_001", source="co", kind="changelog",
                             actor="the-operator", ingested_by="co", product=BETA_SLUG,
                             body="Beta entry", refs={"pr": 4, "url": "https://x/b4"})
    # CI test results (/engineering/e2e reads source="ci" —
    # engineering_e2e_build.CI_SOURCE).
    library_fake.seed_signal(id="ci_alpha_001", source="ci", kind="test-result",
                             stream="e2e-stage-smoke", actor="ci-fetch", ingested_by="ci-fetch",
                             product=ALPHA_SLUG, body="alpha ci", meta={"status": "pass"})
    library_fake.seed_signal(id="ci_beta_001", source="ci", kind="test-result",
                             stream="e2e-stage-smoke", actor="ci-fetch", ingested_by="ci-fetch",
                             product=BETA_SLUG, body="beta ci", meta={"status": "pass"})
    # Release notes (/products/release-notes reads kind="release_note" —
    # release_notes_build._KIND).
    library_fake.seed_signal(id="rn_alpha_001", source="release-notes", kind="release_note",
                             stream="v1.0", actor="the-operator", ingested_by="release-notes",
                             product=ALPHA_SLUG, body="Alpha release note")
    library_fake.seed_signal(id="rn_beta_001", source="release-notes", kind="release_note",
                             stream="v1.0", actor="the-operator", ingested_by="release-notes",
                             product=BETA_SLUG, body="Beta release note")
    # last_reviewed set so Home's own Recent Docs panel (home_build.
    # _recent_docs, which requires it truthy) has something to show —
    # otherwise the walk's "beta" leak check on "/" would pass vacuously
    # with nothing a leak could ever appear in.
    library_fake.seed_reference_doc(id=f"{ALPHA_SLUG}/reference/doc-1", title="Alpha doc",
                                    project=ALPHA_SLUG, body="alpha doc body",
                                    last_reviewed="2026-01-01")
    library_fake.seed_reference_doc(id=f"{BETA_SLUG}/reference/doc-1", title="Beta doc",
                                    project=BETA_SLUG, body="beta doc body",
                                    last_reviewed="2026-01-01")
    # /reference/briefings reads only kind="brief" (the daily AI briefing)
    # or one of ops_reports_build._MEMORY_KIND_LABEL's kinds — neither doc
    # above qualifies (kind defaults to "reference").
    library_fake.seed_reference_doc(id=f"{ALPHA_SLUG}/briefs/2026-01-01", title="Alpha brief",
                                    kind="brief", project=ALPHA_SLUG, body="alpha brief body")
    library_fake.seed_reference_doc(id=f"{BETA_SLUG}/briefs/2026-01-01", title="Beta brief",
                                    kind="brief", project=BETA_SLUG, body="beta brief body")


@pytest.mark.parametrize("path", sorted(SCOPED_AND_TESTED))
def test_scoped_route_names_no_out_of_scope_project(client, path):
    _seed_two_projects()
    if path == "/crews/runs":
        path = f"/crews/runs?product={ALPHA_REPO}"
    r = client.get(path, headers={srv.EMAIL_HEADER: EMAIL})
    assert r.status_code == 200, (path, r.status_code, r.text[:500])
    body = r.text
    for marker in BETA_LEAK_MARKERS:
        assert marker not in body, f"{path} leaked {marker!r}"


def test_scoped_route_assertion_set_is_not_vacuous(client):
    """A leak-marker assertion that never fires because the page never had
    the marker's shape to begin with proves nothing — the positive control:
    confirm each marker DOES appear for a whole-fleet viewer, the same
    scope-and-render path, before trusting its absence above."""
    _seed_two_projects()
    whole_fleet_email = "admin@example.com"
    library_fake.seed_person_scope(whole_fleet_email, WHOLE_FLEET_SCOPE)
    for path in sorted(SCOPED_AND_TESTED):
        request_path = f"/crews/runs?product={BETA_REPO}" if path == "/crews/runs" else path
        r = client.get(request_path, headers={srv.EMAIL_HEADER: whole_fleet_email})
        assert r.status_code == 200, (path, r.status_code)
        found = [m for m in BETA_LEAK_MARKERS if m in r.text]
        assert found, (
            f"{path} shows none of {BETA_LEAK_MARKERS} even to a whole-fleet "
            f"viewer — the scoped assertion above would pass vacuously")


# --- the dynamic per-id pages: an out-of-scope id answers like an unknown one


def test_an_issue_detail_outside_scope_answers_like_an_unknown_id(client):
    _seed_two_projects()
    unknown = client.get("/engineering/issues/issue_does_not_exist",
                         headers={srv.EMAIL_HEADER: EMAIL})
    out_of_scope = client.get(f"/engineering/issues/{BETA_ITEM_ID}",
                              headers={srv.EMAIL_HEADER: EMAIL})
    assert unknown.status_code == out_of_scope.status_code == 404
    assert unknown.text == out_of_scope.text


def test_an_issue_detail_inside_scope_renders(client):
    _seed_two_projects()
    r = client.get(f"/engineering/issues/{ALPHA_ITEM_ID}", headers={srv.EMAIL_HEADER: EMAIL})
    assert r.status_code == 200
    assert "Alpha work" in r.text
    assert BETA_SLUG not in r.text


def test_a_signal_detail_outside_scope_answers_like_an_unknown_id(client):
    _seed_two_projects()
    unknown = client.get("/archives/sig_does_not_exist", headers={srv.EMAIL_HEADER: EMAIL})
    out_of_scope = client.get("/archives/sig_beta_001", headers={srv.EMAIL_HEADER: EMAIL})
    assert unknown.status_code == out_of_scope.status_code == 404
    assert unknown.text == out_of_scope.text


def test_a_signal_detail_inside_scope_renders(client):
    _seed_two_projects()
    r = client.get("/archives/sig_alpha_001", headers={srv.EMAIL_HEADER: EMAIL})
    assert r.status_code == 200
    assert "alpha signal body" in r.text


def test_a_reference_doc_outside_scope_answers_like_an_unknown_id(client):
    _seed_two_projects()
    import urllib.parse
    beta_id = urllib.parse.quote(f"{BETA_SLUG}/reference/doc-1", safe="")
    unknown = client.get("/reference/doc/does-not-exist", headers={srv.EMAIL_HEADER: EMAIL})
    out_of_scope = client.get(f"/reference/doc/{beta_id}", headers={srv.EMAIL_HEADER: EMAIL})
    assert unknown.status_code == out_of_scope.status_code == 404
    assert unknown.text == out_of_scope.text


def test_a_reference_doc_inside_scope_renders(client):
    _seed_two_projects()
    r = client.get(f"/reference/doc/{ALPHA_SLUG}/reference/doc-1",
                   headers={srv.EMAIL_HEADER: EMAIL})
    assert r.status_code == 200
    assert "alpha doc body" in r.text


def test_a_project_page_outside_scope_answers_like_an_unknown_name(client, monkeypatch):
    monkeypatch.setattr(project_build, "KNOWN_PROJECTS",
                        project_build.KNOWN_PROJECTS | {ALPHA_REPO, BETA_REPO})
    unknown = client.get("/projects/not-a-real-project", headers={srv.EMAIL_HEADER: EMAIL})
    out_of_scope = client.get(f"/projects/{BETA_REPO}", headers={srv.EMAIL_HEADER: EMAIL})
    assert unknown.status_code == out_of_scope.status_code == 404
    assert unknown.text == out_of_scope.text


def test_a_project_page_inside_scope_renders(client, monkeypatch):
    monkeypatch.setattr(project_build, "KNOWN_PROJECTS",
                        project_build.KNOWN_PROJECTS | {ALPHA_REPO, BETA_REPO})
    r = client.get(f"/projects/{ALPHA_REPO}", headers={srv.EMAIL_HEADER: EMAIL})
    assert r.status_code == 200
    assert BETA_REPO not in r.text


# Routes both tiers reach: Home renders a contractor body, and the Reading Room
# and the Writing Desk serve every member. Every other discovered route is
# full tier only.
_BOTH_TIERS = frozenset({"/", "/room", "/room/", "/writing-desk", "/writing-desk/"})


def test_every_discovered_route_404s_for_a_contractor(client):
    """The tier gate is Orient's audience wall (ARCH-228). The contractor-404
    test in tests/test_server.py walks `_full_only_routes()` and the scoped
    tuples, which E19·4 emptied of /crews, the Archives, /reference and the
    Roster when it gave each its own block. This walks the same discovery the
    scope check above uses, so a route in any block is covered, and a route
    added later is too."""
    _seed_two_projects()
    paths = sorted(_discovered_static_paths() - _REDIRECTS)
    assert set(_BOTH_TIERS) <= set(paths), "a both-tier route moved; update _BOTH_TIERS"
    reached = sorted(p for p in paths
                     if client.get(p, headers={srv.EMAIL_HEADER: "contractor@example.com"}).status_code != 404)
    assert reached == sorted(_BOTH_TIERS), (
        f"a contractor reached {sorted(set(reached) - _BOTH_TIERS)} — every route outside "
        f"_BOTH_TIERS is full tier only")
