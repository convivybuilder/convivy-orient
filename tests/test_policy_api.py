"""`POST /api/policy` — the deny-list write route (issue_01M1F1ENN8P6RD4W3SJJ04HR45,
epic issue_01M0C91CGDR0YT8T5ERX2HD2HS).

The gate is the whole point of this file, weighted the way `test_room_api.py`
and `test_kb_write_api.py` weight their own admin-only writes: every test
below is about who may call this route and what it is checked against, not
about `library_client.write_policy` itself (pinned separately in
`test_library_client.py`).

Three checks, tested as three separate failure shapes because the epic's own
history (sibling PR #179, five review rounds) is that a route like this gets
fixed once per check rather than once for the whole gate:

1. A non-human identity is refused UNCONDITIONALLY, ahead of the capability
   check — proven against an agent identity granted every capability
   including `policy_write` itself, sending no Origin header, so a gate that
   reordered the human-only check below the capability check would answer
   with the CSRF check's message instead of the human-only refusal. A test
   that only asserted the status code could pass on such a reordering (both
   checks return 403); asserting the specific message is what cannot.
2. `policy_write` is its OWN capability, granted to `admin` and to no other
   role in the shipped `authz.ROLE_CAPS` (the operator corrected the reasoning behind
   this on 2026-09-04; see that file's comment on `ACTION_CAP["policy.write"]`)
   — proven both ways: a full-tier admin writes with no monkeypatching of
   ROLE_CAPS, and a member, who holds every other admin-adjacent write
   (`blog_write`, `kb_write`), still gets a plain 404, not a 403, so the
   surface is not even disclosed to a role that lacks it.
3. The capability is checked against the CREW THE BODY ACTUALLY RESOLVED TO,
   not "holds policy_write" in the abstract — proven by scoping a
   policy_write-holding identity to one product and showing it is refused for
   a crew outside that scope, exactly the request-claimed-vs-actually-resolved
   shape PR #179 kept re-finding.
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
import crew_registry
import library_client
import model_catalog
import server as srv


_CATALOG = [
    model_catalog.Model(model="claude-opus-5-5", alias="opus", provider="anthropic"),
    model_catalog.Model(model="claude-sonnet-5", alias="sonnet", provider="anthropic"),
]


@pytest.fixture(autouse=True)
def listed_models(monkeypatch):
    """agentctl's model list, which `model_ids` now reads. CI has no agentctl,
    so every test here sees this list unless it replaces it."""
    catalog = {"value": list(_CATALOG)}
    monkeypatch.setattr(model_catalog, "load", lambda: catalog["value"])
    return catalog


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()

    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "scoped@example.com": {"role": "policy_writer", "products": ["vivy"]},
    }))
    monkeypatch.setattr(authz, "ROLES", roles)
    # The project scope now comes from the Library (E19·1,
    # issue_01M35K54QHJYYPXWS28YWEHJP3), never from roles.json's `products` —
    # stubbed by re-reading the SAME roles.json file on every call (several
    # tests below rewrite it mid-test to change an email's grant) rather than
    # a dict snapshot, so a test's later `authz.ROLES.write_text(...)` is
    # still reflected here, the way the pre-E19·1 `human_identity` re-read it
    # fresh on every call.
    monkeypatch.setattr(
        library_client, "person_scope",
        lambda email: frozenset(
            json.loads(authz.ROLES.read_text()).get(email, {}).get("products", [])))

    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": ["operator@example.com", "scoped@example.com"]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)

    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)

    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({"agent-secret": {"name": "an-agent", "products": ["vivy"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)

    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


def _human(email, origin="http://testserver"):
    return {srv.EMAIL_HEADER: email, "Origin": origin}


def _agent(token="agent-secret"):
    return {"Authorization": f"Bearer {token}"}


def _body(**over):
    base = {"agent": "scout", "crew": "vivy", "model": "claude-sonnet-5",
            "target": "WebFetch", "decision": "deny"}
    base.update(over)
    return base


def _post(client, headers, **fields):
    return client.post("/api/policy", json=_body(**fields), headers=headers)


def _grant_policy_write(monkeypatch, *, to_role="policy_writer", extra_caps=frozenset()):
    """Give `to_role` the policy_write capability, plus whatever else a test
    wants alongside it — never touching the shipped roles in ROLE_CAPS."""
    monkeypatch.setitem(authz.ROLE_CAPS, to_role,
                        frozenset({"read", "policy_write"}) | extra_caps)


# --- unauthenticated -----------------------------------------------------------


def test_unauthenticated_is_401(client):
    r = client.post("/api/policy", json=_body())
    assert r.status_code == 401


# --- (1) non-human is refused unconditionally, even holding every other cap ---


def test_an_agent_identity_is_refused_even_holding_every_other_capability(
        client, monkeypatch):
    """The load-bearing test the item calls out by name. `agent` role in
    ROLE_CAPS is widened here to hold policy_write ALONGSIDE every other
    capability any shipped role holds — if the kind check were missing or
    conditional on something else, this request would succeed. It must not,
    because `ident.kind != "human"` is checked first and unconditionally,
    before the capability is ever consulted."""
    every_cap = frozenset().union(*authz.ROLE_CAPS.values()) | {"policy_write"}
    monkeypatch.setitem(authz.ROLE_CAPS, "agent", every_cap)

    r = _post(client, _agent())
    assert r.status_code == 403, r.text
    assert "human" in r.json()["error"]


def test_a_system_identity_is_refused_even_holding_every_other_capability(
        client, monkeypatch):
    """Same shape as the agent test, for the other non-human kind this repo
    resolves (`system`, ARCH-352's platform-job tokens)."""
    every_cap = frozenset().union(*authz.ROLE_CAPS.values()) | {"policy_write"}
    monkeypatch.setitem(authz.ROLE_CAPS, "system", every_cap)
    toks = json.loads(api_common.API_TOKENS.read_text())
    toks["system-secret"] = {"name": "a-system-job", "products": ["*"], "kind": "system"}
    api_common.API_TOKENS.write_text(json.dumps(toks))

    r = _post(client, _agent("system-secret"))
    assert r.status_code == 403, r.text
    assert "human" in r.json()["error"]


# --- CSRF, matching room_api.py's pattern --------------------------------------


def test_a_human_write_without_origin_is_refused(client, monkeypatch):
    _grant_policy_write(monkeypatch)
    authz.ROLES.write_text(json.dumps(
        {"scoped@example.com": {"role": "policy_writer", "products": ["*"]}}))
    r = client.post("/api/policy", json=_body(),
                    headers={srv.EMAIL_HEADER: "scoped@example.com"})  # no Origin
    assert r.status_code == 403
    assert "csrf" in r.json()["error"]


# --- (2) its own capability, not a general admin one — 404, not 403 -----------


def test_a_full_tier_admin_can_write(client, monkeypatch):
    """policy_write is granted to admin in the shipped `authz.ROLE_CAPS` —
    unlike every other write test in this file, this one monkeypatches
    neither ROLE_CAPS nor roles.json's role for operator@example.com, so it is the
    one test that would fail outright if that grant were ever reverted."""
    monkeypatch.setattr(library_client, "write_policy", lambda **kw: {
        **kw, "attested_by": "orient", "version": 1})
    r = _post(client, _human("operator@example.com"))
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "deny"


def test_a_member_lacking_policy_write_gets_a_plain_404(client):
    """The DoD's own wording: not 403. A member holds every other
    admin-adjacent write (`blog_write`, `kb_write`, `room_request`) but not
    `policy_write` — admin-only in shipped `authz.ROLE_CAPS`. A 403 here
    would tell a member this route exists and merely refuses them; 404 does
    not even disclose that."""
    roles = json.loads(authz.ROLES.read_text())
    roles["member@example.com"] = {"role": "member", "products": ["*"]}
    authz.ROLES.write_text(json.dumps(roles))
    r = _post(client, _human("member@example.com"))
    assert r.status_code == 404, r.text


def test_a_role_granted_policy_write_can_write(client, monkeypatch):
    _grant_policy_write(monkeypatch)
    monkeypatch.setattr(library_client, "write_policy", lambda **kw: {
        **kw, "attested_by": "orient", "version": 1})
    r = _post(client, _human("scoped@example.com"))
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "deny"


# --- (3) checked against the crew the body ACTUALLY resolved to ---------------


def test_a_product_scoped_writer_is_refused_for_a_crew_outside_its_scope(
        client, monkeypatch):
    """The PR #179 shape, applied here: scoped@example.com holds policy_write
    but is scoped to product "vivy" only. A request CLAIMING crew="vivy"
    (this fixture's default body) must succeed; the same identity writing
    for crew="constance" — a crew outside its own resolved product scope —
    must be refused the same way a missing capability is, not treated as
    'holds policy_write, so anything goes'."""
    _grant_policy_write(monkeypatch)
    monkeypatch.setattr(library_client, "write_policy", lambda **kw: {
        **kw, "attested_by": "orient", "version": 1})

    ok = _post(client, _human("scoped@example.com"), crew="vivy")
    assert ok.status_code == 200, ok.text

    out_of_scope = _post(client, _human("scoped@example.com"), crew="constance")
    assert out_of_scope.status_code == 404, out_of_scope.text


def test_a_wildcard_scoped_writer_may_write_any_crew(client, monkeypatch):
    _grant_policy_write(monkeypatch)
    authz.ROLES.write_text(json.dumps(
        {"scoped@example.com": {"role": "policy_writer", "products": ["*"]}}))
    monkeypatch.setattr(library_client, "write_policy", lambda **kw: {
        **kw, "attested_by": "orient", "version": 1})
    r = _post(client, _human("scoped@example.com"), crew="anything-at-all")
    assert r.status_code == 200, r.text


# --- acting_human is derived, never read off the body --------------------------


def test_a_caller_supplied_acting_human_is_refused_rather_than_dropped(
        client, monkeypatch):
    _grant_policy_write(monkeypatch)
    r = _post(client, _human("scoped@example.com"), acting_human="operator@example.com")
    assert r.status_code == 400, r.text
    assert "acting_human" in r.json()["error"]


def test_the_verified_identity_is_recorded_as_acting_human(client, monkeypatch):
    _grant_policy_write(monkeypatch)
    seen = {}

    def fake_write_policy(**kw):
        seen.update(kw)
        return {**kw, "attested_by": "orient", "version": 1}

    monkeypatch.setattr(library_client, "write_policy", fake_write_policy)
    r = _post(client, _human("scoped@example.com"))
    assert r.status_code == 200, r.text
    assert seen["acting_human"] == "scoped@example.com"


# --- the model is normalized to the id agentctl resolves, not stored as-typed -


def test_an_alias_model_is_stored_under_its_resolved_id(client, monkeypatch):
    """The load-bearing test the item calls out by name
    (issue_01M2HVGKSNFMHZF0FDN8FBT9N6): an operator who types `sonnet` — the
    value the page's own Model datalist offers — must write the row
    `agentctl` actually looks up (`claude-sonnet-5`), or the deny-list entry
    is stored correctly and binds nothing. Confirmed to fail against the
    prior write path (no normalization), which passed `model="sonnet"`
    straight through to `library_client.write_policy`."""
    _grant_policy_write(monkeypatch)
    seen = {}

    def fake_write_policy(**kw):
        seen.update(kw)
        return {**kw, "attested_by": "orient", "version": 1}

    monkeypatch.setattr(library_client, "write_policy", fake_write_policy)
    r = _post(client, _human("scoped@example.com"), model="sonnet")
    assert r.status_code == 200, r.text
    assert seen["model"] == "claude-sonnet-5"
    assert r.json()["model"] == "claude-sonnet-5"


def test_an_already_resolved_model_id_is_unchanged(client, monkeypatch):
    _grant_policy_write(monkeypatch)
    seen = {}

    def fake_write_policy(**kw):
        seen.update(kw)
        return {**kw, "attested_by": "orient", "version": 1}

    monkeypatch.setattr(library_client, "write_policy", fake_write_policy)
    r = _post(client, _human("scoped@example.com"), model="claude-sonnet-5")
    assert r.status_code == 200, r.text
    assert seen["model"] == "claude-sonnet-5"


# --- field validation: type-check to reject, strip as a separate step ---------


@pytest.mark.parametrize("field", ["agent", "crew", "model", "target"])
def test_a_non_string_field_is_rejected_not_silently_emptied(client, monkeypatch, field):
    """The Library `PUT /policy` review's own lesson (round 2): letting
    `isinstance` decide only whether to strip lets a truthy non-string reach
    `len()` uncaught, or a clip function turn it into an empty string that
    still writes. This must 400, not 200 for an empty-string row."""
    _grant_policy_write(monkeypatch)

    def must_not_be_called(**kw):
        raise AssertionError("must not reach the Library with an unvalidated field")

    monkeypatch.setattr(library_client, "write_policy", must_not_be_called)
    r = _post(client, _human("scoped@example.com"), **{field: 12345})
    assert r.status_code == 400, r.text
    assert field in r.json()["error"]


@pytest.mark.parametrize("field", ["agent", "crew", "model", "target"])
def test_a_whitespace_only_field_is_rejected(client, monkeypatch, field):
    _grant_policy_write(monkeypatch)
    r = _post(client, _human("scoped@example.com"), **{field: "   "})
    assert r.status_code == 400, r.text


def test_an_unrecognised_decision_is_rejected(client, monkeypatch):
    """Constrained to what the Library accepts, matching the read page's own
    discipline (`policy_build._DECISION_PILL`): a write route is the first
    thing that could put an arbitrary value into that column, so it must not
    let one through."""
    _grant_policy_write(monkeypatch)

    def must_not_be_called(**kw):
        raise AssertionError("must not reach the Library with an invalid decision")

    monkeypatch.setattr(library_client, "write_policy", must_not_be_called)
    r = _post(client, _human("scoped@example.com"), decision="ALLOW")
    assert r.status_code == 400, r.text


# --- Library failure translation -----------------------------------------------


def test_a_403_from_the_library_reads_as_a_deployment_fault_not_a_permission_one(
        client, monkeypatch):
    """Every Orient-side gate above has already passed by the time this is
    reachable, so a 403 from the Library means its own credential check
    failed — a stale or unregistered approved-client token — never the
    caller's fault. Matches `signal_capture_api.py`'s identical translation
    for its own attested write."""
    def refuse(**kw):
        raise urllib.error.HTTPError(None, 403, "forbidden", None, None)

    _grant_policy_write(monkeypatch)
    monkeypatch.setattr(library_client, "write_policy", refuse)
    r = _post(client, _human("scoped@example.com"))
    assert r.status_code == 502, r.text
    assert "approved-client" in r.json()["error"]


def test_a_library_outage_is_502(client, monkeypatch):
    _grant_policy_write(monkeypatch)
    monkeypatch.setattr(library_client, "write_policy",
                        lambda **kw: (_ for _ in ()).throw(TimeoutError("no route to host")))
    r = _post(client, _human("scoped@example.com"))
    assert r.status_code == 502, r.text


# --- no second copy: nothing here reaches Orient's own control database -------


def test_module_imports_no_control_db():
    import policy_api as mod
    assert not hasattr(mod, "control_db")


# --- the alias table comes from agentctl (issue_01M2HVGKSNFMHZF0FDN8FBT9N6) --


def test_opus_writes_the_id_agentctl_dispatches_today(client, monkeypatch):
    """The hand-kept table wrote `claude-opus-5` for `opus` after agentctl
    moved to `claude-opus-5-5`, so the entry bound nothing."""
    _grant_policy_write(monkeypatch)
    seen = {}

    def fake_write_policy(**kw):
        seen.update(kw)
        return {**kw, "attested_by": "orient", "version": 1}

    monkeypatch.setattr(library_client, "write_policy", fake_write_policy)
    r = _post(client, _human("scoped@example.com"), model="opus")
    assert r.status_code == 200, r.text
    assert seen["model"] == "claude-opus-5-5"


@pytest.mark.parametrize("unavailable", [None, []])
def test_a_write_is_503_while_the_model_list_is_unavailable(client, monkeypatch, listed_models,
                                                           unavailable):
    _grant_policy_write(monkeypatch)
    written = []
    monkeypatch.setattr(library_client, "write_policy", lambda **kw: written.append(kw))
    listed_models["value"] = unavailable
    r = _post(client, _human("scoped@example.com"), model="sonnet")
    assert r.status_code == 503, r.text
    assert "model list is unavailable" in r.json()["error"]
    assert not written


# --- every public crew in one write --------------------------------------------


def _all_crews_body(**over):
    body = _body(**{"all_public_crews": True, **over})
    del body["crew"]
    return body


@pytest.fixture
def public_crews(monkeypatch):
    crews = {"value": ["bosun", "deck", "vivy"]}

    def load():
        if crews["value"] is None:
            raise crew_registry.RegistryUnavailable("absent")
        return list(crews["value"])

    monkeypatch.setattr(crew_registry, "public_crews", load)
    return crews


def _recording_write(monkeypatch, fail_at=None):
    written = []

    def write(**kw):
        if kw["crew"] == fail_at:
            raise TimeoutError("no route to host")
        written.append(kw)
        return {**kw, "attested_by": "orient", "version": len(written)}

    monkeypatch.setattr(library_client, "write_policy", write)
    return written


def test_all_public_crews_writes_the_row_once_per_crew(client, monkeypatch, public_crews):
    """The 36-grant chore this exists for: one request, one exact row per
    registry crew, each carrying the same agent, model, target and decision."""
    written = _recording_write(monkeypatch)
    r = client.post("/api/policy", json=_all_crews_body(decision="allow", model="sonnet"),
                    headers=_human("operator@example.com"))
    assert r.status_code == 200, r.text
    assert [w["crew"] for w in written] == ["bosun", "deck", "vivy"]
    assert {(w["agent"], w["model"], w["target"], w["decision"]) for w in written} == {
        ("scout", "claude-sonnet-5", "WebFetch", "allow")}
    assert all(w["acting_human"] == "operator@example.com" for w in written)
    assert r.json()["crews"] == ["bosun", "deck", "vivy"]


def test_a_writer_scoped_to_one_crew_is_refused_whole_and_nothing_is_written(
        client, monkeypatch, public_crews):
    """scoped@example.com may write for vivy only. Every crew is checked before
    the first write, so it gets the same 404 as any out-of-scope write and
    vivy is not written either."""
    _grant_policy_write(monkeypatch)
    written = _recording_write(monkeypatch)
    r = client.post("/api/policy", json=_all_crews_body(),
                    headers=_human("scoped@example.com"))
    assert r.status_code == 404, r.text
    assert written == []


def test_crew_and_all_public_crews_together_are_refused(client, monkeypatch, public_crews):
    written = _recording_write(monkeypatch)
    r = client.post("/api/policy", json={**_body(), "all_public_crews": True},
                    headers=_human("operator@example.com"))
    assert r.status_code == 400, r.text
    assert written == []


@pytest.mark.parametrize("value", ["true", 1, None])
def test_all_public_crews_must_be_a_boolean(client, monkeypatch, public_crews, value):
    written = _recording_write(monkeypatch)
    r = client.post("/api/policy", json=_all_crews_body(all_public_crews=value),
                    headers=_human("operator@example.com"))
    assert r.status_code == 400, r.text
    assert written == []


def test_all_public_crews_is_503_while_the_registry_is_unavailable(
        client, monkeypatch, public_crews):
    written = _recording_write(monkeypatch)
    public_crews["value"] = None
    r = client.post("/api/policy", json=_all_crews_body(),
                    headers=_human("operator@example.com"))
    assert r.status_code == 503, r.text
    assert "registry" in r.json()["error"]
    assert written == []


def test_a_library_failure_part_way_names_what_was_written(client, monkeypatch, public_crews):
    """The Library has no transaction across rows, so the answer has to say
    where it stopped."""
    written = _recording_write(monkeypatch, fail_at="deck")
    r = client.post("/api/policy", json=_all_crews_body(),
                    headers=_human("operator@example.com"))
    assert r.status_code == 502, r.text
    assert r.json()["written"] == ["bosun"]
    assert r.json()["failed_crew"] == "deck"
    assert [w["crew"] for w in written] == ["bosun"]


def test_a_single_crew_write_answers_as_before(client, monkeypatch, public_crews):
    """Without the flag the registry is not read and the response keeps its
    single `crew` field."""
    public_crews["value"] = None
    written = _recording_write(monkeypatch)
    r = _post(client, _human("operator@example.com"), crew="vivy")
    assert r.status_code == 200, r.text
    assert r.json()["crew"] == "vivy"
    assert [w["crew"] for w in written] == ["vivy"]


def test_a_registry_with_no_public_crew_writes_nothing_and_says_so(
        client, monkeypatch, public_crews):
    """Reviewer, PR #348 round 1: the success branch read the last write's
    result, which an empty crew list never bound."""
    public_crews["value"] = []
    written = _recording_write(monkeypatch)
    r = client.post("/api/policy", json=_all_crews_body(),
                    headers=_human("operator@example.com"))
    assert r.status_code == 200, r.text
    assert r.json()["crews"] == []
    assert written == []
