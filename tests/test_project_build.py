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
import json

import pytest

import library_client
import library_fake
import merge_queue_build as mq
import project_build as p


# --- known projects ------------------------------------------------------


def test_known_projects_is_the_build_area_union():
    import build
    assert p.known_projects() == (
        set(build.COMPANY_PROJECTS) | set(build.PRODUCT_ORDER)
        | set(build.ENGINEERING_PROJECTS) | set(build.STANDALONE_PRODUCTS))


# --- _open_prs (reuses merge_queue_build) ---------------------------------


def test_open_prs_reuses_merge_queue_classification(monkeypatch):
    pr = dict(number=1, title="t", url="https://github.com/convivy/alpha/pull/1",
              isDraft=False, createdAt="2026-07-17T00:00:00Z", updatedAt="2026-07-17T00:00:00Z",
              author={"login": "u"}, reviewDecision="REVIEW_REQUIRED", mergeable="MERGEABLE",
              statusCheckRollup=[{"conclusion": "SUCCESS"}], repo="alpha")
    monkeypatch.setattr(mq, "_open_prs_for_repo", lambda repo: [pr])
    rows = p._open_prs("alpha")
    assert len(rows) == 1
    assert rows[0]["cleared"] and rows[0]["group"] == "cleared"


# --- _merged_prs -----------------------------------------------------------


def test_merged_prs_maps_fields_and_sorts_recent_first(monkeypatch):
    def _gh(args):
        assert args[:2] == ["pr", "list"]
        return [
            {"number": 1, "title": "old", "url": "https://github.com/convivy/alpha/pull/1",
             "mergedAt": "2026-07-10T00:00:00Z", "author": {"login": "a"}},
            {"number": 2, "title": "new", "url": "https://github.com/convivy/alpha/pull/2",
             "mergedAt": "2026-07-17T00:00:00Z", "author": {"login": "b"}},
        ]
    monkeypatch.setattr(mq, "_gh", _gh)
    rows = p._merged_prs("alpha")
    assert [r["number"] for r in rows] == [2, 1]
    assert rows[0]["author"] == "b"


def test_merged_prs_empty_on_gh_failure(monkeypatch):
    monkeypatch.setattr(mq, "_gh", lambda args: None)
    assert p._merged_prs("alpha") == []


def test_merged_prs_defaults_missing_author_to_unknown(monkeypatch):
    monkeypatch.setattr(mq, "_gh", lambda args: [
        {"number": 1, "title": "t", "url": "u", "mergedAt": "2026-07-10T00:00:00Z", "author": None}])
    rows = p._merged_prs("alpha")
    assert rows[0]["author"] == "unknown"


# --- _recent_commits ---------------------------------------------------------


def test_recent_commits_maps_fields(monkeypatch):
    def _gh(args):
        assert args[0] == "api"
        return [{
            "sha": "abcdef0123456789",
            "commit": {"message": "Fix the thing\n\nlonger body here",
                       "author": {"name": "Ann", "date": "2026-07-17T00:00:00Z"}},
            "html_url": "https://github.com/convivy/alpha/commit/abcdef0",
        }]
    monkeypatch.setattr(mq, "_gh", _gh)
    rows = p._recent_commits("alpha")
    assert rows == [{
        "sha": "abcdef0", "message": "Fix the thing", "author": "Ann",
        "date": "2026-07-17T00:00:00Z", "url": "https://github.com/convivy/alpha/commit/abcdef0",
    }]


def test_recent_commits_empty_on_gh_failure(monkeypatch):
    monkeypatch.setattr(mq, "_gh", lambda args: None)
    assert p._recent_commits("alpha") == []


# --- _bead_doc ---------------------------------------------------------------


def _seed_bead(**kw):
    d = {"id": "alpha/bead/x", "project": "alpha", "kind": "bead",
         "title": "Where Alpha is going", "status": "current", "audience": ["team"]}
    d.update(kw)
    return library_fake.seed_reference_doc(**d)


def test_bead_doc_found(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_bead()
    bead = p._bead_doc("alpha")
    assert bead == {
        "id": "alpha/bead/x", "title": "Where Alpha is going",
        "url": "/reference/doc/alpha/bead/x",
    }


def test_bead_doc_picks_lowest_id_when_a_project_has_more_than_one(monkeypatch):
    """`ORDER BY id LIMIT 1` in the old direct query — reproduced client-side
    now that `list_reference_documents` returns the whole entitled set rather
    than a single row."""
    library_fake.patch(monkeypatch)
    _seed_bead(id="alpha/bead/z", title="Z")
    _seed_bead(id="alpha/bead/a", title="A")
    assert p._bead_doc("alpha") == {
        "id": "alpha/bead/a", "title": "A", "url": "/reference/doc/alpha/bead/a"}


def test_bead_doc_none_when_no_bead_for_project(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_bead(project="beta")
    assert p._bead_doc("alpha") is None


def test_bead_doc_ignores_non_bead_kind(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_bead(kind="decision")
    assert p._bead_doc("alpha") is None


def test_bead_doc_excludes_archived_status(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_bead(status="archived")
    assert p._bead_doc("alpha") is None


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
        id="alpha/bead/alpha", source_ref="projects/alpha/bead/alpha.md", kind="bead",
        project="alpha", title="Alpha", status="current", topics=[],
        audience=["team"], last_reviewed=None, updated_at=None, body=None)
    # A lower id than the current bead, so it wins `min` if the filter is gone.
    archived = dataclasses.replace(current, id="alpha/bead/aa-old", status="archived",
                                    title="Retired bead")
    monkeypatch.setattr(library_client, "list_reference_documents",
                        lambda **kw: [archived, current])
    assert p._bead_doc("alpha")["id"] == "alpha/bead/alpha"


def test_bead_doc_none_when_not_team_audience(monkeypatch):
    """`/reference/doc/{id}` 404s for a document outside `team` audience
    (reference_doc_build.py's own gate) — a bead outside it must get no URL
    rather than one that 404s behind the link. Collapsed into the same
    `None` as "no bead on file" — `library_client._entitled` is what makes
    that call, and this function adds no second check."""
    library_fake.patch(monkeypatch)
    _seed_bead(audience=["agent"])
    assert p._bead_doc("alpha") is None


def test_bead_doc_none_when_audience_is_a_non_list_substring_match(monkeypatch):
    """A bare-string `audience` containing "team" as a substring (e.g. a
    malformed row that stored a scalar) must not pass `"team" in aud` as a
    substring test — `library_client._entitled`'s `isinstance(aud, list)`
    guard is what closes that (PR #205 review); this test exercises it
    through the real gate, not a reimplementation of it."""
    library_fake.patch(monkeypatch)
    _seed_bead(audience="steam-team")
    assert p._bead_doc("alpha") is None


def test_bead_doc_none_when_audience_is_empty(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_bead(audience=[])
    assert p._bead_doc("alpha") is None


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
        p._bead_doc("alpha")


# --- _knowledge_history (CQ·165, issue_01M57NS8PJRTWJ9H6ZHHYB3BFN) ----------


def _kb_row(**kw):
    defaults = dict(commit="abc1234", committed_at=None, doc_id="alpha/kind/doc-1",
                    title="A doc", status="modified", subject="Update the thing",
                    source_ref=None)
    defaults.update(kw)
    return library_client.KnowledgeHistoryRow(**defaults)


def test_knowledge_history_calls_the_library_client_with_the_limit(monkeypatch):
    seen = {}
    monkeypatch.setattr(library_client, "list_knowledge_history",
                        lambda project, *, limit: (seen.update(project=project, limit=limit) or []))
    monkeypatch.setattr(p, "_kb_entitled_ids", lambda: set())
    p._knowledge_history("alpha")
    assert seen == {"project": "alpha", "limit": p._KB_HISTORY_LIMIT}


def test_knowledge_history_raises_on_a_library_failure(monkeypatch):
    """Matches `_bead_doc`'s own discipline: an outage must not read as "no
    knowledge history" and that is `_fetch`'s job to catch, not this
    function's."""
    def _boom(project, *, limit):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_knowledge_history", _boom)
    with pytest.raises(RuntimeError):
        p._knowledge_history("alpha")


def test_fetch_distinguishes_unreachable_knowledge_history_from_empty(monkeypatch):
    def _boom(name):
        raise RuntimeError("boom")

    monkeypatch.setattr(p, "_repo_exists", lambda name: False)
    monkeypatch.setattr(p, "_bead_doc", lambda name: None)
    monkeypatch.setattr(p, "_knowledge_history", _boom)
    p._cache.clear()
    data = p._fetch("alpha")
    assert data["kb_history"] == []
    assert data["kb_history_error"] == "The Library could not be reached (RuntimeError). Try again shortly."


# --- _kb_history_block / render (CQ·165) -------------------------------------


def test_kb_history_block_groups_several_documents_in_one_commit():
    rows = [
        _kb_row(commit="c1", doc_id="alpha/kind/a", title="A", subject="Big commit"),
        _kb_row(commit="c1", doc_id="alpha/kind/b", title="B", subject="Big commit"),
    ]
    html = p._kb_history_block(rows, None)
    assert "c1" in html
    assert 'href="/reference/doc/alpha/kind/a"' in html
    assert 'href="/reference/doc/alpha/kind/b"' in html
    assert ">A<" in html and ">B<" in html
    assert "Big commit" in html


def test_kb_history_block_null_doc_id_has_no_link_and_is_not_called_deleted():
    row = _kb_row(doc_id=None, title=None,
                  source_ref="projects/alpha/profiles/daytime.yaml", status="modified")
    html = p._kb_history_block([row], None)
    assert "projects/alpha/profiles/daytime.yaml" in html
    assert "<a " not in html
    assert "deleted" not in html.lower()
    assert "modified" in html.lower()


def test_kb_history_block_null_doc_id_labelled_deleted_when_status_says_so():
    row = _kb_row(doc_id=None, title=None,
                  source_ref="projects/alpha/old/retired.md", status="deleted")
    html = p._kb_history_block([row], None)
    assert "deleted" in html.lower()


def test_kb_history_block_empty_renders_no_knowledge_history():
    html = p._kb_history_block([], None)
    assert "No knowledge history for this project." in html


def test_kb_history_block_error_renders_unavailable_not_empty():
    html = p._kb_history_block([], "The Library could not be reached (RuntimeError). Try again shortly.")
    assert "The Library could not be reached (RuntimeError). Try again shortly." in html
    assert "No knowledge history for this project." not in html


def test_detail_html_renders_knowledge_changes_section(monkeypatch):
    _stub_fetch(monkeypatch, kb_history=[_kb_row()])
    html = p.detail_html("alpha", frozenset(), True)
    assert "Knowledge changes" in html
    assert 'href="/reference/doc/alpha/kind/doc-1"' in html


def test_detail_html_renders_a_400_as_no_knowledge_history(monkeypatch):
    """A 400 from the route is already collapsed to `[]` by
    `library_client.list_knowledge_history` — this is what the page renders
    for that empty list, never an error."""
    _stub_fetch(monkeypatch, kb_history=[], kb_history_error=None)
    html = p.detail_html("alpha", frozenset(), True)
    assert "No knowledge history for this project." in html


def test_detail_html_renders_a_503_as_unavailable(monkeypatch):
    _stub_fetch(monkeypatch, kb_history=[],
               kb_history_error="The Library could not be reached (HTTPError). Try again shortly.")
    html = p.detail_html("alpha", frozenset(), True)
    assert "The Library could not be reached (HTTPError). Try again shortly." in html
    assert "No knowledge history for this project." not in html
    # the rest of the page still renders rather than 500ing
    assert "No bead on file for this project yet." in html


# --- knowledge-history audience filter (CQ·165 revise, security review on
# PR #387, comment 6253822132) -----------------------------------------------


def test_knowledge_history_filters_out_a_non_team_document(monkeypatch):
    """A row for a document outside `_AUDIENCE` must not survive
    `_knowledge_history` — `/reference/doc/<id>` 404s for this document
    (reference_doc_build.py's own gate), so a row naming it here would link
    to a 404 and leak its title to a reader `_bead_doc` already withholds the
    same document from."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(
        id="convivy-orient/agents/orient-coder", title="Orient Coder",
        project="convivy-orient", kind="agent", audience=["orient-coder"])
    monkeypatch.setattr(
        library_client, "list_knowledge_history",
        lambda project, *, limit: [_kb_row(
            doc_id="convivy-orient/agents/orient-coder", title="Orient Coder",
            subject="Add the orient-coder agent doc")])
    assert p._knowledge_history("convivy-orient") == []


def test_knowledge_history_keeps_a_row_with_no_index_entry(monkeypatch):
    """A null `doc_id` row has no title or link to leak (it renders from
    `source_ref`/`status` alone, `_kb_doc_line`) — the audience filter must
    not touch it."""
    library_fake.patch(monkeypatch)
    monkeypatch.setattr(
        library_client, "list_knowledge_history",
        lambda project, *, limit: [_kb_row(
            doc_id=None, title=None, source_ref="projects/alpha/profiles/daytime.yaml")])
    rows = p._knowledge_history("alpha")
    assert len(rows) == 1 and rows[0].doc_id is None


def test_detail_html_omits_a_non_team_documents_title_and_commit(monkeypatch):
    """The real case the review named: `convivy-orient/agents/orient-coder`
    carries `audience: ["orient-coder"]`, so its title must not ride along
    on this page, and the commit it was the only row of must disappear
    along with it — a commit heading with no doc lines under it would be as
    misleading as the title itself."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(
        id="convivy-orient/agents/orient-coder", title="Orient Coder",
        project="convivy-orient", kind="agent", audience=["orient-coder"])
    monkeypatch.setattr(
        library_client, "list_knowledge_history",
        lambda project, *, limit: [_kb_row(
            commit="c1", doc_id="convivy-orient/agents/orient-coder", title="Orient Coder",
            subject="Add the orient-coder agent doc")])
    monkeypatch.setattr(p, "_repo_exists", lambda name: False)
    monkeypatch.setattr(p, "_bead_doc", lambda name: None)
    p._cache.clear()
    html = p.detail_html("convivy-orient", frozenset(), True)
    assert "Orient Coder" not in html
    assert "Add the orient-coder agent doc" not in html


def test_detail_html_keeps_an_entitled_row_in_a_mixed_commit(monkeypatch):
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(
        id="convivy-orient/agents/orient-coder", title="Orient Coder",
        project="convivy-orient", kind="agent", audience=["orient-coder"])
    library_fake.seed_reference_doc(
        id="convivy-orient/bead/x", title="Where Orient is going", project="convivy-orient")
    monkeypatch.setattr(
        library_client, "list_knowledge_history",
        lambda project, *, limit: [
            _kb_row(commit="c1", doc_id="convivy-orient/agents/orient-coder",
                   title="Orient Coder", subject="Mixed commit"),
            _kb_row(commit="c1", doc_id="convivy-orient/bead/x",
                   title="Where Orient is going", subject="Mixed commit"),
        ])
    monkeypatch.setattr(p, "_repo_exists", lambda name: False)
    monkeypatch.setattr(p, "_bead_doc", lambda name: None)
    p._cache.clear()
    html = p.detail_html("convivy-orient", frozenset(), True)
    assert "Orient Coder" not in html
    assert "Where Orient is going" in html
    # the commit's subject survives because an entitled row still backs it
    assert "Mixed commit" in html


def test_fetch_renders_unavailable_when_the_entitlement_read_fails(monkeypatch):
    """An entitlement-read failure must raise into the same "unavailable"
    state the history read's own failure does, never fall back to an
    unfiltered list — the one outcome the filter must never produce."""
    def _boom():
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(p, "_repo_exists", lambda name: False)
    monkeypatch.setattr(p, "_bead_doc", lambda name: None)
    monkeypatch.setattr(library_client, "list_knowledge_history",
                        lambda project, *, limit: [_kb_row()])
    monkeypatch.setattr(p, "_kb_entitled_ids", _boom)
    p._cache.clear()
    data = p._fetch("alpha")
    assert data["kb_history"] == []
    assert data["kb_history_error"] == "The Library could not be reached (RuntimeError). Try again shortly."


# --- detail_html / render ----------------------------------------------------


def _stub_fetch(monkeypatch, *, open_prs=None, merged_prs=None, commits=None, bead=None,
                 bead_error=None, repo_exists=True, kb_history=None, kb_history_error=None):
    monkeypatch.setattr(p, "_fetch", lambda name, force=False: {
        "repo_exists": repo_exists, "open_prs": open_prs or [], "merged_prs": merged_prs or [],
        "commits": commits or [], "bead": bead, "bead_error": bead_error,
        "kb_history": kb_history or [], "kb_history_error": kb_history_error,
    })


def test_detail_html_none_for_unknown_project():
    assert p.detail_html("not-a-real-project", frozenset(), True) is None


def test_detail_html_renders_known_project(monkeypatch):
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset(), True)
    assert html is not None
    assert "Alpha" in html


# --- scope (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) -------------------------


def test_detail_html_none_for_a_known_project_outside_the_viewers_scope(tmp_path, monkeypatch):
    """A project name is a short, guessable repo name, so this page's own
    check, not just the caller's, must refuse it — a scoped viewer not
    entitled to "alpha" gets the same 404 an unknown name gets."""
    _stub_fetch(monkeypatch)
    registry = tmp_path / "projects.json"
    # Carries a `_products` entry too (CQ-171 ruling 1 made PRODUCT_ORDER
    # registry-derived) so "alpha" is in known_projects() and this test keeps
    # isolating the SCOPE rejection it names, rather than passing because the
    # name is unknown for an unrelated reason.
    registry.write_text(
        '{"alpha": {"crew": "alpha", "lifecycle": "active", "repos": ["alpha"]},'
        ' "_products": {"alpha": {"display": "Alpha", "owner": "convivy"}}}')
    monkeypatch.setenv("PROJECTS_JSON", str(registry))
    assert p.detail_html("alpha", frozenset({"other"}), False) is None


def test_detail_html_renders_for_a_viewer_whose_scope_holds_the_repo(tmp_path, monkeypatch):
    _stub_fetch(monkeypatch)
    registry = tmp_path / "projects.json"
    registry.write_text(
        '{"alpha": {"crew": "alpha", "lifecycle": "active", "repos": ["alpha"], "display": "Alpha"},'
        ' "_products": {"alpha": {"display": "Alpha", "owner": "convivy"}}}')
    monkeypatch.setenv("PROJECTS_JSON", str(registry))
    html = p.detail_html("alpha", frozenset({"alpha"}), False)
    assert html is not None
    assert "Alpha" in html


def test_empty_sections_render_empty_state(monkeypatch):
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset(), True)
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
    html = p.detail_html("alpha", frozenset(), True)
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
    data = p._fetch("alpha")
    assert data["bead"] is None
    assert data["bead_error"] == "The Library could not be reached (RuntimeError). Try again shortly."


def test_bead_link_renders_when_present(monkeypatch):
    _stub_fetch(monkeypatch, bead={"id": "alpha/bead/x", "title": "Where Alpha is going",
                                   "url": "/reference/doc/alpha/bead/x"})
    html = p.detail_html("alpha", frozenset(), True)
    assert 'href="/reference/doc/alpha/bead/x"' in html
    assert "Where Alpha is going" in html
    # No Typora / OPEN_BASE anywhere (issue_01M17HYDBQ4GPWHPFS56M6QQPH)
    assert "localhost:8765" not in html
    assert "Typora" not in html


def test_merged_and_commit_rows_escape_content(monkeypatch):
    _stub_fetch(
        monkeypatch,
        merged_prs=[{"number": 1, "title": "<script>alert(1)</script>", "url": "https://github.com/convivy/alpha/pull/1",
                     "merged_at": "2026-07-17T00:00:00Z", "author": "<b>x</b>"}],
        commits=[{"sha": "abcdef0", "message": "<img onerror=alert(1)>",
                  "author": "<b>y</b>", "date": "2026-07-17T00:00:00Z",
                  "url": "https://github.com/convivy/alpha/commit/abcdef0"}],
    )
    html = p.detail_html("alpha", frozenset(), True)
    assert "<script>alert(1)</script>" not in html
    assert "<img onerror=alert(1)>" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "&lt;b&gt;y&lt;/b&gt;" in html


def test_repo_exists_true_when_gh_returns_a_repo(monkeypatch):
    monkeypatch.setattr(mq, "_gh", lambda args: {"id": 1, "name": "alpha"})
    assert p._repo_exists("alpha") is True


def test_repo_exists_false_when_gh_fails(monkeypatch):
    # `gh api repos/convivy/<name>` exits non-zero on a 404 — mq._gh already
    # collapses that to None, same as any other gh failure.
    monkeypatch.setattr(mq, "_gh", lambda args: None)
    assert p._repo_exists("company") is False


def test_repo_exists_queries_the_registrys_org(monkeypatch):
    seen = {}

    def _gh(args):
        seen["path"] = args[-1]
        return {}

    monkeypatch.setattr(mq, "_gh", _gh)
    p._repo_exists("alpha")
    assert seen["path"] == "repos/convivy/alpha"


def test_repo_exists_queries_the_repos_own_unit_github_owner(tmp_path, monkeypatch):
    """No Convivy name: the org queried comes from `alpha`'s own unit's
    `github_owner`, not from `_products`' business-ownership `owner` field
    (which is never read for this)."""
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({
        "alpha-co": {"repos": ["alpha"], "github_owner": "acme"},
        "_products": {"widget": {"display": "Widget", "owner": "omega"}},
    }))
    monkeypatch.setenv("PROJECTS_JSON", str(path))
    seen = {}

    def _gh(args):
        seen["path"] = args[-1]
        return {}

    monkeypatch.setattr(mq, "_gh", _gh)
    p._repo_exists("alpha")
    assert seen["path"] == "repos/acme/alpha"


def test_repo_exists_falls_back_to_convivy_for_an_unregistered_repo(tmp_path, monkeypatch):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"alpha-co": {"repos": ["alpha"], "github_owner": "acme"}}))
    monkeypatch.setenv("PROJECTS_JSON", str(path))
    seen = {}

    def _gh(args):
        seen["path"] = args[-1]
        return {}

    monkeypatch.setattr(mq, "_gh", _gh)
    p._repo_exists("unregistered")
    assert seen["path"] == "repos/convivy/unregistered"


def test_page_for_a_project_with_no_github_repo(monkeypatch):
    _stub_fetch(monkeypatch, repo_exists=False)
    html = p.detail_html("company", frozenset(), True)
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
    monkeypatch.setattr(p, "_knowledge_history", lambda name: [])
    p._cache.clear()
    data = p._fetch("company")
    assert calls == []  # none of the three PR/commit fetchers ran
    assert data == {"repo_exists": False, "open_prs": [], "merged_prs": [], "commits": [],
                     "bead": None, "bead_error": None, "kb_history": [], "kb_history_error": None}


def test_open_prs_count_reflected_in_heading(monkeypatch):
    pr = mq._classify(dict(number=1, title="t", url="https://github.com/convivy/alpha/pull/1",
                            isDraft=False, createdAt="2026-07-17T00:00:00Z",
                            updatedAt="2026-07-17T00:00:00Z", author={"login": "u"},
                            reviewDecision="REVIEW_REQUIRED", mergeable="MERGEABLE",
                            statusCheckRollup=[{"conclusion": "SUCCESS"}], repo="alpha"))
    _stub_fetch(monkeypatch, open_prs=[pr])
    html = p.detail_html("alpha", frozenset(), True)
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
    monkeypatch.setattr(p, "_knowledge_history", lambda name: [])
    p._cache.clear()
    p._fetch("alpha")
    p._fetch("alpha")
    assert calls == ["alpha"]  # second call served from cache, not re-fetched


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
    monkeypatch.setattr(p, "_knowledge_history", lambda name: [])
    p._cache.clear()
    p._fetch("alpha")
    p._fetch("alpha", force=True)
    assert calls == ["alpha", "alpha"]


# --- Course position in the lede (item issue_01M2F4MZQ9NV94KGKPRDN2SY3Y) ------


def test_lede_shows_course_position_for_a_project_with_a_live_course(monkeypatch):
    library_fake.patch(monkeypatch)
    library_fake.seed_course(
        id="course_1", product="alpha", title="Ship it",
        entries=[
            {"issue_id": "i1", "state": "fixed"},
            {"issue_id": "i2", "state": "fixed"},
            {"issue_id": "i3", "state": "open"},
        ],
    )
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset(), True)
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
    html = p.detail_html("agent-platform", frozenset(), True)
    assert "1 of 1 waypoints passed" in html


def test_lede_says_no_live_course_plainly(monkeypatch):
    library_fake.patch(monkeypatch)
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset(), True)
    assert "No live Course yet" in html
    assert 'href="/chart-house"' in html
    assert "No progress measure yet" not in html


def test_lede_shows_unavailable_on_a_library_failure_never_a_false_zero(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_courses", _boom)
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset(), True)
    assert "Course progress unavailable" in html
    assert "0 of 0" not in html
    # the rest of the page still renders rather than 500ing
    assert "No bead on file for this project yet." in html


# --- Course section (CQ·161, issue_01M57NRS6D4HZ2WNCHC2G6QPE1) ---------------


def test_course_section_renders_entries_in_plan_order(monkeypatch):
    """The live Course's entries render inline, each linked to its issue
    page and marked by state — the same shape /engineering/course renders,
    so the two pages agree on what a step looks like."""
    library_fake.patch(monkeypatch)
    library_fake.seed_course(
        id="course_1", product="alpha", title="Ship it",
        entries=[
            {"issue_id": "i1", "state": "fixed", "title": "Foundation"},
            {"issue_id": "i2", "state": "open", "title": "Walls", "maturity": "approved"},
            {"issue_id": "i3", "state": "open", "title": "Roof", "maturity": "proposed"},
        ],
    )
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset(), True)
    assert "Course" in html
    assert "Foundation" in html
    assert "Walls" in html
    assert "Roof" in html
    # step classes match the state-then-maturity priority
    assert "step-done" in html
    assert "step-approved" in html
    assert "step-proposed" in html
    # each step links to its issue page
    assert 'href="/engineering/issues/i1"' in html
    assert 'href="/engineering/issues/i2"' in html
    assert 'href="/engineering/issues/i3"' in html
    # links out to the full Course page
    assert 'href="/engineering/course#course-alpha"' in html


def test_course_section_shows_no_course_plainly(monkeypatch):
    library_fake.patch(monkeypatch)
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset(), True)
    assert "No live Course for this project yet." in html


def test_course_section_renders_unavailable_on_a_library_failure(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_courses", _boom)
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset(), True)
    assert "Course unavailable" in html
    assert "No live Course" not in html


def test_course_section_matches_a_course_whose_product_key_is_not_the_repo(monkeypatch):
    """bosun's Course is filed under product key 'bosun' but the page is
    /projects/agent-platform — the same alias split the lede's position
    clause already resolves."""
    library_fake.patch(monkeypatch)
    library_fake.seed_course(
        id="course_1", product="bosun", title="Dispatch",
        entries=[{"issue_id": "i1", "state": "fixed", "title": "Step one"}],
    )
    _stub_fetch(monkeypatch)
    html = p.detail_html("agent-platform", frozenset(), True)
    assert "Step one" in html
    assert "step-done" in html


def test_course_section_renders_empty_entries_message(monkeypatch):
    library_fake.patch(monkeypatch)
    library_fake.seed_course(
        id="course_1", product="alpha", title="Ship it", entries=[])
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset(), True)
    assert "This Course has no entries yet." in html


# --- Epics section (CQ·161) --------------------------------------------------


def _seed_epic(**kw):
    defaults = dict(id="issue_epic_1", product="alpha", title="Build the thing",
                    type="epic", state="open", maturity="approved")
    defaults.update(kw)
    return library_fake.seed_issue(**defaults)


def test_epics_section_renders_open_epics_with_maturity(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_epic(id="issue_epic_1", title="First epic", maturity="approved")
    _seed_epic(id="issue_epic_2", title="Second epic", maturity="proposed")
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "Epics" in html
    assert "First epic" in html
    assert "Second epic" in html
    assert "maturity-approved" in html
    assert "maturity-proposed" in html
    # each epic links to its issue page
    assert 'href="/engineering/issues/issue_epic_1"' in html
    assert 'href="/engineering/issues/issue_epic_2"' in html


def test_epics_section_renders_unset_maturity_badge(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_epic(id="issue_epic_1", title="No maturity", maturity=None)
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "maturity-unset" in html


def test_epics_section_shows_recently_resolved(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_epic(id="issue_epic_open", title="Open epic", state="open")
    _seed_epic(id="issue_epic_done", title="Done epic", state="fixed",
               resolution="fixed", resolved_at=__import__("datetime").datetime(2026, 7, 1))
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "Recently resolved" in html
    assert "Open epic" in html
    assert "Done epic" in html


def test_epics_section_shows_no_epics_empty_state(monkeypatch):
    library_fake.patch(monkeypatch)
    # seed a non-epic item, which must not appear in the epics section
    library_fake.seed_issue(id="issue_bug_1", product="alpha", title="A bug",
                            type="bug", state="open")
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "No epics for this project." in html


def test_epics_section_renders_unavailable_on_a_library_failure(monkeypatch):
    def _boom():
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_issue_items", _boom)
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "Epics unavailable" in html
    assert "No epics for this project." not in html


def test_epics_section_shows_child_count(monkeypatch):
    """An epic's active work items are counted from the same items list,
    the way issue_build groups by parent_id."""
    library_fake.patch(monkeypatch)
    _seed_epic(id="issue_epic_1", title="Big epic")
    library_fake.seed_issue(id="issue_child_1", product="alpha", title="Child one",
                            type="bug", state="open", parent_id="issue_epic_1")
    library_fake.seed_issue(id="issue_child_2", product="alpha", title="Child two",
                            type="bug", state="open", parent_id="issue_epic_1")
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "Big epic" in html
    assert "2 work items" in html


def test_epics_section_filters_to_the_projects_scope(tmp_path, monkeypatch):
    """A scoped viewer sees only their project's epics — an epic in a
    project outside scope must not appear."""
    library_fake.patch(monkeypatch)
    _seed_epic(id="issue_epic_alpha", product="alpha", title="Alpha epic")
    _seed_epic(id="issue_epic_other", product="beta", title="Beta epic")
    _stub_fetch(monkeypatch)
    registry = tmp_path / "projects.json"
    # Carries a `_products` entry (CQ-171 ruling 1) so "alpha" is in
    # known_projects() and this test keeps isolating the SCOPE filter it
    # names, rather than detail_html returning None for an unrelated reason.
    registry.write_text(
        '{"alpha": {"crew": "alpha", "lifecycle": "active", "repos": ["alpha"]},'
        ' "_products": {"alpha": {"display": "Alpha", "owner": "convivy"}}}')
    monkeypatch.setenv("PROJECTS_JSON", str(registry))
    html = p.detail_html("alpha", frozenset({"alpha"}), False)
    assert "Alpha epic" in html
    assert "Beta epic" not in html


def test_epics_section_only_lists_this_projects_epics(monkeypatch):
    """Only the project's own epics render — another project's epic in
    scope must not appear on this project's page."""
    library_fake.patch(monkeypatch)
    _seed_epic(id="issue_epic_alpha", product="alpha", title="Alpha epic")
    _seed_epic(id="issue_epic_beta", product="beta", title="Beta epic")
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "Alpha epic" in html
    assert "Beta epic" not in html


# --- Course section: get_course fallback (CQ·161 revise round 3) ------------


def test_course_section_falls_back_to_the_list_record_when_get_course_raises(monkeypatch):
    """`_course_for` degrades a single course's `get_course` detail call to
    its `list_courses` record rather than taking the section down — the same
    discipline `chart_house_build._course_rows` applies
    (`test_one_courses_detail_failing_degrades_that_row_and_not_the_others`).
    The bare record carries entries with whatever state/maturity `list_courses`
    serves, so the section renders them rather than "Course unavailable" or
    a 500."""
    library_fake.patch(monkeypatch)
    library_fake.seed_course(
        id="course_1", product="alpha", title="Ship it",
        entries=[
            {"issue_id": "i1", "state": "fixed", "title": "Foundation"},
            {"issue_id": "i2", "state": "open", "title": "Walls", "maturity": "approved"},
        ],
    )

    def _boom(cid):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "get_course", _boom)
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset(), True)
    assert "Foundation" in html
    assert "Walls" in html
    assert "step-done" in html
    assert "step-approved" in html
    assert "Course unavailable" not in html
    # the rest of the page still renders rather than 500ing
    assert "No bead on file for this project yet." in html


# --- Epics section: truncation note (CQ·161 revise round 3) -----------------


def test_epics_section_shows_more_not_rendered_when_open_epics_truncate(monkeypatch):
    """A state's list past `_EPIC_LIMIT` carries an "N more not rendered"
    note, the same shape issue_build's Epics page renders when its cap
    drops entries, so a reader sees the true count rather than the
    rendered one."""
    library_fake.patch(monkeypatch)
    for i in range(p._EPIC_LIMIT + 3):
        _seed_epic(id=f"issue_epic_{i}", title=f"Open epic {i}", state="open")
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "3 more not rendered." in html


def test_epics_section_shows_no_truncation_note_when_under_the_cap(monkeypatch):
    library_fake.patch(monkeypatch)
    for i in range(p._EPIC_LIMIT):
        _seed_epic(id=f"issue_epic_{i}", title=f"Open epic {i}", state="open")
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "more not rendered" not in html


# --- Epics section: project-to-repo mapping (CQ·161 revise round 3) ---------


def test_epics_section_matches_epics_through_the_repo_alias(monkeypatch):
    """An epic's `product` is a project slug, not always the same string as
    the page's GitHub repo name — "bosun" is the Bosun's product key but the
    page is `/projects/agent-platform`. The match goes through
    `chart_house_build._repo_names_for`, the same mapping the Course section
    uses, so the page shows this project's epics rather than none at all."""
    library_fake.patch(monkeypatch)
    _seed_epic(id="issue_epic_bosun", product="bosun", title="Bosun epic")
    _seed_epic(id="issue_epic_alpha", product="alpha", title="Alpha epic")
    _stub_fetch(monkeypatch)
    html = p.detail_html("agent-platform", frozenset({"*"}), True)
    assert "Bosun epic" in html
    assert "Alpha epic" not in html


# --- /projects index static fallback (CQ·160 revise round 2) --------------


def test_the_projects_static_fallback_names_no_project(tmp_path, monkeypatch):
    """Revise round 2 on PR #395: `project_build.build_static()` writes the
    `/projects` index's static fallback, served by server.py to whichever
    viewer's live render failed. Rendered for an empty scope, so it must name
    no project — a scoped viewer served this fallback during a Library stall
    must not see another project's name. Same discipline as
    `chart_house_build.build_static`'s own fallback test."""
    # Several known projects in the registry, so a non-empty scope would name
    # them. build_static() renders for an empty scope, so none appear.
    registry = tmp_path / "projects.json"
    registry.write_text(
        '{"alpha": {"crew": "alpha", "lifecycle": "active", "repos": ["alpha"]},'
        ' "beta": {"crew": "beta", "lifecycle": "active", "repos": ["beta"]}}')
    monkeypatch.setenv("PROJECTS_JSON", str(registry))
    monkeypatch.setattr(p.paths, "STATE_ROOT", tmp_path)
    p.build_static()
    written = (tmp_path / "site" / "projects.html").read_text(encoding="utf-8")
    for name in ("alpha", "beta"):
        assert f'href="/projects/{name}"' not in written
        assert f'<span class="proj-index-slug">{name}</span>' not in written
    assert "Live view unavailable" in written


# --- Changelog and release-notes sections (CQ·162, issue_01M57NRWGBNNXNP72GF1EXY3B5) ---


def _seed_changelog(**kw):
    defaults = dict(id="sig_cl_1", source="co", kind="changelog", actor="alpha-co",
                     ingested_by="agent-platform", product="alpha", stream="alpha",
                     body="Shipped the thing.")
    defaults.update(kw)
    return library_fake.seed_signal(**defaults)


def _seed_release_note(**kw):
    defaults = dict(id="sig_rn_1", source="release-notes", kind="release_note",
                     actor="operator@example.com", ingested_by="agent-platform",
                     product="alpha", stream="v1.0.0", body="What's new.")
    defaults.update(kw)
    return library_fake.seed_signal(**defaults)


def test_changelog_section_shows_only_this_products_entries(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_changelog(id="sig_cl_alpha", product="alpha", stream="alpha",
                    body="ALPHA-CHANGELOG-ENTRY")
    _seed_changelog(id="sig_cl_beta", product="beta", stream="beta",
                    body="BETA-CHANGELOG-ENTRY")
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "ALPHA-CHANGELOG-ENTRY" in html
    assert "BETA-CHANGELOG-ENTRY" not in html


def test_changelog_section_drops_unauthored_github_rows(monkeypatch):
    """Same `source=="co"` filter `/engineering/changelog` applies
    (ARCH-411) — the git-derived row is raw material for changelog-drain,
    never shown as an entry."""
    library_fake.patch(monkeypatch)
    _seed_changelog(id="sig_cl_authored", source="co", body="AUTHORED-ENTRY")
    _seed_changelog(id="sig_cl_github", source="github", actor="operator-gh",
                    body="RAW-GITHUB-MERGE-ROW")
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "AUTHORED-ENTRY" in html
    assert "RAW-GITHUB-MERGE-ROW" not in html


def test_changelog_section_reads_the_repo_name_not_the_course_alias(monkeypatch):
    """The Bosun's changelog entries are filed under `agent-platform`, its
    repo name, though its Course and epics are filed under `bosun`. Keying
    this section by `bosun` showed an empty changelog on
    `/projects/agent-platform` over 163 live entries."""
    library_fake.patch(monkeypatch)
    _seed_changelog(id="sig_cl_ap", product="agent-platform", stream="agent-platform",
                    body="AGENT-PLATFORM-CHANGELOG-ENTRY")
    _seed_changelog(id="sig_cl_alpha", product="alpha", stream="alpha",
                    body="ALPHA-CHANGELOG-ENTRY")
    _stub_fetch(monkeypatch)
    html = p.detail_html("agent-platform", frozenset({"*"}), True)
    assert "AGENT-PLATFORM-CHANGELOG-ENTRY" in html
    assert "ALPHA-CHANGELOG-ENTRY" not in html
    assert '/engineering/changelog?product=agent-platform' in html
    assert '/engineering/changelog?product=bosun' not in html


def test_changelog_section_empty_state(monkeypatch):
    library_fake.patch(monkeypatch)
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "No changelog entries for this project yet." in html
    assert '/engineering/changelog?product=alpha' in html


def test_changelog_section_unavailable_on_a_library_failure(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_signals", _boom)
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "Changelog unavailable" in html
    # the rest of the page still renders rather than 500ing
    assert "No bead on file for this project yet." in html


def test_release_notes_section_shows_only_this_products_entries(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_release_note(id="sig_rn_alpha", product="alpha", stream="v1.0.0",
                       body="ALPHA-RELEASE-NOTE")
    _seed_release_note(id="sig_rn_beta", product="beta", stream="v2.0.0",
                       body="BETA-RELEASE-NOTE")
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "ALPHA-RELEASE-NOTE" in html
    assert "BETA-RELEASE-NOTE" not in html


def test_release_notes_section_matches_through_the_repo_alias(monkeypatch):
    library_fake.patch(monkeypatch)
    _seed_release_note(id="sig_rn_bosun", product="bosun", stream="v1.0.0",
                       body="BOSUN-RELEASE-NOTE")
    _seed_release_note(id="sig_rn_alpha", product="alpha", stream="v1.0.0",
                       body="ALPHA-RELEASE-NOTE")
    _stub_fetch(monkeypatch)
    html = p.detail_html("agent-platform", frozenset({"*"}), True)
    assert "BOSUN-RELEASE-NOTE" in html
    assert "ALPHA-RELEASE-NOTE" not in html
    assert '/products/release-notes?product=bosun' in html
    assert '/products/release-notes?product=agent-platform' not in html


def test_release_notes_section_empty_state(monkeypatch):
    library_fake.patch(monkeypatch)
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "No release notes for this project yet." in html
    assert '/products/release-notes?product=alpha' in html


def test_release_notes_section_unavailable_on_a_library_failure(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_signals", _boom)
    _stub_fetch(monkeypatch)
    html = p.detail_html("alpha", frozenset({"*"}), True)
    assert "Release notes unavailable" in html
    assert "No bead on file for this project yet." in html


def test_products_for_repo_is_identity_for_an_unaliased_project():
    assert p._products_for_repo("alpha") == ["alpha"]


def test_products_for_repo_resolves_the_bosun_alias():
    assert p._products_for_repo("agent-platform") == ["bosun"]


def test_running_the_module_as_a_script_writes_the_static_fallback(monkeypatch, tmp_path):
    """serve.sh runs `project_build.py` as a script at startup. With its
    `__main__` block above `_CSS`, `build_static()` read `_CSS` before it was
    assigned and the fallback went unbuilt on every restart (CQ·173)."""
    import runpy

    import paths
    library_fake.patch(monkeypatch)
    monkeypatch.setattr(paths, "STATE_ROOT", tmp_path)
    runpy.run_path(p.__file__, run_name="__main__")
    assert (tmp_path / "site" / "projects.html").read_text(encoding="utf-8")
