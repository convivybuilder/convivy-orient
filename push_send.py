#!/usr/bin/env python3.11
"""Web Push delivery (ARCH-362 Phase 4b) — VAPID keys, encryption, and the send.

This is the half of the Phase 4 gate that makes an alert REACH the operator. Acknowledgment
(4a) records that they have seen one; this tells them there is something to see while
they are away from a browser. The plan's acceptance test is exactly that question —
"does this alert still reach the operator when they are away from a browser?" — and until it
answers yes, no alert-class Slack post may be retired.

WHY NOT pywebpush
=================
It pulls a whole async HTTP stack (aiohttp and seven friends) to perform one HTTP
POST, taking the dependency addition from 5 packages to 14 on a security-surface
app whose pinning policy exists to keep that surface small. We take only the
crypto that genuinely cannot be hand-rolled — `py_vapid` signs the VAPID JWT,
`http_ece` does RFC-8188 payload encryption — and send the POST with stdlib
urllib. Same instinct as `github_capture.py`, which is stdlib-only rather than
pulling in a client SDK.

KEY POSTURE
===========
The VAPID private key is the ability to sign as us. It lives in
~/.orient-control/vapid.json at 0600 — NEVER in the control DB, so a database
read cannot yield it, and never in the repo. Same posture as the capture token.
The keypair is generated on first use and then stable: rotating it invalidates
every existing browser subscription, so it is deliberately not automatic.

The `p256dh`/`auth` values stored per subscription are the BROWSER's public
material, not ours. They let us encrypt a payload only that browser can read.

FAILURE POSTURE
===============
Sending is BEST-EFFORT and must never break the thing that triggered it. An
alert being captured is the important event; failing to push it is a degraded
notification, not a reason to fail the capture. So every send is wrapped, and a
404/410 (the push service saying the browser discarded this subscription) prunes
the row rather than retrying forever.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from sqlalchemy import select

from control_db import session
from models import PushSubscription, utcnow
from owner_only_paths import ensure_owner_only_dir

# Same directory as the capture token — outside the repo, user-owned.
CONTROL_DIR = Path.home() / ".orient-control"
VAPID_FILE = CONTROL_DIR / "vapid.json"

# The `sub` claim in the VAPID JWT: a contact the push service can reach if our
# sending misbehaves. Required by the spec to be a mailto: or https: URL. Each
# deployment names its own operator, so it is read from vapid.json's "subject"
# beside the keys, and this placeholder is used only when that is unset.
VAPID_SUBJECT = "mailto:operator@example.com"


def vapid_subject(keys: dict) -> str:
    """vapid.json's "subject" when it is a mailto: or https: URL, else the
    placeholder. A malformed value falls back rather than failing every send."""
    from urllib.parse import urlsplit

    subject = keys.get("subject")
    if isinstance(subject, str) and urlsplit(subject).scheme in ("mailto", "https"):
        return subject
    return VAPID_SUBJECT

_TIMEOUT = 10
# A push service holds a notification this long if the device is offline. A day
# is right for an operational alert: useful when a phone comes back, stale after.
_TTL = 86400
# Prune a subscription after this many consecutive failures that were NOT an
# explicit 404/410. Guards against a subscription that errors forever without
# the push service ever telling us it is gone.
_MAX_FAILURES = 10


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects on the outbound push POST.

    THIS IS A SECURITY CONTROL, not a nicety. `endpoint` is validated against an
    allow-list of push-service hosts before it is stored — but `urlopen` follows
    redirects by DEFAULT, so without this the allow-list would only guard the
    FIRST hop. An allowed host answering `302 Location: http://169.254.169.254/…`
    (or any internal address) would be followed, and a 307/308 preserves the POST
    method and body, so the encrypted payload and our signed VAPID header would
    be delivered to whatever the redirect named.

    A real push service does not redirect. Treating a redirect as a failure is
    both correct and the safe default.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


class PushUnavailable(RuntimeError):
    """Raised when push cannot be used at all (no keys, missing crypto deps).

    Distinct from a per-subscription delivery failure: this means the FEATURE is
    unavailable, which the API surfaces as a clear 503 rather than silently
    accepting subscriptions it can never deliver to.
    """


def _b64(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def load_or_create_keys() -> dict:
    """The VAPID keypair, generated on first use and stable thereafter.

    Written 0600 with the directory 0700. Rotation is deliberately manual: a new
    keypair invalidates every existing browser subscription, so it must be a
    decision, never a side effect of a deploy or a missing file at the wrong
    moment.
    """
    if VAPID_FILE.exists():
        try:
            data = json.loads(VAPID_FILE.read_text())
            if data.get("private_key") and data.get("public_key"):
                return data
        except (OSError, ValueError) as exc:
            raise PushUnavailable(f"VAPID key file unreadable: {exc}") from exc

    try:
        from py_vapid import Vapid01
    except ImportError as exc:                                   # pragma: no cover
        raise PushUnavailable(f"py_vapid not installed: {exc}") from exc

    v = Vapid01()
    v.generate_keys()
    from cryptography.hazmat.primitives import serialization
    priv_pem = v.private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode("ascii")
    pub = v.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint)
    data = {"private_key": priv_pem, "public_key": _b64(pub)}

    ensure_owner_only_dir(CONTROL_DIR)
    # Write via a 0600 file descriptor rather than write_text-then-chmod: the
    # latter leaves a window where the private key is world-readable.
    fd = os.open(VAPID_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    return data


def public_key() -> str:
    """The base64url public key the browser needs to subscribe. Safe to serve."""
    return load_or_create_keys()["public_key"]


def _encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    import base64

    import http_ece
    from cryptography.hazmat.primitives.asymmetric import ec

    def _unb64(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    server_key = ec.generate_private_key(ec.SECP256R1())
    return http_ece.encrypt(
        payload, private_key=server_key,
        dh=_unb64(p256dh), auth_secret=_unb64(auth), version="aes128gcm")


def _vapid_header(endpoint: str) -> dict:
    from urllib.parse import urlparse

    from py_vapid import Vapid01
    keys = load_or_create_keys()
    v = Vapid01.from_pem(keys["private_key"].encode("ascii"))
    parts = urlparse(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "sub": vapid_subject(keys)}
    return v.sign(claims)


def send_one(sub: PushSubscription, payload: dict) -> tuple[bool, int | None]:
    """Deliver to ONE subscription. Returns (ok, http_status).

    Never raises for a delivery problem — the caller is usually a capture path,
    and a failed notification must not fail the capture.
    """
    try:
        body = _encrypt(json.dumps(payload).encode("utf-8"), sub.p256dh, sub.auth)
        headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                   "TTL": str(_TTL), "Urgency": "normal"}
        headers.update(_vapid_header(sub.endpoint))
        req = urllib.request.Request(sub.endpoint, data=body, headers=headers, method="POST")
        # _OPENER, not urlopen: the module opener refuses redirects, without which
        # the endpoint allow-list would only guard the first hop. See _NoRedirects.
        with _OPENER.open(req, timeout=_TIMEOUT) as resp:
            return (200 <= resp.status < 300), resp.status
    except urllib.error.HTTPError as exc:
        return False, exc.code
    except Exception:                                            # noqa: BLE001
        return False, None


def notify(identity: str, payload: dict) -> int:
    """Push `payload` to every subscription belonging to `identity`.

    Returns the number delivered. Best-effort throughout: a caller that cannot
    push must still succeed at whatever it was actually doing.

    A 404 or 410 means the push service is telling us the browser discarded this
    subscription — that is authoritative, so the row is deleted rather than
    retried. Other failures increment a counter and prune only after
    _MAX_FAILURES, so one transient outage does not unsubscribe a live device.
    """
    delivered = 0
    try:
        with session() as s:
            subs = list(s.scalars(select(PushSubscription)
                                  .where(PushSubscription.identity == identity)))
            for sub in subs:
                ok, status = send_one(sub, payload)
                if ok:
                    sub.last_ok_at = utcnow()
                    sub.failures = 0
                    delivered += 1
                elif status in (404, 410):
                    s.delete(sub)
                else:
                    sub.failures = (sub.failures or 0) + 1
                    if sub.failures >= _MAX_FAILURES:
                        s.delete(sub)
            s.commit()
    except Exception:                                            # noqa: BLE001
        return delivered
    return delivered


def notify_alert(sig: dict) -> int:
    """Push an alert-class signal to everyone entitled to see it.

    Takes a PLAIN DICT, not the ORM object. It is called through a threadpool
    while the capture session is still open, and reading a SQLAlchemy attribute
    from another thread is a lazy load against a session that does not belong to
    it. Passing the ORM object happened to work only because `_signal_dict()`
    runs immediately before and pulls the fields into the instance dict — a
    correctness that depends entirely on statement order and would break silently
    on a reorder. A dict makes the dependency explicit and survives that edit.
    Raised by the Phase 4b reviewer as the last fragile assumption in the change.

    Scoped by the SAME authz the pages use, re-derived per identity rather than
    broadcast: a push notification is a disclosure channel, and it must not
    become the one place where an alert reaches someone the Alerts page would
    not have shown it to.
    """
    from authz import authorize, human_identity
    from control_db import PRIVACY_CUSTOMER
    sig_id = sig.get("id") or ""
    product = sig.get("product") or ""
    privacy = sig.get("privacy_class")
    body = (sig.get("body") or "").strip().replace("\n", " ")
    payload = {"title": "Convivy alert",
               "body": (body[:180] + " …") if len(body) > 180 else (body or "New alert"),
               "url": "/engineering/alerts", "signal_id": sig_id}
    sent = 0
    try:
        with session() as s:
            idents = list(s.scalars(select(PushSubscription.identity).distinct()))
        for ident_email in idents:
            try:
                ident = human_identity(ident_email)
                if ident is None or not authorize(ident, "signal.read", product):
                    continue
                # The customer wall, checked HERE and not inherited from the page.
                # Today every human role holding signal_read also holds
                # signal_customer, so this changes no current behaviour — which is
                # exactly why it is worth writing down. Without it, adding a future
                # role with signal_read and without signal_customer would silently
                # widen push into a disclosure channel broader than the Alerts page
                # it mirrors, and nothing would fail. A service-layer gap guarded
                # only by today's role table is one role away from being real
                # (ARCH-322). Found by the Phase 4b security review.
                if (privacy == PRIVACY_CUSTOMER
                        and not authorize(ident, "signal.read_customer", product)):
                    continue
            except Exception:                                    # noqa: BLE001
                continue
            sent += notify(ident_email, payload)
    except Exception:                                            # noqa: BLE001
        return sent
    return sent
