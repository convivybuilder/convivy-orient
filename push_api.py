#!/usr/bin/env python3.11
"""Web Push subscription API (ARCH-362 Phase 4b).

Three routes, all HUMAN-ONLY and same-origin:

  GET    /api/push/key         the VAPID public key a browser needs to subscribe
  POST   /api/push/subscribe   register (or refresh) this browser
  DELETE /api/push/subscribe   unregister this browser

HUMAN-ONLY, like acknowledgment and for the same reason: a push subscription is
a channel to a PERSON. An agent registering one would be either meaningless or a
way to exfiltrate alert content to wherever an agent-controlled endpoint points,
which is the more serious half.

THE ENDPOINT IS ATTACKER-INFLUENCED. `endpoint` comes from the browser and is a
URL this server will later POST to — the classic SSRF shape. It is therefore
validated against an allow-list of known push services rather than merely
sanity-checked, because "https and not localhost" is not sufficient: an internal
https service on the tailnet would pass that and fail this.
"""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Request
from sqlalchemy import select

import push_send
from api_common import _json, api_guard, read_json_body, resolve_identity, same_origin
from control_db import new_push_id, session
from models import PushSubscription, utcnow

router = APIRouter(prefix="/api/push")

MAX_BODY = 4096
# Hosts we will POST to. Web Push endpoints belong to a small set of vendor
# services; anything else is either a mistake or an SSRF attempt. An allow-list
# rather than a deny-list, because the failure mode of guessing wrong is that
# this server makes an authenticated-looking POST to an attacker's host.
ALLOWED_PUSH_HOSTS = (
    "android.googleapis.com",
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "updates-autopush.stage.mozaws.net",
    "notify.windows.com",
    "push.apple.com",
)


def _endpoint_ok(endpoint: str) -> bool:
    """Is this a URL we are willing to POST to?

    Requires https, the default port, and a host that is (or is a subdomain of)
    a known push service. Subdomain matching is anchored on a dot so
    `fcm.googleapis.com.evil.example` and `notfcm.googleapis.com` both fail.

    Cases that already fail closed and are pinned by tests, so a later
    "simplification" cannot quietly reintroduce them:
      * userinfo — `https://fcm.googleapis.com@evil.example` parses with
        hostname `evil.example`, so the allow-list sees the real host.
      * trailing dot — `fcm.googleapis.com.` is not equal to the allowed host
        and does not end with `.` + it, so it is refused rather than treated as
        the same name.
      * unicode homographs — a Cyrillic lookalike is simply a different string.

    The PORT check is not cosmetic: without it `https://fcm.googleapis.com:22/`
    passes the host test, which turns this into a port-prober against an allowed
    host. Only the default https port is accepted.
    """
    if not isinstance(endpoint, str) or len(endpoint) > 2000:
        return False
    try:
        u = urlparse(endpoint)
        port = u.port                      # raises ValueError on a bad port
    except ValueError:
        return False
    if u.scheme != "https" or not u.hostname:
        return False
    if port not in (None, 443):
        return False
    host = u.hostname.lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_PUSH_HOSTS)


@router.get("/key")
@api_guard
async def get_key(request: Request):
    """The VAPID public key. Human-only: there is no reason for an agent to hold
    it, and keeping the surface uniform means one rule to reason about."""
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return _json(403, {"error": "forbidden: push is a human channel"})
    try:
        return _json(200, {"public_key": push_send.public_key()})
    except push_send.PushUnavailable as exc:
        # A clear 503 rather than silently accepting subscriptions we could never
        # deliver to — a subscribe button that appears to work and never notifies
        # is worse than one that says it is unavailable.
        return _json(503, {"error": f"push unavailable: {exc}"})


@router.post("/subscribe")
@api_guard
async def subscribe(request: Request):
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return _json(403, {"error": "forbidden: push is a human channel"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    payload, err = await read_json_body(request, MAX_BODY)
    if err:
        return err

    endpoint = payload.get("endpoint")
    keys = payload.get("keys") or {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not _endpoint_ok(endpoint):
        return _json(400, {"error": "endpoint is not a recognised push service"})
    if not (isinstance(p256dh, str) and isinstance(auth, str)
            and 0 < len(p256dh) <= 200 and 0 < len(auth) <= 100):
        return _json(400, {"error": "keys.p256dh and keys.auth are required"})

    with session() as s:
        existing = s.scalar(select(PushSubscription)
                            .where(PushSubscription.endpoint == endpoint))
        if existing is not None:
            # Re-subscribing the same browser UPDATES its row. Two things depend
            # on this: the browser rotates its keys periodically, and a row per
            # re-subscribe would deliver the same notification N times.
            #
            # It also reassigns ownership to the caller, which is correct and
            # deliberate: possession of the endpoint means the push service
            # issued it to THIS browser, and a shared machine where a second
            # person signs in must not keep notifying the first.
            existing.identity = ident.actor
            existing.p256dh = p256dh
            existing.auth = auth
            existing.failures = 0
        else:
            s.add(PushSubscription(id=new_push_id(), identity=ident.actor,
                                   endpoint=endpoint, p256dh=p256dh, auth=auth,
                                   created_at=utcnow(), failures=0))
        s.commit()
    return _json(200, {"subscribed": True})


@router.delete("/subscribe")
@api_guard
async def unsubscribe(request: Request):
    """Remove this browser's subscription. Idempotent.

    Scoped to the caller's own rows: an identity may only delete a subscription
    it owns, so knowing someone else's endpoint does not let you silence them.
    """
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return _json(403, {"error": "forbidden: push is a human channel"})
    if not same_origin(request):
        return _json(403, {"error": "csrf: origin check failed"})
    payload, err = await read_json_body(request, MAX_BODY)
    if err:
        return err
    endpoint = payload.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return _json(400, {"error": "endpoint is required"})
    with session() as s:
        row = s.scalar(select(PushSubscription).where(
            PushSubscription.endpoint == endpoint,
            PushSubscription.identity == ident.actor))
        if row is not None:
            s.delete(row)
            s.commit()
    return _json(200, {"subscribed": False})
