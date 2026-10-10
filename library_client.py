#!/usr/bin/env python3.11
"""The single place Orient talks to the Library (Phase 3 read rewire).

Orient's page builders used to read the vaults (`issue_items`, `decision_items`,
`signals`) in-process through `control_db.session()`. The vault cutover moves
those tables into the Library's own file, so every read has to go over the wire
instead — this module is where that happens, and it is the only place a builder
should ever import `urllib`/`json` to reach the Library.

**Failure is not caught here.** A `control_db` read either returned rows or
raised in-process; over HTTP there is a third outcome — the Library is
unreachable, slow, or refusing — and this module treats it exactly like the
in-process failure did: it raises. `serve.sh` already treats every builder as
non-fatal and server.py wraps every live render in `except Exception`, serving
the static fallback built at the last successful `main()` run. Catching a
request failure here and returning `[]` would turn a Library outage into a page
that renders "no open issues", which is the one answer worse than a 500 — a
reader has no way to tell an empty queue from a service that never answered.

**Every request pages to completion.** The Library's list routes clamp `limit`
to `MAX_PAGE` (200) rather than serving the whole table in one response,
because a client should not (and, past `MAX_OFFSET`, cannot) page arbitrarily
deep into a large result — but a builder that renders only the first page
silently drops every row past it, and the page still looks right. `_page_all`
pages until the Library says `truncated` is false, so a caller here always gets
every row rather than the first 200. The Archives are past `MAX_OFFSET` and so
cannot be walked by offset at all: `_page_signals` resumes by the cursor
`GET /signals` mints, which has no depth limit.

**Neither vault route supports the multi-state filter Orient's queries use**
(`state IN (open, claimed, ...)`) — the Library's `state` query param matches
one value. Rather than issuing one request per state (and re-deriving `total`
by summing across them), every caller here fetches the whole table unfiltered
and does its own filtering/sorting/counting in Python, exactly as the ORM read
did — issue_items is ~750 rows on the live database, so that is a handful of
requests, not a scan.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import machine_sample

# TWO CREDENTIALS, AND KEEPING THEM APART IS THIS FILE'S JOB (ADR-0038).
#
# Same convention `github_capture` uses for its own bearer token: a
# plain-text file under ~/.orient-control/, 0600, never committed.
#
# `TOKEN_FILE` is Orient's AGENT credential and carries every ordinary read. It
# matches an entry in the Library's `api-tokens.json`.
#
# `CLIENT_TOKEN_FILE` is Orient's APPROVED-CLIENT credential, matching an entry
# in the Library's `~/.library/client-tokens.json`, scoped `products: ["*"]`
# because Orient renders every product's pages. It carries ONLY the calls the
# Library reserves for a verified human, and it must not carry anything else:
# `approved_client` is defined as `system` ∪ {signal.ack, decision.decide}, and
# `system` holds `signal.customer`. Send an ordinary read under it and the
# customer wall opens — `waiting_build._unacked_alerts` dropped its own
# privacy filter on the strength of Orient's credential NOT holding that
# capability, so widening it here would render customer-class bodies onto a
# tile every viewer reads, whatever their own tier.
#
# ADR-0038 draws that line at the credential rather than at the caller's
# intentions, so this file draws it the same way: `_request` takes the
# credential as an argument, and only the human-reserved helpers pass the
# client one.
TOKEN_FILE = Path.home() / ".orient-control" / "library-token"
CLIENT_TOKEN_FILE = Path.home() / ".orient-control" / "library-client-token"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _resolve_base_url(raw: str) -> str:
    """`raw`, refusing at import time if it would send the bearer token in
    cleartext.

    TIMEOUT below is sized for a loopback round trip on the assumption that
    BASE_URL stays loopback; nothing enforced that assumption, so pointing
    LIBRARY_BASE_URL at a remote host over plain http would carry the bearer
    token in the clear on every request. Refusing to start is the safer
    failure than silently downgrading to an unencrypted remote connection.
    """
    host = (urllib.parse.urlparse(raw).hostname or "").lower()
    if host not in _LOOPBACK_HOSTS and not raw.startswith("https://"):
        raise RuntimeError(
            f"LIBRARY_BASE_URL={raw!r} is neither loopback nor https — refusing "
            "to start rather than send the bearer token in cleartext over the "
            "network. Use an https:// URL for a remote Library, or leave "
            "LIBRARY_BASE_URL unset to use the local default.")
    return raw


BASE_URL = _resolve_base_url(os.environ.get("LIBRARY_BASE_URL", "http://127.0.0.1:8100"))

# Required on every request — the Library refuses one that omits it rather than
# defaulting, so that a client cannot ship unversioned and discover it the day a
# second version exists (`platform/contracts/library-protocol` §1). This is the
# version this client is written against.
PROTOCOL_HEADER = "Library-Protocol"
PROTOCOL_VERSION = 1

# Loopback, so this is generous relative to the network round trip it is really
# bounding — the failure mode a timeout exists to catch is a Library that hangs
# rather than answers, not ordinary latency.
TIMEOUT = 5.0

# Matches the Library's own MAX_PAGE, so every request asks for as much as the
# Library will ever hand back in one page.
MAX_PAGE = 200


def _token() -> str:
    """The AGENT credential — every ordinary read."""
    tok = os.environ.get("LIBRARY_CLIENT_TOKEN", "").strip()
    if tok:
        return tok
    return TOKEN_FILE.read_text().strip()


def _human_reserved_token() -> str:
    """The APPROVED-CLIENT credential — the calls the Library reserves for a
    verified human, and nothing else.

    **It has no fallback to the agent token, deliberately.** Falling back would
    turn "this deployment has no approved-client credential" into a 403 from the
    Library with no indication of which of the two credentials was wrong, which
    is the confusing symptom `identity.resolve`'s docstring describes from the
    other direction. Raising here names the missing file instead.
    """
    tok = os.environ.get("LIBRARY_HUMAN_RESERVED_TOKEN", "").strip()
    if tok:
        return tok
    try:
        return CLIENT_TOKEN_FILE.read_text().strip()
    except OSError as e:
        raise RuntimeError(
            f"no approved-client credential at {CLIENT_TOKEN_FILE} ({e}) — the "
            f"Library reserves this call for a verified human and Orient's agent "
            f"credential cannot carry it (ADR-0038). Mint one into the Library's "
            f"~/.library/client-tokens.json and write its value here, 0600.") from e


def _credential(human_reserved: bool) -> str:
    """Which of the two credentials this request carries.

    One function rather than a conditional inlined at the call site, so there is
    exactly one place the choice is made and a test can assert on it.
    """
    return _human_reserved_token() if human_reserved else _token()


# THE CAPABILITY ORIENT'S AGENT CREDENTIAL MUST NOT HOLD, CHECKED AGAINST THE
# LIBRARY RATHER THAN ASSUMED (`GET /whoami`).
#
# `signals_build` no longer filters `privacy_class` itself: ADR-0022 gives the
# privacy wall to the Library, and a duplicate filter here would work against
# that design rather than with it. What that trade buys is a single point of
# truth; what it costs is that Orient's own correctness now depends on a fact
# about its credential that nothing in this process could see. Add
# `"kind": "system"` to Orient's entry in the gitignored `api-tokens.json` — a
# file no code review reads — and every full-tier page starts rendering
# customer-class signals, with no error anywhere and no test that fails.
# `convivy-orient#152` is what that looks like when it happens: a tier check in
# the wrong layer served 16,000 internal records to a second caller.
#
# Registering Orient's agent token in the Library's `client-tokens.json` instead
# has the same effect by a different route, since `identity.resolve` consults
# that file first and `approved_client` is a superset of `system`. Asserting on
# the CAPABILITY rather than on `kind` catches both, because both arrive as this
# one string appearing in a set it must never appear in.
FORBIDDEN_AGENT_CAPABILITY = "signal.customer"

# Once per process. `_verifying` is separate from `_verified` so the check's own
# `/whoami` request does not recurse, while a check that RAISED stays unverified
# and refuses the next call too — collapsing the two would let one unreachable
# Library at startup disable the wall for the life of the process.
#
# A LOCK RATHER THAN A SECOND FLAG, BECAUSE THE READERS ARE CONCURRENT
# (reviewer, PR #173). `server.py`'s route handlers are plain `def`, which
# Starlette runs in a thread pool, so two page requests arriving close together
# — routine right after the restart this change requires — run on separate OS
# threads sharing these globals. A flag saying "a check is in flight" waves
# every one of those threads past the wall, not merely the check's own recursive
# call, and the reviewer reproduced exactly that: one thread refused a
# `kind: system` credential correctly while a second thread's read went through
# during the window. So a concurrent caller BLOCKS on the check rather than
# skipping it, and the only caller allowed past is the thread already inside it.
_verify_lock = threading.Lock()
_AGENT_CREDENTIAL_VERIFIED = False
_AGENT_CREDENTIAL_VERIFYING_THREAD: int | None = None


def _verify_agent_credential() -> None:
    """Refuse to serve under a credential wider than Orient is written for.

    **A page that fails to render is recoverable; one that renders customer
    content is not.** So this raises, and the module docstring's reasoning about
    why nothing here catches applies unchanged: `server.py` wraps every live
    render, and a builder that dies leaves the last good static page up.

    **An answer this cannot check is a refusal, not a pass.** A missing
    `capabilities` key raises, and so does a present one that is not a list —
    `set("signal.customer")` on a bare string decomposes it into single
    characters, so the membership test comes back False and the wall opens
    silently (security-reviewer, PR #173). Nothing the Library serves today can
    produce that, which is the point: the check must fail closed on an answer it
    was not written for, rather than on the answers it was.

    **The recursion exemption is per-THREAD, and everyone else waits.** The
    check's own `/whoami` re-enters `_request` on this thread and must pass
    through; a different thread arriving mid-check must block until there is a
    verdict, because "someone is checking" is not "the credential is safe".
    """
    global _AGENT_CREDENTIAL_VERIFIED, _AGENT_CREDENTIAL_VERIFYING_THREAD
    if _AGENT_CREDENTIAL_VERIFIED:
        return
    if _AGENT_CREDENTIAL_VERIFYING_THREAD == threading.get_ident():
        # This thread's own `/whoami`, called from inside the check below. Only
        # the thread holding the lock ever writes that ident, so no other thread
        # can match it.
        return
    with _verify_lock:
        # Re-read under the lock: a thread that waited here may find the check
        # already done, and re-running it would cost a request per concurrent
        # reader on every process start.
        if _AGENT_CREDENTIAL_VERIFIED:
            return
        _AGENT_CREDENTIAL_VERIFYING_THREAD = threading.get_ident()
        try:
            _check_agent_credential()
        finally:
            _AGENT_CREDENTIAL_VERIFYING_THREAD = None
        _AGENT_CREDENTIAL_VERIFIED = True


def _check_agent_credential() -> None:
    """The check itself: ask, and raise unless the answer is one we can read and
    it is narrow. Split from the locking so `_verify_agent_credential` reads as
    the concurrency contract and this reads as the rule."""
    me = _request("/whoami")
    if "capabilities" not in me:
        raise RuntimeError(
            f"{BASE_URL}/whoami answered without a `capabilities` key, so the "
            f"privacy wall cannot be verified. Refusing to serve rather than "
            f"assuming the credential is narrow — an unverifiable answer is not "
            f"a passing one.")
    if not isinstance(me["capabilities"], list):
        raise RuntimeError(
            f"{BASE_URL}/whoami answered with a `capabilities` value of type "
            f"{type(me['capabilities']).__name__}, not a list, so the privacy "
            f"wall cannot be verified. Refusing to serve. This is checked rather "
            f"than assumed because the failure is silent in the direction that "
            f"leaks: a bare string decomposes into characters under `set()`, and "
            f"the membership test then answers False for every capability.")
    caps = set(me["capabilities"])
    if FORBIDDEN_AGENT_CAPABILITY in caps:
        raise RuntimeError(
            f"Orient's Library credential resolves to actor "
            f"{me.get('actor')!r}, kind {me.get('kind')!r}, and it holds "
            f"{FORBIDDEN_AGENT_CAPABILITY!r}. Every page this process renders "
            f"would show customer-class signals to whoever is reading it, "
            f"because `signals_build` relies on the Library to withhold them "
            f"(ADR-0022). Refusing to serve. Fix the credential rather than this "
            f"check: Orient's entry must resolve to the Library's `agent` role, "
            f"which means no \"kind\": \"system\" in api-tokens.json and no entry "
            f"for this token in the Library's client-tokens.json.")


def _iso(raw: str | None) -> dt.datetime | None:
    """An RFC-3339 timestamp (the Library's `_iso`) to a naive-UTC datetime,
    matching the convention every datetime in this codebase already follows."""
    if not raw:
        return None
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def _request(path: str, params: dict | None = None, *,
             human_reserved: bool = False, json_body: dict | None = None,
             method: str | None = None) -> dict:
    """One request against the Library. Raises on any failure rather than
    returning a default — see the module docstring.

    `json_body` makes it a POST. Absent, it is a GET — so a caller has to say it
    is writing, and every existing read stays a read with no argument.

    `human_reserved` picks the APPROVED-CLIENT credential instead of the agent
    one. It defaults to False so a route added later carries the narrow
    credential unless its author says otherwise — the failure that direction is
    a 403 the author sees immediately, where the other direction silently
    widens the customer wall on every page this client renders.

    `doseq=True` so a list-valued param (`source`, `kind`, `signal_id`) repeats
    as `?kind=a&kind=b` — the Library's own `getlist` reads it that way — while
    a plain string value (the overwhelming case) is untouched, since urlencode
    special-cases `str` even under `doseq`.

    `method` overrides urllib's default (GET with no body, POST with one) for
    the one shape neither covers: a DELETE that carries a JSON body (the ack
    withdrawal route).

    **The credential check happens HERE rather than at an entry point somebody
    calls first**, for the reason the Library puts protocol negotiation in a
    middleware rather than on each route: the check a new read path can silently
    skip is the check that stops being true. It costs one extra request per
    process.
    """
    if not human_reserved:
        _verify_agent_credential()
    query = f"?{urllib.parse.urlencode(params, doseq=True)}" if params else ""
    headers = {PROTOCOL_HEADER: str(PROTOCOL_VERSION),
               "Authorization": f"Bearer {_credential(human_reserved)}"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{BASE_URL}{path}{query}", data=data, headers=headers,
        method=method or None)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except TimeoutError:
        _log_timeout(path, method=method, json_body=json_body)
        raise
    except urllib.error.HTTPError:
        # A `URLError` subclass, so it would otherwise fall into the branch
        # below — but a 4xx/5xx is the Library answering, not stalling, and
        # must never be sampled or treated as a timeout. Listed first and
        # re-raised unchanged so it never reaches that check.
        raise
    except urllib.error.URLError as e:
        # The other shape a timeout can arrive in: connection-level, wrapped
        # by urlopen, with `.reason` set to the underlying `TimeoutError`.
        if isinstance(e.reason, TimeoutError):
            _log_timeout(path, method=method, json_body=json_body)
        raise


def _log_timeout(path: str, *, method: str | None, json_body: dict | None) -> None:
    """Best-effort machine-state sample on the one path this module raises
    for a reason worth investigating on the machine rather than in the
    Library: see `machine_sample` (issue_01M2KW4H2CTXXXM2JKWB6NZG09).

    **Wrapped in its own `try` here, on top of `record_timeout`'s own** —
    belt and suspenders on the one property that matters most: a bug in the
    sampler must never cost a caller the `TimeoutError` it is about to
    re-raise."""
    try:
        machine_sample.record_timeout(
            operation=path, direction=machine_sample.classify_direction(
                method=method, json_body=json_body))
    except Exception:  # noqa: BLE001, S110 — a sampler bug must not surface as an error
        pass


# TTL-cached below, keyed on (path, sorted params). A live page already talks
# to the Library once per request, so one more cached loopback probe per
# placeholder is close to free; a static build re-runs it once at build time.
_PROBE_CACHE_TTL = 300.0
_PROBE_TIMEOUT = 3.0
_probe_cache: dict[tuple, tuple[float, int | None]] = {}


def probe_route(path: str, *, params: dict | None = None) -> int | None:
    """The HTTP status the Library answers for a GET of `path`, or `None` when
    the call could not be made at all — connection refused, timeout, a missing
    credential.

    **This never raises, and nothing it does may be outside a `try`.** A
    placeholder that calls this must not be able to blank a page over it — the
    same discipline `engineering_e2e_build.live_html` already applies to a
    malformed signal. That contract is structural here rather than a promise:
    the whole body runs inside the block below, because the first version built
    the cache key before the `try` and a list-valued `params` — the shape
    `_request` supports for `source` and `kind` — raised an unhashable-type
    error the handler never saw (PR #202 review). List values — the one
    multi-valued shape `_request` takes — are normalised to tuples for the key,
    and the query still goes out through `doseq=True` exactly as `_request`
    sends it. Any other unhashable value raises inside the block and degrades
    to `None` like every other failure, rather than being given a shape this
    module does not otherwise support.

    It does not go through `_request`, which would page and parse a body this
    function has no use for. **It runs `_verify_agent_credential` anyway**, so
    the module's one check that Orient's credential is not wider than it is
    written for applies to every Library call without exception. A raise there
    degrades this to `None` like any other failure, which is what the security
    review asked for: uniform, rather than a documented permanent carve-out in
    the one function that skips it.

    TTL-cached 300 seconds in a module-level dict, and a `None` result is
    cached on the same TTL — a Library that is down should not be re-probed on
    every render. The timeout is its own, short one: a hung Library must
    degrade the placeholder to "no probe" rather than hang the page behind it.

    **The cache read above returns before `_verify_agent_credential` runs on a
    hit, which reads like the check being skippable — it is not.**
    `_verify_agent_credential`'s own flag only ever moves False→True, so once
    the check has passed once in this process it returns instantly for every
    later caller regardless of where in the call it runs; a credential widened
    Library-side without an Orient restart is already invisible to every
    caller here, including `_request`, and that is a property of the once-per-
    process check itself, not of where this function happens to consult it
    (PR #202 review).

    **The cache-key list normalisation above is currently unreachable in
    production**, and is directly tested rather than exercised by any live
    caller: every call site today passes `probe[0]` alone with no `params`, so
    the key is always `(path, ())`. It exists because review found the
    un-normalised version raising past this function's own never-raises
    handler when a list-valued `params` reached it. A future caller that does
    pass `params` should re-derive that reasoning rather than assume this path
    is exercised anywhere today.
    """
    status: int | None
    key: tuple | None = None
    now = time.monotonic()
    try:
        candidate = (path, tuple(sorted(
            (k, tuple(v) if isinstance(v, list) else v)
            for k, v in (params or {}).items())))
        cached = _probe_cache.get(candidate)
        # Only now is the key known to be hashable — `.get` is what proves it,
        # since an unhashable value builds a tuple happily and fails on hash.
        # `key` stays None until then so the write below cannot raise outside
        # this block, which is the same escape the review caught on the read.
        key = candidate
        if cached is not None and now - cached[0] < _PROBE_CACHE_TTL:
            return cached[1]
        _verify_agent_credential()
        query = f"?{urllib.parse.urlencode(params, doseq=True)}" if params else ""
        headers = {PROTOCOL_HEADER: str(PROTOCOL_VERSION),
                   "Authorization": f"Bearer {_token()}"}
        req = urllib.request.Request(f"{BASE_URL}{path}{query}", headers=headers)
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception:
        status = None
    if key is not None:
        _probe_cache[key] = (now, status)
    return status


def _page_all(path: str, key: str, params: dict | None = None) -> list[dict]:
    """Every row behind a paginated list route — see the module docstring on
    why a single request is not enough. `params` are filters carried on every
    page."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = _request(path, {**(params or {}), "limit": MAX_PAGE, "offset": offset})
        page_rows = page[key]
        rows.extend(page_rows)
        if not page["truncated"]:
            return rows
        offset += len(page_rows)


def _not_found(path: str) -> dict | None:
    try:
        return _request(path)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


@dataclass
class Comment:
    """One thread comment, on either an issue or a decision. `kind` is
    issue-only (a decision comment carries none)."""

    id: str
    actor: str
    body: str
    kind: str | None = None
    run_id: str | None = None
    at: dt.datetime | None = None


def _comment(raw: dict) -> Comment:
    return Comment(id=raw["id"], actor=raw["actor"], body=raw["body"],
                   kind=raw.get("kind"), run_id=raw.get("run_id"),
                   at=_iso(raw.get("at")))


@dataclass
class ModelChoice:
    """The model an operator chose for a work item — `model_choice` on every
    item record, written only by `POST /items/{id}/model` (library-protocol
    §6.2). `set_by_client` comes from the credential that wrote it, so the
    Library attests it; `acting_human` is the name that client reported and
    is a claim, not a verified identity."""

    model: str
    reason: str | None
    set_by_client: str
    acting_human: str
    set_at: dt.datetime | None


def _model_choice(raw: dict | None) -> ModelChoice | None:
    if not raw:
        return None
    return ModelChoice(model=raw["model"], reason=raw.get("reason"),
                       set_by_client=raw["set_by_client"],
                       acting_human=raw["acting_human"], set_at=_iso(raw["set_at"]))


@dataclass
class IssueRecord:
    """One `issue_items` row, in the shape `issue_build.py`'s renderers already
    read (`item.state`, `item.claimed_at`, ...) — every field `item_dict`
    serves. `comments` is empty on a row read from the list route (the Library
    does not inline a thread on every row of a page); `get_issue_item` fills it
    in for the one item it fetches by id."""

    id: str
    url: str
    product: str
    title: str
    body: str
    type: str
    state: str
    severity: str | None
    needs: str | None
    maturity: str | None
    parent_id: str | None
    topics: list
    refs: dict
    specification: str | None
    definition_of_done: str | None
    resolution: str | None
    resolved_by: str | None
    outcome_note: str | None
    claimed_by: str | None
    claimed_at: dt.datetime | None
    # Lease-aware (ARCH-424/428), computed by the Library's own lease.py —
    # served here rather than re-derived, so a caller does not need its own
    # session or its own lease lookup to show a claim's true expiry.
    claim_expires_at: dt.datetime | None
    claim_expired: bool
    resolved_at: dt.datetime | None
    created_by: str
    created_by_kind: str | None
    created_at: dt.datetime
    run_id: str | None
    park_reason: str | None
    unpark_condition: str | None
    parked_at: dt.datetime | None
    # Prerequisites replace parking as the way a blocker is recorded
    # (platform/decisions/0074). `blocked` is the Library's own computation over
    # `prerequisites` — an item is blocked while any entry is unmet — and is
    # served rather than re-derived here, because the same rule gates what the
    # claimable listing returns and two implementations of it would drift.
    #
    # An entry is either `{"text": str, "met": bool}`, which needs a judge, or
    # `{"item": id, ...}`, which clears itself when that item resolves. An
    # item-shaped entry the reader may not see arrives as
    # `{"item": id, "missing": True}` with no state or title.
    #
    # `park_reason` and `unpark_condition` above are history now: all 26 parked
    # items migrated and `state=parked` returns none. The operator decided on 2026-09-11
    # that `parked` leaves the state vocabulary, so those two fields survive only
    # as the record of why a migrated item was once held.
    blocked: bool
    prerequisites: list
    comments: list = field(default_factory=list)
    # The operator's model choice, or None when none is set. Defaulted so a
    # record built without it reads as "no choice", which is what `null` means.
    model_choice: ModelChoice | None = None
    # The epic whose work turned this item up (issue_01M3BFN34Y8P8K39RWS6MM89ZT).
    # Unlike `parent_id` it leaves the item in the corrections lane, and the
    # Library refuses an epic from another product.
    found_during: str | None = None


def _issue_record(raw: dict) -> IssueRecord:
    return IssueRecord(
        id=raw["id"], url=raw["url"], product=raw["product"], title=raw["title"],
        body=raw["body"], type=raw["type"], state=raw["state"],
        severity=raw["severity"], needs=raw["needs"], maturity=raw["maturity"],
        parent_id=raw["parent_id"], topics=raw["topics"], refs=raw["refs"],
        specification=raw["specification"],
        definition_of_done=raw["definition_of_done"], resolution=raw["resolution"],
        resolved_by=raw["resolved_by"], outcome_note=raw["outcome_note"],
        claimed_by=raw["claimed_by"], claimed_at=_iso(raw["claimed_at"]),
        claim_expires_at=_iso(raw["claim_expires_at"]),
        claim_expired=raw["claim_expired"], resolved_at=_iso(raw["resolved_at"]),
        created_by=raw["created_by"], created_by_kind=raw["created_by_kind"],
        created_at=_iso(raw["created_at"]), run_id=raw["run_id"],
        park_reason=raw["park_reason"], unpark_condition=raw["unpark_condition"],
        parked_at=_iso(raw["parked_at"]),
        blocked=raw["blocked"], prerequisites=raw["prerequisites"],
        model_choice=_model_choice(raw["model_choice"]),
        found_during=raw["found_during"])


def list_issue_items() -> list[IssueRecord]:
    """Every `issue_items` row, every state, every product — paged to
    completion. Callers filter/sort/count in Python, the way the ORM read did."""
    return [_issue_record(r) for r in _page_all("/items", "items")]


def list_found_during(epic_id: str) -> list[IssueRecord]:
    """Every item, in every state, whose `found_during` names `epic_id`, read
    through the Library's own `?found_during=` filter."""
    return [_issue_record(r) for r in _page_all("/items", "items", {"found_during": epic_id})]


def list_related_item_ids(epic_id: str) -> list[str]:
    """The ids of items the Library relates to `epic_id` across a product
    boundary, read from `related_items` on `GET /items/{id}`
    (issue_01M1JSPH7DBHG0BZFPJPNNZGSC). These are not `parent_id` children, so
    no listing carries them. Raises on any failure, including a Library that
    omits the field: a caller must show the list as unavailable, never empty."""
    raw = _request(f"/items/{urllib.parse.quote(epic_id, safe='')}")
    return [r["id"] for r in raw["related_items"]]


def get_issue_item(item_id: str) -> IssueRecord | None:
    """One issue with its full thread, or None if there is no such issue (or it
    is out of this credential's scope — the Library answers both the same way,
    deliberately, so a caller cannot tell the two apart)."""
    raw = _not_found(f"/items/{urllib.parse.quote(item_id, safe='')}")
    if raw is None:
        return None
    item = _issue_record(raw)
    item.comments = [_comment(c) for c in raw.get("comments", [])]
    return item


# Ids the Library has answered for as corrections queues. A product's queue is
# never replaced (library/specs/one-home-per-work-item), so a hit is kept for the
# life of the process and only an unknown id costs a request.
_corrections_queue_ids: set[str] = set()


def corrections_queue_ids(ids) -> set[str]:
    """The members of `ids` that name a corrections queue, each looked up with
    `GET /corrections-queues/{id}`. A queue is identified by this lookup and
    never by its `cq_` prefix, which the plan rules out.

    A 404 means "not a queue". The Library answers it for an epic, a dangling
    id, an id out of this credential's scope, and every id before the route
    ships with the Library's PR 1, so a caller that also counts a null parent
    as a correction reads the data correctly on either side of the migration.
    Any other failure raises, like every read in this module."""
    found = set()
    for item_id in ids:
        if item_id not in _corrections_queue_ids:
            if _not_found(f"/corrections-queues/{urllib.parse.quote(item_id, safe='')}") is None:
                continue
            _corrections_queue_ids.add(item_id)
        found.add(item_id)
    return found


@dataclass
class LeaseRecord:
    """One `issue_leases` row, in the shape `lease.lease_dict` serves. Carries
    no `product` — the Library scopes a lease through its ROOT ITEM's product
    rather than a denormalized column (`GET /leases`'s own docstring); a
    caller that needs product has to join against `list_issue_items()` by
    `root_id`, same as the Library's own listing joins internally."""

    id: str
    root_id: str
    root_url: str
    taken_for: str
    holder: str
    run_id: str | None
    taken_at: dt.datetime | None
    heartbeat_at: dt.datetime | None
    expires_at: dt.datetime | None
    dead: bool
    released_at: dt.datetime | None
    release_reason: str | None
    released_by: str | None
    note: str | None


def _lease_record(raw: dict) -> LeaseRecord:
    return LeaseRecord(
        id=raw["id"], root_id=raw["root_id"], root_url=raw["root_url"],
        taken_for=raw["taken_for"], holder=raw["holder"], run_id=raw.get("run_id"),
        taken_at=_iso(raw.get("taken_at")), heartbeat_at=_iso(raw.get("heartbeat_at")),
        expires_at=_iso(raw.get("expires_at")), dead=raw["dead"],
        released_at=_iso(raw.get("released_at")), release_reason=raw.get("release_reason"),
        released_by=raw.get("released_by"), note=raw.get("note"))


@dataclass
class LeasePolicy:
    """The Library's current lease tunables, in `lease.policy()`'s own shape —
    reported on every `/leases` response so a caller can tell the terms a
    lease was computed under rather than hardcoding them. Both are seconds;
    both move (`lease.py`'s `MIN_GRACE` is one minute, well under the
    near-expiry threshold `crews_build` warns at), which is the whole reason
    this rides the wire instead of living as a constant here."""

    grace_seconds: int
    max_lease_seconds: int


@dataclass
class LeaseListResult:
    """`list_leases`'s own envelope: the rows plus the policy they were
    computed under (`SignalSearchResponse`'s pattern — a plural response
    grows past a bare list once there's a second thing to hand back).
    `policy` is `None` for every shape this client cannot use, and there are
    three of them rather than the two an earlier version of this docstring
    named: the Library omitted it, served it as something other than an
    object, or served an object whose `grace_seconds`/`max_lease_seconds` are
    absent or not integers. A caller must treat `None` as "unknown" in all
    three, not as license to assume either arm of
    `min(heartbeat_at + grace, taken_at + max_lease)`.

    Enumerated exhaustively on purpose. This docstring has now overclaimed
    twice — first saying malformed input was handled when only an empty
    value was, then naming two of the three paths after that was fixed —
    and each time the code was right and the promise was wrong, which is the
    harder version to notice."""

    leases: list[LeaseRecord]
    policy: LeasePolicy | None


def _page_leases(params: dict) -> tuple[list[dict], dict]:
    """Every `/leases` row matching `params`, paged to completion, plus the
    `policy` object the first page carried — same shape as `_page_signals`,
    since `/leases` takes a fixed filter (`state`) plus pagination rather than
    `_page_all`'s no-filter case. `policy` reports the Library's current
    tunables rather than anything scoped to a row, so it does not vary across
    pages of the same call and there is nothing to reconcile by reading it
    once."""
    rows: list[dict] = []
    policy: dict = {}
    offset = 0
    while True:
        page = _request("/leases", {**params, "limit": MAX_PAGE, "offset": offset})
        page_rows = page["leases"]
        rows.extend(page_rows)
        if offset == 0:
            raw_policy = page.get("policy")
            policy = raw_policy if isinstance(raw_policy, dict) else {}
        if not page["truncated"]:
            return rows, policy
        offset += len(page_rows)


def list_leases(*, state: str = "live") -> LeaseListResult:
    """Leases at `state` ("live" — the default, or "all" to include released
    ones), paged to completion, with the grace/max-lease policy they were
    computed under. A caller judging how close a lease is to expiry needs
    this to tell which of the two tunables actually bounds it — see
    `crews_build._lease_state`."""
    rows, policy = _page_leases({"state": state})
    parsed_policy = None
    if isinstance(policy.get("grace_seconds"), int) and isinstance(policy.get("max_lease_seconds"), int):
        parsed_policy = LeasePolicy(grace_seconds=policy["grace_seconds"],
                                    max_lease_seconds=policy["max_lease_seconds"])
    return LeaseListResult(leases=[_lease_record(r) for r in rows], policy=parsed_policy)


@dataclass
class LeaseRefusalRecord:
    """One `lease_refusals` row — a collision the fence actually caught, in the
    shape `GET /lease-refusals` serves.

    **Carries its own `product`, unlike `LeaseRecord`.** A lease is scoped
    through its root item and has to be joined to get one; a refusal names the
    product directly, because the Library scopes this route with the same
    `_scope_clause` every other Chart House read uses. So a caller grouping
    refusals per crew needs no join.

    **`blocks_by` IS COARSER THAN IT LOOKS, and a reader will get this wrong.**
    The enum has exactly two values, `"ancestor"` and `"descendant"`, and
    `lease.live_conflicts` walks `[root_id, *ancestor_ids(root_id)]` tagging
    every one of them `"ancestor"` — so a lease sitting on the very item that
    was asked for is `"ancestor"` too, identically to one sitting on an epic
    three levels up. It does NOT mean "an epic fenced a sibling" (reviewer,
    PR #200, correcting exactly that reading).

    **AND THE DISTINCTION IT HIDES IS NOT RECOVERABLE FROM THIS ROW.** The
    obvious move is to compare `blocking_root_id` against `requested_item_id`
    and call them equal a race for the same item. That is wrong for the case it
    most needs to be right about (reviewer, PR #200): `root_id_for` makes a LEAF
    lease its parent, so two crews racing for the identical leaf under one epic
    store `requested_item_id = L` and `blocking_root_id = E` and never compare
    equal — the shape the comparison exists to name is exactly the shape it
    misses. It is right only when the requested item is its own root.

    Recovering it needs the requester's own root, which is not on the wire, or
    the blocking lease's `taken_for`, which is on `/leases` rather than here.
    Joining to `/leases` would work until a lease is purged and then degrade
    silently, which is the failure this record's `blocked` field exists to
    avoid. So this client does not claim the distinction, and callers should
    not infer it. Asked of the Library as
    `issue_01M0RCF408K7TBE0N9VP224MGA`.

    **`blocked` is the withheld case, and it must not be mistaken for absent
    data.** When the blocking subtree is out of the caller's scope, the Library
    omits `blocking_lease_id`, `blocking_root_id` and `blocking_holder` and
    sends `blocked: true` instead — deliberately, so the row reads as "something
    held this" rather than "nothing did". Reading only the three omitted keys
    would render that as three blanks, which looks like a parsing fault rather
    than a privacy boundary. Orient's own credential is `products: ["*"]` today
    so it never sees this shape, and that is a fact about one token rather than
    anything the contract promises.
    """

    id: str
    at: dt.datetime | None
    requested_item_id: str
    product: str | None
    requester: str
    actor: str | None
    blocks_by: str | None
    blocking_lease_id: str | None
    blocking_root_id: str | None
    blocking_holder: str | None
    blocked: bool = False

    @property
    def fence_was_the_requested_item(self) -> bool | None:
        """Whether the fence that refused this sat on the requested item itself
        rather than on a subtree containing it.

        **This is NOT "the two crews wanted the same work"**, and naming it that
        was the round-2 error. A leaf leases its parent, so a genuine race for
        one leaf answers False here. What it does say is where the fence sat,
        which is what the page renders.

        `None` for a withheld blocker rather than False: there is no root to
        compare, and False would read as a checked negative — the landmine a
        later caller reading this without checking `blocked` would step on
        (reviewer, PR #200)."""
        if self.blocked or self.blocking_root_id is None:
            return None
        return self.blocking_root_id == self.requested_item_id


def _lease_refusal_record(raw: dict) -> LeaseRefusalRecord:
    return LeaseRefusalRecord(
        id=raw["id"], at=_iso(raw.get("at")),
        requested_item_id=raw["requested_item_id"], product=raw.get("product"),
        requester=raw["requester"], actor=raw.get("actor"),
        blocks_by=raw.get("blocks_by"),
        blocking_lease_id=raw.get("blocking_lease_id"),
        blocking_root_id=raw.get("blocking_root_id"),
        blocking_holder=raw.get("blocking_holder"),
        blocked=bool(raw.get("blocked")))


def list_lease_refusals(*, product: str | None = None) -> list[LeaseRefusalRecord]:
    """Every collision the lease fence refused, newest first, paged to
    completion. `product` narrows WITHIN what this credential may already see
    and can never widen it — the Library applies its own scope clause first.

    **Paged to completion rather than capped**, matching `list_leases`. A
    refusal only exists when two crews reach for one fenced subtree, so this
    table grows at the rate collisions happen rather than the rate work happens —
    one row on the live store as of 2026-08-23, against 18,455 signals. If it
    ever grows enough for the paging to matter, that growth is itself the
    finding this route exists to surface, and a silent cap would hide it.
    """
    params = {"product": product} if product else {}
    return [_lease_refusal_record(r)
            for r in _page_refusals(params)]


def _page_refusals(params: dict) -> list[dict]:
    """Every `/lease-refusals` row matching `params`, paged to completion —
    same shape as `_page_leases`, since this route also takes a fixed filter
    plus pagination rather than `_page_all`'s no-filter case."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = _request("/lease-refusals", {**params, "limit": MAX_PAGE, "offset": offset})
        page_rows = page["refusals"]
        rows.extend(page_rows)
        if not page["truncated"]:
            return rows
        offset += len(page_rows)


# `agentctl-review` writes this stamp as the leading block of a `pr_comment` or
# `changelog` body it captures from a review. Anchored to the START of the body
# so a comment occurring later in genuine content is left alone
# (issue_01M15C5CDS28TKXJCA8KAMSBWK).
_STAMP_RE = re.compile(r"^\s*<!--\s*agentctl-review\b.*?-->\s*", re.DOTALL)


def _strip_leading_stamp(body: str) -> str:
    return _STAMP_RE.sub("", body, count=1)


@dataclass
class LiveRunRecord:
    """One row of the Bosun's live in-memory run register, in the shape
    `GET /runs` serves — a different store from the local
    `~/Convivy/agent-runs/<date>/run-*/meta.json` files `crews_build` reads
    for history: this is what is running RIGHT NOW, not what has run."""

    run_id: str | None
    agent: str | None
    task: str | None
    started_at: dt.datetime | None
    repo: str | None
    worktree: str | None
    pid: int | None
    parent_run: str | None
    holds: list


def _live_run_record(raw: dict) -> LiveRunRecord:
    return LiveRunRecord(
        run_id=raw.get("run_id"), agent=raw.get("agent"), task=raw.get("task"),
        started_at=_iso(raw.get("started_at")), repo=raw.get("repo"),
        worktree=raw.get("worktree"), pid=raw.get("pid"),
        parent_run=raw.get("parent_run"), holds=raw.get("holds") or [])


@dataclass
class LiveRunsResult:
    """`GET /runs`'s own envelope. **`generated_at` is `None` on every
    degraded answer (`available: False`, `reason` naming which failure) and
    also on a successful answer whose payload simply omits the key** — a
    caller must null-check it regardless of what `available` says, exactly
    like `crews_build`'s module docstring already states about this route.
    `stale_after_seconds` is `None` under the same two circumstances."""

    runs: list[LiveRunRecord]
    available: bool
    reason: str | None
    generated_at: dt.datetime | None
    stale_after_seconds: float | None
    count: int


def list_live_runs() -> LiveRunsResult:
    """The Bosun's live in-flight run register (`GET /runs`). Not paged —
    the route serves its whole in-memory set in one response, since it is
    holding live process state rather than a growing table.

    Raises on request failure like every other read in this module (module
    docstring): the Library being unreachable is not this function's to
    swallow. A **degraded** answer is different — a 200 whose body reports
    `available: False` with a `reason` — and that is the Library
    successfully telling this client its own upstream (the Bosun) isn't
    answering; this function passes that through rather than raising on it,
    and the caller renders it as degraded, never as "no runs"."""
    raw = _request("/runs")
    runs = [_live_run_record(r) for r in raw.get("runs") or []]
    return LiveRunsResult(
        runs=runs, available=bool(raw.get("available")), reason=raw.get("reason"),
        generated_at=_iso(raw.get("generated_at")),
        stale_after_seconds=raw.get("stale_after_seconds"),
        count=raw.get("count", len(runs)))


@dataclass
class SignalRecord:
    """One `signals` row, in the shape the signal-reading builders already read
    (`sig.product`, `sig.kind`, ...) — every key `store.signal_dict` serves.
    `dedup_key` and `embedded_at` stay off the response, matching the Library's
    own omission (an idempotency key and an internal index stamp, neither
    actionable by a reader) — see `store.signal_dict`'s docstring in the
    Library repo.

    **`body` has already had a leading `agentctl-review` stamp stripped, here,
    once — not by any of the builders that read it.** A stamp-led `pr_comment`
    or `changelog` body reached three separate render sites unstripped before
    a reader could see the raw `<!-- agentctl-review ... -->` block on
    `/signals/prs`, `/signals/changelogs`, `/api/signals/browse` and
    `/api/signals/search` (convivy-orient#254 round 2); centralizing the strip
    where every one of those already gets its `SignalRecord` from is what
    makes a new render site safe by construction rather than by remembering to
    call a helper. No caller in this repo reads the stamp's own content — the
    only in-repo consumer of its PRESENCE (`signals_build._card`'s "no notes
    beyond the review stamp" message) uses `stamp_only` instead of the raw
    text, so stripping it here costs nothing anything here actually needs.
    `agentctl review-status` and the merge gate read the stamp from the PR
    comment on GitHub, never from a captured signal's body, so nothing outside
    this repo depends on it surviving here either."""

    id: str
    source: str
    stream: str | None
    kind: str
    actor: str
    ingested_by: str | None
    product: str
    body: str
    refs: dict
    meta: dict
    privacy_class: str
    produced_at: dt.datetime | None
    ingested_at: dt.datetime
    stamp_only: bool = False


def _signal_record(raw: dict) -> SignalRecord:
    raw_body = raw["body"] or ""
    body = _strip_leading_stamp(raw_body)
    return SignalRecord(
        id=raw["id"], source=raw["source"], stream=raw.get("stream"),
        kind=raw["kind"], actor=raw["actor"], ingested_by=raw.get("ingested_by"),
        product=raw["product"], body=body, refs=raw.get("refs") or {},
        meta=raw.get("meta") or {}, privacy_class=raw["privacy_class"],
        produced_at=_iso(raw.get("produced_at")), ingested_at=_iso(raw["ingested_at"]),
        stamp_only=bool(raw_body.strip()) and not body.strip())


def _page_signals(params: dict, *, cap: int | None) -> list[dict]:
    """Signal rows matching `params`, in the order the caller asked for.

    `cap`, unlike `_page_all`'s unconditional page-to-completion, stops once
    `cap` rows are in hand. Every signal-reading builder wants a bounded top-N
    (the browse page's 60 most recent, e2e's 400, ...) rather than the whole
    18,455-row table, and the `source`/`kind` filters already keep what remains
    small — issue_01KZW183XVJCKFGQT8HMXFJFTY measured e2e's unfiltered scan at
    93 requests for 26 rows out of 18,455. `cap=None` pages to completion,
    matching `_page_all`, for a caller that genuinely wants everything.

    **THE WALK RESUMES BY CURSOR, BECAUSE OFFSET PAGING CANNOT REACH THE END OF
    THIS TABLE** (issue_01M06Y897250HGZHFQH2VP1YZS). The Library refuses an
    offset above `MAX_OFFSET` (10,000) — rightly, since a clamped offset
    silently returns a different page than the one asked for — and the Archives
    hold ~19,700 rows visible to Orient's credential. The old loop counted an
    offset upward into that ceiling, raised `HTTPError` partway through, and
    `signals_build` wrote its static fallback, so the page rendered stale data
    with nothing saying so. A keyset walk has no depth limit, because it never
    counts rows it has passed.

    It is also correct under concurrent writes, which offset paging is not: a
    signal captured mid-walk shifts every later offset by one, so the client
    re-reads one row and skips another. The cursor names the last row seen.

    **`offset` is omitted entirely once a cursor is in hand**, rather than sent
    as a harmless zero. `GET /signals` refuses a request carrying both, on the
    grounds that honouring one and ignoring the other returns a different page
    than the caller asked for, and it tests for the parameter's PRESENCE rather
    than its value.

    **The offset path stays for an order the Library will not keyset-walk.**
    `next_cursor` comes back `None` for `order=produced_at`: that key spans a
    nullable column and a keyset predicate across a NULL boundary is a
    different and easily-wrong shape, so the Library refuses rather than
    guessing. Falling back leaves that order behaving exactly as it does today,
    including its 10,000-row ceiling. No caller pages it to completion —
    `release_notes_build` is the only one that asks for it and it caps at 200,
    one page — and this self-heals into a cursor walk if the Library ever mints
    tokens for that order.
    """
    rows: list[dict] = []
    cursor = None
    while cap is None or len(rows) < cap:
        limit = MAX_PAGE if cap is None else min(MAX_PAGE, cap - len(rows))
        page_params = {**params, "limit": limit}
        if cursor is None:
            # THE OFFSET IS `len(rows)` RATHER THAN A COUNTER THIS LOOP KEEPS,
            # and that is what makes a fallback mid-walk safe (security-reviewer,
            # PR #192). A separate counter is only advanced on the branch that
            # reads it, so a walk that took even one cursor page would leave it
            # frozen behind the real position; falling back to offset later then
            # re-issues a page already read, and the caller silently receives
            # duplicates rather than an error. Rows in hand IS the position, so
            # there is no second number to keep in step.
            page_params["offset"] = len(rows)
        else:
            page_params["cursor"] = cursor
        page = _request("/signals", page_params)
        page_rows = page["signals"]
        rows.extend(page_rows)
        if not page["truncated"]:
            break
        # `next_cursor` is served on every listing, not only on a cursor page,
        # precisely so a walk can switch to it after the first request without
        # composing a token out of fields it would then have to keep composing
        # correctly. A `None` here means this order is not keyset-walkable, so
        # the offset branch above carries the walk exactly as it always has.
        #
        # **A NON-STRING IS TREATED AS NO CURSOR, NOT PASSED THROUGH.**
        # `urlencode(..., doseq=True)` would iterate a list or dict and emit
        # several `cursor=` parameters rather than refusing, which is a stranger
        # failure than the offset walk this falls back to. It is the same
        # discipline `_check_agent_credential` applies to a `capabilities` value
        # that is present but not a list: an answer this code was not written
        # for is refused rather than decomposed.
        cursor = page.get("next_cursor")
        if not isinstance(cursor, str) or not cursor:
            cursor = None
    return rows


def list_signals(*, source: list[str] | None = None, kind: list[str] | None = None,
                  stream: list[str] | None = None,
                  product: str | None = None, order: str | None = None,
                  cap: int | None = None) -> list[SignalRecord]:
    """Signal rows matching an optional `source`/`kind`/`stream`/`product`
    filter (`source`/`kind`/`stream` each repeatable and ORed within itself,
    `product` a single value — `GET /signals`'s own params), in `order`
    (`None`/`"ingested_at"` — the Library's default — or `"produced_at"`), up to
    `cap` rows or the whole matching set if `cap` is None.

    `product` is a PRE-filter, applied by the Library before paging — unlike
    `GET /signals/search`, which retrieves a page and filters `product`
    afterward, so asking that route for 5 product-scoped rows can return far
    fewer (issue_01M0ZXSVZG8K9DGG2CSX0JYE5K). A caller that wants an exact,
    product-scoped top-N (a per-product fair sample across a large kind, e.g.
    the Archives PRs/Changelogs sections) needs this pre-filter; a caller that
    only wants to display a product badge client-side should keep filtering in
    the page instead, as `/signals`'s own chips do.

    No privacy filter: privacy is the Library's wall — Orient's credential
    holds no `signal.customer` capability, so a customer-class row never
    reaches here, the same way it never reached the old `internal`-filtered ORM
    query."""
    params: dict = {}
    if source:
        params["source"] = source
    if kind:
        params["kind"] = kind
    if stream:
        params["stream"] = stream
    if product:
        params["product"] = product
    if order:
        params["order"] = order
    return [_signal_record(r) for r in _page_signals(params, cap=cap)]


def get_signal(signal_id: str) -> SignalRecord | None:
    """One signal, or None if it does not exist, is out of this credential's
    scope, or is customer-class content `signal.read` alone does not cover —
    the Library answers all three the same way, deliberately, the same
    not-found/out-of-scope collapse `get_issue_item` documents."""
    raw = _not_found(f"/signals/{urllib.parse.quote(signal_id, safe='')}")
    if raw is None:
        return None
    return _signal_record(raw)


@dataclass
class SignalFacets:
    """Aggregates over a filter's WHOLE matching set, never a page of it —
    `GET /signals/facets`'s own shape."""

    total: int
    embedded: int
    by_source: list[tuple[str, int]]
    products: list[str]


def signal_facets(*, source: list[str] | None = None,
                   kind: list[str] | None = None) -> SignalFacets:
    """The total, embedded count, source breakdown and distinct product list a
    filter matches — one request, where paging the whole table to compute these
    by hand would cost 93 requests over the 18,455 signals on the live database
    (issue_01KZW183XVJCKFGQT8HMXFJFTY)."""
    params: dict = {}
    if source:
        params["source"] = source
    if kind:
        params["kind"] = kind
    raw = _request("/signals/facets", params)
    return SignalFacets(total=raw["total"], embedded=raw["embedded"],
                        by_source=[(s, n) for s, n in raw["by_source"]],
                        products=raw["products"])


@dataclass
class SignalSearchResult:
    """One `GET /signals/search` row: a `SignalRecord` plus its KNN
    `distance` — lower is closer, matching the values the running Library
    returns (measured 2026-08-27: three deploy-related rows at 0.7464,
    0.7507, 0.754, closest first). Not folded into `SignalRecord` itself,
    since an ordinary listing row carries no distance."""

    signal: SignalRecord
    distance: float | None


@dataclass
class SignalSearchResponse:
    """`GET /signals/search`'s own envelope. **No `total`, deliberately** —
    unlike `GET /signals`, this route retrieves an oversampled KNN window
    rather than scanning the matching set, so there is no whole-store count to
    report (platform/contracts/library-protocol §3.2). A caller must not
    render `count` or `truncated` as anything but a fact about the retrieved
    window — see `search_signals`'s docstring."""

    results: list[SignalSearchResult]
    count: int
    truncated: bool


def search_signals(query: str, *, source: list[str] | None = None,
                   product: str | None = None, cap: int | None = None) -> SignalSearchResponse:
    """`GET /signals/search`: nearest-neighbor text search, ranked closest
    first. **This is not `GET /signals` and does not behave like it.**

    `source`/`product` narrow the RETRIEVED WINDOW, not the store — the
    Library computes an oversampled KNN window first and applies these
    filters after, so a row that is real and would appear on `list_signals`
    can still come back empty here. Measured live against this database
    (issue_01M11WR1ZHSDSBZAE1HTZS6P6Z): `deploy_watch` holds 11 real rows,
    all deploy failures, and `search_signals("deploy", source=["deploy_watch"])`
    answers zero of them. `kind` is not accepted as a parameter here at all —
    the Library refuses it 400 for the same reason, and worse: a kind rare
    enough to matter (110 of 21,000+ rows) is diluted out of the window
    before the filter ever runs, so an empty answer would read as "none
    exist" when hundreds do. `list_signals(kind=[...])` is the route that
    filters the whole table; nothing here should be read as a substitute for
    it.

    `cap` sets the window size (the wire's own `limit`); there is no walk to
    completion the way `_page_signals`/`_page_reference_search` provide,
    because the window has no "next page" — asking for more only widens what
    the Library retrieves before ranking, it does not resume a prior read."""
    params: dict = {"q": query}
    if source:
        params["source"] = source
    if product:
        params["product"] = product
    if cap:
        params["limit"] = cap
    raw = _request("/signals/search", params)
    results = [SignalSearchResult(signal=_signal_record(r), distance=r.get("distance"))
              for r in raw["signals"]]
    return SignalSearchResponse(results=results, count=raw["count"], truncated=raw["truncated"])


def signal_acks(signal_ids: list[str], *, acting_human: str) -> set[str]:
    """Which of `signal_ids` `acting_human` has acknowledged — `GET
    /signals/acks`, resolved through the same per-identity attestation the
    write pair (`POST`/`DELETE /signals/{id}/ack`) uses. Empty input makes no
    request; there is nothing to ask."""
    if not signal_ids:
        return set()
    raw = _request("/signals/acks", {"signal_id": signal_ids, "acting_human": acting_human},
                   human_reserved=True)
    return set(raw["acked"])


def create_signal(*, source: str, kind: str, actor: str, product: str,
                  body: str = "", stream: str | None = None, refs: dict | None = None,
                  meta: dict | None = None, privacy_class: str | None = None,
                  dedup_key: str | None = None,
                  acting_human: str | None = None) -> tuple[str, bool, SignalRecord | None]:
    """Capture one Archives record — `POST /signals` — and return `(id,
    idempotent, existing)`.

    **WHICH CREDENTIAL THIS CARRIES IS DECIDED BY `acting_human`, and that is
    the whole of the rule.** Absent, this is the AGENT credential, matching the
    four `vault_plane` capture jobs: Orient reporting something it observed, on
    its own behalf. Present, it is the APPROVED-CLIENT credential, because
    `identity.attested_human` in the Library refuses `acting_human` from any
    other kind — recording the client in that column is exactly the identity
    ADR-0038 draws the line to keep out of it.

    **Absent is the default and stays the common case.** Most captures have no
    person behind them (the GitHub poller, the CI ingress, every agent filing
    what it saw), and the Library's own route documents that absence as the
    design rather than a gap: requiring a person would break those callers and
    invite them to invent one. So a caller says whose act this is, or says
    nothing and keeps the narrow credential.

    **An empty `acting_human` raises here rather than travelling.** Passing one
    means a caller believed it knew the person; sending an empty string would
    earn a 403 the caller reads as a permission problem, and dropping it
    silently would record the capture as unattested with nothing saying so.

    **The approved-client credential also lifts the customer-class limit** —
    it is `system` plus the human-reserved capabilities, so it holds
    `signal.create_customer` where the agent credential never does. That is the
    capability `signal_capture_api.py`'s own per-human `signal.create_customer`
    gate has been checking into a wall since the capture write moved to the
    Library; ADR-0038 puts that scoping on the client, and this is the
    credential that carries what the client authorized.

    **`idempotent` is True for a `dedup_key` collision the Library answered
    with the existing row (its own 200, not an error) rather than raising**,
    and `existing` is that row, parsed from the SAME response — never `None`
    when `idempotent` is True, always `None` when it is False.

    A caller that only wants the id can ignore both. A caller distinguishing
    "created" from "duplicate" — `github_capture.py`'s overlap-window poller,
    the manual capture form's own double-submit guard — needs `idempotent`:
    collapsing both outcomes to a bare id would make a legitimate retry
    indistinguishable from a fresh row, which is exactly the regression a
    round of review caught in `signal_capture_api.py` (a hard error on every
    collision, breaking both of those callers, which the deleted
    `signal_api.py` never did).

    A caller that needs to re-authorize a collision against ITS OWN actual
    product/privacy_class — `signal_capture_api.py`'s own dedup path, since a
    collision may resolve to a row outside the caller's scope even when the
    product it declared was in scope — needs `existing`, parsed here rather
    than fetched with a second `get_signal` call. A second call would give the
    idempotent path one more round trip than the true-409 path, which is
    exactly the kind of asymmetry a timing side channel lives in (security
    review, PR #179 round 5): a caller could distinguish "collision I can't
    see" from "no collision at all" by how long the 409 took, even with the
    response body byte-identical either way.
    """
    body_json = {"source": source, "kind": kind, "actor": actor, "product": product,
                "body": body}
    if stream is not None:
        body_json["stream"] = stream
    if refs is not None:
        body_json["refs"] = refs
    if meta is not None:
        body_json["meta"] = meta
    if privacy_class is not None:
        body_json["privacy_class"] = privacy_class
    if dedup_key is not None:
        body_json["dedup_key"] = dedup_key
    if acting_human is not None:
        if not acting_human.strip():
            raise ValueError(
                "acting_human was passed but is empty — say who acted or pass "
                "None; the Library refuses an empty one and recording nothing "
                "under a credential that claims attestation is worse than "
                "capturing as the agent")
        body_json["acting_human"] = acting_human
    raw = _request("/signals", json_body=body_json,
                   human_reserved=acting_human is not None)
    idempotent = bool(raw.get("idempotent"))
    return raw["id"], idempotent, (_signal_record(raw) if idempotent else None)


def ack_signal(signal_id: str, *, acting_human: str) -> bool:
    """Acknowledge a signal as `acting_human` — `POST /signals/{id}/ack`, under
    the APPROVED-CLIENT credential: `signal.ack` is one of the two capabilities
    ADR-0038 reserves for it (`decision.decide` was the other, before the
    decision plane was retired). Idempotent, matching the route.

    Returns False for a signal that is missing, out of scope, or customer-class
    without the capability — the same no-existence-leak the read routes use —
    rather than raising, so a caller can answer the browser with a plain 404."""
    try:
        _request(f"/signals/{urllib.parse.quote(signal_id, safe='')}/ack",
                 json_body={"acting_human": acting_human}, human_reserved=True)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
    return True


def unack_signal(signal_id: str, *, acting_human: str) -> bool:
    """Withdraw `acting_human`'s acknowledgment — `DELETE /signals/{id}/ack`.
    Idempotent and the same not-found rule as `ack_signal`: a misfired tap on a
    phone must be reversible, or the safe move becomes never acking at all."""
    try:
        _request(f"/signals/{urllib.parse.quote(signal_id, safe='')}/ack",
                 json_body={"acting_human": acting_human}, human_reserved=True,
                 method="DELETE")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
    return True


class EpicApprovalRefused(Exception):
    """The Library refused an approval, with a cause worth showing the human.

    A refusal here is ordinary and expected — an epic with no specification, a
    product with no live Course, an epic another session holds — so it is a
    distinct exception rather than a bare HTTPError, and it carries the
    Library's own wording. The specification requires the page to name what was
    refused where the control is, and a caller cannot do that from a status
    code alone.
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class EpicApproval:
    """What `POST /items/{id}/approve` records: the client that approved, the
    human it acted for, and where on the Course the epic landed."""

    approved_by: str | None
    acting_human: str | None
    course_id: str | None
    position: int | None


def approve_epic(issue_id: str, *, acting_human: str,
                 owner: str | None = None) -> EpicApproval:
    """Approve an epic and place it on its product's live Course, as one act —
    `POST /items/{id}/approve`, under the APPROVED-CLIENT credential.

    **THIS IS THE ONLY WAY ORIENT SETS `maturity`, AND THE LIBRARY MAKES IT THE
    ONLY WAY ANYONE DOES.** `/correct` and `/revision` refuse the field by name
    and point here, and no agent credential resolves `epic.approve` at any
    protocol version, so the correction route Deck's old Approve button wrote
    through is closed (issue_01M32CA70BNV0WEW150V3KYBJ9).

    `acting_human` is the record, not the authorization: the capability on
    Orient's credential is what authorizes, and the Library checks nothing
    about the person. It must still be the email Orient VERIFIED through
    Cloudflare Access, never a value off the request body — the same rule
    `signal_capture_api` and `policy_api` state for the same field.

    Approval and Course placement commit together or not at all, so a refusal
    leaves the epic `proposed` and off the Course. Raises `EpicApprovalRefused`
    carrying the Library's own wording for a refusal the human should read (no
    specification, no live Course for the product, another holder's lease), and
    lets anything else propagate as the transport error it is."""
    body: dict = {"acting_human": acting_human}
    if owner:
        body["owner"] = owner
    try:
        raw = _request(f"/items/{urllib.parse.quote(issue_id, safe='')}/approve",
                       json_body=body, human_reserved=True)
    except urllib.error.HTTPError as e:
        # 4xx is the Library saying no for a reason the human needs to read;
        # 5xx is a fault, and dressing one up as a refusal would tell the human
        # their epic was rejected when the Library simply fell over.
        if 400 <= e.code < 500:
            raise EpicApprovalRefused(e.code, _refusal_message(e)) from e
        raise
    approval = (raw or {}).get("approval") or {}
    return EpicApproval(
        approved_by=approval.get("approved_by"),
        acting_human=approval.get("acting_human"),
        course_id=approval.get("course_id"),
        position=approval.get("position"))


def _refusal_message(e: "urllib.error.HTTPError", act: str = "the approval") -> str:
    """The Library's own `error` text for a refusal, or a plain fallback.

    Read from the body rather than mapped from the status code, because the
    causes the specification names — no specification, no live Course, held by
    another session — are several different 409s and a code cannot tell them
    apart. The body is consumed defensively: a refusal that arrives without
    readable JSON must still produce something the page can show, or the
    "renders where the control is, naming what was refused" requirement fails
    on exactly the malformed cases nobody tests."""
    try:
        payload = json.loads(e.read().decode("utf-8"))
    except Exception:
        return f"the Library refused {act} (HTTP {e.code})"
    if isinstance(payload, dict):
        for key in ("error", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return f"the Library refused {act} (HTTP {e.code})"


class ItemModelRefused(Exception):
    """The Library refused to set or clear an item's model, with its own
    wording: a terminal item, another holder's lease, a model over its limit.
    Distinct for the reason `EpicApprovalRefused` is: the page shows the cause
    where the control is, and a status code cannot carry it."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def set_item_model(issue_id: str, *, model: str | None, acting_human: str,
                   reason: str | None = None) -> IssueRecord:
    """Set the model an operator chose for a work item, or clear it with
    `model=None` — `POST /items/{id}/model`, under the APPROVED-CLIENT
    credential (library-protocol §6.2).

    **THIS IS THE ONLY ROUTE THAT WRITES `model_choice`.** The Bosun runs an
    item on this model on an autonomous dispatch, where a typed override is
    refused, so the Library reserves the write to `item.model`, which no agent
    credential resolves. `acting_human` must be the email Orient verified
    through Cloudflare Access, never a value off the request body, the same
    rule `approve_epic` states.

    Returns the item as the Library now holds it. Raises `ItemModelRefused`
    with the Library's wording on a 4xx, and lets a 5xx propagate as the fault
    it is."""
    body: dict = {"model": model, "acting_human": acting_human}
    if reason:
        body["reason"] = reason
    try:
        raw = _request(f"/items/{urllib.parse.quote(issue_id, safe='')}/model",
                       json_body=body, human_reserved=True)
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            raise ItemModelRefused(e.code, _refusal_message(e, "the model change")) from e
        raise
    return _issue_record(raw["item"])


# --- revising an epic's text (the epic editor) ------------------------------
#
# Three routes, taken in one request by `epic_revise_api`: check the epic out,
# revise it, release it. The Library makes a live lease the precondition of a
# revision (`POST /items/{id}/revision` refuses a caller that does not hold
# one), and on a work item checkout also CLAIMS it, so holding the lease while
# a human types would show the epic as claimed, and fence off its children,
# for as long as the editor stayed open. Taking all three at save time holds
# the epic for the length of one request instead.
#
# All three run on the AGENT credential, which resolves `item.resolve` — the
# capability `/revision` checks. Confirmed by calling each route with an id
# that does not exist on 2026-09-22: all three answered 404 "no such item",
# which is past the capability gate, while a made-up route under the same
# prefix answered FastAPI's own "Not Found".


class ItemWriteRefused(Exception):
    """The Library refused a checkout, revision or release, with its own
    wording — another session's lease, a lapsed one, a value it will not take.
    A distinct exception for the same reason `EpicApprovalRefused` is one: the
    human needs the cause, and a status code cannot carry it."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _item_write(issue_id: str, action: str, body: dict, act: str) -> dict:
    try:
        return _request(f"/items/{urllib.parse.quote(issue_id, safe='')}/{action}",
                        json_body=body) or {}
    except urllib.error.HTTPError as e:
        # 4xx is a refusal the human should read; 5xx is a fault and propagates
        # as one, the same split `approve_epic` makes.
        if 400 <= e.code < 500:
            raise ItemWriteRefused(e.code, _refusal_message(e, act)) from e
        raise


def checkout_item(issue_id: str, *, owner: str) -> None:
    """Take the lease on `issue_id` (and claim it) as `owner` —
    `POST /items/{id}/checkout`. Another holder's lease raises
    `ItemWriteRefused` naming them."""
    _item_write(issue_id, "checkout", {"owner": owner}, "the checkout")


def revise_item(issue_id: str, *, owner: str, fields: dict) -> None:
    """Revise the item's own content under `owner`'s lease —
    `POST /items/{id}/revision`. `fields` carries any of `specification` and
    `definition_of_done`; the Library refuses `maturity` by name, since
    approving is `approve_epic`'s act alone. A caller wanting the result reads
    the item back through `get_issue_item`, the parser every other read uses."""
    _item_write(issue_id, "revision", {"owner": owner, **fields}, "the edit")


def release_item(issue_id: str, *, owner: str) -> None:
    """Hand back `owner`'s lease and claim — `POST /items/{id}/release`."""
    _item_write(issue_id, "release", {"owner": owner}, "the release")


def resolve_item(issue_id: str, *, owner: str, resolution: str,
                 outcome_note: str | None = None, refs: dict | None = None) -> dict:
    """End `issue_id` with `resolution` — `POST /items/{id}/resolve`, on the
    agent credential, which resolves `item.resolve`
    (issue_01M3J6D09C38T4AT0SGZ20XHDW). The Library refuses an item another
    session holds with 409 `reason: "held"`, which raises `ItemWriteRefused`
    with its wording."""
    body: dict = {"owner": owner, "resolution": resolution}
    if outcome_note:
        body["outcome_note"] = outcome_note
    if refs:
        body["refs"] = refs
    return _item_write(issue_id, "resolve", body, "the resolve")


def create_item(*, product: str, title: str, body: str = "", type: str | None = None,
                severity: str | None = None) -> dict:
    """File one work item — `POST /items`, on the agent credential, which
    resolves `item.create` (issue_01M3J6D09C38T4AT0SGZ20XHDW). Returns the
    stored row. A 4xx raises `ItemWriteRefused` with the Library's wording.

    The Library records the credential's own name as `created_by`, and takes
    no acting-human field on this route, so a caller wanting the person on
    the record puts them in `body`."""
    payload: dict = {"product": product, "title": title, "body": body}
    if type:
        payload["type"] = type
    if severity:
        payload["severity"] = severity
    try:
        return _request("/items", json_body=payload) or {}
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            raise ItemWriteRefused(e.code, _refusal_message(e, "the filing")) from e
        raise


# --- Courses (issue_01M0TKX6DW2E1TXGN9QT6K61J5 step 2) --------------------
#
# Three routes, live on the Library and unreached until this pass — the
# epic's own reconciliation found `/courses` served and simply never called.
# Shape confirmed by calling the live Library on 2026-08-24, following the
# same record-and-parse convention as `IssueRecord`/`_issue_record` above.


@dataclass
class CourseEntry:
    """One entry in a Course's ordered `entries` list: an issue id and its
    current state. `GET /courses` (`list_courses`) serves entries as bare id
    strings with no per-issue detail, so `state`, `title` and `maturity` are
    `None` there; `GET /courses/{id}` (`get_course`) additionally resolves
    every entry against its own issue record and fills all three in, in the
    same order.

    `maturity` is the same free-text field `IssueRecord.maturity` carries
    (`None`, `"proposed"` or `"approved"`, confirmed live 2026-08-28) — served
    here too because the Chart House step track needs it per entry to draw a
    step as done/approved/proposed/unspecified, and `/courses/{id}` already
    returns it on every item without an extra round trip per entry.

    `specification` is the same `IssueRecord.specification` field, resolved
    the same free way `maturity` is: `get_course` already carries every
    `IssueRecord` field per item, this one was just never read. A caller
    needs it alongside `maturity` to tell a step actually awaiting a human
    sign-off from one merely proposed with nothing written yet
    (issue_01M1FN5GDQGHR1PXY4AG9QTBWX).

    `resolved_at` is the same `IssueRecord.resolved_at` field — `get_course`
    already carries it per item (`store.item_dict` includes it), this one
    was just never parsed. `None` on a `list_courses` entry (which resolves
    nothing) and on an entry whose issue is still open. A caller computing
    windowed waypoint pace (`waypoints.py`) needs it to tell a waypoint
    passed inside a window from one passed too long ago to count."""

    issue_id: str
    state: str | None = None
    title: str | None = None
    maturity: str | None = None
    specification: str | None = None
    resolved_at: dt.datetime | None = None


@dataclass
class CourseRecord:
    """One `courses` row. `entries` carries only issue ids from `list_courses`
    and full `CourseEntry` detail from `get_course` — see that dataclass."""

    id: str
    product: str
    title: str
    status: str
    version: int
    entries: list[CourseEntry]
    created_by: str
    created_at: dt.datetime | None
    updated_by: str
    updated_at: dt.datetime | None


def _course_record(raw: dict) -> CourseRecord:
    return CourseRecord(
        id=raw["id"], product=raw["product"], title=raw["title"],
        status=raw["status"], version=raw["version"],
        entries=[CourseEntry(issue_id=e) for e in raw["entries"]],
        created_by=raw["created_by"], created_at=_iso(raw.get("created_at")),
        updated_by=raw["updated_by"], updated_at=_iso(raw.get("updated_at")))


def _page_courses(params: dict) -> list[dict]:
    """Every `/courses` row matching `params`, paged to completion — same
    shape as `_page_leases`, since this route also takes a fixed filter
    (`product`, `status`) plus pagination rather than `_page_all`'s no-filter
    case."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = _request("/courses", {**params, "limit": MAX_PAGE, "offset": offset})
        page_rows = page["courses"]
        rows.extend(page_rows)
        if not page["truncated"]:
            return rows
        offset += len(page_rows)


def list_courses(*, product: str | None = None, status: str = "live") -> list[CourseRecord]:
    """Courses at `status` ("live" — the default, matching the Library's own
    default, or "all" to include superseded ones), optionally narrowed to one
    `product`, paged to completion.

    Every entry on every row comes back with `state` and `title` unset — this
    route does not resolve them. A caller that needs entries-done-against-
    total or a next-entry name (the Chart House overview) calls `get_course`
    per course it renders; a caller that only needs to know a course exists
    and its position (id, title, version, entry count) does not."""
    params: dict = {"status": status}
    if product:
        params["product"] = product
    return [_course_record(r) for r in _page_courses(params)]


def get_course(course_id: str) -> CourseRecord | None:
    """One Course with every entry resolved to its issue's current state and
    title, or None if there is no such course (or it is out of this
    credential's scope — the Library answers both the same way, the same
    not-found/out-of-scope collapse `get_issue_item` documents)."""
    raw = _not_found(f"/courses/{urllib.parse.quote(course_id, safe='')}")
    if raw is None:
        return None
    record = _course_record(raw)
    items = raw.get("items") or []
    record.entries = [CourseEntry(issue_id=it["id"], state=it.get("state"),
                                  title=it.get("title"), maturity=it.get("maturity"),
                                  specification=it.get("specification"),
                                  resolved_at=_iso(it.get("resolved_at")))
                       for it in items]
    return record


# --- Reference documents (issue_01M08YBCDTEFJ6N9S798ERF89P step 3) ---------
#
# Four routes, live on the Library and unreached until this pass. Shape
# confirmed by calling the live Library on 2026-08-24: `GET
# /reference/documents` returns 702 rows with metadata and no `body`, no
# `blurb` and no `body_len` — bodies are deliberately omitted from the
# listing; `GET /reference/documents/{id}` returns one document WITH `body`;
# `GET /reference/search` returns the same row shape as the listing plus
# `snippet` and `score`; `GET /reference/topics` is a single unpaginated
# response, not a list route (no `offset`/`truncated`).


@dataclass
class ReferenceDocument:
    """One `reference documents` row, in the shape the Library actually
    serves (confirmed by calling `/reference/documents`,
    `/reference/documents/{id}` and `/reference/search` on the live service,
    not read off a route table).

    `body` is `None` from `list_reference_documents`/`search_reference` (the
    listing route omits it) and set from `get_reference_document` (the only
    route that serves it). `snippet`/`score` are `None` except from
    `search_reference`, which is the only route that serves them — the same
    per-route-shape collapse `CourseEntry` documents for `state`/`title`."""

    id: str
    source_ref: str
    kind: str
    project: str
    title: str
    status: str
    topics: list
    audience: list
    last_reviewed: str | None
    updated_at: dt.datetime | None
    body: str | None = None
    snippet: str | None = None
    score: float | None = None
    # Served by the LISTING route only (convivy/library#107): a one-line extract
    # and the stored body's length, so a caller can render an index without
    # pulling 644 bodies at 3.7ms each. Both are `None` from `/reference/search`
    # and from a single-document fetch, which is why they are decoded with
    # `.get` rather than the strict indexing the always-present fields use — a
    # strict read here would break search.
    #
    # **`body_len` measures the STORED body, and `/memory` compares it against a
    # threshold rather than showing it.** Orient's own `_clean_body` strips
    # provenance sections, so the two differ on the 238 documents that carry
    # one; at the 3000-character drawer threshold that moves 12 documents of 763
    # from expanding inline to opening in a drawer. The operator accepted that on
    # 2026-09-11 rather than have Orient fetch every body to compute its own.
    blurb: str | None = None
    body_len: int | None = None


def _reference_document(raw: dict) -> ReferenceDocument:
    return ReferenceDocument(
        id=raw["id"], source_ref=raw["source_ref"], kind=raw["kind"],
        project=raw["project"], title=raw["title"], status=raw["status"],
        topics=raw.get("topics") or [], audience=raw.get("audience") or [],
        last_reviewed=raw.get("last_reviewed"), updated_at=_iso(raw.get("updated_at")),
        body=raw.get("body"), snippet=raw.get("snippet"), score=raw.get("score"),
        blurb=raw.get("blurb"), body_len=raw.get("body_len"))


def _page_reference_documents(params: dict) -> list[dict]:
    """Every `/reference/documents` row matching `params`, paged to
    completion — same shape as `_page_courses`. Cheap even to completion: the
    listing omits bodies, so 702 rows on the live corpus is four requests."""
    rows: list[dict] = []
    offset = 0
    while True:
        page = _request("/reference/documents", {**params, "limit": MAX_PAGE, "offset": offset})
        page_rows = page["documents"]
        rows.extend(page_rows)
        if not page["truncated"]:
            return rows
        offset += len(page_rows)


def _entitled(doc: ReferenceDocument, audience: str) -> bool:
    """Whether `audience` may see this document.

    **The Library serves the WHOLE corpus and does not filter by audience —
    359 of its 702 documents are not team-audience.** So the entitlement
    decision is the caller's, and this is where it lives, once, rather than in
    each page that reads reference documents (PR #205 review).

    A document with a missing, empty or non-list `audience` is NOT entitled.
    Fail closed: the Library omitting a field must never widen who can read a
    document, and `_reference_document` already defaults the field to `[]`.

    **The two checks below are not redundant and neither may be simplified
    away.** `_reference_document`'s `raw.get("audience") or []` replaces only
    FALSY values, so a truthy non-list — the Library returning a bare string —
    arrives here intact. Without the `isinstance`, `audience in aud` is then a
    SUBSTRING test, and an `audience` of `"teamwork"` or `"steam-team"` grants
    `"team"` access. Measured rather than theorised (PR #205 review)."""
    aud = doc.audience
    # isinstance FIRST — see the docstring. Dropping it turns the line below
    # into a substring match on a bare string, which is a bypass rather than
    # a tidiness point.
    return isinstance(aud, list) and audience in aud


def list_reference_documents(*, audience: str,
                             status: str | None = None) -> list[ReferenceDocument]:
    """Reference document metadata for `audience`, no body, paged to
    completion.

    **`audience` is a required keyword and there is deliberately no default.**
    An optional one is the parameter a caller forgets, and forgetting it here
    lists the ids, titles, projects and topics of every restricted document in
    the corpus. Making it required means a new caller cannot fail to decide —
    the call does not run without it. Pass `"team"` for the human site, which
    is the rule `build.load_docs` and `kb_index.team_docs` already apply.

    **`status` omitted is NOT "every status" — the route's default is `current`
    plus `draft`**, 760 of 778 documents when measured on 2026-09-10. A caller
    that wants the whole corpus must say `status="all"`, which is the sentinel
    the Library added for exactly this (convivy/library#103). `"all"` applies no
    status clause at all rather than an `IN` over the known values, so it stays
    correct when a status is added with nobody editing either side; an unknown
    value is refused with a 400 naming the permitted set.

    The default is left as the route's rather than forced here, because every
    caller but the memories page genuinely wants current+draft — a page showing
    what the company currently claims should not surface a superseded document.

    **The key is omitted when unset rather than sent empty**, matching
    `signal_facets` and `list_lease_refusals`. `_request` urlencodes whatever it
    is given and drops nothing, so `None` would travel as the literal
    `?status=None`; relying on the route treating an empty value as absent would
    also be depending on behaviour the Library is free to change."""
    params = {"status": status} if status else {}
    docs = (_reference_document(r) for r in _page_reference_documents(params))
    return [d for d in docs if _entitled(d, audience)]


def list_reference_documents_unfiltered() -> list[ReferenceDocument]:
    """Every reference document's metadata, WITH NO ENTITLEMENT GATE.

    **"Unfiltered" means by AUDIENCE and by STATUS both.** Passing `status="all"`
    is deliberate: omitting it inherits the route's current+draft default, which
    would make this function's name a promise it does not keep and leave its
    caller's own status filter redundant by luck rather than by intent. If the
    Library's default set ever narrowed, an omitted parameter would silently drop
    documents here with nothing to catch it — the "quietly smaller" failure the
    `status=all` sentinel was added to prevent (review, PR #280).

    **The name is the safety property.** `list_reference_documents` makes
    `audience` a required keyword precisely so a caller cannot reach the corpus
    by forgetting to decide (PR #205's review). Giving that function an
    "unfiltered" mode would put the ungated path one default away from every
    caller; a separate function nobody arrives at by omission keeps the decision
    explicit, and greps for the one place it is used.

    **There is exactly one legitimate caller: the Greater KB browse at
    `/memory`**, which by design lists every current/draft document of a
    human-browsable kind and marks per row whether the reader may open it — 300
    of its 644 rows are team-audience, and the other 344 are shown as titles a
    full-tier human can see exist but not read. `engineering_build` applies
    `_entitled` to each row for that flag, so the gate is per-row rather than
    absent.

    **A NEW CALLER SHOULD ALMOST CERTAINLY USE `list_reference_documents`
    INSTEAD.** This one hands back metadata for documents written for a single
    agent role or person, including the Library's own `blurb` — a body-derived
    extract. That is the pre-existing behaviour of `/memory`, ported rather than
    introduced here, and it is worth a fresh look rather than inheritance: the
    review that centralised `_entitled` treated listing restricted documents as
    a leak, and this page has always done it deliberately.
    """
    return [_reference_document(r) for r in _page_reference_documents({"status": "all"})]


def get_reference_document(doc_id: str, *, audience: str) -> ReferenceDocument | None:
    """One reference document with its full body, or None — for no such
    document, OR for one this `audience` may not read. **The two collapse
    deliberately**: a caller that could tell them apart would let a guessed id
    confirm a document exists.

    `audience` is required for the reason `list_reference_documents` gives.

    The body is fetched before the entitlement check, so a gated id costs a
    round trip a missing one does not — a timing difference an authenticated
    full-tier caller could use to confirm an id exists (PR #205 review). Judged
    an accepted trade: the call is loopback, the caller is already trusted, and
    `build.load_docs` and `get_issue_item` have the same fetch-then-filter
    shape. Revisit it if reference ids ever become enumerable at scale."""
    raw = _not_found(f"/reference/documents/{urllib.parse.quote(doc_id, safe='')}")
    if raw is None:
        return None
    doc = _reference_document(raw)
    return doc if _entitled(doc, audience) else None


def _page_reference_search(query: str, cap: int | None) -> list[dict]:
    """Ranked rows matching `query`, in the order the Library ranked them —
    same cap/page-to-completion split as `_page_signals`, since a search
    result set has no natural upper bound the way a listing's total does."""
    rows: list[dict] = []
    offset = 0
    while cap is None or len(rows) < cap:
        limit = MAX_PAGE if cap is None else min(MAX_PAGE, cap - len(rows))
        page = _request("/reference/search", {"q": query, "limit": limit, "offset": offset})
        page_rows = page["documents"]
        rows.extend(page_rows)
        if not page["truncated"]:
            break
        offset += len(page_rows)
    return rows


def search_reference(query: str, *, audience: str,
                     cap: int | None = None) -> list[ReferenceDocument]:
    """Column-weighted BM25 search over the reference corpus (`GET
    /reference/search`), ranked best-first, filtered to `audience`. Same row
    shape as `list_reference_documents` plus `snippet` and `score`.

    `audience` is required for the reason `list_reference_documents` gives, and
    it matters more here: a search SNIPPET carries document text, so an
    unfiltered result set leaks content rather than only metadata.

    `cap` truncates the ranked list at N results and is applied to the rows the
    Library returns, BEFORE this filter — so a capped search can yield fewer
    than `cap` entitled results. That is the safe direction; the alternative
    pages further on the caller's behalf and makes the cost of a query depend
    on how much of the corpus they cannot see."""
    rows = _page_reference_search(query, cap)
    return [d for d in (_reference_document(r) for r in rows)
            if _entitled(d, audience)]


@dataclass
class ReferenceTopic:
    """One `/reference/topics` row: a topic tag and how many documents carry
    it."""

    topic: str
    documents: int


def _reference_topic(raw: dict) -> ReferenceTopic:
    return ReferenceTopic(topic=raw["topic"], documents=raw["documents"])


def list_reference_topics() -> list[ReferenceTopic]:
    """Every topic the reference corpus is tagged with and its document
    count — `GET /reference/topics`, a single unpaginated response (no
    `offset`/`truncated` to page past)."""
    raw = _request("/reference/topics")
    return [_reference_topic(r) for r in raw["topics"]]


# --- Reference-document coordination (issue_01M0TKX6DW2E1TXGN9QT6K61J5 §4,
# the option-C KB editor) ----------------------------------------------------
#
# **The lock, not the write.** `library#45`'s own module docstring is explicit:
# "The Library does not commit or push." These four calls only ever move a
# `reference_checkouts` row — they never touch a document's text, which stays a
# file in the `knowledge` repository (ADR-0022). The actual commit is Orient's
# own concern and lives elsewhere (see `kb_write_api.py`'s module docstring for
# where that stands).
#
# **The AGENT credential carries all four**, deliberately not `human_reserved`.
# ADR-0038's human-reserved credential exists for a call the Library's `agent`
# role does not hold the capability for (`signal.ack`, `decision.decide`);
# `identity.ROLE_CAPS["agent"]` in the Library repo already grants
# `reference.checkout` and `reference.admit` outright, matching
# `reference.read`, which every existing reference function above already
# calls under the plain agent credential. There is nothing narrower to reach
# for. Attribution of WHO is editing travels as the `owner` field in the
# request body, not as a choice of credential — the same shape `_claim_owner`
# already gives every Chart House checkout.
#
# **The Library may answer 503 to any of the three write calls below** with its
# fixed "not the write path yet" refusal (`store.WritesDisabled` in the Library
# repo, gated on `LIBRARY_WRITES_ENABLED`). That is Library-side deployment
# state rather than a client bug, and `ReferenceWritesNotEnabled` exists so a
# caller can show one clear message instead of parsing a refusal's `hint` prose
# to guess why a 5xx came back.
#
# **Writes are ENABLED on the running Library, measured 2026-08-25.** An earlier
# draft of this comment said the refusal was "every deploy as of this writing",
# read off the Library's source rather than asked of the service. Calling it
# settled it: `POST /reference/documents/{id}/checkout` on a real document
# answered 200 and took a real checkout, heartbeat refreshed it, and checkin
# released it. The handling below is right either way — it is reactive, not a
# config read — but the claim was wrong, and a client that tells a human "the
# Library is read-only" when it is not is the same defect this product spent
# the week removing from its own pages.


class ReferenceWritesNotEnabled(Exception):
    """The Library answered its fixed "not the write path yet" refusal
    (`store.WritesDisabled` in the Library repo) to a checkout, heartbeat or
    checkin call. Distinct from a bare `HTTPError` so a caller can show one
    plain message — "document locking isn't turned on yet" — rather than a
    generic 503."""


class ReferenceCheckoutConflict(Exception):
    """A live, unexpired checkout already holds this document — carries the
    row (`.checkout`) so the caller can name the holder and the expiry rather
    than a bare "no", matching `LeaseConflict` and `CheckoutConflict` in the
    Library repo itself."""

    def __init__(self, checkout: "ReferenceCheckout"):
        self.checkout = checkout
        super().__init__(f"{checkout.doc_id} is held by {checkout.holder} "
                         f"until {checkout.expires_at}")


@dataclass
class ReferenceCheckout:
    """One `reference_checkouts` row, in the shape `reference_checkout.
    checkout_dict` serves. `expires_at` is computed by the Library from
    `taken_at`/`heartbeat_at` against its current grace tunables, not stored —
    so it moves if the Library's policy does, without this client needing to
    know the policy itself."""

    id: str
    doc_id: str
    holder: str
    run_id: str | None
    taken_at: dt.datetime | None
    heartbeat_at: dt.datetime | None
    expires_at: dt.datetime | None
    released_at: dt.datetime | None
    release_reason: str | None
    released_by: str | None


def _reference_checkout(raw: dict) -> ReferenceCheckout:
    return ReferenceCheckout(
        id=raw["id"], doc_id=raw["doc_id"], holder=raw["holder"],
        run_id=raw.get("run_id"), taken_at=_iso(raw.get("taken_at")),
        heartbeat_at=_iso(raw.get("heartbeat_at")),
        expires_at=_iso(raw.get("expires_at")),
        released_at=_iso(raw.get("released_at")),
        release_reason=raw.get("release_reason"),
        released_by=raw.get("released_by"))


def _reference_write(path: str, body: dict) -> dict:
    """One coordination POST, translating the Library's fixed "not the write
    path yet" refusal into `ReferenceWritesNotEnabled` — the one outcome every
    caller below needs to recognise by more than a status code. Anything else
    (a genuine 409, 404, malformed request, ...) is the caller's to interpret,
    matching this module's "failure is not caught" rule for everything it does
    not name explicitly."""
    try:
        return _request(path, json_body=body)
    except urllib.error.HTTPError as e:
        if e.code == 503:
            raise ReferenceWritesNotEnabled(
                "the Library is not the write path yet") from e
        raise


def get_reference_checkout(doc_id: str) -> ReferenceCheckout | None:
    """Whether `doc_id` is checked out, without attempting to take it — `GET
    /reference/documents/{id}/checkout`. `None` for free, or for a document
    that does not exist (the route 404s on the latter, and this collapses the
    two — a caller that must tell them apart already called
    `get_reference_document` to render the page in the first place).

    A dead checkout (past grace) is still reported rather than hidden — see
    the route's own docstring — so an editor can say "held by X, but their
    session looks gone" rather than nothing."""
    raw = _not_found(f"/reference/documents/{urllib.parse.quote(doc_id, safe='')}/checkout")
    if raw is None or raw.get("checkout") is None:
        return None
    return _reference_checkout(raw["checkout"])


def checkout_reference_document(doc_id: str, *, holder: str,
                                run_id: str | None = None) -> ReferenceCheckout:
    """Take the coordination lock ahead of an edit — `POST /reference/
    documents/{id}/checkout`. `holder` is the caller-declared owner (the
    verified human's email, for the KB editor) and travels as `owner` in the
    body, the same field every Chart House checkout uses.

    Raises `ReferenceCheckoutConflict` for a live checkout already on this
    document (409) and `ReferenceWritesNotEnabled` for the Library's shadow
    refusal (503). Everything else — a 404 on a document that no longer
    exists, a malformed request — raises the underlying `HTTPError`."""
    body: dict = {"owner": holder}
    if run_id is not None:
        body["run_id"] = run_id
    try:
        raw = _reference_write(
            f"/reference/documents/{urllib.parse.quote(doc_id, safe='')}/checkout", body)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            raise ReferenceCheckoutConflict(
                _reference_checkout(json.loads(e.read())["checkout"])) from None
        raise
    return _reference_checkout(raw["checkout"])


def heartbeat_reference_checkout(doc_id: str, *, holder: str) -> ReferenceCheckout | None:
    """Say `holder` is still holding this document's checkout — `POST
    /reference/documents/{id}/heartbeat`. `None` for the route's 404, which
    covers three causes the route deliberately does not distinguish (no live
    checkout, someone else's, or dead past grace) — see its own docstring;
    a caller that needs to tell them apart calls `get_reference_checkout`.

    **Never creates a checkout.** A heartbeat that could is a checkout by
    another name with none of the conflict checking — the route's own rule."""
    try:
        raw = _reference_write(
            f"/reference/documents/{urllib.parse.quote(doc_id, safe='')}/heartbeat",
            {"owner": holder})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return _reference_checkout(raw["checkout"])


def checkin_reference_document(doc_id: str, *, holder: str) -> ReferenceCheckout | None:
    """Hand a document's checkout back — `POST /reference/documents/{id}/
    checkin`. `None` for the route's 409, which covers two causes (nothing
    checked out, or held by a different holder) without distinguishing them —
    matching the route, whose refusal for this pair carries no `checkout` key
    to parse in the first place."""
    try:
        raw = _reference_write(
            f"/reference/documents/{urllib.parse.quote(doc_id, safe='')}/checkin",
            {"owner": holder})
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return None
        raise
    return _reference_checkout(raw["checkout"])


@dataclass
class AdmissionResult:
    """`POST /reference/admission`'s answer: whether this document's
    frontmatter validates, and the structural errors if not."""

    may_land: bool
    errors: list


def check_reference_admission(frontmatter: dict, *, owner: str) -> AdmissionResult:
    """May this document land — the Library's half of the write split. Never
    raises for a validation failure — `may_land=False` with `errors` IS the
    answer, not an exception; this raises only for a genuine failure to ask
    (network, credential, a malformed request this client itself built wrong).

    **`owner` is required, and it is the label the person's checkout is held
    under** (`ident.actor`, the same string every `holder=` above passes). It
    turns on the Library's checkout clause, which refuses unless `owner` holds
    the live checkout on `frontmatter["id"]`. The Library is making that clause
    unconditional (issue_01M19QW42CFK7TDZCKWK18ECY5), after which a request
    with no `owner` is refused outright, so a keyword with no default is what
    keeps a caller from dropping it. Never pass Orient's own service identity:
    the checkout is the person's, and the clause would refuse every save."""
    raw = _request("/reference/admission",
                   json_body={"frontmatter": frontmatter, "owner": owner})
    return AdmissionResult(may_land=bool(raw["may_land"]),
                           errors=list(raw.get("errors") or []))


# --- Tool policy (ADR-0048) — read half (issue_01M1F1DT9SM5V17BF4AV8FTTN2) --
#
# First of three items under the deny-list epic
# (issue_01M0C91CGDR0YT8T5ERX2HD2HS): a read client for `GET /policy/resolve`,
# so the page (the second item) and the write route (the third, which carries
# the epic's whole security burden) both resolve a target's decision through
# one place rather than each re-implementing the fallback and drifting apart.
# `GET /policy/version` and `PUT /policy` are the other two routes ADR-0048
# defines; neither is built here.
#
# **Read at request time, always.** No TTL cache, no module-level dict —
# unlike `probe_route`'s deliberate 300s cache, a stale deny-list is exactly
# the property this epic exists to prevent (its own definition of done: "no
# second copy in Orient's control database").


@dataclass
class PolicyResolution:
    """`GET /policy/resolve`'s own envelope for one (agent, crew, model)
    triple, in the shape the route's own docstring describes: every explicit
    override on file (`explicit`), the two classes that supply a default
    where no override exists (`connector_servers`, `exfiltration_builtins`),
    and what everything outside both classes resolves to (`default`).

    **Not a single yes/no** — the route answers "the policy" rather than "may
    X" because a caller needs an answer for more than one target per
    dispatch. Call `.decision(target)` for one target's answer rather than
    re-deriving the documented fallback (`explicit.get(target)`, then
    `"deny"` for a target in either class, then `default`) at the call site —
    two callers each implementing that fallback is how they end up
    disagreeing about what is denied.
    """

    agent: str
    crew: str
    model: str
    explicit: dict
    connector_servers: list
    exfiltration_builtins: list
    default: str
    version: int

    def decision(self, target: str) -> str:
        """`target`'s resolved decision under this triple: an explicit
        override first, then `"deny"` for a target in `connector_servers` or
        `exfiltration_builtins`, then `default` — the exact fallback `GET
        /policy/resolve`'s docstring specifies, so a caller gets the same
        answer whether it reads `.explicit` itself or asks here."""
        if target in self.explicit:
            return self.explicit[target]
        if target in self.connector_servers or target in self.exfiltration_builtins:
            return "deny"
        return self.default


def _policy_resolution(raw: dict) -> PolicyResolution:
    return PolicyResolution(
        agent=raw["agent"], crew=raw["crew"], model=raw["model"],
        explicit=dict(raw["explicit"]),
        connector_servers=list(raw["connector_servers"]),
        exfiltration_builtins=list(raw["exfiltration_builtins"]),
        default=raw["default"], version=raw["version"])


def resolve_policy(agent: str, crew: str, model: str) -> PolicyResolution:
    """The tool policy for one (agent, crew, model) triple — `GET
    /policy/resolve` (ADR-0048). Never cached; see the section banner above.

    **Stripped here, not left for the route to strip.** `GET /policy/resolve`
    strips all three fields before its own lookup, deliberately, because the
    table is exact-match with no wildcards — its docstring warns that a
    write's `"scout "` and a read's `"scout"` would otherwise silently
    address different rows. This client strips client-side rather than
    rejecting an unstripped value: stripping is what the route does to the
    value regardless of what arrives, so matching that here means a caller
    passing `"scout "` gets the row for `"scout"` instead of a 400 that only
    says its whitespace was wrong. A value that is empty once stripped raises
    `ValueError` locally rather than spending a request the Library would
    only 400 — the same call-site check `create_signal` applies to
    `acting_human`, rather than inside `_request`.

    That is the only precedent in this file, and an earlier draft of this
    docstring also cited `list_reference_documents`'s `audience`, which does
    not validate anything: it makes the argument a required keyword with no
    default, so a caller cannot forget it, but an empty string passes
    straight through. Required-keyword and runtime-validation are different
    techniques and this file uses both; conflating them would have told a
    reader that `audience=""` is rejected somewhere it is not.

    Field-length validation (`store.MAX_POLICY_FIELD`) is left to the
    Library: duplicating that bound here would be a second copy of a
    Library-owned constant to keep in sync, for a check whose failure this
    module already lets travel as a plain `HTTPError` (the module
    docstring's "failure is not caught" rule).
    """
    stripped = {}
    for name, value in (("agent", agent), ("crew", crew), ("model", model)):
        v = value.strip()
        if not v:
            raise ValueError(f"{name} is required")
        stripped[name] = v
    raw = _request("/policy/resolve", stripped)
    return _policy_resolution(raw)


# The decision values `PUT /policy` accepts, mirroring `store.POLICY_DECISIONS`
# on the Library side. Orient is the only writer (see the section banner
# above), so this is the one place that constant needs echoing — for
# `policy_api.py`'s own local validation to refuse an unrecognised decision
# before it ever reaches the wire, the same posture `policy_build._DECISION_PILL`
# already takes on the read side (an unrecognised value gets its own state,
# never "allow").
POLICY_DECISIONS = frozenset({"allow", "deny"})


def write_policy(*, agent: str, crew: str, model: str, target: str, decision: str,
                 acting_human: str) -> dict:
    """Set the decision for one (agent, crew, model, target) combination —
    `PUT /policy` (ADR-0048's write half). Reserved for the write route this
    epic exists to build (issue_01M1F1ENN8P6RD4W3SJJ04HR45); nothing else in
    this repo should call it.

    **Always the APPROVED-CLIENT credential.** The Library refuses
    `policy.write` for any identity kind other than `approved_client` — the
    capability is structurally absent from `agent` and `system`
    (`identity.py`), and the route separately refuses on `kind` too, with no
    exception. This function does not decide that; `human_reserved=True`
    below does, the same way `ack_signal`/`create_signal(acting_human=...)`
    pick it for the other two routes the Library reserves for a verified
    human.

    `acting_human` is required and is recorded as the row's `actor` on the
    Library side — never the client credential's own identity. **The caller
    must derive it from the identity Cloudflare Access verified, never from a
    request body field** — `signal_capture_api.py`'s rule for its own
    `acting_human`, for the same reason: a caller-supplied value would let
    the attestation assert whatever the caller typed.

    **Every field is checked for non-empty only here — no length bound, no
    membership check against `POLICY_DECISIONS`.** The Library re-validates
    all of that before it writes anything (type, non-empty after strip,
    `store.MAX_POLICY_FIELD`, `decision in store.POLICY_DECISIONS`), and
    Orient is the only writer, so duplicating those bounds here would be a
    second copy to keep in sync for no caller this module has — the same
    reasoning `resolve_policy` gives for leaving length validation to the
    Library. `policy_api.py` still validates locally (including against
    `POLICY_DECISIONS` above) before this is ever called: a caller-facing 400
    beats a bare `HTTPError` bubbling out of a route a browser is waiting on.

    Returns the Library's own response: `agent`, `crew`, `model`, `target`,
    `decision`, `acting_human`, `attested_by`, `version`.
    """
    for name, value in (("agent", agent), ("crew", crew), ("model", model),
                        ("target", target), ("decision", decision),
                        ("acting_human", acting_human)):
        if not value or not value.strip():
            raise ValueError(f"{name} is required")
    return _request("/policy", json_body={
        "agent": agent, "crew": crew, "model": model, "target": target,
        "decision": decision, "acting_human": acting_human,
    }, human_reserved=True, method="PUT")


# --- the spend ledger (issue_01M38TF7S5RXVKB1JG6P4VT9JW) ---------------------
#
# Read on the agent credential, which holds `ledger.read`: both routes answered
# 200 with it on 2026-09-23. Person and team are not served (the Library
# withholds them until ADR-0021's disclosure question is settled), so nothing
# here can group by them.


@dataclass
class LedgerEntry:
    """One spend entry, as `GET /ledger-entries` serves it. `voided_at` set
    means the Library no longer counts it in the ledger's total."""

    id: str
    ledger_id: str | None
    day: str
    usd: float
    at: dt.datetime | None
    source: str
    product: str | None
    run_id: str | None
    agent: str | None
    model: str | None
    item_id: str | None
    voided_at: dt.datetime | None


def _ledger_entry(raw: dict) -> LedgerEntry:
    return LedgerEntry(
        id=raw["id"], ledger_id=raw["ledger_id"], day=raw["day"], usd=raw["usd"],
        at=_iso(raw["at"]), source=raw["source"], product=raw["product"],
        run_id=raw["run_id"], agent=raw["agent"], model=raw["model"],
        item_id=raw["item_id"], voided_at=_iso(raw["voided_at"]))


def list_ledger_entries(*, product: str | None = None) -> list[LedgerEntry]:
    """Every spend entry, voided ones included, paged to completion. The route
    does no grouping, so a caller that totals a page set short of complete
    under-reports without any sign it did."""
    params = {"product": product} if product else None
    return [_ledger_entry(r) for r in _page_all("/ledger-entries", "entries", params)]


@dataclass
class Ledger:
    """One live ledger with today's total, as `GET /ledgers` serves it. `day`
    is the Library's local day that `total_usd` covers."""

    scope_kind: str
    scope_id: str
    day: str
    total_usd: float
    warn_usd: float | None
    soft_cap_usd: float | None
    hard_cap_usd: float | None
    state: str


def list_ledgers() -> list[Ledger]:
    """Every live ledger this credential may see, paged to completion."""
    return [Ledger(scope_kind=r["scope_kind"], scope_id=r["scope_id"], day=r["day"],
                   total_usd=r["total_usd"], warn_usd=r["caps"]["warn_usd"],
                   soft_cap_usd=r["caps"]["soft_cap_usd"],
                   hard_cap_usd=r["caps"]["hard_cap_usd"], state=r["state"])
            for r in _page_all("/ledgers", "ledgers")]


# --- review records (GET /review-records, protocol version 1) ----------------
#
# The Library's record of each review, written by `agentctl review-post` before
# it posts the PR comment (agent-platform#722). Read on the agent credential,
# which holds `review_record.read`: the route answered 200 with it on
# 2026-09-23 (issue_01M38316270TMDKT5MQSAANEM3).


@dataclass
class ReviewRecord:
    """One review of one pull request at one commit, as `GET /review-records`
    serves it. `auth` is the Library's verdict on who wrote it: `run-verified`
    means it matched a live review run's facts, `unverified` means it did not
    (a backfilled comment, or a runless `--allow-unverified` post), and an
    unverified record does not count toward a merge. `findings` is a list of
    objects whose keys the Library leaves to the writer."""

    id: str
    repo_identity: str
    pr: int
    reviewed_ref: str
    role: str
    run_id: str | None
    verdict: str
    findings: list
    prose: str
    auth: str
    auth_reason: str | None
    created_at: dt.datetime | None
    updated_at: dt.datetime | None


def list_review_records(repo_identity: str, pr: int) -> list[ReviewRecord]:
    """Every review record for one pull request, newest first (the route's
    own order), paged to completion. `repo_identity` is `github:<owner>/<repo>`."""
    return [ReviewRecord(
                id=r["id"], repo_identity=r["repo_identity"], pr=r["pr"],
                reviewed_ref=r["reviewed_ref"], role=r["role"], run_id=r["run_id"],
                verdict=r["verdict"], findings=r["findings"], prose=r["prose"],
                auth=r["auth"], auth_reason=r["auth_reason"],
                created_at=_iso(r["created_at"]), updated_at=_iso(r["updated_at"]))
            for r in _page_all("/review-records", "review_records",
                               {"repo_identity": repo_identity, "pr": str(pr)})]
