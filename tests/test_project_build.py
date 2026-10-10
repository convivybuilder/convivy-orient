"""Tests for the per-project page (/projects/<name>, epic
issue_01M0AK8CM3ADKDTG8PM6GRCP8H, item 5).

The GitHub fetch is pure I/O (subprocess, via merge_queue_build._gh), so these
test the pure layer — merged-PR mapping/sorting, commit mapping, the bead
lookup — and the render (escaping, empty states, unknown-project None) against
monkeypatched fetches. No `gh` runs; the bead lookup goes through
`library_client` (issue_01M1MWSCCJP7THRV8YG1KDZ2YE), seeded via
`tests/library_fake.py` rather than a real KB index.
"""
from __future__ import annotations

import dataclasses

import pytest

import library_client
import library_fake
import merge_queue_build as mq
import project_build as p


# --- known projects ------------------------------------------------------


def test_known_projects_is_the_build_area_union():
    import build
    assert p.KNOWN_PROJECTS == (
        set(build.COMPANY_PROJECTS) | set(build.PRODUCT_ORDER)
        | set(build.ENGINEERING_PROJECTS) | set(build.STANDALONE_PRODUCTS))


# --- _open_prs (reuses merge_queue_build) ---------------------------------


def test_open_prs_reuses_merge_queue_classification(monkeypatch):
    pr = dict(number=1, title="t", url="https://github.com/convivy/vivy/pull/1",
              isDraft=False, createdAt="2026-07-17T00:00:00Z", updatedAt="2026-07-17T00:00:00Z",
              author={"login": "u"}, reviewDecision="REVIEW_REQUIRED", mergeable="MERGEABLE",
              statusCheckRollup=[{"conclusion": "SUCCESS"}], repo="vivy")
    monkeypatch.setattr(mq, "_open_prs_for_repo", lambda repo: [pr])
    rows = p._open_prs("vivy")
    assert len(rows) == 1
    assert rows[0]["cleared"] and rows[0]["group"] == "cleared"


# --- _merged_prs -----------------------------------------------------------


def test_merged_prs_maps_fields_and_sorts_recent_first(monkeypatch):
    def _gh(args):
        assert args[:2] == ["pr", "list"]
        return [
            {"number": 1, "title": "old", "url": "https://github.com/convivy/vivy/pull/1",
             "mergedAt": "2026-07-10T00:00:00Z", "author": {"login": "a"}},
            {"number": 2, "title": "new", "url": "https://github.com/convivy/vivy/pull/2",
             "mergedAt": "2026-07-17T00:00:00Z", "author": {"login": "b"}},
        ]
    monkeypatch.setattr(mq, "_gh", _gh)
    rows = p._merged_prs("vivy")
    assert [r["number"] for r in rows] == [2, 1]
    assert rows[0]["author"] == "b"


def test_merged_prs_empty_on_gh_failure(monkeypatch):
    monkeypatch.setattr(mq, "_gh", lambda args: None)
    assert p._merged_prs("vivy") == []


def test_merged_prs_defaults_missing_author_to_unknown(monkeypatch):
    monkeypatch.setattr(mq, "_gh", lambda args: [
        {"number": 1, "title": "t", "url": "u", "mergedAt": "2026-07-10T00:00:00Z", "author": None}])
    rows = p._merged_prs("vivy")
    assert rows[0]["author"] == "unknown"


# --- _recent_commits ---------------------------------------------------------


def test_recent_commits_maps_fields(monkeypatch):
    def _gh(args):
        assert args[0] == "api"
        return [{
            "sha": "abcdef0123456789",
            "commit": {"message": "Fix the thing\n\nlonger body here",
                       "author": {"name": "Ann", "date": "2026-07-17T00:00:00Z"}},
            "html_url": "https://github.com/convivy/vivy/commit/abcdef0",
        }]
    monkeypatch.setattr(mq, "_gh", _gh)
    rows = p._recent_commits("vivy")
    assert rows == [{
        "sha": "abcdef0", "message": "Fix the thing", "author": "Ann",
        "date": "2026-07-17T00:00:00Z", "url": "https://github.com/convivy/vivy/commit/abcdef0",
    }]


def test_recent_commits_empty_on_gh_failure(monkeypatch):
    monkeypatch.setattr(mq, "_gh", lambda args: None)
    assert p._recent_commits("vivy") == []


# --- _bead_doc ---------------------------------------------------------------


def _seed_bead(**kw):
    d = {"id": "vivy/bead/x", "project": "vivy", "kind": "bead",
         "title": "Where Vivy is going", "status": "current", "audience": ["team"]}
    d.update(kw)
    return library_fake.seed_reference_doc(**d)


def test_bead_doc_found(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_bead()
    bead = p._bead_doc("vivy")
    assert bead == {
        "id": "vivy/bead/x", "title": "Where Vivy is going",
        "url": "/reference/doc/vivy/bead/x",
    }


def test_bead_doc_picks_lowest_id_when_a_project_has_more_than_one(monkeypatch):
    """`ORDER BY id LIMIT 1` in the old direct query — reproduced client-side
    now that `list_reference_documents` returns the whole entitled set rather
    than a single row."""
    library_fake.patch(monkeypatch)
    _seed_bead(id="vivy/bead/z", title="Z")
    _seed_bead(id="vivy/bead/a", title="A")
    assert p._bead_doc("vivy") == {
        "id": "vivy/bead/a", "title": "A", "url": "/reference/doc/vivy/bead/a"}


def test_bead_doc_none_when_no_bead_for_project(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_bead(project="constance")
    assert p._bead_doc("vivy") is None


def test_bead_doc_ignores_non_bead_kind(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_bead(kind="decision")
    assert p._bead_doc("vivy") is None


def test_bead_doc_excludes_archived_status(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_bead(status="archived")
    assert p._bead_doc("vivy") is None


def test_bead_doc_filters_status_itself_not_just_via_the_route(monkeypatch):
    """The test above passes even with NO status filter in `_bead_doc`, because
    `library_fake` drops non-current/draft rows itself (`library_fake.py:400`)
    the way the Library's route does. So it pins the fake, not this module —
    a widened server default would leave it green while production changed
    (reviewer, PR #263).

    This one hands `_bead_doc` an archived row directly, bypassing both
    filters, so it fails if `_BEAD_STATUSES` is ever dropped from the
    comprehension. That matters beyond tidiness: `min(docs, key=id)` picks the
    LOWEST id, so a superseded bead sorting before the current one would win
    silently and the page would link the wrong document."""
    library_fake.patch(monkeypatch)
    current = library_client.ReferenceDocument(
        id="vivy/bead/vivy", source_ref="projects/vivy/bead/vivy.md", kind="bead",
        project="vivy", title="Vivy", status="current", topics=[],
        audience=["team"], last_reviewed=None, updated_at=None, body=None)
    # A lower id than the current bead, so it wins `min` if the filter is gone.
    archived = dataclasses.replace(current, id="vivy/bead/aa-old", status="archived",
                                    title="Retired bead")
    monkeypatch.setattr(library_client, "list_reference_documents",
                        lambda **kw: [archived, current])
    assert p._bead_doc("vivy")["id"] == "vivy/bead/vivy"


def test_bead_doc_none_when_not_team_audience(monkeypatch):
    """`/reference/doc/{id}` 404s for a document outside `team` audience
    (reference_doc_build.py's own gate) — a bead outside it must get no URL
    rather than one that 404s behind the link. Collapsed into the same
    `None` as "no bead on file" — `library_client._entitled` is what makes
    that call, and this function adds no second check."""
    library_fake.patch(monkeypatch)
    _seed_bead(audience=["agent"])
    assert p._bead_doc("vivy") is None


def test_bead_doc_none_when_audience_is_a_non_list_substring_match(monkeypatch):
    """A bare-string `audience` containing "team" as a substring (e.g. a
    malformed row that stored a scalar) must not pass `"team" in aud` as a
    substring test — `library_client._entitled`'s `isinstance(aud, list)`
    guard is what closes that (PR #205 review); this test exercises it
    through the real gate, not a reimplementation of it."""
    library_fake.patch(monkeypatch)
    _seed_bead(audience="steam-team")
    assert p._bead_doc("vivy") is None


def test_bead_doc_none_when_audience_is_empty(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_bead(audience=[])
    assert p._bead_doc("vivy") is None


def test_bead_doc_raises_on_a_library_failure(monkeypatch):
    """The Library being unreachable must not read as "no bead" — the
    three-way collapse `_bead_doc` used to make around a raw sqlite
    connection (issue_01M1MWSCCJP7THRV8YG1KDZ2YE). `_bead_doc` itself raises;
    `_fetch` is what turns this into a distinct rendered state (see the
    `_fetch`/`_bead_block` tests below)."""
    def _boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_reference_documents", _boom)
    with pytest.raises(RuntimeError):
        p._bead_doc("vivy")


# --- detail_html / render ----------------------------------------------------


def _stub_fetch(monkeypatch, *, open_prs=None, merged_prs=None, commits=None, bead=None,
                 bead_error=None, repo_exists=True):
    monkeypatch.setattr(p, "_fetch", lambda name, force=False: {
        "repo_exists": repo_exists, "open_prs": open_prs or [], "merged_prs": merged_prs or [],
        "commits": commits or [], "bead": bead, "bead_error": bead_error,
    })


def test_detail_html_none_for_unknown_project():
    assert p.detail_html("not-a-real-project") is None


def test_detail_html_renders_known_project(monkeypatch):
    _stub_fetch(monkeypatch)
    html = p.detail_html("vivy")
    assert html is not None
    assert "Vivy" in html


def test_empty_sections_render_empty_state(monkeypatch):
    _stub_fetch(monkeypatch)
    html = p.detail_html("vivy")
    assert "No open pull requests." in html
    assert "No recently merged pull requests." in html
    assert "No recent commits." in html
    assert "No bead on file for this project yet." in html


def test_project_page_renders_error_state_when_the_library_is_unreachable(monkeypatch):
    """A Library failure must render distinctly from "no bead on file" — the
    three-way collapse `_bead_doc` used to make around a raw sqlite
    connection (issue_01M1MWSCCJP7THRV8YG1KDZ2YE). The page must still
    render (no 500) and the bead section must show the error text, not the
    empty-state text."""
    _stub_fetch(monkeypatch, bead_error="The Library could not be reached "
                "(RuntimeError). Try again shortly.")
    html = p.detail_html("vivy")
    assert html is not None
    assert "The Library could not be reached (RuntimeError). Try again shortly." in html
    assert "No bead on file for this project yet." not in html


def test_fetch_distinguishes_unreachable_library_from_no_bead(monkeypatch):
    """`_fetch` catches `_bead_doc`'s raise and records it separately from
    `bead`, rather than letting it collapse back to `None`."""
    def _boom(name):
        raise RuntimeError("boom")

    monkeypatch.setattr(p, "_repo_exists", lambda name: False)
    monkeypatch.setattr(p, "_bead_doc", _boom)
    p._cache.clear()
    data = p._fetch("vivy")
    assert data["bead"] is None
    assert data["bead_error"] == "The Library could not be reached (RuntimeError). Try again shortly."


def test_bead_link_renders_when_present(monkeypatch):
    _stub_fetch(monkeypatch, bead={"id": "vivy/bead/x", "title": "Where Vivy is going",
                                   "url": "/reference/doc/vivy/bead/x"})
    html = p.detail_html("vivy")
    assert 'href="/reference/doc/vivy/bead/x"' in html
    assert "Where Vivy is going" in html
    # No Typora / OPEN_BASE anywhere (issue_01M17HYDBQ4GPWHPFS56M6QQPH)
    assert "localhost:8765" not in html
    assert "Typora" not in html


def test_merged_and_commit_rows_escape_content(monkeypatch):
    _stub_fetch(
        monkeypatch,
        merged_prs=[{"number": 1, "title": "<script>alert(1)</script>", "url": "https://github.com/convivy/vivy/pull/1",
                     "merged_at": "2026-07-17T00:00:00Z", "author": "<b>x</b>"}],
        commits=[{"sha": "abcdef0", "message": "<img onerror=alert(1)>",
                  "author": "<b>y</b>", "date": "2026-07-17T00:00:00Z",
                  "url": "https://github.com/convivy/vivy/commit/abcdef0"}],
    )
    html = p.detail_html("vivy")
    assert "<script>alert(1)</script>" not in html
    assert "<img onerror=alert(1)>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "&lt;b&gt;y&lt;/b&gt;" in html


def test_repo_exists_true_when_gh_returns_a_repo(monkeypatch):
    monkeypatch.setattr(mq, "_gh", lambda args: {"id": 1, "name": "vivy"})
    assert p._repo_exists("vivy") is True


def test_repo_exists_false_when_gh_fails(monkeypatch):
    # `gh api repos/convivy/<name>` exits non-zero on a 404 — mq._gh already
    # collapses that to None, same as any other gh failure.
    monkeypatch.setattr(mq, "_gh", lambda args: None)
    assert p._repo_exists("company") is False


def test_page_for_a_project_with_no_github_repo(monkeypatch):
    _stub_fetch(monkeypatch, repo_exists=False)
    html = p.detail_html("company")
    assert "No GitHub repo named convivy/company" in html
    # No dead link to a 404'ing github.com/convivy/company.
    assert 'href="https://github.com/convivy/company"' not in html
    # None of the three misleadingly-empty sections render for a non-repo project.
    assert "No open pull requests." not in html
    assert "No recently merged pull requests." not in html
    assert "No recent commits." not in html


def test_fetch_skips_gh_calls_when_repo_does_not_exist(monkeypatch):
    calls = []
    monkeypatch.setattr(p, "_repo_exists", lambda name: False)
    monkeypatch.setattr(p, "_open_prs", lambda name: calls.append(("open", name)))
    monkeypatch.setattr(p, "_merged_prs", lambda name, limit=8: calls.append(("merged", name)))
    monkeypatch.setattr(p, "_recent_commits", lambda name, limit=8: calls.append(("commits", name)))
    monkeypatch.setattr(p, "_bead_doc", lambda name: None)
    p._cache.clear()
    data = p._fetch("company")
    assert calls == []  # none of the three PR/commit fetchers ran
    assert data == {"repo_exists": False, "open_prs": [], "merged_prs": [], "commits": [],
                     "bead": None, "bead_error": None}


def test_open_prs_count_reflected_in_heading(monkeypatch):
    pr = mq._classify(dict(number=1, title="t", url="https://github.com/convivy/vivy/pull/1",
                            isDraft=False, createdAt="2026-07-17T00:00:00Z",
                            updatedAt="2026-07-17T00:00:00Z", author={"login": "u"},
                            reviewDecision="REVIEW_REQUIRED", mergeable="MERGEABLE",
                            statusCheckRollup=[{"conclusion": "SUCCESS"}], repo="vivy"))
    _stub_fetch(monkeypatch, open_prs=[pr])
    html = p.detail_html("vivy")
    assert "Open pull requests" in html
    assert "· 1" in html


def test_fetch_caches_per_project(monkeypatch):
    calls = []

    def _open_prs(name):
        calls.append(name)
        return []

    monkeypatch.setattr(p, "_repo_exists", lambda name: True)
    monkeypatch.setattr(p, "_open_prs", _open_prs)
    monkeypatch.setattr(p, "_merged_prs", lambda name, limit=8: [])
    monkeypatch.setattr(p, "_recent_commits", lambda name, limit=8: [])
    monkeypatch.setattr(p, "_bead_doc", lambda name: None)
    p._cache.clear()
    p._fetch("vivy")
    p._fetch("vivy")
    assert calls == ["vivy"]  # second call served from cache, not re-fetched


def test_fetch_force_bypasses_cache(monkeypatch):
    calls = []

    def _open_prs(name):
        calls.append(name)
        return []

    monkeypatch.setattr(p, "_repo_exists", lambda name: True)
    monkeypatch.setattr(p, "_open_prs", _open_prs)
    monkeypatch.setattr(p, "_merged_prs", lambda name, limit=8: [])
    monkeypatch.setattr(p, "_recent_commits", lambda name, limit=8: [])
    monkeypatch.setattr(p, "_bead_doc", lambda name: None)
    p._cache.clear()
    p._fetch("vivy")
    p._fetch("vivy", force=True)
    assert calls == ["vivy", "vivy"]


# --- Course position in the lede (item issue_01M2F4MZQ9NV94KGKPRDN2SY3Y) ------


def test_lede_shows_course_position_for_a_project_with_a_live_course(monkeypatch):
    library_fake.patch(monkeypatch)
    library_fake.seed_course(
        id="course_1", product="vivy", title="Ship it",
        entries=[
            {"issue_id": "i1", "state": "fixed"},
            {"issue_id": "i2", "state": "fixed"},
            {"issue_id": "i3", "state": "open"},
        ],
    )
    _stub_fetch(monkeypatch)
    html = p.detail_html("vivy")
    assert "2 of 3 waypoints passed" in html
    assert 'href="/chart-house"' in html
    assert "No progress measure yet" not in html


def test_lede_matches_a_course_whose_product_key_is_not_the_repo_name(monkeypatch):
    """"bosun" is the Course's product key for the agent-platform repo
    (`chart_house_build._REPO_ALIASES`) — the two vocabularies
    `_course_progress_for`'s own docstring names as not always equal."""
    library_fake.patch(monkeypatch)
    library_fake.seed_course(
        id="course_1", product="bosun", title="Make dispatch accountable",
        entries=[{"issue_id": "i1", "state": "fixed"}],
    )
    _stub_fetch(monkeypatch)
    html = p.detail_html("agent-platform")
    assert "1 of 1 waypoints passed" in html


def test_lede_says_no_live_course_plainly(monkeypatch):
    library_fake.patch(monkeypatch)
    _stub_fetch(monkeypatch)
    html = p.detail_html("vivy")
    assert "No live Course yet" in html
    assert 'href="/chart-house"' in html
    assert "No progress measure yet" not in html


def test_lede_shows_unavailable_on_a_library_failure_never_a_false_zero(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_courses", _boom)
    _stub_fetch(monkeypatch)
    html = p.detail_html("vivy")
    assert "Course progress unavailable" in html
    assert "0 of 0" not in html
    # the rest of the page still renders rather than 500ing
    assert "No bead on file for this project yet." in html
