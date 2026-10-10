#!/usr/bin/env python3.11
"""`POST /api/items/{issue_id}/model` — the operator's model-override button
(issue_01M38WVA7XHEDN1NFNPP8KDM0C).

**The operator, 2026-09-23:** the button "changes the model on work already queued,
loudly." The Bosun reads the choice when `agentctl spawn --issue` takes the
item and runs it on that model even on an autonomous dispatch, where a typed
`--model` is refused (platform/contracts/work-item-model-override). So this
write lifts the roster's model pin for one item, and the Library reserves it to
`item.model`, which only an approved client holds.

This is `approve_api` again, gate for gate, because it is the same shape: a
human-reserved Library capability on Orient's credential, a browser that cannot
hold that credential, and the person carried as `acting_human` for the record.

- **A non-human is refused first, with 403**, before the body is read or the
  item looked up, so an agent token holding every capability still stops here.
- **Then `same_origin`, then the capability scoped to the item's own product.**
  The item is read first to learn that product; a missing item, one outside
  scope, and a human without `item_model` all answer 404, as in approve_api.
- **Only a queued item** — `open`, or claimed with the claim expired — takes a
  change. A run already under way keeps the model it started on, so a choice
  set on an item somebody is working would read as applied when it is not.
- **`acting_human` is `ident.actor`**, the Access-verified email, and never a
  value from the request body. A body carrying `acting_human` is not an error;
  the field is ignored.
- **Only a model `agentctl models` lists** can be set, matched on its resolved
  id (issue_01M3NWDHCDQK6PE9NEXFQ37G9K): 400 for anything else, an alias
  included, and 503 for every set while the list is unavailable. Deck's
  free-text field stored `claude-opus`, which a dispatch then refused, and the
  page's select is cosmetic without this check. Clearing needs no list.
- **A Library refusal comes back as 409 with the Library's own wording**, so
  the page renders it where the control is.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

import library_client
import model_catalog
from api_common import _json, api_guard as _api, read_json_body, resolve_identity, same_origin
from authz import authorize
from issue_build import is_queued, model_choice_dict

router = APIRouter(prefix="/api")

# The Library caps model at 64 characters and reason at 300 (library-protocol
# §6.2); 4 KiB holds both with room for JSON escaping.
_MAX_BODY = 4096


@router.post("/items/{issue_id:path}/model")
@_api
async def set_item_model(issue_id: str, request: Request):
    """Set the model `issue_id` runs on, or clear it with `"model": null`, as
    the calling human. Body: `{"model": str | null, "reason": str?}`."""
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return _json(403, {"error": "forbidden: choosing an item's model is a human act"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    if not issue_id.strip():
        return _json(404, {"error": "not found"})
    item = library_client.get_issue_item(issue_id)
    if item is None or not authorize(ident, "item.model", item.product):
        return _json(404, {"error": "not found"})
    body, refused = await read_json_body(request, _MAX_BODY)
    if refused is not None:
        return refused
    if "model" not in body:
        return _json(400, {"error": "model is required: a string, or null to clear"})
    model, reason = body["model"], body.get("reason")
    if not (model is None or isinstance(model, str)) or not (reason is None or isinstance(reason, str)):
        return _json(400, {"error": "model must be a string or null, and reason a string"})
    if not is_queued(item):
        return _json(409, {"error": "only a queued item's model can be changed; "
                                    f"this one is {item.state}"})
    if model is not None:
        catalog = model_catalog.load()
        if not catalog:
            return _json(503, {"error": "the model list is unavailable (agentctl models gave "
                                        "no usable answer), so no model can be set"})
        if model not in {m.model for m in catalog}:
            return _json(400, {"error": "that is not a model agentctl dispatches; "
                                        "choose one from the list"})
    try:
        updated = library_client.set_item_model(
            issue_id, model=model, acting_human=ident.actor, reason=reason)
    except library_client.ItemModelRefused as refusal:
        return _json(409, {"error": refusal.message})
    return _json(200, {"issue_id": issue_id,
                       "model_choice": model_choice_dict(updated.model_choice)})
