"""Manual signal capture proxy (S2, ARCH-349; issue_01M08YKNV185DFJ6832GV8ZY22).

The "Log a signal" form's `POST /api/signals` target, moved to the Library.
Field validation and the dedup no-leak rule are ported unchanged from the
deleted `signal_api.py` — the Library's own `POST /signals` re-validates and
re-applies dedup independently (it has to, for its other callers), so this is
belt-and-suspenders rather than the only check, but a caller talking to Orient
still gets Orient's own error shapes rather than a bare passthrough.

Orient's own authz gate (`signal.create` / `signal.create_customer`,
roles.json) is the per-human wall, checked here before the write — the same
reasoning `signal_ack_api.py`'s `_ack_visible` carries: the write goes out
under one of Orient's own SHARED credentials, which knows nothing about who
asked Orient, so Orient's per-human role is what scopes it, not the Library.

**A capture a HUMAN submitted carries that person as `acting_human`, and
that is what picks the credential.** `ingested_by` records the credential, so
without this every capture a person made through Orient reads as Orient having
made it and the person is lost (`issue_01M0R0CDCAG6SMC2X8ZTTGY0GR`). The
Library reserves the column for an approved-client credential, so passing the
verified email flips `create_signal` onto Orient's approved-client one — the
same credential and the same `identity.attested_human` path `signal_ack_api.py`
already uses for the other act reserved to a person.

**An agent's capture passes none and keeps the agent credential.** An agent
filing what it observed is a machine acting on its own, which is the state the
Library's route calls the design rather than a gap.

**`acting_human` IS NEVER READ OFF THE REQUEST BODY**, and a body carrying one
is refused with a 400. It is derived here from the identity Cloudflare Access
verified, because a caller-supplied value would let a bearer-authed agent name
any person as the actor — the attestation would then assert whatever the caller
typed, which is the one thing the column exists not to do. Refused rather than
dropped, so a caller that sent one is told it was not recorded.

**This is what makes customer-class capture reachable again, and the
per-human gate above is now the real wall.** The approved-client credential is
`system` plus the human-reserved capabilities, so it holds
`signal.create_customer`; between the capture write moving to the Library
(2026-08-19) and now, Orient's shared AGENT credential lacked that capability
and every customer-class capture refused with a 403 no matter what the
submitting human's own role said. `authorize(ident, "signal.create_customer",
product)` below was checking into a wall. ADR-0038 puts per-human scoping on
the client, and that check is now what decides it. An AGENT caller is
unaffected: it passes no `acting_human`, keeps the narrow credential, and a
customer-class capture from it still refuses.

**A `dedup_key` collision this caller could have created is success (200,
`idempotent: true`), not a 409** — the same distinction the deleted
`signal_api.py` made and the Library's own `POST /signals` still makes
server-side. Two real callers depend on it: `github_capture.py`'s
overlap-window poller reads the response status to tell "created" from
"duplicate" and only advances its cursor once a batch has zero errors, and
the "Log a signal" form's own double-submit guard expects a resubmit to read
as success. `library_client.create_signal` returns `(id, idempotent,
existing)` rather than a bare id so this proxy can tell the two apart.

**But an `idempotent` hit still needs its OWN visibility check before the id
goes out, and the wider credential makes that check matter MORE rather than
less** (security review, PR #179 round 4). The Library answers a collision with
the existing row only for a caller that could have created it, judged against
the credential that asked — so under the approved-client credential a human's
collision can now resolve to a customer-class or cross-product row the agent
credential would have refused outright. `_ack_visible` re-checks the row that
was actually hit against THIS human's own role, including
`signal.read_customer`, and answers the same bare 409 either way. `authorize(ident,
"signal.create", product)` above checks the product the CALLER DECLARED, not
the product the EXISTING row actually belongs to — the Library's own dedup
match is scoped to whichever shared credential Orient presented, and neither
of them has any notion of Orient's own per-human/per-product roles.json
scoping. Without a second
check, a caller scoped to one product could learn the real id of a signal in
a product it holds no grant on at all, just by guessing or knowing its
`dedup_key`. `signal_ack_api._ack_visible` already does exactly this check
(re-`authorize` against the row's ACTUAL product/privacy_class) for the same
reason on the ack path, so it is reused here rather than re-derived — a
collision that fails it answers the same bare 409 as one the Library itself
refuses, so the response body discloses nothing either way.

**`existing` is parsed from `create_signal`'s own response, never fetched
with a second `get_signal` call** (security review, PR #179 round 5). A
second request only on the idempotent branch would give it one more round
trip than the true-409 branch (raised inside the first request, before any
second call could happen) — identical response bodies with different
latencies is still a working oracle, just a timing one instead of a content
one. Keeping both branches at exactly one backend call closes that too.
"""
from __future__ import annotations

import urllib.error

from fastapi import APIRouter, Request

import library_client
from api_common import _json, _nonempty, api_guard, read_json_body, resolve_identity, same_origin
from authz import authorize
from control_db import PRIVACY_CLASSES, PRIVACY_CUSTOMER, PRIVACY_INTERNAL
from signal_ack_api import _ack_visible

router = APIRouter(prefix="/api")

MAX_SIGNAL_BODY = 512 * 1024   # a captured email/review can be large; still bounded
MAX_IDENT = 64                 # source / kind / product identifiers
MAX_ACTOR = 200
MAX_STREAM = 200


@router.post("/signals")
@api_guard
async def create_signal(request: Request):
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind == "human" and not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    payload, err = await read_json_body(request, MAX_SIGNAL_BODY)
    if err:
        return err

    source, kind, actor, product = (payload.get("source"), payload.get("kind"),
                                    payload.get("actor"), payload.get("product"))
    if not (_nonempty(source) and _nonempty(kind) and _nonempty(actor) and _nonempty(product)):
        return _json(400, {"error": "source, kind, actor, product are required"})
    if len(source) > MAX_IDENT or len(kind) > MAX_IDENT or len(product) > MAX_IDENT:
        return _json(400, {"error": "source/kind/product too long"})
    if len(actor) > MAX_ACTOR:
        return _json(400, {"error": "actor too long"})
    stream = payload.get("stream")
    if stream is not None and (not isinstance(stream, str) or len(stream) > MAX_STREAM):
        return _json(400, {"error": "stream must be a short string"})
    refs = payload.get("refs") or {}
    meta = payload.get("meta") or {}
    if not isinstance(refs, dict) or not isinstance(meta, dict):
        return _json(400, {"error": "refs and meta must be objects"})
    privacy = payload.get("privacy_class") or PRIVACY_INTERNAL
    if privacy not in PRIVACY_CLASSES:
        return _json(400, {"error": f"privacy_class must be one of {sorted(PRIVACY_CLASSES)}"})
    dedup_key = payload.get("dedup_key") or None
    if dedup_key is not None and (not isinstance(dedup_key, str) or len(dedup_key) > 300):
        return _json(400, {"error": "dedup_key must be a string of at most 300 chars"})
    if "acting_human" in payload:
        return _json(400, {"error": "acting_human is not a caller-supplied field; "
                                    "it is the verified identity of the submitting human"})

    # authz: the base capability first, then the elevated one for customer content.
    if not authorize(ident, "signal.create", product):
        return _json(403, {"error": "forbidden"})
    if privacy == PRIVACY_CUSTOMER and not authorize(ident, "signal.create_customer", product):
        return _json(403, {"error": "forbidden: customer-class capture requires signal_customer"})

    # THE VERIFIED EMAIL, NEVER `ident.actor` UNCONDITIONALLY. An agent identity
    # also has an `actor` — its token name — and writing that here would record
    # the client as the person, which is the fallback `identity.attested_human`
    # names as the thing that must not happen.
    acting_human = ident.actor if ident.kind == "human" else None
    try:
        sid, idempotent, existing = library_client.create_signal(
            source=source, kind=kind, actor=actor, product=product,
            body=payload.get("body", ""), stream=stream, refs=refs or None,
            meta=meta or None, privacy_class=privacy, dedup_key=dedup_key,
            acting_human=acting_human)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # A dedup_key collision this caller could NOT have created — the
            # Library's own no-leak rule, not a fault. This is the ONLY
            # dedup_key outcome that reaches this branch: a collision the
            # caller COULD have created is the Library's own 200, which
            # `create_signal` returns as `idempotent=True` below rather than
            # raising — the distinction two real callers depend on
            # (`github_capture.py`'s overlap-window poller reads "duplicate"
            # vs "created" off the status; the capture form's own
            # double-submit guard expects a resubmit to read as success).
            return _json(409, {"error": "dedup_key already in use"})
        if e.code == 403:
            if acting_human is not None:
                # NOT THE CALLER'S FAULT, so not a 403 back. On the attested
                # path Orient's own gates above have already decided this human
                # may make this capture, and the credential carrying it holds
                # `signal.create_customer` — so a refusal here means the
                # approved-client credential was rejected (a stale or
                # unregistered token in the Library's `client-tokens.json`),
                # which is a deployment fault dressed as a permission one.
                # Answering 403 would send whoever hit it to check roles.json,
                # where there is nothing to find.
                return _json(502, {"error": "the Library refused Orient's "
                                            "approved-client credential"})
            return _json(403, {"error": "forbidden: customer-class capture requires signal_customer"})
        return _json(502, {"error": "the Library refused the write"})
    except Exception:
        return _json(502, {"error": "the Library was unreachable"})

    if idempotent:
        # The Library matched an EXISTING row, which may belong to a product
        # or privacy_class this caller's own role does not cover — the check
        # above only validated the product the caller DECLARED. Re-check
        # against the row this collision actually resolved to before handing
        # its id back; a failure here answers the same 409 a genuine no-leak
        # refusal does, so nothing is disclosed either way. `existing` came
        # back on the SAME response `create_signal` already made — no second
        # request here, deliberately: an extra round trip only on this branch
        # would let a caller distinguish "collision I can't see" from "no
        # collision at all" by timing alone, even with an identical body
        # (security review, PR #179 round 5).
        if not _ack_visible(ident, existing):
            return _json(409, {"error": "dedup_key already in use"})
        return _json(200, {"id": sid, "idempotent": True})
    return _json(201, {"id": sid, "idempotent": False})
