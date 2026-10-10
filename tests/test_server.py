"""Security tests for server.py — the identity gate for team.convivy.com.

The risk these guard: server.py hand-rolls RSA PKCS#1-v1.5 / RS256 JWT
verification and the tier routing that decides who sees full-team data. A
regression here either lets a forged/expired token through or leaks the full
bundle (and now the /dashboard cost+perf page) to a contractor. Stdlib-only,
matching the server: a throwaway 1024-bit test keypair signs JWTs in-process and
_jwks_key is monkeypatched to the test public key, so no network/real Access app.
"""
import base64
import hashlib
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

import server as srv
# Identity primitives moved to access_identity (WS0.3). They resolve their own
# module globals (TIERS, JWT_CONFIG, _jwks_cache), so a monkeypatch must target
# THIS module, not srv — server.py only re-exports the callables its request
# path uses.
import access_identity as ident

# Throwaway 1024-bit RSA keypair (test-only; never a real signing key).
N = 155618387791555344105267757618989010656302111508729641003043183980377213802200848908194388759449544112033081658622820404132486581962583919814951616322642147009506193688691752507590190471206838224368987828138305043145949320888659676409298834147118199951912009988124361150326270105447792291907437622267114791033
E = 65537
D = 76908066621613531901452880075704335965133727354871970679884121731852809524993260223804996529133333601547820038163701879387935180175872111614606526100890423597892697857397753786464021123864356627024440873334204290879191793746513823347918549299669396799967291673342951379218201061802377560252766834083097850113

TEAM = "convivy.cloudflareaccess.com"
AUD = "test-aud-tag"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _sign(msg: bytes) -> bytes:
    """RS256 sign with the test private key, matching server._rsa_pkcs1_verify's
    expected EMSA-PKCS1-v1_5 encoding."""
    k = (N.bit_length() + 7) // 8
    t = ident._SHA256_DIGESTINFO + hashlib.sha256(msg).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    sig_int = pow(int.from_bytes(em, "big"), D, N)
    return sig_int.to_bytes(k, "big")


def make_jwt(*, email="operator@example.com", iss=None, aud=AUD, exp_delta=3600,
             nbf_delta=-60, kid="testkid", alg="RS256", tamper=False) -> str:
    iss = iss if iss is not None else f"https://{TEAM}"
    header = {"alg": alg, "kid": kid}
    now = time.time()
    payload = {"email": email, "iss": iss, "aud": aud,
               "exp": now + exp_delta, "nbf": now + nbf_delta}
    signing_input = f"{_b64u(json.dumps(header).encode())}.{_b64u(json.dumps(payload).encode())}"
    sig = _sign(signing_input.encode("ascii"))
    if tamper:
        sig = bytes([sig[0] ^ 0xFF]) + sig[1:]
    return f"{signing_input}.{_b64u(sig)}"


@pytest.fixture(autouse=True)
def _patch_jwks(monkeypatch):
    """Serve the test public key for any kid — isolates verify from the network."""
    monkeypatch.setattr(ident, "_jwks_key", lambda team, kid: (N, E))


# --- verify_access_jwt: the security core ------------------------------------


def test_valid_jwt_verifies():
    ok, claims, reason = ident.verify_access_jwt(make_jwt(), TEAM, AUD)
    assert ok and reason == "ok" and claims["email"] == "operator@example.com"


def test_tampered_signature_rejected():
    ok, _, reason = ident.verify_access_jwt(make_jwt(tamper=True), TEAM, AUD)
    assert not ok and reason == "bad-signature"


def test_expired_jwt_rejected():
    ok, _, reason = ident.verify_access_jwt(make_jwt(exp_delta=-120), TEAM, AUD)
    assert not ok and reason == "expired"


def test_not_yet_valid_jwt_rejected():
    ok, _, reason = ident.verify_access_jwt(make_jwt(nbf_delta=3600), TEAM, AUD)
    assert not ok and reason == "not-yet-valid"


def test_wrong_issuer_rejected():
    ok, _, reason = ident.verify_access_jwt(make_jwt(iss="https://evil.example"), TEAM, AUD)
    assert not ok and reason == "bad-iss"


def test_wrong_audience_rejected():
    ok, _, reason = ident.verify_access_jwt(make_jwt(aud="some-other-app"), TEAM, AUD)
    assert not ok and reason == "bad-aud"


def test_non_rs256_alg_rejected():
    # an attacker downgrading alg (e.g. to "none"/HS256) must not slip through
    ok, _, reason = ident.verify_access_jwt(make_jwt(alg="none"), TEAM, AUD)
    assert not ok and reason.startswith("alg=")


def test_malformed_token_rejected():
    ok, _, reason = ident.verify_access_jwt("not.a.jwt.at.all", TEAM, AUD)
    assert not ok
    ok2, _, reason2 = ident.verify_access_jwt("only-one-part", TEAM, AUD)
    assert not ok2 and reason2 == "malformed"


def test_empty_aud_config_skips_aud_check():
    # monitor phase: no aud configured → aud isn't enforced, but sig/iss/exp still are
    ok, _, reason = ident.verify_access_jwt(make_jwt(aud="anything"), TEAM, "")
    assert ok and reason == "ok"


def test_unknown_kid_fails_closed(monkeypatch):
    monkeypatch.setattr(ident, "_jwks_key", lambda team, kid: None)
    ok, _, reason = ident.verify_access_jwt(make_jwt(), TEAM, AUD)
    assert not ok and reason == "unknown-kid"


# --- jwt_config: fail-closed on a missing/unreadable/malformed file ----------


def test_jwt_config_missing_file_enforces(tmp_path, monkeypatch):
    """A missing jwt.json (e.g. the deploy area an unset ORIENT_CONFIG_ROOT
    falls back to, issue_01M1F1H8RAQD2JGWQGDGW10M0B) must return enforce=True,
    not the field's normal False default — enforce=False is what makes
    _identity() trust the plaintext header with no cryptographic check at
    all, so a read failure defaulting to it would silently drop into the
    weaker of the two modes on exactly the path meant to require
    verification."""
    monkeypatch.setattr(ident, "JWT_CONFIG", tmp_path / "does-not-exist.json")
    cfg = ident.jwt_config()
    assert cfg["enforce"] is True
    assert cfg["team_domain"] == "" and cfg["aud"] == ""


def test_jwt_config_malformed_file_enforces(tmp_path, monkeypatch):
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text("{ not json")
    monkeypatch.setattr(ident, "JWT_CONFIG", jwtc)
    cfg = ident.jwt_config()
    assert cfg["enforce"] is True


def test_missing_jwt_config_refuses_header_only_request(tmp_path, monkeypatch):
    """End-to-end: with jwt.json missing, a request carrying only the
    plaintext email header (no JWT) must be refused, not silently accepted
    in monitor mode — this is the scenario the BLOCKING finding on PR #257
    identified as strictly worse than the pre-fix stale-file behavior."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.full.html").write_text("FULL-BUNDLE __WHOAMI__")
    (site / "index.contractor.html").write_text("CONTRACTOR-BUNDLE __WHOAMI__")
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": ["operator@example.com"]}))
    monkeypatch.setattr(srv, "SITE", site)
    monkeypatch.setattr(ident, "TIERS", tiers)
    monkeypatch.setattr(ident, "JWT_CONFIG", tmp_path / "does-not-exist.json")
    monkeypatch.setattr(srv, "ACCESS_LOG", tmp_path / "access.log")
    with TestClient(srv.app, follow_redirects=False) as client:
        r = client.get("/orientation", headers={srv.EMAIL_HEADER: "operator@example.com"})
        assert r.status_code == 403
        # a validly-signed JWT is refused too: jwt_config() can't supply a
        # team_domain when the file is absent, so verify_access_jwt() fails
        # closed ("no-team-domain") rather than trusting any team's tokens.
        r2 = client.get("/orientation", headers={srv.JWT_HEADER: make_jwt()})
        assert r2.status_code == 403


# --- _rsa_pkcs1_verify: the primitive ----------------------------------------


def test_rsa_verify_accepts_good_and_rejects_bad():
    msg = b"the.signing.input"
    sig = _sign(msg)
    assert ident._rsa_pkcs1_verify(N, E, msg, sig) is True
    assert ident._rsa_pkcs1_verify(N, E, b"different", sig) is False
    assert ident._rsa_pkcs1_verify(N, E, msg, sig[:-1]) is False  # wrong length


# --- tier membership ----------------------------------------------------------


def test_full_emails_reads_tiers(tmp_path, monkeypatch):
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": ["Operator@Example.com", "contributor@example.com"]}))
    monkeypatch.setattr(ident, "TIERS", tiers)
    emails = ident.full_emails()
    assert "operator@example.com" in emails  # lowercased
    assert "emily@example.com" not in emails


def test_full_emails_missing_file_grants_no_full_tier(tmp_path, monkeypatch):
    """An absent tiers.json (e.g. a deploy area an unset ORIENT_CONFIG_ROOT
    falls back to, issue_01M1F1H8RAQD2JGWQGDGW10M0B) must resolve to nobody
    holding the full tier, not raise or silently grant it — the same
    fail-safe direction as test_missing_roles_file_denies_all in
    test_authz.py."""
    monkeypatch.setattr(ident, "TIERS", tmp_path / "does-not-exist.json")
    assert ident.full_emails() == set()
    assert ident.tier_for("operator@example.com") == "contractor"


# --- /dashboard tier gate (in-process integration) ---------------------------


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """A TestClient over the real FastAPI app, in monitor mode (header-trusted),
    with tmp site/ + tiers.json — exercises the actual request handlers.
    follow_redirects=False so the 302 routes are observed, not followed."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.full.html").write_text("FULL-BUNDLE __WHOAMI__")
    (site / "index.contractor.html").write_text("CONTRACTOR-BUNDLE __WHOAMI__")
    # ARCH-216 Phase 2: home_build renders live; static fallbacks for when it throws.
    (site / "home.full.html").write_text("HOME-FULL-STATIC __WHOAMI__")
    (site / "home.contractor.html").write_text("HOME-CONTRACTOR-STATIC __WHOAMI__")
    # The full-only pages render live (ARCH-182). Stub each builder's live_html()
    # so routing tests don't depend on real data files; the static .html files
    # below are the fallback the server serves only if a live render throws.
    (site / "overview.html").write_text("OVERVIEW-STATIC-FALLBACK")
    (site / "spending.html").write_text("SPENDING-STATIC-FALLBACK")
    (site / "cost-performance.html").write_text("COSTPERF-STATIC-FALLBACK")
    (site / "velocity.html").write_text("VELOCITY-STATIC-FALLBACK")
    (site / "health.html").write_text("HEALTH-STATIC-FALLBACK")
    (site / "kb.html").write_text("KB-STATIC-FALLBACK")
    (site / "engineering.html").write_text("ENG-STATIC-FALLBACK")
    (site / "engineering-arch-log.html").write_text("ARCHLOG-STATIC-FALLBACK")
    (site / "alerts.html").write_text("ALERTS-STATIC-FALLBACK")
    (site / "engineering-briefs.html").write_text("BRIEFS-STATIC-FALLBACK")
    (site / "roster.html").write_text("ROSTER-STATIC-FALLBACK")
    # issue_01M2KVHTVFF4MAQF94M2SP6TZ0: /crews and /chart-house had no builder
    # at all, so a live-render throw had no fallback to serve and 500'd.
    (site / "crews.html").write_text("CREWS-STATIC-FALLBACK")
    (site / "chart-house.html").write_text("CHARTHOUSE-STATIC-FALLBACK")
    # Home page renders live via home_build; stub it out.
    monkeypatch.setattr(
        srv.home_build, "live_html",
        # `email` is accepted because server.py passes it by keyword (Phase 4a scopes
        # the waiting-strip alert count to the viewer). A stub that omits it raises
        # TypeError, which the route swallows into its static fallback — so the test
        # silently stops exercising the live path instead of failing loudly.
        lambda tier, label="", email=None: f"HOME-{tier.upper()}-LIVE __WHOAMI__")
    monkeypatch.setattr(srv.overview_build, "live_html", lambda: "OVERVIEW-LIVE")
    monkeypatch.setattr(srv.dashboard_build, "spending_live_html", lambda: "SPENDING-LIVE")
    monkeypatch.setattr(srv.dashboard_build, "cost_performance_live_html", lambda: "COSTPERF-LIVE")
    monkeypatch.setattr(srv.velocity_build, "live_html", lambda: "VELOCITY-LIVE")
    monkeypatch.setattr(srv.code_health_build, "live_html", lambda: "HEALTH-LIVE")
    monkeypatch.setattr(srv.kb_build, "live_html", lambda: "KB-LIVE")
    monkeypatch.setattr(srv.alerts_build, "live_html", lambda: "ALERTS-LIVE")
    # Engineering pages carry __WHOAMI__ in their header (unlike the dashboards), so
    # the live render must go through the same identity substitution (ARCH-194).
    monkeypatch.setattr(srv.engineering_build, "live_html", lambda: "ENG-LIVE __WHOAMI__")
    monkeypatch.setattr(srv.engineering_build, "arch_log_html", lambda: "ARCHLOG-LIVE __WHOAMI__")
    monkeypatch.setattr(srv.roster_build, "live_html", lambda: "ROSTER-LIVE __WHOAMI__")
    # issue_01M2KVHTVFF4MAQF94M2SP6TZ0: /crews and /chart-house had no builder
    # at all, so a live-render throw had no fallback to serve and 500'd.
    monkeypatch.setattr(srv.crews_build, "live_html", lambda: "CREWS-LIVE")
    monkeypatch.setattr(srv.chart_house_build, "live_html", lambda: "CHARTHOUSE-LIVE")
    # (reports_live_html and notes_live_html are gone — Reports absorbed into
    # Notes and then Notes itself retired, the operator 2026-08-26. alerts_live_html is
    # gone too — /alerts is now a plain Dashboards page, stubbed above with
    # the other Dashboards builders.)
    # Briefings is stubbed alongside its now-gone ops_reports siblings for the
    # same reason they were: calling the real builder made the test pass only
    # on a machine that already had the right local store populated and fail
    # in CI on a fresh one — a green local suite hiding a red CI. The
    # underlying store changed 2026-08-26 too: it now reads the KB index
    # (~/.knowledge-index/index.db) rather than the control DB, so leaving it
    # live would swap one machine-only dependency for another (this machine
    # has an index; a fresh CI checkout has none).
    monkeypatch.setattr(srv.ops_reports_build, "briefings_live_html", lambda: "BRIEFINGS-LIVE")
    # The queue page is control-DB-backed like the builders above, and was the
    # only one this fixture had never stubbed — so it 500'd on a fresh DB. Nothing
    # noticed until test_moved_destinations_all_terminate started walking /bugs
    # through to its destination.
    monkeypatch.setattr(srv.issue_build, "live_html", lambda: "ISSUES-LIVE")
    # Same class of gap, same discovery mechanism: /signals and its two
    # Archives siblings (Library-backed, ARCH-349/archives_build.py) were
    # never stubbed either, so their /archives destinations 500'd the moment
    # issue_01M1A678RA8CVDNNMZC6777NNA's rename made
    # test_moved_destinations_all_terminate walk them for the first time.
    monkeypatch.setattr(srv.signals_build, "live_html", lambda: "SIGNALS-LIVE")
    monkeypatch.setattr(srv.archives_build, "prs_live_html", lambda: "PRS-LIVE")
    monkeypatch.setattr(srv.archives_build, "changelogs_live_html", lambda: "CHANGELOGS-LIVE")
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": ["operator@example.com"]}))
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))  # monitor: trust the email header
    monkeypatch.setattr(srv, "SITE", site)
    monkeypatch.setattr(ident, "TIERS", tiers)
    monkeypatch.setattr(ident, "JWT_CONFIG", jwtc)
    monkeypatch.setattr(srv, "ACCESS_LOG", tmp_path / "access.log")
    with TestClient(srv.app, follow_redirects=False) as client:
        yield client


def _get(client, path, email=None):
    headers = {srv.EMAIL_HEADER: email} if email else {}
    r = client.get(path, headers=headers)
    return r.status_code, r.text


def test_one_signal_has_a_page_at_its_own_url(live_server, monkeypatch):
    """A signal id appears in ADRs, issues and messages, and resolved to nothing
    until this route existed — /archives renders a 600-char excerpt with no link,
    so a long body had no reachable form on the site at all."""
    seen = {}

    def _detail(signal_id, *, viewer_tier):
        seen["id"] = signal_id
        seen["tier"] = viewer_tier
        return f"SIGNAL-PAGE {signal_id} __WHOAMI__"

    monkeypatch.setattr(srv.signals_build, "detail_html", _detail)
    code, text = _get(live_server, "/archives/sig_01ABC", "operator@example.com")
    assert code == 200
    assert seen["id"] == "sig_01ABC"      # the id reaches the renderer intact
    assert "SIGNAL-PAGE sig_01ABC" in text
    assert seen["tier"] == "full"   # the route passes the tier it gated on
    assert "__WHOAMI__" not in text       # identity substituted


def test_an_unknown_signal_id_is_a_404_not_a_500(live_server, monkeypatch):
    monkeypatch.setattr(srv.signals_build, "detail_html", lambda signal_id, **kw: None)
    code, _ = _get(live_server, "/archives/sig_nope", "operator@example.com")
    assert code == 404


def test_a_customer_class_signal_is_indistinguishable_from_a_missing_one(live_server, monkeypatch):
    """detail_html returns None for BOTH, and the route must not tell them apart.
    The full tier is broader than the signal_customer capability, so a viewer the
    API would 403 must not read customer content off a server-rendered page — and
    a distinguishable response would still confirm the id exists."""
    monkeypatch.setattr(srv.signals_build, "detail_html", lambda signal_id, **kw: None)
    customer, missing = _get(live_server, "/archives/sig_customer", "operator@example.com"), \
        _get(live_server, "/archives/sig_missing", "operator@example.com")
    assert customer[0] == missing[0] == 404
    assert customer[1] == missing[1]      # byte-identical, no oracle


def test_a_signal_page_render_failure_is_a_500_not_a_stale_snapshot(live_server, monkeypatch):
    """A per-id page has no static fallback; serving the browse snapshot would
    render one signal's URL as a different signal."""
    def _boom(signal_id, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(srv.signals_build, "detail_html", _boom)
    code, _ = _get(live_server, "/archives/sig_x", "operator@example.com")
    assert code == 500


def test_a_signal_page_leaks_nothing_to_a_contractor(live_server, monkeypatch):
    """Full-tier only, like the browse page it is reached from — and the renderer
    is never reached, so the tier 404 cannot be told from a nonexistent id."""
    called = []
    monkeypatch.setattr(srv.signals_build, "detail_html",
                        lambda signal_id, **kw: called.append(signal_id) or "PAGE")
    code, _ = _get(live_server, "/archives/sig_01ABC", "emily@example.com")
    assert code == 404
    assert called == []


def test_a_trailing_slash_on_signals_lands_on_the_browse_page(live_server):
    r = live_server.get("/archives/", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 302 and r.headers["location"] == "/archives"


def test_old_signal_prefix_redirects_to_the_new_one(live_server):
    """issue_01M1A678RA8CVDNNMZC6777NNA: /signals/<id> is not a `_MOVED` entry
    (the id is dynamic), so it needs its own redirect — every old permalink,
    in a document or a message, must keep resolving."""
    r = live_server.get("/signals/sig_01ABC", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 302 and r.headers["location"] == "/archives/sig_01ABC"


def test_old_signal_prefix_bare_trailing_slash_redirects_to_the_new_landing(live_server):
    r = live_server.get("/signals/", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 302 and r.headers["location"] == "/archives"


def test_old_signal_prefix_redirect_is_gated_on_full_tier(live_server):
    r = live_server.get("/signals/sig_01ABC", headers={srv.EMAIL_HEADER: "emily@example.com"})
    assert r.status_code == 404


def test_a_known_project_has_a_page_at_its_own_url(live_server, monkeypatch):
    """Epic issue_01M0AK8CM3ADKDTG8PM6GRCP8H, item 5: open PRs, recently merged
    PRs and recent commits, all readable without waiting on the Library."""
    seen = {}

    def _detail(name):
        seen["name"] = name
        return f"PROJECT-PAGE {name} __WHOAMI__"

    monkeypatch.setattr(srv.project_build, "detail_html", _detail)
    code, text = _get(live_server, "/projects/vivy", "operator@example.com")
    assert code == 200
    assert seen["name"] == "vivy"
    assert "PROJECT-PAGE vivy" in text
    assert "__WHOAMI__" not in text  # identity substituted


def test_an_unknown_project_name_is_a_404_not_a_500(live_server, monkeypatch):
    monkeypatch.setattr(srv.project_build, "detail_html", lambda name: None)
    code, _ = _get(live_server, "/projects/not-a-real-project", "operator@example.com")
    assert code == 404


def test_a_project_page_render_failure_is_a_500_not_a_stale_snapshot(live_server, monkeypatch):
    """A per-project page has no static fallback; serving some other snapshot
    would render one project's URL as a different project's state."""
    def _boom(name):
        raise RuntimeError("gh down")

    monkeypatch.setattr(srv.project_build, "detail_html", _boom)
    code, _ = _get(live_server, "/projects/vivy", "operator@example.com")
    assert code == 500


def test_a_project_page_leaks_nothing_to_a_contractor(live_server, monkeypatch):
    """Full-tier only, like /engineering/merge-queue — this shells out to `gh`."""
    called = []
    monkeypatch.setattr(srv.project_build, "detail_html",
                        lambda name: called.append(name) or "PAGE")
    code, _ = _get(live_server, "/projects/vivy", "emily@example.com")
    assert code == 404
    assert called == []


def test_a_trailing_slash_on_projects_is_a_404(live_server):
    code, _ = _get(live_server, "/projects/", "operator@example.com")
    assert code == 404


def test_one_document_has_a_page_at_its_own_url(live_server, monkeypatch):
    """issue_01M08YBCDTEFJ6N9S798ERF89P step 3: a document id appears in
    Home's recent-docs panel and has never resolved to its own page — every
    read went through /reference's whole bundle instead."""
    seen = {}

    def _detail(doc_id):
        seen["id"] = doc_id
        return f"DOC-PAGE {doc_id} __WHOAMI__"

    monkeypatch.setattr(srv.reference_doc_build, "detail_html", _detail)
    code, text = _get(live_server, "/reference/doc/proj/kind/doc-1", "operator@example.com")
    assert code == 200
    # the id's own internal slashes reach the builder intact, not truncated
    # at the first segment
    assert seen["id"] == "proj/kind/doc-1"
    assert "DOC-PAGE proj/kind/doc-1" in text
    assert "__WHOAMI__" not in text  # identity substituted


def test_an_unknown_document_id_is_a_404_not_a_500(live_server, monkeypatch):
    monkeypatch.setattr(srv.reference_doc_build, "detail_html", lambda doc_id: None)
    code, _ = _get(live_server, "/reference/doc/proj/kind/nope", "operator@example.com")
    assert code == 404


def test_a_document_page_builder_bug_is_a_500_not_a_stale_snapshot(live_server, monkeypatch):
    """A per-id page has no static fallback. This exercises a bug in the
    builder itself, not a Library outage — reference_doc_build.detail_html
    catches a Library failure internally and renders ds.error_state instead
    of raising, so this route's except-block is defense against a genuine
    bug rather than the expected degrade path."""
    def _boom(doc_id):
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(srv.reference_doc_build, "detail_html", _boom)
    code, _ = _get(live_server, "/reference/doc/proj/kind/doc-1", "operator@example.com")
    assert code == 500


def test_a_document_page_leaks_nothing_to_a_contractor(live_server, monkeypatch):
    """Full-tier only, like /reference — the renderer is never reached, so the
    tier 404 cannot be told from a nonexistent id."""
    called = []
    monkeypatch.setattr(srv.reference_doc_build, "detail_html",
                        lambda doc_id: called.append(doc_id) or "PAGE")
    code, _ = _get(live_server, "/reference/doc/proj/kind/doc-1", "emily@example.com")
    assert code == 404
    assert called == []


def test_a_trailing_slash_on_reference_doc_is_a_404(live_server):
    code, _ = _get(live_server, "/reference/doc/", "operator@example.com")
    assert code == 404


def test_signals_substitutes_can_capture_by_role(live_server, tmp_path, monkeypatch):
    # __CAN_CAPTURE__ drives the /archives log-a-signal form the same way
    # __CAN_DECIDE__ drives the decide controls: "true" for signal_write roles
    # (the contributor role), "false" for observer. Cosmetic — the POST
    # /api/signals authz gate is the wall.
    (tmp_path / "tiers.json").write_text(json.dumps(
        {"full": ["k@example.com", "obs@example.com"]}))
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "k@example.com": {"role": "contributor", "products": ["*"]},
        "obs@example.com": {"role": "observer", "products": ["*"]},
    }))
    monkeypatch.setattr(srv.authz, "ROLES", roles)
    monkeypatch.setattr(srv.signals_build, "live_html",
                        lambda: 'SIGNALS-PAGE data-can-capture="__CAN_CAPTURE__"')
    code, text = _get(live_server, "/archives", "k@example.com")
    assert code == 200 and 'data-can-capture="true"' in text
    code, text = _get(live_server, "/archives", "obs@example.com")
    assert code == 200 and 'data-can-capture="false"' in text


def test_health_substitutes_can_alerts_write_by_role(live_server, tmp_path, monkeypatch):
    """__CAN_ALERTS_WRITE__ (issue_01M17W6XGXVHNDWCFRTQ38Y2RK) gates the
    alert-threshold write form on /health, /spending and /kb the same way
    __CAN_POLICY_WRITE__ gates policy's: "true" for a role holding
    alerts_policy_write (admin-only), "false" for one that does not
    (contractor). All three pages render through `_full_only_routes()`,
    so this one substitution — proven here on /health — covers every one of
    them; alerts_policy_api's own gate is the actual wall."""
    (tmp_path / "tiers.json").write_text(json.dumps(
        {"full": ["admin2@example.com", "contractor2@example.com"]}))
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "admin2@example.com": {"role": "admin", "products": ["*"]},
        "contractor2@example.com": {"role": "contractor", "products": []},
    }))
    monkeypatch.setattr(srv.authz, "ROLES", roles)
    monkeypatch.setattr(srv.code_health_build, "live_html",
                        lambda: 'HEALTH-PAGE data-can-alerts-write="__CAN_ALERTS_WRITE__"')
    code, text = _get(live_server, "/health", "admin2@example.com")
    assert code == 200 and 'data-can-alerts-write="true"' in text
    code, text = _get(live_server, "/health", "contractor2@example.com")
    assert code == 200 and 'data-can-alerts-write="false"' in text


def test_spending_and_cost_performance_served_to_full_tier(live_server):
    # the two split dashboards (ARCH-191) each render live per request (ARCH-182)
    for path, marker in [("/spending", "SPENDING-LIVE"), ("/cost-performance", "COSTPERF-LIVE")]:
        code, body = _get(live_server, path, email="operator@example.com")
        assert code == 200 and marker in body, path


def test_dashboard_redirects_to_spending_for_full_tier(live_server):
    # the old combined route 302-redirects to /spending so bookmarks still land
    r = live_server.get("/dashboard", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 302 and r.headers["location"] == "/spending"


def test_old_decisions_path_is_gone(live_server):
    """/decisions used to 302 to /engineering/decisions (ARCH-362); the decision
    plane is removed (2026-08-18) and neither path answers any more, for any
    tier — a plain 404, not a redirect to a page that no longer exists."""
    for email in ("operator@example.com", "emily@example.com"):
        r = live_server.get("/decisions", headers={srv.EMAIL_HEADER: email})
        assert r.status_code == 404 and r.headers.get("location") is None
        r = live_server.get("/engineering/decisions", headers={srv.EMAIL_HEADER: email})
        assert r.status_code == 404 and r.headers.get("location") is None


def test_old_reports_and_notes_paths_are_gone(live_server):
    """Reports retired into Notes (the operator 2026-07-19); Notes itself retired
    (the operator, 2026-08-26). /memory/reports and /engineering/reports used to
    redirect to /engineering/notes — now that destination is gone too, so all
    three answer a plain 404, for every tier, same as /decisions
    (test_old_decisions_path_is_gone) and the ingest routes (handle_post): a
    redirect ending in a 404 misleads a reader into thinking something moved
    rather than telling them plainly that it's gone."""
    for path in ("/memory/reports", "/engineering/reports", "/engineering/notes"):
        for email in ("operator@example.com", "emily@example.com"):
            r = live_server.get(path, headers={srv.EMAIL_HEADER: email})
            assert r.status_code == 404 and r.headers.get("location") is None, (
                f"{path} ({email}) -> {r.status_code}")


def test_market_path_is_gone(live_server):
    """/market rendered zero cards — no customer-class signal traffic reaches
    it — and is retired (the operator, 2026-08-26). A plain 404 for every tier, same
    precedent as the other fully-retired routes above."""
    for email in ("operator@example.com", "emily@example.com"):
        r = live_server.get("/market", headers={srv.EMAIL_HEADER: email})
        assert r.status_code == 404 and r.headers.get("location") is None, (
            f"/market ({email}) -> {r.status_code}")


def test_dashboard_hidden_from_contractor_no_redirect(live_server):
    # a contractor must get a plain 404 at the old route — not even a redirect leaks it
    r = live_server.get("/dashboard", headers={srv.EMAIL_HEADER: "emily@example.com"})
    assert r.status_code == 404 and r.headers.get("location") is None


def test_full_only_pages_render_live(live_server):
    for path, marker in [("/velocity", "VELOCITY-LIVE"), ("/health", "HEALTH-LIVE"),
                         ("/kb", "KB-LIVE")]:
        code, body = _get(live_server, path, email="operator@example.com")
        assert code == 200 and marker in body, path


def test_engineering_pages_render_live_and_substitute_whoami(live_server):
    # ARCH-194: the KB browse + ARCH log render live AND get the __WHOAMI__
    # identity substitution (regression — the full-only path used to skip it).
    # They live under /reference since the Memory IA move (ARCH-355) and the
    # issue_01M1A678RA8CVDNNMZC6777NNA rename off /memory.
    for path, marker in [("/reference", "ENG-LIVE"),
                         ("/reference/arch-log", "ARCHLOG-LIVE")]:
        code, body = _get(live_server, path, email="operator@example.com")
        assert code == 200 and marker in body, path
        assert "__WHOAMI__" not in body, f"{path} left the identity token unsubstituted"
        assert "operator@example.com · full team view" in body, path


def test_engineering_pages_hidden_from_contractor(live_server):
    for path in ("/engineering", "/engineering/arch-log"):
        code, _ = _get(live_server, path, email="emily@example.com")
        assert code == 404, path


def test_briefs_retire_into_the_reading_room(live_server):
    # ARCH-372: briefs are narrative prose and belong in the Reading Room;
    # Engineering becomes an operational plane. BOTH old paths land on /room.
    # This is the fourth placement, and the first where /room actually renders
    # briefs — see the note on the reading-room test for why the previous three
    # failed at the entitlement layer rather than the routing layer.
    for path in ("/engineering/briefs", "/memory/briefs"):
        r = live_server.get(path, headers={srv.EMAIL_HEADER: "operator@example.com"})
        assert r.status_code == 302, f"{path} -> {r.status_code}"
        assert r.headers["location"] == "/room"


def test_no_redirect_in_moved_is_a_loop():
    """R5 REVERSED an existing _MOVED entry rather than adding one. Had it been
    added, /engineering/briefs and /memory/briefs would 302 at each other forever.

    Stated as the general invariant, not the briefs special case: no redirect
    source may also be a redirect destination. An infinite redirect surfaces as a
    browser error rather than a failing assertion, so nothing else catches it.
    """
    sources, destinations = set(srv._MOVED), set(srv._MOVED.values())
    assert not (sources & destinations), \
        f"path is both a redirect source and destination (loop): {sorted(sources & destinations)}"


def test_moved_destinations_all_terminate(live_server):
    """The table-level check above cannot see a destination that is missing
    entirely. Walk every redirect for real and require a rendered page at the
    end — this is what would have caught the ARCH-357 orphaning, where a path
    redirected somewhere that never rendered the content."""
    for old in srv._MOVED:
        r = live_server.get(old, headers={srv.EMAIL_HEADER: "operator@example.com"})
        assert r.status_code == 302, (old, r.status_code)
        dest = live_server.get(r.headers["location"],
                               headers={srv.EMAIL_HEADER: "operator@example.com"})
        assert dest.status_code == 200, (old, r.headers["location"], dest.status_code)


def test_briefings_page_renders_and_is_full_tier_only(live_server):
    """ARCH-362/R2: the two AI briefings split out of Reports onto their own
    Documents page. Tier-gated like the rest of Documents — a contractor gets
    a plain 404, not a redirect that would leak the new route."""
    ok = live_server.get("/reference/briefings", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert ok.status_code == 200 and "BRIEFINGS-LIVE" in ok.text
    no = live_server.get("/reference/briefings", headers={srv.EMAIL_HEADER: "emily@example.com"})
    assert no.status_code == 404
    assert "location" not in {k.lower() for k in no.headers}


def test_moved_engineering_paths_redirect_for_full(live_server):
    # ARCH-355 (Memory IA): the old /engineering homes 302 to the new paths.
    # (Reference-docs + design retired to /room, ARCH-357; briefs restored to
    # /memory/briefs 2026-07-17 — both tested separately.) Destinations moved
    # again off /memory onto /reference (issue_01M1A678RA8CVDNNMZC6777NNA).
    for old, new in [("/engineering", "/reference"),
                     ("/engineering/arch-log", "/reference/arch-log")]:
        r = live_server.get(old, headers={srv.EMAIL_HEADER: "operator@example.com"})
        assert r.status_code == 302 and r.headers["location"] == new, (old, r.status_code)


def test_old_memory_and_signals_paths_redirect_to_the_new_ones(live_server):
    """issue_01M1A678RA8CVDNNMZC6777NNA: /memory and /signals named what fed
    each page, not what it is — the nav has read "Documents" and "Archives"
    all along. Every sub-path moves with its parent."""
    for old, new in [("/memory", "/reference"),
                     ("/memory/arch-log", "/reference/arch-log"),
                     ("/memory/briefings", "/reference/briefings"),
                     ("/signals", "/archives"),
                     ("/signals/prs", "/archives/prs"),
                     ("/signals/changelogs", "/archives/changelogs")]:
        r = live_server.get(old, headers={srv.EMAIL_HEADER: "operator@example.com"})
        assert r.status_code == 302 and r.headers["location"] == new, (old, r.status_code)


def test_every_moved_path_404s_for_contractor(live_server):
    """Every redirect is tier-gated like the page it points at — a contractor must
    not learn a new route from a Location header.

    Driven off `_MOVED` itself rather than a hand-listed pair, so an entry added
    or reversed later is covered the day it lands. The hand-listed version missed
    /memory/briefs, which is the entry ARCH-362/R5 reversed — exactly the edit
    class (a key/value swap) that is hardest to eyeball at review time.
    """
    for old in srv._MOVED:
        r = live_server.get(old, headers={srv.EMAIL_HEADER: "emily@example.com"})
        assert r.status_code == 404, old
        assert "location" not in {k.lower() for k in r.headers}, old


def test_every_full_only_route_404s_for_contractor(live_server):
    """**Driven off `_full_only_routes()` itself, for the reason the `_MOVED`
    test above already records**: a hand-listed version covers the routes
    somebody remembered, and the one it misses is the one that matters.

    Nothing asserted this end to end before. Full-only routes were pinned by
    MEMBERSHIP in the map (`test_changelog_build.py`, `test_merge_queue_build.py`
    and three others all assert `path in server._full_only_routes()`), which
    says the route is registered and says nothing about the gate refusing
    anyone. The shared gate is one `if tier != "full"` in the dispatch, so a
    single test over the whole map covers every route and every route added
    later.

    Raised as a defense-in-depth suggestion by `security-reviewer` on PR #200,
    where `/crews` began rendering one crew's activity to whoever can load the
    page — a disclosure the Library bounds on the assumption that this gate is
    the audience wall."""
    routes = srv._full_only_routes()
    assert routes, "no full-only routes — the map or its accessor moved"
    for path in routes:
        code, _ = _get(live_server, path, email="emily@example.com")
        assert code == 404, path


def test_memory_routes_404_for_contractor(live_server):
    # the surviving canonical routes are full-tier-only, directly — not just via
    # the redirect path (security-review nit, PR #61). Briefs retired (ARCH-357).
    for path in ("/reference", "/reference/arch-log"):
        code, _ = _get(live_server, path, email="emily@example.com")
        assert code == 404, path


def test_roster_page_renders_live_and_substitutes_whoami(live_server):
    code, body = _get(live_server, "/engineering/roster", email="operator@example.com")
    assert code == 200 and "ROSTER-LIVE" in body
    assert "__WHOAMI__" not in body
    assert "operator@example.com · full team view" in body


def test_roster_page_hidden_from_contractor(live_server):
    code, _ = _get(live_server, "/engineering/roster", email="emily@example.com")
    assert code == 404


def test_cost_performance_falls_back_to_static_on_live_error(live_server, monkeypatch):
    # a live-render failure must serve the last static build, never blank the page
    def boom():
        raise RuntimeError("data file corrupt")
    monkeypatch.setattr(srv.dashboard_build, "cost_performance_live_html", boom)
    code, body = _get(live_server, "/cost-performance", email="operator@example.com")
    assert code == 200 and "COSTPERF-STATIC-FALLBACK" in body


def test_crews_falls_back_to_static_on_library_stall(live_server, monkeypatch):
    """Regression test for issue_01M2KVHTVFF4MAQF94M2SP6TZ0: The operator hit a 500
    reading 'crews.html missing — run its build' during a transient Library
    timeout, because nothing had ever built crews.html. A Library read
    failing here must serve the stale page, exactly like every other
    full-only route with a fallback — not a 500."""
    def boom():
        raise TimeoutError("timed out")
    monkeypatch.setattr(srv.crews_build, "live_html", boom)
    code, body = _get(live_server, "/crews", email="operator@example.com")
    assert code == 200 and "CREWS-STATIC-FALLBACK" in body


def test_chart_house_falls_back_to_static_on_library_stall(live_server, monkeypatch):
    """Same gap as /crews (issue_01M2KVHTVFF4MAQF94M2SP6TZ0): chart-house.html
    was never built either, so this route was one Library stall away from the
    same 500."""
    def boom():
        raise TimeoutError("timed out")
    monkeypatch.setattr(srv.chart_house_build, "live_html", boom)
    code, body = _get(live_server, "/chart-house", email="operator@example.com")
    assert code == 200 and "CHARTHOUSE-STATIC-FALLBACK" in body


# --- a stale fallback must say so, not read as a current render ----------------
#
# Found 2026-08-17: the fallback above is silent — a normal 200, no marker, no
# log — so a page that has been failing to render since the last restart is
# indistinguishable from a current one. issue_01M08YAPQ6XRR83JZXEBW2C73X.


def test_a_stale_fallback_response_carries_the_render_header(live_server, monkeypatch):
    def boom():
        raise RuntimeError("data file corrupt")
    monkeypatch.setattr(srv.dashboard_build, "cost_performance_live_html", boom)
    r = live_server.get("/cost-performance", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 200
    assert r.headers[srv.STALE_FALLBACK_HEADER] == "stale-fallback"


def test_a_live_render_response_carries_no_render_header(live_server):
    r = live_server.get("/cost-performance", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 200
    assert srv.STALE_FALLBACK_HEADER not in r.headers


def test_a_stale_fallback_is_logged_to_access_log(live_server, monkeypatch):
    def boom():
        raise RuntimeError("data file corrupt")
    monkeypatch.setattr(srv.dashboard_build, "cost_performance_live_html", boom)
    live_server.get("/cost-performance", headers={srv.EMAIL_HEADER: "operator@example.com"})
    log = srv.ACCESS_LOG.read_text(encoding="utf-8")
    assert "RENDER-FAILED" in log
    assert "cost-performance" in log
    assert "RuntimeError: data file corrupt" in log


def test_home_falls_back_with_the_render_header_too(live_server, monkeypatch):
    # the home route has its own fallback branch (per-tier static bundles),
    # separate from the generic _full_only_routes dispatcher above — pinned
    # independently rather than assumed to share the same fix.
    def boom(tier, label="", email=None):
        raise RuntimeError("home data broken")
    monkeypatch.setattr(srv.home_build, "live_html", boom)
    r = live_server.get("/", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 200 and "HOME-FULL-STATIC" in r.text
    assert r.headers[srv.STALE_FALLBACK_HEADER] == "stale-fallback"
    assert "RENDER-FAILED" in srv.ACCESS_LOG.read_text(encoding="utf-8")


def test_alerts_falls_back_with_the_render_header_too(live_server, monkeypatch):
    # /alerts renders through the generic _full_only_routes dispatcher, like
    # every other Dashboards page (it carries no per-viewer state, unlike the
    # old /engineering/alerts) — pinned the same way spending/cost-performance
    # are, above.
    def boom():
        raise RuntimeError("alerts data broken")
    monkeypatch.setattr(srv.alerts_build, "live_html", boom)
    r = live_server.get("/alerts", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 200 and "ALERTS-STATIC-FALLBACK" in r.text
    assert r.headers[srv.STALE_FALLBACK_HEADER] == "stale-fallback"
    log = srv.ACCESS_LOG.read_text(encoding="utf-8")
    assert "RENDER-FAILED" in log
    assert "alerts" in log
    assert "RuntimeError: alerts data broken" in log


def test_a_no_fallback_render_failure_is_logged_too(live_server, monkeypatch):
    """The pages with no static fallback already answered a visible 500 — this
    pins that the exception itself is now logged too, not just the fact of a
    failure. Blockers has no site/*.html fallback file at all."""
    def boom():
        raise RuntimeError("issue store unreachable")
    monkeypatch.setattr(srv.issue_build, "live_html_blockers", boom)
    r = live_server.get("/engineering/blockers", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 500
    log = srv.ACCESS_LOG.read_text(encoding="utf-8")
    assert "RENDER-FAILED" in log
    assert "blockers" in log
    assert "RuntimeError: issue store unreachable" in log


def test_a_refused_api_call_leaves_a_line_rather_than_no_trace(live_server):
    """The defect this closes: `access_log` was called only from the catch-all
    page handler and the shared auth gate, so a router route's refusal reached
    the browser and left the log looking exactly as it does when the browser
    never sent a request at all (issue_01M1Z882KE85C88GBSB2CS1XJ5). The
    authenticated-but-forbidden 403 the issue names is pinned in
    tests/test_blog_api.py, where roles.json is a fixture rather than the
    operator's real file.

    Asserting the line is PRESENT rather than that no line is missing — an empty
    log is what a broken logger and an idle server have in common."""
    r = live_server.post("/api/blog/drafts", json={"slug": "x", "body": "y"})
    assert r.status_code == 401
    line = [ln for ln in srv.ACCESS_LOG.read_text(encoding="utf-8").splitlines()
            if "/api/blog/drafts" in ln]
    assert len(line) == 1
    fields = line[0].split("\t")
    assert fields[1] == "401"
    assert fields[2] == "(no-email)"      # nobody authenticated
    assert fields[3] == "-"               # no identity kind resolved
    assert fields[4] == "auth=none"
    assert fields[5] == "POST /api/blog/drafts"


def test_every_access_line_ends_with_the_servers_time_in_ms(live_server, monkeypatch):
    """issue_01M388E3Q5GWBJTF2BKTHKD5WT: a slow page and a slow transfer left
    identical lines. The field is last, so the columns every reader indexes
    stay where they were. Both shapes are checked, a page route that logs
    itself and a router route the middleware backstop logs, and the page's
    figure must include its render, so the render is made to take 50 ms."""
    import time as _time
    real = srv.home_build.live_html

    def slow_home(*a, **k):
        _time.sleep(0.05)
        return real(*a, **k)

    monkeypatch.setattr(srv.home_build, "live_html", slow_home)
    assert live_server.get("/", headers={srv.EMAIL_HEADER: "operator@example.com"}).status_code == 200
    assert live_server.post("/api/blog/drafts", json={}).status_code == 401
    lines = srv.ACCESS_LOG.read_text(encoding="utf-8").splitlines()
    page = [ln.split("\t") for ln in lines if ln.split("\t")[5] == "GET /"]
    api = [ln.split("\t") for ln in lines if ln.split("\t")[5] == "POST /api/blog/drafts"]
    assert len(page) == 1 and len(api) == 1
    for fields in (page[0], api[0]):
        assert len(fields) == 7
        assert re.fullmatch(r"ms=\d+", fields[6]), fields[6]
    assert int(page[0][6][3:]) >= 50, "the page's figure must include its render"


def test_activity_nodes_serves_one_tree_to_the_full_tier_only(live_server, tmp_path, monkeypatch):
    """issue_01M388E7WD8C83T5430YS88EJG: /activity fetches a session's tree
    on expand. Full tier gets it; a contractor and an unknown id get 404."""
    import activity_build
    f = tmp_path / "sessions.json"
    f.write_text(json.dumps({"sessions": [{"session_id": "s-1", "nodes": [{"agent": "w"}]}]}))
    monkeypatch.setattr(activity_build, "ACTIVITY_FILE", f)
    r = live_server.get("/activity/nodes?id=s-1", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 200, r.text
    assert r.json() == {"nodes": [{"agent": "w"}]}
    r = live_server.get("/activity/nodes?id=s-1", headers={srv.EMAIL_HEADER: "emily@example.com"})
    assert r.status_code == 404
    assert "w" not in r.text
    r = live_server.get("/activity/nodes?id=nope", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 404


def test_a_page_route_is_logged_exactly_once(live_server):
    """The middleware backstop writes only for a request the route did not log
    itself. Every page route calls `access_log` directly, so a second line here
    would double every entry in the file the moment this shipped — which a
    reader counting page loads would see as twice the traffic."""
    r = live_server.get("/", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 200
    lines = [ln for ln in srv.ACCESS_LOG.read_text(encoding="utf-8").splitlines()
             if ln.split("\t")[5] == "GET /"]
    assert len(lines) == 1


def test_a_public_asset_is_logged_too(live_server):
    """`_serve_asset` returns before the auth gate and never logged either, so
    the icon and service-worker fetches were the other unlogged surface. They
    are low-volume (24h cache, and sw.js only on registration), and a request
    that reached the origin belongs in the origin's request log."""
    r = live_server.get("/sw.js")
    assert r.status_code in (200, 404)     # 404 where assets/ has no sw.js
    assert "GET /sw.js" in srv.ACCESS_LOG.read_text(encoding="utf-8")


def test_crew_runs_page_renders_live_and_passes_query_params_through(live_server, monkeypatch):
    """/crews/runs (issue_01M15C5W1GFGC81CW7YNCB0CP9) is handled outside
    `_full_only_routes()` because it needs `from`/`to`/`product` off the query
    string — that dispatcher only ever calls its renderer with no arguments.
    Pinned here the way the signal/project/document detail routes above pin
    their own query-carrying dynamic pages."""
    seen = {}

    def _runs_html(query):
        seen["query"] = query
        return "CREW-RUNS-LIVE __WHOAMI__"

    monkeypatch.setattr(srv.crews_build, "runs_html", _runs_html)
    r = live_server.get("/crews/runs?product=vivy&from=2026-08-01&to=2026-08-05",
                        headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 200
    assert "CREW-RUNS-LIVE" in r.text
    assert "__WHOAMI__" not in r.text
    assert seen["query"] == {"product": "vivy", "from": "2026-08-01", "to": "2026-08-05"}


def test_crew_runs_page_404s_for_contractor(live_server):
    r = live_server.get("/crews/runs", headers={srv.EMAIL_HEADER: "emily@example.com"})
    assert r.status_code == 404 and r.headers.get("location") is None


def test_crew_runs_page_render_failure_is_a_500_with_no_static_fallback(
        live_server, monkeypatch):
    def boom(query):
        raise RuntimeError("agent-runs corpus unreadable")
    monkeypatch.setattr(srv.crews_build, "runs_html", boom)
    r = live_server.get("/crews/runs", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 500
    log = srv.ACCESS_LOG.read_text(encoding="utf-8")
    assert "RENDER-FAILED" in log
    assert "crews-runs" in log
    assert "RuntimeError: agent-runs corpus unreadable" in log


def test_policy_page_renders_live_and_passes_query_params_through(live_server, monkeypatch):
    """/engineering/policy (issue_01M1F1E59D38JMZ8WJVKH6W1AY) is handled
    outside `_full_only_routes()` for the same reason /crews/runs is — it
    needs `agent`/`crew`/`model` off the query string, which that dispatcher's
    zero-argument call can't carry."""
    seen = {}

    def _live_html(query):
        seen["query"] = query
        return "POLICY-LIVE __WHOAMI__"

    monkeypatch.setattr(srv.policy_build, "live_html", _live_html)
    r = live_server.get("/engineering/policy?agent=scout&crew=vivy&model=claude-sonnet-5",
                        headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 200
    assert "POLICY-LIVE" in r.text
    assert "__WHOAMI__" not in r.text
    assert seen["query"] == {"agent": "scout", "crew": "vivy", "model": "claude-sonnet-5"}


def test_policy_page_404s_for_contractor(live_server):
    r = live_server.get("/engineering/policy", headers={srv.EMAIL_HEADER: "emily@example.com"})
    assert r.status_code == 404 and r.headers.get("location") is None


def test_policy_page_render_failure_is_a_500_with_no_static_fallback(
        live_server, monkeypatch):
    """A Library outage must not fall back to a stale on-disk render — the
    one copy of a deny-list this page is built not to hold — and must be
    visibly different (a 500) from a successful render that found nothing
    explicitly denied."""
    def boom(query):
        raise RuntimeError("Library unreachable")
    monkeypatch.setattr(srv.policy_build, "live_html", boom)
    r = live_server.get("/engineering/policy?agent=scout&crew=vivy&model=claude-sonnet-5",
                        headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 500
    log = srv.ACCESS_LOG.read_text(encoding="utf-8")
    assert "RENDER-FAILED" in log
    assert "engineering-policy" in log
    assert "RuntimeError: Library unreachable" in log


def test_render_log_cannot_be_forged_with_embedded_control_characters(live_server):
    """`page` on the issue/signal-detail branches is built from a caller-
    supplied URL path segment, and the exception text on every branch can
    carry caller-influenced content too. Either could embed a tab or newline
    that forges a new log line or shifts a parser's column boundaries
    (security review, PR #180) — exercised directly against
    `_log_render_failure`, since the ASGI stack itself already refuses a raw
    control character in a request path (confirmed separately: httpx's own
    URL parser rejects one before this server ever sees it) and normalizes
    away what a browser or curl could otherwise be coaxed into sending, so a
    live end-to-end repro of the raw bytes isn't reachable through the test
    client — the sanitization inside `_log_render_failure` is the thing that
    has to hold regardless of how a hostile `page`/exception string arrives."""
    forged_page = "X\t9999\toperator@example.com\tfull\tok\tGET /fake\nEVIL-PAGE"
    forged_exc = RuntimeError("boom\tINJECTED\ncomment: forged")
    srv._log_render_failure(forged_page, forged_exc)
    log = srv.ACCESS_LOG.read_text(encoding="utf-8")
    render_lines = [line for line in log.splitlines() if "RENDER-FAILED" in line]
    assert len(render_lines) == 1          # not split into multiple lines
    line = render_lines[0]
    assert line.count("\t") == 3           # ts, RENDER-FAILED, page, exception — no extra fields
    assert "EVIL-PAGE" in line and "INJECTED" in line   # content survives, just flattened
    assert "9999\toperator@example.com" not in log
    assert "boom\tINJECTED" not in log


def test_tech_debt_route_is_gone(live_server):
    # The tech-debt dashboard was retired (issue_01M2HY3K2SWV10VGZFRWJD34RS):
    # its source counted open GitHub issues, which no longer exist now that
    # work items live in the Library queue, so 404 for every tier, not just
    # the contractor bundle.
    for email in ("operator@example.com", "emily@example.com"):
        code, body = _get(live_server, "/tech-debt", email=email)
        assert code == 404 and "LIVE" not in body


def test_split_dashboards_hidden_from_contractor(live_server):
    # a contractor must get a plain 404 — the routes aren't revealed, no leak
    for path in ("/spending", "/cost-performance"):
        code, body = _get(live_server, path, email="emily@example.com")
        assert code == 404 and "LIVE" not in body, path


def test_home_renders_live_for_full_tier(live_server):
    code, body = _get(live_server, "/", email="operator@example.com")
    assert code == 200 and "HOME-FULL-LIVE" in body, f"got: {body[:200]}"
    assert "__WHOAMI__" not in body
    assert "operator@example.com" in body


def test_home_renders_live_for_contractor(live_server):
    code, body = _get(live_server, "/", email="emily@example.com")
    assert code == 200 and "HOME-CONTRACTOR-LIVE" in body
    assert "FULL" not in body or "CONTRACTOR" in body
    assert "__WHOAMI__" not in body


def test_orientation_retired_redirects_to_room(live_server):
    # Orientation retired (ARCH-357) → the Reading Room, both tiers (un-tier-gated).
    for email in ("operator@example.com", "emily@example.com"):
        r = live_server.get("/orientation", headers={srv.EMAIL_HEADER: email})
        assert r.status_code == 302 and r.headers["location"] == "/room", email


def test_knowledge_redirects_to_room_both_tiers(live_server):
    # the old /knowledge alias 302-redirects to the Reading Room (ARCH-357), both
    # tiers, so existing links land.
    for email in ("operator@example.com", "emily@example.com"):
        r = live_server.get("/knowledge", headers={srv.EMAIL_HEADER: email})
        assert r.status_code == 302 and r.headers["location"] == "/room", email


def test_overview_served_to_full_tier(live_server):
    code, body = _get(live_server, "/overview", email="operator@example.com")
    assert code == 200 and "OVERVIEW-LIVE" in body


def test_overview_hidden_from_contractor(live_server):
    code, _ = _get(live_server, "/overview", email="emily@example.com")
    assert code == 404


def test_home_falls_back_to_static_on_live_error(live_server, monkeypatch):
    def boom(tier, label=""):
        raise RuntimeError("home data broken")
    monkeypatch.setattr(srv.home_build, "live_html", boom)
    code, body = _get(live_server, "/", email="operator@example.com")
    assert code == 200 and "HOME-FULL-STATIC" in body


def test_no_identity_refused(live_server):
    code, _ = _get(live_server, "/dashboard", email=None)
    assert code == 403


def test_enforce_mode_ignores_header_requires_valid_jwt(tmp_path, monkeypatch):
    # enforce=true (ARCH-056 hardened): the plaintext email header is NOT trusted;
    # only a cryptographically verified Cf-Access-Jwt-Assertion grants identity,
    # and the JWT's own email drives the tier. A write plane must never run in the
    # header-trust monitor mode, so this is the load-bearing end-to-end check.
    # (The autouse _patch_jwks fixture serves the test public key to verify.)
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.full.html").write_text("FULL-BUNDLE __WHOAMI__")
    (site / "index.contractor.html").write_text("CONTRACTOR-BUNDLE __WHOAMI__")
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": ["operator@example.com"]}))
    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": True, "team_domain": TEAM, "aud": AUD}))
    monkeypatch.setattr(srv, "SITE", site)
    monkeypatch.setattr(ident, "TIERS", tiers)
    monkeypatch.setattr(ident, "JWT_CONFIG", jwtc)
    monkeypatch.setattr(srv, "ACCESS_LOG", tmp_path / "access.log")
    with TestClient(srv.app, follow_redirects=False) as client:
        # header email alone (no JWT) is refused — the monitor-mode trust is gone
        r = client.get("/orientation", headers={srv.EMAIL_HEADER: "operator@example.com"})
        assert r.status_code == 403
        # a valid signed JWT is accepted (identity resolves), so /orientation
        # reaches its retirement redirect to /room (ARCH-357) rather than the 403.
        r2 = client.get("/orientation", headers={srv.JWT_HEADER: make_jwt()})
        assert r2.status_code == 302 and r2.headers["location"] == "/room"
        # a forged (tampered) JWT is refused even with a good header email present
        r3 = client.get("/orientation", headers={srv.JWT_HEADER: make_jwt(tamper=True),
                                                  srv.EMAIL_HEADER: "operator@example.com"})
        assert r3.status_code == 403


# --- ingest write paths -------------------------------------------------------


def test_ingest_llm_spend_is_gone(live_server):
    """/ingest/llm-spend (ARCH-152) is removed (2026-08-18): Orient should not
    hold a write path over app spend at all. No auth branch, no ledger write —
    the whole route is unhandled, same as any other unmounted path."""
    r = live_server.post("/ingest/llm-spend",
                         json={"model": "m", "provider": "p", "usd": 0.01})
    assert r.status_code == 404
    assert not hasattr(srv, "LLM_SPEND_DIR")  # the module-level constant is gone too


def test_ingest_unknown_path_404(live_server):
    r = live_server.post("/ingest/other", json={"x": 1})
    assert r.status_code == 404


def test_server_header_is_the_app_identifier_not_the_framework(live_server):
    # the Server header advertises convivy-orient, never "uvicorn" (framework leak)
    r = live_server.get("/", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.headers.get("server") == "convivy-orient/1.1"


def test_bugs_redirects_to_engineering_issues_for_full_tier(live_server):
    # ARCH-361: /bugs was renamed to /issues and moved under Engineering; the old
    # path 302s so existing links and bookmarks still land.
    r = live_server.get("/bugs", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 302 and r.headers["location"] == "/engineering/issues"


def test_bugs_hidden_from_contractor_no_redirect(live_server):
    # a contractor gets a plain 404 at the old route — the redirect must not leak
    # the existence of a full-tier page (same rule as the /dashboard move)
    r = live_server.get("/bugs", headers={srv.EMAIL_HEADER: "emily@example.com"})
    assert r.status_code == 404 and r.headers.get("location") is None


def test_engineering_issues_substitutes_can_log_by_role(live_server, tmp_path, monkeypatch):
    # ARCH-361 renamed the token __CAN_LOG_BUG__ -> __CAN_LOG_ISSUE__ and moved
    # the page to /engineering/issues. It gates the Log-an-issue form on "create".
    # member vs contributor is the sharp contrast: contributor CAN capture signals
    # (signal_write) yet still cannot log an issue, because logging needs "create".
    # Cosmetic — the POST /api/issues authz gate is the wall.
    (tmp_path / "tiers.json").write_text(json.dumps(
        {"full": ["m@example.com", "k@example.com"]}))
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "m@example.com": {"role": "member", "products": ["*"]},
        "k@example.com": {"role": "contributor", "products": ["*"]},
    }))
    monkeypatch.setattr(srv.authz, "ROLES", roles)
    monkeypatch.setattr(srv.issue_build, "live_html",
                        lambda: 'ISSUES-PAGE data-can-log="__CAN_LOG_ISSUE__"')
    code, text = _get(live_server, "/engineering/issues", "m@example.com")
    assert code == 200 and 'data-can-log="true"' in text
    code, text = _get(live_server, "/engineering/issues", "k@example.com")
    assert code == 200 and 'data-can-log="false"' in text


# --- nav ↔ route agreement (M1) ----------------------------------------------
#
# `nav.py` was deleted because it was a second, stale copy of the nav that
# nothing rendered. It still listed Orientation, Reference Docs and
# /memory/briefs long after design_system.py had moved on, and the team-site IA
# audit read it as though it were live — filing rail cleanup that was already
# done. Its test file asserted against it and therefore proved nothing.
#
# Two sources of truth remain: design_system.py's nav tables and server.py's
# route table. This test is what keeps them honest. It walks the REAL request
# path, so a nav entry pointing at a dead route fails here rather than in a
# user's browser.


def _nav_tables():
    """design_system's nav tables as {table_name: [href, ...]}.

    Discovered by SHAPE (a list of `(href, label)` pairs) rather than by table
    name, so a sub-strip added later is covered the day it lands instead of
    silently sitting outside the guard.
    """
    import design_system as ds

    tables = {}
    for name, value in vars(ds).items():
        if not isinstance(value, list) or not value:
            continue
        if not all(isinstance(item, tuple) and len(item) == 2
                   and isinstance(item[0], str) and item[0].startswith("/")
                   and isinstance(item[1], str)
                   for item in value):
            continue
        tables[name] = [href for href, _ in value]
    return tables


def _nav_hrefs():
    return sorted({h for hrefs in _nav_tables().values() for h in hrefs})


def test_nav_href_discovery_is_not_vacuous():
    """The discovery above is shape-based, so a refactor could quietly stop
    matching and leave the agreement test passing over an empty set.

    Pinned per TABLE, not by href. An href-only floor cannot notice a table
    dropping out whose single href also appears in another table — the floor
    would still be satisfied by the other copy. _ROOM_SUBNAV and
    _PRODUCTS_SUBNAV retired with the vaults-mirror reshape (epic
    issue_01M0TKX6DW2E1TXGN9QT6K61J5 step 2); _NARROW_LINKS and
    _DOCUMENTS_SUBNAV replaced them.
    """
    tables = _nav_tables()
    for expected in ("_DEFAULT_LINKS", "_NARROW_LINKS", "_DOCUMENTS_SUBNAV",
                     "_DASHBOARD_SUBNAV", "_CHART_HOUSE_SUBNAV", "_ARCHIVES_SUBNAV"):
        assert expected in tables, f"nav href discovery no longer matches {expected}"
    assert "_ROOM_SUBNAV" not in tables and "_PRODUCTS_SUBNAV" not in tables
    hrefs = set(_nav_hrefs())
    for expected in ("/", "/room", "/writing-desk", "/reference", "/overview",
                     "/engineering/roster",
                     "/products/release-notes",      # Chart House sub-strip
                     "/reference/briefings",          # Archives sub-strip
                     "/spending",                    # Dashboards sub-strip
                     "/engineering/merge-queue"):    # Chart House sub-strip
        assert expected in hrefs, f"nav href discovery missed {expected}"
    # Trimmed off the Archives sub-strip (the operator, 2026-08-26): Market's page is
    # retired and the ARCH log lost its nav entry deliberately (still routes,
    # see test_every_full_only_route_has_a_nav_entry's exemption).
    assert "/market" not in hrefs and "/reference/arch-log" not in hrefs


def test_every_nav_href_resolves_to_a_real_route(live_server):
    """No nav entry may point at a path the server does not route.

    Asserted as "not 404" rather than "200" on purpose: live_server stubs only
    some builders, and an unstubbed one whose static fallback is absent yields a
    500. A 500 still proves the route is REGISTERED, which is the property this
    test is about. A 404 means the nav points at nothing.
    """
    headers = {srv.EMAIL_HEADER: "operator@example.com"}  # full tier — sees every area
    for href in _nav_hrefs():
        r = live_server.get(href, headers=headers)
        assert r.status_code != 404, f"nav href {href} resolves to no route"
        # A redirect must land somewhere real too. ARCH-357 retired Briefs into
        # /room while /room never rendered briefs, orphaning the page — a
        # redirect to a dead end is the exact failure mode this catches.
        if r.status_code in (301, 302, 307, 308):
            target = r.headers["location"]
            follow = live_server.get(target, headers=headers)
            assert follow.status_code != 404, (
                f"nav href {href} redirects to {target}, which resolves to no route")


def _overview_card_hrefs():
    """Every dashboard route linked from a card on the grouped Overview index
    (nav-reshape spec §4) — the second real click path a route can have,
    alongside a design_system nav-table entry. Discovered the same way
    `test_no_full_tier_dashboard_is_reachable_only_by_typing_its_url` does."""
    import re
    import overview_build
    return set(re.findall(r'class="overview-card" href="([^"]+)"',
                          overview_build._build_body()))


def test_every_full_only_route_has_a_nav_entry():
    """The reverse direction: a page the server routes must be reachable by
    clicking, not only by knowing its URL.

    The nav→route test above cannot catch this class. A route with no nav entry
    has nothing pointing at it to check. That is how /memory/briefs was orphaned
    twice: ARCH-357 redirected it to /room, which never rendered briefs; the
    2026-07-17 restoration then brought the route back (server.py:322) without
    the nav link, so the page was routed but still unclickable.

    Deliberately kept with NO SILENT exemption list — a route excluded below
    is a decision made explicitly here, not absorbed quietly. `navigable`
    unions in `_overview_card_hrefs()` because §4 gave the Dashboards trim
    (Overview/Spending/Velocity/Security/Alerts) a SECOND real click path for
    the seven it dropped: a card on /overview. That is not an exemption — a
    route earning it must actually appear as a card, which
    `test_no_full_tier_dashboard_is_reachable_only_by_typing_its_url` polices
    on the same regex.

    /reference/arch-log IS an explicit exemption (the operator, 2026-08-26): the ARCH log
    froze at ARCH-441 and lost its front-page space and its Archives strip
    entry, on purpose, but the route stays because 245 KB documents cite an
    ARCH-NNN id and engineering_build serves deep links into it. It is meant
    to keep resolving without being clickable from anywhere in the nav.
    """
    routed = set(srv._full_only_routes()) - {"/reference/arch-log"}
    navigable = set(_nav_hrefs()) | _overview_card_hrefs()
    orphaned = routed - navigable
    assert not orphaned, (
        f"routed but unreachable by clicking: {sorted(orphaned)} — add a nav "
        f"entry in design_system, or retire the route")


def test_nav_component_contractor_dashboards_and_chart_house_substrips():
    """The tier gate is `if tier != "full": pass` ahead of every subnav elif
    branch (Dashboards, Chart House, Crews, Archives, Documents) — one guard
    for all five rather than one per branch, since the narrow tier's rail
    (`_NARROW_LINKS`) never has a subnav to leak in the first place.
    Dashboards and Chart House (which now carries Release Notes, since
    Products dissolved) are exercised here for a contractor `current`.
    """
    import design_system as ds
    for page in ("/spending", "/velocity", "/kb", "/products/release-notes"):
        html = ds.nav_component(page, "contractor")
        assert "atelier-subnav" not in html, f"contractor got a sub-strip on {page}"
        for leaked in ("/spending", "/velocity", "/kb",
                       "/products/release-notes", "/overview"):
            assert f'href="{leaked}"' not in html, f"contractor nav leaked {leaked} on {page}"


def test_no_full_only_route_ever_appears_in_a_contractor_nav():
    """No full-tier-only route may appear in a contractor's nav, from any page.

    This is the PROPERTY; test_every_full_only_route_has_a_nav_entry is only
    structure. The reverse guard requires *some* nav entry, not one in an
    already-gated area — so a future full-only route added straight to the
    primary rail would satisfy it while missing _FULL_ONLY_LINKS membership, and
    leak to contractors. Found by security review of that guard.

    Asserted over every nav path as `current`, because the sub-strip a page
    renders depends on which area _active_area resolves it to.
    """
    import design_system as ds
    full_only = set(srv._full_only_routes())
    for current in _nav_hrefs() + ["/", "/room"]:
        html = ds.nav_component(current, "contractor")
        for route in sorted(full_only):
            assert f'href="{route}"' not in html, (
                f"contractor nav leaked full-only route {route} while on {current}")


def test_alerts_served_from_the_route_table(live_server):
    """/alerts (epic issue_01M0EHRNW5HVJCAXZHYQCBZ3YW) carries no per-viewer
    state — unlike the old /engineering/alerts's ack, a threshold either is or
    isn't crossed regardless of who's looking — so it renders through the plain
    _full_only_routes dispatcher, same as /spending or /health."""
    r = live_server.get("/alerts", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 200 and "ALERTS-LIVE" in r.text


def test_alerts_404s_for_a_contractor(live_server):
    r = live_server.get("/alerts", headers={srv.EMAIL_HEADER: "emily@example.com"})
    assert r.status_code == 404
    assert "location" not in {k.lower() for k in r.headers}


def test_old_alerts_path_redirects_to_the_new_one(live_server):
    """/engineering/alerts (moved under Dashboards) 302s via _MOVED like every
    other retired path, so nothing that linked the old URL 404s."""
    r = live_server.get("/engineering/alerts", headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 302 and r.headers["location"] == "/alerts"


def test_old_alerts_path_404s_for_a_contractor_not_a_redirect(live_server):
    """_MOVED is tier-gated like every full-only route: a contractor gets the
    ordinary 404, not a redirect that would reveal the route exists."""
    r = live_server.get("/engineering/alerts", headers={srv.EMAIL_HEADER: "emily@example.com"})
    assert r.status_code == 404
    assert "location" not in {k.lower() for k in r.headers}


def test_one_issue_has_a_page_at_the_url_it_advertises(live_server, monkeypatch):
    """Every issue carries `url=/engineering/issues/<id>` — `_issue_dict` sets it
    on every row the API has ever returned — and until now nothing served it. The
    path fell through to the catchall 404, so an id quoted in a commit message, a
    PR body or another issue's text was a dead link. Decisions and signals each
    got this page; issues did not."""
    seen = {}

    def _detail(item_id):
        seen["id"] = item_id
        return f"ISSUE-PAGE {item_id} __WHOAMI__"

    monkeypatch.setattr(srv.issue_build, "detail_html", _detail)
    code, text = _get(live_server, "/engineering/issues/issue_01ABC", "operator@example.com")
    assert code == 200
    assert seen["id"] == "issue_01ABC"        # the id reaches the renderer intact
    assert "ISSUE-PAGE issue_01ABC" in text
    assert "__WHOAMI__" not in text           # identity substituted like the queue


def test_a_bare_trailing_slash_on_the_issue_queue_redirects(live_server):
    code, _ = _get(live_server, "/engineering/issues/", "operator@example.com")
    assert code in (301, 302)


def test_an_unknown_issue_id_is_a_404_not_a_500(live_server, monkeypatch):
    """A mistyped URL must read as missing rather than as a broken page."""
    monkeypatch.setattr(srv.issue_build, "detail_html", lambda item_id: None)
    code, _ = _get(live_server, "/engineering/issues/issue_nope", "operator@example.com")
    assert code == 404


def test_an_issue_page_render_failure_is_a_500_not_a_stale_snapshot(live_server,
                                                                    monkeypatch):
    """A per-id page has no static fallback. Falling back to the queue's snapshot
    would show one issue's URL rendering a different issue."""
    def _boom(item_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(srv.issue_build, "detail_html", _boom)
    code, _ = _get(live_server, "/engineering/issues/issue_x", "operator@example.com")
    assert code == 500


# --- the Writing Desk (epic issue_01M0EHQHQ7M39K811XKX1BAG98) ----------------


def test_writing_desk_reachable_by_both_tiers(live_server):
    # Like /room, the Writing Desk is not tier-gated — the blog panel gates on
    # authz role instead (__CAN_BLOG_WRITE__), not on site tier.
    for email in ("operator@example.com", "emily@example.com"):
        code, body = _get(live_server, "/writing-desk", email)
        assert code == 200, (email, code)
        assert "Writing Desk" in body


def test_writing_desk_substitutes_can_blog_write_by_role(live_server, tmp_path, monkeypatch):
    # __CAN_BLOG_WRITE__ works on /writing-desk the same way it did on /room
    # before the panel moved: "true" for a role holding blog_write (member),
    # "false" for a bare contractor role. Cosmetic — the blog_api authz gate on
    # every route is the wall.
    (tmp_path / "tiers.json").write_text(json.dumps(
        {"full": ["m@example.com", "c@example.com"]}))
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "m@example.com": {"role": "member", "products": ["*"]},
    }))
    monkeypatch.setattr(srv.authz, "ROLES", roles)
    code, text = _get(live_server, "/writing-desk", "m@example.com")
    assert code == 200 and 'data-can-blog="true"' in text
    code, text = _get(live_server, "/writing-desk", "c@example.com")
    assert code == 200 and 'data-can-blog="false"' in text


# --- Blog Posts (the operator, 2026-09-24) -------------------------------------------


def test_blog_posts_is_full_tier_and_decides_drafts_from_the_role(live_server, tmp_path,
                                                                  monkeypatch):
    """Full tier like the rest of Documents, so a contractor gets the same plain
    404 as for any full-tier page. Inside the full tier, drafts go to a role
    holding blog_write and to nobody else, the same line `GET /api/blog/drafts`
    draws, and the decision is passed to the builder rather than left to markup."""
    (tmp_path / "tiers.json").write_text(json.dumps(
        {"full": ["m@example.com", "o@example.com"]}))
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "m@example.com": {"role": "member", "products": ["*"]},
        "o@example.com": {"role": "observer", "products": ["*"]},
    }))
    monkeypatch.setattr(srv.authz, "ROLES", roles)
    seen = []
    monkeypatch.setattr(srv.blog_posts_build, "live_html",
                        lambda email, *, can_see_drafts: seen.append((email, can_see_drafts))
                        or "BLOGPOSTS __WHOAMI__")

    code, body = _get(live_server, "/reference/blog-posts", "m@example.com")
    assert code == 200 and "BLOGPOSTS" in body and "__WHOAMI__" not in body
    code, _ = _get(live_server, "/reference/blog-posts", "o@example.com")
    assert code == 200
    assert seen == [("m@example.com", True), ("o@example.com", False)]

    code, body = _get(live_server, "/reference/blog-posts", "c@example.com")
    assert code == 404 and "BLOGPOSTS" not in body
    assert len(seen) == 2, "a contractor must never reach the builder"


# --- the KB editor's role gate on the browse page ----------------------------
# (issue_01M17HYDBQ4GPWHPFS56M6QQPH)


def test_reference_substitutes_can_kb_write_by_role(live_server, tmp_path, monkeypatch):
    """/reference now carries the editor, so it carries `__CAN_KB_WRITE__` too —
    and its copy rides INSIDE the page's JSON payload, which the browse script
    reads. A literal left unfilled there would be read by `canEdit` as a role
    string rather than merely looking wrong on screen, which is why the token
    moved into the shared substitution every full-tier route already runs.

    Cosmetic either way: kb_write_api's own authz gate on every route it calls
    is the wall."""
    (tmp_path / "tiers.json").write_text(json.dumps(
        {"full": ["m@example.com", "o@example.com"]}))
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "m@example.com": {"role": "member", "products": ["*"]},
        "o@example.com": {"role": "observer", "products": ["*"]},
    }))
    monkeypatch.setattr(srv.authz, "ROLES", roles)
    # The payload shape engineering_build actually emits — pinned against the
    # real renderer by `test_the_browse_payload_carries_the_kb_write_placeholder`
    # over in tests/test_engineering_build.py. This half is about the server
    # filling whatever the page emitted.
    monkeypatch.setattr(srv.engineering_build, "live_html",
                        lambda: '<html><script id="data" type="application/json">'
                                '{"canKbWrite": "__CAN_KB_WRITE__"}</script></html>')
    code, text = _get(live_server, "/reference", "m@example.com")
    assert code == 200 and '"canKbWrite": "true"' in text
    code, text = _get(live_server, "/reference", "o@example.com")
    assert code == 200, code
    assert '"canKbWrite": "false"' in text, (
        "a role without kb_write must get the editor withheld")
    assert "__CAN_KB_WRITE__" not in text, (
        "an unsubstituted token would be read as a role string by the script")


def test_reference_doc_page_still_substitutes_can_kb_write(live_server, tmp_path, monkeypatch):
    """The document's own page is handled outside the full-tier route table
    (it takes an id), and its substitution moved from a hand-written pair of
    lines onto the shared helper. That is exactly the lift-out drift the helper
    exists to prevent, so it is pinned here as well as on /reference."""
    (tmp_path / "tiers.json").write_text(json.dumps({"full": ["m@example.com"]}))
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({"m@example.com": {"role": "member", "products": ["*"]}}))
    monkeypatch.setattr(srv.authz, "ROLES", roles)
    monkeypatch.setattr(
        srv.reference_doc_build, "detail_html",
        lambda doc_id: '<html><body data-can-kb-write="__CAN_KB_WRITE__">__WHOAMI__</body></html>')
    code, text = _get(live_server, "/reference/doc/proj%2Fkind%2Fdoc-1", "m@example.com")
    assert code == 200
    assert 'data-can-kb-write="true"' in text
    assert "__WHOAMI__" not in text


@pytest.mark.parametrize("path, target", [
    ("/engineering/issues/issue_x", "detail_html"),
    ("/engineering/blockers", "live_html_blockers"),
])
def test_the_lifted_out_card_routes_fill_can_approve_epic(
        live_server, tmp_path, monkeypatch, path, target):
    """An issue's own page and Blockers are handled outside `_full_only_routes()`
    and used to replace only `__WHOAMI__`. Both render `issue_build._card`, which
    now carries Approve and so `__CAN_APPROVE_EPIC__` — and a literal left there
    would be read by the CSS gate as a role that is neither "true" nor "false".
    Both now go through `_substitute_viewer_tokens`, the helper extracted for
    exactly this lift-out case (issue_01M32CB6A9T04NVYPARGDT2GWQ).

    **The viewer is an ADMIN, and the reason is worth keeping.** This test was
    written when `epic_approve` was held by admin and member, and it read as a
    substitution test while quietly depending on the grant. Narrowing the
    capability to admin (PR #320) turned "true" into "false" and failed it —
    on `main`, after both PRs passed their own CI against their own bases.
    An admin keeps the assertion about the substitution rather than about who
    may approve, which is `test_approve_api.py`'s subject."""
    (tmp_path / "tiers.json").write_text(json.dumps({"full": ["m@example.com"]}))
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({"m@example.com": {"role": "admin", "products": ["*"]}}))
    monkeypatch.setattr(srv.authz, "ROLES", roles)
    page = '<div data-can-approve="__CAN_APPROVE_EPIC__">__WHOAMI__</div>'
    if target == "detail_html":
        monkeypatch.setattr(srv.issue_build, target, lambda _id: page)
    else:
        monkeypatch.setattr(srv.issue_build, target, lambda: page)
    code, text = _get(live_server, path, "m@example.com")
    assert code == 200, (path, code)
    assert 'data-can-approve="true"' in text, path
    assert "__CAN_APPROVE_EPIC__" not in text and "__WHOAMI__" not in text, path


@pytest.mark.parametrize("role, shown", [("admin", "true"), ("member", "false")])
def test_an_item_page_fills_can_set_item_model_by_role(
        live_server, tmp_path, monkeypatch, role, shown):
    """The item page's model control is shown to a role holding `item_model`,
    which is admin alone (issue_01M38WVA7XHEDN1NFNPP8KDM0C). The member case is
    what shows the token is derived from the grant rather than filled "true"."""
    (tmp_path / "tiers.json").write_text(json.dumps({"full": ["m@example.com"]}))
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({"m@example.com": {"role": role, "products": ["*"]}}))
    monkeypatch.setattr(srv.authz, "ROLES", roles)
    monkeypatch.setattr(srv.issue_build, "detail_html",
                        lambda _id: '<section data-can-set="__CAN_SET_ITEM_MODEL__"></section>')
    code, text = _get(live_server, "/engineering/issues/issue_x", "m@example.com")
    assert code == 200, code
    assert f'data-can-set="{shown}"' in text


# --- _log_field routes through logutil (issue_01M0VBZRCA15QSVYFMEF8MZ8N0) -----


def test_log_field_calls_the_shared_sanitiser(monkeypatch):
    # Pinned so a future edit that re-inlines the old copy fails here instead
    # of silently drifting from chart_house_build's copy of the same rule.
    calls = []
    monkeypatch.setattr(srv, "clean_log_field", lambda v: calls.append(v) or "REPLACED")
    assert srv._log_field("value\nwith\tcontrol chars") == "REPLACED"
    assert calls == ["value\nwith\tcontrol chars"]


def test_log_field_replaces_bidi_override_in_a_path_segment():
    # A percent-encoded U+202E in a URL path segment (e.g. doc_id) decodes
    # before _log_render_failure builds `page` — reachable at server.py's
    # f"reference/doc/{doc_id}" (:911), f"engineering-issues/{item_id}" (:731),
    # f"archives/{signal_id}" (:868), and f"projects/{name}" (:943) — and must
    # not flip the rendered direction of the RENDER-FAILED line for whoever
    # reads access.log (issue_01M1KJ4VCR127XJC9SMKPRVGRG).
    doc_id = "‮gnp.eux"
    page = f"reference/doc/{doc_id}"
    assert srv._log_field(page) == "reference/doc/ gnp.eux"


def test_the_corrections_page_passes_sort_through_to_the_renderer(live_server, monkeypatch):
    """THE FAILURE THIS PAGE ALREADY HAD: a read path that silently ignores a
    query parameter answers 200 with the whole queue, so "the grouping is not
    wired up" and "there is nothing to group" look identical to whoever follows
    the link. The operator was handed a `?sort=topic` URL and it did nothing
    (issue_01M22998K9JYPVYDAHQT0EX3G4).

    So this asserts on what the RENDERER WAS CALLED WITH, not on the status
    code — a 200 is what both the working and the broken version return."""
    seen = {}

    def _corrections(query=None):
        seen["query"] = query
        return "CORRECTIONS-LIVE __WHOAMI__ __CAN_LOG_ISSUE__"

    monkeypatch.setattr(srv.issue_build, "live_html_corrections", _corrections)
    r = live_server.get("/engineering/corrections?sort=topic",
                        headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 200
    assert seen["query"] == {"sort": "topic"}

    live_server.get("/engineering/corrections",
                    headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert seen["query"] == {}


def test_the_corrections_page_still_fills_its_viewer_tokens(live_server, monkeypatch):
    """Lifting the route out of `_full_only_routes()` to read a query parameter
    keeps every other property of the page, and this is the one a hand-copied
    handler drops: the dispatcher substitutes `__CAN_LOG_ISSUE__`, the "Log an
    issue" form reads it as `data-can-log`, so an unfilled token breaks the form
    rather than merely looking wrong."""
    monkeypatch.setattr(srv.issue_build, "live_html_corrections",
                        lambda query=None: "__WHOAMI__ can-log=__CAN_LOG_ISSUE__")
    r = live_server.get("/engineering/corrections",
                        headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert "__CAN_LOG_ISSUE__" not in r.text
    assert "__WHOAMI__" not in r.text


def test_a_grouped_corrections_render_never_falls_back_to_the_flat_build(live_server, monkeypatch):
    """The static fallback file is always ungrouped. Serving it for a
    `?sort=topic` request would answer 200 with an ungrouped page — the exact
    "it did nothing" evidence this issue was filed for, produced by a render
    failure instead. A grouped render that throws is a visible 500."""
    (srv.SITE / "corrections.html").write_text("CORRECTIONS-STATIC-FALLBACK")

    def boom(query=None):
        raise RuntimeError("corrections data broken")

    monkeypatch.setattr(srv.issue_build, "live_html_corrections", boom)
    grouped = live_server.get("/engineering/corrections?sort=topic",
                              headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert grouped.status_code == 500
    assert "CORRECTIONS-STATIC-FALLBACK" not in grouped.text

    # The flat view keeps the fallback it has always had.
    flat = live_server.get("/engineering/corrections",
                           headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert flat.status_code == 200
    assert "CORRECTIONS-STATIC-FALLBACK" in flat.text
    assert flat.headers[srv.STALE_FALLBACK_HEADER] == "stale-fallback"


def test_a_severity_sorted_corrections_render_never_falls_back_to_the_flat_build(
        live_server, monkeypatch):
    """Same trap as `?sort=topic`, one sort over: the static fallback is
    always lane-ordered, never worst-first, so serving it for `?sort=severity`
    would answer 200 with the wrong ordering — indistinguishable from a
    working page to whoever followed the link."""
    (srv.SITE / "corrections.html").write_text("CORRECTIONS-STATIC-FALLBACK")

    def boom(query=None):
        raise RuntimeError("corrections data broken")

    monkeypatch.setattr(srv.issue_build, "live_html_corrections", boom)
    r = live_server.get("/engineering/corrections?sort=severity",
                        headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 500
    assert "CORRECTIONS-STATIC-FALLBACK" not in r.text


def test_an_unrecognised_sort_still_gets_the_fallback_on_render_failure(
        live_server, monkeypatch):
    """A typo in `?sort=` must not earn the 500 treatment reserved for the
    sorts this page actually implements — it renders the flat default, so a
    render failure degrades to the flat static fallback like it always has."""
    (srv.SITE / "corrections.html").write_text("CORRECTIONS-STATIC-FALLBACK")

    def boom(query=None):
        raise RuntimeError("corrections data broken")

    monkeypatch.setattr(srv.issue_build, "live_html_corrections", boom)
    r = live_server.get("/engineering/corrections?sort=bogus",
                        headers={srv.EMAIL_HEADER: "operator@example.com"})
    assert r.status_code == 200
    assert "CORRECTIONS-STATIC-FALLBACK" in r.text


def test_the_corrections_page_is_still_full_tier_only(live_server):
    """It was tier-gated by `_full_only_routes()`; a route lifted out of that
    table must carry its own gate, and a contractor must still get a plain 404
    rather than a page that reveals the route exists."""
    r = live_server.get("/engineering/corrections",
                        headers={srv.EMAIL_HEADER: "contractor@example.com"})
    assert r.status_code == 404


# --- the editor's modules (issue_01M35KC8PZMZBMJKX4RZR9F1Y8) -----------------

def test_the_editor_modules_are_served_to_the_full_tier_as_javascript(live_server):
    for name in ("kb-doc.js", "editor.js", "render.js", "md-serial.js"):
        r = live_server.get(f"/editor/js/{name}", headers={srv.EMAIL_HEADER: "operator@example.com"})
        assert r.status_code == 200, name
        assert r.headers["content-type"].startswith("text/javascript"), name
        assert r.headers["cache-control"] == "no-cache"
        assert r.content == srv.EDITOR_JS_FILES[name].read_bytes()


def test_a_contractor_cannot_reach_the_editor_modules(live_server):
    """Editing is full-tier, and the route that serves the editor is not
    revealed to a contractor: the same 404 as a name that does not exist."""
    code, _ = _get(live_server, "/editor/js/editor.js", "emily@example.com")
    assert code == 404


def test_the_editor_route_serves_only_its_allow_list(live_server):
    for path in ("/editor/js/SOURCE.md", "/editor/js/../server.py", "/editor/js/",
                 "/editor/js/test-helpers.js", "/editor/js/kb_editor.py"):
        code, _ = _get(live_server, path, "operator@example.com")
        assert code == 404, path
