"""A test double for `library_client.py`, holding an in-memory store instead of
a real database or the wire.

Orient no longer holds any of the tables this used to seed and read back
(issue_01M08YKNV185DFJ6832GV8ZY22, 2026-08-19) — issue and signal data lives
only in the Library now. A test still wants a durable, inspectable way to seed
the shapes `library_client` reads and see them come back out through a
builder, so this module holds its own registry and monkeypatches
`library_client`'s read functions to serve from it, reproducing the exact
shape the real Library's `item_dict`/`signal_dict` responses carry — including
the gaps: the list route does not inline comments. A fake that were more
generous than the real Library would let a test pass here and then fail the
moment it points at the genuine service.

`patch(monkeypatch)` clears the store and installs the fake; call it from a
`db`-style fixture. `seed_issue(...)`/`seed_signal(...)`/`seed_ack(...)`
populate it — each takes the fields the old ORM models did (defaults included),
so a caller migrating off `with control_db.session() as s: s.add(IssueItem(...))`
drops the session wrapper and calls `library_fake.seed_issue(...)` directly.

`seed_reference_doc(...)` covers the Reference Room the same way
(issue_01M08YBCDTEFJ6N9S798ERF89P) — the corpus never lived in an Orient
table, but a caller moving off a direct `~/.knowledge-index/index.db` read
onto `library_client`'s reference readers wants the same durable,
inspectable seeding rather than building a real sqlite file in `tmp_path`.
"""
from __future__ import annotations

import datetime as dt
import urllib.error

import library_client
# `prerequisite_met` is control_db's rather than re-stated here: a fake with
# its own copy of the blocked rule could drift from the app's reading and go
# on passing while the page it stands in for is wrong.
from control_db import new_signal_id, prerequisite_met as _prerequisite_met

_ISSUES: dict[str, dict] = {}
_ISSUE_COMMENTS: dict[str, list] = {}
_SIGNALS: dict[str, dict] = {}
_ACKS: set[tuple[str, str]] = set()
_REFERENCE_DOCS: dict[str, dict] = {}
# Empty by default (no live Courses) so any caller of `patch()` — including
# ones that only care about issues/signals/docs — gets a safe, non-network
# answer from `library_client.list_courses`/`get_course` without having to
# know Courses exist. `seed_course()` populates it for a test that does.
_COURSES: dict[str, dict] = {}
# Empty by default for the same reason: an item page whose refs link a GitHub
# pull request reads its reviews, and a test that seeds none gets "no record".
_REVIEW_RECORDS: list[dict] = []
# Empty by default: an epic with no cross-product relation reads as none.
_RELATED_ITEMS: dict[str, list[str]] = {}
# Empty by default, which is what the Library serves before its corrections
# queues exist: every lookup answers 404.
_CORRECTIONS_QUEUES: set[str] = set()
# Empty by default, matching the real route's answer for an email naming no
# Library person (E19·1, issue_01M35K54QHJYYPXWS28YWEHJP3): the empty set,
# never "every project". `seed_person_scope` populates it per email.
_PERSON_SCOPE: dict[str, frozenset[str]] = {}

# The claim TTL the removed control_db.CLAIM_TTL carried — a claim older than
# this reads as expired, matching the real Library's own computation of the
# `claim_expires_at`/`claim_expired` fields it sends over the wire (this fake
# stands in for that response, not for Orient's own now-deleted lease logic).
_CLAIM_TTL = dt.timedelta(hours=24)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def seed_issue(*, id, product, title, body="", type="bug", severity="unset",
               state="open", needs=None, maturity=None, specification=None,
               definition_of_done=None, parent_id=None, topics=None, refs=None,
               claimed_by=None, claimed_at=None, resolution=None,
               resolved_by=None, resolved_at=None, outcome_note=None,
               park_reason=None, unpark_condition=None, parked_at=None,
               prerequisites=None, model_choice=None,
               created_by="agent", created_by_kind=None, created_at=None,
               run_id=None, found_during=None) -> str:
    """Register one issue row, in the shape `fake_get_issue_item`/
    `fake_list_issue_items` serve. Defaults mirror the removed `IssueItem`
    model's column defaults.

    **`blocked` is DERIVED here rather than seeded**, the way the Library
    derives it, so a test cannot register a row the Library could never serve —
    `blocked=False` alongside an unmet prerequisite is exactly the fixture that
    would let a display test pass against an impossible item."""
    _ISSUES[id] = dict(
        id=id, product=product, title=title, body=body, type=type,
        severity=severity, state=state, needs=needs, maturity=maturity,
        specification=specification, definition_of_done=definition_of_done,
        parent_id=parent_id, topics=topics if topics is not None else [],
        refs=refs if refs is not None else {}, claimed_by=claimed_by,
        claimed_at=claimed_at, resolution=resolution, resolved_by=resolved_by,
        resolved_at=resolved_at, outcome_note=outcome_note,
        park_reason=park_reason, unpark_condition=unpark_condition,
        parked_at=parked_at,
        prerequisites=prerequisites if prerequisites is not None else [],
        model_choice=model_choice,
        created_by=created_by,
        created_by_kind=created_by_kind, created_at=created_at or utcnow(),
        run_id=run_id, found_during=found_during)
    _ISSUE_COMMENTS.setdefault(id, [])
    return id


def _is_blocked(row: dict) -> bool:
    return any(not _prerequisite_met(p) for p in (row.get("prerequisites") or []))


def seed_issue_comment(*, item_id, id, actor, body, kind="note", run_id=None,
                       at=None) -> None:
    _ISSUE_COMMENTS.setdefault(item_id, []).append(dict(
        id=id, actor=actor, body=body, kind=kind, run_id=run_id, at=at or utcnow()))


def seed_signal(*, id, source, kind, actor, ingested_by, product, stream=None,
                refs=None, body="", meta=None, privacy_class="internal",
                dedup_key=None, produced_at=None, ingested_at=None,
                embedded_at=None, acting_human=None) -> str:
    """Register one signal row, in the shape `fake_get_signal`/
    `fake_list_signals` serve. Defaults mirror the removed `Signal` model's
    column defaults."""
    _SIGNALS[id] = dict(
        id=id, source=source, kind=kind, actor=actor, ingested_by=ingested_by,
        product=product, stream=stream, refs=refs if refs is not None else {},
        body=body, meta=meta if meta is not None else {},
        privacy_class=privacy_class, dedup_key=dedup_key,
        produced_at=produced_at, ingested_at=ingested_at or utcnow(),
        embedded_at=embedded_at, acting_human=acting_human)
    return id


def seed_ack(*, signal_id, identity) -> None:
    _ACKS.add((signal_id, identity))


def fake_create_signal(*, source, kind, actor, product, body="", stream=None,
                       refs=None, meta=None, privacy_class=None,
                       dedup_key=None, acting_human=None,
                       ) -> tuple[str, bool, "library_client.SignalRecord | None"]:
    """Stands in for `library_client.create_signal`. Returns `(id, idempotent,
    existing)`.

    **WHICH CREDENTIAL WENT OUT IS MODELLED BY `acting_human`, because that is
    how the real client decides it** and the difference is a capability rather
    than a detail. Absent, this is Orient's agent credential, which holds
    `signal.create` but never `signal.create_customer` (the Library's own
    `identity.ROLE_CAPS["agent"]`), so a customer-class create refuses exactly
    as it would over the wire. Present, it is the approved-client credential —
    `system` plus the human-reserved capabilities — which does hold it, so the
    same create succeeds.

    A fake that refused customer-class unconditionally would keep passing after
    the credential rule changed underneath it, and the tests reading it would
    report a capability as absent that is present. `signal_capture_api.py`'s
    own authz gate is expected to have already run; this only reproduces what
    the Library does after that.

    **A `dedup_key` match returns the existing row (`idempotent=True`, plus
    the row itself) rather than raising** — the real Library's
    `_can_return_existing` no-leak gate, simplified: this fake has one shared
    store rather than per-request credential scoping, so it models Orient's
    own (broad) shared credential always being able to return what it itself
    already wrote. A genuine "cannot return this" 409 — a caller lacking the
    capability to have created the existing row — is not reachable through
    this fake; tests for that path mock `library_client.create_signal`
    directly instead. `existing` is returned rather than requiring a follow-up
    `get_signal` call, matching `library_client.create_signal`'s own shape —
    a caller that needed a second call only on this branch would cost the
    idempotent path an extra round trip the true-409 path never pays, which
    a timing measurement could then use as its own oracle (security review,
    PR #179 round 5)."""
    if acting_human is not None and not acting_human.strip():
        raise ValueError("acting_human was passed but is empty")
    if privacy_class == "customer" and acting_human is None:
        raise urllib.error.HTTPError(None, 403, "forbidden: signal.create_customer", None, None)
    if dedup_key:
        for sid, row in _SIGNALS.items():
            if row.get("dedup_key") == dedup_key:
                return sid, True, _signal_record(row)
    sid = new_signal_id()
    seed_signal(id=sid, source=source, kind=kind, actor=actor, ingested_by="fake-agent",
               product=product, body=body, stream=stream, refs=refs, meta=meta,
               privacy_class=privacy_class or "internal", dedup_key=dedup_key,
               acting_human=acting_human)
    return sid, False, None


def fake_ack_signal(signal_id: str, *, acting_human: str) -> bool:
    """Stands in for `library_client.ack_signal` — `signal_ack_api.py` calls
    this only after its own `_ack_visible` check, so this fake does not repeat
    that gate; it just answers False for a signal the store never got, the
    same not-found rule `library_client.ack_signal` documents."""
    if signal_id not in _SIGNALS:
        return False
    _ACKS.add((signal_id, acting_human))
    return True


def fake_unack_signal(signal_id: str, *, acting_human: str) -> bool:
    if signal_id not in _SIGNALS:
        return False
    _ACKS.discard((signal_id, acting_human))
    return True


def _claim_fields(claimed_at):
    if claimed_at is None:
        # Claimed with no timestamp is corrupt, and stranding it is the worse
        # failure — the real Library's `lease.claim_status` makes the same call.
        return None, True
    expires = claimed_at + _CLAIM_TTL
    return expires, utcnow() >= expires


def _issue_record(row: dict) -> library_client.IssueRecord:
    expires, expired = _claim_fields(row["claimed_at"])
    # Guarded on state, matching the real store: an expiry in the past on an
    # item nobody claimed is not an expired claim.
    expired = row["state"] == "claimed" and expired
    return library_client.IssueRecord(
        id=row["id"], url=f"/engineering/issues/{row['id']}", product=row["product"],
        title=row["title"], body=row["body"], type=row["type"], state=row["state"],
        severity=row["severity"], needs=row["needs"], maturity=row["maturity"],
        parent_id=row["parent_id"], topics=row["topics"], refs=row["refs"],
        specification=row["specification"],
        definition_of_done=row["definition_of_done"], resolution=row["resolution"],
        resolved_by=row["resolved_by"], outcome_note=row["outcome_note"],
        claimed_by=row["claimed_by"], claimed_at=row["claimed_at"],
        claim_expires_at=expires, claim_expired=expired,
        resolved_at=row["resolved_at"], created_by=row["created_by"],
        created_by_kind=row["created_by_kind"], created_at=row["created_at"],
        run_id=row["run_id"], park_reason=row["park_reason"],
        unpark_condition=row["unpark_condition"], parked_at=row["parked_at"],
        blocked=_is_blocked(row), prerequisites=row.get("prerequisites") or [],
        model_choice=row.get("model_choice"), found_during=row.get("found_during"))


def _issue_comment(c: dict) -> library_client.Comment:
    return library_client.Comment(id=str(c["id"]), actor=c["actor"], body=c["body"],
                                  kind=c["kind"], run_id=c["run_id"], at=c["at"])


def fake_list_issue_items() -> list[library_client.IssueRecord]:
    return [_issue_record(r) for r in _ISSUES.values()]


def fake_list_found_during(epic_id: str) -> list[library_client.IssueRecord]:
    return [_issue_record(r) for r in _ISSUES.values() if r.get("found_during") == epic_id]


def seed_related_item(*, epic_id: str, item_id: str) -> None:
    """`item_id` is part of `epic_id`'s work across products — the relation the
    Library serves under `related_items` (issue_01M1JSPH7DBHG0BZFPJPNNZGSC)."""
    _RELATED_ITEMS.setdefault(epic_id, []).append(item_id)


def fake_list_related_item_ids(epic_id: str) -> list[str]:
    return list(_RELATED_ITEMS.get(epic_id, []))


def seed_corrections_queue(queue_id: str) -> None:
    """`queue_id` names a corrections queue, a row in the Library's
    `corrections_queues` table rather than an item."""
    _CORRECTIONS_QUEUES.add(queue_id)


def fake_corrections_queue_ids(ids) -> set[str]:
    return {i for i in ids if i in _CORRECTIONS_QUEUES}


def fake_get_issue_item(item_id: str):
    row = _ISSUES.get(item_id)
    if row is None:
        return None
    record = _issue_record(row)
    record.comments = [_issue_comment(c) for c in _ISSUE_COMMENTS.get(item_id, [])]
    return record


def _signal_record(row: dict) -> library_client.SignalRecord:
    """Mirrors `library_client._signal_record`'s own post-processing, not just
    the wire shape — this fake stands in for `library_client`, and the leading
    `agentctl-review` stamp strip happens in that module, not in the Library
    itself, so a fake that skipped it would pass tests the real client fails
    (or the reverse)."""
    raw_body = row["body"] or ""
    body = library_client._strip_leading_stamp(raw_body)
    return library_client.SignalRecord(
        id=row["id"], source=row["source"], stream=row["stream"], kind=row["kind"],
        actor=row["actor"], ingested_by=row["ingested_by"], product=row["product"],
        body=body, refs=row["refs"], meta=row["meta"],
        privacy_class=row["privacy_class"], produced_at=row["produced_at"],
        ingested_at=row["ingested_at"],
        stamp_only=bool(raw_body.strip()) and not body.strip())


# The capability set the fake credential holds, MODELLED rather than assumed.
#
# Orient's real token resolves to the Library's `agent` role, which does not carry
# `signal.customer` — that is the whole reason `signals_build` no longer filters
# `privacy_class` itself (ADR-0022 gives the privacy wall to the Library, so a
# duplicate Orient-side filter would work against the design rather than with it).
#
# But a hardcoded exclusion here asserts the OUTCOME of that capability check
# instead of performing it, so Orient's suite could never catch the one way this
# actually breaks: `kind: "system"` added to Orient's entry in the gitignored,
# un-code-reviewed `api-tokens.json`, which grants `signal.customer` and starts
# rendering customer rows on a full-tier page. Modelling the check means a test
# can flip this set and watch the pages leak (security review, PR #170).
FAKE_CAPABILITIES = {"signal.read"}
_PRIVACY_CUSTOMER = "customer"


def _may_see(row: dict) -> bool:
    """The customer wall for ONE row, evaluated against `FAKE_CAPABILITIES`.

    Every path that can return a signal goes through this — the list, the facets,
    the detail route and the ack read — so a capability flip moves all four
    together. Applying it to only the browse path would leave the detail and ack
    routes asserting the OUTCOME of a capability check while claiming to model
    it, which is the very thing this indirection was added to stop (reviewer,
    PR #170 round 2)."""
    if "signal.customer" in FAKE_CAPABILITIES:
        return True
    return row["privacy_class"] != _PRIVACY_CUSTOMER


def _visible_signals() -> list[dict]:
    """Every signal row this credential's capabilities allow — the customer
    wall `GET /signals` and `GET /signals/facets` both apply (`_customer_wall` in
    the Library repo), evaluated against `FAKE_CAPABILITIES` rather than assumed."""
    return [r for r in _SIGNALS.values() if _may_see(r)]


# `order=` → the sort key `GET /signals` computes for it (`SIGNAL_ORDER_CLAUSES`
# in the Library repo). `produced_at` sorts NULLs last under DESC, matching
# SQLite's own rule for the real route.
_SIGNAL_ORDER_KEYS = {
    None: lambda r: (r["ingested_at"],),
    "ingested_at": lambda r: (r["ingested_at"],),
    "produced_at": lambda r: (r["produced_at"] or dt.datetime.min, r["ingested_at"]),
}


def fake_list_signals(*, source=None, kind=None, product=None, order=None, cap=None):
    """Reproduces `GET /signals`'s filtering (`source`/`kind` IN-lists, the
    `product` PRE-filter, the customer wall, the whitelisted `order` clauses)
    against the in-memory store rather than the wire — the same trick
    `fake_list_issue_items` plays for issues. `cap` slices the sorted result,
    standing in for the real route's pagination: the fake reads every matching
    row at once, so there is no MAX_PAGE clamp to loop past here.

    `product` is applied BEFORE `cap`, same ordering as `source`/`kind` — a
    caller asking for `cap=5` product-scoped rows gets 5 rows of that product,
    not 5 rows of anything with the wrong ones filtered out afterward. That
    distinction is the whole reason `list_signals` grew a `product` param
    instead of reusing the post-filtering search route."""
    rows = _visible_signals()
    if source:
        rows = [r for r in rows if r["source"] in source]
    if kind:
        rows = [r for r in rows if r["kind"] in kind]
    if product:
        rows = [r for r in rows if r["product"] == product]
    rows.sort(key=_SIGNAL_ORDER_KEYS[order], reverse=True)
    if cap is not None:
        rows = rows[:cap]
    return [_signal_record(r) for r in rows]


def fake_get_signal(signal_id: str):
    row = _SIGNALS.get(signal_id)
    if row is None or not _may_see(row):
        return None
    return _signal_record(row)


def fake_signal_facets(*, source=None, kind=None):
    rows = _visible_signals()
    if source:
        rows = [r for r in rows if r["source"] in source]
    if kind:
        rows = [r for r in rows if r["kind"] in kind]
    by_source_counts: dict[str, int] = {}
    for r in rows:
        by_source_counts[r["source"]] = by_source_counts.get(r["source"], 0) + 1
    by_source = sorted(by_source_counts.items(), key=lambda kv: -kv[1])
    products = sorted({r["product"] for r in rows})
    total = len(rows)
    embedded = sum(1 for r in rows if r["embedded_at"] is not None)
    return library_client.SignalFacets(total=total, embedded=embedded,
                                       by_source=by_source, products=products)


def fake_search_signals(query, *, source=None, product=None, cap=None):
    """Stands in for `library_client.search_signals`. A plain case-insensitive
    substring match against `body` stands in for the Library's real KNN
    ranking — good enough to exercise a caller's handling of the response
    shape, not a model of relevance ranking.

    **`source`/`product` are applied AFTER the text match, on purpose** —
    mirroring the real route's documented defect (post-filtering an already
    -retrieved window rather than pre-filtering the table,
    issue_01M11WR1ZHSDSBZAE1HTZS6P6Z) rather than reproducing `fake_list_signals`'s
    correct pre-filter. A caller that filtered by `source`/`product` here and
    got the same count `fake_list_signals` would give is testing the wrong
    fake."""
    rows = _visible_signals()
    q = (query or "").lower()
    matched = [r for r in rows if q and q in (r["body"] or "").lower()]
    if source:
        matched = [r for r in matched if r["source"] in source]
    if product:
        matched = [r for r in matched if r["product"] == product]
    if cap is not None:
        matched = matched[:cap]
    results = [library_client.SignalSearchResult(signal=_signal_record(r), distance=0.5)
              for r in matched]
    return library_client.SignalSearchResponse(
        results=results, count=len(results), truncated=False)


_UNSET = object()


def seed_reference_doc(*, id, title, source_ref=None, kind="reference",
                       project="proj", status="current", topics=None,
                       audience=_UNSET, last_reviewed=None, updated_at=None,
                       body=None, blurb=None, body_len=None) -> str:
    """Register one reference-document row, in the shape
    `fake_list_reference_documents`/`fake_get_reference_document` serve.

    `audience` defaults to `["team"]`, matching what every real document a
    reader can see actually carries — a test exercising the gate itself
    passes something else explicitly. `status` defaults to `"current"`, one
    of the two the Library serves by default (`reference.DEFAULT_STATUSES`);
    a test wanting a retired/superseded row to prove it is excluded passes
    that status explicitly.

    **`audience=None` seeds a row whose audience is genuinely absent, and is
    NOT the same as omitting the argument.** A plain `None` default collapsed
    the two, so a test asking for a null-audience document silently got a
    `["team"]` one and asserted the gate against a row that was never gated.
    `_entitled`'s `isinstance(aud, list)` check is load-bearing — PR #205's
    review found it stops a substring bypass — and a fake that cannot produce a
    missing or non-list audience cannot exercise it."""
    _REFERENCE_DOCS[id] = dict(
        id=id, source_ref=source_ref or f"projects/{project}/{kind}/{id}.md",
        kind=kind, project=project, title=title, status=status,
        topics=topics if topics is not None else [],
        audience=["team"] if audience is _UNSET else audience,
        last_reviewed=last_reviewed, updated_at=updated_at, body=body,
        # **Derived from `body` when not given, the way the Library computes
        # them**, so a test cannot seed a row whose blurb and length disagree
        # with its own text — the fixture shape that would let a display test
        # pass against a document the Library could never serve.
        blurb=blurb if blurb is not None else _blurb_of(body or ""),
        body_len=body_len if body_len is not None else len(body or ""))
    return id


def seed_course(*, id, product, title, entries, status="live", version=1) -> str:
    """Register one Course row, in the shape `fake_list_courses`/
    `fake_get_course` serve — `entries` is a list of dicts with `issue_id`
    and `state` (the two fields every waypoint-pace caller reads) plus
    optional `title`/`maturity`/`specification`/`resolved_at`, matching
    `library_client.CourseEntry`."""
    _COURSES[id] = dict(id=id, product=product, title=title, status=status,
                        version=version, entries=list(entries))
    return id


def _course_entry(e: dict) -> library_client.CourseEntry:
    return library_client.CourseEntry(
        issue_id=e["issue_id"], state=e.get("state"), title=e.get("title"),
        maturity=e.get("maturity"), specification=e.get("specification"),
        resolved_at=e.get("resolved_at"))


def _course_record(row: dict) -> library_client.CourseRecord:
    return library_client.CourseRecord(
        id=row["id"], product=row["product"], title=row["title"], status=row["status"],
        version=row["version"], entries=[_course_entry(e) for e in row["entries"]],
        created_by="ops-mcp", created_at=None, updated_by="ops-mcp", updated_at=None)


def fake_list_courses(*, product=None, status="live") -> list[library_client.CourseRecord]:
    rows = [r for r in _COURSES.values() if r["status"] == status]
    if product:
        rows = [r for r in rows if r["product"] == product]
    return [_course_record(r) for r in rows]


def fake_get_course(course_id: str):
    row = _COURSES.get(course_id)
    return _course_record(row) if row is not None else None


def _blurb_of(body: str) -> str:
    """The Library's blurb rule, which is Orient's own applied to the stored
    body (convivy/library#107). Imported rather than restated so the fake cannot
    drift from the page it stands in for."""
    import engineering_build
    return engineering_build._blurb(body)


def _reference_doc_record(row: dict) -> library_client.ReferenceDocument:
    return library_client.ReferenceDocument(
        id=row["id"], source_ref=row["source_ref"], kind=row["kind"],
        project=row["project"], title=row["title"], status=row["status"],
        topics=row["topics"], audience=row["audience"],
        last_reviewed=row["last_reviewed"], updated_at=row["updated_at"],
        body=row["body"], blurb=row.get("blurb"), body_len=row.get("body_len"))


_DEFAULT_STATUSES = ("current", "draft")
# The route's permitted values, `all` included (convivy/library#103).
_KNOWN_STATUSES = frozenset(_DEFAULT_STATUSES) | {"deprecated", "archived"}


def fake_list_reference_documents(*, audience: str,
                                  status: str | None = None
                                  ) -> list["library_client.ReferenceDocument"]:
    """Stands in for `library_client.list_reference_documents`.

    Reproduces the Library's own default (`GET /reference/documents` with no
    `status` param serves only `current`/`draft`, `reference.DEFAULT_STATUSES`)
    and then reuses the real `_entitled` gate rather than re-implementing it —
    a fake with its own copy of the isinstance-guarded check could drift and
    stop catching what PR #205's review found (the substring-match bypass a
    missing `isinstance` opens).

    **`status="all"` lifts the filter entirely rather than expanding to the
    known values**, mirroring the route: a status this fake has never heard of
    is still returned under `"all"`, so a test cannot pass here on an
    implementation that would lose an unfamiliar status in production.

    **An unrecognised status raises `HTTPError(400)`**, because that is what a
    caller actually sees: `list_reference_documents` forwards `status` without
    validating it, so the refusal comes from the Library and reaches the caller
    through `_request` as an `HTTPError`. A `ValueError` here would be the shape
    of a LOCALLY validated parameter — what `create_signal` raises for an empty
    `acting_human` — and would hand whoever first writes a test against this
    branch the wrong exception type to assert. Raising at all is the point: a
    fake answering an empty list would let a misspelled status, the failure the
    400 was added for, read as "no documents matched" in every test using it."""
    if status is not None and status != "all" and status not in _KNOWN_STATUSES:
        raise urllib.error.HTTPError(
            None, 400,
            f"unknown status {status!r}; permitted: "
            f"{sorted(_KNOWN_STATUSES | {'all'})}", None, None)
    rows = _REFERENCE_DOCS.values()
    if status is None:
        rows = [r for r in rows if r["status"] in _DEFAULT_STATUSES]
    elif status != "all":
        rows = [r for r in rows if r["status"] == status]
    docs = [_reference_doc_record(r) for r in rows]
    return [d for d in docs if library_client._entitled(d, audience)]


def fake_list_reference_documents_unfiltered() -> list["library_client.ReferenceDocument"]:
    """Stands in for `library_client.list_reference_documents_unfiltered`.

    **Applies NO entitlement gate, matching the real one.** A fake that filtered
    here would hide the very property that function exists to provide — the
    Greater KB browse listing documents a reader may see exist but not open —
    and a test of that page would pass against a corpus it never actually gets.
    """
    return [_reference_doc_record(r) for r in _REFERENCE_DOCS.values()]


def fake_get_reference_document(doc_id: str, *, audience: str):
    """Stands in for `library_client.get_reference_document`. Collapses
    "no such document" and "not this audience" into `None`, same as the real
    function — reusing `_entitled` for the same reason `fake_list_reference_documents`
    does."""
    row = _REFERENCE_DOCS.get(doc_id)
    if row is None:
        return None
    doc = _reference_doc_record(row)
    return doc if library_client._entitled(doc, audience) else None


def fake_signal_acks(signal_ids, *, acting_human: str):
    """Mirrors `_ack_visible`: an id that is missing or customer-class is simply
    absent from the answer, the same collapse `fake_get_signal` gives a single
    id."""
    if not signal_ids:
        return set()
    visible = {sid for sid in signal_ids
              if (row := _SIGNALS.get(sid)) is not None and _may_see(row)}
    if not visible:
        return set()
    return {sid for sid in visible if (sid, acting_human) in _ACKS}


def seed_review_record(*, repo_identity, pr, role, verdict, reviewed_ref,
                       auth="run-verified", auth_reason=None, prose="",
                       findings=None, run_id=None, created_at=None) -> None:
    """Register one review record. Seeding order is age order: the last one
    seeded is the newest, which is the order the real route serves first."""
    _REVIEW_RECORDS.append(dict(
        id=f"rev_{len(_REVIEW_RECORDS)}", repo_identity=repo_identity, pr=pr,
        role=role, verdict=verdict, reviewed_ref=reviewed_ref, auth=auth,
        auth_reason=auth_reason, prose=prose, findings=list(findings or []),
        run_id=run_id, created_at=created_at, updated_at=created_at))


def fake_list_review_records(repo_identity: str, pr: int):
    rows = [r for r in reversed(_REVIEW_RECORDS)
            if r["repo_identity"] == repo_identity and r["pr"] == pr]
    return [library_client.ReviewRecord(**r) for r in rows]


def seed_person_scope(email: str, products) -> None:
    """Register the project slugs `email` resolves to through
    `library_client.person_scope` — the fake's stand-in for the Library's
    `POST /people/by-email/projects` (E19·1, issue_01M35K54QHJYYPXWS28YWEHJP3).
    Pass `{"*"}` for an administrator, matching what the real route answers
    for one: the full registry, never a flag this fake or `authz` branches
    on."""
    _PERSON_SCOPE[email.strip().lower()] = frozenset(products)


def fake_person_scope(email: str) -> frozenset[str]:
    """An email naming no seeded person resolves to the empty set — the real
    route's own answer for a person it does not know, never "every
    project"."""
    return _PERSON_SCOPE.get(email.strip().lower(), frozenset())


def patch(monkeypatch) -> None:
    _ISSUES.clear()
    _ISSUE_COMMENTS.clear()
    _SIGNALS.clear()
    _ACKS.clear()
    _REFERENCE_DOCS.clear()
    _COURSES.clear()
    _REVIEW_RECORDS.clear()
    _RELATED_ITEMS.clear()
    _CORRECTIONS_QUEUES.clear()
    _PERSON_SCOPE.clear()
    FAKE_CAPABILITIES.clear()
    FAKE_CAPABILITIES.add("signal.read")
    monkeypatch.setattr(library_client, "list_issue_items", fake_list_issue_items)
    monkeypatch.setattr(library_client, "get_issue_item", fake_get_issue_item)
    monkeypatch.setattr(library_client, "list_found_during", fake_list_found_during)
    monkeypatch.setattr(library_client, "list_related_item_ids", fake_list_related_item_ids)
    monkeypatch.setattr(library_client, "corrections_queue_ids", fake_corrections_queue_ids)
    monkeypatch.setattr(library_client, "list_signals", fake_list_signals)
    monkeypatch.setattr(library_client, "get_signal", fake_get_signal)
    monkeypatch.setattr(library_client, "signal_facets", fake_signal_facets)
    monkeypatch.setattr(library_client, "search_signals", fake_search_signals)
    monkeypatch.setattr(library_client, "signal_acks", fake_signal_acks)
    monkeypatch.setattr(library_client, "create_signal", fake_create_signal)
    monkeypatch.setattr(library_client, "ack_signal", fake_ack_signal)
    monkeypatch.setattr(library_client, "unack_signal", fake_unack_signal)
    monkeypatch.setattr(library_client, "list_reference_documents", fake_list_reference_documents)
    monkeypatch.setattr(library_client, "list_reference_documents_unfiltered",
                        fake_list_reference_documents_unfiltered)
    monkeypatch.setattr(library_client, "get_reference_document", fake_get_reference_document)
    monkeypatch.setattr(library_client, "list_courses", fake_list_courses)
    monkeypatch.setattr(library_client, "get_course", fake_get_course)
    monkeypatch.setattr(library_client, "list_review_records", fake_list_review_records)
    monkeypatch.setattr(library_client, "person_scope", fake_person_scope)
