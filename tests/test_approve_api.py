"""`POST /api/epics/{id}/approve` — the operator's approve button
(issue_01M32CB6A9T04NVYPARGDT2GWQ).

Weighted like `test_policy_api.py`, and for its stated reason: the gate is the
whole point, and a route like this gets fixed once per check rather than once
for the whole gate. Four checks, tested as four separate failure shapes:

1. **A non-human identity is refused unconditionally**, ahead of everything
   else — proven against an agent identity granted every capability there is,
   including `epic_approve`, and sending no Origin header, so a gate that put
   the kind check below the CSRF or capability check would answer with the
   wrong message. Both return 403, so asserting the status alone would pass on
   that reordering; the message is what cannot.
2. **`epic_approve` is its own capability**, held by admin and by no other
   shipped role, and a role without it gets a plain 404 rather than a
   403, so the surface is not disclosed.
3. **The capability is checked against the EPIC'S OWN PRODUCT**, not "holds
   epic_approve" in the abstract — proven by scoping a holder to one product
   and showing it is refused for an epic outside that scope. This is also why
   the route reads the epic first: an unscoped `authorize` call refuses
   everyone, admin included, because `authz` treats every unlisted action as
   product-scoped.
4. **`acting_human` is the VERIFIED email and never a body value** — proven by
   sending a body that claims to be somebody else and asserting what reached
   the client call.

The Library call itself is stubbed here. `approve_epic`'s own request shape,
its credential, and `_refusal_message`'s parsing — including the malformed-body
fallback — are pinned in `test_library_client.py`, under "approving an epic".
(That claim was false when this file was first written: the route-level stub
was the only coverage those had, which a reviewer caught on PR #316.)
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

_EPIC_ID = "issue_01TESTEPIC"


def _epic(product="convivy-orient", itype="epic"):
    """A minimal IssueRecord — only the fields the route reads."""
    return library_client.IssueRecord(
        id=_EPIC_ID, url=f"/engineering/issues/{_EPIC_ID}", product=product,
        title="An epic", body="", type=itype, state="open", severity="high",
        needs="agent", maturity="proposed", parent_id=None, topics=[], refs={},
        specification="spec text", definition_of_done="dod text",
        resolution=None, resolved_by=None, outcome_note=None, claimed_by=None,
        claimed_at=None, claim_expires_at=None, claim_expired=False,
        resolved_at=None, created_by="ops-mcp", created_by_kind="agent",
        created_at=api_common_now(), run_id=None, park_reason=None,
        unpark_condition=None, parked_at=None, blocked=False, prerequisites=[])


def api_common_now():
    import datetime as dt
    return dt.datetime(2026, 9, 21, 12, 0, 0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "adm@example.com": {"role": "admin", "products": ["*"]},
        "mem@example.com": {"role": "member", "products": ["*"]},
        "obs@example.com": {"role": "observer", "products": ["*"]},
        "scoped@example.com": {"role": "admin", "products": ["vivy"]},
        "po@example.com": {"role": "product_owner", "products": ["vivy"]},
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
        "operator@example.com", "adm@example.com", "mem@example.com", "obs@example.com", "scoped@example.com",
        "po@example.com"]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)

    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)

    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({"agent-secret": {"name": "an-agent", "products": ["*"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)

    monkeypatch.setattr(library_client, "get_issue_item", lambda _id: _epic())
    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def approved(monkeypatch):
    """Record what reached `library_client.approve_epic`, and answer success."""
    seen = {}

    def fake(issue_id, *, acting_human, owner=None):
        seen["issue_id"] = issue_id
        seen["acting_human"] = acting_human
        return library_client.EpicApproval(
            approved_by="convivy-orient", acting_human=acting_human,
            course_id="course_1", position=7)

    monkeypatch.setattr(library_client, "approve_epic", fake)
    return seen


def _human(email, origin="http://testserver"):
    return {srv.EMAIL_HEADER: email, "Origin": origin}


def _agent(token="agent-secret"):
    return {"Authorization": f"Bearer {token}"}


def _post(client, headers, epic_id=_EPIC_ID, **kw):
    return client.post(f"/api/epics/{epic_id}/approve", headers=headers, **kw)


# --- unauthenticated ---------------------------------------------------------


def test_unauthenticated_is_401(client):
    assert client.post(f"/api/epics/{_EPIC_ID}/approve").status_code == 401


# --- (1) a non-human is refused unconditionally ------------------------------


def test_an_agent_is_refused_even_holding_every_capability(client, monkeypatch, approved):
    """**The definition of done names this refusal specifically**: "an
    agent-authenticated request to the endpoint is refused with a 403 naming
    the act as a human one."

    This is the defect the Library's half closed from its side —
    `POST /items/{id}/correct` was gated on `item.resolve`, which every
    dispatched agent holds, so any agent could approve its own epic. Orient
    must not reopen it. The agent role is widened here to hold every
    capability any shipped role holds, `epic_approve` included, and no Origin
    header is sent: if the kind check were missing, conditional, or ordered
    below CSRF or capability, this request would either succeed or answer with
    a different message."""
    every_cap = frozenset().union(*authz.ROLE_CAPS.values()) | {"epic_approve"}
    monkeypatch.setitem(authz.ROLE_CAPS, "agent", every_cap)

    r = _post(client, _agent())
    assert r.status_code == 403, r.text
    assert "human" in r.json()["error"]
    assert not approved, "no approval may reach the Library from an agent"


def test_a_cross_site_post_is_refused(client, approved):
    """CSRF: the approve button rides the ambient CF Access cookie, so a
    cross-site POST must not carry it."""
    r = _post(client, _human("operator@example.com", origin="https://evil.example"))
    assert r.status_code == 403, r.text
    assert "csrf" in r.json()["error"]
    assert not approved


# --- (2) the capability is its own, and its absence is not disclosed ---------


def test_an_admin_approves(client, approved):
    r = _post(client, _human("operator@example.com"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approved"] is True
    assert body["position"] == 7
    assert approved["issue_id"] == _EPIC_ID


def test_a_member_is_refused(client, approved):
    """`epic_approve` is held by admin and product_owner, and not by member
    (the operator, 2026-09-22: "yes to admin only for now, we will need a product owner
    role too"). A member is a full-tier human, so this is the case the
    capability gate exists for, and the 404 matches the observer's below."""
    r = _post(client, _human("mem@example.com"))
    assert r.status_code == 404, r.text
    assert not approved


def test_a_full_tier_role_without_the_capability_gets_404_not_403(client, approved):
    """`observer` is the full-read internal role and holds no writes. A 403
    would tell them the route is here and they are merely not allowed; 404
    matches `policy_api`'s choice and does not disclose the surface."""
    r = _post(client, _human("obs@example.com"))
    assert r.status_code == 404, r.text
    assert not approved


def test_a_contractor_is_refused(client, approved, tmp_path, monkeypatch):
    """**Definition of done: "a contractor-tier human sees no Approve control,
    and a direct request to the endpoint under that identity is refused."**
    This is the endpoint half; `test_chart_house_build` covers the control.

    A contractor is refused twice over — no `epic_approve` in `ROLE_CAPS`, and
    no roles.json entry at all makes them a bare contractor — so the assertion
    is on the refusal, and `authz` is left exactly as shipped."""
    r = _post(client, _human("stranger@example.com"))
    assert r.status_code == 404, r.text
    assert not approved


# --- (3) the capability is checked against the epic's own product ------------


def test_a_holder_scoped_to_another_product_is_refused(client, approved):
    """`epic_approve` is product-scoped, because an epic belongs to a product
    and approving it writes that product's Course. An admin scoped to `vivy`
    must not approve a `convivy-orient` epic — the "check against what the
    request ACTUALLY RESOLVES TO" rule, with the epic's own product as the
    thing it resolves to."""
    r = _post(client, _human("scoped@example.com"))
    assert r.status_code == 404, r.text
    assert not approved


def test_an_out_of_scope_epic_and_a_missing_one_answer_byte_identically(client, approved, monkeypatch):
    """E19·2 (issue_01M35K5AB4PRTJ71DMQW7Q4D3B): an epic outside the caller's
    scope must read exactly like one that does not exist, not a distinct
    refusal that would confirm the epic is there. The route already reads the
    epic before checking scope, so this pins that the two 404 paths converge
    on the same bytes rather than merely the same status code."""
    out_of_scope = _post(client, _human("scoped@example.com"))
    monkeypatch.setattr(library_client, "get_issue_item", lambda _id: None)
    missing = _post(client, _human("scoped@example.com"))
    assert out_of_scope.status_code == missing.status_code == 404
    assert out_of_scope.content == missing.content
    assert not approved


def test_the_same_scoped_holder_approves_an_epic_in_their_own_product(
        client, approved, monkeypatch):
    """The positive control for the test above. Without it, that refusal would
    hold just as well if the route refused everybody — which is exactly what an
    unscoped `authorize` call does, admin included, and is the bug this pair
    was written after finding."""
    monkeypatch.setattr(library_client, "get_issue_item", lambda _id: _epic(product="vivy"))
    r = _post(client, _human("scoped@example.com"))
    assert r.status_code == 200, r.text


def test_a_product_owner_approves_an_epic_in_its_own_product(client, approved, monkeypatch):
    """issue_01M350CGZB6DR577FASP53K7WM's done-when, first half: a
    `product_owner` scoped to one product approves an epic in that product."""
    monkeypatch.setattr(library_client, "get_issue_item", lambda _id: _epic(product="vivy"))
    r = _post(client, _human("po@example.com"))
    assert r.status_code == 200, r.text
    assert approved["acting_human"] == "po@example.com"


def test_a_product_owner_is_refused_on_another_products_epic(client, approved):
    """The second half: a direct request for another product's epic is
    refused, with the same 404 a role lacking the capability gets."""
    r = _post(client, _human("po@example.com"))
    assert r.status_code == 404, r.text
    assert not approved


def test_a_missing_epic_and_a_non_epic_item_both_404(client, approved, monkeypatch):
    monkeypatch.setattr(library_client, "get_issue_item", lambda _id: None)
    assert _post(client, _human("operator@example.com")).status_code == 404
    monkeypatch.setattr(library_client, "get_issue_item", lambda _id: _epic(itype="bug"))
    assert _post(client, _human("operator@example.com")).status_code == 404
    assert not approved


# --- (4) acting_human is the verified email, never the body ------------------


def test_acting_human_is_the_verified_email_not_a_body_value(client, approved):
    """The rule `signal_capture_api` and `policy_api` both state for this
    field: a caller-supplied value would let the attestation assert whatever
    the caller typed, which is the one thing the Library's `attested_human`
    exists not to do. The request below claims to be the operator while authenticating
    as a second administrator."""
    r = _post(client, _human("adm@example.com"),
              json={"acting_human": "operator@example.com", "owner": "somebody-else"})
    assert r.status_code == 200, r.text
    assert approved["acting_human"] == "adm@example.com"
    assert r.json()["acting_human"] == "adm@example.com"


# --- a Library refusal is rendered, not swallowed ----------------------------


def test_a_library_refusal_comes_back_as_409_with_the_librarys_own_wording(
        client, monkeypatch):
    """**Definition of done: "a Library refusal renders on the page naming the
    cause, tested with an epic that has no specification."** This is the
    endpoint half — the message has to survive the proxy for the page to have
    anything to name. The causes are several different 409s (no specification,
    no live Course for the product, held by another session), which is why the
    Library's own text is passed through rather than mapped from a code."""
    def refuse(issue_id, *, acting_human, owner=None):
        raise library_client.EpicApprovalRefused(
            409, "an epic needs a specification before it can be approved")

    monkeypatch.setattr(library_client, "approve_epic", refuse)
    r = _post(client, _human("operator@example.com"))
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["approved"] is False
    assert "specification" in body["error"]


def test_a_library_fault_is_not_dressed_up_as_a_refusal(client, monkeypatch):
    """A 5xx from the Library is a fault, not a rejection. Reporting it as a
    refusal would tell the human their epic was turned down when the Library
    simply fell over — and they would go and edit an epic that was fine."""
    def boom(issue_id, *, acting_human, owner=None):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "approve_epic", boom)
    r = _post(client, _human("operator@example.com"))
    assert r.status_code >= 500, r.text
    assert r.status_code != 409


# --- Orient writes maturity by this path and no other ------------------------


def test_orient_writes_maturity_nowhere_else():
    """**Definition of done: "Orient writes no maturity anywhere else, shown by
    a grep over Orient for writes to the field."**

    Run as a test rather than pasted into a PR body, because a grep in a PR
    body is true on the day it is run and nothing re-runs it.

    **THE CHECK IS THE SHAPE THAT COULD ACTUALLY SEND THE FIELD, not every
    mention of the word.** Orient holds no store of its own for an epic — the
    Library does — so the only way Orient could set a maturity is by putting it
    in a request body, which in this codebase means a `"maturity":` key in a
    dict literal. Comparisons (`entry.maturity == "approved"`), locals
    (`maturity = (item.maturity or "").strip()`) and the dataclass field are
    reads of what the Library served, and a check that flagged those would be
    noise nobody could act on. An earlier draft of this test did flag them, and
    would have had to be suppressed case by case until it caught nothing.

    **BOTH WRITE SHAPES ARE MATCHED, because both are used here.** A reviewer
    suggested the dict-literal pattern would miss a subscript assignment,
    `payload["maturity"] = value`, and I first answered that every body in this
    codebase is a literal so there was no live gap. That was false and I had
    not checked it: `library_client.approve_epic` builds its own body as a
    literal and then adds `body["owner"] = owner`, and `body["run_id"]` does
    the same a few hundred lines down. The suggestion was right and the
    dismissal was the unverified claim — so the pattern covers the assignment
    shape rather than documenting an edge that does not hold."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    # A dict-literal key, or a subscript assignment into one. Both appear in
    # this codebase; see the docstring.
    body_key = re.compile(r"""["']maturity["']\s*:|\[\s*["']maturity["']\s*\]\s*=[^=]""")
    offenders = []
    for path in sorted(root.glob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if body_key.search(line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, (
        "these put `maturity` in a request body; approving through "
        "`library_client.approve_epic` is the only path that may set it:\n"
        + "\n".join(offenders))


def test_only_the_client_addresses_the_librarys_approve_route():
    """The other half of the rule above: one write path, named.

    **Matched on the LIBRARY'S path shape (`/items/.../approve`), not on the
    bare word.** Orient has an approve route of its own (`/api/epics/{id}/
    approve`) and the Chart House fetches it, so a check for "/approve" flags
    those two and says nothing about who talks to the Library. What must stay
    singular is the module that addresses the Library."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    # A STRING LITERAL opening `/items/`, which is how a request path is
    # built here — not the bare path, which also appears in this repo's prose
    # about the route it replaced.
    library_path = re.compile(r"""["']/items/.*?/approve""")
    callers = [p.name for p in sorted(root.glob("*.py"))
               if library_path.search(p.read_text(encoding="utf-8"))]
    assert callers == ["library_client.py"], (
        f"only library_client may address the Library's approve route; found {callers}")


def test_nothing_sets_maturity_through_the_correction_route():
    """The bypass the Library's half closed, kept shut on Orient's side.
    `POST /items/{id}/correct` was gated on `item.resolve`, which every
    dispatched agent holds, and Deck's old Approve button wrote through it. The
    Library now refuses the field by name there — this asserts Orient never
    tries, so the refusal is never something Orient has to handle."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    # A string literal, for the reason the test above gives: `approve_api`'s
    # own docstring names the correction route as the path this replaced, and
    # a bare-path match would flag that prose and nothing else.
    correct = re.compile(r"""["']/items/.*?/(correct|revision)""")
    users = [p.name for p in sorted(root.glob("*.py"))
             if correct.search(p.read_text(encoding="utf-8"))]
    assert users == [], (
        f"Orient addresses the Library's correction route in {users}; approving "
        "is the only path that may set an epic's maturity")
