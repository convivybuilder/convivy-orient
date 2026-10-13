#!/usr/bin/env python3.11
"""`POST /api/ledgers/{project}/caps` — the Spending page's ledger-caps form
(issue_01M4BH8KSSMC8XBKB2C2CX9QTF).

The Library's ledger epic reserves a project's cap set — `warn_usd`,
`soft_cap_usd`, `hard_cap_usd` and `per_run_cap_usd` — to a human acting
through an approved client (`PUT /ledgers`, `ledger.grant`, library-protocol
§6.4). Orient had no surface for it; `library_client.list_ledgers` could only
read what Deck or a human operator set by hand against the Library directly.
This route is that surface, and it is the only place in this repo that calls
`library_client.set_ledger_caps`.

The gate follows `model_override_api.py` and `approve_api.py`, same shape:

- **A non-human is refused first, with 403**, before the body is read or the
  product looked up. An agent token holding every other capability still
  stops here — raising a project's own ceiling is exactly the move a worker
  refused at checkout would make if it could reach this route.
- **Then `same_origin`, then `ledger.caps_write` scoped to the project.**
  There is no item to read first (unlike `model_override_api`, a cap belongs
  to a project, not a work item), so the product in the path is the scope
  checked directly. **404, not 403, for a human lacking the capability**,
  matching `approve_api`/`model_override_api`: this is a financial control
  that ends runs and refuses checkouts, not a dashboard preference, so a
  human without `ledger_caps_write` is not told the control exists.
- **All four fields must be sent, each a number or `null`.** `PUT /ledgers`
  replaces the whole cap set, so a body missing one of the four would
  silently clear it; this route refuses a body missing any of them, naming
  which. An extra key is ignored, the same as every other write route here —
  `acting_human` included, see below.
- **`acting_human` is `ident.actor`**, the Access-verified email, never a
  value from the request body — the rule `approve_api` and
  `model_override_api` both state for this field. A body carrying one is not
  an error; the field is ignored.
- **A Library refusal comes back as 409 with the Library's own wording**,
  the way `set_item_model`'s caller shows it: an out-of-order threshold, or a
  value over the Library's ceiling.
"""
from __future__ import annotations

import math

from fastapi import APIRouter, Request

import library_client
from api_common import _json, api_guard as _api, read_json_body, resolve_identity, same_origin
from authz import authorize

router = APIRouter(prefix="/api")

# Four small numbers, one acting-human email; 1 KiB is generous.
_MAX_BODY = 1024

_CAP_FIELDS = ("warn_usd", "soft_cap_usd", "hard_cap_usd", "per_run_cap_usd")


def _number_or_null(body: dict, field: str):
    """`(value, None)` for a null or a finite, non-negative number; `(None,
    error)` otherwise. Mirrors the Library's own `_amount`/`_optional_amount`
    checks (app.py) so a bad value is rejected here with a plain message
    rather than round-tripping to the Library only to come back as the same
    400 with less context."""
    value = body[field]
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"{field} must be a number, or null to clear it"
    if not math.isfinite(value) or value < 0:
        return None, f"{field} must be a non-negative number, or null to clear it"
    return float(value), None


@router.post("/ledgers/{project:path}/caps")
@_api
async def set_ledger_caps(project: str, request: Request):
    """Set `project`'s ledger cap set as the calling human. Body:
    `{"warn_usd": num|null, "soft_cap_usd": num|null, "hard_cap_usd": num|null,
    "per_run_cap_usd": num|null}` — every key required, `null` clearing that
    cap."""
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return _json(403, {"error": "forbidden: setting a ledger cap is a human act"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    if not project.strip():
        return _json(404, {"error": "not found"})
    if not authorize(ident, "ledger.caps_write", project):
        # 404, matching approve_api/model_override_api: a human lacking the
        # capability is not told this control exists.
        return _json(404, {"error": "not found"})
    body, refused = await read_json_body(request, _MAX_BODY)
    if refused is not None:
        return refused
    missing = set(_CAP_FIELDS) - set(body)
    if missing:
        return _json(400, {"error": f"missing {', '.join(sorted(missing))} — warn_usd, "
                                    "soft_cap_usd, hard_cap_usd and per_run_cap_usd must all "
                                    "be sent, null to clear one, or it would be cleared by "
                                    "being left out"})
    values = {}
    for field in _CAP_FIELDS:
        value, err = _number_or_null(body, field)
        if err:
            return _json(400, {"error": err})
        values[field] = value
    try:
        result = library_client.set_ledger_caps(
            project, warn_usd=values["warn_usd"], soft_cap_usd=values["soft_cap_usd"],
            hard_cap_usd=values["hard_cap_usd"], per_run_cap_usd=values["per_run_cap_usd"],
            acting_human=ident.actor)
    except library_client.LedgerCapsRefused as refusal:
        return _json(409, {"error": refusal.message})
    return _json(200, {"project": project, "caps": result.get("caps", {}),
                       "acting_human": ident.actor})
