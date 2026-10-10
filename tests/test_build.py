"""build.load_docs() — the Library reader that feeds build.build()'s static
bundle (serve.sh runs it, unconditionally, before every other builder).

Moved off a direct `~/.knowledge-index/index.db` read onto `library_client`'s
reference readers (issue_01M1MFJRR46TMQ6DSJSD7DGX7C) — the entitlement gate
(`"team" in audience`) now lives in `library_client._entitled`, exercised
here through `library_fake` rather than reimplemented, so these tests seed
`tests/library_fake.py` instead of building a real `index.db` in `tmp_path`.
The malformed/non-list-audience fail-closed cases that used to live here
moved with the gate — `tests/test_library_client.py` covers them for the
route both `list_reference_documents` and `get_reference_document` share.
"""
import dataclasses

import pytest

import build
import library_client
import library_fake


def test_load_docs_returns_only_team_entitled_docs(monkeypatch):
    """`list_reference_documents(audience="team")` already applies the
    entitlement gate — a doc the Library doesn't consider team-audience
    (a bare `["llm"]` tag, no `"team"` at all) must not reach the bundle."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="shared/reference/good", title="Good",
                                    audience=["team"], body="b\n")
    library_fake.seed_reference_doc(id="shared/reference/llm-only", title="LLM only",
                                    audience=["llm"], body="b\n")
    docs = build.load_docs()
    ids = {d["id"] for d in docs}
    assert ids == {"shared/reference/good"}


def test_load_docs_contractor_flag_from_audience(monkeypatch):
    """`load_docs()` computes `contractor_ok` from the fetched document's
    parsed `audience` list — a team doc with "contractor" is flagged, a team
    doc without it isn't."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="shared/reference/team-only", title="TEAM-ONLY",
                                    audience=["team"], body="b\n")
    library_fake.seed_reference_doc(id="shared/reference/team-and-contractor",
                                    title="TEAM-AND-CONTRACTOR",
                                    audience=["team", "contractor"], body="b\n")
    docs = {d["id"]: d for d in build.load_docs()}
    assert docs["shared/reference/team-only"]["contractor"] is False
    assert docs["shared/reference/team-and-contractor"]["contractor"] is True


def test_load_docs_cleans_the_body_and_carries_metadata(monkeypatch):
    """Each row carries the fields `render()` needs, and the body has been
    through `_clean_body` (frontmatter/HIDE_SECTIONS stripped) — the one
    piece of site policy that stays in Orient rather than moving to the
    Library (epic issue_01M08YBCDTEFJ6N9S798ERF89P)."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(
        id="vivy/runbooks/deploy", project="vivy", kind="runbook",
        title="Deploy runbook", audience=["team"],
        body="---\nfoo: bar\n---\n## Provenance\nsecret\n## Steps\ndo it\n")
    docs = build.load_docs()
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "vivy/runbooks/deploy"
    assert doc["project"] == "vivy"
    assert doc["kind"] == "runbook"
    assert doc["title"] == "Deploy runbook"
    assert doc["status"] == "current"
    assert "secret" not in doc["body"]
    assert "do it" in doc["body"]


def test_load_docs_sorted_by_project_kind_title(monkeypatch):
    """The Library's listing order is not guaranteed; load_docs() re-sorts to
    match the old SQL's `ORDER BY project, kind, title` so the rendered
    bundle doesn't reshuffle on every build."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="b/z", project="b", kind="reference",
                                    title="Z", audience=["team"], body="x\n")
    library_fake.seed_reference_doc(id="a/z", project="a", kind="reference",
                                    title="Z", audience=["team"], body="x\n")
    library_fake.seed_reference_doc(id="a/a", project="a", kind="overview",
                                    title="A", audience=["team"], body="x\n")
    ids = [d["id"] for d in build.load_docs()]
    assert ids == ["a/a", "a/z", "b/z"]


def test_load_docs_respects_browse_exclude_ids(monkeypatch):
    """A doc listed in BROWSE_EXCLUDE_IDS is dropped even though it is
    otherwise team-entitled — its canonical home is a curated section
    elsewhere on the site (ARCH-308)."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="excluded/doc", title="Excluded",
                                    audience=["team"], body="x\n")
    library_fake.seed_reference_doc(id="kept/doc", title="Kept",
                                    audience=["team"], body="x\n")
    monkeypatch.setattr(build, "BROWSE_EXCLUDE_IDS", {"excluded/doc"})
    ids = {d["id"] for d in build.load_docs()}
    assert ids == {"kept/doc"}


def test_load_docs_raises_on_a_library_failure(monkeypatch):
    """A batch site build must fail loudly rather than write a bundle
    silently missing every document — the worse failure, since a half-built
    site that looks complete gives no sign anything is wrong."""
    def _boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_reference_documents", _boom)
    with pytest.raises(RuntimeError):
        build.load_docs()


def test_load_docs_drops_a_doc_that_vanishes_between_the_list_and_the_fetch(monkeypatch):
    """`load_docs()` fetches each listed document individually, so a doc can be
    deleted or re-tagged in the window between the two calls and come back
    `None` — a real race against a live Library during a 373-document batch,
    not a hypothetical. That row is DROPPED and the rest of the bundle builds
    (`build.py:278-283`).

    Pinned because the branch is the whole difference between losing one stale
    reference and losing the build: a regression turning the `continue` into a
    raise would abort every restart over a single mid-build edit, and one
    turning it into a silent pass-through would put a `None` body into
    `_clean_body`. Neither is visible without this test (reviewer, PR #262)."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="shared/reference/stays", title="Stays",
                                    audience=["team"], body="b\n")
    library_fake.seed_reference_doc(id="shared/reference/vanishes", title="Vanishes",
                                    audience=["team"], body="b\n")
    listed = library_client.get_reference_document

    def _gone_for_one(doc_id, **kw):
        return None if doc_id == "shared/reference/vanishes" else listed(doc_id, **kw)

    monkeypatch.setattr(library_client, "get_reference_document", _gone_for_one)
    docs = build.load_docs()
    assert {d["id"] for d in docs} == {"shared/reference/stays"}


def test_load_docs_drops_a_doc_whose_body_is_none(monkeypatch):
    """The same branch's other half. `full.body is None` is checked alongside
    `full is None` because `_clean_body` would raise on it, turning one
    body-less document into a failed build — the outcome the drop exists to
    avoid."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="shared/reference/stays", title="Stays",
                                    audience=["team"], body="b\n")
    library_fake.seed_reference_doc(id="shared/reference/bodyless", title="Bodyless",
                                    audience=["team"], body="b\n")
    listed = library_client.get_reference_document

    def _bodyless_for_one(doc_id, **kw):
        doc = listed(doc_id, **kw)
        if doc is not None and doc_id == "shared/reference/bodyless":
            return dataclasses.replace(doc, body=None)
        return doc

    monkeypatch.setattr(library_client, "get_reference_document", _bodyless_for_one)
    docs = build.load_docs()
    assert {d["id"] for d in docs} == {"shared/reference/stays"}
