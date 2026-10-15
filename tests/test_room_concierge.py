"""Reading Room concierge tests (ARCH-357 fast-follow).

The load-bearing property is that the concierge's plan is a proposal, never an
authority: the server re-validates every doc_id against the member's live grant
before applying, so a plan referencing an out-of-grant doc (buggy or hostile)
refuses whole with no mutation, and a grant-widening ask is denied outright and
logged rather than escalated or widened in-loop (the decision plane it used to
escalate to is gone, 2026-08-18). Plus authz (human-only, room.request, CSRF),
and the concierge-disabled (no config) path. The model call is mocked — the suite
never hits the network.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import access_identity
import api_common
import authz
import concierge
import control_db
import library_client
import library_fake
import server as srv
from control_db import new_room_id, session
from models import ReadingRoom, RoomEntry, RoomRequest

AGENT_TOKEN = "agent-secret-xyz"
BODY_A = "AAA_vivy_contractor_AAA"
BODY_B = "BBB_vivy_team_only_BBB"


def _seed_docs():
    library_fake.seed_reference_doc(
        id="vivy/a", project="vivy", kind="runbook", title="Doc A",
        status="current", body=BODY_A, audience=["team", "contractor"])
    library_fake.seed_reference_doc(
        id="vivy/b", project="vivy", kind="runbook", title="Doc B",
        status="current", body=BODY_B, audience=["team"])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()

    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "full-vivy@example.com": {"role": "member", "products": ["vivy"]},
        "con@example.com": {"role": "contractor", "products": ["vivy"]},
    }))
    monkeypatch.setattr(authz, "ROLES", roles)
    tiers = tmp_path / "tiers.json"
    # con@example.com is deliberately absent: contractor-tier, not full-tier,
    # even though its role above holds room.request.
    tiers.write_text(json.dumps({"full": ["operator@example.com", "full-vivy@example.com"]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)
    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({AGENT_TOKEN: {"name": "an-agent", "products": ["*"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)
    library_fake.patch(monkeypatch)
    _seed_docs()
    library_fake.seed_person_scope("operator@example.com", {"*"})
    library_fake.seed_person_scope("full-vivy@example.com", {"vivy"})
    library_fake.seed_person_scope("con@example.com", {"vivy"})

    # A valid concierge config so config() is non-None (the model call itself is mocked).
    cfgfile = tmp_path / "concierge.json"
    cfgfile.write_text(json.dumps({"api_key": "test-key", "model": "claude-haiku-4-5"}))
    monkeypatch.setattr(concierge, "CONFIG", cfgfile)

    # operator: full-tier, room seeded with one entry (vivy/b in a "Docs" section).
    with session() as s:
        room = ReadingRoom(id=new_room_id(), email="operator@example.com", title="R",
                           landing_note="", created_by="operator@example.com")
        room.entries.append(RoomEntry(doc_id="vivy/b", section="Docs", position=0,
                                      source="provision", added_by="operator@example.com"))
        s.add(room)
        # full-vivy: full-tier, scoped to vivy — the item/signal grant CQ·149 adds.
        s.add(ReadingRoom(id=new_room_id(), email="full-vivy@example.com", title="R",
                          landing_note="", created_by="operator@example.com"))
        # con: contractor-tier, scoped to vivy by project but with no item/signal
        # half to its grant at all (CQ·147's rule, reused here).
        s.add(ReadingRoom(id=new_room_id(), email="con@example.com", title="R",
                          landing_note="", created_by="operator@example.com"))

    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


def _human(email="operator@example.com", origin="http://testserver", **extra):
    h = {srv.EMAIL_HEADER: email, "Origin": origin}
    h.update(extra)
    return h


def _plan(monkeypatch, plan):
    monkeypatch.setattr(concierge, "interpret", lambda *a, **k: plan)


def _entries():
    with session() as s:
        room = s.scalar(
            __import__("sqlalchemy").select(ReadingRoom).where(ReadingRoom.email == "operator@example.com"))
        return {e.doc_id for e in room.entries}


def _kinded_entries(email):
    with session() as s:
        room = s.scalar(
            __import__("sqlalchemy").select(ReadingRoom).where(ReadingRoom.email == email))
        return {(e.kind, e.doc_id) for e in room.entries}


def test_add_entitled_doc_applies(client, monkeypatch):
    _plan(monkeypatch, {"actions": [{"op": "add", "doc_id": "vivy/a", "section": "New"}],
                        "reply": "Added it.", "escalate": None})
    r = client.post("/api/room/requests", json={"request": "add doc a"}, headers=_human())
    assert r.status_code == 200 and r.json()["status"] == "applied"
    assert "vivy/a" in _entries()


def test_out_of_grant_action_refuses_whole_plan(client, monkeypatch):
    # A plan (buggy/hostile) referencing a doc the member isn't entitled to: the
    # server re-validates and refuses whole — nothing changes.
    _plan(monkeypatch, {"actions": [{"op": "add", "doc_id": "secret/financials"}],
                        "reply": "sure", "escalate": None})
    before = _entries()
    r = client.post("/api/room/requests", json={"request": "add the financials"}, headers=_human())
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert _entries() == before          # unchanged


def test_grant_widening_is_denied_and_logged(client, monkeypatch):
    """No decision plane to escalate to any more: an out-of-grant ask is denied
    outright, and the ask is recorded as a signal — in the Library now
    (2026-08-19), not a local table — so a human can find it later (the operator,
    2026-08-18: "deny by default, log the attempt")."""
    sent = {}

    def fake_create_signal(**kwargs):
        sent.update(kwargs)
        return "sig_fake"

    monkeypatch.setattr(library_client, "create_signal", fake_create_signal)
    _plan(monkeypatch, {"actions": [], "reply": "Sent to the operator.",
                        "escalate": {"reason": "not in grant", "wanted": "the vivy financials"}})
    r = client.post("/api/room/requests", json={"request": "add the vivy financials"}, headers=_human())
    assert r.status_code == 200
    assert r.json()["status"] == "denied"
    assert sent["kind"] == "room-grant-denied" and sent["source"] == "room"
    assert sent["actor"] == "operator@example.com" and sent["product"] == "shared"
    assert "vivy financials" in sent["body"]
    with session() as s:
        from sqlalchemy import select as _select
        reqs = s.scalars(_select(RoomRequest)).all()
        assert reqs[0].status == "denied"
        assert sent["refs"]["request_id"] == reqs[0].id
        assert sent["refs"]["room_id"] == reqs[0].room_id


def test_grant_widening_denial_does_not_echo_internals(client, monkeypatch):
    _plan(monkeypatch, {"actions": [], "reply": "Sent to the operator.",
                        "escalate": {"reason": "not in grant", "wanted": "the vivy financials"}})
    r = client.post("/api/room/requests", json={"request": "add the vivy financials"}, headers=_human())
    assert "entitled to see" in r.json()["reply"]
    assert "vivy financials" not in r.json()["reply"]


def test_mixed_valid_invalid_add_refuses_whole(client, monkeypatch):
    # One in-grant add + one out-of-grant add: the whole plan refuses and the
    # valid add is NOT partially applied (the two-pass invariant).
    _plan(monkeypatch, {"actions": [
        {"op": "add", "doc_id": "vivy/a"},
        {"op": "add", "doc_id": "secret/x"},
    ], "reply": "ok", "escalate": None})
    before = _entries()
    r = client.post("/api/room/requests", json={"request": "add both"}, headers=_human())
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert _entries() == before          # vivy/a NOT added — no partial mutation


def test_remove_works_when_doc_no_longer_entitled(client, monkeypatch):
    # A member can always remove an entry from their OWN room — even a doc no
    # longer in their grant (removal reduces exposure, never widens).
    from sqlalchemy import select as _select
    with session() as s:
        room = s.scalar(_select(ReadingRoom).where(ReadingRoom.email == "operator@example.com"))
        room.entries.append(RoomEntry(doc_id="orphan/doc", section="Docs", position=9,
                                      source="provision", added_by="operator@example.com"))
    _plan(monkeypatch, {"actions": [{"op": "remove", "doc_id": "orphan/doc"}],
                        "reply": "removed", "escalate": None})
    r = client.post("/api/room/requests", json={"request": "remove orphan"}, headers=_human())
    assert r.status_code == 200 and r.json()["status"] == "applied"
    assert "orphan/doc" not in _entries()


def test_remove_entitled_doc(client, monkeypatch):
    _plan(monkeypatch, {"actions": [{"op": "remove", "doc_id": "vivy/b"}],
                        "reply": "Removed.", "escalate": None})
    r = client.post("/api/room/requests", json={"request": "drop doc b"}, headers=_human())
    assert r.status_code == 200 and r.json()["status"] == "applied"
    assert "vivy/b" not in _entries()


def test_agent_token_cannot_edit_a_room(client):
    r = client.post("/api/room/requests", json={"request": "add doc a"},
                    headers={"Authorization": f"Bearer {AGENT_TOKEN}"})
    assert r.status_code == 403


def test_csrf_blocks_cross_origin(client, monkeypatch):
    _plan(monkeypatch, {"actions": [], "reply": "ok", "escalate": None})
    r = client.post("/api/room/requests", json={"request": "x"},
                    headers=_human(origin="http://evil.example"))
    assert r.status_code == 403


def test_no_config_disables_concierge(client, monkeypatch, tmp_path):
    monkeypatch.setattr(concierge, "CONFIG", tmp_path / "does-not-exist.json")
    r = client.post("/api/room/requests", json={"request": "x"}, headers=_human())
    assert r.status_code == 503


def test_model_failure_records_failed(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(concierge, "interpret", boom)
    r = client.post("/api/room/requests", json={"request": "x"}, headers=_human())
    assert r.status_code == 502 and r.json()["status"] == "failed"


# --- item and signal references (CQ·149, E22) ---------------------------------
#
# The concierge's plan may now name a work item or an Archives entry, not only
# a KB document. The model's output is untrusted either way: every reference is
# re-validated server-side against the member's live, per-kind grant (CQ·147)
# before it is applied, so an out-of-grant or nonexistent one is denied the same
# way a document outside the grant is denied, and logged as a grant-widening ask.


def _seed_item_and_signal():
    library_fake.seed_issue(id="issue_vivyone", product="vivy", title="Vivy work item")
    library_fake.seed_issue(id="issue_constanceone", product="constance",
                            title="Constance work item")
    library_fake.seed_signal(id="sig_vivyone", source="room", kind="note", actor="x",
                             ingested_by="x", product="vivy", body="a vivy signal")
    library_fake.seed_signal(id="sig_constanceone", source="room", kind="note", actor="x",
                             ingested_by="x", product="constance", body="a constance signal")


def test_add_in_grant_item_applies(client, monkeypatch):
    _seed_item_and_signal()
    _plan(monkeypatch, {"actions": [{"op": "add", "doc_id": "issue_vivyone", "kind": "item"}],
                        "reply": "Added it.", "escalate": None})
    r = client.post("/api/room/requests", json={"request": "add the vivy work item"},
                    headers=_human("full-vivy@example.com"))
    assert r.status_code == 200 and r.json()["status"] == "applied"
    assert ("item", "issue_vivyone") in _kinded_entries("full-vivy@example.com")


def test_add_in_grant_signal_applies(client, monkeypatch):
    _seed_item_and_signal()
    _plan(monkeypatch, {"actions": [{"op": "add", "doc_id": "sig_vivyone", "kind": "signal"}],
                        "reply": "Added it.", "escalate": None})
    r = client.post("/api/room/requests", json={"request": "add the vivy signal"},
                    headers=_human("full-vivy@example.com"))
    assert r.status_code == 200 and r.json()["status"] == "applied"
    assert ("signal", "sig_vivyone") in _kinded_entries("full-vivy@example.com")


def test_out_of_grant_item_denied_and_logged(client, monkeypatch):
    _seed_item_and_signal()
    sent = {}
    monkeypatch.setattr(library_client, "create_signal", lambda **kw: sent.update(kw) or "sig_fake")
    before = _kinded_entries("full-vivy@example.com")
    _plan(monkeypatch, {"actions": [{"op": "add", "doc_id": "issue_constanceone", "kind": "item"}],
                        "reply": "sure", "escalate": None})
    r = client.post("/api/room/requests", json={"request": "add the constance item"},
                    headers=_human("full-vivy@example.com"))
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert _kinded_entries("full-vivy@example.com") == before
    assert sent["kind"] == "room-grant-denied" and sent["actor"] == "full-vivy@example.com"
    assert "item:issue_constanceone" in sent["body"]


def test_nonexistent_item_denied_identically_to_out_of_grant(client, monkeypatch):
    """The DoD clause: a missing reference and an out-of-grant one must be
    denied the same way, so a member cannot tell a rejected id is real."""
    _seed_item_and_signal()
    _plan(monkeypatch, {"actions": [{"op": "add", "doc_id": "issue_does_not_exist", "kind": "item"}],
                        "reply": "sure", "escalate": None})
    missing = client.post("/api/room/requests", json={"request": "add it"},
                          headers=_human("full-vivy@example.com"))
    _plan(monkeypatch, {"actions": [{"op": "add", "doc_id": "issue_constanceone", "kind": "item"}],
                        "reply": "sure", "escalate": None})
    out_of_scope = client.post("/api/room/requests", json={"request": "add it"},
                               headers=_human("full-vivy@example.com"))
    assert missing.status_code == out_of_scope.status_code == 200
    assert missing.json()["status"] == out_of_scope.json()["status"] == "failed"
    assert missing.json()["reply"] == out_of_scope.json()["reply"]


def test_out_of_grant_signal_denied_and_logged(client, monkeypatch):
    _seed_item_and_signal()
    sent = {}
    monkeypatch.setattr(library_client, "create_signal", lambda **kw: sent.update(kw) or "sig_fake")
    before = _kinded_entries("full-vivy@example.com")
    _plan(monkeypatch, {"actions": [{"op": "add", "doc_id": "sig_constanceone", "kind": "signal"}],
                        "reply": "sure", "escalate": None})
    r = client.post("/api/room/requests", json={"request": "add the constance signal"},
                    headers=_human("full-vivy@example.com"))
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert _kinded_entries("full-vivy@example.com") == before
    assert sent["kind"] == "room-grant-denied"
    assert "signal:sig_constanceone" in sent["body"]


def test_contractor_item_request_denied(client, monkeypatch):
    """con@example.com is contractor-tier, scoped to vivy by project — the item
    below is genuinely in scope by project and still refused, because a
    contractor's grant has no item/signal half at all (CQ·147's rule)."""
    _seed_item_and_signal()
    before = _kinded_entries("con@example.com")
    _plan(monkeypatch, {"actions": [{"op": "add", "doc_id": "issue_vivyone", "kind": "item"}],
                        "reply": "sure", "escalate": None})
    r = client.post("/api/room/requests", json={"request": "add the vivy item"},
                    headers=_human("con@example.com"))
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert _kinded_entries("con@example.com") == before


def test_contractor_signal_request_denied(client, monkeypatch):
    _seed_item_and_signal()
    before = _kinded_entries("con@example.com")
    _plan(monkeypatch, {"actions": [{"op": "add", "doc_id": "sig_vivyone", "kind": "signal"}],
                        "reply": "sure", "escalate": None})
    r = client.post("/api/room/requests", json={"request": "add the vivy signal"},
                    headers=_human("con@example.com"))
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert _kinded_entries("con@example.com") == before


def test_remove_item_no_longer_in_scope_still_works(client, monkeypatch):
    # Removal never widens access, for an item exactly as for a doc
    # (test_remove_works_when_doc_no_longer_entitled above).
    with session() as s:
        from sqlalchemy import select as _select
        room = s.scalar(_select(ReadingRoom).where(ReadingRoom.email == "full-vivy@example.com"))
        room.entries.append(RoomEntry(kind="item", doc_id="issue_orphan", section="",
                                      position=0, source="provision",
                                      added_by="full-vivy@example.com"))
    _plan(monkeypatch, {"actions": [{"op": "remove", "doc_id": "issue_orphan", "kind": "item"}],
                        "reply": "removed", "escalate": None})
    r = client.post("/api/room/requests", json={"request": "remove it"},
                    headers=_human("full-vivy@example.com"))
    assert r.status_code == 200 and r.json()["status"] == "applied"
    assert ("item", "issue_orphan") not in _kinded_entries("full-vivy@example.com")


def test_unknown_kind_refuses_whole_plan(client, monkeypatch):
    _plan(monkeypatch, {"actions": [{"op": "add", "doc_id": "issue_vivyone", "kind": "bogus"}],
                        "reply": "sure", "escalate": None})
    before = _kinded_entries("full-vivy@example.com")
    r = client.post("/api/room/requests", json={"request": "add it"},
                    headers=_human("full-vivy@example.com"))
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert _kinded_entries("full-vivy@example.com") == before
