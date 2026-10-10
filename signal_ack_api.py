"""Signal acknowledgment proxy (ARCH-362 Phase 4a).

Acking a signal is one of the two calls the Library reserves for a verified
human (`signal.ack`, alongside `decision.decide` before the decision plane was
retired) — ADR-0038's approved-client shape. A browser cannot hold that
credential, so this is the proxy: verify the human via Cloudflare Access,
CSRF-check the request, then call the Library under Orient's approved-client
credential, carrying the verified email as `acting_human`.

Orient held its own copy of the acknowledgment table locally until 2026-08-19
(issue_01M08YKNV185DFJ6832GV8ZY22); the identity and CSRF gates below are
ported unchanged from that version. The write itself moved to the Library, and
so did visibility of the SIGNAL CONTENT (browse/search now enforce it, not
this file) — but the ACT of acking still needs its own gate here, because the
write goes out under Orient's approved-client credential, which carries
`signal.customer` at the Library (ADR-0038's `system ∪ {signal.ack,
decision.decide}`). Without `_ack_visible` below, any authenticated human
could ack or unack any signal id in the system, including customer-class rows
outside their product — and use the 200-vs-404 split as an oracle for which
ids exist. Orient's own per-human role, not the Library's credential, is what
scopes this act; ADR-0038 puts that scoping on the client.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

import library_client
from api_common import _json, api_guard, resolve_identity, same_origin
from authz import authorize
from control_db import PRIVACY_CUSTOMER

router = APIRouter(prefix="/api")


def _ack_visible(ident, sig: library_client.SignalRecord | None) -> bool:
    """Same visibility rule GET /signals/{id} used before this table moved:
    out-of-scope, missing, and customer-class-without-the-cap are
    indistinguishable, so acking cannot be used to probe which signal ids
    exist."""
    if not sig or not authorize(ident, "signal.read", sig.product):
        return False
    if sig.privacy_class == PRIVACY_CUSTOMER and not authorize(
            ident, "signal.read_customer", sig.product):
        return False
    return True


@router.post("/signals/{signal_id}/ack")
@api_guard
async def ack_signal(signal_id: str, request: Request):
    """Acknowledge a signal as the calling human. Idempotent."""
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        # Deliberately 403 and not 404: the caller authenticated fine, it is the
        # ACT that is not available to a non-human. Nothing is disclosed by
        # saying so — an agent already knows it is an agent.
        return _json(403, {"error": "forbidden: acknowledgment is a human act"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    if not _ack_visible(ident, library_client.get_signal(signal_id)):
        return _json(404, {"error": "not found"})
    if not library_client.ack_signal(signal_id, acting_human=ident.actor):
        return _json(404, {"error": "not found"})
    return _json(200, {"signal_id": signal_id, "acknowledged": True})


@router.delete("/signals/{signal_id}/ack")
@api_guard
async def unack_signal(signal_id: str, request: Request):
    """Withdraw this identity's acknowledgment. Idempotent.

    Exists because an ack is a claim about attention, and a misfired tap on a
    phone must be reversible — otherwise the safe move is never to ack, and the
    state stops meaning anything.
    """
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return _json(403, {"error": "forbidden: acknowledgment is a human act"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    if not _ack_visible(ident, library_client.get_signal(signal_id)):
        return _json(404, {"error": "not found"})
    if not library_client.unack_signal(signal_id, acting_human=ident.actor):
        return _json(404, {"error": "not found"})
    return _json(200, {"signal_id": signal_id, "acknowledged": False})
