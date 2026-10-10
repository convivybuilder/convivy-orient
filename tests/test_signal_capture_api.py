"""Manual signal capture proxy tests (S2, ARCH-349; issue_01M08YKNV185DFJ6832GV8ZY22).

Ported from the deleted signal_api.py's own create-signal suite. Which
credential a capture goes out under is now decided by whether a person made it
(issue_01M0R0CDCAG6SMC2X8ZTTGY0GR), and that decides two things at once, so
the suite pins both: whether the person is RECORDED in `acting_human`, and
whether customer-class capture is REACHABLE at all.

A human's capture carries the verified email, which puts it on Orient's
approved-client credential — the same one the ack pair uses. That credential
holds signal.create_customer, so customer-class capture works again for a role
that holds signal_customer in Orient's own roles.json, and Orient's own gate is
what decides it. Between 2026-08-19 and now it did not: the write went out
under Orient's shared agent credential, whose Library role never holds that
capability, so customer-class refused universally no matter what the human's
role said.

An agent's capture carries no person and keeps the narrow agent credential, so
customer-class from an agent still refuses. That split is the substance of the
suite below.
"""
from __future__ import annotations

import json
import urllib.error

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
CONSTANCE_TOKEN = "tok-agent-constance"
SYSTEM_TOKEN = "tok-system-capture"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    library_fake.patch(monkeypatch)
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "mem@example.com": {"role": "member", "products": ["vivy"]},
        "con@example.com": {"role": "contractor", "products": ["vivy"]},
    }))
    monkeypatch.setattr(authz, "ROLES", roles)
    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({
        AGENT_TOKEN: {"name": "agent", "products": ["vivy"]},
        CONSTANCE_TOKEN: {"name": "constance-agent", "products": ["constance"]},
        SYSTEM_TOKEN: {"name": "system-capture", "products": ["*"], "kind": "system"},
    }))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)
    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


def _human(email="operator@example.com", origin="http://testserver"):
    return {srv.EMAIL_HEADER: email, "Origin": origin}


def _agent(token=AGENT_TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _post(client, headers, **fields):
    body = {"source": "github_review", "kind": "review_comment", "actor": "reviewer",
            "product": "vivy", "body": "looks good"}
    body.update(fields)
    return client.post("/api/signals", json=body, headers=headers)


def test_agent_creates_internal_signal(client):
    r = _post(client, _agent())
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["id"].startswith("sig_")
    assert library_fake._SIGNALS[d["id"]]["privacy_class"] == "internal"
    assert library_fake._SIGNALS[d["id"]]["product"] == "vivy"


def test_unauthenticated_is_401(client):
    r = client.post("/api/signals", json={"source": "x", "kind": "y", "actor": "z", "product": "vivy"})
    assert r.status_code == 401


def test_missing_required_fields_400(client):
    r = client.post("/api/signals", json={"source": "github_review"}, headers=_agent())
    assert r.status_code == 400


def test_invalid_privacy_class_400(client):
    r = _post(client, _agent(), privacy_class="secret")
    assert r.status_code == 400


def test_agent_writes_need_no_origin_but_human_does(client):
    # bearer-authed agents are CSRF-exempt; a cookie-authed human needs same-origin
    assert _post(client, _agent()).status_code == 201
    r = client.post("/api/signals",
                    json={"source": "note", "kind": "market", "actor": "operator@example.com", "product": "vivy"},
                    headers={srv.EMAIL_HEADER: "operator@example.com"})  # no Origin
    assert r.status_code == 403


def test_human_capture_works_with_origin(client):
    r = _post(client, _human("mem@example.com"))
    assert r.status_code == 201


def test_agent_out_of_scope_product_is_forbidden(client):
    r = _post(client, _agent(), product="constance")  # agent token is scoped to vivy
    assert r.status_code == 403


def test_a_role_lacking_signal_write_is_denied_even_for_internal_capture(client):
    """The base gate, not just the customer-class one: `contractor` holds no
    `signal_read`/`signal_write` at all, so it must not clear even a plain
    internal-class capture. Mirrors signal_ack_api's equivalent coverage of
    its own base authz check (security review, PR #179 round 3)."""
    r = _post(client, _human("con@example.com"))
    assert r.status_code == 403


# --- who made the capture, and the privacy-class wall that follows from it ----
#
# The two are one question. A capture a person made goes out under the
# approved-client credential so the Library will record that person; that
# credential is `system` plus the human-reserved capabilities, so it also holds
# signal.create_customer. A capture an agent made goes out under the agent
# credential, which holds neither. So "is the person recorded" and "is
# customer-class reachable" have the same answer on every call, and the tests
# below pin them together rather than as two unrelated facts.


def _row(sid):
    return library_fake._SIGNALS[sid]


def test_a_human_capture_records_the_person_who_made_it(client):
    """The column the Library added for exactly this. Without it the row's
    `ingested_by` says Orient and the person is gone — which is what every
    capture through this proxy looked like until now."""
    r = _post(client, _human("mem@example.com"))
    assert r.status_code == 201, r.text
    assert _row(r.json()["id"])["acting_human"] == "mem@example.com"


def test_an_agent_capture_records_no_person_rather_than_its_own_name(client):
    """An agent filing what it observed is acting on its own, and `ident.actor`
    for an agent is its TOKEN NAME. Writing that into `acting_human` would
    record the client as the person — the fallback the Library's own
    `attested_human` names as the thing that must not happen."""
    r = _post(client, _agent())
    assert r.status_code == 201, r.text
    assert _row(r.json()["id"])["acting_human"] is None


def test_a_caller_supplied_acting_human_is_refused_rather_than_dropped(client):
    """The attestation is derived from the verified identity, never read off the
    body. A bearer-authed agent could otherwise name any person as the actor,
    and the column would then assert whatever the caller typed.

    Refused rather than silently ignored, so a caller that sent one learns it
    was not recorded instead of believing it was."""
    for headers in (_agent(), _human("operator@example.com")):
        r = _post(client, headers, acting_human="operator@example.com")
        assert r.status_code == 400, r.text
        assert "acting_human" in r.json()["error"]


def test_agent_cannot_create_customer_signal(client):
    r = _post(client, _agent(), privacy_class="customer", kind="inbound_email")
    assert r.status_code == 403


def test_customer_class_capture_works_again_for_a_capable_role(client):
    """operator@example.com holds signal_customer in Orient's own roles.json, and
    that is once more the gate that decides this. From 2026-08-19 until now it
    was not enough — the write went out under the shared agent credential and
    the Library refused privacy_class=customer regardless of who asked Orient.
    Carrying the person flips the credential to the approved-client one, which
    holds signal.create_customer, so the capability is reachable again and the
    row records who made it."""
    r = _post(client, _human("operator@example.com"), privacy_class="customer",
             source="email", kind="inbound_email", actor="alice@bigco.example",
             body="loved the demo, when can we start a pilot?")
    assert r.status_code == 201, r.text
    row = _row(r.json()["id"])
    assert row["privacy_class"] == "customer"
    assert row["acting_human"] == "operator@example.com"


def test_a_human_without_signal_customer_is_still_refused_customer_class(
        client, monkeypatch):
    """ORIENT'S OWN GATE IS NOW THE WALL, so it needs its own test. While the
    Library refused every customer-class capture through this proxy, this
    `authorize` call was checking into a closed door and a test could not tell
    a working gate from a missing one. Now it is the only thing between a human
    with signal_write and a customer-class row.

    No shipped role holds signal_write WITHOUT signal_customer, so the role is
    constructed here rather than borrowed — the point is the gate, and pinning
    it to today's role table would make this test go quiet the day a
    capture-only role is added, which is exactly when it would matter."""
    monkeypatch.setitem(authz.ROLE_CAPS, "capturer",
                        frozenset({"read", "signal_read", "signal_write"}))
    authz.ROLES.write_text(json.dumps(
        {"cap@example.com": {"role": "capturer", "products": ["vivy"]}}))

    ok = _post(client, _human("cap@example.com"))
    assert ok.status_code == 201, ok.text

    r = _post(client, _human("cap@example.com"), privacy_class="customer",
             source="email", kind="inbound_email", actor="alice@bigco.example")
    assert r.status_code == 403
    assert "signal_customer" in r.json()["error"]


def test_an_attestation_refusal_reads_as_a_deployment_fault_not_a_permission_one(
        client, monkeypatch):
    """A 403 on the attested path cannot be the caller's fault: Orient's own
    gates have already passed and the credential carrying it holds
    signal.create_customer. It means the Library rejected Orient's
    approved-client credential — a stale or unregistered token. Answering 403
    would send whoever hit it to check roles.json, where there is nothing to
    find."""
    def refuse(**kwargs):
        assert kwargs["acting_human"] == "operator@example.com"
        raise urllib.error.HTTPError(None, 403, "acting_human is recorded only "
                                     "for an approved-client credential", None, None)

    monkeypatch.setattr(library_client, "create_signal", refuse)
    r = _post(client, _human("operator@example.com"))
    assert r.status_code == 502, r.text
    assert "approved-client" in r.json()["error"]


def test_a_system_token_still_cannot_create_customer_signal(client):
    """A system-kind token used to carry the capability that let a poller's
    customer-class captures clear Orient's own gate. That token still
    resolves as `system` in Orient's own authz.py, but the Library-side
    capability this now depends on lives on a different credential entirely
    (Orient's own agent token, not this caller's kind) — so kind=system buys
    nothing here any more, which is worth pinning explicitly since it is
    exactly the kind of silent capability loss a poller would only discover
    when a real customer capture failed."""
    r = _post(client, _agent(SYSTEM_TOKEN), privacy_class="customer",
             source="manual", kind="note", actor="customer@client.example", product="commercial")
    assert r.status_code == 403


# --- dedup, ported from the deleted suite's no-leak rule -----------------------


def test_dedup_key_collision_is_409(client, monkeypatch):
    def fake_create_signal(**kwargs):
        import urllib.error
        raise urllib.error.HTTPError(None, 409, "dedup_key already in use", None, None)
    import library_client
    monkeypatch.setattr(library_client, "create_signal", fake_create_signal)
    r = _post(client, _agent(), dedup_key="capture:m-e2e")
    assert r.status_code == 409


def test_dedup_key_retry_lands_idempotent_not_error(client):
    """The property both github_capture.py's overlap-window poller and the
    capture form's own double-submit guard depend on: a resubmit of the same
    dedup_key is SUCCESS (200, the existing row, idempotent=true), not an
    error — a round of review caught this proxy hard-erroring on every
    collision, which would have broken both callers."""
    first = _post(client, _agent(), dedup_key="retry-1")
    assert first.status_code == 201
    first_id = first.json()["id"]
    assert first.json()["idempotent"] is False

    second = _post(client, _agent(), dedup_key="retry-1")
    assert second.status_code == 200
    assert second.json() == {"id": first_id, "idempotent": True}


def test_a_human_collision_into_an_out_of_scope_row_does_not_leak_the_id(client):
    """THE SAME NO-LEAK RULE, ON THE PATH THE WIDER CREDENTIAL OPENED (reviewer,
    PR #198). The test below pins this with two agent tokens; a human's capture
    now goes out under the approved-client credential, which the Library's
    `_can_return_existing` gate judges collisions against — so a human can match
    rows the agent credential would have refused outright, and this is the path
    where the re-check actually has to hold.

    `_ack_visible` is credential-agnostic, checking the ACTUAL row against this
    human's own role, so the mechanism is shared rather than duplicated. Pinning
    the human path anyway, because "the mechanism is shared" is a fact about
    today's code and this is the scenario the change makes reachable."""
    created = _post(client, _agent(CONSTANCE_TOKEN), product="constance",
                    dedup_key="human-probe")
    assert created.status_code == 201
    real_id = created.json()["id"]

    # mem@example.com is scoped to vivy and holds no grant on constance.
    probe = _post(client, _human("mem@example.com"), product="vivy",
                  dedup_key="human-probe")
    assert probe.status_code == 409
    assert real_id not in probe.text
    assert probe.json() == {"error": "dedup_key already in use"}


def test_a_human_collision_into_a_customer_row_they_cannot_read_is_refused(
        client, monkeypatch):
    """The other half, and the one only this change makes reachable. A human
    without signal_customer could not previously collide with a customer-class
    row at all — the agent credential's `_can_return_existing` refused it before
    Orient saw anything. Under the approved-client credential the Library will
    hand that row back, and `_ack_visible`'s `signal.read_customer` check is the
    only thing that stops its id going out."""
    monkeypatch.setitem(authz.ROLE_CAPS, "capturer",
                        frozenset({"read", "signal_read", "signal_write"}))
    authz.ROLES.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "cap@example.com": {"role": "capturer", "products": ["vivy"]},
    }))

    customer = _post(client, _human("operator@example.com"), privacy_class="customer",
                     product="vivy", source="email", kind="inbound_email",
                     actor="alice@bigco.example", dedup_key="customer-probe")
    assert customer.status_code == 201, customer.text
    real_id = customer.json()["id"]

    # Same product, so scope alone does not refuse it — the privacy class does.
    probe = _post(client, _human("cap@example.com"), product="vivy",
                  dedup_key="customer-probe")
    assert probe.status_code == 409
    assert real_id not in probe.text
    assert probe.json() == {"error": "dedup_key already in use"}


def test_dedup_key_collision_outside_the_callers_scope_does_not_leak_the_id(client):
    """Security review, PR #179 round 4: an idempotent hit is a match against
    Orient's whole shared store, not against this caller's own scope. A
    vivy-scoped token guessing (or reusing) a dedup_key that happens to match
    a constance signal must not learn that signal's real id — the same
    no-existence-leak property `_ack_visible` holds for the ack path,
    reused here rather than re-derived."""
    created = _post(client, _agent(CONSTANCE_TOKEN), product="constance", dedup_key="shared-key")
    assert created.status_code == 201
    real_id = created.json()["id"]

    probe = _post(client, _agent(), product="vivy", dedup_key="shared-key")  # vivy-scoped token
    assert probe.status_code == 409
    assert real_id not in probe.text
    assert probe.json() == {"error": "dedup_key already in use"}


# --- the access log (issue_01M1Z882KE85C88GBSB2CS1XJ5) -----------------------
#
# Re-homed from test_blog_api.py's now-deleted `/api/blog/corpus` route, which
# no longer resolves identity via a bearer token. The property under test is
# `api_common.resolve_identity`'s, not this route's, and this route is the one
# bearer-reachable GET-or-POST surface among the nine `resolve_identity`
# callers with its own test-client fixture and an agent token that clears the
# base gate.


def _log_lines(path_fragment):
    return [ln.split("\t") for ln in srv.ACCESS_LOG.read_text(encoding="utf-8").splitlines()
            if path_fragment in ln]


def test_an_agent_call_is_logged_as_the_agent_not_as_anonymous(client):
    """A bearer-authed call carries no CF Access email, so the naive middleware
    — one resolving identity itself, before the route — logs it as anonymous.
    The route's own resolution is what makes the agent name reachable."""
    r = _post(client, _agent())
    assert r.status_code == 201, r.text
    fields = _log_lines("/api/signals")
    assert len(fields) == 1
    assert fields[0][2] == "agent"
    assert fields[0][3] == "agent"             # kind, which for an agent equals its role
    assert fields[0][4] == "auth=bearer"


def test_an_unrecognised_bearer_token_is_distinguishable_from_no_credential(client):
    """A revoked or mistyped agent token and a request with no credential at all
    return the same body, which is right for the caller and useless for whoever
    has to work out why an integration stopped."""
    r = client.post("/api/signals",
                    json={"source": "x", "kind": "y", "actor": "z", "product": "vivy"},
                    headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401
    assert _log_lines("/api/signals")[0][4] == "auth=bearer-unknown"
