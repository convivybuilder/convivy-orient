"""The Reading Room admin/provisioning API (ARCH-357).

Provisioning and curation of members' rooms: create a room with a seed, add or
update its entries, archive it at tenure end. All admin-only (room.admin, held by
the admin role alone), human-only, CSRF-checked, and written THROUGH this API per
the control DB's single-writer rule — one of the few tables still local to
Orient (ADR-0022), unlike the issue/signal/decision vaults this API used to
share that rule with.

Every entry a room gets is validated against the TARGET member's live grant at
write time, so a seed can never place a reference the member isn't entitled to.
A `doc` entry checks kb_index.room_entitlement, as it always has; an `item` or
`signal` entry (CQ·147, E22) is fetched from the Library and checked against the
member's Library-resolved project scope, and either kind is refused outright for
a contractor — their grant has no item or signal half at all. This is
belt-and-suspenders with the render, which intersects curation ∩ grant again on
every load: write-time validation gives a clean error, the render is the wall.

The concierge request→edit loop (a member editing their OWN room via an agent,
`room_request` below) applies the SAME per-kind check to whatever the model's
plan names (CQ·149, E22): a `doc`/`item`/`signal` reference the member is not
entitled to is refused and the whole plan applied or not at all, exactly as a
curated entry is refused here.
"""
from __future__ import annotations

import sys

from fastapi import APIRouter, Request
from sqlalchemy import select

import access_identity
import concierge
import library_client
from api_common import (
    _clip, _iso, _json, _nonempty, api_guard as _api, read_json_body,
    resolve_identity, same_origin,
)
from authz import authorize
from control_db import PRIVACY_INTERNAL, new_room_id, new_room_request_id, session
from kb_index import entitled_docs, room_entitlement
from models import ReadingRoom, RoomEntry, RoomRequest, utcnow

MAX_BODY = 512 * 1024   # a room seed (landing note + up to MAX_ENTRIES entries), bounded
MAX_TITLE = 300
MAX_DOC_ID = 200
MAX_SECTION = 120
MAX_TEXT = 100_000      # landing_note / note cap
MAX_ENTRIES = 500       # a full-tier everything-seed is ~150 docs; cap well above

router = APIRouter(prefix="/api")


def _log_grant_widening_request(room, req, escalate: dict) -> None:
    """Record a grant-widening ask as a signal, for a human to review later.

    A member asked for something outside their entitlement and no human is
    present to grant it live. This used to escalate to the decision plane
    (approve/redirect/deny); that plane is gone (the operator, 2026-08-18: "we're
    getting rid of the concept of decisions"), and no replacement mechanism
    was invented for it — the request is denied outright, here, and logged so
    the ask is not silently lost. See `room_request` below.

    **Best-effort.** The signal lives in the Library now (2026-08-19), outside
    the local transaction that commits the RoomRequest row, so a failure here
    must not block the denial itself — the member gets a clean answer either
    way, the same posture the removed decision-plane escalation held.
    """
    # CLIPPED HERE, not left to whatever reads this signal later. `req.actor` is
    # a verified email so it is bounded in practice, but an oversized title or
    # reason should not be able to blow the signal body out to something a
    # reader can't work with.
    title = _clip(f"Reading Room access request from {req.actor}", MAX_TITLE)
    wanted = _clip(str(escalate.get("wanted", "")), MAX_TEXT)
    reason = _clip(str(escalate.get("reason", "")), 500)
    try:
        library_client.create_signal(
            source="room", kind="room-grant-denied", actor=req.actor,
            product="shared", privacy_class=PRIVACY_INTERNAL,
            refs={"room_id": room.id, "request_id": req.id},
            body=f"{title}\n\nWanted: {wanted}\nReason: {reason}")
    except Exception as exc:
        print(f"room-grant-denied signal failed for {req.id}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)


# --- serialization ------------------------------------------------------------


def _entry_dict(e: RoomEntry) -> dict:
    return {
        "doc_id": e.doc_id, "kind": e.kind, "section": e.section, "position": e.position,
        "note": e.note, "pinned": e.pinned, "source": e.source,
    }


def _room_dict(room: ReadingRoom) -> dict:
    return {
        "id": room.id, "url": "/room", "email": room.email, "title": room.title,
        "landing_note": room.landing_note, "status": room.status,
        "created_by": room.created_by, "created_at": _iso(room.created_at),
        "entries": [_entry_dict(e) for e in room.entries],
    }


# --- shared entry validation --------------------------------------------------

_KINDS = ("doc", "item", "signal")


def _in_scope(product: str, scope: frozenset[str]) -> bool:
    """Whether `product` is in the member's Library-resolved scope. "*" means
    every product — the Library's own answer for an administrator, never
    something this module decides (the same rule `issue_build._in_scope`
    applies to a Chart House read)."""
    return "*" in scope or product in scope


def _clean_entries(raw, email: str) -> tuple[list[dict] | None, str | None]:
    """Validate an entries payload against `email`'s live grant. Returns
    (entries, None) or (None, error).

    Every entry names a `kind` (doc | item | signal — "doc" when omitted, so
    every existing doc-only caller is unaffected) and a reference in `doc_id`.
    A `doc` entry is checked against the member's KB entitlement, as before.
    An `item` or `signal` entry is checked by fetching it from the Library and
    requiring its product to be in the member's Library-resolved project scope
    (E19·1) — and, for either, requiring the member's tier to be "full": a
    contractor's grant admits neither kind at all, so the check never reaches
    the Library for a contractor's entry.

    ONE REFUSAL ANSWERS BOTH an out-of-scope reference and one that does not
    exist, within a kind. `library_client.get_issue_item`/`get_signal` already
    collapse "missing" and "out of this credential's scope" into the same
    None, and the scope check below reads identically whether the record came
    back out of scope or not at all — naming nothing about whether a rejected
    id is real."""
    if not isinstance(raw, list):
        return None, "entries must be a list"
    if len(raw) > MAX_ENTRIES:
        return None, "too many entries"
    scope = library_client.person_scope(email)
    doc_grant = room_entitlement(email, scope)
    tier = access_identity.tier_for(email)
    out = []
    seen = set()
    for e in raw:
        if not isinstance(e, dict):
            return None, "each entry must be an object"
        kind = e.get("kind", "doc")
        if kind not in _KINDS:
            return None, f"kind must be one of {_KINDS}"
        doc_id = e.get("doc_id")
        if not _nonempty(doc_id) or len(doc_id) > MAX_DOC_ID:
            return None, "each entry needs a valid doc_id"
        if (kind, doc_id) in seen:
            return None, f"duplicate {kind} entry: {doc_id}"
        if kind == "doc":
            if doc_id not in doc_grant:
                return None, f"doc_id not in this member's entitlement: {doc_id}"
        elif kind == "item":
            item = library_client.get_issue_item(doc_id) if tier == "full" else None
            if item is None or not _in_scope(item.product, scope):
                return None, f"item not in this member's entitlement: {doc_id}"
        else:  # signal
            sig = library_client.get_signal(doc_id) if tier == "full" else None
            if sig is None or not _in_scope(sig.product, scope):
                return None, f"signal not in this member's entitlement: {doc_id}"
        seen.add((kind, doc_id))
        pinned = e.get("pinned", False)
        position = e.get("position", 0)
        # bool is an int subclass, so reject it explicitly for position (a JSON
        # `true` would otherwise store as 1).
        if not isinstance(pinned, bool) or isinstance(position, bool) or not isinstance(position, int):
            return None, "pinned must be a boolean and position a non-boolean integer"
        out.append({
            "kind": kind,
            "doc_id": doc_id,
            "section": _clip(e.get("section", ""), MAX_SECTION),
            "position": position,
            "note": _clip(e.get("note", ""), MAX_TEXT) or None,
            "pinned": pinned,
        })
    return out, None


# --- routes -------------------------------------------------------------------


@router.post("/rooms")
@_api
async def create_room(request: Request):
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return _json(403, {"error": "only a human admin may provision rooms"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    if not authorize(ident, "room.admin", None):
        return _json(403, {"error": "forbidden"})
    payload, err = await read_json_body(request, MAX_BODY)
    if err:
        return err
    email = payload.get("email")
    if not _nonempty(email):
        return _json(400, {"error": "email is required"})
    email = email.strip().lower()
    title = _clip(payload.get("title", ""), MAX_TITLE)
    landing_note = _clip(payload.get("landing_note", ""), MAX_TEXT)
    # The TARGET member's own grant, not the provisioning admin's — a room is
    # provisioned for `email`, so entries are checked against `email`'s own
    # live entitlement/scope/tier, resolved fresh inside `_clean_entries`.
    entries, e_err = _clean_entries(payload.get("entries", []), email)
    if e_err:
        return _json(400, {"error": e_err})
    with session() as s:
        if s.scalar(select(ReadingRoom).where(ReadingRoom.email == email)):
            return _json(409, {"error": "this member already has a room"})
        room = ReadingRoom(
            id=new_room_id(), email=email, title=title, landing_note=landing_note,
            created_by=ident.actor)
        for e in entries:
            room.entries.append(RoomEntry(
                kind=e["kind"], doc_id=e["doc_id"], section=e["section"],
                position=e["position"], note=e["note"], pinned=e["pinned"],
                source="provision", added_by=ident.actor))
        s.add(room)
        s.flush()
        return _json(201, _room_dict(room))


@router.get("/rooms/{room_id}")
@_api
async def get_room(room_id: str, request: Request):
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    # Consistent with the mutating routes: room admin is a human-only surface.
    # (authorize() already fails closed for agent/system — this is explicitness.)
    if ident.kind != "human":
        return _json(403, {"error": "only a human admin may read rooms"})
    if not authorize(ident, "room.admin", None):
        return _json(403, {"error": "forbidden"})
    with session() as s:
        room = s.get(ReadingRoom, room_id)
        if room is None:
            return _json(404, {"error": "not found"})
        return _json(200, _room_dict(room))


@router.post("/rooms/{room_id}/entries")
@_api
async def add_entries(room_id: str, request: Request):
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return _json(403, {"error": "only a human admin may curate rooms"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    if not authorize(ident, "room.admin", None):
        return _json(403, {"error": "forbidden"})
    payload, err = await read_json_body(request, MAX_BODY)
    if err:
        return err
    with session() as s:
        room = s.get(ReadingRoom, room_id)
        if room is None:
            return _json(404, {"error": "not found"})
        # The TARGET member's own grant, the same reason create_room resolves
        # it fresh rather than reusing the curating admin's ident.products.
        entries, e_err = _clean_entries(payload.get("entries", []), room.email)
        if e_err:
            return _json(400, {"error": e_err})
        existing = {(e.kind, e.doc_id): e for e in room.entries}
        for e in entries:
            row = existing.get((e["kind"], e["doc_id"]))
            if row is not None:  # update presentation in place (unique on room_id+kind+doc_id)
                row.section, row.position = e["section"], e["position"]
                row.note, row.pinned = e["note"], e["pinned"]
                row.source, row.added_by = "steward", ident.actor
            else:
                row = RoomEntry(
                    kind=e["kind"], doc_id=e["doc_id"], section=e["section"],
                    position=e["position"], note=e["note"], pinned=e["pinned"],
                    source="steward", added_by=ident.actor)
                room.entries.append(row)
                existing[(e["kind"], e["doc_id"])] = row
        s.flush()
        return _json(200, _room_dict(room))


@router.post("/rooms/{room_id}/archive")
@_api
async def archive_room(room_id: str, request: Request):
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return _json(403, {"error": "only a human admin may archive rooms"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    if not authorize(ident, "room.admin", None):
        return _json(403, {"error": "forbidden"})
    with session() as s:
        room = s.get(ReadingRoom, room_id)
        if room is None:
            return _json(404, {"error": "not found"})
        room.status = "archived"
        s.flush()
        return _json(200, _room_dict(room))


# --- the concierge edit loop (ARCH-357 fast-follow, widened CQ·149/E22) ------


def _apply_actions(room: ReadingRoom, actions, doc_grant: frozenset[str],
                    scope: frozenset[str], tier: str, actor: str
                    ) -> tuple[bool, dict | None]:
    """Apply the concierge plan to the member's OWN room. Two passes: pre-validate
    every referenced entry against the member's live, PER-KIND grant FIRST (so a
    single out-of-grant entry refuses the whole plan with no partial mutation),
    then apply.

    Returns `(True, None)` on success, or `(False, denied)` on refusal — the
    caller records a failed request and the transaction commits only the
    (unchanged) room + audit row. `denied` is `{"kind", "doc_id"}` naming the
    entry that failed an entitlement check, so the caller can log it as a
    grant-widening ask (the model proposing one is exactly a member asking for
    something outside their entitlement, the case
    `_log_grant_widening_request` exists for) — or `None` when the refusal is a
    malformed plan rather than an entitlement question, which is not a request
    for anything and so names nothing to log.

    `doc_grant` is the member's KB entitlement (kind="doc", unchanged); `scope`
    is their Library-resolved project scope and `tier` their tier, the same two
    inputs `_clean_entries` uses to check an `item`/`signal` reference — this is
    that same per-kind check, reused rather than re-derived."""
    if not isinstance(actions, list):
        return False, None
    # Pass 1 — validate. A malformed action, or an `add` of an out-of-grant or
    # nonexistent entry, refuses the ENTIRE plan (no partial mutation). Only
    # `add` is grant-gated: it is the one op that introduces a NEW entry.
    # remove/reorder/annotate act on EXISTING entries (a no-op if absent) and
    # never widen access — the render intersects grant ∩ curation regardless —
    # so a member can always clean up or reorder an entry whose grant later
    # narrowed.
    for a in actions:
        if not isinstance(a, dict):
            return False, None
        op = a.get("op")
        if op not in ("add", "remove", "reorder", "annotate", "set_landing_note"):
            return False, None
        if op == "set_landing_note":
            continue
        d = a.get("doc_id")
        if not _nonempty(d):
            return False, None
        kind = a.get("kind", "doc")
        if kind not in _KINDS:
            return False, None
        if op == "add":
            if kind == "doc":
                if d not in doc_grant:
                    return False, {"kind": kind, "doc_id": d}
            elif kind == "item":
                item = library_client.get_issue_item(d) if tier == "full" else None
                if item is None or not _in_scope(item.product, scope):
                    return False, {"kind": kind, "doc_id": d}
            else:  # signal
                sig = library_client.get_signal(d) if tier == "full" else None
                if sig is None or not _in_scope(sig.product, scope):
                    return False, {"kind": kind, "doc_id": d}
    # Pass 2 — apply. Keyed on (kind, doc_id), matching RoomEntry's own
    # uniqueness (models.py) now that the same id text can in principle name a
    # doc and an item/signal without colliding.
    existing = {(e.kind, e.doc_id): e for e in room.entries}
    for a in actions:
        op = a["op"]
        if op == "set_landing_note":
            room.landing_note = _clip(a.get("text", ""), MAX_TEXT)
            continue
        kind = a.get("kind", "doc")
        doc_id = a["doc_id"]
        pos = a.get("position", 0)
        pos = pos if isinstance(pos, int) and not isinstance(pos, bool) else 0
        key = (kind, doc_id)
        if op == "add":
            if key not in existing:
                e = RoomEntry(
                    kind=kind, doc_id=doc_id,
                    section=_clip(a.get("section", "") or "Added", MAX_SECTION),
                    position=pos, source="request", added_by=actor)
                room.entries.append(e)
                existing[key] = e
        elif op == "remove":
            e = existing.pop(key, None)
            if e is not None:
                room.entries.remove(e)
        elif op == "reorder":
            e = existing.get(key)
            if e is not None:
                if _nonempty(a.get("section")):
                    e.section = _clip(a["section"], MAX_SECTION)
                e.position = pos
        elif op == "annotate":
            e = existing.get(key)
            if e is not None:
                e.note = _clip(a.get("text", ""), MAX_TEXT) or None
    return True, None


@router.post("/room/requests")
@_api
async def room_request(request: Request):
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return _json(403, {"error": "only a human may edit a room"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    # A member edits their OWN room only — the target is the verified email, never
    # a client-supplied id — and must hold room.request (deny-by-default).
    if not authorize(ident, "room.request", None):
        return _json(403, {"error": "forbidden"})
    payload, err = await read_json_body(request, 8 * 1024)  # a request is a sentence
    if err:
        return err
    body = payload.get("request")
    if not _nonempty(body):
        return _json(400, {"error": "request text is required"})
    body = _clip(body, 4000)
    cfg = concierge.config()
    if cfg is None:
        return _json(503, {"error": "concierge unavailable"})

    email = ident.actor
    doc_grant = room_entitlement(email, ident.products)
    # The Library-resolved project scope and tier an `item`/`signal` reference is
    # checked against — the same two inputs `_clean_entries` uses for the admin
    # curation API (CQ·147), resolved fresh so a narrowed scope takes effect on
    # this request rather than a cached one.
    scope = library_client.person_scope(email)
    tier = access_identity.tier_for(email)
    # Input-side minimization: the model sees ONLY this member's entitled catalog.
    # There is no item/signal catalog — see concierge.py's module docstring.
    catalog = concierge.build_catalog(entitled_docs(email, ident.products))
    with session() as s:
        room = s.scalar(select(ReadingRoom).where(
            ReadingRoom.email == email, ReadingRoom.status == "active"))
        if room is None:
            return _json(404, {"error": "no room"})
        room_state = {
            "landing_note": room.landing_note,
            "entries": [{"doc_id": e.doc_id, "kind": e.kind, "section": e.section,
                         "position": e.position, "note": e.note or ""} for e in room.entries],
        }
        req = RoomRequest(id=new_room_request_id(), room_id=room.id, actor=email,
                          body=body, status="failed", plan={}, reply="")
        try:
            plan = concierge.interpret(cfg, catalog, room_state, body)
        except Exception:
            req.reply = "Sorry — I couldn't process that just now. Please try again."
            req.resolved_at = utcnow()
            s.add(req)
            s.flush()
            return _json(502, {"status": "failed", "reply": req.reply})
        req.plan = plan if isinstance(plan, dict) else {}
        reply = _clip((plan.get("reply") if isinstance(plan, dict) else "") or "", MAX_TEXT) or "Done."

        # Grant WIDENING is impossible in-loop: an out-of-grant ask is denied
        # outright and logged for a human to find later, rather than escalated —
        # there is no live human-in-the-loop mechanism left to grant it
        # (the decision plane withdrawal, the operator 2026-08-18). The concierge never
        # writes roles.json/audience.
        escalate = plan.get("escalate") if isinstance(plan, dict) else None
        if isinstance(escalate, dict):
            _log_grant_widening_request(room, req, escalate)
            req.status = "denied"
            req.reply = ("I can't grant that — it's outside what you're "
                         "entitled to see, and there's no one to ask right now. "
                         "Your request has been logged for review.")
            req.resolved_at = utcnow()
            s.add(req)
            s.flush()
            return _json(200, {"status": "denied", "reply": req.reply})

        actions = plan.get("actions") if isinstance(plan, dict) else None
        ok, denied = _apply_actions(room, actions or [], doc_grant, scope, tier, email)
        if not ok:
            # `denied` names an entitlement failure (as opposed to a malformed
            # plan, which names nothing to log): the model proposed an add the
            # member is not entitled to, which is itself a grant-widening ask,
            # logged the same way an explicit `escalate` is above.
            if denied is not None:
                _log_grant_widening_request(room, req, {
                    "reason": "the concierge plan named a reference outside "
                              "this member's grant",
                    "wanted": f"{denied['kind']}:{denied['doc_id']}",
                })
            req.status = "failed"
            req.reply = "That referenced something outside your room, so nothing was changed."
            req.resolved_at = utcnow()
            s.add(req)
            s.flush()
            return _json(200, {"status": "failed", "reply": req.reply})
        req.status, req.reply, req.resolved_at = "applied", reply, utcnow()
        s.add(req)
        s.flush()
        return _json(200, {"status": "applied", "reply": reply})
