"""Tests for the Web Push surface (ARCH-362 Phase 4b).

The sharp edge here is not the crypto — it is that `endpoint` is an
attacker-influenced URL this server later POSTs to. That is the classic SSRF
shape, so the allow-list gets the most attention below.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import access_identity
import api_common
import authz
import control_db
import library_fake
import push_api
import server as srv
from models import PushSubscription

AGENT_TOKEN = "tok-agent-vivy"

_GOOD_ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"
_KEYS = {"p256dh": "BM" + "A" * 84, "auth": "c2VjcmV0MTIzNDU2Nzg"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    # push_send.notify_alert reads signal visibility through library_client
    # rather than control_db directly.
    library_fake.patch(monkeypatch)
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "mem@example.com": {"role": "member", "products": ["vivy"]},
    }))
    monkeypatch.setattr(authz, "ROLES", roles)
    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({AGENT_TOKEN: {"name": "agent", "products": ["vivy"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)
    # Keys live outside the repo; never touch the real ~/.orient-control in a test.
    monkeypatch.setattr("push_send.CONTROL_DIR", tmp_path / "ctl")
    monkeypatch.setattr("push_send.VAPID_FILE", tmp_path / "ctl" / "vapid.json")
    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


def _human(email="operator@example.com", origin="http://testserver"):
    return {srv.EMAIL_HEADER: email, "Origin": origin}


def _agent():
    return {"Authorization": f"Bearer {AGENT_TOKEN}"}


# --- SSRF: the endpoint is an attacker-influenced URL we will POST to ----------


@pytest.mark.parametrize("bad", [
    "http://fcm.googleapis.com/fcm/send/x",        # not https
    "https://localhost/fcm/send/x",                 # loopback
    "https://127.0.0.1/push",                       # loopback by ip
    "https://169.254.169.254/latest/meta-data/",    # cloud metadata
    "https://100.64.0.1/push",                      # the tailnet — https, not a push service
    "https://evil.example/push",                    # unknown host
    "https://fcm.googleapis.com.evil.example/x",    # suffix-confusion
    "https://notfcm.googleapis.com/x",              # prefix-confusion, no dot boundary
    "file:///etc/passwd",
    "",
])
def test_endpoint_allowlist_rejects(bad):
    """`endpoint` is a URL this server will later POST to with a signed header.

    'https and not localhost' is NOT sufficient — an internal https service on
    the tailnet passes that and must still be refused. So the check is an
    allow-list of known push services, anchored on a dot so
    `fcm.googleapis.com.evil.example` and `notfcm.googleapis.com` both fail.
    """
    assert push_api._endpoint_ok(bad) is False, bad


@pytest.mark.parametrize("good", [
    "https://fcm.googleapis.com/fcm/send/abc",
    "https://android.googleapis.com/gcm/send/abc",
    "https://updates.push.services.mozilla.com/wpush/v2/abc",
    "https://sub.push.apple.com/x",                 # subdomain of an allowed host
])
def test_endpoint_allowlist_accepts_real_push_services(good):
    assert push_api._endpoint_ok(good) is True, good


def test_subscribe_rejects_a_disallowed_endpoint(client):
    r = client.post("/api/push/subscribe", headers=_human(),
                    json={"endpoint": "https://evil.example/push", "keys": _KEYS})
    assert r.status_code == 400
    with control_db.session() as s:
        assert list(s.scalars(select(PushSubscription.id))) == []


# --- human-only ---------------------------------------------------------------


def test_an_agent_cannot_subscribe(client):
    """A push subscription is a channel to a PERSON. An agent registering one
    would be a way to point alert content at an agent-controlled endpoint."""
    r = client.post("/api/push/subscribe", headers=_agent(),
                    json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS})
    assert r.status_code == 403


def test_an_agent_cannot_read_the_vapid_key(client):
    assert client.get("/api/push/key", headers=_agent()).status_code == 403


def test_unauthenticated_is_401(client):
    assert client.get("/api/push/key").status_code == 401
    assert client.post("/api/push/subscribe",
                       json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS}).status_code == 401


# --- CSRF ---------------------------------------------------------------------


def test_subscribe_and_unsubscribe_require_same_origin(client):
    no_origin = {srv.EMAIL_HEADER: "operator@example.com"}
    assert client.post("/api/push/subscribe", headers=no_origin,
                       json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS}).status_code == 403
    assert client.request("DELETE", "/api/push/subscribe", headers=no_origin,
                          json={"endpoint": _GOOD_ENDPOINT}).status_code == 403


# --- registration behaviour ---------------------------------------------------


def test_resubscribing_the_same_browser_updates_rather_than_duplicates(client):
    """A row per re-subscribe would deliver the same notification N times, and
    browsers rotate their keys periodically — so the endpoint is the identity of
    the registration and must be updated in place."""
    for _ in range(3):
        assert client.post("/api/push/subscribe", headers=_human(),
                           json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS}).status_code == 200
    with control_db.session() as s:
        rows = list(s.scalars(select(PushSubscription.endpoint)))
    assert len(rows) == 1


def test_resubscribing_reassigns_ownership_to_the_current_caller(client):
    """Possession of the endpoint means the push service issued it to THIS
    browser. On a shared machine, a second person signing in must not leave the
    first still being notified."""
    client.post("/api/push/subscribe", headers=_human("operator@example.com"),
                json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS})
    client.post("/api/push/subscribe", headers=_human("mem@example.com"),
                json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS})
    with control_db.session() as s:
        idents = list(s.scalars(select(PushSubscription.identity)))
    assert idents == ["mem@example.com"]


def test_unsubscribe_only_removes_your_own(client):
    """Knowing someone else's endpoint must not let you silence them."""
    client.post("/api/push/subscribe", headers=_human("operator@example.com"),
                json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS})
    r = client.request("DELETE", "/api/push/subscribe",
                       headers=_human("mem@example.com"),
                       json={"endpoint": _GOOD_ENDPOINT})
    assert r.status_code == 200          # idempotent, reveals nothing
    with control_db.session() as s:
        assert list(s.scalars(select(PushSubscription.identity))) == ["operator@example.com"]


def test_unsubscribe_is_idempotent(client):
    for _ in range(2):
        r = client.request("DELETE", "/api/push/subscribe", headers=_human(),
                           json={"endpoint": _GOOD_ENDPOINT})
        assert r.status_code == 200


def test_malformed_keys_are_rejected(client):
    for keys in ({}, {"p256dh": "x"}, {"auth": "y"}, {"p256dh": 1, "auth": 2},
                 {"p256dh": "a" * 500, "auth": "b"}):
        r = client.post("/api/push/subscribe", headers=_human(),
                        json={"endpoint": _GOOD_ENDPOINT, "keys": keys})
        assert r.status_code == 400, keys


# --- the VAPID key -------------------------------------------------------------


def test_vapid_key_is_served_and_stable(client):
    r1 = client.get("/api/push/key", headers=_human())
    r2 = client.get("/api/push/key", headers=_human())
    assert r1.status_code == 200
    k = r1.json()["public_key"]
    assert k and r2.json()["public_key"] == k, (
        "the public key changed between calls — rotating it silently invalidates "
        "every existing browser subscription")


def test_the_private_key_is_never_served(client):
    """The VAPID private key is the ability to sign as us. It lives 0600 on disk
    and must never appear in an API response."""
    import push_send
    body = client.get("/api/push/key", headers=_human()).text
    priv = push_send.load_or_create_keys()["private_key"]
    assert "PRIVATE" not in body
    assert priv not in body
    assert "private_key" not in body


def test_the_key_file_is_0600(client, tmp_path):
    import push_send
    push_send.load_or_create_keys()
    mode = push_send.VAPID_FILE.stat().st_mode & 0o777
    assert mode == 0o600, f"VAPID private key is mode {oct(mode)}"


def test_the_control_dir_is_0700_under_a_permissive_umask(client, tmp_path):
    """issue_01M2MFGVAW1C8ZQ7G9CT0E1YXZ: `mkdir(mode=...)` is masked by the
    ambient umask, so the guarantee has to come from the code, not the
    caller's environment."""
    import os

    import push_send
    old_umask = os.umask(0o022)
    try:
        push_send.load_or_create_keys()
    finally:
        os.umask(old_umask)
    mode = push_send.CONTROL_DIR.stat().st_mode & 0o777
    assert mode == 0o700, f"control dir is mode {oct(mode)}"


# --- the service worker --------------------------------------------------------


def test_service_worker_is_served_from_the_origin_root(client):
    """A worker's default scope is its own directory, so one served from
    /assets/ could not control the site."""
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "push" in r.text


def test_service_worker_does_not_cache_or_intercept_fetches(client):
    """This is an authenticated, tier-gated site. A cache in the worker would be
    a place where one person's page could be served to the next viewer of the
    same browser — deliberately absent."""
    body = client.get("/sw.js").text
    assert "caches.open" not in body
    assert "addEventListener('fetch'" not in body


# --- the outbound POST must not follow redirects ------------------------------


def test_the_push_opener_refuses_redirects():
    """The endpoint allow-list only guards the FIRST hop unless redirects are
    refused, and `urlopen` follows them by default.

    Concrete attack: an allowed push host answers `302 Location:
    http://169.254.169.254/...`. urllib follows it, and a 307/308 preserves the
    POST method AND body — so the encrypted payload and our signed VAPID header
    get delivered to whatever the redirect named.
    """
    import urllib.request

    import push_send
    handlers = [type(h).__name__ for h in push_send._OPENER.handlers]
    assert "_NoRedirects" in handlers, "push opener has no redirect guard"

    # and the guard actually declines, rather than merely existing
    h = push_send._NoRedirects()
    req = urllib.request.Request("https://fcm.googleapis.com/fcm/send/x", method="POST")
    assert h.redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/") is None
    assert h.redirect_request(req, None, 307, "Temp", {}, "https://evil.example/") is None


def test_send_one_uses_the_guarded_opener_not_urlopen(monkeypatch):
    """Pin the call site too: a future edit that reaches for urllib.request.urlopen
    would silently restore redirect-following with every test still passing."""
    import push_send
    src = (__import__("pathlib").Path(push_send.__file__)).read_text()
    body = src[src.index("def send_one"):src.index("def notify(")]
    assert "_OPENER.open(" in body
    assert "urllib.request.urlopen(" not in body


# --- port and host-shape bypasses ---------------------------------------------


def test_a_nondefault_port_is_refused():
    """Without this, https://fcm.googleapis.com:22/ passes the HOST test and the
    endpoint becomes a port-prober aimed at an allowed host."""
    assert push_api._endpoint_ok("https://fcm.googleapis.com:22/x") is False
    assert push_api._endpoint_ok("https://fcm.googleapis.com:8080/x") is False
    assert push_api._endpoint_ok("https://fcm.googleapis.com:443/x") is True


def test_host_shape_bypasses_fail_closed():
    """These already fail; pinned so a later 'simplification' cannot bring them back."""
    # userinfo — the real host is after the '@'
    assert push_api._endpoint_ok("https://fcm.googleapis.com@evil.example/x") is False
    # trailing dot is a distinct name and must not be normalised into the allowed one
    assert push_api._endpoint_ok("https://fcm.googleapis.com./x") is False
    # a Cyrillic homograph is simply a different string
    assert push_api._endpoint_ok("https://fcm.googlеapis.com/x") is False
    # case is normalised, so this one is ACCEPTED
    assert push_api._endpoint_ok("https://FCM.GOOGLEAPIS.COM/fcm/send/x") is True


# --- delivery failure handling -------------------------------------------------


def test_a_410_prunes_the_subscription(client, monkeypatch):
    """404/410 is the push service saying authoritatively that the browser
    discarded this subscription. Retrying it forever would be pointless traffic."""
    import push_send
    client.post("/api/push/subscribe", headers=_human(),
                json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS})
    monkeypatch.setattr(push_send, "send_one", lambda sub, payload: (False, 410))
    push_send.notify("operator@example.com", {"title": "t"})
    with control_db.session() as s:
        assert list(s.scalars(select(PushSubscription.id))) == []


def test_a_transient_failure_increments_rather_than_pruning_and_PERSISTS(client,
                                                                        monkeypatch):
    """One outage must not unsubscribe a live device — but the counter is only
    useful if it actually survives the commit. Mutating an attached ORM object
    inside the loop and committing once at the end is the pattern here, so this
    asserts the value is really persisted rather than lost."""
    import push_send
    client.post("/api/push/subscribe", headers=_human(),
                json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS})
    monkeypatch.setattr(push_send, "send_one", lambda sub, payload: (False, 500))

    push_send.notify("operator@example.com", {"title": "t"})
    with control_db.session() as s:
        assert list(s.scalars(select(PushSubscription.failures))) == [1]

    push_send.notify("operator@example.com", {"title": "t"})
    with control_db.session() as s:
        assert list(s.scalars(select(PushSubscription.failures))) == [2], \
            "the failure counter did not persist across calls"


def test_repeated_transient_failures_eventually_prune(client, monkeypatch):
    import push_send
    client.post("/api/push/subscribe", headers=_human(),
                json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS})
    monkeypatch.setattr(push_send, "send_one", lambda sub, payload: (False, 500))
    for _ in range(push_send._MAX_FAILURES):
        push_send.notify("operator@example.com", {"title": "t"})
    with control_db.session() as s:
        assert list(s.scalars(select(PushSubscription.id))) == []


def test_a_success_resets_the_failure_counter(client, monkeypatch):
    import push_send
    client.post("/api/push/subscribe", headers=_human(),
                json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS})
    monkeypatch.setattr(push_send, "send_one", lambda sub, payload: (False, 500))
    push_send.notify("operator@example.com", {"title": "t"})
    monkeypatch.setattr(push_send, "send_one", lambda sub, payload: (True, 201))
    assert push_send.notify("operator@example.com", {"title": "t"}) == 1
    with control_db.session() as s:
        assert list(s.scalars(select(PushSubscription.failures))) == [0]


def test_notify_never_raises_into_its_caller(client, monkeypatch):
    """The capture path calls this. A push failure must never fail a capture."""
    import push_send

    def boom(*a, **k):
        raise RuntimeError("push service on fire")
    monkeypatch.setattr(push_send, "send_one", boom)
    client.post("/api/push/subscribe", headers=_human(),
                json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS})
    assert push_send.notify("operator@example.com", {"title": "t"}) == 0


def test_notify_alert_never_raises_and_respects_authz(client, monkeypatch):
    import push_send

    Sig = {"id": "sig_X", "body": "disk full", "product": "vivy", "kind": "alert",
           "privacy_class": "internal"}
    monkeypatch.setattr(push_send, "send_one", lambda s, p: (True, 201))
    client.post("/api/push/subscribe", headers=_human("mem@example.com"),
                json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS})
    # mem@ is scoped to vivy, so a vivy alert reaches them...
    assert push_send.notify_alert(Sig) == 1
    # ...and a constance alert does not.
    assert push_send.notify_alert({**Sig, "product": "constance"}) == 0


def test_a_customer_class_alert_needs_the_customer_capability_to_push(client,
                                                                     monkeypatch):
    """The customer wall must be enforced in the PUSH path, not inherited.

    Today every human role with signal_read also has signal_customer, so this
    passes either way — which is the point. It is pinned so that adding a future
    role with signal_read and WITHOUT signal_customer cannot silently make push
    a broader disclosure channel than the Alerts page it mirrors (ARCH-322).
    """
    import authz

    import push_send

    Sig = {"id": "sig_C", "body": "customer said something", "product": "vivy",
           "kind": "alert", "privacy_class": "customer"}

    monkeypatch.setattr(push_send, "send_one", lambda s, p: (True, 201))
    client.post("/api/push/subscribe", headers=_human("mem@example.com"),
                json={"endpoint": _GOOD_ENDPOINT, "keys": _KEYS})

    # mem@ holds signal_customer today, so the customer alert reaches them.
    assert push_send.notify_alert(Sig) == 1

    # Now simulate the role that does not: read yes, customer no.
    real = authz.authorize

    def no_customer(ident, cap, product=None):
        if cap == "signal.read_customer":
            return False
        return real(ident, cap, product)
    monkeypatch.setattr(push_send_authz_target(), "authorize", no_customer)
    assert push_send.notify_alert(Sig) == 0, \
        "a customer-class alert pushed to an identity without signal_customer"

    # ...and an internal alert still reaches them, so the guard is not a blanket block.
    assert push_send.notify_alert({**Sig, "privacy_class": "internal"}) == 1


def push_send_authz_target():
    """notify_alert imports authz INSIDE the function, so the patch target is the
    authz module itself rather than a push_send attribute."""
    import authz
    return authz


def test_vapid_subject_comes_from_the_key_file_and_falls_back_to_the_placeholder():
    import push_send
    assert push_send.vapid_subject({"subject": "mailto:ops@example.com"}) == "mailto:ops@example.com"
    assert push_send.vapid_subject({"subject": "https://example.com/contact"}) == "https://example.com/contact"
    assert push_send.vapid_subject({}) == push_send.VAPID_SUBJECT
    assert push_send.vapid_subject({"subject": "ops@example.com"}) == push_send.VAPID_SUBJECT
