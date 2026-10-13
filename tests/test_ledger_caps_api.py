"""`POST /api/ledgers/{project}/caps` — the Spending page's ledger-caps form
(issue_01M4BH8KSSMC8XBKB2C2CX9QTF).

The gate is `approve_api`'s, so these follow `test_model_override_api.py`: each
check is its own failure shape. The Library call is stubbed; `set_ledger_caps`'s
request shape and credential are pinned in `test_library_client.py`.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import access_identity
import api_common
import authz
import library_client
import server as srv

_PROJECT = "vivy"


@pytest.fixture
def client(tmp_path, monkeypatch):
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "mem@example.com": {"role": "member", "products": ["*"]},
        "scoped@example.com": {"role": "admin", "products": ["other-project"]},
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
        "operator@example.com", "mem@example.com", "scoped@example.com"]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)
    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({"agent-secret": {"name": "an-agent", "products": ["*"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)
    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def written(monkeypatch):
    """Record what reached `library_client.set_ledger_caps`, and answer with
    the Library's own response shape."""
    seen = {}

    def fake(scope_id, *, warn_usd, soft_cap_usd, hard_cap_usd, per_run_cap_usd, acting_human):
        seen.update(scope_id=scope_id, warn_usd=warn_usd, soft_cap_usd=soft_cap_usd,
                    hard_cap_usd=hard_cap_usd, per_run_cap_usd=per_run_cap_usd,
                    acting_human=acting_human)
        return {"id": "led_1", "scope_kind": "project", "scope_id": scope_id,
                "caps": {"warn_usd": warn_usd, "soft_cap_usd": soft_cap_usd,
                        "hard_cap_usd": hard_cap_usd, "per_run_cap_usd": per_run_cap_usd},
                "acting_human": acting_human, "attested_by": "convivy-orient"}

    monkeypatch.setattr(library_client, "set_ledger_caps", fake)
    return seen


_ALL_CAPS = {"warn_usd": 1.0, "soft_cap_usd": 2.0, "hard_cap_usd": 3.0, "per_run_cap_usd": 0.5}


def _human(email, origin="http://testserver"):
    return {srv.EMAIL_HEADER: email, "Origin": origin}


def _post(client, headers, body=None, project=_PROJECT):
    return client.post(f"/api/ledgers/{project}/caps", headers=headers,
                       json=_ALL_CAPS if body is None else body)


def test_unauthenticated_is_401(client, written):
    assert client.post(f"/api/ledgers/{_PROJECT}/caps", json=_ALL_CAPS).status_code == 401
    assert not written


def test_an_agent_is_refused_even_holding_every_capability(client, monkeypatch, written):
    """An agent that could raise its own project's ceiling could lift the
    very refusal `agentctl`'s per-run kill depends on. Widened to every
    capability and sent with no Origin, so the kind check — ordered first —
    is what is asserted, not a CSRF failure that would read differently."""
    every_cap = frozenset().union(*authz.ROLE_CAPS.values()) | {"ledger_caps_write"}
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


def test_an_admin_sets_all_four_caps(client, written):
    r = _post(client, _human("operator@example.com"))
    assert r.status_code == 200, r.text
    assert written == {"scope_id": _PROJECT, "warn_usd": 1.0, "soft_cap_usd": 2.0,
                       "hard_cap_usd": 3.0, "per_run_cap_usd": 0.5,
                       "acting_human": "operator@example.com"}
    assert r.json()["caps"]["hard_cap_usd"] == 3.0


def test_a_blank_field_is_sent_as_null_and_clears_that_cap(client, written):
    body = {**_ALL_CAPS, "soft_cap_usd": None}
    r = _post(client, _human("operator@example.com"), body)
    assert r.status_code == 200, r.text
    assert written["soft_cap_usd"] is None
    assert written["warn_usd"] == 1.0 and written["hard_cap_usd"] == 3.0


def test_acting_human_is_the_verified_email_not_a_body_value(client, written):
    body = {**_ALL_CAPS, "acting_human": "someone-else@example.com"}
    r = _post(client, _human("operator@example.com"), body)
    assert r.status_code == 200, r.text
    assert written["acting_human"] == "operator@example.com"


# --- a contractor-tier viewer and a viewer without the capability --------
#
# Both get neither the control nor a working route: the page's substitution
# reads false (asserted in test_server.py, where the page is rendered), and
# this route answers the same 404 as a missing project, naming nothing about
# why — approve_api's and model_override_api's own choice for a sensitive
# control.


def test_a_member_full_tier_but_lacking_the_capability_is_refused_with_404(client, written):
    assert _post(client, _human("mem@example.com")).status_code == 404
    assert not written


def test_a_contractor_tier_viewer_with_no_roles_entry_is_refused_with_404(client, written):
    """No roles.json entry at all, the default shape of a contractor-tier
    human: `human_identity` falls back to role=contractor, which holds
    neither `ledger_caps_write` nor any other write capability."""
    assert _post(client, _human("nobody@example.com")).status_code == 404
    assert not written


def test_a_holder_scoped_to_another_project_is_refused(client, written):
    assert _post(client, _human("scoped@example.com")).status_code == 404
    assert not written


def test_the_same_scoped_holder_sets_caps_in_their_own_project(client, written):
    """The positive control for the test above: an unscoped `authorize` call
    refuses everyone, so the refusal alone cannot show the scoping works."""
    r = _post(client, _human("scoped@example.com"), project="other-project")
    assert r.status_code == 200, r.text
    assert written["scope_id"] == "other-project"


@pytest.mark.parametrize("body", [{}, {"warn_usd": 1.0}, []])
def test_a_body_missing_a_key_is_400(client, written, body):
    """`PUT /ledgers` replaces the whole cap set, so a body missing one of
    the four keys is refused — sending it through would silently clear
    whatever it omits."""
    r = _post(client, _human("operator@example.com"), body)
    assert r.status_code == 400, r.text
    assert not written


def test_an_extra_key_is_ignored_like_every_other_write_route_here(client, written):
    r = _post(client, _human("operator@example.com"), {**_ALL_CAPS, "extra": 1})
    assert r.status_code == 200, r.text
    assert "extra" not in written


@pytest.mark.parametrize("field", ["warn_usd", "soft_cap_usd", "hard_cap_usd", "per_run_cap_usd"])
@pytest.mark.parametrize("bad", ["five", True, -1])
def test_an_invalid_value_is_rejected_with_a_clear_message(client, written, field, bad):
    body = {**_ALL_CAPS, field: bad}
    r = _post(client, _human("operator@example.com"), body)
    assert r.status_code == 400, r.text
    assert field in r.json()["error"]
    assert not written


@pytest.mark.parametrize("field", ["warn_usd", "soft_cap_usd", "hard_cap_usd", "per_run_cap_usd"])
@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN"])
def test_a_non_finite_literal_is_rejected(client, written, field, literal):
    """JSON's `Infinity`/`NaN` are accepted by Python's parser and are not
    amounts of money — the same hole the Library's own `_amount` closes.
    Sent as raw bytes: the stdlib `json` encoder httpx uses for `json=`
    refuses to produce these non-standard literals at all, so the only way
    to exercise the server's own check is to write the body by hand."""
    raw = ("{" + ",".join(
        f'"{k}":{literal if k == field else v}' for k, v in _ALL_CAPS.items()) + "}")
    r = client.post(f"/api/ledgers/{_PROJECT}/caps", headers=_human("operator@example.com"),
                    content=raw.encode(), params=None)
    assert r.status_code == 400, r.text
    assert field in r.json()["error"]
    assert not written


def test_a_library_refusal_comes_back_as_409_with_its_wording(client, monkeypatch):
    def refuse(scope_id, **k):
        raise library_client.LedgerCapsRefused(
            400, "caps must be non-decreasing: warn_usd_micro <= soft_cap_usd_micro "
                 "<= hard_cap_usd_micro")

    monkeypatch.setattr(library_client, "set_ledger_caps", refuse)
    r = _post(client, _human("operator@example.com"))
    assert r.status_code == 409, r.text
    assert "non-decreasing" in r.json()["error"]


def test_a_library_fault_is_not_dressed_up_as_a_refusal(client, monkeypatch):
    def boom(scope_id, **k):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "set_ledger_caps", boom)
    r = _post(client, _human("operator@example.com"))
    assert r.status_code >= 500, r.text
