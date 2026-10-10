#!/usr/bin/env python3.11
"""`POST /api/epics/{issue_id}/revise` — edit a proposed epic's text in place.

**The operator, 2026-09-22:** proposed epics show "just Decline, Approve, and if it's
possible to send it to the edit pane we should do that too." The edit pane
they meant edits KB documents and cannot reach a work item, so this is the
equivalent for an epic: the specification and definition of done, edited on
the epic's own page and saved to the Library.

It follows `approve_api.py`'s gate step for step — human only, same origin,
the epic read first so the capability can be scoped to its product — and
gates on `epic.revise`, which maps to the same `epic_approve` capability,
because the text is what an approval approves. What differs is worth a
reviewer's attention:

- **Only a put-forward epic is editable here** (`issue_build.put_forward`,
  the rule the Chart House and the card read). An approved epic's text is
  approved text, and changing it from a web form would quietly change what
  was approved.
- **The Library's lease is taken at SAVE time, not while the human types.**
  `/revision` requires a live lease, and checking a work item out also claims
  it, so a lease held for as long as the editor stayed open would show the
  epic as claimed and fence off its children meanwhile. The route checks out,
  re-reads, revises and releases inside one request, and releases in a
  `finally` so a refused revision does not strand the claim.
- **A save over text that changed since the editor opened is refused.** The
  browser sends the text it started from as `base`, and the route compares it
  with the epic as read UNDER the lease, where nobody else can be writing. The
  alternative is the last save silently discarding someone else's edit.
- **The holder is the verified email** (`ident.actor`), never a body value, as
  in `kb_write_api`. The Library's revision note records it ("holding the
  lease as …"). That is a coordination label and not an attestation: unlike
  `/approve`, `/revision` takes no `acting_human`, so the person is recorded
  only as the label Orient chose. Orient still refuses to set it from the
  request.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

import issue_build
import library_client
from api_common import _json, api_guard as _api, resolve_identity, same_origin
from authz import authorize
from control_db import ISSUE_ACTIVE, ISSUE_TYPE_EPIC

router = APIRouter(prefix="/api")

# The two fields this route edits. Title and body stay with the crew that
# filed the epic: what an approval reads is these two.
_FIELDS = ("specification", "definition_of_done")


def _norm(text) -> str:
    """Compare text the way a browser round-trips it. A `<textarea>` drops one
    leading newline and serves `\\n` line breaks whatever it was given, so an
    untouched field would otherwise compare unequal to the stored text and
    every save would read as a conflict."""
    return (text or "").replace("\r\n", "\n").strip()


@router.post("/epics/{issue_id:path}/revise")
@_api
async def revise_epic(issue_id: str, request: Request):
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        # 403 and first, for approve_api's reason: the act is a human's.
        return _json(403, {"error": "forbidden: editing an epic here is a human act"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    try:
        payload = await request.json()
    except Exception:
        return _json(400, {"error": "body must be a JSON object"})
    base = payload.get("base") if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or not isinstance(base, dict)
            or any(not isinstance(payload.get(f), str) or not isinstance(base.get(f), str)
                   for f in _FIELDS)):
        return _json(400, {"error": "specification, definition_of_done and base "
                                    "(the text the editor opened with) are required strings"})
    if not issue_id.strip():
        return _json(404, {"error": "not found"})

    # Read first, for the product the capability is scoped to — approve_api's
    # reasoning, verbatim in effect.
    item = library_client.get_issue_item(issue_id)
    if item is None or item.type != ISSUE_TYPE_EPIC:
        return _json(404, {"error": "not found"})
    if not authorize(ident, "epic.revise", item.product):
        return _json(404, {"error": "not found"})
    if item.state not in ISSUE_ACTIVE:
        return _json(409, {"error": "this epic is closed, so its text is its record"})
    if not issue_build.put_forward(item):
        return _json(409, {"error": "only a proposed epic is edited here; this one "
                                    "is approved, and its text is what was approved"})

    wanted = {f: _norm(payload[f]) for f in _FIELDS}
    if all(wanted[f] == _norm(getattr(item, f)) for f in _FIELDS):
        # Nothing to write, so no reason to claim the epic even for a moment.
        return _json(200, {"saved": False, "unchanged": True})

    owner = ident.actor
    try:
        library_client.checkout_item(issue_id, owner=owner)
    except library_client.ItemWriteRefused as refused:
        return _json(409, {"error": refused.message, "saved": False})

    status, out = 500, {"error": "the edit did not complete", "saved": False}
    try:
        status, out = _revise_held(issue_id, owner, base, wanted)
    finally:
        try:
            library_client.release_item(issue_id, owner=owner)
        except Exception as e:  # noqa: BLE001 — reported in the response, never swallowed
            # Whatever happened to the edit, say that the hand-back failed rather
            # than let the epic sit claimed until its claim expires unexplained.
            out["release_error"] = (f"the epic is still checked out as {owner}: {e}")
    return _json(status, out)


def _revise_held(issue_id: str, owner: str, base: dict, wanted: dict) -> tuple[int, dict]:
    """The part of a save that runs under the lease."""
    # Re-read UNDER the lease: from here nobody else can be writing, so this is
    # the comparison that decides whether the save would discard an edit.
    held = library_client.get_issue_item(issue_id)
    if held is None:
        return 404, {"error": "not found"}
    # Both halves of the outer gate, again, because only under the lease are
    # they still true when we write. Declining is `wontfix` and leaves
    # `maturity` alone, so a declined proposal still reads as put forward and
    # only its state shows it is closed.
    if held.state not in ISSUE_ACTIVE:
        return 409, {"error": "this epic was closed while you were editing, so its "
                              "text is its record",
                     "saved": False}
    if not issue_build.put_forward(held):
        # Approved between the first read and the checkout. Checked again here
        # because only under the lease is the answer still true when we write.
        return 409, {"error": "this epic was approved while you were editing, and "
                              "its text is now what was approved",
                     "saved": False}
    if any(_norm(base[f]) != _norm(getattr(held, f)) for f in _FIELDS):
        return 409, {"error": "this epic's text changed since you opened the editor. "
                              "Your draft is kept; reload to see the current text.",
                     "saved": False, "conflict": True}
    # Only what changed, so an untouched field is not rewritten. An emptied
    # field is sent as null, which the Library reads as "clear it".
    changed = {f: (wanted[f] or None) for f in _FIELDS
               if wanted[f] != _norm(getattr(held, f))}
    if not changed:
        # Someone saved this same text first. The Library refuses a revision
        # naming no field, and there is nothing to write anyway.
        return 200, {"saved": False, "unchanged": True}
    try:
        library_client.revise_item(issue_id, owner=owner, fields=changed)
    except library_client.ItemWriteRefused as refused:
        return 409, {"error": refused.message, "saved": False}
    return 200, {"saved": True, "fields": sorted(changed)}
