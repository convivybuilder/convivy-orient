#!/usr/bin/env python3.11
"""`POST /api/alerts-policy` — the dashboard alert-threshold write route
(issue_01M17W6XGXVHNDWCFRTQ38Y2RK).

Settles the question the item's own body raised: whether Orient holds this
policy at all, or whether it belongs to the Library the way the deny-list
epic's policy table does. The deny-list belongs to the Library because the
Library's OWN clients evaluate it. Here the evaluator is Orient's own code —
`code_health_build.ci_status`, `dashboard_build.overage_status` and
`kb_build.cls_recall` are called from inside this same repo, by both a source
dashboard's own render and `alerts_build.py`'s /alerts feed — so nothing
outside Orient ever reads or writes a threshold. `control_db` already holds
exactly this shape of Orient-only state (the Reading Room, the blog store),
so this is a fourth table there, not a reach into the Library.

Same posture as `policy_api.py` and `room_api.py`, matching every other
control-plane write surface: human identity only (an alert threshold is what
Orient itself calls "in policy" — an automated caller choosing its own bar
would defeat the point of having one), CSRF via `same_origin` (room_api.py's
pattern), and this route's OWN capability (`alerts_policy_write`) checked
before the body is read. Unlike `policy_api.py`'s deny-list route, a missing
capability here answers plain 403 rather than 404 — `room_api.py`'s own
convention — because this is not a security-sensitive surface warranting the
"don't even reveal it exists" treatment; it changes what a dashboard alerts
on, not what an agent may do.

**No second copy of a threshold.** Every write goes through
`alert_policy_store.write`, which is also what every evaluator's `get()` call
reads — the same store on both sides of the write is the whole invariant this
item exists to hold.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

import alert_policy_store
from api_common import _json, api_guard as _api, read_json_body, resolve_identity, same_origin
from authz import authorize

router = APIRouter(prefix="/api")

# One key, up to two small numeric fields — generous relative to any actual
# payload (largest today is {"key": ..., "good_min": 0.9, "warn_min": 0.7}).
MAX_BODY = 1024


@router.post("/alerts-policy")
@_api
async def write_alerts_policy(request: Request):
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return _json(403, {"error": "only a human may set an alert threshold"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    if not authorize(ident, "alerts.write", None):
        return _json(403, {"error": "forbidden"})

    payload, err = await read_json_body(request, MAX_BODY)
    if err:
        return err
    key = payload.get("key")
    if not isinstance(key, str) or key not in alert_policy_store.DEFAULTS:
        return _json(400, {"error": "unknown alert-policy key", "key": key})

    values = {name: payload.get(name) for name in alert_policy_store.FIELDS[key]}
    why = alert_policy_store.validate(key, values)
    if why:
        return _json(400, {"error": why})

    result = alert_policy_store.write(key, values, actor=ident.actor)
    return _json(200, {"key": key, **result, "updated_by": ident.actor})
