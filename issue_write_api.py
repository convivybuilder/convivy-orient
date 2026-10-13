#!/usr/bin/env python3.11
"""The issue queue's two write controls, served again
(issue_01M3J6D09C38T4AT0SGZ20XHDW).

`issue_build` renders a card's resolve buttons, which post to
`POST /api/issues/{id}/resolve`, and the "Log an issue" panel, which posts to
`POST /api/issues`. Commit 9022c01 (2026-08-18) deleted `issue_api` when the
Library became the queue's store, and nothing replaced either route, so both
fell through to `server.handle_post` and answered 404. These proxy the two
acts to the Library's `/items/{id}/resolve` and `POST /items`.

The gate is `approve_api`'s, in its order:

- **A non-human is refused first, with 403.** Agents reach the Library
  directly on their own tokens. This route calls the Library on Orient's
  shared credential, so an agent routed through it would act under Orient's
  name instead of its own.
- **Then `same_origin`, then the capability scoped to the product.** A resolve
  reads the item first to learn its product; a missing item, one outside
  scope, and a human without `issue.resolve` all answer 404. A filing checks
  `issue.create` against the product the body names.
- **The person is the Access-verified email, `ident.actor`**, never a body
  value. A resolve names them as the Library's `owner`. A filing appends them
  to the item's body, because `POST /items` records the credential as
  `created_by` and takes no acting-human field.
- **A Library refusal comes back with its status and its own wording**, which
  the page shows in its toast or note: another session's lease, a resolution
  the vault does not define, an unknown product.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

import library_client
from api_common import _json, api_guard as _api, read_json_body, resolve_identity, same_origin
from authz import authorize

router = APIRouter(prefix="/api")

# A filing's body is free prose; 64 KiB is far past any issue a person types
# into the panel and bounds what one request can make the Library store.
_MAX_BODY = 64 * 1024


def _human_gate(request: Request, act: str):
    """The identity, or the response refusing it: 401, then 403 for a
    non-human, then 403 for a cross-site request."""
    ident = resolve_identity(request)
    if ident is None:
        return None, _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return None, _json(403, {"error": f"forbidden: {act} through Orient is a human act"})
    if not same_origin(request):
        return None, _json(403, {"error": "csrf: origin check failed"})
    return ident, None


@router.post("/issues/{issue_id:path}/resolve")
@_api
async def resolve_issue(issue_id: str, request: Request):
    """Resolve `issue_id` as the calling human. Body:
    `{"resolution": str, "outcome_note": str?, "refs": {"duplicate_of": str}?}`."""
    ident, refused = _human_gate(request, "resolving an issue")
    if refused is not None:
        return refused
    if not issue_id.strip():
        return _json(404, {"error": "not found"})
    item = library_client.get_issue_item(issue_id)
    if item is None or not authorize(ident, "issue.resolve", item.product):
        return _json(404, {"error": "not found"})
    body, refused = await read_json_body(request, _MAX_BODY)
    if refused is not None:
        return refused
    resolution, note, refs = body.get("resolution"), body.get("outcome_note"), body.get("refs")
    if not isinstance(resolution, str) or not resolution:
        return _json(400, {"error": "resolution is required"})
    if note is not None and not isinstance(note, str):
        return _json(400, {"error": "outcome_note must be a string"})
    if refs is not None and not (isinstance(refs, dict) and set(refs) <= {"duplicate_of"}
                                 and all(isinstance(v, str) for v in refs.values())):
        return _json(400, {"error": "refs may carry only duplicate_of, a string"})
    try:
        library_client.resolve_item(issue_id, owner=ident.actor, resolution=resolution,
                                    outcome_note=note, refs=refs)
    except library_client.ItemWriteRefused as refusal:
        return _json(refusal.status, {"error": refusal.message})
    return _json(200, {"issue_id": issue_id, "resolution": resolution})


@router.post("/issues")
@_api
async def create_issue(request: Request):
    """File an issue as the calling human. Body:
    `{"product": str, "title": str, "body": str?, "severity": str?}`. A work
    item carries no type, so a `type` in the body is ignored and never reaches
    the Library, which refuses the retired ones."""
    ident, refused = _human_gate(request, "filing an issue")
    if refused is not None:
        return refused
    body, refused = await read_json_body(request, _MAX_BODY)
    if refused is not None:
        return refused
    fields = {k: body.get(k) for k in ("product", "title", "body", "severity")}
    if any(v is not None and not isinstance(v, str) for v in fields.values()):
        return _json(400, {"error": "product, title, body and severity must be strings"})
    product, title = (fields["product"] or "").strip(), (fields["title"] or "").strip()
    if not product or not title:
        return _json(400, {"error": "product and title are required"})
    if not authorize(ident, "issue.create", product):
        return _json(404, {"error": "not found"})
    prose = (fields["body"] or "").rstrip()
    filed_by = f"— filed by {ident.actor} through team.convivy.com"
    try:
        row = library_client.create_item(
            product=product, title=title,
            body=f"{prose}\n\n{filed_by}" if prose else filed_by,
            severity=fields["severity"] or None)
    except library_client.ItemWriteRefused as refusal:
        return _json(refusal.status, {"error": refusal.message})
    return _json(200, {"id": row.get("id"), "idempotent": bool(row.get("idempotent"))})
