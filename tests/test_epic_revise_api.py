"""`POST /api/epics/{id}/revise` — editing a proposed epic's text.

The gate is `approve_api`'s and is tested the same way, one failure shape per
check. The rest pins what is new here: the lease is taken at save time and
always handed back, and a save over text that changed since the editor opened
is refused rather than applied.

The Library calls are stubbed with a recorder, so each test asserts what was
sent and in what order. Their request shapes are pinned in
`test_library_client.py`, under "revising an epic's text".
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

_ID = "issue_01TESTEPIC"


def _epic(product="convivy-orient", maturity="proposed", spec="old spec",
          dod="- old bullet", state="open", itype="epic"):
    return library_client.IssueRecord(
        id=_ID, url=f"/engineering/issues/{_ID}", product=product,
        title="An epic", body="", type=itype, state=state, severity="high",
        needs="agent", maturity=maturity, parent_id=None, topics=[], refs={},
        specification=spec, definition_of_done=dod,
        resolution=None, resolved_by=None, outcome_note=None, claimed_by=None,
        claimed_at=None, claim_expires_at=None, claim_expired=False,
        resolved_at=None, created_by="ops-mcp", created_by_kind="agent",
        created_at=None, run_id=None, park_reason=None,
        unpark_condition=None, parked_at=None, blocked=False, prerequisites=[])


@pytest.fixture
def client(tmp_path, monkeypatch):
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "obs@example.com": {"role": "observer", "products": ["*"]},
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
        "operator@example.com", "obs@example.com", "scoped@example.com"]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)
    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({"agent-secret": {"name": "an-agent", "products": ["*"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)
    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


class _Library:
    """Records every write, in order, and serves `reads` for `get_issue_item`
    in turn — the first read is the gate's, the second is the one under the
    lease."""

    def __init__(self, monkeypatch, *reads):
        self.calls = []
        self.reads = list(reads)
        self.refuse = {}
        self.release_fails = False
        monkeypatch.setattr(library_client, "get_issue_item", self._get)
        for name in ("checkout_item", "revise_item", "release_item"):
            monkeypatch.setattr(library_client, name, self._writer(name))

    def _get(self, _id):
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]

    def _writer(self, name):
        def call(issue_id, **kw):
            self.calls.append((name, kw))
            if name in self.refuse:
                raise library_client.ItemWriteRefused(409, self.refuse[name])
            if name == "release_item" and self.release_fails:
                raise OSError("connection reset")
        return call

    def names(self):
        return [n for n, _ in self.calls]


def _human(email="operator@example.com", origin="http://testserver"):
    return {srv.EMAIL_HEADER: email, "Origin": origin}


def _body(spec="new spec", dod="- old bullet", base_spec="old spec", base_dod="- old bullet", **extra):
    return {"specification": spec, "definition_of_done": dod,
            "base": {"specification": base_spec, "definition_of_done": base_dod}, **extra}


def _post(client, headers, body=None):
    return client.post(f"/api/epics/{_ID}/revise", headers=headers,
                       json=_body() if body is None else body)


# --- the gate ------------------------------------------------------------------


def test_unauthenticated_is_401(client, monkeypatch):
    _Library(monkeypatch, _epic())
    assert client.post(f"/api/epics/{_ID}/revise", json=_body()).status_code == 401


def test_an_agent_is_refused_as_a_human_act_even_holding_every_capability(client, monkeypatch):
    """Ahead of the origin check, so the message is the one that proves the
    order: an agent sending no Origin header still hears it is not a human."""
    lib = _Library(monkeypatch, _epic())
    monkeypatch.setitem(authz.ROLE_CAPS, "agent",
                        frozenset().union(*authz.ROLE_CAPS.values()))
    r = client.post(f"/api/epics/{_ID}/revise", json=_body(),
                    headers={"Authorization": "Bearer agent-secret"})
    assert r.status_code == 403
    assert "human act" in r.json()["error"]
    assert lib.calls == []


def test_a_cross_origin_request_is_refused(client, monkeypatch):
    lib = _Library(monkeypatch, _epic())
    r = _post(client, _human(origin="http://evil.example"))
    assert r.status_code == 403 and "csrf" in r.json()["error"]
    assert lib.calls == []


@pytest.mark.parametrize("body", [
    {"specification": "x", "definition_of_done": "y"},                       # no base
    {"specification": "x", "definition_of_done": "y", "base": "old"},        # base not an object
    {"specification": 1, "definition_of_done": "y",
     "base": {"specification": "a", "definition_of_done": "b"}},             # not a string
    {"definition_of_done": "y",
     "base": {"specification": "a", "definition_of_done": "b"}},             # field missing
])
def test_a_malformed_body_is_a_400(client, monkeypatch, body):
    lib = _Library(monkeypatch, _epic())
    assert _post(client, _human(), body).status_code == 400
    assert lib.calls == []


def test_a_role_without_the_capability_gets_404(client, monkeypatch):
    lib = _Library(monkeypatch, _epic())
    assert _post(client, _human("obs@example.com")).status_code == 404
    assert lib.calls == []


def test_the_capability_is_checked_against_the_epics_own_product(client, monkeypatch):
    """The refusal, then its positive control: without the second half the
    first would pass just as well if the route refused everybody."""
    lib = _Library(monkeypatch, _epic())
    assert _post(client, _human("scoped@example.com")).status_code == 404
    assert lib.calls == []
    lib = _Library(monkeypatch, _epic(product="vivy"))
    assert _post(client, _human("scoped@example.com")).status_code == 200


def test_an_out_of_scope_epic_and_a_missing_one_answer_byte_identically(client, monkeypatch):
    """E19·2 (issue_01M35K5AB4PRTJ71DMQW7Q4D3B): an epic outside the caller's
    scope must read exactly like one that does not exist, never a distinct
    refusal that would confirm the epic is there."""
    scoped_out_lib = _Library(monkeypatch, _epic())
    out_of_scope = _post(client, _human("scoped@example.com"))
    missing_lib = _Library(monkeypatch, None)
    missing = _post(client, _human("scoped@example.com"))
    assert out_of_scope.status_code == missing.status_code == 404
    assert out_of_scope.content == missing.content
    assert scoped_out_lib.calls == missing_lib.calls == []


def test_a_missing_epic_and_a_non_epic_both_404(client, monkeypatch):
    _Library(monkeypatch, None)
    assert _post(client, _human()).status_code == 404
    _Library(monkeypatch, _epic(itype="bug"))
    assert _post(client, _human()).status_code == 404


# --- what may be edited ----------------------------------------------------------


def test_an_approved_epic_is_not_edited_here(client, monkeypatch):
    """Its text is what was approved. Refused before any lease is taken."""
    lib = _Library(monkeypatch, _epic(maturity="approved"))
    r = _post(client, _human())
    assert r.status_code == 409 and "approved" in r.json()["error"]
    assert lib.calls == []


def test_a_closed_epic_is_not_edited_here(client, monkeypatch):
    lib = _Library(monkeypatch, _epic(state="resolved"))
    assert _post(client, _human()).status_code == 409
    assert lib.calls == []


def test_an_unchanged_save_takes_no_lease(client, monkeypatch):
    """Claiming the epic, even for a moment, is a visible act on every queue.
    Line endings and surrounding whitespace do not count as a change, since a
    textarea changes both on the way back."""
    lib = _Library(monkeypatch, _epic())
    r = _post(client, _human(), _body(spec="old spec\r\n", dod="\n- old bullet"))
    assert r.status_code == 200 and r.json() == {"saved": False, "unchanged": True}
    assert lib.calls == []


# --- the save ----------------------------------------------------------------------


def test_a_save_checks_out_revises_only_what_changed_and_releases(client, monkeypatch):
    lib = _Library(monkeypatch, _epic(), _epic())
    r = _post(client, _human())
    assert r.status_code == 200, r.text
    assert r.json() == {"saved": True, "fields": ["specification"]}
    assert lib.names() == ["checkout_item", "revise_item", "release_item"]
    assert lib.calls[1][1]["fields"] == {"specification": "new spec"}


def test_the_holder_is_the_verified_email_never_a_body_value(client, monkeypatch):
    lib = _Library(monkeypatch, _epic(), _epic())
    _post(client, _human(), _body(owner="somebody-else", acting_human="somebody-else"))
    assert {kw["owner"] for _, kw in lib.calls} == {"operator@example.com"}


def test_an_emptied_field_is_sent_as_null_to_clear_it(client, monkeypatch):
    lib = _Library(monkeypatch, _epic(), _epic())
    _post(client, _human(), _body(spec="old spec", dod="   "))
    assert lib.calls[1][1]["fields"] == {"definition_of_done": None}


def test_text_changed_since_the_editor_opened_is_refused_and_released(client, monkeypatch):
    """**The check that keeps a second editor from overwriting the first.** The
    comparison is against the epic as read UNDER the lease — the second read —
    so the gate's read passing proves nothing about it."""
    lib = _Library(monkeypatch, _epic(), _epic(spec="someone else's edit"))
    r = _post(client, _human())
    assert r.status_code == 409
    assert r.json()["conflict"] is True
    assert lib.names() == ["checkout_item", "release_item"], "no revision, and the lease goes back"


def test_an_epic_approved_while_the_editor_was_open_is_not_rewritten(client, monkeypatch):
    lib = _Library(monkeypatch, _epic(), _epic(maturity="approved"))
    r = _post(client, _human())
    assert r.status_code == 409 and "approved" in r.json()["error"]
    assert lib.names() == ["checkout_item", "release_item"]


def test_an_epic_declined_while_the_editor_was_open_is_not_rewritten(client, monkeypatch):
    """Decline is the resolve route's `wontfix`, which closes the epic and
    leaves `maturity` at `proposed`, so `put_forward` alone would let this
    through. Only the state under the lease shows it."""
    lib = _Library(monkeypatch, _epic(), _epic(state="resolved"))
    r = _post(client, _human())
    assert r.status_code == 409 and "closed" in r.json()["error"]
    assert lib.names() == ["checkout_item", "release_item"]


def test_identical_text_saved_by_someone_else_first_writes_nothing(client, monkeypatch):
    """The Library refuses a revision naming no field; this answers unchanged
    instead of surfacing that refusal as an error."""
    lib = _Library(monkeypatch, _epic(), _epic(spec="new spec"))
    r = _post(client, _human(), _body(base_spec="new spec"))
    assert r.json() == {"saved": False, "unchanged": True}
    assert lib.names() == ["checkout_item", "release_item"]


def test_another_holders_lease_is_named_and_nothing_is_released(client, monkeypatch):
    """A refused checkout holds nothing, so releasing would hand back somebody
    else's claim — the one call here that must not happen."""
    lib = _Library(monkeypatch, _epic())
    lib.refuse["checkout_item"] = "held by co-selector until 18:00"
    r = _post(client, _human())
    assert r.status_code == 409
    assert r.json()["error"] == "held by co-selector until 18:00"
    assert lib.names() == ["checkout_item"]


def test_a_refused_revision_is_shown_and_the_lease_still_goes_back(client, monkeypatch):
    lib = _Library(monkeypatch, _epic(), _epic())
    lib.refuse["revise_item"] = "specification must be at most 8000 characters"
    r = _post(client, _human())
    assert r.status_code == 409
    assert "8000" in r.json()["error"]
    assert lib.names() == ["checkout_item", "revise_item", "release_item"]


def test_a_failed_release_is_reported_rather_than_a_clean_save(client, monkeypatch):
    lib = _Library(monkeypatch, _epic(), _epic())
    lib.release_fails = True
    r = _post(client, _human())
    assert r.status_code == 200
    assert r.json()["saved"] is True
    assert "still checked out as operator@example.com" in r.json()["release_error"]


def test_a_failed_release_after_a_conflict_is_reported_too(client, monkeypatch):
    lib = _Library(monkeypatch, _epic(), _epic(spec="someone else's edit"))
    lib.release_fails = True
    r = _post(client, _human())
    assert r.status_code == 409
    assert "release_error" in r.json()


def test_the_route_is_mounted_and_reaches_this_handler(client, monkeypatch):
    """Called rather than looked up. FastAPI 0.139, which CI installs, wraps an
    included router so `app.routes` reports every included path as `''`; a
    check on that list passed locally against an older FastAPI and failed in
    CI. A 401 carrying this handler's own error body proves the route is
    mounted and is not answered by the approve route's `:path` converter,
    which would say nothing about editing."""
    lib = _Library(monkeypatch, _epic())
    r = client.post(f"/api/epics/{_ID}/revise", json=_body())
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    r = client.post(f"/api/epics/{_ID}/revise", json=_body(),
                    headers={"Authorization": "Bearer agent-secret"})
    assert "editing an epic" in r.json()["error"]
    assert lib.calls == []
