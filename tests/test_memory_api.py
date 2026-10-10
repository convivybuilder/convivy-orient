"""memory_api.py — /reference's on-demand body fetch and full-text search
(issue_01M0Y7GZED2THYETHCT8NPHHDS).

`engineering_build.render()` dropped `body` from /reference's payload (94% of a
10.5 MB page); these two routes are what the drawer and the search box call
instead. The point of this file is the same one the issue calls out by name:
a search that only covers the Library-backed KB half of the corpus would
silently miss a phrase in an ARCH entry, which the Library never serves at
all. Both halves are exercised here, for both routes.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import access_identity
import api_common
import authz
import engineering_build
import library_client
import server as srv


def _doc_stub(**over):
    base = dict(id="proj/kind/doc-1", source_ref="projects/proj/kind/doc-1.md",
                kind="reference", project="proj", title="A Document", status="current",
                topics=[], audience=["team"], last_reviewed=None, updated_at=None,
                body="the third paragraph carries zephyrquartz-signature nowhere else")
    base.update(over)
    return library_client.ReferenceDocument(**base)


@pytest.fixture
def client(tmp_path, monkeypatch):
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "mem@example.com": {"role": "member", "products": ["vivy"]},
    }))
    monkeypatch.setattr(authz, "ROLES", roles)

    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": ["mem@example.com"]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)

    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)

    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({"agent-secret": {"name": "an-agent", "products": ["vivy"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)

    log = tmp_path / "decisions.md"
    log.write_text(
        "## ARCH-001: first entry\n\nSome unrelated prose.\n"
        "## ARCH-002: second entry\n\nA phrase found nowhere else on the "
        "page: zephyrquartz-signature.\n")
    monkeypatch.setattr(engineering_build, "ARCH_LOG", log)

    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


def _human(email):
    return {srv.EMAIL_HEADER: email}


DOC = "proj/kind/doc-1"


# --- the gate ------------------------------------------------------------

def test_a_contractor_tier_identity_gets_a_plain_404(client):
    r = client.get(f"/api/memory/documents/{DOC}/body",
                    headers={srv.EMAIL_HEADER: "contractor-only@example.com"})
    assert r.status_code == 404


def test_unauthenticated_is_401(client):
    r = client.get(f"/api/memory/documents/{DOC}/body")
    assert r.status_code == 401


def test_an_agent_bearer_token_is_refused_a_human_only_surface(client):
    r = client.get(f"/api/memory/documents/{DOC}/body",
                    headers={"Authorization": "Bearer agent-secret"})
    assert r.status_code == 403
    assert "human" in r.json()["error"]


def test_search_gate_applies_too(client):
    r = client.get("/api/memory/search", params={"q": "x"},
                    headers={srv.EMAIL_HEADER: "contractor-only@example.com"})
    assert r.status_code == 404


# --- document_body: dispatch by id shape, no client-supplied kind --------

def test_body_route_serves_a_kb_doc_from_the_library(client, monkeypatch):
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, *, audience: _doc_stub(id=doc_id))
    r = client.get(f"/api/memory/documents/{DOC}/body", headers=_human("mem@example.com"))
    assert r.status_code == 200
    assert "zephyrquartz-signature" in r.json()["body"]


def test_body_route_404s_a_kb_doc_the_library_does_not_know(client, monkeypatch):
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, *, audience: None)
    r = client.get(f"/api/memory/documents/{DOC}/body", headers=_human("mem@example.com"))
    assert r.status_code == 404


def test_body_route_503s_on_a_library_outage(client, monkeypatch):
    def boom(doc_id, *, audience):
        raise ConnectionError("down")
    monkeypatch.setattr(library_client, "get_reference_document", boom)
    r = client.get(f"/api/memory/documents/{DOC}/body", headers=_human("mem@example.com"))
    assert r.status_code == 503


def test_body_route_serves_an_arch_entry_with_no_library_call(client, monkeypatch):
    """The split-source trap the issue calls out: an ARCH id must resolve
    without ever reaching the Library, which does not serve these at all."""
    def must_not_be_called(doc_id, *, audience):
        raise AssertionError("an ARCH id must not be looked up in the Library")
    monkeypatch.setattr(library_client, "get_reference_document", must_not_be_called)
    r = client.get("/api/memory/documents/substrate/arch/arch-2/body",
                    headers=_human("mem@example.com"))
    assert r.status_code == 200
    assert "zephyrquartz-signature" in r.json()["body"]


def test_body_route_404s_an_unknown_arch_id(client):
    r = client.get("/api/memory/documents/substrate/arch/arch-999/body",
                    headers=_human("mem@example.com"))
    assert r.status_code == 404


# --- search: the regression this issue exists to prevent -----------------
#
# A phrase appearing ONLY in a document's body (not its title, blurb,
# reflabel or id) must still be findable, for both halves of the split
# corpus — a route that covered only one would silently degrade search for
# the other, exactly the failure mode the issue names.

def test_search_finds_a_body_only_phrase_in_a_kb_doc(client, monkeypatch):
    doc = _doc_stub(title="A Document", id=DOC)   # phrase is in body, not title/id

    def fake_search(query, *, audience, cap=None):
        assert query == "zephyrquartz-signature"
        return [doc] if "zephyrquartz-signature" in doc.body else []
    monkeypatch.setattr(library_client, "search_reference", fake_search)

    r = client.get("/api/memory/search", params={"q": "zephyrquartz-signature"},
                    headers=_human("mem@example.com"))
    assert r.status_code == 200
    assert DOC in r.json()["ids"]


def test_search_finds_a_body_only_phrase_in_an_arch_entry(client, monkeypatch):
    monkeypatch.setattr(library_client, "search_reference", lambda query, *, audience, cap=None: [])
    r = client.get("/api/memory/search", params={"q": "zephyrquartz-signature"},
                    headers=_human("mem@example.com"))
    assert r.status_code == 200
    assert "substrate/arch/arch-2" in r.json()["ids"]
    # the entry with no matching phrase must not show up
    assert "substrate/arch/arch-1" not in r.json()["ids"]


def test_search_combines_both_sources_in_one_response(client, monkeypatch):
    doc = _doc_stub(id=DOC)
    monkeypatch.setattr(library_client, "search_reference",
                        lambda query, *, audience, cap=None: [doc])
    r = client.get("/api/memory/search", params={"q": "zephyrquartz-signature"},
                    headers=_human("mem@example.com"))
    ids = r.json()["ids"]
    assert DOC in ids and "substrate/arch/arch-2" in ids


def test_search_503s_on_a_library_outage_rather_than_a_partial_result(client, monkeypatch):
    """A Library outage must read as a failed search, not as 'found nothing in
    the KB half' — silently returning the ARCH-only results would look
    identical to a healthy search that happened to match no KB doc."""
    def boom(query, *, audience, cap=None):
        raise ConnectionError("down")
    monkeypatch.setattr(library_client, "search_reference", boom)
    r = client.get("/api/memory/search", params={"q": "zephyrquartz-signature"},
                    headers=_human("mem@example.com"))
    assert r.status_code == 503


def test_search_empty_query_returns_no_ids_without_calling_anything(client, monkeypatch):
    def must_not_be_called(*a, **k):
        raise AssertionError("an empty query must not reach the Library")
    monkeypatch.setattr(library_client, "search_reference", must_not_be_called)
    r = client.get("/api/memory/search", params={"q": ""}, headers=_human("mem@example.com"))
    assert r.status_code == 200
    assert r.json()["ids"] == []
