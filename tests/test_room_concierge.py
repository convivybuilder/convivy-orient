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
    roles.write_text(json.dumps({"operator@example.com": {"role": "admin", "products": ["*"]}}))
    monkeypatch.setattr(authz, "ROLES", roles)
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": ["operator@example.com"]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)
    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({AGENT_TOKEN: {"name": "an-agent", "products": ["*"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)
    library_fake.patch(monkeypatch)
    _seed_docs()

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
