#!/usr/bin/env python3.11
"""`POST /api/epics/{issue_id}/approve` — the operator's approve button
(issue_01M32CB6A9T04NVYPARGDT2GWQ).

**The operator, 2026-09-21:** "all epics stay as proposed, and are displayed as
proposed, until the operator presses the approve button in either Deck or
Orient, and that is the path to mark epics as approved." Pressing it also
appends the epic to its product's Course.

Approving is one of the acts the Library reserves for a verified human, on the
approved-client shape ADR-0038 describes: the capability lives on Orient's
credential, a browser cannot hold that credential, and the person travels as
`acting_human` for the record. This module is the proxy, and it follows
`signal_ack_api.py` step for step, because that is the worked example of
exactly this shape. What differs is worth a reviewer's attention:

- **The Library's route is new and gates on `epic.approve`**, which only an
  `approved_client` resolves to. The old path — `POST /items/{id}/correct`
  with `maturity: approved` — was gated on `item.resolve`, which every
  dispatched agent holds, so any agent could approve its own epic. That is the
  defect the pair of epics closes, and it is why nothing here may fall back to
  the correction route.
- **A capability gate on top of the human check, scoped to the epic's own
  product.** `epic.approve` is granted to admin and product_owner
  (`authz.ROLE_CAPS`), so a member or contractor is refused here as well as
  shown no control — and
  a human scoped to one product cannot approve another product's epic, because
  an epic belongs to a product and `authz` treats every unlisted action as
  product-scoped. The epic is therefore READ FIRST, to learn what to scope the
  check to; see the comment at that call.
- **404, not 403, for a human lacking the capability**, matching `policy_api`'s
  choice and for its reason: a full-tier human who does not hold `epic_approve`
  is not told the route exists. The NON-HUMAN refusal is a deliberate 403,
  matching `signal_ack_api` — the caller authenticated fine and it is the ACT
  that is unavailable, which discloses nothing an agent does not already know
  about itself. The epic's definition of done names that 403 specifically.

**`acting_human` is `ident.actor` and is never read off the request body**, the
rule `signal_capture_api` and `policy_api` both state for this field: a
caller-supplied value would let the attestation assert whatever the caller
typed, which is the one thing the Library's `attested_human` exists not to do.

**A refusal is rendered, not swallowed.** The specification requires that "a
refusal from the Library renders where the control is, naming what was
refused," and that the epic "stays proposed and keeps its control." So a
Library refusal comes back as a 409 carrying the Library's own wording, rather
than a bare 500 the page could only report as "something went wrong."

**Nothing here writes `maturity` by any other path**, and nothing else in
Orient writes it at all — Orient reads the field everywhere else.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

import library_client
from api_common import _json, api_guard as _api, resolve_identity, same_origin
from authz import authorize
from control_db import ISSUE_TYPE_EPIC

router = APIRouter(prefix="/api")


@router.post("/epics/{issue_id:path}/approve")
@_api
async def approve_epic(issue_id: str, request: Request):
    """Approve `issue_id` as the calling human, placing it on its product's
    Course in the same act.

    Not idempotent in the way `signal_ack_api`'s ack is, and deliberately not
    made so here: re-approving an epic already on its Course is the Library's
    own business (its route neither duplicates the entry nor moves it), so this
    proxy passes the second press through rather than guessing at the answer
    from a state it would have to read separately and could read stale."""
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        # Deliberately 403 and not 404, matching signal_ack_api: the caller
        # authenticated fine, it is the ACT that is not available to a
        # non-human, and saying so discloses nothing — an agent already knows
        # it is an agent. Checked before anything else, so an agent token
        # carrying every other capability still stops here.
        return _json(403, {"error": "forbidden: approving an epic is a human act"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    if not issue_id.strip():
        return _json(404, {"error": "not found"})
    # **THE EPIC IS READ BEFORE THE CAPABILITY IS CHECKED, BECAUSE THE CHECK
    # NEEDS ITS PRODUCT.** An epic belongs to one product, so `epic.approve` is
    # a product-scoped write by `authz.NON_SCOPED_ACTIONS`' own reasoning, and
    # an unscoped `authorize(ident, "epic.approve")` returns False for
    # everybody — including an admin holding "*" — because `product=None` is in
    # scope only for the actions listed there. That is deny-by-default working
    # as designed rather than a quirk to route around, so the product is
    # fetched and passed.
    #
    # This is `policy_api`'s "check the caller's right against what the request
    # ACTUALLY RESOLVES TO, never what it claims" rule, arrived at from the
    # other side: there is no caller-supplied product to distrust here, so one
    # scoped check on the epic's own product is the whole of it.
    item = library_client.get_issue_item(issue_id)
    if item is None or item.type != ISSUE_TYPE_EPIC:
        # A missing epic, one outside this credential's scope, and a non-epic
        # item all answer alike — the same no-existence-leak the Library's own
        # read routes use.
        return _json(404, {"error": "not found"})
    if not authorize(ident, "epic.approve", item.product):
        # 404 rather than 403, matching policy_api: a human without the
        # capability, and one scoped to other products, are not told this epic
        # is here to approve.
        return _json(404, {"error": "not found"})
    try:
        approval = library_client.approve_epic(issue_id, acting_human=ident.actor)
    except library_client.EpicApprovalRefused as refused:
        # The epic stays proposed and keeps its control; the page shows this
        # text where the control is. 409 rather than passing the Library's own
        # status through, so the browser has one shape to branch on for every
        # cause — no specification, no live Course, held by another session.
        return _json(409, {"error": refused.message, "approved": False})
    return _json(200, {
        "issue_id": issue_id,
        "approved": True,
        "approved_by": approval.approved_by,
        "acting_human": approval.acting_human,
        "course_id": approval.course_id,
        "position": approval.position,
    })
