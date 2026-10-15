"""CQ·151 (E21) clause 3: "Every write route that gates on identity today
gates the same way for a tailnet identity." Checked by walking every
non-read route mounted on `server.app` — discovered from the app itself,
never a hand-kept list, so a route added later is covered automatically —
and hitting each one on the tailnet listener as a whois-named contractor
carrying a forged Cloudflare Access header that names a full-tier,
fully-scoped person. Every one must refuse, and the asserted refusal is the
SPECIFIC status and message its own role/tier check gives a contractor —
not merely "not 2xx" — because several of these routes also answer a
validation error with a non-2xx status for ANY identity, which would make a
coarser assertion pass whether or not the header had leaked through as the
real identity. Pinning the exact refusal means a regression that let the
forged header's identity through would surface as a DIFFERENT status or
message (most of these routes would otherwise proceed toward success, since
the forged person holds every capability), not as a silent pass.

This file complements `tests/test_server.py`'s
`test_write_api_identity_on_tailnet_is_whois_never_the_header`, which pins
the same property at the `api_common.resolve_identity` layer directly. That
test proves the identity primitive is correct; this one proves every
mounted write route actually calls it rather than reading a header on its
own — the gap CQ·151's own body asks to close.

**How every route is found.** `_discovered_write_routes()` walks
`server.app.routes`, unwrapping FastAPI's `_IncludedRouter` (the shape
`app.include_router()` produces on this pinned FastAPI version — see
`ROUTE_EXPECTATIONS`'s own partition test) down to each `APIRoute`, and
keeps every method other than GET/HEAD. `test_every_discovered_get_is_read_only`
walks the GET side of the same discovery and pins it against a hand-read
list, so a future GET that writes cannot dodge this file by using a method
the main walk does not check.

Every discovered write route is filed into `ROUTE_EXPECTATIONS` (rendered
and asserted below) or `EXCLUDED` (with a reason) by
`test_every_discovered_write_route_is_accounted_for` — the same
cannot-grow-silently shape `tests/test_scope_route_walk.py` uses for the
page side.
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi.routing import APIRoute, _IncludedRouter
from fastapi.testclient import TestClient

import access_identity as ident
import authz
import control_db
import library_fake
import server as srv
import tailscale_whois

# --- the two identities every route below is asked about --------------------

CONTRACTOR_LOGIN = "contractor@example.com"  # whois's answer: no roles.json
                                              # entry (role=contractor) and
                                              # not in tiers.json's full list.
FORGED_FULL_TIER_EMAIL = "admin@example.com"  # the Cloudflare email header's
                                              # claim: full tier, role=admin,
                                              # "*" scope — maximally
                                              # privileged, so a route that
                                              # mistakenly trusted it would
                                              # have every capability to
                                              # succeed with.
PROJECT = "proj-x"
ISSUE_ID = "issue-under-test"
EPIC_ID = "epic-under-test"
SIGNAL_ID = "sig-under-test"

TAILNET_BIND = "192.0.2.1"
TAILNET_PORT = 8099
ORIGIN = f"http://{TAILNET_BIND}:{TAILNET_PORT}"


def _tailnet_url(path: str) -> str:
    return f"{ORIGIN}{path}"


def _stub_whois(monkeypatch, login: str | None) -> None:
    body = None if login is None else json.dumps({"UserProfile": {"LoginName": login}}).encode()
    monkeypatch.setattr(tailscale_whois, "_request", lambda path, **kw: body)
    monkeypatch.setattr(tailscale_whois, "_cache", {})


# --- route discovery (so a route added later cannot dodge this walk) --------


def _walk_api_routes():
    def walk(routes):
        for r in routes:
            if isinstance(r, _IncludedRouter):
                yield from walk(r.original_router.routes)
            elif isinstance(r, APIRoute):
                yield r
    yield from walk(srv.app.routes)


def _discovered_write_routes() -> set[tuple[str, str]]:
    out = set()
    for r in _walk_api_routes():
        for m in r.methods:
            if m not in ("GET", "HEAD"):
                out.add((m, r.path))
    return out


def _discovered_get_routes() -> set[str]:
    return {r.path for r in _walk_api_routes() if "GET" in r.methods}


# GET routes on the mounted /api routers, read by hand (each handler's own
# body) rather than inferred — confirmed none of these mutates state. A GET
# discovered here that is not in this set fails
# `test_every_discovered_get_is_read_only` rather than silently sitting
# outside the write walk.
_READ_ONLY_GETS = frozenset({
    "/api/rooms/{room_id}", "/api/blog/drafts", "/api/blog/posts",
    "/api/blog/drafts/{slug}", "/api/memory/documents/{doc_id:path}/body",
    "/api/memory/search", "/api/push/key", "/api/signals/browse",
    "/api/signals/search",
    "/{full_path:path}",  # the page catch-all (GET/HEAD) — server.handle_get
})


def test_every_discovered_get_is_read_only():
    discovered = _discovered_get_routes()
    missing = sorted(discovered - _READ_ONLY_GETS)
    assert not missing, (
        f"GET route(s) this file has not reviewed for a write side effect: "
        f"{missing} — read the handler and add to _READ_ONLY_GETS, or if it "
        f"writes, add it to the walk below instead")
    extra = sorted(_READ_ONLY_GETS - discovered)
    assert not extra, f"GET route(s) listed here no longer exist: {extra}"


# --- the expected refusal, exact rather than "not 2xx" -----------------------
#
# Each value is the (status, error-message) a contractor-role, contractor-tier
# identity gets from that route's OWN gate, read from its source (the
# comments beside each entry name the exact check). A coarser "not 2xx"
# assertion would also accept the validation errors several of these routes
# give ANY identity on a malformed body — so bodies/path params below are
# chosen to be well-formed enough to clear validation and reach the
# identity-driven check, which is the one line this file exists to pin.

ROUTE_EXPECTATIONS: dict[tuple[str, str], tuple[int, str]] = {
    # room_api.py: authorize("room.admin") precedes any body/lookup.
    ("POST", "/api/rooms"): (403, "forbidden"),
    ("POST", "/api/rooms/{room_id}/entries"): (403, "forbidden"),
    ("POST", "/api/rooms/{room_id}/archive"): (403, "forbidden"),
    # blog_api.py: _authorized()'s authorize("blog.write") precedes the body.
    ("POST", "/api/blog/drafts"): (403, "blog.write required"),
    ("PUT", "/api/blog/drafts/{slug}"): (403, "blog.write required"),
    ("POST", "/api/blog/drafts/{slug}/publish"): (403, "blog.write required"),
    # kb_write_api.py: _authorized()'s tier_for() != "full" check precedes
    # kb.write, CSRF and every data lookup — a contractor-tier identity 404s
    # here before the document's own existence is ever consulted.
    ("POST", "/api/kb/documents/{doc_id:path}/checkout"): (404, "not found"),
    ("POST", "/api/kb/documents/{doc_id:path}/heartbeat"): (404, "not found"),
    ("POST", "/api/kb/documents/{doc_id:path}/checkin"): (404, "not found"),
    ("POST", "/api/kb/documents/{doc_id:path}/save"): (404, "not found"),
    # signal_ack_api.py: _ack_visible()'s authorize("signal.read") on a real
    # (seeded) signal — an unseeded id would 404 for ANY identity and prove
    # nothing, so SIGNAL_ID is seeded below.
    ("POST", "/api/signals/{signal_id}/ack"): (404, "not found"),
    ("DELETE", "/api/signals/{signal_id}/ack"): (404, "not found"),
    # signal_capture_api.py: authorize("signal.create") on a well-formed body.
    ("POST", "/api/signals"): (403, "forbidden"),
    # policy_api.py: authorize("policy.write") precedes the body.
    ("POST", "/api/policy"): (404, "not found"),
    # alerts_policy_api.py: authorize("alerts.write") precedes the body.
    ("POST", "/api/alerts-policy"): (403, "forbidden"),
    # approve_api.py / epic_revise_api.py: authorize("epic.approve"/"epic.
    # revise") on a real (seeded) epic — an unseeded id 404s for ANY
    # identity at the `item is None` branch and proves nothing, so EPIC_ID is
    # seeded below as an epic-typed item.
    ("POST", "/api/epics/{issue_id:path}/approve"): (404, "not found"),
    ("POST", "/api/epics/{issue_id:path}/revise"): (404, "not found"),
    # model_override_api.py / issue_write_api.py: authorize("item.model"/
    # "issue.resolve") on a real (seeded) issue, same reasoning as the epics.
    ("POST", "/api/items/{issue_id:path}/model"): (404, "not found"),
    ("POST", "/api/issues/{issue_id:path}/resolve"): (404, "not found"),
    # issue_write_api.py: authorize("issue.create") on a well-formed body.
    ("POST", "/api/issues"): (404, "not found"),
    # ledger_caps_api.py: authorize("ledger.caps_write") precedes the body.
    ("POST", "/api/ledgers/{project:path}/caps"): (404, "not found"),
}

# Discovered write routes this walk does not assert a refusal for, each with
# the reason — the same shape `tests/test_scope_route_walk.py`'s `_EXCLUDED`
# uses.
EXCLUDED: dict[tuple[str, str], str] = {
    # authz.ROLE_CAPS grants "room_request" to every human role, a bare
    # contractor included (room.request is a human editing their OWN room
    # through the concierge, not a privileged write) — a correctly-sourced
    # whois contractor and the forged full-tier header succeed equally here,
    # so a refusal cannot tell them apart. Covered instead by the
    # identity-level test (test_server.py::
    # test_write_api_identity_on_tailnet_is_whois_never_the_header) and this
    # route's own capability tests.
    ("POST", "/api/room/requests"):
        "room.request is granted to every human role including a bare "
        "contractor (authz.ROLE_CAPS); a correct and a regressed identity "
        "succeed equally, so refusal can't distinguish them here",
    # push_api.py's subscribe/unsubscribe check only ident.kind == "human" —
    # no role capability gates either, by design (every team member may hold
    # a push subscription). Same "cannot distinguish by refusal" reason.
    ("POST", "/api/push/subscribe"):
        "push subscribe checks only that the caller is human, no role "
        "capability; every human role succeeds equally here",
    ("DELETE", "/api/push/subscribe"):
        "push unsubscribe, same gate as POST /api/push/subscribe",
    # server.handle_post: a retired ingest path that answers 404
    # unconditionally, before resolve_identity is ever called — nothing here
    # depends on which listener the request landed on.
    ("POST", "/{full_path:path}"):
        "retired ingest catch-all (server.handle_post); always 404 before "
        "any identity resolution runs",
}

# Path parameters a route needs filled with a seeded id rather than the
# generic "does-not-exist" placeholder, for the reasons noted beside each
# entry in ROUTE_EXPECTATIONS above.
_PATH_PARAMS: dict[str, dict[str, str]] = {
    "/api/signals/{signal_id}/ack": {"signal_id": SIGNAL_ID},
    "/api/epics/{issue_id:path}/approve": {"issue_id": EPIC_ID},
    "/api/epics/{issue_id:path}/revise": {"issue_id": EPIC_ID},
    "/api/items/{issue_id:path}/model": {"issue_id": ISSUE_ID},
    "/api/issues/{issue_id:path}/resolve": {"issue_id": ISSUE_ID},
}

# Request bodies a route needs well-formed to clear validation and reach its
# own identity-driven check (see ROUTE_EXPECTATIONS). Every other route in
# the walk is refused before its body is ever read, so `{}` is enough.
_BODIES: dict[str, dict] = {
    "/api/signals": {"source": "capture", "kind": "note", "actor": "someone",
                     "product": PROJECT},
    "/api/issues": {"product": PROJECT, "title": "Test issue"},
    # epic_revise_api.py validates specification/definition_of_done/base
    # BEFORE the item lookup/authorize this test means to reach.
    "/api/epics/{issue_id:path}/revise": {
        "specification": "spec text", "definition_of_done": "dod text",
        "base": {"specification": "", "definition_of_done": ""},
    },
}

_PARAM_RE = re.compile(r"\{(\w+)(?::[^}]+)?\}")


def _fill_path(path: str, params: dict) -> str:
    return _PARAM_RE.sub(lambda m: params.get(m.group(1), "does-not-exist"), path)


def test_every_discovered_write_route_is_accounted_for():
    discovered = _discovered_write_routes()
    accounted = set(ROUTE_EXPECTATIONS) | set(EXCLUDED)
    missing = sorted(discovered - accounted)
    assert not missing, (
        f"write route(s) discovered that this walk has not filed anywhere: "
        f"{missing} — add each to ROUTE_EXPECTATIONS or to EXCLUDED (with a "
        f"reason)")
    extra = sorted(accounted - discovered)
    assert not extra, f"this walk lists a route that no longer exists: {extra}"


# --- the fixture every route below is hit through ----------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    library_fake.patch(monkeypatch)
    # The forged header's claimed person, with the widest possible scope —
    # so that if a route ever mistook it for the real identity, every
    # product-scoped capability check below would ALSO have gone its way,
    # rather than still refusing on scope alone and masking the mistake.
    library_fake.seed_person_scope(FORGED_FULL_TIER_EMAIL, frozenset({"*"}))

    library_fake.seed_issue(id=ISSUE_ID, product=PROJECT, title="An issue", state="open")
    library_fake.seed_issue(id=EPIC_ID, product=PROJECT, title="An epic", type="epic",
                            state="open")
    library_fake.seed_signal(id=SIGNAL_ID, source="ci", kind="test-result", actor="ci",
                             ingested_by="ci", product=PROJECT, body="irrelevant")

    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": [FORGED_FULL_TIER_EMAIL]}))
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({FORGED_FULL_TIER_EMAIL: {"role": "admin"}}))
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    tailnetc = tmp_path / "tailnet.json"
    tailnetc.write_text(json.dumps(
        {"enabled": True, "bind": TAILNET_BIND, "port": TAILNET_PORT}))

    monkeypatch.setattr(srv, "SITE", tmp_path / "site")
    monkeypatch.setattr(ident, "TIERS", tiers)
    monkeypatch.setattr(ident, "JWT_CONFIG", jwtc)
    monkeypatch.setattr(authz, "ROLES", roles)
    monkeypatch.setattr(srv.tailnet_identity, "TAILNET_CONFIG", tailnetc)
    monkeypatch.setattr(srv, "ACCESS_LOG", tmp_path / "access.log")
    monkeypatch.setattr(srv.tailnet_identity, "_bound", {
        (TAILNET_BIND, TAILNET_PORT): srv.tailnet_identity.TAILNET,
    })
    _stub_whois(monkeypatch, CONTRACTOR_LOGIN)

    with TestClient(srv.app, client=("192.0.2.9", 54321), follow_redirects=False) as c:
        yield c


@pytest.mark.parametrize("method,path", sorted(ROUTE_EXPECTATIONS))
def test_tailnet_contractor_is_refused_on_every_write_route(client, method, path):
    """The whois login is a bare contractor; the Cloudflare email header
    forges a full-tier admin with "*" scope. If the route ever read that
    header instead of whois, it would proceed as that admin — who holds
    every capability these routes check — rather than stop at this exact
    refusal."""
    expected_status, expected_error = ROUTE_EXPECTATIONS[(method, path)]
    url = _tailnet_url(_fill_path(path, _PATH_PARAMS.get(path, {})))
    headers = {srv.EMAIL_HEADER: FORGED_FULL_TIER_EMAIL, "Origin": ORIGIN}
    body = _BODIES.get(path, {})
    r = client.request(method, url, json=body, headers=headers)
    assert r.status_code == expected_status, (method, path, r.status_code, r.text)
    assert r.json().get("error") == expected_error, (method, path, r.text)
