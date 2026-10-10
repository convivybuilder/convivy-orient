"""Shared request/identity plumbing for the control-plane write APIs (ARCH-349).

The decision API (WS0.7) and the signal API (S0) are two security-sensitive
surfaces that must resolve identity, verify CF Access, and defend CSRF the SAME
way — a divergence between them is exactly how a fix applied to one and forgotten
on the other becomes a hole. So the identity/auth/CSRF path lives here once, and
both routers call it. One api-tokens.json (gitignored secret) serves both.
"""
from __future__ import annotations

import hmac
import json
import sys
import traceback
from functools import wraps

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import StatementError
from urllib.parse import urlsplit

from access_identity import EMAIL_HEADER, JWT_HEADER, jwt_config, verify_access_jwt
from authz import Identity, agent_identity, human_identity, system_identity
from logutil import clean_log_field
from paths import CONFIG_ROOT

API_TOKENS = CONFIG_ROOT / "api-tokens.json"


def _json(status: int, obj: dict) -> JSONResponse:
    return JSONResponse(obj, status_code=status, headers={"Cache-Control": "no-store"})


def _nonempty(x) -> bool:
    return isinstance(x, str) and bool(x.strip())


def _clip(x, n: int) -> str:
    return x[:n] if isinstance(x, str) else ""


def _iso(value) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None  # stored naive UTC


def _exception_line(exc: BaseException) -> str:
    """The one line of an exception a log may carry: its type and a bounded,
    sanitised message.

    **`str(exc)` is not safe to print for a database error.** SQLAlchemy's
    `StatementError.__str__` (which `DBAPIError` extends) appends the SQL and
    its bound parameters, so `traceback.format_exc()` on a failed
    `push_api.subscribe` writes the push endpoint, `p256dh` and `auth` secret
    into `serve.log`, and a failed room or blog write writes the request text
    or a draft there. The driver's own error (`.orig`, e.g. "database is
    locked") carries the cause without either. A `StatementError` with no
    `.orig` is reduced to its type name for the same reason.

    The fix stops at `StatementError`. `PendingRollbackError` extends
    `InvalidRequestError`, and its message embeds the SQL and parameters, so
    this would print it. No handler in this repo is known to reach one."""
    shown = exc.orig if isinstance(exc, StatementError) else exc
    if shown is None:
        return type(exc).__name__
    return f"{type(shown).__name__}: {clean_log_field(str(shown))[:500]}"


def _log_unhandled(exc: BaseException, a: tuple, k: dict) -> None:
    """Write the exception being handled to stderr, which `serve.sh` sends to
    serve.log, with the method and path of the request that raised it.

    `api_guard` used to answer 500 and log nothing, so the only trace of a
    failed save was a status code in access.log; the cause had to be
    reconstructed by hand (2026-09-18, a `datetime.date` in a document's
    frontmatter). The detail goes here and never into the response body,
    which stays the generic "internal".

    **The frames are printed as they are and the message is sanitised.** A
    frame is a file, a line number and the source line, which are code and
    never the request's data (`traceback.format_tb`, not `format_exc`, which
    would append `str(exc)` unfiltered). The message is the part that can hold
    caller text or a credential, so it is one line from `_exception_line`,
    the same treatment `kb_commit._sanitize` and
    `chart_house_build._log_swallowed` give exception text. serve.log is
    world-readable.

    The request is found among the handler's arguments rather than assumed to
    be keyword `request`. Method and path pass through `clean_log_field`
    because the path is caller-supplied. Logging must not change the
    response, so a failure here is swallowed."""
    try:
        request = next((v for v in (*a, *k.values()) if isinstance(v, Request)), None)
        where = (f"{clean_log_field(request.method)} {clean_log_field(request.url.path)}"
                 if request is not None else "(request not found)")
        frames = "".join(traceback.format_tb(exc.__traceback__))
        print(f"api_guard: unhandled exception in {where}\n"
              f"Traceback (most recent call last):\n{frames}{_exception_line(exc)}",
              file=sys.stderr, flush=True)
    except Exception:
        pass


def api_guard(fn):
    """Any unhandled error becomes a terse 5xx (no stack-trace leak), so a polling
    agent reads it as UNREADABLE and does not act on a misleading 200. Both write
    APIs wrap their routes with this — the trichotomy (ARCH-348) as one decorator.
    The detail stays server-side: see `_log_unhandled`."""
    @wraps(fn)
    async def wrapper(*a, **k):
        try:
            return await fn(*a, **k)
        except Exception as exc:
            _log_unhandled(exc, a, k)
            return _json(500, {"error": "internal"})
    return wrapper


# --- agent bearer tokens ------------------------------------------------------


def api_tokens() -> dict:
    """{bearer-secret: {name, products, kind}} from api-tokens.json. Absent/
    malformed → {} (agent auth off). Only well-formed entries are kept. kind is
    normalized fail-closed: exactly "system" survives (a platform job's token,
    ARCH-352); anything else becomes "agent"."""
    try:
        raw = json.loads(API_TOKENS.read_text())
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for secret, v in raw.items():
        if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"].strip():
            products = frozenset(str(p) for p in v.get("products", []) if isinstance(p, str))
            kind = "system" if v.get("kind") == "system" else "agent"
            out[str(secret)] = {"name": v["name"], "products": products, "kind": kind}
    return out


def _agent_for_token(secret: str) -> dict | None:
    """Constant-time match of a presented bearer secret to its agent, or None."""
    match = None
    for tok, meta in api_tokens().items():
        if hmac.compare_digest(secret, tok):
            match = meta
    return match


def _verified_email(request: Request) -> str | None:
    """The trusted CF Access email, by mode — mirrors server._identity: enforce
    trusts only a verified JWT email, monitor trusts the header. None = no identity."""
    cfg = jwt_config()
    header_email = (request.headers.get(EMAIL_HEADER) or "").strip().lower()
    assertion = request.headers.get(JWT_HEADER) or ""
    jwt_ok, jwt_email = False, ""
    if assertion:
        try:
            jwt_ok, claims, _ = verify_access_jwt(assertion, cfg["team_domain"], cfg["aud"])
            jwt_email = (claims.get("email") or "").strip().lower()
        except Exception:
            jwt_ok, jwt_email = False, ""
    if cfg["enforce"]:
        return jwt_email if (jwt_ok and jwt_email) else None
    return header_email or None


def _note_identity(request: Request, ident: Identity | None, auth: str) -> Identity | None:
    """Record what this request authenticated as, so server.py's access-log
    middleware can write a real identity after the route returns.

    THE MIDDLEWARE CANNOT RESOLVE IDENTITY ITSELF. It runs before the route, so
    doing this there would mean verifying the JWT the route is about to verify
    anyway — and a middleware that skipped the verification to stay cheap would
    log every API call as anonymous, producing a log that is present but useless.
    That is the same "evidence that does not distinguish" defect the logging was
    added to fix (issue_01M1Z882KE85C88GBSB2CS1XJ5), one layer over.

    So the resolution the route already did is stashed here, in the one function
    every router calls, rather than in nine routers that could each forget.
    `request.state` is backed by the ASGI scope dict, which the middleware's
    Request object and the route's Request object share, so a value set inside
    the route is readable from the middleware after `call_next`.

    THE KIND IS LOGGED AND THE ROLE IS NOT, and that is a policy line rather
    than a choice of detail. `authz`'s module docstring says roles.json's
    CONTENTS are access-control data that must never be logged, and
    `human_identity` reads `role` straight out of that file — so a role column
    would make access.log a second, append-only copy of the email-to-role
    mapping, readable by anything with log access rather than by roles.json's
    intended readers. `kind` is not sourced from roles.json at all: it is fixed
    at "human" for a CF Access identity, and comes from which constructor the
    bearer token selected for the other two. It still answers the question a
    reader of this log has, which is what sort of caller this was.

    Returns `ident` so the caller can stash and return in one expression."""
    request.state.log_actor = ident.actor if ident else ""
    request.state.log_kind = ident.kind if ident else "-"
    request.state.log_auth = auth
    return ident


def resolve_identity(request: Request) -> Identity | None:
    """A Bearer token → an agent identity (or a system identity when its
    api-tokens.json entry says "kind": "system" — the capture pollers, which
    need signal_customer; ARCH-352); otherwise the verified CF Access email →
    a human identity. None when neither authenticates. Fail-closed: any kind
    value other than the exact string "system" resolves as agent.

    Every outcome is also stashed for the access log — see `_note_identity`."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        meta = _agent_for_token(auth[len("Bearer "):].strip())
        if not meta:
            # A presented-but-unrecognised token is worth distinguishing in the
            # log from no credential at all: it is what a revoked or mistyped
            # agent token looks like, and the two have identical 403 bodies.
            return _note_identity(request, None, "auth=bearer-unknown")
        make = system_identity if meta.get("kind") == "system" else agent_identity
        return _note_identity(request, make(meta["name"], meta["products"]), "auth=bearer")
    email = _verified_email(request)
    ident = human_identity(email) if email else None
    return _note_identity(request, ident, "auth=cf-access" if ident else "auth=none")


def same_origin(request: Request) -> bool:
    """CSRF defence for cookie-authed (human) state changes: the Origin (or
    Referer) host must match the request host. A cross-site POST riding the
    ambient CF Access cookie fails this. Bearer-authed agents are exempt (their
    auth is not ambient). A cookie POST with neither header is rejected.

    A malformed header (e.g. an unbalanced IPv6-literal bracket) makes
    `urlsplit` raise `ValueError` instead of just failing the netloc compare
    — this is the CSRF check, so that must fail closed (reject) rather than
    crash the endpoint, and must not fall through to the next header, which
    would let a second, attacker-supplied header decide the outcome instead."""
    host = request.headers.get("host", "")
    for h in ("origin", "referer"):
        val = request.headers.get(h)
        if val:
            try:
                return urlsplit(val).netloc == host
            except ValueError:
                return False
    return False


async def read_json_body(request: Request, max_body: int) -> tuple[dict | None, JSONResponse | None]:
    """Read + parse a bounded JSON object body, or return an error response. The
    cap is per-surface (a decision bundle and an ingested email are very different
    sizes), so the caller passes its own limit."""
    raw = await request.body()
    if len(raw) > max_body:
        return None, _json(413, {"error": "payload too large"})
    try:
        obj = json.loads(raw or b"{}")
    except ValueError:
        return None, _json(400, {"error": "invalid json"})
    if not isinstance(obj, dict):
        return None, _json(400, {"error": "expected a json object"})
    return obj, None
