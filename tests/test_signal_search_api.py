"""signal_search_api.py — the Archives tail's two read routes: browse-by-shape
(`GET /api/signals/browse`, exact) and text search (`GET /api/signals/search`,
approximate).

The load-bearing checks are the ones the spec calls out by name: a shape with
no dedicated section on `/archives` must still be reachable through browse
(the whole point of this change), a kind typed free-text must reach a row of
that kind with no hardcoded allowlist blocking it, and a shape's total must
come from `signal_facets` rather than from the length of the capped list —
planted with a fixture where the two numbers disagree, so a fake that made
them agree by accident could not pass this file for the wrong reason.
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
    # person_scope is the Library's own scope resolver (E19·1,
    # issue_01M35K54QHJYYPXWS28YWEHJP3), NOT `authz.ROLES`'s products list
    # below — browse/search now restrict to it (E19·4,
    # issue_01M35K5JQ7Z7MHYZGWRSB5F85M), so it must be seeded here or every
    # fixture signal (product="vivy") reads as out-of-scope.
    library_fake.seed_person_scope("mem@example.com", {"vivy"})

    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "mem@example.com": {"role": "member", "products": ["vivy"]},
    }))
    monkeypatch.setattr(authz, "ROLES", roles)

    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": ["mem@example.com"]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)

    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)

    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({AGENT_TOKEN: {"name": "an-agent", "products": ["vivy"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)

    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


def _human(email="mem@example.com"):
    return {srv.EMAIL_HEADER: email}


def _mk(**kw):
    base = dict(id=control_db.new_signal_id(), source="github_review",
                kind="pr_comment", actor="reviewer", ingested_by="github-capture",
                product="vivy", body="a finding")
    base.update(kw)
    return base


# --- the gate, both routes ---------------------------------------------------


def test_a_contractor_tier_identity_gets_a_plain_404_on_browse(client):
    r = client.get("/api/signals/browse", params={"kind": "alert"},
                    headers={srv.EMAIL_HEADER: "contractor-only@example.com"})
    assert r.status_code == 404


def test_a_contractor_tier_identity_gets_a_plain_404_on_search(client):
    r = client.get("/api/signals/search", params={"q": "x"},
                    headers={srv.EMAIL_HEADER: "contractor-only@example.com"})
    assert r.status_code == 404


def test_unauthenticated_is_401_on_both_routes(client):
    assert client.get("/api/signals/browse", params={"kind": "alert"}).status_code == 401
    assert client.get("/api/signals/search", params={"q": "x"}).status_code == 401


def test_an_agent_bearer_token_is_refused_a_human_only_surface(client):
    r = client.get("/api/signals/browse", params={"kind": "alert"},
                    headers={"Authorization": f"Bearer {AGENT_TOKEN}"})
    assert r.status_code == 403
    assert "human" in r.json()["error"]


# --- browse: exact, whole-table, reaches a kind with no section ------------


def test_browse_returns_only_the_requested_kind(client):
    """A kind with no dedicated section on /signals (alert, finding,
    deploy_failure) must be reachable here — the guarantee this change exists
    to make true."""
    library_fake.seed_signal(**_mk(kind="alert", body="disk full"))
    library_fake.seed_signal(**_mk(kind="pr_comment", body="lgtm"))
    library_fake.seed_signal(**_mk(kind="alert", body="cert expiring"))

    r = client.get("/api/signals/browse", params={"kind": "alert"}, headers=_human())
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "alert"
    kinds = {s["kind"] for s in body["signals"]}
    assert kinds == {"alert"}
    bodies = {s["body"] for s in body["signals"]}
    assert bodies == {"disk full", "cert expiring"}


def test_browse_reaches_a_kind_typed_free_text_not_in_any_hardcoded_list(client):
    """The mechanism, not the constant: `signal_search_api.browse` passes
    `kind` straight through with no allowlist check, so a kind this page has
    never rendered a section for — and that is not even in `_KNOWN_KINDS` —
    must still come back. Using an invented kind proves the free-text path is
    real rather than a dropdown that happens to be long enough."""
    import signals_build
    invented_kind = "quarterly-retro-note"
    assert invented_kind not in signals_build._KNOWN_KINDS
    library_fake.seed_signal(**_mk(kind=invented_kind, body="ran the retro"))

    r = client.get("/api/signals/browse", params={"kind": invented_kind}, headers=_human())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["signals"][0]["body"] == "ran the retro"


def test_browse_total_comes_from_facets_not_from_the_capped_list_length(client, monkeypatch):
    """Plant a fixture where `signal_facets().total` and the capped browse
    list's length DISAGREE, and assert the exact one renders. A fixture where
    they happen to match would prove nothing — this is `signals_build`'s own
    'the decisions_build pattern' rule, applied to the Archives tail.

    This is the WHOLE-FLEET path specifically (E19·4,
    issue_01M35K5JQ7Z7MHYZGWRSB5F85M): a scoped viewer's own total is
    `len(signals)` instead, deliberately narrower, since `signal_facets`
    takes no multi-project filter to recompute a true scoped total from
    (`browse`'s own docstring) — forced here via `crew_registry.
    covers_every_project` rather than relying on this machine's real
    registry file to agree that "vivy" is every project."""
    import crew_registry
    import signal_search_api
    monkeypatch.setattr(crew_registry, "covers_every_project", lambda scope: True)
    monkeypatch.setattr(signal_search_api, "_BROWSE_CAP", 2)
    for i in range(5):
        library_fake.seed_signal(**_mk(kind="alert", body=f"alert-{i}"))

    r = client.get("/api/signals/browse", params={"kind": "alert"}, headers=_human())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5           # the true total, from signal_facets
    assert len(body["signals"]) == 2    # the capped list, deliberately smaller
    assert body["total"] != len(body["signals"])


_REVIEW_STAMP = (
    "<!-- agentctl-review\n"
    "v: 1\n"
    "pr: 42\n"
    "repo: vivy\n"
    "reviewed-commit: 7842af3bf82cb74fa1522af\n"
    "verdict: APPROVE\n"
    "agent: reviewer\n"
    "run-id: run_9f8e7d\n"
    "-->\n"
)


def test_browse_strips_a_leading_review_stamp_from_the_json_body(client):
    # Round-2 finding (convivy-orient#254): `_signal_json` put `sig.body`
    # straight into the JSON payload with no strip, so a stamp-led pr_comment
    # showed the raw <!-- agentctl-review ... --> block here (and in the
    # client-side card() builder /signals renders this payload into).
    library_fake.seed_signal(**_mk(body=_REVIEW_STAMP + "LGTM, ship it."))
    r = client.get("/api/signals/browse", params={"kind": "pr_comment"}, headers=_human())
    assert r.status_code == 200
    body = r.json()
    assert body["signals"][0]["body"] == "LGTM, ship it."
    assert "agentctl-review" not in json.dumps(body)


def test_browse_requires_a_kind(client):
    r = client.get("/api/signals/browse", headers=_human())
    assert r.status_code == 400


def test_browse_503s_on_a_library_outage(client, monkeypatch):
    def boom(**kw):
        raise ConnectionError("down")
    monkeypatch.setattr(library_client, "list_signals", boom)
    r = client.get("/api/signals/browse", params={"kind": "alert"}, headers=_human())
    assert r.status_code == 503


# --- search: approximate, no kind, honest envelope --------------------------


def test_search_finds_a_body_match(client):
    library_fake.seed_signal(**_mk(body="the deploy failed at 03:14"))
    library_fake.seed_signal(**_mk(body="unrelated content"))
    r = client.get("/api/signals/search", params={"q": "deploy"}, headers=_human())
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["signals"][0]["body"] == "the deploy failed at 03:14"
    assert "distance" in body["signals"][0]


def test_search_strips_a_leading_review_stamp_from_the_json_body(client):
    # Same defect as browse's, on the search route's own JSON payload.
    library_fake.seed_signal(**_mk(body=_REVIEW_STAMP + "LGTM, ship it."))
    r = client.get("/api/signals/search", params={"q": "ship it"}, headers=_human())
    assert r.status_code == 200
    body = r.json()
    assert body["signals"][0]["body"] == "LGTM, ship it."
    assert "agentctl-review" not in json.dumps(body)


def test_search_response_carries_no_total_field(client):
    """`GET /signals/search` reports no whole-store total
    (platform/contracts/library-protocol §3.2), and this route must not
    invent one — `count`/`truncated` describe the retrieved window only."""
    library_fake.seed_signal(**_mk(body="deploy notes"))
    r = client.get("/api/signals/search", params={"q": "deploy"}, headers=_human())
    assert r.status_code == 200
    assert "total" not in r.json()


def test_search_rejects_a_kind_parameter_silently_ignoring_it(client):
    """No `kind` is ever forwarded to the Library — `search_signals` (the
    client function this route calls) takes no `kind` argument at all, so
    there is nothing here to plumb a `kind=` query param through to even if a
    caller sent one."""
    library_fake.seed_signal(**_mk(kind="alert", body="deploy alert fired"))
    r = client.get("/api/signals/search", params={"q": "deploy", "kind": "alert"},
                    headers=_human())
    assert r.status_code == 200  # never a 400 — the param is simply not read


def test_search_empty_query_answers_directly_without_calling_the_library(client, monkeypatch):
    def must_not_be_called(*a, **k):
        raise AssertionError("an empty query must not reach the Library")
    monkeypatch.setattr(library_client, "search_signals", must_not_be_called)
    r = client.get("/api/signals/search", params={"q": ""}, headers=_human())
    assert r.status_code == 200
    assert r.json() == {"count": 0, "truncated": False, "signals": []}


def test_search_503s_on_a_library_outage_rather_than_a_silent_empty_result(client, monkeypatch):
    """A Library outage must read as a failed search, not as 'nothing among
    the closest matches' — the same silent-degradation failure
    `library_client`'s module docstring names, one layer up."""
    def boom(query, **kw):
        raise ConnectionError("down")
    monkeypatch.setattr(library_client, "search_signals", boom)
    r = client.get("/api/signals/search", params={"q": "deploy"}, headers=_human())
    assert r.status_code == 503


# --- scope: a second, out-of-scope project never reaches the viewer ----------


def test_browse_excludes_a_project_outside_the_viewers_scope(client):
    """The viewer holds `vivy` only (the fixture's person_scope). A `constance`
    row of the same kind must not appear, and must not count toward `total`."""
    library_fake.seed_signal(**_mk(kind="alert", body="vivy disk full"))
    library_fake.seed_signal(**_mk(kind="alert", product="constance", body="constance disk full"))

    r = client.get("/api/signals/browse", params={"kind": "alert"}, headers=_human())
    assert r.status_code == 200
    body = r.json()
    assert [s["product"] for s in body["signals"]] == ["vivy"]
    assert body["total"] == 1
    assert "constance" not in r.text


def test_search_excludes_a_project_outside_the_viewers_scope_from_rows_and_count(client):
    """`count` must agree with the filtered list: a Library count over the
    unfiltered window would read 2 here, and the gap between it and the one
    row shown would tell the viewer that another project matched `deploy`."""
    library_fake.seed_signal(**_mk(body="vivy deploy failed"))
    library_fake.seed_signal(**_mk(product="constance", body="constance deploy failed"))

    r = client.get("/api/signals/search", params={"q": "deploy"}, headers=_human())
    assert r.status_code == 200
    body = r.json()
    assert [s["product"] for s in body["signals"]] == ["vivy"]
    assert body["count"] == 1
    assert "constance" not in r.text
