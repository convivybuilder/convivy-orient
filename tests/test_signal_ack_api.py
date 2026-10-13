"""Signal acknowledgment proxy tests (ARCH-362 Phase 4a; issue_01M08YKNV185DFJ6832GV8ZY22).

Two properties carry the design, ported from the deleted signal_api.py's own
suite along with the code:
  * PER-IDENTITY — one person clearing an alert must not clear anyone else's.
  * HUMAN-ONLY — an agent acking would let automation silence the queue.

Plus the visibility gate this proxy has to enforce itself now (`_ack_visible`):
the write goes out under Orient's approved-client credential, which carries
`signal.customer` at the Library (ADR-0038's `system ∪ {signal.ack,
decision.decide}`), so the per-human product/customer scoping that used to
come for free from a local DB row has to be checked here.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import access_identity
import api_common
import authz
import control_db
import library_client
import library_fake
import server as srv

AGENT_TOKEN = "tok-agent-vivy"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    library_fake.patch(monkeypatch)
    # Every built-in role that holds signal_read also holds signal_customer
    # (authz.ROLE_CAPS) — there is no shipped role that reads signals but not
    # customer-class ones, so the two probe tests below add a synthetic one to
    # exercise _ack_visible's second branch directly, rather than asserting a
    # split that doesn't exist in production today.
    monkeypatch.setitem(authz.ROLE_CAPS, "signal_reader_no_customer",
                        frozenset({"read", "signal_read", "room_request"}))
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "mem@example.com": {"role": "member", "products": ["vivy"]},
        "con@example.com": {"role": "contractor", "products": ["vivy"]},
        "narrow@example.com": {"role": "signal_reader_no_customer", "products": ["vivy"]},
    }))
    monkeypatch.setattr(authz, "ROLES", roles)
    # The project scope now comes from the Library (E19·1,
    # issue_01M35K54QHJYYPXWS28YWEHJP3), never from roles.json's `products` —
    # stubbed by re-reading the same roles.json file on every call, overriding
    # `library_fake.patch`'s own fake (which has no seed for these emails).
    monkeypatch.setattr(
        library_client, "person_scope",
        lambda email: frozenset(
            json.loads(authz.ROLES.read_text()).get(email, {}).get("products", [])))
    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({AGENT_TOKEN: {"name": "agent", "products": ["vivy"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)
    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


def _human(email="operator@example.com", origin="http://testserver"):
    return {srv.EMAIL_HEADER: email, "Origin": origin}


def _agent():
    return {"Authorization": f"Bearer {AGENT_TOKEN}"}


def _seed(**kw):
    base = dict(id=control_db.new_signal_id(), source="github", kind="review_comment",
                actor="reviewer", ingested_by="agent", product="vivy", body="looks good",
                privacy_class="internal")
    base.update(kw)
    return library_fake.seed_signal(**base)


def test_ack_is_idempotent_and_reversible(client):
    sid = _seed()
    for _ in range(2):
        r = client.post(f"/api/signals/{sid}/ack", headers=_human())
        assert r.status_code == 200 and r.json()["acknowledged"] is True
    # reversible: a misfired tap on a phone must be undoable, or the safe move
    # is never to ack and the state stops meaning anything
    for _ in range(2):
        r = client.delete(f"/api/signals/{sid}/ack", headers=_human())
        assert r.status_code == 200 and r.json()["acknowledged"] is False


def test_one_identity_ack_does_not_clear_another(client):
    """The load-bearing property. An infra alert is the operator's to clear; someone
    else acknowledging it must not clear theirs. A global ack would have made one
    person's reading silence everyone's queue."""
    sid = _seed()
    assert client.post(f"/api/signals/{sid}/ack", headers=_human("mem@example.com")).status_code == 200
    assert library_fake._ACKS == {(sid, "mem@example.com")}
    assert client.post(f"/api/signals/{sid}/ack", headers=_human("operator@example.com")).status_code == 200
    assert library_fake._ACKS == {(sid, "mem@example.com"), (sid, "operator@example.com")}


def test_an_agent_cannot_acknowledge(client):
    """Acknowledgment means a PERSON read it. An agent acking would let
    automation silence the queue, which inverts the point of the state."""
    sid = _seed()
    r = client.post(f"/api/signals/{sid}/ack", headers=_agent())
    assert r.status_code == 403
    assert "human" in r.text.lower()


def test_ack_requires_same_origin(client):
    sid = _seed()
    r = client.post(f"/api/signals/{sid}/ack", headers={srv.EMAIL_HEADER: "operator@example.com"})  # no Origin
    assert r.status_code == 403
    r = client.post(f"/api/signals/{sid}/ack", headers=_human(origin="http://evil.example"))
    assert r.status_code == 403


def test_ack_rejects_a_malformed_origin_instead_of_500ing(client):
    """`urlsplit` raises `ValueError: Invalid IPv6 URL` on an unbalanced
    bracket — `same_origin` is the CSRF check for every cookie-authed state
    change, and it is reading the client-controlled Origin header, so a
    malformed value here must fail closed (reject), not crash the endpoint."""
    sid = _seed()
    r = client.post(f"/api/signals/{sid}/ack", headers=_human(origin="http://[::1"))
    assert r.status_code == 403


def test_ack_on_an_unknown_signal_is_404_not_an_existence_oracle(client):
    r = client.post("/api/signals/sig_DOESNOTEXIST/ack", headers=_human())
    assert r.status_code == 404


def test_ack_refuses_a_signal_outside_the_actors_product_scope(client):
    """The gap this proxy closed (security review, PR #179): the write goes
    out under Orient's approved-client credential, which is broader than any
    one human's own role, so scoping the act to what THIS human can read is
    this file's job — not the Library's, and not free from a local DB row any
    more."""
    sid = _seed(product="constance")  # mem@ holds signal_read, but only for vivy
    r = client.post(f"/api/signals/{sid}/ack", headers=_human("mem@example.com"))
    assert r.status_code == 404
    assert library_fake._ACKS == set()


def test_ack_cannot_probe_a_customer_class_signal(client):
    """Acking must not become a side door around the customer wall. In
    production this refuses twice over: the Library's own agent-credential
    read (`library_client.get_signal`) already can't fetch a customer-class
    row, and `_ack_visible`'s own check would refuse it too — pinned
    separately below since the fake's default capability set means only the
    first layer is exercised here."""
    cid = _seed(privacy_class="customer")
    r = client.post(f"/api/signals/{cid}/ack", headers=_human("con@example.com"))
    assert r.status_code == 404, "customer-class row leaked its existence via ack"


def test_ack_visible_still_refuses_customer_class_if_the_librarys_wall_ever_widens(client, monkeypatch):
    """Defense in depth: even if Orient's own agent credential ever gained
    `signal.customer` at the Library (widening what `get_signal` can fetch),
    `_ack_visible`'s own privacy-class check must still refuse a human who
    lacks `signal_customer` in Orient's roles.json. This is the exact
    regression the security review flagged as invisible without a test."""
    monkeypatch.setattr(library_fake, "FAKE_CAPABILITIES", {"signal.read", "signal.customer"})
    cid = _seed(privacy_class="customer")
    r = client.post(f"/api/signals/{cid}/ack", headers=_human("narrow@example.com"))  # signal_read, no signal_customer
    assert r.status_code == 404


def test_a_role_holding_signal_customer_can_still_ack_a_customer_class_signal(client, monkeypatch):
    """The gate is scoped, not a blanket refusal of every customer-class row —
    an admin (signal_customer) can ack one once the Library actually serves it."""
    monkeypatch.setattr(library_fake, "FAKE_CAPABILITIES", {"signal.read", "signal.customer"})
    cid = _seed(privacy_class="customer")
    r = client.post(f"/api/signals/{cid}/ack", headers=_human("operator@example.com"))
    assert r.status_code == 200 and r.json()["acknowledged"] is True


def test_unauthenticated_ack_is_401(client):
    sid = _seed()
    assert client.post(f"/api/signals/{sid}/ack").status_code == 401


def test_unack_is_gated_the_same_way(client):
    """The DELETE route shares `_ack_visible` and the same human/CSRF checks;
    pinned independently rather than assumed symmetric with POST."""
    sid = _seed()
    assert client.post(f"/api/signals/{sid}/ack", headers=_human()).status_code == 200
    assert client.delete(f"/api/signals/{sid}/ack", headers=_agent()).status_code == 403
    r = client.delete(f"/api/signals/{sid}/ack", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 403  # no Origin
    assert client.delete("/api/signals/sig_DOESNOTEXIST/ack", headers=_human()).status_code == 404
    r = client.delete(f"/api/signals/{sid}/ack", headers=_human())
    assert r.status_code == 200 and r.json()["acknowledged"] is False
