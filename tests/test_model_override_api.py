"""`POST /api/items/{id}/model` — the operator's model-override button
(issue_01M38WVA7XHEDN1NFNPP8KDM0C).

The gate is `approve_api`'s, so these follow `test_approve_api.py`: each check
is its own failure shape, and the refusal message is asserted where two checks
share a status. The Library call is stubbed; `set_item_model`'s request shape
and credential are pinned in `test_library_client.py`.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

import access_identity
import api_common
import authz
import library_client
import model_catalog
import server as srv

_ID = "issue_01TESTITEM"

_CATALOG = [
    model_catalog.Model(model="claude-opus-5-5", alias="opus", provider="anthropic"),
    model_catalog.Model(model="claude-sonnet-4-6", alias=None, provider="anthropic"),
]


def _item(product="convivy-orient", state="open", claim_expired=False, choice=None):
    return library_client.IssueRecord(
        id=_ID, url=f"/engineering/issues/{_ID}", product=product,
        title="A queued item", body="", type="task", state=state, severity="medium",
        needs="agent", maturity=None, parent_id=None, topics=[], refs={},
        specification=None, definition_of_done=None,
        resolution=None, resolved_by=None, outcome_note=None,
        claimed_by="someone" if state == "claimed" else None,
        claimed_at=None, claim_expires_at=None, claim_expired=claim_expired,
        resolved_at=None, created_by="ops-mcp", created_by_kind="agent",
        created_at=dt.datetime(2026, 9, 27, 12, 0, 0), run_id=None, park_reason=None,
        unpark_condition=None, parked_at=None, blocked=False, prerequisites=[],
        model_choice=choice)


@pytest.fixture
def client(tmp_path, monkeypatch):
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "adm@example.com": {"role": "admin", "products": ["*"]},
        "mem@example.com": {"role": "member", "products": ["*"]},
        "scoped@example.com": {"role": "admin", "products": ["vivy"]},
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
        "operator@example.com", "adm@example.com", "mem@example.com", "scoped@example.com"]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)
    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({"agent-secret": {"name": "an-agent", "products": ["*"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)
    monkeypatch.setattr(library_client, "get_issue_item", lambda _id: _item())
    monkeypatch.setattr(model_catalog, "load", lambda: list(_CATALOG))
    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def written(monkeypatch):
    """Record what reached `library_client.set_item_model`, and answer with the
    item carrying the choice the Library would now hold."""
    seen = {}

    def fake(issue_id, *, model, acting_human, reason=None):
        seen.update(issue_id=issue_id, model=model, acting_human=acting_human, reason=reason)
        choice = None if model is None else library_client.ModelChoice(
            model=model, reason=reason, set_by_client="convivy-orient",
            acting_human=acting_human, set_at=dt.datetime(2026, 9, 27, 19, 0, 0))
        return _item(choice=choice)

    monkeypatch.setattr(library_client, "set_item_model", fake)
    return seen


def _human(email, origin="http://testserver"):
    return {srv.EMAIL_HEADER: email, "Origin": origin}


def _post(client, headers, body=None):
    return client.post(f"/api/items/{_ID}/model", headers=headers,
                       json={"model": "claude-opus-5-5"} if body is None else body)


def test_unauthenticated_is_401(client, written):
    assert client.post(f"/api/items/{_ID}/model", json={"model": "m"}).status_code == 401
    assert not written


def test_an_agent_is_refused_even_holding_every_capability(client, monkeypatch, written):
    """The Bosun honours this choice on an autonomous dispatch, so an agent
    that could set it could lift its own roster pin. The agent role is widened
    to every capability, `item_model` included, and no Origin is sent: a kind
    check ordered below CSRF or the capability would answer with a different
    message, which is why the message is asserted and not only the 403."""
    every_cap = frozenset().union(*authz.ROLE_CAPS.values()) | {"item_model"}
    monkeypatch.setitem(authz.ROLE_CAPS, "agent", every_cap)
    r = _post(client, {"Authorization": "Bearer agent-secret"})
    assert r.status_code == 403, r.text
    assert "human" in r.json()["error"]
    assert not written


def test_a_cross_site_post_is_refused(client, written):
    r = _post(client, _human("operator@example.com", origin="https://evil.example"))
    assert r.status_code == 403, r.text
    assert "csrf" in r.json()["error"]
    assert not written


def test_an_admin_sets_a_model(client, written):
    r = _post(client, _human("operator@example.com"),
              {"model": "claude-opus-5-5", "reason": "needs frontier judgment"})
    assert r.status_code == 200, r.text
    assert written == {"issue_id": _ID, "model": "claude-opus-5-5",
                       "acting_human": "operator@example.com", "reason": "needs frontier judgment"}
    assert r.json()["model_choice"]["model"] == "claude-opus-5-5"


def test_an_admin_clears_a_model(client, written):
    r = _post(client, _human("operator@example.com"), {"model": None})
    assert r.status_code == 200, r.text
    assert written["model"] is None
    assert r.json()["model_choice"] is None


def test_acting_human_is_the_verified_email_not_a_body_value(client, written):
    """The body claims to be the operator while the request authenticates as a second
    administrator; the Library must record the second administrator."""
    r = _post(client, _human("adm@example.com"),
              {"model": "claude-opus-5-5", "acting_human": "operator@example.com"})
    assert r.status_code == 200, r.text
    assert written["acting_human"] == "adm@example.com"


def test_a_member_is_refused_with_404(client, written):
    """`item_model` is admin-only; a member is a full-tier human, and the 404
    does not disclose the route."""
    assert _post(client, _human("mem@example.com")).status_code == 404
    assert not written


def test_a_holder_scoped_to_another_product_is_refused(client, written):
    assert _post(client, _human("scoped@example.com")).status_code == 404
    assert not written


def test_the_same_scoped_holder_sets_a_model_in_their_own_product(client, written, monkeypatch):
    """The positive control for the test above: an unscoped `authorize` call
    refuses everyone, so the refusal alone cannot show the scoping works."""
    monkeypatch.setattr(library_client, "get_issue_item", lambda _id: _item(product="vivy"))
    assert _post(client, _human("scoped@example.com")).status_code == 200


def test_a_missing_item_is_404(client, written, monkeypatch):
    monkeypatch.setattr(library_client, "get_issue_item", lambda _id: None)
    assert _post(client, _human("operator@example.com")).status_code == 404
    assert not written


@pytest.mark.parametrize("state,expired", [("claimed", False), ("pr-open", False), ("fixed", False)])
def test_an_item_that_is_not_queued_is_refused(client, written, monkeypatch, state, expired):
    """A run already under way keeps its model, so a choice set on an item
    somebody holds would read as applied when it is not."""
    monkeypatch.setattr(library_client, "get_issue_item",
                        lambda _id: _item(state=state, claim_expired=expired))
    r = _post(client, _human("operator@example.com"))
    assert r.status_code == 409, r.text
    assert state in r.json()["error"]
    assert not written


def test_a_claimed_item_whose_claim_expired_is_queued(client, written, monkeypatch):
    monkeypatch.setattr(library_client, "get_issue_item",
                        lambda _id: _item(state="claimed", claim_expired=True))
    assert _post(client, _human("operator@example.com")).status_code == 200


@pytest.mark.parametrize("body", [{}, {"model": 5}, {"model": "m", "reason": 5}, []])
def test_a_malformed_body_is_400(client, written, body):
    assert _post(client, _human("operator@example.com"), body).status_code == 400
    assert not written


def test_a_library_refusal_comes_back_as_409_with_its_wording(client, monkeypatch):
    def refuse(issue_id, *, model, acting_human, reason=None):
        raise library_client.ItemModelRefused(409, "another holder's live lease covers the item")

    monkeypatch.setattr(library_client, "set_item_model", refuse)
    r = _post(client, _human("operator@example.com"))
    assert r.status_code == 409, r.text
    assert "lease" in r.json()["error"]


def test_a_library_fault_is_not_dressed_up_as_a_refusal(client, monkeypatch):
    def boom(issue_id, *, model, acting_human, reason=None):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "set_item_model", boom)
    r = _post(client, _human("operator@example.com"))
    assert r.status_code >= 500, r.text



# --- only a model agentctl dispatches (issue_01M3NWDHCDQK6PE9NEXFQ37G9K) -----


def test_a_listed_model_without_an_alias_is_accepted(client, written):
    r = _post(client, _human("operator@example.com"), {"model": "claude-sonnet-4-6"})
    assert r.status_code == 200, r.text
    assert written["model"] == "claude-sonnet-4-6"


@pytest.mark.parametrize("model", ["claude-opus", "opus", "CLAUDE-OPUS-5-5", " claude-opus-5-5"])
def test_a_model_agentctl_does_not_list_is_400(client, written, model):
    """`claude-opus` is what Deck's free-text field stored. An alias is refused
    too: the Library stores what it is given, and the Bosun reads a resolved id."""
    r = _post(client, _human("operator@example.com"), {"model": model})
    assert r.status_code == 400, r.text
    assert "not a model agentctl dispatches" in r.json()["error"]
    assert not written


@pytest.mark.parametrize("unavailable", [None, []])
def test_every_set_is_503_while_the_list_is_unavailable(client, written, monkeypatch, unavailable):
    monkeypatch.setattr(model_catalog, "load", lambda: unavailable)
    r = _post(client, _human("operator@example.com"), {"model": "claude-opus-5-5"})
    assert r.status_code == 503, r.text
    assert "unavailable" in r.json()["error"]
    assert not written


def test_clearing_needs_no_list(client, written, monkeypatch):
    monkeypatch.setattr(model_catalog, "load", lambda: None)
    r = _post(client, _human("operator@example.com"), {"model": None})
    assert r.status_code == 200, r.text
    assert written["model"] is None


def test_a_member_learns_nothing_about_the_list(client, written, monkeypatch):
    """The capability check runs before the list is read, so a refused caller
    gets the same 404 whether or not agentctl answers."""
    monkeypatch.setattr(model_catalog, "load", lambda: None)
    assert _post(client, _human("mem@example.com")).status_code == 404
