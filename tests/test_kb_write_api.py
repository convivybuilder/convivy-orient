"""kb_write_api.py — the in-window KB editor's coordination routes
(issue_01M0TKX6DW2E1TXGN9QT6K61J5 §4).

The gate is the point of this file, weighted the same way test_blog_api.py
weights attestation over CRUD: full tier AND kb_write, both required. The
commit step itself (kb_commit.py, which shells to `agentctl kb apply`) is
mocked throughout — its own subprocess-level behaviour is pinned in
test_kb_commit.py — so what's tested here is this file's branch logic: which
`kb_commit` exception maps to which status, and that the checkout is never
released on a failed save (test_save_reports_each_commit_failure_as_its_own_
outcome and its neighbors).

Every `library_client` reference-coordination call is mocked directly (no
fake-Library double for these exists yet in tests/library_fake.py), matching
how test_reference_doc_build.py verifies this surface.
"""
from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

import access_identity
import api_common
import authz
import control_db
import kb_commit
import kb_write_api
import library_client
import server as srv

# Captured before any fixture stubs it, for the test of the gate itself.
_REAL_ACCESS_ENFORCED = kb_write_api._access_enforced


def _doc_stub(**over):
    base = dict(id=DOC, source_ref="projects/proj/kind/doc-1.md", kind="reference",
               project="proj", title="A Document", status="current", topics=[],
               audience=["team"], last_reviewed=None, updated_at=None, body="old body")
    base.update(over)
    return library_client.ReferenceDocument(**base)


@pytest.fixture(autouse=True)
def _document_is_visible(monkeypatch):
    """Every route here first asks `get_reference_document` whether the caller
    may touch this document at all (PR #207 security review), so without this
    the tests below would all fail closed at that gate instead of exercising
    their own subject. The gate itself is tested explicitly further down —
    `test_a_non_team_document_cannot_be_locked_by_id` and its siblings — rather
    than relied on incidentally here."""
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, *, audience: _doc_stub())


_REAL_READ_DOCUMENT = kb_commit.read_document


@pytest.fixture(autouse=True)
def _no_real_agentctl(monkeypatch):
    """The checkout route now reads the stored file through `agentctl kb
    checkout`, which does a `git fetch` to GitHub with whatever credential the
    machine holds. Every test here reaches that route or a sibling, so without
    this an unmocked one would run the real binary. `read_document` answers a
    fixed file by default; `_run` raises for anything that gets past it. The
    seam tests below restore the real `read_document` and fake `_run` instead."""
    monkeypatch.setattr(kb_commit, "read_document",
                        lambda doc_id, *, holder: kb_commit.StoredDocument(
                            content=_STORED_FILE, content_sha="sha-stored"))

    def refuse(agentctl, args):
        raise AssertionError(f"a test reached the real agentctl: {args[:2]}")

    monkeypatch.setattr(kb_commit, "_run", refuse)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()

    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "mem@example.com": {"role": "member", "products": ["vivy"]},
        "obs@example.com": {"role": "observer", "products": ["*"]},
        "con-full@example.com": {"role": "contractor", "products": ["vivy"]},
    }))
    monkeypatch.setattr(authz, "ROLES", roles)
    # The project scope now comes from the Library (E19·1,
    # issue_01M35K54QHJYYPXWS28YWEHJP3), never from roles.json's `products` —
    # stubbed by re-reading the same roles.json file on every call, the way
    # the pre-E19·1 `human_identity` re-read it fresh, rather than a dict
    # snapshot that would go stale against a test that rewrites the file.
    monkeypatch.setattr(
        library_client, "person_scope",
        lambda email: frozenset(
            json.loads(authz.ROLES.read_text()).get(email, {}).get("products", [])))

    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": [
        "operator@example.com", "mem@example.com", "obs@example.com", "con-full@example.com",
    ]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)

    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)
    # The save tests run in monitor mode (identity from the plaintext header),
    # where a real save is refused because it would commit under `person`
    # without a verified assertion. They exercise the save path past that
    # gate; the gate itself is tested on its own below.
    monkeypatch.setattr(kb_write_api, "_access_enforced", lambda: True)

    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({"agent-secret": {"name": "an-agent", "products": ["vivy"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)

    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


def _human(email, origin="http://testserver"):
    return {srv.EMAIL_HEADER: email, "Origin": origin}


DOC = "proj/kind/doc-1"
# A well-formed content-sha, the shape `kb_writes.content_sha` returns: what
# every save below posts as the version its editor opened at.
_OPEN_SHA = "a" * 64

# The file as origin/main stores it: a frontmatter block, then the body.
_STORED_FILE = ("---\nid: proj/kind/doc-1\ntitle: A Document\nstatus: current\n"
                "audience:\n- team\n---\n\n# A Document\n\nBody text.\n")


def _checkout_row(*, holder="mem@example.com"):
    import datetime as dt
    return library_client.ReferenceCheckout(
        id="chk_1", doc_id=DOC, holder=holder, run_id=None,
        taken_at=dt.datetime(2026, 8, 25, 17, 0), heartbeat_at=dt.datetime(2026, 8, 25, 17, 0),
        expires_at=dt.datetime(2026, 8, 25, 18, 0), released_at=None,
        release_reason=None, released_by=None)


# --- the gate: full tier AND kb.write, checked here rather than assumed -----


def test_a_contractor_tier_identity_gets_a_plain_404(client, monkeypatch):
    """Not 403 — a contractor-tier viewer is not told this surface exists,
    matching every other full-tier route in server.py."""
    monkeypatch.setattr(library_client, "checkout_reference_document",
                        lambda doc_id, **k: _checkout_row())
    r = client.post(f"/api/kb/documents/{DOC}/checkout",
                    headers={srv.EMAIL_HEADER: "contractor-only@example.com",
                             "Origin": "http://testserver"})
    assert r.status_code == 404


def test_a_full_tier_viewer_without_kb_write_is_refused(client):
    # obs@example.com is full-tier (observer) but observer does not hold
    # kb_write — full tier alone must not be enough.
    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("obs@example.com"))
    assert r.status_code == 403
    assert "kb.write" in r.json()["error"]


def test_an_agent_bearer_token_is_refused_a_human_only_surface(client):
    r = client.post(f"/api/kb/documents/{DOC}/checkout",
                    headers={"Authorization": "Bearer agent-secret"})
    assert r.status_code == 403
    assert "human" in r.json()["error"]


def test_missing_origin_is_a_csrf_refusal(client):
    r = client.post(f"/api/kb/documents/{DOC}/checkout",
                    headers={srv.EMAIL_HEADER: "mem@example.com"})
    assert r.status_code == 403
    assert "csrf" in r.json()["error"]


# --- checkout ----------------------------------------------------------------


def test_checkout_succeeds_for_a_full_tier_kb_writer(client, monkeypatch):
    seen = {}

    def fake_checkout(doc_id, *, holder, run_id=None):
        seen["doc_id"] = doc_id
        seen["holder"] = holder
        return _checkout_row(holder=holder)

    monkeypatch.setattr(library_client, "checkout_reference_document", fake_checkout)
    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))
    assert r.status_code == 200
    assert r.json()["checkout"]["holder"] == "mem@example.com"
    assert seen["doc_id"] == DOC
    assert seen["holder"] == "mem@example.com"  # the verified identity, not caller input


def test_checkout_returns_the_stored_file_and_its_sha(client, monkeypatch):
    seen = {}

    def fake_read(doc_id, *, holder):
        seen["args"] = (doc_id, holder)
        return kb_commit.StoredDocument(content=_STORED_FILE, content_sha="sha-at-open")

    monkeypatch.setattr(library_client, "checkout_reference_document",
                        lambda doc_id, *, holder, run_id=None: _checkout_row(holder=holder))
    monkeypatch.setattr(kb_commit, "read_document", fake_read)
    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))

    assert r.status_code == 200
    body = r.json()
    assert body["content"] == _STORED_FILE
    assert body["content"].startswith("---\n")
    assert body["content_sha"] == "sha-at-open"
    assert body["checkout"]["holder"] == "mem@example.com"  # the Library lock is still taken
    assert seen["args"] == (DOC, "mem@example.com")  # the verified identity, not caller input


@pytest.mark.parametrize("exc,status", [
    (kb_commit.CommitFailed("could not fetch origin/main"), 502),
    (kb_commit.CommitLeaseConflict("leased by another-holder"), 409),
])
def test_an_unreadable_document_fails_the_checkout_and_gives_the_lock_back(
        client, monkeypatch, exc, status):
    """A lock with no content behind it opens an editor on nothing, and the
    member learns it only when the save is refused. So the whole checkout
    fails, and the Library lock this request just took is handed back rather
    than left for its TTL."""
    checked_in = []

    def unreadable(doc_id, *, holder):
        raise exc

    monkeypatch.setattr(library_client, "checkout_reference_document",
                        lambda doc_id, *, holder, run_id=None: _checkout_row(holder=holder))
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, *, holder: checked_in.append((doc_id, holder)))
    monkeypatch.setattr(kb_commit, "read_document", unreadable)
    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))

    assert r.status_code == status
    assert "content" not in r.json() and "checkout" not in r.json()
    assert checked_in == [(DOC, "mem@example.com")]


def test_a_failed_checkin_does_not_replace_the_read_failure(client, monkeypatch):
    def unreadable(doc_id, *, holder):
        raise kb_commit.CommitFailed("could not fetch origin/main")

    def checkin_down(doc_id, *, holder):
        raise ConnectionError("Library unreachable")

    monkeypatch.setattr(library_client, "checkout_reference_document",
                        lambda doc_id, *, holder, run_id=None: _checkout_row(holder=holder))
    monkeypatch.setattr(library_client, "checkin_reference_document", checkin_down)
    monkeypatch.setattr(kb_commit, "read_document", unreadable)
    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))
    assert r.status_code == 502


def test_a_held_document_is_not_read(client, monkeypatch):
    """A refused Library checkout hands the editor no content: it opens
    read-only on the holder's name, and the file is not fetched for nothing."""
    read = []

    def fake_checkout(doc_id, *, holder, run_id=None):
        raise library_client.ReferenceCheckoutConflict(_checkout_row(holder="operator@example.com"))

    monkeypatch.setattr(library_client, "checkout_reference_document", fake_checkout)
    monkeypatch.setattr(kb_commit, "read_document",
                        lambda doc_id, *, holder: read.append(doc_id))
    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))
    assert r.status_code == 409
    assert read == []
    assert "content" not in r.json()


def test_checkout_conflict_names_the_holder(client, monkeypatch):
    def fake_checkout(doc_id, *, holder, run_id=None):
        raise library_client.ReferenceCheckoutConflict(_checkout_row(holder="operator@example.com"))

    monkeypatch.setattr(library_client, "checkout_reference_document", fake_checkout)
    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))
    assert r.status_code == 409
    assert r.json()["checkout"]["holder"] == "operator@example.com"


def test_checkout_reports_writes_not_enabled_as_503(client, monkeypatch):
    def fake_checkout(doc_id, *, holder, run_id=None):
        raise library_client.ReferenceWritesNotEnabled("shadow")

    monkeypatch.setattr(library_client, "checkout_reference_document", fake_checkout)
    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))
    assert r.status_code == 503


# --- heartbeat -----------------------------------------------------------


def test_heartbeat_refreshes_the_lock(client, monkeypatch):
    monkeypatch.setattr(library_client, "heartbeat_reference_checkout",
                        lambda doc_id, *, holder: _checkout_row(holder=holder))
    r = client.post(f"/api/kb/documents/{DOC}/heartbeat", headers=_human("mem@example.com"))
    assert r.status_code == 200


def test_a_lapsed_heartbeat_is_reported_not_silent(client, monkeypatch):
    monkeypatch.setattr(library_client, "heartbeat_reference_checkout",
                        lambda doc_id, *, holder: None)
    r = client.post(f"/api/kb/documents/{DOC}/heartbeat", headers=_human("mem@example.com"))
    assert r.status_code == 409


# --- checkin -------------------------------------------------------------


def test_checkin_releases(client, monkeypatch):
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, *, holder: _checkout_row(holder=holder))
    r = client.post(f"/api/kb/documents/{DOC}/checkin", headers=_human("mem@example.com"))
    assert r.status_code == 200
    assert r.json()["released"] is True


# --- the entitlement gate: holding kb.write is not being entitled to a doc ---
#
# 359 of the Library's 702 documents are not team-audience. `/reference` lists
# them to a full-tier reader by id, title and a summary (the operator kept that on
# 2026-09-12), but never serves their body. Every route here takes `doc_id` straight
# from the request path, and the lock routes never read a body — so before this
# gate existed, an admin or member could take, refresh or release the Library's
# real edit-lock on any of them by id. The disclosure is not only the lock: a
# 200 from the lock table CONFIRMS a doc id that a read would refuse, which
# makes it an existence oracle over exactly the half of the corpus the audience
# rule exists to hide (PR #207 security review).


@pytest.mark.parametrize("route,method", [
    ("checkout", "post"), ("heartbeat", "post"), ("checkin", "post"), ("save", "post"),
])
def test_a_document_this_viewer_may_not_read_cannot_be_locked_by_id(
        client, monkeypatch, route, method):
    """Not-ours and not-there must answer the same. `get_reference_document`
    already collapses them to None (PR #205), and this asserts the lock routes
    inherit that rather than reintroducing a distinguishable refusal."""
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, *, audience: None)
    called = []
    for fn in ("checkout_reference_document", "heartbeat_reference_checkout",
               "checkin_reference_document"):
        monkeypatch.setattr(library_client, fn,
                            lambda *a, **k: called.append(True))
    r = client.post(f"/api/kb/documents/{DOC}/{route}", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": "x"})
    assert r.status_code == 404
    assert r.json()["error"] == "no such document"
    assert called == [], "the Library's lock was touched for a document the viewer may not read"


def test_the_gate_asks_for_the_team_audience(client, monkeypatch):
    """A stub cannot catch the page asking for the wrong audience, so assert
    the argument directly."""
    seen = {}

    def fake(doc_id, *, audience):
        seen["audience"] = audience
        return _doc_stub()

    monkeypatch.setattr(library_client, "get_reference_document", fake)
    monkeypatch.setattr(library_client, "checkout_reference_document",
                        lambda doc_id, **k: _checkout_row())
    client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))
    assert seen["audience"] == "team"


def test_an_unreachable_library_fails_closed_rather_than_locking(client, monkeypatch):
    """A lock we cannot justify is a lock we do not take. If the Library cannot
    answer whether this document is ours, refuse rather than proceed."""
    def boom(doc_id, *, audience):
        raise ConnectionError("Library unreachable")

    touched = []
    monkeypatch.setattr(library_client, "get_reference_document", boom)
    monkeypatch.setattr(library_client, "checkout_reference_document",
                        lambda *a, **k: touched.append(True))
    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))
    assert r.status_code == 503
    assert touched == []


# --- save: admission runs for real; commit is mocked (its own subprocess
# behaviour is pinned in test_kb_commit.py) ------------------------------------


def _body_with_frontmatter(fm: dict, text: str = "content") -> str:
    return f"---\n{yaml.safe_dump(fm)}---\n\n{text}\n"


# A body with SOME well-formed frontmatter block, for tests below that are not
# about frontmatter content at all — since round 3, a body with no `---`
# block fails closed (422) before admission or commit are ever reached
# (test_save_fails_closed_when_the_body_has_no_frontmatter_block), so a bare
# "new body" string no longer exercises the post-admission/commit branches
# these tests actually target.
_SOME_VALID_BODY = _body_with_frontmatter({"id": DOC, "title": "A Document"}, "new body")


# --- the post-commit branches ------------------------------------------------
#
# Unit tests of THIS file's branch logic with `kb_commit.commit_document`
# mocked as a collaborator — they need no subprocess or git wiring at all. The
# proof this technique catches real bugs: the missing `except` around the
# post-commit checkin lived in exactly this region before PR #207's review.


def _ready_to_commit(monkeypatch, ident="mem@example.com"):
    """Everything up to the commit succeeding: the checkout is held by this
    caller, and admission passes."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder=ident))
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: library_client.AdmissionResult(may_land=True, errors=[]))
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: None)


def test_a_successful_save_reports_saved_and_checked_in(client, monkeypatch):
    _ready_to_commit(monkeypatch)
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, **k: _checkout_row())
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": _SOME_VALID_BODY})
    assert r.status_code == 200
    assert r.json() == {"saved": True, "checked_in": True}


@pytest.mark.parametrize("checkin", [
    lambda doc_id, **k: None,                                    # released nothing
    lambda doc_id, **k: (_ for _ in ()).throw(
        library_client.ReferenceWritesNotEnabled("off")),        # Library refused
    lambda doc_id, **k: (_ for _ in ()).throw(ConnectionError("down")),  # unreachable
])
def test_a_failed_checkin_after_a_successful_commit_still_reports_saved(
        client, monkeypatch, checkin):
    """**The commit has already landed. Nothing may report failure to save.**

    This is the partial failure the spec names as the one that strands a
    document, and the inversion the reviewer caught: before the fix, a 503 here
    fell through to a generic 500 and the editor said "Couldn't save" — telling
    a human their edit was lost at the moment it had been committed."""
    _ready_to_commit(monkeypatch)
    monkeypatch.setattr(library_client, "checkin_reference_document", checkin)
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": _SOME_VALID_BODY})
    assert r.status_code == 200
    payload = r.json()
    assert payload["saved"] is True, "the commit landed; this must never say otherwise"
    assert payload["checked_in"] is False
    assert "still locked" in payload["warning"]


def test_a_502_save_writes_its_cause_to_stderr(client, monkeypatch, capsys):
    """The save's 502 carries agentctl's reason in the response body, which the
    editor renders as "Couldn't save" and nothing else reads. The reason must
    also reach serve.log, or a failed save leaves no server-side trace (the
    2026-09-18 overwrite refusal left none). The reason here is a control-
    character-laden message: it must land on one line, so a message cannot
    forge a log line of its own."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc_stub())
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: library_client.AdmissionResult(may_land=True, errors=[]))

    def refused(doc_id, *, body, actor, holder, base_sha, assertion):
        raise kb_commit.CommitFailed(
            "kb apply: refused - target exists-6183\nsave failed in FORGED LINE")

    monkeypatch.setattr(kb_commit, "commit_document", refused)

    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": _SOME_VALID_BODY})

    assert r.status_code == 502
    lines = capsys.readouterr().err.splitlines()
    assert any("target exists-6183" in ln and "CommitFailed" in ln for ln in lines)
    assert any(f"POST /api/kb/documents/{DOC}/save" in ln for ln in lines)
    assert not any(ln.startswith("save failed in FORGED") for ln in lines)


def test_save_requires_holding_the_checkout(client, monkeypatch):
    monkeypatch.setattr(library_client, "get_reference_checkout", lambda doc_id: None)
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": "new text"})
    assert r.status_code == 409


def test_save_refuses_an_empty_body(client, monkeypatch):
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": "   "})
    assert r.status_code == 400


def test_save_reports_a_failed_admission_and_does_not_attempt_commit(client, monkeypatch):
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc_stub())
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: library_client.AdmissionResult(
                            may_land=False, errors=["missing required field: title"]))

    committed = []
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: committed.append(1))

    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": _SOME_VALID_BODY})
    assert r.status_code == 422
    assert "missing required field: title" in r.json()["errors"]
    assert committed == []  # admission refused it — the commit must never run


@pytest.mark.parametrize("exc,status", [
    (kb_commit.CommitStale("origin/main moved"), 409),
    (kb_commit.CommitLeaseConflict("leased by someone else"), 409),
    (kb_commit.CommitRejected("missing required field: title"), 422),
    (kb_commit.CommitFailed("agentctl is not on PATH"), 502),
    (kb_commit.CommitAuthExpired("needs a valid Cloudflare Access assertion (expired)"), 401),
])
def test_save_reports_each_commit_failure_as_its_own_outcome(client, monkeypatch, exc, status):
    """Each way `kb_commit.commit_document` can fail is its own exception —
    a lost-update race, a lease held elsewhere, a validator rejection, or
    anything else — and must reach the human as its own status and detail,
    not collapse into one generic failure. The checkout is left held on every
    branch: nothing was written, so there is nothing to release."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc_stub())
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: library_client.AdmissionResult(may_land=True, errors=[]))
    checkin_calls = []
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, *, holder: checkin_calls.append(1))

    def raise_it(doc_id, *, body, actor, holder, base_sha, assertion):
        raise exc

    monkeypatch.setattr(kb_commit, "commit_document", raise_it)

    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": _SOME_VALID_BODY})
    assert r.status_code == status
    assert str(exc) in r.json()["detail"]
    assert checkin_calls == []  # nothing was written, so nothing is released


def test_an_expired_access_session_tells_the_human_to_reload(client, monkeypatch):
    """agentctl's exit 8 means it could not verify the assertion; the fix is a
    fresh one, which a reload of the site takes."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: library_client.AdmissionResult(may_land=True, errors=[]))

    def expired(doc_id, *, body, actor, holder, base_sha, assertion):
        raise kb_commit.CommitAuthExpired("(expired)")

    monkeypatch.setattr(kb_commit, "commit_document", expired)
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": _SOME_VALID_BODY})
    assert r.status_code == 401
    assert "your Access session expired, reload team.convivy.com" in r.json()["error"]


def test_a_save_forwards_the_requests_own_access_assertion(client, monkeypatch):
    """The assertion handed to commit_document is the raw header this request
    carried, which agentctl verifies. A body value claiming another is ignored."""
    seen = {}
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: library_client.AdmissionResult(may_land=True, errors=[]))
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, **k: _checkout_row())
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion:
                        seen.update(assertion=assertion))
    headers = {**_human("mem@example.com"), access_identity.JWT_HEADER: "header.assertion.sig"}
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=headers,
                    json={"content_sha": _OPEN_SHA, "body": _SOME_VALID_BODY,
                          "assertion": "forged.body.value"})
    assert r.status_code == 200, r.text
    assert seen["assertion"] == "header.assertion.sig"


def test_the_real_gate_reads_enforce_from_jwt_json(tmp_path, monkeypatch):
    """The fixture stubs `_access_enforced` open for the save-path tests, so
    this pins the real function both ways, and a gate inverted to always
    refuse, or always allow, fails here."""
    jwtc = tmp_path / "jwt.json"
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)
    jwtc.write_text(json.dumps({"enforce": True}))
    assert _REAL_ACCESS_ENFORCED() is True
    jwtc.write_text(json.dumps({"enforce": False}))
    assert _REAL_ACCESS_ENFORCED() is False


def test_monitor_mode_refuses_a_save_before_anything_runs(client, monkeypatch):
    """In monitor mode the route trusts the plaintext email header, so a commit
    under `person` could name someone other than the lease holder. The save is
    refused before admission, the lock or agentctl are touched."""
    # The fixture stubs the gate open; put the real one back, reading the
    # fixture's jwt.json, which is in monitor mode.
    monkeypatch.setattr(kb_write_api, "_access_enforced", _REAL_ACCESS_ENFORCED)
    touched = []
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: touched.append("checkout") or _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: touched.append("admission"))
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: touched.append("commit"))
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": _SOME_VALID_BODY})
    assert r.status_code == 503
    assert "monitor mode" in r.json()["error"]
    assert touched == []


def test_save_builds_admission_frontmatter_from_the_submitted_body(client, monkeypatch):
    """The frontmatter sent to admission comes from the body actually being
    committed, not from the Library's pre-edit record (PR #210 security
    review — the reverse of this used to be true, and that was a live
    privilege-escalation path: the stale, already-stored frontmatter always
    already validates, so re-checking it caught nothing a submitted body
    changed)."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc_stub(title="Real Title", status="draft"))
    seen = {}

    def fake_admission(fm, *, owner):
        seen["fm"] = fm
        return library_client.AdmissionResult(may_land=True, errors=[])

    monkeypatch.setattr(library_client, "check_reference_admission", fake_admission)
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: None)
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, *, holder: _checkout_row())

    submitted_body = _body_with_frontmatter({
        "id": DOC, "kind": "reference", "project": "proj", "title": "Edited Title",
        "status": "current", "topics": [], "audience": ["team"]})
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": submitted_body})
    assert r.status_code == 200
    # From the SUBMITTED body ("Edited Title"/"current"), not the stale
    # pre-edit row this test's own get_reference_document stub returns
    # ("Real Title"/"draft") — if this were still built from `doc`, these two
    # assertions would fail against the stub's values instead.
    assert seen["fm"]["title"] == "Edited Title"
    assert seen["fm"]["status"] == "current"


def test_save_admission_sees_a_body_that_widens_its_own_audience(client, monkeypatch):
    """The exact attack the security review named: a full-tier `kb.write`
    human editing a `team`-audience document submits a body whose frontmatter
    has changed `audience` to something wider. Admission must be asked about
    THAT — the escalation attempt — not about the document's still-narrow
    stored frontmatter, which would validate regardless of what the submitted
    body says."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc_stub(audience=["team"]))
    seen = {}

    def fake_admission(fm, *, owner):
        seen["fm"] = fm
        return library_client.AdmissionResult(may_land=True, errors=[])

    monkeypatch.setattr(library_client, "check_reference_admission", fake_admission)
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: None)
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, *, holder: _checkout_row())

    widened_body = _body_with_frontmatter({
        "id": DOC, "kind": "reference", "project": "proj", "title": "A Document",
        "status": "current", "topics": [], "audience": ["team", "architect", "operator"]})
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": widened_body})
    assert r.status_code == 200
    assert seen["fm"]["audience"] == ["team", "architect", "operator"], (
        "admission must see the audience the submitted body actually asks for, "
        "not the document's current, still-narrow stored audience")


def test_save_fails_closed_when_the_body_has_no_frontmatter_block(client, monkeypatch):
    """A submitted body with no `---`-delimited frontmatter block at all must
    be refused here (422) rather than sent to admission as an empty payload.
    An empty payload validates trivially against the Library's admission
    route, which only checks a field when the payload contains it — passing
    `{}` through would delegate a fail-closed decision to a remote contract
    this repo cannot verify (PR #210 round 3 security review). Neither
    admission nor commit may be reached."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc_stub())
    admission_calls = []
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: admission_calls.append(fm))
    committed = []
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: committed.append(1))

    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": "just plain markdown, no frontmatter at all"})
    assert r.status_code == 422
    assert admission_calls == []
    assert committed == []


def test_save_fails_closed_when_the_frontmatter_is_malformed(client, monkeypatch):
    """A body that opens a frontmatter block but whose YAML is not a mapping
    (a bare scalar, here) is the same "genuinely no mapping" case as a
    missing block — 422, not an empty payload forwarded to admission."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc_stub())
    admission_calls = []
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: admission_calls.append(fm))
    committed = []
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: committed.append(1))

    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": "---\njust a scalar, not a mapping\n---\n\ncontent\n"})
    assert r.status_code == 422
    assert admission_calls == []
    assert committed == []


def test_save_admission_sees_a_crlf_body_that_widens_its_own_audience(client, monkeypatch):
    """The same escalation attempt as
    test_save_admission_sees_a_body_that_widens_its_own_audience, submitted
    with CRLF line endings throughout — the shape a non-browser API
    caller can send even though a `<textarea>` never would. Before the
    round-3 fix, `_parse_frontmatter`'s literal opening and closing
    delimiter match failed outright on CRLF, fell back to `{}`, and the
    widened audience was never checked. Must be CAUGHT, not silently
    accepted, and not spuriously rejected either."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc_stub(audience=["team"]))
    seen = {}

    def fake_admission(fm, *, owner):
        seen["fm"] = fm
        return library_client.AdmissionResult(may_land=True, errors=[])

    monkeypatch.setattr(library_client, "check_reference_admission", fake_admission)
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: None)
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, *, holder: _checkout_row())

    widened_body = _body_with_frontmatter({
        "id": DOC, "kind": "reference", "project": "proj", "title": "A Document",
        "status": "current", "topics": [], "audience": ["team", "architect", "operator"]},
    ).replace("\n", "\r\n")
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": widened_body})
    assert r.status_code == 200
    assert seen["fm"]["audience"] == ["team", "architect", "operator"], (
        "a \\r\\n-delimited body must be parsed, not treated as having no "
        "frontmatter at all")


def test_save_admission_sees_a_mixed_line_ending_body(client, monkeypatch):
    """A hand-assembled request can carry both `\\r\\n` and bare `\\n` in the
    same body (e.g. the frontmatter block typed with one convention, the
    prose body with another) — this must parse the same as a pure-`\\n`
    body, not fail closed on the mix."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc_stub())
    seen = {}

    def fake_admission(fm, *, owner):
        seen["fm"] = fm
        return library_client.AdmissionResult(may_land=True, errors=[])

    monkeypatch.setattr(library_client, "check_reference_admission", fake_admission)
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: None)
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, *, holder: _checkout_row())

    fm_block = yaml.safe_dump({
        "id": DOC, "kind": "reference", "project": "proj", "title": "Mixed Endings",
        "status": "current", "topics": [], "audience": ["team"]}).replace("\n", "\r\n")
    mixed_body = f"---\r\n{fm_block}---\n\ncontent with a\r\nmix of endings\n"
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": mixed_body})
    assert r.status_code == 200
    assert seen["fm"]["title"] == "Mixed Endings"


# --- admission frontmatter: value shape, not just key membership ------------
#
# `_admission_frontmatter` used to filter by key membership only and forward
# whatever type each present key held. `library_client.check_reference_
# admission` just JSON-serializes the result and POSTs it — no type checking
# anywhere on this path otherwise. `audience: team` (a bare string) parsed
# straight through and would have been forwarded exactly as written, same for
# `id: 12345` as an int (PR #210 round 4 security review).


_WELL_SHAPED_FM = {"id": DOC, "kind": "reference", "project": "proj", "title": "A Document",
                   "status": "current", "topics": ["ops"], "audience": ["team"]}


@pytest.mark.parametrize("field,bad_value", [
    ("id", 12345),
    ("kind", 12345),
    ("project", 12345),
    ("title", 12345),
    ("status", 12345),
    ("audience", "team"),
    ("topics", "ops"),
])
def test_save_fails_closed_when_a_field_has_the_wrong_type(client, monkeypatch, field, bad_value):
    """Each of the seven checked fields must fail closed (422), naming the
    field, rather than reach admission or commit with the wrong shape."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc_stub())
    admission_calls = []
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: admission_calls.append(fm))
    committed = []
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: committed.append(1))

    fm = dict(_WELL_SHAPED_FM)
    fm[field] = bad_value
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": _body_with_frontmatter(fm)})
    assert r.status_code == 422
    assert field in r.json()["error"]
    assert admission_calls == []
    assert committed == []


@pytest.mark.parametrize("field", ["audience", "topics"])
def test_save_fails_closed_when_a_list_field_contains_a_non_string_element(
        client, monkeypatch, field):
    """A list of the right container type with the wrong element type is the
    case a shallow `isinstance(value, list)` check misses — `audience:
    [team, 5]` must fail closed the same as `audience: team`."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc_stub())
    admission_calls = []
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: admission_calls.append(fm))
    committed = []
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: committed.append(1))

    fm = dict(_WELL_SHAPED_FM)
    fm[field] = ["team", 5]
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": _body_with_frontmatter(fm)})
    assert r.status_code == 422
    assert field in r.json()["error"]
    assert admission_calls == []
    assert committed == []


def test_save_admission_sees_well_shaped_values_unchanged(client, monkeypatch):
    """The type check must not reject or alter a well-shaped payload — string
    fields as strings, `audience`/`topics` as lists of strings — the same
    values that already reached admission before this round's fix."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc_stub())
    seen = {}

    def fake_admission(fm, *, owner):
        seen["fm"] = fm
        return library_client.AdmissionResult(may_land=True, errors=[])

    monkeypatch.setattr(library_client, "check_reference_admission", fake_admission)
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: None)
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, *, holder: _checkout_row())

    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": _body_with_frontmatter(_WELL_SHAPED_FM)})
    assert r.status_code == 200
    assert seen["fm"] == _WELL_SHAPED_FM


def test_save_forwards_an_explicitly_empty_frontmatter_mapping_unchanged(client, monkeypatch):
    """A body whose frontmatter block is a deliberate, well-formed empty
    mapping (`---\\n{}\\n---`) still forwards `{}` to admission unchanged —
    the security reviewer judged that the Library's call to make, not this
    repo's, and the new type check has no field to reject since none is
    present."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc_stub())
    seen = {}

    def fake_admission(fm, *, owner):
        seen["fm"] = fm
        return library_client.AdmissionResult(may_land=True, errors=[])

    monkeypatch.setattr(library_client, "check_reference_admission", fake_admission)
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: None)
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, *, holder: _checkout_row())

    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": "---\n{}\n---\n\ncontent\n"})
    assert r.status_code == 200
    assert seen["fm"] == {}


# --- the seam: what the editor loads is what the save route accepts ------------
#
# Every save test above builds its body through `_body_with_frontmatter`, so
# none ever fed the save route what the editor actually loads. The editor was
# seeded from `ReferenceDocument.body`, which the Library serves with its
# frontmatter stripped, and no document could be saved (the operator's first real save,
# 2026-09-18: 422, "could not find a frontmatter block to validate"). Both sides
# were individually correct. This covers the ROUTE half of that seam: checkout
# to save, with a document shaped the way the site really serves it. The fake
# `agentctl` answers with the stored file, so the frontmatter assertions below
# hold whatever the editor does with the response. The JS half, that the editor
# fills its textarea from `content` and not from the page, rests on
# `test_the_editor_fills_from_the_checkout_response_not_the_page` and
# `test_the_editor_payload_carries_the_doc_id_and_no_body` in
# `test_reference_doc_build.py`, which match the rendered source.


def _checkout_then_save(client, monkeypatch, stored_file, served_body, edit=lambda c: c):
    """Open `DOC` through the checkout route, apply `edit` to the content the
    editor is loaded with, and save it. Returns the save response, what reached
    admission, and the file `agentctl kb apply` was handed."""
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, *, audience: _doc_stub(body=served_body))
    written = {}
    posted = {}

    def fake_agentctl(agentctl, args, *, stdin=None):
        """What `agentctl` answers, at the layer `kb_commit` calls it."""
        import subprocess
        verb = args[1]
        if verb == "checkout":
            return subprocess.CompletedProcess(args, 0, stderr="", stdout=json.dumps(
                {"source_ref": "projects/proj/kind/doc-1.md", "exists": True,
                 "content": stored_file, "content_sha": _OPEN_SHA}))
        if verb == "apply":
            assert args[args.index("--base-sha") + 1] == posted["sha"]
            written["content"] = open(args[args.index("--content-file") + 1]).read()
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        assert verb == "release"
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    admitted = []
    monkeypatch.setattr(kb_commit, "read_document", _REAL_READ_DOCUMENT)
    monkeypatch.setattr(kb_commit, "_run", fake_agentctl)
    monkeypatch.setattr(kb_commit.paths, "resolve_tool", lambda name: None)
    monkeypatch.setattr(library_client, "checkout_reference_document",
                        lambda doc_id, *, holder, run_id=None: _checkout_row(holder=holder))
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: admitted.append(fm) or
                        library_client.AdmissionResult(may_land=True, errors=[]))
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, **k: _checkout_row())

    opened = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))
    assert opened.status_code == 200
    content = opened.json()["content"]
    posted["sha"] = opened.json()["content_sha"]
    assert content.startswith("---\n"), "the editor must open on the file, not the stripped body"
    assert content != served_body

    saved = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                        json={"content_sha": posted["sha"], "body": edit(content)})
    return saved, admitted, written


# The Library's view of the document: frontmatter stripped, a provenance quote first.
_SERVED_BODY = "> **Provenance:** migrated from the old wiki.\n\n# A Document\n\nBody text.\n"


def test_a_document_loaded_through_checkout_saves_unchanged(client, monkeypatch):
    assert not _SERVED_BODY.startswith("---")
    stored_file = ("---\nid: proj/kind/doc-1\ntitle: A Document\nstatus: current\n"
                   "audience:\n- team\n---\n\n" + _SERVED_BODY)

    # What the member submits is what the editor was loaded with, untouched.
    saved, admitted, written = _checkout_then_save(client, monkeypatch, stored_file, _SERVED_BODY)

    assert saved.status_code == 200, saved.text
    assert saved.json() == {"saved": True, "checked_in": True}
    # The frontmatter admission checked and the block that was committed are
    # both the submitted one.
    assert admitted == [{"id": DOC, "title": "A Document", "status": "current",
                         "audience": ["team"]}]
    assert written["content"] == stored_file


def test_an_edited_frontmatter_value_is_committed_and_every_other_key_survives(client, monkeypatch):
    """The KB-editor epic's frontmatter clause (issue_01M35KCCR4SCYKZ1A3T93N46HJ):
    the member changes one value in the document as checkout served it, and the
    committed file carries that value and is otherwise the stored file byte for
    byte. The stored file carries keys a hand-built body would not think to
    include (`owner`, `supersedes`, a nested `review` mapping, a comment), so a
    save path that rebuilt the block from the fields it knows would drop them."""
    stored_file = ("---\nid: proj/kind/doc-1\ntitle: A Document\nstatus: current\n"
                   "owner: operator@example.com\n"
                   "# kept verbatim: a comment the editor never renders as a field\n"
                   "supersedes:\n- proj/kind/doc-0\n"
                   "review:\n  cadence: quarterly\n  last: 2026-08-01\n"
                   "audience:\n- team\n---\n\n" + _SERVED_BODY)
    expected = stored_file.replace("\nstatus: current\n", "\nstatus: superseded\n", 1)
    assert expected != stored_file

    saved, admitted, written = _checkout_then_save(
        client, monkeypatch, stored_file, _SERVED_BODY,
        edit=lambda c: c.replace("\nstatus: current\n", "\nstatus: superseded\n", 1))

    assert saved.status_code == 200, saved.text
    assert written["content"] == expected
    # Admission checks the edited value. It sees only the fields
    # `_admission_frontmatter` forwards, so the survival of the rest is
    # asserted on the committed file above, where it matters.
    assert admitted == [{"id": DOC, "title": "A Document", "status": "superseded",
                         "audience": ["team"]}]


def _ready_for_the_real_commit(monkeypatch, run):
    """Everything `_ready_to_commit` sets up EXCEPT the commit: the real
    `kb_commit.commit_document` runs, with `kb_commit._run` (the one place it
    reaches agentctl) replaced by `run(agentctl, args)`, so a test can see
    exactly which agentctl calls a save makes."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: library_client.AdmissionResult(may_land=True, errors=[]))
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, **k: _checkout_row())
    monkeypatch.setattr(kb_commit.paths, "resolve_tool", lambda name: None)
    monkeypatch.setattr(kb_commit, "_run", run)


@pytest.mark.parametrize("payload", [
    {},                                             # an editor that never sent one
    {"content_sha": None},
    {"content_sha": ""},
    {"content_sha": "a" * 63},
    {"content_sha": "a" * 65},
    {"content_sha": "A" * 64},
    {"content_sha": "a" * 64 + "\n"},
    {"content_sha": "--allow-overwrite"},
    {"content_sha": ["a" * 64]},
], ids=repr)
def test_a_save_without_a_well_formed_open_time_sha_is_refused_before_agentctl(
        client, monkeypatch, payload):
    """`content_sha` is client-supplied and becomes an argv element of `kb
    apply`, and it is the only thing that lets that call refuse a stale
    editor. A save without a plain 64-digit hex digest is refused, and the
    assertion is that NO subprocess ran, not merely that the status was 4xx."""
    ran = []
    _ready_for_the_real_commit(
        monkeypatch, lambda agentctl, args: ran.append(args) or pytest.fail("agentctl was invoked"))

    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"body": _SOME_VALID_BODY, **payload})

    assert r.status_code == 400
    assert "reopen" in r.json()["error"]
    assert ran == []


def test_a_commit_that_landed_while_the_editor_was_open_makes_the_save_a_409(
        client, monkeypatch):
    """End to end through the real `kb_commit`: the editor opened at
    `_OPEN_SHA`, main has since moved to `moved`, and the in-request checkout
    therefore reports `moved`. The save must be refused and the member told to
    reopen, and must not write. A `--base-sha` read from that checkout would
    have passed the apply's own gate and erased the other commit."""
    import subprocess
    moved = "b" * 64
    calls = []

    def fake_run(agentctl, args, *, stdin=None):
        cmd = [agentctl, *args]
        calls.append(cmd)
        verb = cmd[2]
        if verb == "checkout":
            return subprocess.CompletedProcess(cmd, 0, stderr="", stdout=json.dumps(
                {"exists": True, "content": _SOME_VALID_BODY, "content_sha": moved}))
        if verb == "apply":
            base = cmd[cmd.index("--base-sha") + 1]
            return subprocess.CompletedProcess(cmd, 0 if base == moved else 5, stdout="",
                                               stderr=f"stale base; main holds {moved[:12]}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    _ready_for_the_real_commit(monkeypatch, fake_run)

    stale = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                        json={"content_sha": _OPEN_SHA, "body": _SOME_VALID_BODY})
    assert stale.status_code == 409
    assert "reopen" in stale.json()["error"]
    apply_cmd = next(c for c in calls if c[2] == "apply")
    assert apply_cmd[apply_cmd.index("--base-sha") + 1] == _OPEN_SHA

    # The same edit from an editor opened after that commit lands.
    fresh = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                        json={"content_sha": moved, "body": _SOME_VALID_BODY})
    assert fresh.status_code == 200, fresh.text


def test_a_save_seeded_from_the_stripped_body_is_still_refused(client, monkeypatch):
    """The other half of the seam, so the test above cannot pass by weakening
    the save route: what the editor used to submit is still refused."""
    served_body = "> **Provenance:** migrated from the old wiki.\n\n# A Document\n"
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": served_body})
    assert r.status_code == 422
    assert "frontmatter" in r.json()["error"]


# --- the blocking agentctl calls stay off the event loop ------------------------
#
# Orient runs one uvicorn worker, so a synchronous `subprocess.run` inside an
# `async def` handler stalls every request to the app for as long as it runs:
# `read_document` up to 60s (a `git fetch` on every call), `commit_document` up to
# three sequential 60s calls. The handlers dispatch them via `asyncio.to_thread`.
# This is the same source-text assertion `test_waiting_build.py` makes for the
# merge-queue warmer; a source check is weak, and it is the repo's check for this
# property. It goes vacuous if the call moves into a helper, so it would have to
# move with it.

@pytest.mark.parametrize("handler, blocking", [
    ("checkout_document", "read_document"),
    ("save_document", "commit_document"),
])
def test_blocking_agentctl_calls_are_dispatched_off_the_event_loop(handler, blocking):
    import inspect

    import kb_write_api

    src = inspect.getsource(getattr(kb_write_api, handler))
    assert f"asyncio.to_thread(kb_commit.{blocking}" in src, (
        f"{handler} must dispatch kb_commit.{blocking} via asyncio.to_thread")
    assert f"= kb_commit.{blocking}(" not in src
    assert f"    kb_commit.{blocking}(" not in src


# --- an unquoted date in the frontmatter --------------------------------------
#
# `yaml.safe_load` reads `last_reviewed: 2026-08-22` as a `datetime.date`, which
# `json.dumps` refuses, so every save of such a document 500'd (2026-09-18).

# Written as literal text, not through `yaml.safe_dump`, so the date stays
# unquoted exactly as it is in the corpus.
_DATED_FILE = ("---\nid: proj/kind/doc-1\nkind: reference\nproject: proj\n"
               "title: A Document\nstatus: current\ntopics: []\naudience:\n- team\n"
               "last_reviewed: 2026-08-22\n---\n\n# A Document\n\nBody text.\n")


def _json_admission(monkeypatch, sent):
    """Fake the Library at the HTTP seam, where the real `TypeError` came from:
    `_request` JSON-encodes `json_body`, which the fake does too."""
    def fake_request(path, *, json_body=None, **k):
        assert path == "/reference/admission"
        sent.append(json.loads(json.dumps(json_body))["frontmatter"])
        return {"may_land": True, "errors": []}

    monkeypatch.setattr(library_client, "_request", fake_request)


def test_a_document_with_an_unquoted_date_saves_and_is_committed_byte_identical(
        client, monkeypatch):
    """The payload carries a string; the committed bytes are the submitted
    ones, date still unquoted. Coercing the payload must not reach the file."""
    sent, committed = [], []
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    _json_admission(monkeypatch, sent)
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: committed.append(body))
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, **k: _checkout_row())

    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": _DATED_FILE})

    assert r.status_code == 200, r.text
    assert sent[0]["last_reviewed"] == "2026-08-22"
    assert committed == [_DATED_FILE]
    assert "last_reviewed: 2026-08-22\n" in committed[0]
    assert "'2026-08-22'" not in committed[0] and '"2026-08-22"' not in committed[0]


def test_a_save_names_the_checkout_holder_as_the_admission_owner(client, monkeypatch):
    """The Library's checkout clause refuses unless `owner` holds the live
    checkout, and it is becoming unconditional (issue_01M19QW42CFK7TDZCKWK18ECY5).
    So the request that reaches the Library carries the verified identity the
    checkout is held under — never a value from the request body, which here
    claims someone else."""
    bodies = []

    def fake_request(path, *, json_body=None, **k):
        assert path == "/reference/admission"
        bodies.append(json.loads(json.dumps(json_body)))
        return {"may_land": True, "errors": []}

    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "_request", fake_request)
    holders = []
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion:
                        holders.append(holder))
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, **k: _checkout_row())

    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": _DATED_FILE,
                          "owner": "someone-else@example.com"})

    assert r.status_code == 200, r.text
    assert len(bodies) == 1
    assert bodies[0]["owner"] == "mem@example.com"
    # agentctl's lease is taken under the same holder the Library recorded,
    # unchanged (agent-platform#889), so both locks name one person.
    assert holders == ["mem@example.com"]
    assert bodies[0]["frontmatter"]["id"] == "proj/kind/doc-1"


def test_a_save_whose_frontmatter_names_another_document_is_refused(client, monkeypatch):
    """The Library checks the checkout on `frontmatter["id"]`; the commit writes
    the route's document. A member holding BOTH checkouts could otherwise type
    the second document's id into the first and have the clause verify the
    wrong document, so the checkout row here answers held for either id, and
    the save must still be refused before admission or the commit runs."""
    calls = []
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "check_reference_admission",
                        lambda fm, *, owner: calls.append("admission"))
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: calls.append("commit"))

    other = _DATED_FILE.replace("id: proj/kind/doc-1", "id: proj/kind/doc-2")
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": other})

    assert r.status_code == 422
    assert "proj/kind/doc-1" in r.json()["error"]
    assert calls == []


def test_the_payload_conversion_leaves_the_parsed_frontmatter_alone():
    import datetime as dt

    import kb_write_api

    fm = kb_write_api._parse_frontmatter(_DATED_FILE)
    assert fm["last_reviewed"] == dt.date(2026, 8, 22)
    payload = kb_write_api._admission_frontmatter(fm)
    assert payload["last_reviewed"] == "2026-08-22"
    assert fm["last_reviewed"] == dt.date(2026, 8, 22), "the parsed value was mutated"
    json.dumps(payload)


@pytest.mark.parametrize("raw, expected", [
    ("2026-08-22", "2026-08-22"),
    ("2026-08-22T10:30:00", "2026-08-22T10:30:00"),
    ("2026-08-22T10:30:00Z", "2026-08-22T10:30:00+00:00"),
    ("'2026-08-22'", "2026-08-22"),
    ("last week", "last week"),
])
def test_last_reviewed_is_forwarded_as_json(raw, expected):
    import kb_write_api

    fm = kb_write_api._parse_frontmatter(f"---\nlast_reviewed: {raw}\n---\n\nx\n")
    payload = kb_write_api._admission_frontmatter(fm)
    assert payload["last_reviewed"] == expected
    json.dumps(payload)


# Anything but a date or a string. The corpus holds no other shape. A container
# built with an alias never gets this far (see `_ALIAS_FRONTMATTER`).
_NOT_A_DATE_OR_STRING = [
    "!!set {a, b}",              # a set
    "!!binary aGk=",             # bytes
    "{2026-08-22: x}",           # a mapping
    "[2026-08-22, 2026-09-01]",  # a list
    "42",
    "true",
    "~",                         # null
]


@pytest.mark.parametrize("raw", _NOT_A_DATE_OR_STRING)
def test_a_last_reviewed_that_is_not_a_date_or_string_is_a_422_naming_the_field(
        client, monkeypatch, raw):
    """Refused here rather than reaching `json.dumps` and surfacing as a 500."""
    reached = []
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "_request", lambda *a, **k: reached.append("admission"))
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: reached.append("commit"))
    body = f"---\nid: {DOC}\nlast_reviewed: {raw}\n---\n\nx\n"

    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": body})

    assert r.status_code == 422, r.text
    assert "'last_reviewed'" in r.json()["error"]
    assert reached == []


# Every alias shape the parser refuses. Each was, or would have been, an
# amplification: the first three re-expand on the way to `json.dumps`, the merge
# key doubles inside `yaml.safe_load` itself.
_ALIAS_FRONTMATTER = {
    "one alias": "x: &a hello\ny: *a\n",
    "a self-referential anchor": "last_reviewed: &a [*a]\n",
    "a merge key over an anchor": "base: &b {k: 1}\nx:\n  <<: *b\n",
    "a shared list, once per level": (
        "x0: &a0 [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]\n"
        + "".join(f"x{n}: &a{n} [" + ", ".join([f"*a{n - 1}"] * 12) + "]\n"
                  for n in range(1, 6))
        + "last_reviewed: *a5\n"),
    "topics fanned out from one string": (
        "x: &s " + "A" * 2000 + "\ntopics:\n" + "- *s\n" * 500),
    "merge-key doubling": (
        "a0: &a0 {k: 1}\n"
        + "".join(f"a{n}: &a{n} {{<<: [*a{n - 1}, *a{n - 1}]}}\n" for n in range(1, 40))),
}


@pytest.mark.parametrize("frontmatter", list(_ALIAS_FRONTMATTER.values()),
                         ids=list(_ALIAS_FRONTMATTER))
def test_a_frontmatter_alias_is_refused_at_parse_time_and_names_aliases(frontmatter):
    """Refused by the composer, before anything is built from a shared node.
    The doubling case is 39 levels: unrefused it would not return."""
    import kb_write_api

    with pytest.raises(kb_write_api.FrontmatterAliasError, match="aliases"):
        kb_write_api._parse_frontmatter(f"---\nid: {DOC}\n{frontmatter}---\n\nx\n")


@pytest.mark.parametrize("frontmatter", list(_ALIAS_FRONTMATTER.values()),
                         ids=list(_ALIAS_FRONTMATTER))
def test_a_frontmatter_alias_is_a_422_naming_aliases_and_reaches_nothing(
        client, monkeypatch, frontmatter):
    reached = []
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    monkeypatch.setattr(library_client, "_request", lambda *a, **k: reached.append("admission"))
    monkeypatch.setattr(kb_commit, "commit_document",
                        lambda doc_id, *, body, actor, holder, base_sha, assertion: reached.append("commit"))
    body = f"---\nid: {DOC}\n{frontmatter}---\n\nx\n"

    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": body})

    assert r.status_code == 422, r.text
    assert "aliases" in r.json()["error"]
    assert reached == []


def test_a_crlf_frontmatter_alias_is_refused_too():
    import kb_write_api

    with pytest.raises(kb_write_api.FrontmatterAliasError):
        kb_write_api._parse_frontmatter("---\r\nx: &a 1\r\ny: *a\r\n---\r\n\r\nx\r\n")


def test_an_anchor_nobody_aliases_and_an_inline_merge_still_parse():
    """The refusal is of sharing. An anchor with no alias, and a merge over an
    inline mapping, share nothing."""
    import kb_write_api

    fm = kb_write_api._parse_frontmatter(
        "---\nid: &a doc-1\nx:\n  <<: {k: 1}\n  j: 2\n---\n\nx\n")
    assert fm == {"id": "doc-1", "x": {"k": 1, "j": 2}}


def test_the_frontmatter_parse_contract_is_unchanged_for_everything_but_aliases():
    """`None` is no mapping found; `{}` is an explicitly empty mapping."""
    import kb_write_api

    assert kb_write_api._parse_frontmatter("no frontmatter\n") is None
    assert kb_write_api._parse_frontmatter("---\nid: x\n") is None
    assert kb_write_api._parse_frontmatter("---\n[unclosed\n---\n\nx\n") is None
    assert kb_write_api._parse_frontmatter("---\n- a\n- b\n---\n\nx\n") is None
    assert kb_write_api._parse_frontmatter("---\n{}\n---\n\nx\n") == {}
    assert kb_write_api._parse_frontmatter(
        "---\r\nid: x\r\naudience:\r\n- team\r\n---\r\n\r\nx\r\n"
    ) == {"id": "x", "audience": ["team"]}


def test_an_unquoted_date_in_a_string_field_is_still_a_422(client, monkeypatch):
    """`title: 2026-08-22` parses to a date, and the existing string check
    refuses it by name; the coercion is for `last_reviewed` only."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))
    body = f"---\nid: {DOC}\ntitle: 2026-08-22\n---\n\nx\n"
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": body})
    assert r.status_code == 422
    assert "'title'" in r.json()["error"]


def test_an_unexpected_error_in_a_route_leaves_a_traceback_in_stderr(
        client, monkeypatch, capsys):
    """Through a real route: the Library failing mid-save is a 500 with the
    generic body, and the cause is in the log."""
    monkeypatch.setattr(library_client, "get_reference_checkout",
                        lambda doc_id: _checkout_row(holder="mem@example.com"))

    def down(fm, *, owner):
        raise RuntimeError("library-exploded-4417")

    monkeypatch.setattr(library_client, "check_reference_admission", down)
    r = client.post(f"/api/kb/documents/{DOC}/save", headers=_human("mem@example.com"),
                    json={"content_sha": _OPEN_SHA, "body": _DATED_FILE})

    assert r.status_code == 500
    assert r.json() == {"error": "internal"}
    err = capsys.readouterr().err
    assert "RuntimeError: library-exploded-4417" in err
    assert f"POST /api/kb/documents/{DOC}/save" in err


# --- a member's own checkout ----------------------------------------------------


def _conflict_with(held_by):
    """A `checkout_reference_document` that always finds `held_by` holding the
    lock. Its signature is the real one, so a call that passes anything else
    fails here."""
    def fake_checkout(doc_id, *, holder, run_id=None):
        raise library_client.ReferenceCheckoutConflict(_checkout_row(holder=held_by))
    return fake_checkout


def test_a_checkout_the_member_already_holds_is_adopted_and_opens_the_editor(
        client, monkeypatch):
    """Their own lock, left by a save that failed (which keeps it held). The
    Library refuses the take; Orient refreshes it and answers as a fresh
    checkout would."""
    beats, reads = [], []
    monkeypatch.setattr(library_client, "checkout_reference_document",
                        _conflict_with("mem@example.com"))
    monkeypatch.setattr(library_client, "heartbeat_reference_checkout",
                        lambda doc_id, *, holder: beats.append((doc_id, holder))
                        or _checkout_row(holder=holder))
    monkeypatch.setattr(kb_commit, "read_document",
                        lambda doc_id, *, holder: reads.append((doc_id, holder))
                        or kb_commit.StoredDocument(content=_STORED_FILE,
                                                    content_sha="sha-at-open"))

    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == _STORED_FILE and body["content_sha"] == "sha-at-open"
    assert body["checkout"]["holder"] == "mem@example.com"
    assert beats == [(DOC, "mem@example.com")]
    assert reads == [(DOC, "mem@example.com")]


def test_a_checkout_held_by_someone_else_is_still_read_only(client, monkeypatch):
    beats, reads = [], []
    monkeypatch.setattr(library_client, "checkout_reference_document",
                        _conflict_with("operator@example.com"))
    monkeypatch.setattr(library_client, "heartbeat_reference_checkout",
                        lambda doc_id, *, holder: beats.append(holder))
    monkeypatch.setattr(kb_commit, "read_document",
                        lambda doc_id, *, holder: reads.append(doc_id))

    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))

    assert r.status_code == 409
    assert r.json()["error"] == "already checked out"
    assert r.json()["checkout"]["holder"] == "operator@example.com"
    assert "content" not in r.json()
    assert beats == [] and reads == [], "someone else's lock was touched or the file was read"


def test_a_holder_that_differs_only_in_case_is_not_adopted(client, monkeypatch):
    """Exact equality, as `save_document` compares: a near-match stays
    read-only rather than being guessed to be the member."""
    beats = []
    monkeypatch.setattr(library_client, "checkout_reference_document",
                        _conflict_with("Mem@Example.com"))
    monkeypatch.setattr(library_client, "heartbeat_reference_checkout",
                        lambda doc_id, *, holder: beats.append(holder))
    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))
    assert r.status_code == 409 and r.json()["checkout"]["holder"] == "Mem@Example.com"
    assert beats == []


def test_a_lock_that_cannot_be_refreshed_is_not_reported_as_opened(client, monkeypatch):
    """The heartbeat's 404 covers no live checkout, someone else's, and dead
    past grace. Nothing was refreshed, so no editor opens and no content is
    read, and the lock is not checked in on the way out."""
    reads, checkins = [], []
    monkeypatch.setattr(library_client, "checkout_reference_document",
                        _conflict_with("mem@example.com"))
    monkeypatch.setattr(library_client, "heartbeat_reference_checkout",
                        lambda doc_id, *, holder: None)
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, *, holder: checkins.append(holder))
    monkeypatch.setattr(kb_commit, "read_document",
                        lambda doc_id, *, holder: reads.append(doc_id))

    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))

    assert r.status_code == 409
    body = r.json()
    assert "content" not in body and "content_sha" not in body
    assert "could not be refreshed" in body["error"]
    assert "checkout" not in body, "a `checkout` key would render as 'Checked out by <holder>'"
    assert reads == [] and checkins == []


def test_adopting_a_lock_when_locking_is_off_answers_503(client, monkeypatch):
    monkeypatch.setattr(library_client, "checkout_reference_document",
                        _conflict_with("mem@example.com"))

    def off(doc_id, *, holder):
        raise library_client.ReferenceWritesNotEnabled("shadow")

    monkeypatch.setattr(library_client, "heartbeat_reference_checkout", off)
    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))
    assert r.status_code == 503


def test_an_adopted_lock_is_left_held_when_the_file_cannot_be_read(client, monkeypatch):
    """Unlike a lock this request just took, an adopted one is not ours to give
    back: a successful heartbeat shows it is live, so it may be another tab's,
    and checking it in would make that tab's next heartbeat or save a 409.
    Adopting again is idempotent, so keeping it costs the member nothing."""
    checkins = []
    monkeypatch.setattr(library_client, "checkout_reference_document",
                        _conflict_with("mem@example.com"))
    monkeypatch.setattr(library_client, "heartbeat_reference_checkout",
                        lambda doc_id, *, holder: _checkout_row(holder=holder))
    monkeypatch.setattr(library_client, "checkin_reference_document",
                        lambda doc_id, *, holder: checkins.append(holder))

    def unreadable(doc_id, *, holder):
        raise kb_commit.CommitFailed("could not fetch origin/main")

    monkeypatch.setattr(kb_commit, "read_document", unreadable)
    r = client.post(f"/api/kb/documents/{DOC}/checkout", headers=_human("mem@example.com"))
    assert r.status_code == 502
    assert checkins == [], "an adopted lock was checked in"
