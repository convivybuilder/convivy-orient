#!/usr/bin/env python3.11
"""On-demand body fetch and full-text search backing `/reference`
(issue_01M0Y7GZED2THYETHCT8NPHHDS). Keeps its `/api/memory` prefix — an
internal API namespace, separate from the page route it serves — across the
page's own rename from `/memory` (issue_01M1A678RA8CVDNNMZC6777NNA).

`/reference`'s payload used to embed every document's body inline — 94% of a
10.5 MB page across 1158 rows (716 KB reference documents + 442 ARCH log
entries). `engineering_build.render()` now ships a thin payload (~0.5 MB)
with `body` dropped, and these two routes are what the drawer and the search
box call instead:

- `GET /api/memory/documents/{id}/body` — one document's body, fetched when
  the drawer or inline expansion opens. The body source is split
  (`engineering_build.py`'s module docstring for the full accounting): a KB
  document's body lives in the Library (`GET /reference/documents/{id}`); an
  ARCH entry's body is parsed in-process from `substrate/decisions.md` and
  the Library does not serve it at all. This route dispatches on the id's
  own shape — `arch_docs()` mints every ARCH id as `substrate/arch/{anchor}`,
  which no KB id collides with — so one endpoint covers all 1158 rows with
  no client-supplied kind and no extra lookup.
- `GET /api/memory/search?q=` — ids whose BODY text matches `q`, for the
  client to OR into the instant reflabel/id/title/blurb match it already
  runs locally (those three fields stay in the thin payload). The Library's
  BM25 `/reference/search` covers the KB half of the corpus; ARCH bodies are
  scanned in-process, the same substring test the client's own `matchQ` ran
  against `d.body` before this change. **Both halves run on every search, not
  just one** — a route that only covered the Library would find a phrase in
  a KB doc but silently miss the same phrase in an ARCH entry, which is
  exactly the degradation this issue exists to avoid.

Full tier only, like `/reference` itself. `server.py`'s page gate stops a
contractor from ever loading the page that calls these, but a saved link or
a replayed request would reach this router directly — the same posture
`kb_write_api.py` takes for `/reference/doc/{id}`'s write routes, so this
re-checks rather than trusting the page gate alone.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

import engineering_build
import library_client
from access_identity import tier_for
from api_common import _json, api_guard as _api, resolve_identity

router = APIRouter(prefix="/api/memory")

# The human site shows team-audience KB documents and nothing else — the same
# rule `reference_doc_build.py` and `kb_write_api.py` apply. `get_reference_document`
# takes this as a required keyword and returns `None` for anything outside it,
# so a document that exists in the Library but is not team-audience 404s here
# exactly like a missing one.
_AUDIENCE = "team"

# `arch_docs()` (engineering_build.py) mints every ARCH entry's id in this
# shape; no KB id collides with it, so this is a safe dispatch key.
_ARCH_PREFIX = "substrate/arch/"


def _authorized(request: Request):
    """A verified human on the full tier, or the `(None, error-response)`
    pair every route below returns instead. Matches `kb_write_api._authorized`'s
    ordering and status codes: 403 for a non-human identity (an agent bearer
    token has no business reading the human KB browse's on-demand bodies),
    404 — not 401/403 — for a wrong tier, so a contractor-tier identity is not
    told this surface exists at all, same as every full-tier page and per-id
    route in server.py."""
    ident = resolve_identity(request)
    if ident is None:
        return None, _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return None, _json(403, {"error": "only a human may browse the knowledge base"})
    if tier_for(ident.actor) != "full":
        return None, _json(404, {"error": "not found"})
    return ident, None


@router.get("/documents/{doc_id:path}/body")
@_api
async def document_body(doc_id: str, request: Request):
    _ident, err = _authorized(request)
    if err:
        return err
    if doc_id.startswith(_ARCH_PREFIX):
        body = engineering_build.get_arch_body(doc_id)
        if body is None:
            return _json(404, {"error": "no such document"})
        return _json(200, {"body": body})
    try:
        doc = library_client.get_reference_document(doc_id, audience=_AUDIENCE)
    except Exception:
        return _json(503, {"error": "the Library could not be reached; try again shortly"})
    if doc is None:
        return _json(404, {"error": "no such document"})
    return _json(200, {"body": doc.body or ""})


@router.get("/search")
@_api
async def search(request: Request):
    _ident, err = _authorized(request)
    if err:
        return err
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return _json(200, {"ids": []})
    # The Library call is not caught into an empty result: a Library outage
    # must read as "search failed", not as "search found nothing in the KB
    # half of the corpus" — the same silent-degradation failure mode the rest
    # of this issue exists to close (library_client.py's module docstring).
    try:
        kb_ids = [d.id for d in library_client.search_reference(q, audience=_AUDIENCE)]
    except Exception:
        return _json(503, {"error": "the Library could not be reached; try again shortly"})
    arch_ids = engineering_build.search_arch_bodies(q)
    return _json(200, {"ids": kb_ids + arch_ids})
