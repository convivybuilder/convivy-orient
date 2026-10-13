"""`POST /api/alerts-policy` — the dashboard alert-threshold write route
(issue_01M17W6XGXVHNDWCFRTQ38Y2RK).

Same gate shape as `test_policy_api.py`, minus its crew-scoping tests (this
route has no product dimension — `alerts.write` is a NON_SCOPED_ACTION, like
blog.write/kb.write) and its Library-translation tests (this route writes to
Orient's own control_db, not the Library, so there is no far-side credential
to translate a 403 for).
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import access_identity
import alert_policy_store
import api_common
import authz
import control_db
import library_client
import server as srv


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()

    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "member@example.com": {"role": "member", "products": ["*"]},
        "contractor@example.com": {"role": "contractor", "products": []},
    }))
    monkeypatch.setattr(authz, "ROLES", roles)
    # The project scope now comes from the Library (E19·1,
    # issue_01M35K54QHJYYPXWS28YWEHJP3), never from roles.json's `products` —
    # stubbed by re-reading the same roles.json file on every call, the way
    # the pre-E19·1 `human_identity` re-read it fresh.
    monkeypatch.setattr(
        library_client, "person_scope",
        lambda email: frozenset(
            json.loads(authz.ROLES.read_text()).get(email, {}).get("products", [])))

    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": [
        "operator@example.com", "member@example.com", "contractor@example.com"]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)

    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)

    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({"agent-secret": {"name": "an-agent", "products": ["*"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)

    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


def _human(email, origin="http://testserver"):
    return {srv.EMAIL_HEADER: email, "Origin": origin}


def _agent(token="agent-secret"):
    return {"Authorization": f"Bearer {token}"}


def _post(client, headers, **body):
    return client.post("/api/alerts-policy", json=body, headers=headers)


# --- unauthenticated -----------------------------------------------------------


def test_unauthenticated_is_401(client):
    r = client.post("/api/alerts-policy",
                    json={"key": "kb.recall", "good_min": 0.9, "warn_min": 0.7})
    assert r.status_code == 401


# --- non-human is refused unconditionally --------------------------------------


def test_an_agent_identity_is_refused_even_holding_every_capability(client, monkeypatch):
    every_cap = frozenset().union(*authz.ROLE_CAPS.values()) | {"alerts_policy_write"}
    monkeypatch.setitem(authz.ROLE_CAPS, "agent", every_cap)
    r = _post(client, _agent(), key="kb.recall", good_min=0.9, warn_min=0.7)
    assert r.status_code == 403, r.text
    assert "human" in r.json()["error"]


# --- CSRF, matching room_api.py's pattern --------------------------------------


def test_a_human_write_without_origin_is_refused(client):
    r = client.post("/api/alerts-policy",
                    json={"key": "kb.recall", "good_min": 0.9, "warn_min": 0.7},
                    headers={srv.EMAIL_HEADER: "operator@example.com"})  # no Origin
    assert r.status_code == 403
    assert "csrf" in r.json()["error"]


# --- alerts_policy_write is its own capability (admin-only; not a plain 404) --


def test_an_admin_can_write(client):
    r = _post(client, _human("operator@example.com"),
              key="code_health.ci_pass_rate", good_min=0.95, warn_min=0.8)
    assert r.status_code == 200, r.text
    assert r.json()["good_min"] == 0.95
    assert r.json()["updated_by"] == "operator@example.com"


def test_a_member_is_refused(client):
    """Asked plainly whether this should be open to members, the operator said
    "administrators for now" (2026-09-16) — like policy_write, alerts_policy_write
    is admin-only."""
    r = _post(client, _human("member@example.com"),
              key="kb.recall", good_min=0.9, warn_min=0.7)
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "forbidden"


def test_a_contractor_lacking_the_capability_gets_a_plain_403(client):
    r = _post(client, _human("contractor@example.com"),
              key="kb.recall", good_min=0.9, warn_min=0.7)
    assert r.status_code == 403, r.text
    assert r.json()["error"] == "forbidden"


# --- validation ------------------------------------------------------------------


def test_an_unknown_key_is_rejected(client):
    r = _post(client, _human("operator@example.com"), key="not-a-real-key", x=1)
    assert r.status_code == 400, r.text


def test_a_missing_field_is_rejected(client):
    r = _post(client, _human("operator@example.com"), key="kb.recall", good_min=0.9)
    assert r.status_code == 400, r.text


def test_an_out_of_order_threshold_is_rejected(client):
    r = _post(client, _human("operator@example.com"),
              key="kb.recall", good_min=0.5, warn_min=0.9)
    assert r.status_code == 400, r.text


def test_a_negative_run_count_is_rejected(client):
    r = _post(client, _human("operator@example.com"),
              key="spending.overage_runs", max_runs_in_overage=-1)
    assert r.status_code == 400, r.text


# --- the write reaches the one store every evaluator reads ---------------------


def test_a_successful_write_is_immediately_visible_through_get(client):
    r = _post(client, _human("operator@example.com"),
              key="spending.overage_runs", max_runs_in_overage=3)
    assert r.status_code == 200, r.text
    assert alert_policy_store.get("spending.overage_runs") == {"max_runs_in_overage": 3}
