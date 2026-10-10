"""Unit tests for library_client.py, the single place Orient talks to the
Library.

Mocks `urllib.request.urlopen` directly rather than standing up a real HTTP
server — these tests are about the client's own contract (the header, the
credential, the timeout, and above all the paging), not about the Library's
implementation, which lives in a separate repo.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

import library_client


class _FakeResponse:
    def __init__(self, body: dict):
        self._buf = io.BytesIO(json.dumps(body).encode("utf-8"))

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _query(url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def _issue_row(n: int) -> dict:
    """A minimal-but-complete `item_dict`-shaped row — every key
    `library_client._issue_record` reads, so a fixture missing one would raise
    where the real Library never would."""
    return {
        "id": f"issue_{n:04d}", "url": f"/engineering/issues/issue_{n:04d}",
        "product": "vivy", "title": f"issue {n}", "body": "", "type": "bug",
        "state": "open", "severity": "unset", "needs": None, "maturity": None,
        "parent_id": None, "topics": [], "refs": {}, "specification": None,
        "definition_of_done": None, "resolution": None, "resolved_by": None,
        "outcome_note": None, "claimed_by": None, "claimed_at": None,
        "claim_expires_at": None, "claim_expired": False, "resolved_at": None,
        "created_by": "overnight-orchestrator", "created_by_kind": "agent",
        "created_at": "2026-08-01T00:00:00Z", "run_id": None,
        "park_reason": None, "unpark_condition": None, "parked_at": None,
        "blocked": False, "prerequisites": [], "model_choice": None, "found_during": None,
    }


def _lease_row(n: int, *, dead: bool = False) -> dict:
    """A minimal-but-complete `lease_dict`-shaped row — every key
    `library_client._lease_record` reads. No `product`: the Library scopes a
    lease through its root item, never a denormalized column on the lease."""
    return {
        "id": f"lease_{n:04d}", "root_id": f"issue_{n:04d}",
        "root_url": f"/engineering/issues/issue_{n:04d}", "taken_for": "build it",
        "holder": "coder", "run_id": f"run-{n:04d}",
        "taken_at": "2026-08-01T00:00:00Z", "heartbeat_at": "2026-08-01T00:05:00Z",
        "expires_at": "2026-08-01T00:30:00Z", "dead": dead,
        "released_at": None, "release_reason": None, "released_by": None,
        "note": None,
    }


def _refusal_row(n: int) -> dict:
    return {"id": f"ref_{n:04d}", "at": "2026-08-08 04:37:22.821295",
            "requested_item_id": "issue_wanted", "product": "keel",
            "requester": "session-B", "actor": "ops-mcp", "blocks_by": "ancestor",
            "blocking_lease_id": "lease_1", "blocking_root_id": "issue_root1",
            "blocking_holder": "session-A"}


def _signal_row(n: int) -> dict:
    """A minimal-but-complete `signal_dict`-shaped row — every key
    `library_client._signal_record` reads."""
    return {
        "id": f"sig_{n:04d}", "source": "ci", "stream": "e2e-stage-smoke",
        "kind": "test-result", "actor": "ci-bot", "ingested_by": "ci-bot",
        "product": "vivy", "body": f"run {n}", "refs": {}, "meta": {},
        "privacy_class": "internal", "produced_at": None,
        "ingested_at": "2026-08-01T00:00:00Z",
    }


@pytest.fixture
def token_env(monkeypatch):
    """BOTH credentials, so no test using this fixture depends on a file
    existing on the machine running it.

    The second one was added after a CI failure this fixture caused. With only
    the agent variable set, `test_signal_acks_request` fell through to
    `CLIENT_TOKEN_FILE` and passed on the laptop that had just minted that
    credential while failing on a runner that had not. `conftest`'s autouse
    `_no_real_library_credentials` now makes that fall-through land in an empty
    temp directory rather than on the operator's real token, and this sets the
    value the ack tests actually want.
    """
    monkeypatch.setenv("LIBRARY_CLIENT_TOKEN", "test-token")
    monkeypatch.setenv("LIBRARY_HUMAN_RESERVED_TOKEN", "test-client-token")


@pytest.fixture(autouse=True)
def _agent_credential_already_verified(monkeypatch):
    """Start every test with the once-per-process credential check already
    satisfied, and un-satisfy it in teardown.

    Two separate reasons, and both are about the check being real rather than
    decorative. Every test in this file mocks `urlopen` with a fake shaped for
    the ONE route it is about, so a live check would make each of those fakes
    also have to answer `/whoami` — forty fixtures carrying a detail forty tests
    are not about. And the flag is module state that would otherwise PERSIST: the
    first test to trigger the check would satisfy it for every test after it in
    the same process, so the check's own tests would pass without running it.

    The check's tests set the flag back to False themselves; `monkeypatch`
    restores it either way, so nothing leaks into the next test.
    """
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFIED", True)


# --- the credential check: Orient reads its own grant and refuses on a wide one
# ---------------------------------------------------------------------------
#
# `signals_build` stopped filtering `privacy_class` when ADR-0022 gave the privacy
# wall to the Library. The capability that decides it is set in a gitignored token
# file no review sees, so nothing in this process could tell a correctly-narrow
# credential from one silently widened — which is the gap `GET /whoami` closes and
# these tests hold shut.


def _whoami_first(capabilities, then, *, kind="agent", actor="orient"):
    """A `urlopen` double answering `/whoami` with `capabilities`, and delegating
    every other path to `then`. Records the paths it saw, in order."""
    seen = []

    def fake_urlopen(req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        seen.append(path)
        if path == "/whoami":
            return _FakeResponse({"actor": actor, "kind": kind, "products": ["*"],
                                  "capabilities": capabilities})
        return then(req)

    return fake_urlopen, seen


def _one_page_of_issues(req):
    return _FakeResponse({"items": [_issue_row(0)], "count": 1, "total": 1,
                          "offset": 0, "truncated": False})


def test_a_narrow_credential_verifies_and_the_read_proceeds(monkeypatch, token_env):
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFIED", False)
    fake, seen = _whoami_first(["item.read", "signal.read"], _one_page_of_issues)
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    got = library_client.list_issue_items()

    assert len(got) == 1
    assert seen[0] == "/whoami", "the credential is checked before anything is read"


def test_a_system_credential_makes_orient_refuse_rather_than_leak(
        monkeypatch, token_env):
    """The issue's own done-condition. A token misprovisioned as `kind: "system"`
    resolves to a capability set holding `signal.customer`, which would render
    customer-class rows onto every full-tier page; Orient must refuse to serve
    instead."""
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFIED", False)
    fake, _ = _whoami_first(["item.read", "signal.read", "signal.customer"],
                            _one_page_of_issues, kind="system")
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    with pytest.raises(RuntimeError) as e:
        library_client.list_issue_items()
    assert "signal.customer" in str(e.value)
    assert "system" in str(e.value), "the message names the kind it resolved to"


def test_an_approved_client_credential_is_refused_the_same_way(
        monkeypatch, token_env):
    """The second way in, and the reason this asserts on the capability rather
    than on `kind`. Orient's agent token registered in the Library's
    `client-tokens.json` resolves as `approved_client`, which is a superset of
    `system` — a check reading `kind != "system"` would wave it straight through.
    """
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFIED", False)
    fake, _ = _whoami_first(["signal.read", "signal.customer", "decision.decide"],
                            _one_page_of_issues, kind="approved_client")
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    with pytest.raises(RuntimeError):
        library_client.list_issue_items()


def test_a_whoami_without_capabilities_is_a_refusal_not_a_pass(
        monkeypatch, token_env):
    """An answer that cannot be checked must not read as a clean one — otherwise
    a Library serving a stripped `/whoami` disables the wall silently, which is
    the failure this whole check exists to stop."""
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFIED", False)

    def fake_urlopen(req, timeout=None):
        if urllib.parse.urlsplit(req.full_url).path == "/whoami":
            return _FakeResponse({"actor": "orient", "kind": "agent"})
        return _one_page_of_issues(req)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as e:
        library_client.list_issue_items()
    assert "capabilities" in str(e.value)
    assert library_client._AGENT_CREDENTIAL_VERIFIED is False


@pytest.mark.parametrize("value", ["signal.customer", 7, {"signal.read": True}])
def test_a_capabilities_value_that_is_not_a_list_is_a_refusal(
        monkeypatch, token_env, value):
    """**The string case is the dangerous one and the reason this is checked at
    all** (security-reviewer, PR #173). `set("signal.customer")` decomposes the
    string into single characters, so `"signal.customer" in caps` answers False
    and a credential holding it sails through — the wall opens silently, which is
    precisely the failure this check exists to refuse.

    Nothing the Library serves today produces any of these; `whoami()` returns
    `sorted(ident.caps)`. That is the point. The check has to fail closed on an
    answer it was not written for, not only on the ones it was.
    """
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFIED", False)
    fake, _ = _whoami_first(value, _one_page_of_issues)
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    with pytest.raises(RuntimeError) as e:
        library_client.list_issue_items()
    assert "not a list" in str(e.value)
    assert library_client._AGENT_CREDENTIAL_VERIFIED is False


def test_a_human_reserved_call_does_not_run_the_check(monkeypatch, token_env):
    """`signal_acks` travels under the APPROVED-CLIENT credential, which holds
    `signal.customer` by design (ADR-0038: `approved_client` is `system` ∪
    {signal.ack, decision.decide}). Running the wall against it would refuse a
    correctly-configured deployment on every ack read, breaking the acknowledged
    sections of `waiting_build` and `ops_reports_build`.

    Untested until now, and the suite did not notice: the reviewer removed the
    `if not human_reserved:` guard and all 40 tests still passed, because the
    autouse fixture pre-seeds the flag for every test that does not reset it. So
    this resets it and gives `/whoami` a handler that would FAIL the check —
    the call must succeed anyway, having never asked.
    """
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFIED", False)
    fake, seen = _whoami_first(["signal.read", "signal.customer"],
                               lambda req: _FakeResponse({"acked": ["sig_1"]}),
                               kind="approved_client")
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    got = library_client.signal_acks(["sig_1", "sig_2"], acting_human="operator@example.com")

    assert got == {"sig_1"}
    assert "/whoami" not in seen, "a human-reserved call must not run the wall"
    assert library_client._AGENT_CREDENTIAL_VERIFIED is False


def test_a_concurrent_reader_waits_for_the_verdict_rather_than_skipping_it(
        monkeypatch, token_env):
    """**"Someone is checking" is not "the credential is safe"** (reviewer,
    PR #173, blocking).

    `server.py`'s route handlers are plain `def`, so Starlette runs them in a
    thread pool and two page requests arriving close together share this
    module's globals across OS threads. An earlier version exempted every caller
    while a check was in flight, not just the checking thread, so a second
    thread's read went straight through the wall during the window — reproduced
    by the reviewer against a `kind: system` credential.

    Here the `/whoami` handler blocks until the second thread has had its chance
    to slip past, so the test fails against that version and passes only when the
    second thread actually waits for the verdict.
    """
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFIED", False)
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFYING_THREAD", None)
    monkeypatch.setattr(library_client, "_verify_lock", threading.Lock())
    check_in_flight = threading.Event()
    release_the_check = threading.Event()
    reads_that_went_through = []

    def fake_urlopen(req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        if path == "/whoami":
            check_in_flight.set()
            # Hold the check open, so the window the second thread could slip
            # through is real rather than a matter of scheduling luck.
            release_the_check.wait(timeout=5)
            return _FakeResponse({"actor": "orient", "kind": "system",
                                  "products": ["*"],
                                  "capabilities": ["signal.read",
                                                   "signal.customer"]})
        reads_that_went_through.append(path)
        return _one_page_of_issues(req)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    errors = []

    def read():
        try:
            library_client.list_issue_items()
        except Exception as exc:  # noqa: BLE001 — the refusal is the outcome
            errors.append(exc)

    checker = threading.Thread(target=read)
    checker.start()
    assert check_in_flight.wait(timeout=5), "the checking thread never asked"
    second = threading.Thread(target=read)
    second.start()
    # The window. Against the flag-only version the second thread returns from
    # the guard immediately and its read lands in `reads_that_went_through`
    # well inside this; against the lock it is still blocked when time is up.
    time.sleep(0.3)
    assert reads_that_went_through == [], (
        "a read reached the Library while the credential was still unverified")
    release_the_check.set()
    checker.join(timeout=10)
    second.join(timeout=10)

    assert reads_that_went_through == [], (
        "a read reached the Library under an unverified credential holding "
        "signal.customer")
    assert len(errors) == 2, "both threads must refuse, not just the checking one"
    assert all(isinstance(e, RuntimeError) for e in errors)


def test_the_credential_is_checked_once_per_process(monkeypatch, token_env):
    """One extra request for the life of the process, not one per read."""
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFIED", False)
    fake, seen = _whoami_first(["item.read"], _one_page_of_issues)
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    library_client.list_issue_items()
    library_client.list_issue_items()
    library_client.list_issue_items()

    assert seen.count("/whoami") == 1


def test_a_failed_check_does_not_count_as_a_check(monkeypatch, token_env):
    """A `/whoami` that could not be reached leaves the credential UNVERIFIED, so
    the next read tries again. Marking it verified on the way in would let one
    unreachable Library at startup switch the wall off for the whole process."""
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFIED", False)
    attempts = []

    def fake_urlopen(req, timeout=None):
        attempts.append(urllib.parse.urlsplit(req.full_url).path)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    for _ in range(2):
        with pytest.raises(urllib.error.URLError):
            library_client.list_issue_items()
    assert attempts == ["/whoami", "/whoami"]
    assert library_client._AGENT_CREDENTIAL_VERIFIED is False


def test_the_check_carries_the_agent_credential_not_the_client_one(
        monkeypatch, token_env):
    """It must verify the credential the READS travel under. Checking the
    approved-client one would assert the wall against a token that legitimately
    holds `signal.customer` and refuse a correctly-configured deployment."""
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFIED", False)
    auth = {}

    def fake_urlopen(req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        auth[path] = req.headers["Authorization"]
        if path == "/whoami":
            return _FakeResponse({"actor": "orient", "kind": "agent",
                                  "products": ["*"], "capabilities": ["item.read"]})
        return _one_page_of_issues(req)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.list_issue_items()
    assert auth["/whoami"] == "Bearer test-token"


# --- paging: the invariant the spec calls out as the likeliest way this ships
# broken --------------------------------------------------------------------


def test_list_issue_items_pages_past_the_max_page_clamp(monkeypatch, token_env):
    """748 issue_items exist on the live database and MAX_PAGE clamps a page to
    200; a client that stops after one request silently drops everything past
    row 200 while the page still looks right. This fixture holds MORE than
    MAX_PAGE rows and asserts every single one renders."""
    total = 250
    rows = [_issue_row(n) for n in range(total)]
    requests_made = []

    def fake_urlopen(req, timeout=None):
        requests_made.append(req)
        params = _query(req.full_url)
        offset, limit = int(params["offset"]), int(params["limit"])
        assert limit <= library_client.MAX_PAGE
        page = rows[offset:offset + limit]
        return _FakeResponse({
            "items": page, "count": len(page), "total": total,
            "offset": offset, "truncated": offset + len(page) < total,
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_issue_items()

    assert len(got) == total
    assert {r.id for r in got} == {f"issue_{n:04d}" for n in range(total)}
    # every row past the first MAX_PAGE actually arrived, not just the count
    assert any(r.id == "issue_0000" for r in got)
    assert any(r.id == "issue_0249" for r in got)
    # MAX_PAGE=200 over 250 rows takes exactly two requests — pinning the
    # count catches a client that (wrongly) re-requests the same page forever
    # or that stops one short.
    assert len(requests_made) == 2


def test_list_leases_pages_past_the_max_page_clamp(monkeypatch, token_env):
    total = 250
    rows = [_lease_row(n) for n in range(total)]
    requests_made = []

    def fake_urlopen(req, timeout=None):
        requests_made.append(req)
        params = _query(req.full_url)
        offset, limit = int(params["offset"]), int(params["limit"])
        assert limit <= library_client.MAX_PAGE
        page = rows[offset:offset + limit]
        return _FakeResponse({
            "leases": page, "policy": {}, "count": len(page), "total": total,
            "offset": offset, "truncated": offset + len(page) < total,
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_leases()

    assert len(got.leases) == total
    assert {r.id for r in got.leases} == {f"lease_{n:04d}" for n in range(total)}
    assert any(r.id == "lease_0000" for r in got.leases)
    assert any(r.id == "lease_0249" for r in got.leases)
    assert len(requests_made) == 2


def test_list_leases_defaults_to_state_live(monkeypatch, token_env):
    seen_params = []

    def fake_urlopen(req, timeout=None):
        seen_params.append(_query(req.full_url))
        return _FakeResponse({"leases": [], "policy": {}, "count": 0, "total": 0,
                              "offset": 0, "truncated": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.list_leases()
    assert seen_params[0]["state"] == "live"

    library_client.list_leases(state="all")
    assert seen_params[1]["state"] == "all"


def test_list_leases_maps_every_field_lease_dict_serves(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"leases": [_lease_row(1, dead=True)], "policy": {}, "count": 1, "total": 1,
         "offset": 0, "truncated": False}))
    got = library_client.list_leases()
    assert len(got.leases) == 1
    lease = got.leases[0]
    assert lease.id == "lease_0001"
    assert lease.root_id == "issue_0001"
    assert lease.holder == "coder"
    assert lease.dead is True
    assert lease.taken_at is not None and lease.taken_at.year == 2026


def test_list_leases_carries_the_served_policy(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"leases": [], "policy": {"grace_seconds": 14400, "max_lease_seconds": 86400},
         "count": 0, "total": 0, "offset": 0, "truncated": False}))
    got = library_client.list_leases()
    assert got.policy == library_client.LeasePolicy(
        grace_seconds=14400, max_lease_seconds=86400)


def test_list_leases_policy_is_none_rather_than_a_hardcoded_guess_when_omitted(
        monkeypatch, token_env):
    """The Library omitting `policy` (or serving it malformed) must not make
    this client fabricate 4h/24h from thin air — a caller (`crews_build`)
    treats `None` as license to warn rather than as license to assume either
    tunable."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"leases": [], "policy": {}, "count": 0, "total": 0, "offset": 0,
         "truncated": False}))
    got = library_client.list_leases()
    assert got.policy is None


def test_list_leases_policy_is_none_when_served_as_the_wrong_type(monkeypatch, token_env):
    """A truthy, non-dict `policy` (a string here) is exactly the "malformed"
    case `test_list_leases_policy_is_none_rather_than_a_hardcoded_guess_when_omitted`'s
    docstring already claims: it must not reach `.get("grace_seconds")` and
    raise, and must not be treated as usable."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"leases": [], "policy": "not-a-dict", "count": 0, "total": 0, "offset": 0,
         "truncated": False}))
    got = library_client.list_leases()
    assert got.policy is None


def test_list_leases_policy_is_none_when_a_subfield_is_absent_or_wrong_typed(monkeypatch, token_env):
    """The third unusable shape, and the one that was untested: a policy that
    IS an object but whose numbers are missing or not integers.

    It survives the `isinstance(raw_policy, dict)` guard and is nulled a step
    later by `list_leases`'s own subfield check. Covered because
    `LeaseListResult`'s docstring now enumerates all three paths, and an
    enumerated claim with an untested member is how this docstring came to
    overclaim twice already."""
    for bad in ({"grace_seconds": "4h", "max_lease_seconds": 86400},   # wrong type
                {"grace_seconds": 14400},                              # absent
                {"grace_seconds": None, "max_lease_seconds": None}):   # null
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout=None, _p=bad: _FakeResponse(
                                {"leases": [], "policy": _p, "count": 0, "total": 0,
                                 "offset": 0, "truncated": False}))
        got = library_client.list_leases()
        assert got.policy is None, f"expected None for policy={bad!r}"


def test_a_single_page_under_the_clamp_makes_one_request(monkeypatch, token_env):
    rows = [_issue_row(n) for n in range(3)]
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return _FakeResponse({"items": rows, "count": 3, "total": 3,
                              "offset": 0, "truncated": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_issue_items()
    assert len(got) == 3
    assert len(calls) == 1


# --- signals: paging, the OR-workaround's two calls, facets, acks -----------


def test_list_lease_refusals_pages_past_the_max_page_clamp(monkeypatch, token_env):
    """Paged to completion rather than capped, matching `list_leases`. A cap
    here would silently drop the oldest collisions, and a repeat collision
    across a long window is exactly what this route exists to make visible."""
    pages = [
        {"refusals": [_refusal_row(i) for i in range(200)], "count": 200,
         "total": 203, "offset": 0, "truncated": True},
        {"refusals": [_refusal_row(i) for i in range(200, 203)], "count": 3,
         "total": 203, "offset": 200, "truncated": False},
    ]
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _FakeResponse(pages[len(seen) - 1])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rows = library_client.list_lease_refusals()
    assert len(rows) == 203
    assert "offset=200" in seen[1]


def test_list_lease_refusals_maps_every_field_the_route_serves(monkeypatch, token_env):
    """Including `blocks_by`, which says whether the fence was found by walking
    UP from what was asked for or DOWN into it.

    It does NOT say whether the two crews wanted the same item — that reading
    was this PR's round-1 error, and leaving it written here would teach the
    next reader the conflation two rounds went into removing (reviewer,
    PR #200). Mapped anyway, because dropping the field would lose the
    direction as well."""
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(
                            {"refusals": [_refusal_row(1)], "count": 1, "total": 1,
                             "offset": 0, "truncated": False}))
    row = library_client.list_lease_refusals()[0]
    assert row.id == "ref_0001"
    assert row.product == "keel"
    assert row.requester == "session-B"
    assert row.blocking_holder == "session-A"
    assert row.blocking_root_id == "issue_root1"
    assert row.blocks_by == "ancestor"
    assert row.at == dt.datetime(2026, 8, 8, 4, 37, 22, 821295)


def test_a_withheld_blocker_is_read_as_blocked_rather_than_as_three_nulls(
        monkeypatch, token_env):
    """**The wire has a shape this client would otherwise silently flatten**
    (reviewer, PR #200). When the blocking subtree is outside the caller's
    scope, `lease.refusal_dict` omits `blocking_lease_id`, `blocking_root_id`
    and `blocking_holder` and sends `blocked: true` — so the row still says
    "something held this" rather than "nothing did".

    Reading only the three omitted keys with `.get()` produces three `None`s,
    which is indistinguishable from a parse failure. Orient's credential is
    `products: ["*"]` today so this shape never arrives, and that is a fact
    about one token rather than anything the contract promises."""
    withheld = {"id": "ref_0009", "at": "2026-08-08 04:37:22.821295",
                "requested_item_id": "issue_wanted", "product": "keel",
                "requester": "session-B", "actor": "ops-mcp",
                "blocks_by": "ancestor", "blocked": True}
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(
                            {"refusals": [withheld], "count": 1, "total": 1,
                             "offset": 0, "truncated": False}))
    row = library_client.list_lease_refusals()[0]
    assert row.blocked is True
    assert row.blocking_holder is None
    # None rather than False: there is no root to compare, and False would read
    # as a checked negative to a caller that did not look at `blocked` first.
    assert row.fence_was_the_requested_item is None


def test_a_visible_blocker_is_not_marked_blocked(monkeypatch, token_env):
    """The ordinary shape carries no `blocked` key at all, so the flag has to
    default False rather than inheriting whatever `.get()` returns."""
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(
                            {"refusals": [_refusal_row(1)], "count": 1, "total": 1,
                             "offset": 0, "truncated": False}))
    assert library_client.list_lease_refusals()[0].blocked is False


def test_the_fence_property_says_where_the_fence_sat_not_who_wanted_what(
        monkeypatch, token_env):
    """`blocks_by` is `"ancestor"` for both rows below, because
    `lease.live_conflicts` walks `[root_id, *ancestor_ids(root_id)]` and tags
    every one that way. So the enum alone cannot say where the fence was.

    `fence_was_the_requested_item` can, and that is ALL it says. It is
    deliberately not "the two crews wanted the same item": `root_id_for` makes a
    leaf lease its parent, so a genuine race for one leaf answers False here
    (reviewer, PR #200). The third row pins exactly that, since reading False as
    "not the same work" is the error this property is named to prevent."""
    fence_on_it = {**_refusal_row(1), "requested_item_id": "issue_x",
                   "blocking_root_id": "issue_x"}
    fence_above = {**_refusal_row(2), "requested_item_id": "issue_child",
                   "blocking_root_id": "issue_epic"}
    race_for_one_leaf = {**_refusal_row(3), "requested_item_id": "issue_leaf_L",
                         "blocking_root_id": "issue_epic_E"}
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(
                            {"refusals": [fence_on_it, fence_above,
                                          race_for_one_leaf],
                             "count": 3, "total": 3,
                             "offset": 0, "truncated": False}))
    rows = library_client.list_lease_refusals()
    assert rows[0].fence_was_the_requested_item is True
    assert rows[1].fence_was_the_requested_item is False
    # A real race for one leaf, indistinguishable here from the row above it.
    assert rows[2].fence_was_the_requested_item is False
    assert {r.blocks_by for r in rows} == {"ancestor"}


def test_list_lease_refusals_passes_product_through_only_when_given(
        monkeypatch, token_env):
    """The Library narrows WITHIN this credential's scope and can never widen
    it, so sending the parameter is a filter rather than an escalation — but an
    absent product must not travel as an empty string, which the route would
    read as a product named ''."""
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _FakeResponse({"refusals": [], "count": 0, "total": 0,
                              "offset": 0, "truncated": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.list_lease_refusals()
    assert "product=" not in seen[0]
    library_client.list_lease_refusals(product="vivy")
    assert "product=vivy" in seen[1]


def _live_run_row(n: int, **overrides) -> dict:
    row = {"run_id": f"run-{n:04d}", "agent": "orient-coder", "task": "a task",
          "started_at": "2026-09-02T20:00:00Z", "repo": "agent-platform",
          "worktree": "/x", "pid": 1000 + n, "parent_run": None, "holds": []}
    row.update(overrides)
    return row


def test_list_live_runs_maps_every_field_the_route_serves(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"runs": [_live_run_row(1)], "available": True, "reason": None,
         "generated_at": "2026-09-02T20:31:00Z", "stale_after_seconds": 60.0, "count": 1}))
    got = library_client.list_live_runs()
    assert got.available is True
    assert got.reason is None
    assert got.generated_at is not None and got.generated_at.year == 2026
    assert got.stale_after_seconds == 60.0
    assert got.count == 1
    run = got.runs[0]
    assert run.run_id == "run-0001"
    assert run.agent == "orient-coder"
    assert run.repo == "agent-platform"
    assert run.pid == 1001


def test_list_live_runs_is_degraded_when_available_is_false(monkeypatch, token_env):
    """A degraded answer is a 200 whose body says `available: False` with a
    `reason` — this must not raise, and the reason must survive to the
    caller rather than being dropped."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"runs": [], "available": False, "reason": "bosun unreachable",
         "generated_at": None, "stale_after_seconds": None, "count": 0}))
    got = library_client.list_live_runs()
    assert got.available is False
    assert got.reason == "bosun unreachable"
    assert got.runs == []


def test_list_live_runs_generated_at_is_none_when_the_payload_simply_omits_it(
        monkeypatch, token_env):
    """`generated_at` is null on every degraded answer AND on a successful
    one whose payload happens to omit the key — a caller must null-check it
    regardless of `available`, per this route's own contract."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"runs": [_live_run_row(1)], "available": True, "count": 1}))
    got = library_client.list_live_runs()
    assert got.available is True
    assert got.generated_at is None
    assert got.stale_after_seconds is None


def test_list_signals_pages_past_the_max_page_clamp(monkeypatch, token_env):
    """18,455 signals exist on the live database; a cap-less call (the whole
    matching set) must page to completion exactly like `list_issue_items`."""
    total = 250
    rows = [_signal_row(n) for n in range(total)]

    def fake_urlopen(req, timeout=None):
        params = _query(req.full_url)
        offset, limit = int(params["offset"]), int(params["limit"])
        assert limit <= library_client.MAX_PAGE
        page = rows[offset:offset + limit]
        return _FakeResponse({
            "signals": page, "count": len(page), "total": total,
            "offset": offset, "truncated": offset + len(page) < total,
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_signals()

    assert len(got) == total
    assert {r.id for r in got} == {f"sig_{n:04d}" for n in range(total)}


def test_list_signals_walks_past_the_offset_ceiling_by_cursor(monkeypatch, token_env):
    """**The regression this whole change exists for, asserted at the far end of
    the walk rather than on the loop's shape** (issue_01M06Y897250HGZHFQH2VP1YZS).

    The old loop counted an offset upward, the Library refuses one above 10,000,
    and the walk died partway with an `HTTPError`. Nothing failed visibly:
    `signals_build` caught it and wrote the last static build, so `/archives`
    rendered stale data and every test stayed green. So this fake refuses a deep
    offset exactly as the Library does, and the assertion is that the LAST row
    arrives — a test that merely checks a cursor was sent would pass against a
    walk that still stopped at 10,000.
    """
    total = 10_400  # past MAX_OFFSET, which is the only interesting size here
    rows = [_signal_row(n) for n in range(total)]
    at = {row["id"]: n for n, row in enumerate(rows)}

    def fake_urlopen(req, timeout=None):
        params = _query(req.full_url)
        # The route refuses both rather than silently honouring one, and it
        # tests for the parameter's PRESENCE, so a transitional client sending
        # `offset=0` beside a cursor would 400 in production. Pin it here.
        assert not ("cursor" in params and "offset" in params)
        limit = int(params["limit"])
        if "cursor" in params:
            start = at[params["cursor"]] + 1
        else:
            start = int(params["offset"])
            if start > 10_000:
                raise urllib.error.HTTPError(
                    req.full_url, 400, "offset may be at most 10000", {}, None)
        page = rows[start:start + limit]
        remaining = total - start
        truncated = len(page) < remaining
        return _FakeResponse({
            "signals": page, "count": len(page), "total": remaining,
            "truncated": truncated,
            # Served on every listing, not only on a cursor page, which is what
            # lets the walk switch over after its first offset request.
            "next_cursor": page[-1]["id"] if page and truncated else None,
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_signals()

    assert len(got) == total
    assert got[-1].id == f"sig_{total - 1:04d}"
    assert len({r.id for r in got}) == total  # no row read twice, none skipped


def test_list_signals_does_not_re_read_a_page_if_the_cursor_reverts_mid_walk(
        monkeypatch, token_env):
    """**Both reviewers found this independently on PR #192, and the reviewer
    reproduced it: 800 rows returned for a 600-row table, 200 of them duplicates,
    with no error.**

    The first version kept its own `offset` counter and advanced it only on the
    branch that read it, so a walk that took even one cursor page left it frozen
    behind the real position. Falling back to offset after that re-issued a page
    already in hand, and the caller received duplicates silently — the exact
    shape of silent corruption this change exists to remove, reintroduced at a
    different boundary.

    Today's Library decides cursor-walkability once per ORDER, so a mid-walk
    reversion means it has broken that contract. The client should still come
    back with a correct list rather than a quietly wrong one, because "the
    server broke its contract" is not something a page render can notice.
    """
    total = 600
    rows = [_signal_row(n) for n in range(total)]
    at = {row["id"]: n for n, row in enumerate(rows)}

    def fake_urlopen(req, timeout=None):
        params = _query(req.full_url)
        assert not ("cursor" in params and "offset" in params)
        limit = int(params["limit"])
        start = at[params["cursor"]] + 1 if "cursor" in params else int(params["offset"])
        page = rows[start:start + limit]
        remaining = total - start
        truncated = len(page) < remaining
        # THE ANOMALY: the second request answers `truncated` with no cursor,
        # after the first already handed one over.
        withhold = start == 200
        return _FakeResponse({
            "signals": page, "count": len(page), "total": remaining,
            "truncated": truncated,
            "next_cursor": None if withhold else (
                page[-1]["id"] if page and truncated else None),
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_signals()

    assert [r.id for r in got] == [f"sig_{n:04d}" for n in range(total)]


@pytest.mark.parametrize("value", [["tok"], {"cursor": "tok"}, 7, ""])
def test_list_signals_treats_a_non_string_cursor_as_no_cursor(
        monkeypatch, token_env, value):
    """A list or dict reaching `urlencode(..., doseq=True)` would be ITERATED
    into several `cursor=` parameters rather than refused, which is a stranger
    failure than the offset walk this falls back to. Same discipline
    `_check_agent_credential` applies to a `capabilities` value that is present
    but not a list: an answer this code was not written for is refused rather
    than decomposed. Nothing the Library serves produces one of these, which is
    the point — the check has to hold for answers it was not written for."""
    total = 300
    rows = [_signal_row(n) for n in range(total)]

    def fake_urlopen(req, timeout=None):
        params = _query(req.full_url)
        assert "cursor" not in params  # never composed from a malformed value
        offset, limit = int(params["offset"]), int(params["limit"])
        page = rows[offset:offset + limit]
        return _FakeResponse({
            "signals": page, "count": len(page), "total": total,
            "offset": offset, "truncated": offset + len(page) < total,
            "next_cursor": value,
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_signals()

    assert len(got) == total


def test_list_signals_still_walks_by_offset_when_no_cursor_is_offered(
        monkeypatch, token_env):
    """`next_cursor` comes back `None` for `order=produced_at`: that key spans a
    nullable column, and the Library refuses to keyset-walk it rather than guess
    a predicate across the NULL boundary. The walk must fall back to offset
    rather than stopping, which would silently drop every row past the first
    page — the failure `_page_all`'s own docstring exists to prevent."""
    total = 450
    rows = [_signal_row(n) for n in range(total)]
    offsets_seen = []

    def fake_urlopen(req, timeout=None):
        params = _query(req.full_url)
        assert "cursor" not in params
        offset, limit = int(params["offset"]), int(params["limit"])
        offsets_seen.append(offset)
        page = rows[offset:offset + limit]
        return _FakeResponse({
            "signals": page, "count": len(page), "total": total,
            "offset": offset, "truncated": offset + len(page) < total,
            "next_cursor": None,
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_signals(order="produced_at")

    assert len(got) == total
    assert offsets_seen == [0, 200, 400]


def test_list_signals_cap_stops_before_the_whole_table(monkeypatch, token_env):
    """Every signal-reading builder wants a bounded top-N, not the whole table
    — `cap` must stop at the requested size in ONE request rather than paging
    to completion and slicing client-side, which would cost a full scan for a
    60-row browse page."""
    total = 250
    rows = [_signal_row(n) for n in range(total)]
    requests_made = []

    def fake_urlopen(req, timeout=None):
        requests_made.append(req)
        params = _query(req.full_url)
        offset, limit = int(params["offset"]), int(params["limit"])
        page = rows[offset:offset + limit]
        return _FakeResponse({
            "signals": page, "count": len(page), "total": total,
            "offset": offset, "truncated": offset + len(page) < total,
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_signals(cap=60)

    assert len(got) == 60
    assert len(requests_made) == 1
    assert int(_query(requests_made[0].full_url)["limit"]) == 60


def test_list_signals_cap_above_max_page_still_pages(monkeypatch, token_env):
    """e2e's cap (400) exceeds MAX_PAGE (200) — the exact case
    issue_01KZFTHZP4FAHRHWTR9MF9AHV3 calls out — so it must take two requests,
    not stop after the first page's 200 rows."""
    total = 250
    rows = [_signal_row(n) for n in range(total)]
    requests_made = []

    def fake_urlopen(req, timeout=None):
        requests_made.append(req)
        params = _query(req.full_url)
        offset, limit = int(params["offset"]), int(params["limit"])
        assert limit <= library_client.MAX_PAGE
        page = rows[offset:offset + limit]
        return _FakeResponse({
            "signals": page, "count": len(page), "total": total,
            "offset": offset, "truncated": offset + len(page) < total,
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_signals(cap=400)

    assert len(got) == total   # every row past MAX_PAGE actually arrived
    assert len(requests_made) == 2


def test_list_signals_repeats_source_and_kind_and_carries_order(monkeypatch, token_env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["params"] = urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query)
        return _FakeResponse({"signals": [], "count": 0, "total": 0,
                              "offset": 0, "truncated": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.list_signals(source=["slack", "ci"], kind=["alert"],
                                order="produced_at", cap=10)
    params = seen["params"]
    assert ("source", "slack") in params and ("source", "ci") in params
    assert ("kind", "alert") in params
    assert ("order", "produced_at") in params


def test_list_signals_stream_filter_returns_the_audit_control_signals(monkeypatch, token_env):
    """Positive control for the kb-audit read (issue_01M1MRC8RJ66XPHYDZYD0KK28M).

    The two audit signals below are the ones the issue names, a digest and an
    alert. The fake honours `stream` the way the Library does, so a client that
    drops the parameter returns the decoy as well and fails here."""
    def row(sid, kind, stream):
        return {**_signal_row(0), "id": sid, "source": "platform", "stream": stream,
                "kind": kind, "product": "substrate", "body": "audit"}

    rows = [
        row("sig_01M19AY72THFGSXMSBG68RA64P", "digest", "kb-audit"),
        row("sig_01M0QA5GJA4Q0FYXFA9TPCE1GN", "alert", "kb-audit"),
        row("sig_decoy", "digest", "release-notes"),
    ]
    seen = {}

    def fake_urlopen(req, timeout=None):
        wanted = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query).get("stream")
        seen["stream"] = wanted
        page = [r for r in rows if wanted is None or r["stream"] in wanted]
        return _FakeResponse({"signals": page, "count": len(page), "total": len(page),
                              "offset": 0, "truncated": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_signals(stream=["kb-audit"])

    assert seen["stream"] == ["kb-audit"]
    assert {r.id for r in got} == {"sig_01M19AY72THFGSXMSBG68RA64P", "sig_01M0QA5GJA4Q0FYXFA9TPCE1GN"}
    assert {r.stream for r in got} == {"kb-audit"}


def test_list_signals_passes_product_through_only_when_given(monkeypatch, token_env):
    """Absent `product` must not travel as an empty string, the same
    `list_lease_refusals` rule — the route would read `product=''` as a
    product literally named the empty string, not as "no filter"."""
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _FakeResponse({"signals": [], "count": 0, "total": 0,
                              "offset": 0, "truncated": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.list_signals()
    assert "product=" not in seen[0]
    library_client.list_signals(product="vivy")
    assert ("product", "vivy") in _query(seen[1]).items() or "product=vivy" in seen[1]


def test_list_signals_product_filter_is_a_pre_filter_not_client_side(monkeypatch, token_env):
    """`product` must reach the Library on every page of a walk, not just the
    first request — `_page_signals` reuses the same `params` dict across pages,
    but a regression that built it once and mutated it per-page could drop the
    filter after page 1. This is also the discriminating check against
    `/signals/search`'s known bug: search POST-filters a retrieved page (5
    requested can come back as 1 matching row); this fake server filters
    BEFORE paging, like `GET /signals` actually does, so every row this test
    hands back already matches `product`."""
    all_rows = [{**_signal_row(n), "product": "vivy" if n % 2 == 0 else "constance"}
                for n in range(6)]
    requests_made = []

    def fake_urlopen(req, timeout=None):
        requests_made.append(req)
        params = _query(req.full_url)
        assert params.get("product") == "vivy"          # every page must carry it
        matching = [r for r in all_rows if r["product"] == "vivy"]
        offset, limit = int(params["offset"]), int(params["limit"])
        page = matching[offset:offset + limit]
        return _FakeResponse({
            "signals": page, "count": len(page), "total": len(matching),
            "offset": offset, "truncated": offset + len(page) < len(matching),
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(library_client, "MAX_PAGE", 2)   # force >1 request
    got = library_client.list_signals(product="vivy", cap=10)

    assert len(requests_made) > 1                        # the walk actually paged
    assert len(got) == 3                                 # all 3 vivy rows, none dropped
    assert {sig.product for sig in got} == {"vivy"}


def test_signal_facets_request(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse({"total": 18455, "embedded": 12000,
                              "by_source": [["github_review", 9000], ["slack", 5000]],
                              "products": ["constance", "vivy"]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    facets = library_client.signal_facets(kind=["changelog"])

    assert facets.total == 18455
    assert facets.embedded == 12000
    assert facets.by_source == [("github_review", 9000), ("slack", 5000)]
    assert facets.products == ["constance", "vivy"]


def test_search_signals_request_and_response_shape(monkeypatch, token_env):
    """Shape measured against the live Library 2026-08-27: `signals` (each row
    a full `signal_dict` plus `distance`), `count`, `truncated` — and, unlike
    `GET /signals`, no `total`."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["params"] = urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query)
        row = {**_signal_row(1), "distance": 0.7464}
        return _FakeResponse({"signals": [row], "count": 1, "offset": 0,
                              "truncated": True, "query": "deploy"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    resp = library_client.search_signals("deploy", source=["deploy_watch"],
                                         product="vivy", cap=20)

    assert ("q", "deploy") in seen["params"]
    assert ("source", "deploy_watch") in seen["params"]
    assert ("product", "vivy") in seen["params"]
    assert ("limit", "20") in seen["params"]
    assert "kind" not in dict(seen["params"])

    assert resp.count == 1
    assert resp.truncated is True
    assert len(resp.results) == 1
    assert resp.results[0].distance == 0.7464
    assert resp.results[0].signal.id == "sig_0001"
    assert not hasattr(resp, "total")


def test_search_signals_omits_absent_filters(monkeypatch, token_env):
    """`source`/`product`/`cap` must not travel as empty/zero values — the
    same `list_signals` rule (`product=''` would be a literal filter, not
    "no filter")."""
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _FakeResponse({"signals": [], "count": 0, "offset": 0,
                              "truncated": False, "query": "x"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.search_signals("x")
    assert "source=" not in seen[0]
    assert "product=" not in seen[0]
    assert "limit=" not in seen[0]
    assert ("q", "x") in _query(seen[0]).items()


def test_search_signals_distance_defaults_to_none_when_absent(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse({"signals": [_signal_row(1)], "count": 1, "offset": 0,
                              "truncated": False, "query": "x"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    resp = library_client.search_signals("x")
    assert resp.results[0].distance is None


def test_signal_acks_request(monkeypatch, token_env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["params"] = urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query)
        return _FakeResponse({"acked": ["sig_0001", "sig_0002"]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    acked = library_client.signal_acks(
        ["sig_0001", "sig_0002", "sig_0003"], acting_human="operator@example.com")

    assert acked == {"sig_0001", "sig_0002"}
    assert ("signal_id", "sig_0001") in seen["params"]
    assert ("signal_id", "sig_0003") in seen["params"]
    assert ("acting_human", "operator@example.com") in seen["params"]


def test_signal_acks_empty_input_makes_no_request(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise AssertionError("should not be called for an empty id list")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.signal_acks([], acting_human="operator@example.com") == set()


def test_get_signal_404_is_none_not_an_exception(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.get_signal("sig_missing") is None


def test_get_signal_found(monkeypatch, token_env):
    row = _signal_row(1)

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(row)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sig = library_client.get_signal("sig_0001")
    assert sig is not None
    assert sig.id == "sig_0001"
    assert sig.source == "ci"


# --- the agentctl-review stamp: stripped once, for every SignalRecord --------
#
# `agentctl-review` writes this stamp as the leading block of a `pr_comment`
# or `changelog` body it captures from a review. `signals_build`'s detail page
# stripped it for itself; three more render sites read the identical body
# through this same client and rendered it unstripped (convivy-orient#254
# round 2: archives_build.py's PR-comment, PR-description and changelog cards,
# plus signal_search_api.py's browse/search JSON). These tests pin the strip
# to `_signal_record` — the one place every one of `get_signal`/`list_signals`/
# `search_signals` builds a `SignalRecord` — so a caller reading `sig.body`
# gets the stripped body with nothing left to remember at the call site.

_REVIEW_STAMP = (
    "<!-- agentctl-review\n"
    "v: 1\n"
    "pr: 320\n"
    "repo: knowledge\n"
    "reviewed-commit: 7842af3bf82cb74fa1522af\n"
    "verdict: APPROVE\n"
    "agent: reviewer\n"
    "run-id: run_9f8e7d\n"
    "-->\n"
)


def test_get_signal_strips_a_leading_review_stamp(monkeypatch, token_env):
    row = {**_signal_row(1), "body": _REVIEW_STAMP + "LGTM, ship it."}

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(row)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sig = library_client.get_signal("sig_0001")
    assert sig.body == "LGTM, ship it."
    assert "agentctl-review" not in sig.body
    assert sig.stamp_only is False


def test_get_signal_marks_a_stamp_only_body_via_stamp_only_rather_than_leaving_it_in_the_body(
        monkeypatch, token_env):
    row = {**_signal_row(1), "body": _REVIEW_STAMP}

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(row)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sig = library_client.get_signal("sig_0001")
    assert sig.body == ""
    assert sig.stamp_only is True


def test_get_signal_leaves_a_body_with_no_leading_stamp_unchanged(monkeypatch, token_env):
    row = {**_signal_row(1), "body": "plain prose, no stamp at all"}

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(row)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sig = library_client.get_signal("sig_0001")
    assert sig.body == "plain prose, no stamp at all"
    assert sig.stamp_only is False


def test_get_signal_preserves_a_genuine_html_comment_later_in_the_body(monkeypatch, token_env):
    # The strip is anchored to a LEADING agentctl-review block, not a blind
    # "remove every HTML comment" — a comment further into real content stays.
    body = "some real prose\n\n<!-- a genuine inline note -->\nmore prose after it"
    row = {**_signal_row(1), "body": body}

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(row)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sig = library_client.get_signal("sig_0001")
    assert sig.body == body
    assert sig.stamp_only is False


def test_list_signals_strips_a_leading_review_stamp_from_every_row(monkeypatch, token_env):
    rows = [{**_signal_row(0), "body": _REVIEW_STAMP + "first"},
           {**_signal_row(1), "body": "second, no stamp"}]

    def fake_urlopen(req, timeout=None):
        return _FakeResponse({"signals": rows, "count": 2, "total": 2,
                              "offset": 0, "truncated": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = {r.id: r.body for r in library_client.list_signals()}
    assert got == {"sig_0000": "first", "sig_0001": "second, no stamp"}


def test_search_signals_strips_a_leading_review_stamp(monkeypatch, token_env):
    row = {**_signal_row(1), "body": _REVIEW_STAMP + "ship it"}

    def fake_urlopen(req, timeout=None):
        return _FakeResponse({"signals": [row], "count": 1, "offset": 0,
                              "truncated": False, "query": "ship"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    resp = library_client.search_signals("ship")
    assert resp.results[0].signal.body == "ship it"


def test_a_signal_library_outage_raises_rather_than_returning_an_empty_list(monkeypatch, token_env):
    """Same rule `list_issue_items` follows — see that test's docstring."""
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.URLError):
        library_client.list_signals()


# --- the protocol header, the credential, the timeout ------------------------


def test_every_request_carries_the_protocol_header_and_bearer_token(monkeypatch, token_env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["headers"] = dict(req.headers)
        seen["timeout"] = timeout
        return _FakeResponse({"items": [], "count": 0, "total": 0,
                              "offset": 0, "truncated": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.list_issue_items()

    assert seen["headers"]["Library-protocol"] == str(library_client.PROTOCOL_VERSION)
    assert seen["headers"]["Authorization"] == "Bearer test-token"
    assert seen["timeout"] == library_client.TIMEOUT


def test_credential_falls_back_to_the_token_file(monkeypatch, tmp_path):
    monkeypatch.delenv("LIBRARY_CLIENT_TOKEN", raising=False)
    token_file = tmp_path / "library-token"
    token_file.write_text("from-the-file\n")
    monkeypatch.setattr(library_client, "TOKEN_FILE", token_file)

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["auth"] = req.headers["Authorization"]
        return _FakeResponse({"items": [], "count": 0, "total": 0,
                              "offset": 0, "truncated": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.list_issue_items()
    assert seen["auth"] == "Bearer from-the-file"


# --- failure propagates rather than being swallowed into an empty result ----


def test_a_library_outage_raises_rather_than_returning_an_empty_list(monkeypatch, token_env):
    """The one outcome the spec calls out as worse than a 500: a page silently
    rendering "no open issues" because the Library never answered. This must
    raise, so the render path that already falls back to a static page
    (server.py's `except Exception`) is the thing that catches it — never a
    quiet empty list here."""
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.URLError):
        library_client.list_issue_items()


def test_a_timeout_raises_too(monkeypatch, token_env):
    import socket

    def fake_urlopen(req, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(socket.timeout):
        library_client.list_issue_items()


def test_a_non_404_error_on_get_propagates(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        library_client.get_issue_item("issue_0001")


# --- signal writes: create (agent) / ack / unack (approved-client) ----------


def test_create_signal_posts_and_returns_the_id(monkeypatch, token_env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["method"] = req.get_method()
        seen["url"] = req.full_url
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = json.loads(req.data)
        return _FakeResponse({"id": "sig_new1"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sid, idempotent, existing = library_client.create_signal(
        source="room", kind="room-grant-denied", actor="operator@example.com",
        product="shared", privacy_class="internal", body="wanted: X")

    assert sid == "sig_new1"
    assert idempotent is False   # no "idempotent" key in the response → False
    assert existing is None      # only populated on an idempotent hit
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/signals")
    assert seen["auth"] == "Bearer test-token"  # the agent credential, not the client one
    assert seen["body"] == {"source": "room", "kind": "room-grant-denied",
                            "actor": "operator@example.com", "product": "shared",
                            "body": "wanted: X", "privacy_class": "internal"}


def test_create_signal_with_an_acting_human_carries_the_client_credential(
        both_creds, monkeypatch):
    """The whole of the fix. The Library's `attested_human` refuses
    `acting_human` from anything but an approved client, so sending the field
    under the agent credential is a 403 rather than a recorded capture — which
    is what every attempt would have been, since the field could not be sent at
    all before this.

    Both halves are asserted together on purpose: the field without the
    credential is refused, and the credential without the field records nobody,
    so either one alone is the bug.
    """
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = json.loads(req.data)
        return _FakeResponse({"id": "sig_att1"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sid, idempotent, existing = library_client.create_signal(
        source="email", kind="inbound_email", actor="alice@bigco.example",
        product="commercial", privacy_class="customer",
        acting_human="operator@example.com")

    assert sid == "sig_att1"
    assert idempotent is False
    assert existing is None
    assert seen["auth"] == "Bearer CLIENT-CRED"
    assert seen["body"]["acting_human"] == "operator@example.com"


def test_create_signal_without_an_acting_human_sends_no_such_key(
        both_creds, monkeypatch):
    """Absent means absent — not an explicit null. The Library treats the key's
    PRESENCE as the request to attest (`payload.get("acting_human") is not
    None` guards its whole attestation branch), so a null would be inert today
    and is one refactor away from being read as a claim. The agent credential
    stays, which is what keeps the four capture jobs working."""
    seen = _capture(monkeypatch, {"id": "sig_plain"})
    library_client.create_signal(source="room", kind="room-grant-denied",
                                 actor="operator@example.com", product="shared")
    posts = [(url, auth) for url, auth in seen if url.endswith("/signals")]
    assert [auth for _, auth in posts] == ["Bearer AGENT-CRED"]


def test_an_empty_acting_human_raises_rather_than_travelling(both_creds, monkeypatch):
    """A caller that passed one believed it knew the person. Sending an empty
    string earns a 403 that reads as a permission problem, and dropping it
    silently records the capture as unattested with nothing saying so — so
    neither happens, and no request goes out at all."""
    def fake_urlopen(req, timeout=None):
        pytest.fail("a request was sent with an empty acting_human")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ValueError, match="acting_human"):
        library_client.create_signal(source="note", kind="market",
                                     actor="operator@example.com", product="vivy",
                                     acting_human="   ")


def test_create_signal_surfaces_an_idempotent_dedup_hit_as_success(monkeypatch, token_env):
    """A dedup_key collision the caller could have created is the Library's
    own 200 (not an error), carrying `idempotent: true` and the EXISTING row's
    full record. Collapsing this to a bare id would make it indistinguishable
    from a fresh 201 — the regression a round of review caught in
    signal_capture_api.py, which broke github_capture.py's created-vs-
    duplicate tally and the capture form's own double-submit guard."""
    existing_row = {**_signal_row(1), "id": "sig_existing", "product": "constance",
                    "idempotent": True}

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(existing_row)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sid, idempotent, existing = library_client.create_signal(
        source="gh", kind="review_comment", actor="reviewer", product="vivy",
        dedup_key="gh:vivy:pr1:review:1")
    assert sid == "sig_existing"
    assert idempotent is True
    # existing is parsed from THIS SAME response — no second request — and
    # carries the row's REAL product, which may differ from the product the
    # caller declared (here: vivy declared, constance actual). A caller
    # checking authz against `existing.product` rather than the declared one
    # is exactly what closes the round-5 cross-product disclosure.
    assert existing is not None
    assert existing.id == "sig_existing"
    assert existing.product == "constance"


def test_ack_signal_posts_under_the_client_credential_and_returns_true(monkeypatch, token_env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["method"] = req.get_method()
        seen["url"] = req.full_url
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = json.loads(req.data)
        return _FakeResponse({"signal_id": "sig_0001", "acknowledged": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.ack_signal("sig_0001", acting_human="operator@example.com") is True
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/signals/sig_0001/ack")
    assert seen["auth"] == "Bearer test-client-token"
    assert seen["body"] == {"acting_human": "operator@example.com"}


def test_ack_signal_404_returns_false_rather_than_raising(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.ack_signal("sig_missing", acting_human="operator@example.com") is False


def test_ack_signal_non_404_error_propagates(monkeypatch, token_env):
    """A 403 (a widened-then-narrowed credential, say) must surface loudly, not
    collapse into the same False a missing signal returns — the proxy above
    this function tells the two apart (404 vs. a 5xx it lets bubble)."""
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        library_client.ack_signal("sig_0001", acting_human="operator@example.com")


def test_unack_signal_uses_delete_and_the_client_credential(monkeypatch, token_env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["method"] = req.get_method()
        seen["auth"] = req.headers["Authorization"]
        return _FakeResponse({"signal_id": "sig_0001", "acknowledged": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.unack_signal("sig_0001", acting_human="operator@example.com") is True
    assert seen["method"] == "DELETE"
    assert seen["auth"] == "Bearer test-client-token"


def test_unack_signal_404_returns_false(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.unack_signal("sig_missing", acting_human="operator@example.com") is False


# --- not-found is a real answer, not a failure -------------------------------


def test_get_issue_item_404_is_none_not_an_exception(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.get_issue_item("issue_missing") is None


def test_get_issue_item_carries_its_thread(monkeypatch, token_env):
    row = _issue_row(1)
    row["comments"] = [{"id": "c1", "actor": "bug-investigator", "kind": "diagnosis",
                        "body": "root cause: X", "run_id": None,
                        "at": "2026-08-01T01:00:00Z"}]

    def fake_urlopen(req, timeout=None):
        return _FakeResponse(row)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    item = library_client.get_issue_item("issue_0001")
    assert item is not None
    assert len(item.comments) == 1
    assert item.comments[0].kind == "diagnosis"
    assert item.comments[0].body == "root cause: X"


def test_a_list_row_carries_no_comments(monkeypatch, token_env):
    """The list route does not inline a thread on every row — only the
    single-item route does (library_client.IssueRecord's docstring)."""
    def fake_urlopen(req, timeout=None):
        return _FakeResponse({"items": [_issue_row(1)], "count": 1, "total": 1,
                              "offset": 0, "truncated": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_issue_items()
    assert got[0].comments == []


# --- Courses (issue_01M0TKX6DW2E1TXGN9QT6K61J5 step 2) ----------------------


def _course_row(n: int, *, entries=None) -> dict:
    """A minimal-but-complete `/courses` list-route row — every key
    `library_client._course_record` reads. `entries` defaults to bare issue id
    strings, matching what the list route actually serves (no per-entry
    detail; see `_course_detail_row` for the `/courses/{id}` shape, which adds
    `items`)."""
    return {
        "id": f"course_{n:04d}", "product": "vivy", "title": f"course {n}",
        "status": "live", "version": 1,
        "entries": entries if entries is not None else ["issue_0001", "issue_0002"],
        "created_by": "ops-mcp", "created_at": "2026-08-01T00:00:00Z",
        "updated_by": "ops-mcp", "updated_at": "2026-08-02T00:00:00Z",
    }


def _course_detail_row(n: int, *, items=None) -> dict:
    """A minimal-but-complete `/courses/{id}` row — the list row plus `items`,
    each entry's issue resolved to its full record (only `id`/`title`/`state`
    matter to `_course_record`'s resolution, but a real Library row carries
    every `IssueRecord` field)."""
    row = _course_row(n)
    default_items = [
        {"id": "issue_0001", "title": "first entry", "state": "fixed",
         "resolved_at": "2026-09-10T12:00:00Z"},
        {"id": "issue_0002", "title": "second entry", "state": "open", "maturity": "approved",
         "specification": "the plan for entry two"},
    ]
    row["entries"] = [it["id"] for it in (items if items is not None else default_items)]
    row["items"] = items if items is not None else default_items
    return row


def test_list_courses_pages_past_the_max_page_clamp(monkeypatch, token_env):
    total = 250
    rows = [_course_row(n) for n in range(total)]
    requests_made = []

    def fake_urlopen(req, timeout=None):
        requests_made.append(req)
        params = _query(req.full_url)
        offset, limit = int(params["offset"]), int(params["limit"])
        assert limit <= library_client.MAX_PAGE
        page = rows[offset:offset + limit]
        return _FakeResponse({
            "courses": page, "count": len(page), "total": total,
            "offset": offset, "truncated": offset + len(page) < total,
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_courses()

    assert len(got) == total
    assert {r.id for r in got} == {f"course_{n:04d}" for n in range(total)}
    assert len(requests_made) == 2


def test_list_courses_defaults_to_status_live_and_no_product_filter(monkeypatch, token_env):
    seen_params = []

    def fake_urlopen(req, timeout=None):
        seen_params.append(_query(req.full_url))
        return _FakeResponse({"courses": [], "count": 0, "total": 0,
                              "offset": 0, "truncated": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.list_courses()
    assert seen_params[0]["status"] == "live"
    assert "product" not in seen_params[0]

    library_client.list_courses(product="vivy", status="all")
    assert seen_params[1]["status"] == "all"
    assert seen_params[1]["product"] == "vivy"


def test_list_courses_entries_carry_ids_only_no_state_or_title(monkeypatch, token_env):
    """The list route does not resolve entries — only `get_course` does. A
    caller reading `CourseEntry.state` off a `list_courses()` row must find
    `None` there rather than a stale or wrong guess."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"courses": [_course_row(1)], "count": 1, "total": 1,
         "offset": 0, "truncated": False}))
    got = library_client.list_courses()
    assert [e.issue_id for e in got[0].entries] == ["issue_0001", "issue_0002"]
    assert all(e.state is None and e.title is None and e.maturity is None
               and e.resolved_at is None
               for e in got[0].entries)


def test_get_course_resolves_every_entry_to_its_state_and_title(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        _course_detail_row(1)))
    course = library_client.get_course("course_0001")
    assert course is not None
    assert course.id == "course_0001"
    assert [e.issue_id for e in course.entries] == ["issue_0001", "issue_0002"]
    assert [e.state for e in course.entries] == ["fixed", "open"]
    assert [e.title for e in course.entries] == ["first entry", "second entry"]
    assert [e.maturity for e in course.entries] == [None, "approved"]
    assert [e.specification for e in course.entries] == [None, "the plan for entry two"]
    # `resolved_at` is the same field `IssueRecord.resolved_at` already parses off
    # `store.item_dict` — resolved here for the entry the Library marked resolved,
    # `None` for the one still open.
    assert course.entries[0].resolved_at == dt.datetime(2026, 9, 10, 12, 0, 0)
    assert course.entries[1].resolved_at is None


def test_get_course_404_is_none_not_an_exception(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.get_course("course_missing") is None


# --- base URL and timestamp parsing ------------------------------------------


def test_base_url_defaults_to_loopback_8100():
    assert library_client.BASE_URL == "http://127.0.0.1:8100"


@pytest.mark.parametrize("loopback", [
    "http://127.0.0.1:8100",
    "http://localhost:8100",
    "http://[::1]:8100",
])
def test_resolve_base_url_allows_plain_http_on_loopback(loopback):
    assert library_client._resolve_base_url(loopback) == loopback


def test_resolve_base_url_allows_https_on_a_remote_host():
    remote = "https://library.internal.convivy.com"
    assert library_client._resolve_base_url(remote) == remote


@pytest.mark.parametrize("remote_http", [
    "http://library.internal.convivy.com",
    "http://library.internal.convivy.com:8100",
    "http://203.0.113.10:8100",
])
def test_resolve_base_url_refuses_plain_http_on_a_remote_host(remote_http):
    """Nothing else enforces the loopback assumption TIMEOUT is sized for, so a
    misconfigured LIBRARY_BASE_URL would otherwise send the bearer token in
    cleartext over the network on every request. Refuse at startup rather than
    silently downgrading."""
    with pytest.raises(RuntimeError, match="cleartext"):
        library_client._resolve_base_url(remote_http)


def test_timestamps_parse_to_naive_utc(monkeypatch, token_env):
    row = _issue_row(1)
    row["created_at"] = "2026-08-08T20:09:32Z"
    row["claimed_at"] = "2026-08-08T21:00:00Z"

    def fake_urlopen(req, timeout=None):
        return _FakeResponse({"items": [row], "count": 1, "total": 1,
                              "offset": 0, "truncated": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_issue_items()[0]
    assert got.created_at.isoformat() == "2026-08-08T20:09:32"
    assert got.created_at.tzinfo is None


# --- the two credentials, and the wall that depends on keeping them apart ----
#
# ADR-0038: `approved_client` is `system` ∪ {signal.ack, decision.decide}, and
# `system` holds `signal.customer`. So the approved-client credential is not a
# strictly-better token to reach for — sending an ordinary read under it opens
# the customer wall on every page this client feeds. `overnight_build._alerts`
# and `waiting_build._unacked_alerts` deleted their own privacy filters on the
# strength of Orient's credential NOT holding that capability, which is what
# makes this an enforceable boundary rather than a preference.


@pytest.fixture
def both_creds(monkeypatch, tmp_path):
    monkeypatch.delenv("LIBRARY_CLIENT_TOKEN", raising=False)
    monkeypatch.delenv("LIBRARY_HUMAN_RESERVED_TOKEN", raising=False)
    agent = tmp_path / "library-token"
    agent.write_text("AGENT-CRED\n")
    client = tmp_path / "library-client-token"
    client.write_text("CLIENT-CRED\n")
    monkeypatch.setattr(library_client, "TOKEN_FILE", agent)
    monkeypatch.setattr(library_client, "CLIENT_TOKEN_FILE", client)
    yield


def _capture(monkeypatch, body):
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append((req.full_url, req.headers["Authorization"]))
        return _FakeResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def test_the_ack_read_is_the_only_call_that_carries_the_client_credential(
        both_creds, monkeypatch):
    """Enumerated over the client's whole read surface rather than spot-checked.

    A test that named two or three readers would go quiet the day a fourth is
    added, and a new reader written against the wrong credential is precisely
    the regression this exists to catch — it would not fail, it would silently
    widen the customer wall.
    """
    ordinary = [
        ("list_issue_items", lambda: library_client.list_issue_items(),
         {"items": [], "count": 0, "total": 0, "offset": 0, "truncated": False}),
        ("get_issue_item", lambda: library_client.get_issue_item("issue_0001"),
         _issue_row(1)),
        ("list_leases", lambda: library_client.list_leases(),
         {"leases": [], "policy": {}, "count": 0, "total": 0, "offset": 0, "truncated": False}),
        ("list_signals", lambda: library_client.list_signals(),
         {"signals": [], "count": 0, "total": 0, "offset": 0, "truncated": False}),
        ("get_signal", lambda: library_client.get_signal("sig_0001"), _signal_row(1)),
        ("signal_facets", lambda: library_client.signal_facets(),
         {"total": 0, "embedded": 0, "by_source": [], "products": []}),
    ]
    for name, call, body in ordinary:
        seen = _capture(monkeypatch, body)
        call()
        assert seen, f"{name} made no request"
        for url, auth in seen:
            assert auth == "Bearer AGENT-CRED", (
                f"{name} carried the APPROVED-CLIENT credential on an ordinary "
                f"read ({url}) — that opens signal.customer on every page fed "
                f"by this call")

    seen = _capture(monkeypatch, {"acked": []})
    library_client.signal_acks(["sig_0001"], acting_human="operator@example.com")
    assert [auth for _, auth in seen] == ["Bearer CLIENT-CRED"], (
        "the ack read must carry the approved-client credential; the agent one "
        "cannot hold signal.ack and the Library refuses it")


def test_a_missing_client_credential_names_itself_rather_than_falling_back(
        monkeypatch, tmp_path):
    """The fallback that is NOT there. Reaching for the agent token here would
    turn a missing credential into a bare 403 from the Library, with nothing
    saying which of the two was wrong."""
    monkeypatch.delenv("LIBRARY_CLIENT_TOKEN", raising=False)
    monkeypatch.delenv("LIBRARY_HUMAN_RESERVED_TOKEN", raising=False)
    agent = tmp_path / "library-token"
    agent.write_text("AGENT-CRED\n")
    monkeypatch.setattr(library_client, "TOKEN_FILE", agent)
    monkeypatch.setattr(library_client, "CLIENT_TOKEN_FILE", tmp_path / "absent")

    def fake_urlopen(req, timeout=None):
        pytest.fail("a request was sent with no approved-client credential")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="no approved-client credential"):
        library_client.signal_acks(["sig_0001"], acting_human="operator@example.com")


# --- probe_route: the never-raising status check backing awaiting_state -----
#
# `design_system.awaiting_state` calls this at render time to re-verify a
# "waiting on the Library" claim rather than trusting a date that can silently
# go stale — two of Orient's three placeholders did exactly that. A probe
# failure must degrade the placeholder, never take the page down with it.


class _FakeStatusResponse:
    """A `urlopen` response double carrying only what `probe_route` reads —
    `.status`, not a JSON body; the paging tests above have their own fake for
    that shape."""

    def __init__(self, status: int):
        self.status = status

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """The cache is a plain module-level dict, not reset between tests on its
    own — without this, whichever test runs first would decide every later
    test's cache hit/miss."""
    library_client._probe_cache.clear()
    yield
    library_client._probe_cache.clear()


def test_probe_route_returns_the_status(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeStatusResponse(200))
    assert library_client.probe_route("/courses") == 200


def test_probe_route_returns_an_http_errors_status_rather_than_raising(
        monkeypatch, token_env):
    """A 404 is an answer a probe needs to be able to NAME — it is exactly
    what confirms Bosun's genuine gap (`GET /runs`) — not a failure to
    swallow into None."""
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.probe_route("/runs") == 404


def test_probe_route_returns_none_rather_than_raising_when_unreachable(
        monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.probe_route("/runs") is None


def test_probe_route_returns_none_rather_than_raising_with_no_credential(monkeypatch):
    # No token_env fixture here: conftest's autouse fixture points both
    # credential files at an empty temp dir, so _token() raises
    # FileNotFoundError before any request would even be built.
    def fake_urlopen(req, timeout=None):
        pytest.fail("a request must not be sent with no credential to sign it")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.probe_route("/runs") is None


def test_probe_route_runs_the_agent_credential_wall_like_every_other_call(
        monkeypatch, token_env):
    """The wall applies here too — no carve-out (PR #202 security review).

    `probe_route` skips `_request` because it has no use for a paged, parsed
    body, and the first version skipped `_verify_agent_credential` along with
    it. That left the module's one check that Orient's credential is not
    wider than it is written for with a documented permanent exception, in the
    one function nothing constrains to stay status-only. It is called inside
    the same `try` as everything else, so the never-raises contract is
    untouched — see the test below."""
    called = []
    monkeypatch.setattr(library_client, "_verify_agent_credential",
                        lambda: called.append(True))
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeStatusResponse(200))
    assert library_client.probe_route("/runs") == 200
    assert called == [True]


def test_probe_route_degrades_to_none_when_the_credential_wall_raises(
        monkeypatch, token_env):
    """Running the wall must not hand a placeholder the power to blank a page.

    A credential wider than Orient is written for is exactly the failure a
    probe must not inherit, so the raise lands in the same handler as a
    refused connection: the placeholder reads "unverified" and the page
    renders."""
    def boom():
        raise RuntimeError("credential wider than Orient is written for")

    monkeypatch.setattr(library_client, "_verify_agent_credential", boom)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeStatusResponse(200))
    assert library_client.probe_route("/runs") is None


def test_probe_route_accepts_a_list_valued_param_without_raising(
        monkeypatch, token_env):
    """A list is the multi-valued shape `_request` takes for `source` and
    `kind`, and the first version of this function built its cache key
    BEFORE the `try` — so `probe_route("/signals", params={"source": [...]})`
    raised `TypeError: unhashable type: 'list'` past the handler that exists
    to make this never raise (PR #202 review). The key normalises lists to
    tuples, and `doseq=True` still sends the query the way `_request` does."""
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return _FakeStatusResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.probe_route(
        "/signals", params={"source": ["ci", "test"]}) == 200
    assert "source=ci" in seen[0] and "source=test" in seen[0]


def test_probe_route_degrades_rather_than_raising_on_an_unhashable_param(
        monkeypatch, token_env):
    """Every other unhashable value degrades to None instead of raising, on
    the WRITE as well as the read.

    A set builds a cache-key tuple happily and only fails when hashed, so
    guarding the read alone left the cache write to raise outside the `try` —
    the same escape as the original finding, one line further down. Nothing
    is cached under a key that never proved hashable."""
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeStatusResponse(200))
    assert library_client.probe_route(
        "/signals", params={"kind": {"a", "b"}}) is None
    assert library_client._probe_cache == {}


def test_probe_route_caches_within_the_ttl_and_makes_one_request(monkeypatch, token_env):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _FakeStatusResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.probe_route("/courses") == 200
    assert library_client.probe_route("/courses") == 200
    assert len(calls) == 1


def test_probe_route_caches_a_none_result_too(monkeypatch, token_env):
    """A down Library must not be re-probed on every render of every page
    carrying a placeholder."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.probe_route("/runs") is None
    assert library_client.probe_route("/runs") is None
    assert len(calls) == 1


def test_probe_route_cache_key_includes_params(monkeypatch, token_env):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _FakeStatusResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.probe_route("/signals/search", params={"q": "a"})
    library_client.probe_route("/signals/search", params={"q": "b"})
    assert len(calls) == 2, "different params must not share a cache entry"


def test_probe_route_expires_after_the_ttl(monkeypatch, token_env):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _FakeStatusResponse(200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fake_clock = [1000.0]
    monkeypatch.setattr(library_client.time, "monotonic", lambda: fake_clock[0])
    library_client.probe_route("/courses")
    fake_clock[0] += library_client._PROBE_CACHE_TTL + 1
    library_client.probe_route("/courses")
    assert len(calls) == 2


# --- Reference documents (issue_01M08YBCDTEFJ6N9S798ERF89P step 3) ----------


def _reference_row(n: int, *, audience=None) -> dict:
    """A minimal-but-complete `/reference/documents` list-row — every key
    `library_client._reference_document` reads from the listing shape
    (confirmed against the live Library 2026-08-24: no `body`, no `snippet`,
    no `score` on this route)."""
    return {
        "id": f"proj/kind/doc-{n:04d}", "source_ref": f"projects/proj/kind/doc-{n:04d}.md",
        "kind": "reference", "project": "proj", "title": f"doc {n}",
        "status": "current", "topics": ["a", "b"],
        "audience": audience if audience is not None else ["team"],
        "last_reviewed": "2026-08-01", "updated_at": "2026-08-02T00:00:00Z",
    }


def _reference_detail_row(n: int, *, body: str = "body text", audience=None) -> dict:
    """A minimal-but-complete `/reference/documents/{id}` row — the listing
    row plus `body`, which only this route serves."""
    row = _reference_row(n, audience=audience)
    row["body"] = body
    return row


def _reference_search_row(n: int, *, snippet: str = "…snippet…", score: float = 4.2) -> dict:
    """A minimal-but-complete `/reference/search` row — the listing row plus
    `snippet` and `score`, which only this route serves."""
    row = _reference_row(n)
    row["snippet"] = snippet
    row["score"] = score
    return row


def test_list_reference_documents_pages_past_the_max_page_clamp(monkeypatch, token_env):
    total = 250
    rows = [_reference_row(n) for n in range(total)]
    requests_made = []

    def fake_urlopen(req, timeout=None):
        requests_made.append(req)
        params = _query(req.full_url)
        offset, limit = int(params["offset"]), int(params["limit"])
        assert limit <= library_client.MAX_PAGE
        page = rows[offset:offset + limit]
        return _FakeResponse({
            "documents": page, "count": len(page), "total": total,
            "offset": offset, "truncated": offset + len(page) < total,
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_reference_documents(audience="team")

    assert len(got) == total
    assert {r.id for r in got} == {f"proj/kind/doc-{n:04d}" for n in range(total)}
    assert len(requests_made) == 2


def test_a_listing_row_decodes_blurb_and_body_len(monkeypatch, token_env):
    """**The `.get`-versus-strict decode is the design point, and it had no
    direct test** (review, PR #280). `tests/library_fake.py` builds a
    `ReferenceDocument` itself and never calls `_reference_document`, so these
    two lines were exercised nowhere — including in the direction of proving the
    correct case."""
    row = dict(_reference_row(1), blurb="A one-line extract.", body_len=8321)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"documents": [row], "count": 1, "total": 1, "offset": 0, "truncated": False}))
    got = library_client.list_reference_documents(audience="team")
    assert got[0].blurb == "A one-line extract."
    assert got[0].body_len == 8321


def test_a_row_without_blurb_or_body_len_decodes_to_none(monkeypatch, token_env):
    """`/reference/search` serves neither field, so the decode has to tolerate
    their absence — a strict read would break search. This pins the tolerance as
    intentional rather than incidental, and pins what a caller sees if the
    listing ever stopped serving them: `None`, which
    `engineering_build.load_docs` renders as an empty blurb and a zero length
    rather than failing. Nothing today would notice that; this test at least
    makes the behaviour a stated one."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"documents": [_reference_row(1)], "count": 1, "total": 1,
         "offset": 0, "truncated": False}))
    got = library_client.list_reference_documents(audience="team")
    assert got[0].blurb is None
    assert got[0].body_len is None


def test_list_reference_documents_carries_no_body(monkeypatch, token_env):
    """The listing route deliberately omits the body — verified against the
    live Library, not assumed — so a row read off it must not fabricate one."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"documents": [_reference_row(1)], "count": 1, "total": 1,
         "offset": 0, "truncated": False}))
    got = library_client.list_reference_documents(audience="team")
    assert got[0].body is None
    assert got[0].snippet is None
    assert got[0].score is None


# --- the audience gate, which lives here rather than in each page -----------
#
# The Library serves the WHOLE reference corpus unfiltered — 359 of its 702
# documents are not team-audience. Before PR #205's review the entitlement check
# lived only in `reference_doc_build`, so the next page built on these functions
# would have had to know to rebuild it, and a page that forgot would have listed
# ids, titles, projects and topics for every restricted document. It is one
# function now and `audience` is a required keyword on all three readers.


def test_get_reference_document_gates_on_audience(monkeypatch, token_env):
    """A document the caller may not read is None — the SAME answer as no such
    document, so a guessed id cannot confirm one exists."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        _reference_detail_row(1, audience=["orient-coder"])))
    assert library_client.get_reference_document(
        "proj/kind/doc-0001", audience="team") is None


@pytest.mark.parametrize("audience", [
    [],                 # entitled to nothing
    "team",             # a bare STRING, not a list — see the bypass note below
    "teamwork",         # `"team" in "teamwork"` is True without the isinstance
    "steam-team",       # substring in the middle, same bypass
    ["Team"],           # case must match exactly
    ["teamwork"],       # a longer value this one is a prefix of
    0,                  # not a collection at all
])
def test_the_audience_gate_fails_closed_on_every_odd_shape(
        monkeypatch, token_env, audience):
    """Fail closed on anything that is not a list containing the exact value.

    An empty list, a bare string rather than a list, a different case, a value
    this one is a prefix of, and a non-collection must all be refused. The
    Library reshaping this field must never widen who can read a document.

    The bare-string cases are the ones with teeth. `_reference_document`'s
    `or []` default catches only FALSY values, so a truthy string survives to
    `_entitled` — and without its `isinstance` guard `audience in aud` becomes
    a substring test, under which `"teamwork"` and `"steam-team"` both grant
    `"team"` access. A bypass rather than a tidiness point, which is why the
    guard carries a comment saying so.

    (The missing-key and explicit-null cases are the test below — this
    fixture's `audience=None` means "use the default", so passing it here would
    be testing the fixture rather than the gate.)"""
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        _reference_detail_row(1, audience=audience)))
    assert library_client.get_reference_document(
        "proj/kind/doc-0001", audience="team") is None


@pytest.mark.parametrize("mutate", [
    lambda row: row.pop("audience"),          # key absent entirely
    lambda row: row.update(audience=None),    # key present, null
])
def test_the_audience_gate_fails_closed_when_the_field_is_absent_or_null(
        monkeypatch, token_env, mutate):
    """A Library response that omits `audience` must block, not allow. This is
    the shape a protocol change could introduce silently, so it is worth its
    own test rather than an assumption about `_reference_document`'s default."""
    row = _reference_detail_row(1)
    mutate(row)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(row))
    assert library_client.get_reference_document(
        "proj/kind/doc-0001", audience="team") is None


def test_list_reference_documents_drops_what_the_audience_may_not_see(
        monkeypatch, token_env):
    rows = [_reference_row(1), _reference_row(2, audience=["orient-coder"]),
            _reference_row(3, audience=[]), _reference_row(4)]
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"documents": rows, "count": len(rows), "total": len(rows),
         "offset": 0, "truncated": False}))
    got = library_client.list_reference_documents(audience="team")
    assert [d.id for d in got] == ["proj/kind/doc-0001", "proj/kind/doc-0004"]


def test_search_reference_drops_what_the_audience_may_not_see(
        monkeypatch, token_env):
    """This one matters more than the listing: a search row carries a SNIPPET,
    so an unfiltered result set leaks document text rather than only metadata."""
    rows = [_reference_search_row(1), _reference_search_row(2)]
    rows[1]["audience"] = ["orient-coder"]
    rows[1]["snippet"] = "…secret agent instructions…"
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"documents": rows, "count": len(rows), "total": len(rows),
         "offset": 0, "truncated": False, "query": "x"}))
    got = library_client.search_reference("x", audience="team")
    assert [d.id for d in got] == ["proj/kind/doc-0001"]
    assert all("secret" not in (d.snippet or "") for d in got)


@pytest.mark.parametrize("call", [
    lambda: library_client.list_reference_documents(),
    lambda: library_client.get_reference_document("proj/kind/doc-0001"),
    lambda: library_client.search_reference("x"),
])
def test_audience_cannot_be_omitted(call):
    """There is deliberately no default. An optional entitlement parameter is
    the one a caller forgets, and forgetting it here is the leak."""
    with pytest.raises(TypeError):
        call()


def test_get_reference_document_carries_the_body(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        _reference_detail_row(1, body="# Title\n\nsome text")))
    doc = library_client.get_reference_document("proj/kind/doc-0001", audience="team")
    assert doc is not None
    assert doc.id == "proj/kind/doc-0001"
    assert doc.body == "# Title\n\nsome text"


def test_get_reference_document_url_quotes_a_slash_bearing_id(monkeypatch, token_env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["path"] = urllib.parse.urlsplit(req.full_url).path
        return _FakeResponse(_reference_detail_row(1))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.get_reference_document("proj/kind/doc-0001", audience="team")
    assert seen["path"] == "/reference/documents/proj%2Fkind%2Fdoc-0001"


def test_get_reference_document_404_is_none_not_an_exception(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.get_reference_document("proj/kind/missing", audience="team") is None


def test_search_reference_carries_snippet_and_score(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"documents": [_reference_search_row(1)], "count": 1, "total": 1,
         "offset": 0, "truncated": False, "query": "orient"}))
    got = library_client.search_reference("orient", audience="team")
    assert got[0].snippet == "…snippet…"
    assert got[0].score == 4.2


def test_search_reference_cap_stops_early_without_paging_past_it(monkeypatch, token_env):
    rows = [_reference_search_row(n) for n in range(50)]
    requests_made = []

    def fake_urlopen(req, timeout=None):
        requests_made.append(req)
        params = _query(req.full_url)
        offset, limit = int(params["offset"]), int(params["limit"])
        page = rows[offset:offset + limit]
        return _FakeResponse({
            "documents": page, "count": len(page), "total": len(rows),
            "offset": offset, "truncated": offset + len(page) < len(rows),
            "query": "orient",
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.search_reference("orient", audience="team", cap=5)
    assert len(got) == 5
    assert len(requests_made) == 1
    seen_limit = int(_query(requests_made[0].full_url)["limit"])
    assert seen_limit == 5


def test_list_reference_topics_reads_the_unpaginated_shape(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse({"topics": [{"topic": "keel", "documents": 30},
                                         {"topic": "security", "documents": 26}],
                              "count": 2})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.list_reference_topics()
    assert [(t.topic, t.documents) for t in got] == [("keel", 30), ("security", 26)]


# --- reference-document coordination (issue_01M0TKX6DW2E1TXGN9QT6K61J5 §4) ---


def _checkout_row(*, doc_id="proj/kind/doc-1", holder="operator@example.com",
                  expires_at="2026-08-25T18:00:00Z"):
    return {"id": "chk_0001", "doc_id": doc_id, "holder": holder, "run_id": None,
            "taken_at": "2026-08-25T17:00:00Z", "heartbeat_at": "2026-08-25T17:00:00Z",
            "expires_at": expires_at, "released_at": None, "release_reason": None,
            "released_by": None}


def _http_error(status: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x", status, "refused", {}, io.BytesIO(json.dumps(body).encode()))


def test_get_reference_checkout_reads_a_live_lock(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"doc_id": "proj/kind/doc-1", "checkout": _checkout_row(), "policy": {}}))
    got = library_client.get_reference_checkout("proj/kind/doc-1")
    assert got.holder == "operator@example.com"


def test_get_reference_checkout_is_none_when_free(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"doc_id": "proj/kind/doc-1", "checkout": None, "policy": {}}))
    assert library_client.get_reference_checkout("proj/kind/doc-1") is None


def test_get_reference_checkout_404_is_none(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.get_reference_checkout("proj/kind/missing") is None


def test_checkout_reference_document_sends_owner_as_holder(monkeypatch, token_env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["path"] = urllib.parse.urlsplit(req.full_url).path
        seen["body"] = json.loads(req.data)
        return _FakeResponse({"checkout": _checkout_row(), "policy": {}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.checkout_reference_document(
        "proj/kind/doc-1", holder="operator@example.com")
    assert seen["path"] == "/reference/documents/proj%2Fkind%2Fdoc-1/checkout"
    assert seen["body"] == {"owner": "operator@example.com"}
    assert got.holder == "operator@example.com"


def test_checkout_reference_document_conflict_carries_the_holder(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise _http_error(409, {
            "error": "already checked out",
            "checkout": _checkout_row(holder="someone-else@example.com"),
            "hint": "an agent may reclaim a checkout whose holder is gone",
        })

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(library_client.ReferenceCheckoutConflict) as exc:
        library_client.checkout_reference_document("proj/kind/doc-1", holder="operator@example.com")
    assert exc.value.checkout.holder == "someone-else@example.com"


def test_checkout_reference_document_503_is_writes_not_enabled(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise _http_error(503, {"error": "the Library is not the write path yet",
                                "hint": "read-only shadow"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(library_client.ReferenceWritesNotEnabled):
        library_client.checkout_reference_document("proj/kind/doc-1", holder="operator@example.com")


def test_checkout_reference_document_other_failure_raises_plain(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        library_client.checkout_reference_document("proj/kind/missing", holder="operator@example.com")


def test_heartbeat_reference_checkout_refreshes(monkeypatch, token_env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["path"] = urllib.parse.urlsplit(req.full_url).path
        return _FakeResponse({"checkout": _checkout_row(), "policy": {}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.heartbeat_reference_checkout("proj/kind/doc-1", holder="operator@example.com")
    assert seen["path"] == "/reference/documents/proj%2Fkind%2Fdoc-1/heartbeat"
    assert got.holder == "operator@example.com"


def test_heartbeat_reference_checkout_404_is_none(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise _http_error(404, {"error": "holds no live checkout"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.heartbeat_reference_checkout(
        "proj/kind/doc-1", holder="operator@example.com") is None


def test_heartbeat_reference_checkout_503_is_writes_not_enabled(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise _http_error(503, {"error": "the Library is not the write path yet"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(library_client.ReferenceWritesNotEnabled):
        library_client.heartbeat_reference_checkout("proj/kind/doc-1", holder="operator@example.com")


def test_checkin_reference_document_releases(monkeypatch, token_env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["path"] = urllib.parse.urlsplit(req.full_url).path
        return _FakeResponse({"checkout": _checkout_row()})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.checkin_reference_document("proj/kind/doc-1", holder="operator@example.com")
    assert seen["path"] == "/reference/documents/proj%2Fkind%2Fdoc-1/checkin"
    assert got.holder == "operator@example.com"


def test_checkin_reference_document_409_is_none(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise _http_error(409, {"error": "not checked out"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.checkin_reference_document(
        "proj/kind/doc-1", holder="operator@example.com") is None


def test_checkin_reference_document_503_is_writes_not_enabled(monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise _http_error(503, {"error": "the Library is not the write path yet"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(library_client.ReferenceWritesNotEnabled):
        library_client.checkin_reference_document("proj/kind/doc-1", holder="operator@example.com")


def test_check_reference_admission_reports_frontmatter_errors(monkeypatch, token_env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["path"] = urllib.parse.urlsplit(req.full_url).path
        seen["body"] = json.loads(req.data)
        return _FakeResponse({"may_land": False, "errors": ["missing required field: title"]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.check_reference_admission({"id": "proj/kind/doc-1"},
                                                   owner="operator@example.com")
    assert seen["path"] == "/reference/admission"
    assert seen["body"] == {"frontmatter": {"id": "proj/kind/doc-1"},
                            "owner": "operator@example.com"}
    assert got.may_land is False
    assert got.errors == ["missing required field: title"]


def test_check_reference_admission_passing_frontmatter(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"may_land": True, "errors": []}))
    got = library_client.check_reference_admission({"id": "proj/kind/doc-1"},
                                                   owner="operator@example.com")
    assert got.may_land is True
    assert got.errors == []


def test_check_reference_admission_cannot_be_called_without_an_owner():
    """Once the Library makes its checkout clause unconditional
    (issue_01M19QW42CFK7TDZCKWK18ECY5), a request with no `owner` is refused, so
    every KB-editor save would fail. A keyword with no default turns an omitted
    owner into a TypeError at the call site instead of a refused save in
    production."""
    with pytest.raises(TypeError):
        library_client.check_reference_admission({"id": "proj/kind/doc-1"})


# --- Tool policy (ADR-0048) — read half (issue_01M1F1DT9SM5V17BF4AV8FTTN2) --


def _policy_row(*, explicit=None, connectors=None, builtins=None, default="allow",
                version=0, agent="scout", crew="test", model="claude-sonnet-5"):
    """A minimal-but-complete `GET /policy/resolve`-shaped row, measured
    against the running Library 2026-09-02 (zero rows in the policy table:
    `explicit` empty, `default` "allow")."""
    return {
        "agent": agent, "crew": crew, "model": model,
        "explicit": explicit if explicit is not None else {},
        "connector_servers": connectors if connectors is not None else [
            "claude_ai_Gmail", "claude_ai_Google_Calendar", "claude_ai_Google_Drive",
            "claude_ai_Notion", "claude_ai_Resy", "claude_ai_Slack",
            "claude_ai_Spotify", "claude_ai_Uber_Eats"],
        "exfiltration_builtins": builtins if builtins is not None else [
            "DesignSync", "RemoteTrigger", "WebFetch"],
        "default": default, "version": version,
    }


def test_resolve_policy_request_and_response_shape(monkeypatch, token_env):
    """Shape measured against the running Library 2026-09-02: eight connector
    servers, three exfiltration built-ins, an empty `explicit` map and
    `default: "allow"` — the live policy table holds zero rows."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["path"] = urllib.parse.urlsplit(req.full_url).path
        seen["params"] = _query(req.full_url)
        return _FakeResponse(_policy_row())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.resolve_policy("scout", "test", "claude-sonnet-5")

    assert seen["path"] == "/policy/resolve"
    assert seen["params"] == {"agent": "scout", "crew": "test", "model": "claude-sonnet-5"}
    assert got.agent == "scout"
    assert got.crew == "test"
    assert got.model == "claude-sonnet-5"
    assert got.explicit == {}
    assert got.connector_servers == [
        "claude_ai_Gmail", "claude_ai_Google_Calendar", "claude_ai_Google_Drive",
        "claude_ai_Notion", "claude_ai_Resy", "claude_ai_Slack",
        "claude_ai_Spotify", "claude_ai_Uber_Eats"]
    assert got.exfiltration_builtins == ["DesignSync", "RemoteTrigger", "WebFetch"]
    assert got.default == "allow"
    assert got.version == 0


def test_resolve_policy_strips_whitespace_before_sending(monkeypatch, token_env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["params"] = _query(req.full_url)
        return _FakeResponse(_policy_row())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.resolve_policy(" scout ", "test\t", "\nclaude-sonnet-5")

    assert seen["params"] == {"agent": "scout", "crew": "test", "model": "claude-sonnet-5"}


@pytest.mark.parametrize("field", ["agent", "crew", "model"])
def test_resolve_policy_raises_locally_on_empty_after_strip(field, monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise AssertionError("must not make a request for a value empty after stripping")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    kwargs = {"agent": "scout", "crew": "test", "model": "claude-sonnet-5"}
    kwargs[field] = "   "
    with pytest.raises(ValueError, match=field):
        library_client.resolve_policy(**kwargs)


def test_policy_resolution_decision_prefers_an_explicit_override(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        _policy_row(explicit={"WebFetch": "allow"})))
    got = library_client.resolve_policy("scout", "test", "claude-sonnet-5")
    assert got.decision("WebFetch") == "allow"


def test_policy_resolution_decision_denies_an_unlisted_connector_server(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        _policy_row()))
    got = library_client.resolve_policy("scout", "test", "claude-sonnet-5")
    assert got.decision("claude_ai_Gmail") == "deny"


def test_policy_resolution_decision_denies_an_unlisted_exfiltration_builtin(
        monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        _policy_row()))
    got = library_client.resolve_policy("scout", "test", "claude-sonnet-5")
    assert got.decision("WebFetch") == "deny"


def test_policy_resolution_decision_falls_back_to_default_otherwise(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        _policy_row(default="allow")))
    got = library_client.resolve_policy("scout", "test", "claude-sonnet-5")
    assert got.decision("kb") == "allow"


def test_policy_resolution_decision_falls_back_to_a_non_default_default(monkeypatch, token_env):
    """`default` rides the wire rather than being hardcoded `"allow"` here —
    this proves `.decision` reads it rather than assuming it."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        _policy_row(default="deny")))
    got = library_client.resolve_policy("scout", "test", "claude-sonnet-5")
    assert got.decision("kb") == "deny"


# --- Tool policy (ADR-0048) — write half (issue_01M1F1ENN8P6RD4W3SJJ04HR45) --


def _write_response(**over):
    base = {"agent": "scout", "crew": "test", "model": "claude-sonnet-5",
            "target": "WebFetch", "decision": "deny", "acting_human": "operator@example.com",
            "attested_by": "orient", "version": 1}
    base.update(over)
    return base


def test_write_policy_puts_under_the_client_credential(monkeypatch, token_env):
    """`PUT /policy`, not POST — and the APPROVED-CLIENT credential, matching
    every other route the Library reserves for a verified human
    (`ack_signal`, `create_signal(acting_human=...)`)."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["method"] = req.get_method()
        seen["url"] = req.full_url
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = json.loads(req.data)
        return _FakeResponse(_write_response())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = library_client.write_policy(
        agent="scout", crew="test", model="claude-sonnet-5", target="WebFetch",
        decision="deny", acting_human="operator@example.com")

    assert seen["method"] == "PUT"
    assert seen["url"].endswith("/policy")
    assert seen["auth"] == "Bearer test-client-token"
    assert seen["body"] == {
        "agent": "scout", "crew": "test", "model": "claude-sonnet-5",
        "target": "WebFetch", "decision": "deny", "acting_human": "operator@example.com"}
    assert got == _write_response()


@pytest.mark.parametrize("field", ["agent", "crew", "model", "target", "decision", "acting_human"])
def test_write_policy_raises_locally_on_an_empty_field(field, monkeypatch, token_env):
    def fake_urlopen(req, timeout=None):
        raise AssertionError("must not make a request for a field empty after stripping")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    kwargs = {"agent": "scout", "crew": "test", "model": "claude-sonnet-5",
              "target": "WebFetch", "decision": "deny", "acting_human": "operator@example.com"}
    kwargs[field] = "   "
    with pytest.raises(ValueError, match=field):
        library_client.write_policy(**kwargs)


def test_write_policy_does_not_run_the_agent_credential_check(monkeypatch, token_env):
    """`human_reserved=True` skips `_verify_agent_credential` — the same
    property `test_a_human_reserved_call_does_not_run_the_check` pins for the
    signal-ack pair, and pinned the same way: `/whoami` is given an answer
    that would FAIL the check (`signal.customer` present), so if the write
    path ever asked it, this test would see a raised `RuntimeError` rather
    than silently passing."""
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFIED", False)
    fake, seen = _whoami_first(["signal.read", "signal.customer"],
                               lambda req: _FakeResponse(_write_response()),
                               kind="approved_client")
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    library_client.write_policy(
        agent="scout", crew="test", model="claude-sonnet-5", target="WebFetch",
        decision="deny", acting_human="operator@example.com")
    assert seen == ["/policy"]  # the write only — no /whoami check


# --- machine-state sample on a Library timeout (issue_01M2KW4H2CTXXXM2JKWB6NZG09) ---


def test_a_successful_request_samples_nothing(monkeypatch, token_env):
    """The sampler must fire only on the failure path. Written so it fails if
    that call is ever moved (or added) to the success path: a future
    `record_timeout()` call on a normal 200 would trip this assertion."""
    calls = []
    monkeypatch.setattr(library_client.machine_sample, "record_timeout",
                        lambda **kw: calls.append(kw))

    def fake_urlopen(req, timeout=None):
        return _FakeResponse({"items": [], "count": 0, "total": 0,
                              "offset": 0, "truncated": False})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.list_issue_items()
    assert calls == []


def test_a_timeout_error_samples_and_still_raises(monkeypatch, token_env):
    calls = []
    monkeypatch.setattr(library_client.machine_sample, "record_timeout",
                        lambda **kw: calls.append(kw))

    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(TimeoutError):
        library_client.list_issue_items()

    assert len(calls) == 1
    assert calls[0]["operation"] == "/items"
    assert calls[0]["direction"] == "read"


def test_a_urlerror_wrapping_a_timeout_samples_and_still_raises(monkeypatch, token_env):
    """`urlopen` can surface a connect-level timeout as `URLError` whose
    `.reason` is the underlying `TimeoutError` rather than raising
    `TimeoutError` directly — both shapes must be sampled."""
    calls = []
    monkeypatch.setattr(library_client.machine_sample, "record_timeout",
                        lambda **kw: calls.append(kw))

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.URLError):
        library_client.list_issue_items()

    assert len(calls) == 1


def test_an_ordinary_urlerror_does_not_sample(monkeypatch, token_env):
    """Connection refused is not a timeout and must not be sampled — only a
    `URLError` whose `.reason` is actually a `TimeoutError` qualifies."""
    calls = []
    monkeypatch.setattr(library_client.machine_sample, "record_timeout",
                        lambda **kw: calls.append(kw))

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.URLError):
        library_client.list_issue_items()

    assert calls == []


def test_an_http_error_does_not_sample(monkeypatch, token_env):
    """A 4xx/5xx is the Library answering, not stalling — this is explicitly
    not the failure this item covers, and must never be sampled as one."""
    calls = []
    monkeypatch.setattr(library_client.machine_sample, "record_timeout",
                        lambda **kw: calls.append(kw))

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        library_client.list_issue_items()

    assert calls == []


def test_a_sampler_that_raises_does_not_block_the_timeout_from_propagating(
        monkeypatch, token_env):
    """`library_client._log_timeout` wraps the sampler in its own `try`, on
    top of `machine_sample.record_timeout`'s own — belt and suspenders. This
    test breaks the inner belt (patches `record_timeout` itself to raise) to
    prove the outer suspenders still hold."""
    monkeypatch.setattr(library_client.machine_sample, "record_timeout",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(TimeoutError):
        library_client.list_issue_items()


def test_write_operations_are_sampled_as_write(monkeypatch, token_env):
    calls = []
    monkeypatch.setattr(library_client.machine_sample, "record_timeout",
                        lambda **kw: calls.append(kw))

    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(TimeoutError):
        library_client.create_signal(source="ci", kind="test-result", actor="ci-bot",
                                     product="vivy")

    assert len(calls) == 1
    assert calls[0]["direction"] == "write"


def test_delete_with_a_body_is_sampled_as_write(monkeypatch, token_env):
    """The one shape neither GET-no-body nor plain POST covers: a DELETE
    carrying a JSON body (the ack-withdrawal route)."""
    calls = []
    monkeypatch.setattr(library_client.machine_sample, "record_timeout",
                        lambda **kw: calls.append(kw))

    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(TimeoutError):
        library_client.unack_signal("sig_0001", acting_human="operator@example.com")

    assert len(calls) == 1
    assert calls[0]["direction"] == "write"


# --- approving an epic (issue_01M32CB6A9T04NVYPARGDT2GWQ) --------------------
#
# The client layer of the approve route, missing from this file when PR #316
# was first reviewed while `test_approve_api.py` claimed it lived here. The
# route-level tests stub `approve_epic` wholesale, so without these the
# `human_reserved=True` wiring — the credential requirement that is the whole
# point of the fix — and `_refusal_message`'s malformed-body fallback were
# exercised nowhere.


def test_approve_epic_carries_the_client_credential_and_the_acting_human(
        both_creds, monkeypatch):
    """**The credential is the authorization and the human is the record.**
    `epic.approve` resolves only on the approved-client credential — Orient's
    agent credential does not hold it, confirmed against the live Library — so
    sending this under the agent token would be refused outright. Asserted
    together with the body, because the credential without the field records
    nobody and the field without the credential is a 403; either alone is the
    bug."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = json.loads(req.data)
        return _FakeResponse({"item": {"id": "issue_1"},
                              "approval": {"approved_by": "orient",
                                           "acting_human": "operator@example.com",
                                           "course_id": "course_1",
                                           "position": 3}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    approval = library_client.approve_epic("issue_1", acting_human="operator@example.com")

    assert seen["auth"] == "Bearer CLIENT-CRED"
    assert seen["url"].endswith("/items/issue_1/approve")
    assert seen["body"] == {"acting_human": "operator@example.com"}
    assert approval.approved_by == "orient"
    assert approval.acting_human == "operator@example.com"
    assert approval.course_id == "course_1"
    assert approval.position == 3


def test_approve_epic_quotes_an_id_containing_a_slash(both_creds, monkeypatch):
    """The route's path parameter is a `:path` converter, so an id may carry
    slashes. Quoted with `safe=''` so it cannot address a different Library
    route."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _FakeResponse({"approval": {}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.approve_epic("a/b/../c", acting_human="operator@example.com")
    assert "/items/a%2Fb%2F..%2Fc/approve" in seen["url"]


def test_approve_epic_passes_owner_only_when_given(both_creds, monkeypatch):
    """The Library treats the approval as a checked-out write. An absent
    `owner` and an empty one are different requests, so the key is omitted
    rather than sent as null."""
    bodies = []

    def fake_urlopen(req, timeout=None):
        bodies.append(json.loads(req.data))
        return _FakeResponse({"approval": {}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.approve_epic("issue_1", acting_human="operator@example.com")
    library_client.approve_epic("issue_1", acting_human="operator@example.com", owner="co-x")
    assert "owner" not in bodies[0]
    assert bodies[1]["owner"] == "co-x"


def _raw_http_error(code: int, payload: bytes):
    """An HTTPError over RAW BYTES, which the sibling helper 560 lines up
    cannot give: that one takes a dict and always produces valid JSON, and the
    malformed-body case below needs a body that is not JSON at all.

    Named distinctly rather than overloading it — an earlier draft of this
    block called it `_http_error` too, and since Python binds the later
    definition, every reference-document test above it started passing a dict
    to a function expecting bytes. The suite caught it; a reader would not
    have."""
    return urllib.error.HTTPError(
        "http://library/items/issue_1/approve", code, "refused", {},
        io.BytesIO(payload))


def test_a_4xx_becomes_a_refusal_carrying_the_librarys_own_wording(
        both_creds, monkeypatch):
    """The page names what was refused, and the causes — no specification, no
    live Course for the product, held by another session — are several
    different 409s that a status code cannot tell apart."""
    def fake_urlopen(req, timeout=None):
        raise _raw_http_error(409, json.dumps(
            {"error": "an epic needs a specification before it can be approved"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(library_client.EpicApprovalRefused) as caught:
        library_client.approve_epic("issue_1", acting_human="operator@example.com")
    assert caught.value.status == 409
    assert "specification" in caught.value.message


def test_a_5xx_is_not_dressed_up_as_a_refusal(both_creds, monkeypatch):
    """A fault is not a rejection. Reporting one as a refusal would tell the
    human their epic was turned down when the Library fell over, and they would
    go and edit an epic that was fine."""
    def fake_urlopen(req, timeout=None):
        raise _raw_http_error(500, b"upstream exploded")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        library_client.approve_epic("issue_1", acting_human="operator@example.com")


def test_a_refusal_with_an_unreadable_body_still_names_something(
        both_creds, monkeypatch):
    """**The case `_refusal_message`'s own docstring calls "exactly the
    malformed cases nobody tests."** A refusal that arrives without readable
    JSON must still produce something the page can show, or the "renders
    naming what was refused" requirement fails on the one shape no happy-path
    test covers."""
    for payload in (b"", b"<html>502 Bad Gateway</html>", b'{"error": ""}',
                    b'["not", "a", "dict"]'):
        def fake_urlopen(req, timeout=None, _p=payload):
            raise _raw_http_error(409, _p)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(library_client.EpicApprovalRefused) as caught:
            library_client.approve_epic("issue_1", acting_human="operator@example.com")
        assert caught.value.message.strip(), f"empty message for payload {payload!r}"
        assert "409" in caught.value.message, (
            f"the fallback must name the status for payload {payload!r}")


def test_a_refusal_prefers_detail_or_message_when_error_is_absent(
        both_creds, monkeypatch):
    """Three keys are read because the Library's own error shapes are not
    uniform across its routes; the fallback below them is the status."""
    for key in ("error", "detail", "message"):
        def fake_urlopen(req, timeout=None, _k=key):
            raise _raw_http_error(409, json.dumps({_k: f"refused via {_k}"}).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(library_client.EpicApprovalRefused) as caught:
            library_client.approve_epic("issue_1", acting_human="operator@example.com")
        assert caught.value.message == f"refused via {key}"


# --- revising an epic's text (the epic editor) --------------------------------
#
# The three calls `epic_revise_api` makes in one request. The route-level tests
# stub them wholesale, so the credential and request shapes are pinned here.


@pytest.mark.parametrize("call,action,kwargs,body", [
    (library_client.checkout_item, "checkout", {"owner": "operator@example.com"},
     {"owner": "operator@example.com"}),
    (library_client.revise_item, "revision",
     {"owner": "operator@example.com", "fields": {"specification": "new"}},
     {"owner": "operator@example.com", "specification": "new"}),
    (library_client.release_item, "release", {"owner": "operator@example.com"},
     {"owner": "operator@example.com"}),
])
def test_the_edit_calls_go_under_the_agent_credential(
        both_creds, monkeypatch, call, action, kwargs, body):
    """`/revision` checks `item.resolve`, which the AGENT credential holds and
    which is not a human-reserved act, so these must not borrow the approved
    client's credential: that one exists for the acts the Library reserves for
    a verified human, and widening what it is used for widens what it signs."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = json.loads(req.data)
        return _FakeResponse({"item": {"id": "issue_1"}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    call("a/b", **kwargs)
    assert seen["auth"] == "Bearer AGENT-CRED"
    assert seen["url"].endswith(f"/items/a%2Fb/{action}")
    assert seen["body"] == body


def test_an_edit_refusal_carries_the_librarys_wording_and_names_the_act(
        both_creds, monkeypatch):
    """Another session's lease is the refusal a human editing an epic will
    actually meet, and the page has to say whose. The fallback names the act,
    so an unreadable refusal does not read as a refused approval."""
    def held(req, timeout=None):
        raise _raw_http_error(409, json.dumps(
            {"error": "held by co-selector until 18:00"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", held)
    with pytest.raises(library_client.ItemWriteRefused) as caught:
        library_client.checkout_item("issue_1", owner="operator@example.com")
    assert caught.value.message == "held by co-selector until 18:00"

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(_raw_http_error(400, b"")))
    with pytest.raises(library_client.ItemWriteRefused) as caught:
        library_client.revise_item("issue_1", owner="o", fields={"specification": "x"})
    assert caught.value.message == "the Library refused the edit (HTTP 400)"


def test_an_edit_fault_is_not_dressed_up_as_a_refusal(both_creds, monkeypatch):
    def boom(req, timeout=None):
        raise _raw_http_error(503, b"down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(urllib.error.HTTPError):
        library_client.release_item("issue_1", owner="o")


# --- the issue queue's resolve and filing (issue_01M3J6D09C38T4AT0SGZ20XHDW) --


def test_a_resolve_goes_under_the_agent_credential_with_the_person_as_owner(
        both_creds, monkeypatch):
    """`/resolve` checks `item.resolve`, which the agent credential holds, so it
    must not borrow the approved client's credential."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen.update(url=req.full_url, auth=req.headers["Authorization"],
                    body=json.loads(req.data))
        return _FakeResponse({"id": "issue_1", "state": "duplicate"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.resolve_item("a/b", owner="operator@example.com", resolution="duplicate",
                                outcome_note="same as issue_2",
                                refs={"duplicate_of": "issue_2"})
    assert seen["auth"] == "Bearer AGENT-CRED"
    assert seen["url"].endswith("/items/a%2Fb/resolve")
    assert seen["body"] == {"owner": "operator@example.com", "resolution": "duplicate",
                            "outcome_note": "same as issue_2",
                            "refs": {"duplicate_of": "issue_2"}}


def test_a_resolve_omits_an_empty_note_and_refs(both_creds, monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data)
        return _FakeResponse({})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    library_client.resolve_item("issue_1", owner="o", resolution="fixed")
    assert seen["body"] == {"owner": "o", "resolution": "fixed"}


def test_a_filing_goes_under_the_agent_credential(both_creds, monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen.update(url=req.full_url, auth=req.headers["Authorization"],
                    body=json.loads(req.data))
        return _FakeResponse({"id": "issue_new", "product": "convivy-orient"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    row = library_client.create_item(product="orient", title="T", body="B",
                                     type="bug", severity="low")
    assert seen["auth"] == "Bearer AGENT-CRED"
    assert seen["url"].endswith("/items")
    assert seen["body"] == {"product": "orient", "title": "T", "body": "B",
                            "type": "bug", "severity": "low"}
    assert row["id"] == "issue_new"


def test_a_filing_refusal_carries_the_librarys_wording(both_creds, monkeypatch):
    def refuse(req, timeout=None):
        raise _raw_http_error(400, json.dumps({"error": "unknown product 'nope'"}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(library_client.ItemWriteRefused) as caught:
        library_client.create_item(product="nope", title="T")
    assert caught.value.status == 400
    assert caught.value.message == "unknown product 'nope'"


# --- an item's model choice (issue_01M38WVA7XHEDN1NFNPP8KDM0C) ---------------


def test_an_item_record_carries_its_model_choice():
    """`model_choice` is on every item record (library-protocol §6.2): null when
    none is set, otherwise the model, the reason, the client that wrote it, the
    person that client named, and when."""
    assert library_client._issue_record(_issue_row(1)).model_choice is None
    row = {**_issue_row(2), "model_choice": {
        "model": "claude-opus-5-5", "reason": "needs frontier judgment",
        "set_by_client": "convivy-orient", "acting_human": "operator@example.com",
        "set_at": "2026-09-27T19:00:00Z"}}
    choice = library_client._issue_record(row).model_choice
    assert choice.model == "claude-opus-5-5"
    assert choice.reason == "needs frontier judgment"
    assert choice.set_by_client == "convivy-orient"
    assert choice.acting_human == "operator@example.com"
    assert choice.set_at is not None


def test_set_item_model_carries_the_client_credential_and_the_acting_human(
        both_creds, monkeypatch):
    """`item.model` resolves only on the approved-client credential, as
    `epic.approve` does, so the agent token would be refused outright."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = json.loads(req.data)
        return _FakeResponse({"item": {**_issue_row(1), "model_choice": {
            "model": "claude-opus-5-5", "reason": None, "set_by_client": "convivy-orient",
            "acting_human": "operator@example.com", "set_at": "2026-09-27T19:00:00Z"}}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    item = library_client.set_item_model("a/b", model="claude-opus-5-5",
                                         acting_human="operator@example.com")
    assert seen["auth"] == "Bearer CLIENT-CRED"
    assert seen["url"].endswith("/items/a%2Fb/model")
    assert seen["body"] == {"model": "claude-opus-5-5", "acting_human": "operator@example.com"}
    assert item.model_choice.model == "claude-opus-5-5"


def test_clearing_sends_a_null_model_and_the_reason(both_creds, monkeypatch):
    """A clear is the same route with `model: null`; the key is sent, since a
    body without `model` is a 400 at the Library."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data)
        return _FakeResponse({"item": _issue_row(1)})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    item = library_client.set_item_model("issue_1", model=None,
                                         acting_human="operator@example.com", reason="back to roster")
    assert seen["body"] == {"model": None, "acting_human": "operator@example.com",
                            "reason": "back to roster"}
    assert item.model_choice is None


def test_set_item_model_raises_the_librarys_wording_on_a_refusal(both_creds, monkeypatch):
    def refuse(req, timeout=None):
        raise _raw_http_error(409, b'{"error": "the item is fixed"}')

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(library_client.ItemModelRefused, match="the item is fixed"):
        library_client.set_item_model("issue_1", model="m", acting_human="operator@example.com")


def test_set_item_model_lets_a_fault_propagate(both_creds, monkeypatch):
    def boom(req, timeout=None):
        raise _raw_http_error(503, b"down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(urllib.error.HTTPError):
        library_client.set_item_model("issue_1", model="m", acting_human="operator@example.com")


# --- the spend ledger (issue_01M38TF7S5RXVKB1JG6P4VT9JW) ---------------------


def _entry_row(n: int, *, product="vivy", usd=1.25, voided=False) -> dict:
    """Every key `ledger_entry_dict` serves, so a missing one raises here
    where the real Library never would."""
    return {"id": f"le_{n:04d}", "ledger_id": "led_1", "day": "2026-09-27", "usd": usd,
            "at": "2026-09-27T12:00:00Z", "ingested_at": "2026-09-27T12:00:01Z",
            "source": "gateway", "product": product, "run_id": f"run-{n}", "agent": "coder",
            "model": "claude-sonnet-5", "item_id": None, "reported_by": "gateway",
            "voided_at": "2026-09-27T13:00:00Z" if voided else None}


def test_list_ledger_entries_follows_every_page_and_carries_the_filter(monkeypatch, token_env):
    """The route does no grouping, so a total over a short page set
    under-reports silently. Two pages are served; the reader must ask for the
    second at the first page's length and keep the product filter on both."""
    pages = {0: [_entry_row(1), _entry_row(2)], 2: [_entry_row(3)]}
    asked = []

    def fake_urlopen(req, timeout=None):
        q = _query(req.full_url)
        asked.append(q)
        off = int(q["offset"])
        rows = pages[off]
        return _FakeResponse({"entries": rows, "count": len(rows), "total": 3,
                              "offset": off, "truncated": off + len(rows) < 3})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    entries = library_client.list_ledger_entries(product="vivy")
    assert [e.id for e in entries] == ["le_0001", "le_0002", "le_0003"]
    assert [q["offset"] for q in asked] == ["0", "2"]
    assert all(q["product"] == "vivy" for q in asked)
    assert all(q["limit"] == str(library_client.MAX_PAGE) for q in asked)


def test_a_ledger_entry_carries_its_void(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"entries": [_entry_row(1, voided=True)], "count": 1, "total": 1, "offset": 0,
         "truncated": False}))
    (entry,) = library_client.list_ledger_entries()
    assert entry.voided_at is not None
    assert entry.usd == 1.25 and entry.model == "claude-sonnet-5"


def test_list_ledgers_reads_todays_total_and_caps(monkeypatch, token_env):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(
        {"ledgers": [{"id": "led_1", "scope_kind": "project", "scope_id": "vivy",
                      "day": "2026-09-27", "total_usd": 3.5,
                      "caps": {"warn_usd": 5.0, "soft_cap_usd": None, "hard_cap_usd": 10.0},
                      "state": "ok", "granted_by": "operator", "updated_at": "2026-09-27T00:00:00Z"}],
         "day": "2026-09-27", "count": 1, "total": 1, "offset": 0, "truncated": False}))
    (lg,) = library_client.list_ledgers()
    assert (lg.scope_id, lg.total_usd, lg.hard_cap_usd, lg.soft_cap_usd) == ("vivy", 3.5, 10.0, None)


# --- corrections queues (library/specs/one-home-per-work-item) ------------------


def test_corrections_queue_ids_keeps_a_200_drops_a_404_and_asks_once(monkeypatch, token_env):
    """A 404 is "not a queue", which is also what the Library answers for every
    id before the route ships. A hit is cached, so a page that renders the same
    queue on every request asks the Library once per process."""
    monkeypatch.setattr(library_client, "_corrections_queue_ids", set())
    asked = []

    def fake_urlopen(req, timeout=None):
        asked.append(req.full_url)
        if req.full_url.endswith("/corrections-queues/cq_vivy"):
            return _FakeResponse({"id": "cq_vivy", "product": "vivy"})
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert library_client.corrections_queue_ids({"cq_vivy", "issue_epic"}) == {"cq_vivy"}
    lookups = [u for u in asked if "/corrections-queues/" in u]
    assert len(lookups) == 2
    assert library_client.corrections_queue_ids({"cq_vivy"}) == {"cq_vivy"}
    assert [u for u in asked if "/corrections-queues/" in u] == lookups


def test_corrections_queue_ids_raises_on_a_library_error(monkeypatch, token_env):
    monkeypatch.setattr(library_client, "_corrections_queue_ids", set())

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        library_client.corrections_queue_ids({"cq_vivy"})
