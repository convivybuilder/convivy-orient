"""`POST /api/issues/{id}/resolve` and `POST /api/issues` — the issue queue's
resolve buttons and "Log an issue" panel (issue_01M3J6D09C38T4AT0SGZ20XHDW).

The gate is `approve_api`'s, so these follow `test_model_override_api.py`: each
check is its own failure shape, and the refusal message is asserted where two
checks share a status. The Library calls are stubbed; their request shapes and
credential are pinned in `test_library_client.py`.
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
import server as srv

_ID = "issue_01TESTITEM"


def _item(product="convivy-orient"):
    return library_client.IssueRecord(
        id=_ID, url=f"/engineering/issues/{_ID}", product=product,
        title="A queued item", body="", type="bug", state="open", severity="medium",
        needs="agent", maturity=None, parent_id=None, topics=[], refs={},
        specification=None, definition_of_done=None,
        resolution=None, resolved_by=None, outcome_note=None,
        claimed_by=None, claimed_at=None, claim_expires_at=None, claim_expired=False,
        resolved_at=None, created_by="ops-mcp", created_by_kind="agent",
        created_at=dt.datetime(2026, 9, 27, 12, 0, 0), run_id=None, park_reason=None,
        unpark_condition=None, parked_at=None, blocked=False, prerequisites=[],
        model_choice=None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "mem@example.com": {"role": "member", "products": ["*"]},
        "scoped@example.com": {"role": "member", "products": ["vivy"]},
        "ro@example.com": {"role": "observer", "products": ["*"]},
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
        "operator@example.com", "mem@example.com", "scoped@example.com", "ro@example.com"]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)
    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({"agent-secret": {"name": "an-agent", "products": ["*"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)
    monkeypatch.setattr(library_client, "get_issue_item", lambda _id: _item())
    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def lib(monkeypatch):
    """Record what reached the Library, answering as it would."""
    seen = {}

    def fake_resolve(issue_id, *, owner, resolution, outcome_note=None, refs=None):
        seen["resolve"] = dict(issue_id=issue_id, owner=owner, resolution=resolution,
                               outcome_note=outcome_note, refs=refs)
        return {"id": issue_id, "state": resolution}

    def fake_create(**kwargs):
        # Every keyword as passed, so a test can assert that `type` never is.
        seen["create"] = kwargs
        return {"id": "issue_NEW", "product": kwargs["product"]}

    monkeypatch.setattr(library_client, "resolve_item", fake_resolve)
    monkeypatch.setattr(library_client, "create_item", fake_create)
    return seen


def _human(email, origin="http://testserver"):
    return {srv.EMAIL_HEADER: email, "Origin": origin}


def _resolve(client, headers, body=None):
    return client.post(f"/api/issues/{_ID}/resolve", headers=headers,
                       json={"resolution": "fixed"} if body is None else body)


def _file(client, headers, body=None):
    return client.post("/api/issues", headers=headers, json=body if body is not None else {
        "product": "convivy-orient", "title": "A thing is broken", "body": "Details.",
        "type": "bug", "severity": "medium"})


# --- the routes are mounted again ---------------------------------------------


def test_both_routes_run_their_own_auth_check_rather_than_the_catch_all(client, lib):
    """The defect: both fell through to `handle_post` and answered 404
    unauthenticated. A mounted route answers 401 first."""
    assert client.post(f"/api/issues/{_ID}/resolve", json={}).status_code == 401
    assert client.post("/api/issues", json={}).status_code == 401
    assert not lib


# --- resolve ------------------------------------------------------------------


def test_a_member_resolves_as_themselves(client, lib):
    r = _resolve(client, _human("mem@example.com"),
                 {"resolution": "duplicate", "outcome_note": "same as issue_2",
                  "refs": {"duplicate_of": "issue_2"}, "owner": "someone-else"})
    assert r.status_code == 200, r.text
    assert lib["resolve"] == {"issue_id": _ID, "owner": "mem@example.com",
                              "resolution": "duplicate", "outcome_note": "same as issue_2",
                              "refs": {"duplicate_of": "issue_2"}}


def test_an_agent_is_refused_resolving_even_holding_every_capability(client, monkeypatch, lib):
    every_cap = frozenset().union(*authz.ROLE_CAPS.values())
    monkeypatch.setitem(authz.ROLE_CAPS, "agent", every_cap)
    r = _resolve(client, {"Authorization": "Bearer agent-secret"})
    assert r.status_code == 403, r.text
    assert "human" in r.json()["error"]
    assert not lib


def test_a_cross_site_resolve_is_refused(client, lib):
    r = _resolve(client, _human("operator@example.com", origin="https://evil.example"))
    assert r.status_code == 403, r.text
    assert "csrf" in r.json()["error"]
    assert not lib


def test_a_resolve_without_the_capability_is_404(client, lib):
    """An observer holds `read` and not `consume`, which `issue.resolve` needs."""
    assert _resolve(client, _human("ro@example.com")).status_code == 404
    assert not lib


def test_a_filing_without_the_capability_is_404(client, lib):
    assert _file(client, _human("ro@example.com")).status_code == 404
    assert not lib


def test_a_resolve_outside_scope_is_404(client, lib):
    assert _resolve(client, _human("scoped@example.com")).status_code == 404
    assert not lib


def test_the_same_scoped_member_resolves_in_their_own_product(client, lib, monkeypatch):
    """The positive control for the test above."""
    monkeypatch.setattr(library_client, "get_issue_item", lambda _id: _item(product="vivy"))
    assert _resolve(client, _human("scoped@example.com")).status_code == 200


def test_resolving_a_missing_item_is_404(client, lib, monkeypatch):
    monkeypatch.setattr(library_client, "get_issue_item", lambda _id: None)
    assert _resolve(client, _human("operator@example.com")).status_code == 404
    assert not lib


def test_an_out_of_scope_resolve_and_a_missing_item_answer_byte_identically(client, lib, monkeypatch):
    """E19·2 (issue_01M35K5AB4PRTJ71DMQW7Q4D3B): an item outside the caller's
    scope must read exactly like one that does not exist, never a distinct
    refusal that would confirm the item is there. `client`'s default
    `get_issue_item` already answers a `convivy-orient` item, out of
    `scoped@example.com`'s own `vivy` scope."""
    out_of_scope = _resolve(client, _human("scoped@example.com"))
    monkeypatch.setattr(library_client, "get_issue_item", lambda _id: None)
    missing = _resolve(client, _human("scoped@example.com"))
    assert out_of_scope.status_code == missing.status_code == 404
    assert out_of_scope.content == missing.content
    assert not lib


@pytest.mark.parametrize("body", [
    {}, {"resolution": ""}, {"resolution": 5}, {"resolution": "fixed", "outcome_note": 5},
    {"resolution": "fixed", "refs": "x"}, {"resolution": "fixed", "refs": {"other": "x"}},
    {"resolution": "fixed", "refs": {"duplicate_of": 5}}, [],
])
def test_a_malformed_resolve_is_400(client, lib, body):
    assert _resolve(client, _human("operator@example.com"), body).status_code == 400
    assert not lib


def test_a_library_refusal_keeps_its_status_and_wording(client, monkeypatch):
    def held(issue_id, **kw):
        raise library_client.ItemWriteRefused(409, "held by co-selector until 18:00")

    monkeypatch.setattr(library_client, "resolve_item", held)
    r = _resolve(client, _human("operator@example.com"))
    assert r.status_code == 409
    assert r.json()["error"] == "held by co-selector until 18:00"


def test_a_library_fault_is_not_dressed_up_as_a_refusal(client, monkeypatch):
    def boom(issue_id, **kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "resolve_item", boom)
    assert _resolve(client, _human("operator@example.com")).status_code >= 500


# --- file ---------------------------------------------------------------------


def test_a_member_files_an_issue_and_is_named_in_its_body(client, lib):
    """The filing below still carries `"type": "bug"`, as an older page would
    send it. A work item carries no type and the Library refuses the retired
    ones, so the type never reaches `create_item`."""
    r = _file(client, _human("mem@example.com"))
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "issue_NEW", "idempotent": False}
    assert lib["create"] == {
        "product": "convivy-orient", "title": "A thing is broken",
        "body": "Details.\n\n— filed by mem@example.com through team.convivy.com",
        "severity": "medium"}


def test_a_filing_with_no_body_still_names_the_person(client, lib):
    r = _file(client, _human("mem@example.com"),
              {"product": "convivy-orient", "title": "T", "severity": ""})
    assert r.status_code == 200, r.text
    assert lib["create"]["body"] == "— filed by mem@example.com through team.convivy.com"
    assert lib["create"]["severity"] is None


def test_the_named_person_is_the_verified_email_not_a_body_value(client, lib):
    r = _file(client, _human("mem@example.com"),
              {"product": "convivy-orient", "title": "T", "acting_human": "operator@example.com",
               "created_by": "operator@example.com"})
    assert r.status_code == 200, r.text
    assert "filed by mem@example.com" in lib["create"]["body"]
    assert "operator@example.com" not in lib["create"]["body"]


def test_an_agent_is_refused_filing(client, monkeypatch, lib):
    every_cap = frozenset().union(*authz.ROLE_CAPS.values())
    monkeypatch.setitem(authz.ROLE_CAPS, "agent", every_cap)
    r = _file(client, {"Authorization": "Bearer agent-secret"})
    assert r.status_code == 403, r.text
    assert "human" in r.json()["error"]
    assert not lib


def test_a_cross_site_filing_is_refused(client, lib):
    r = _file(client, _human("operator@example.com", origin="https://evil.example"))
    assert r.status_code == 403
    assert not lib


def test_a_filing_outside_scope_is_404(client, lib):
    assert _file(client, _human("scoped@example.com")).status_code == 404
    assert not lib


def test_the_same_scoped_member_files_in_their_own_product(client, lib):
    r = _file(client, _human("scoped@example.com"), {"product": "vivy", "title": "T"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("body", [
    {}, {"product": "convivy-orient"}, {"title": "T"}, {"product": " ", "title": "T"},
    {"product": "convivy-orient", "title": 5}, {"product": "convivy-orient", "title": "T",
                                                 "severity": 3}, [],
])
def test_a_malformed_filing_is_400(client, lib, body):
    assert _file(client, _human("operator@example.com"), body).status_code == 400
    assert not lib


def test_a_filing_refusal_keeps_its_status_and_wording(client, monkeypatch):
    def refuse(**kw):
        raise library_client.ItemWriteRefused(400, "unknown product 'nope'")

    monkeypatch.setattr(library_client, "create_item", refuse)
    r = _file(client, _human("operator@example.com"), {"product": "nope", "title": "T"})
    assert r.status_code == 400
    assert r.json()["error"] == "unknown product 'nope'"


# --- the panel offers only what the Library accepts ---------------------------


def test_the_log_panel_offers_no_type_and_exactly_the_librarys_severities():
    """A work item carries no type, and the Library refuses bug, nit,
    improvement and task on create (Library CQ·199), so the panel has no Type
    field and its script sends none. `ITEM_SEVERITIES` is {high, medium, low}."""
    import re as _re
    import issue_build
    html = issue_build._log_issue_form(["convivy-orient"])
    assert 'name="type"' not in html
    assert "form.elements.type" not in issue_build._JS
    sev_block = html[html.index('<select name="severity"'):]
    sev_block = sev_block[:sev_block.index("</select>")]
    assert set(_re.findall(r'<option value="([^"]+)"', sev_block)) == {"high", "medium", "low"}
