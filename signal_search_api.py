#!/usr/bin/env python3.11
"""The Archives tail's two read routes: browse-by-shape and text search
(issue naming the epic: "Archives gets its tail access").

`/archives` shows a recent-60 window and Archives gets its own PRs/Changelogs
sections (archives_build.py) — a few known shapes, browsable by a click.
Everything else stays reachable only by these two routes, which the page's
client-side JS calls: `GET /signals/browse?kind=` (exact, server-filters the
whole table) and `GET /signals/search?q=` (approximate, ranks a retrieved
window). Full tier only, like `/api/memory`'s routes, whose `_authorized`
shape this copies — a saved link or a replayed request must re-check rather
than trust the page gate alone.

**Both routes call `library_client` under Orient's AGENT credential (the
plain, non-`human_reserved` one), the same credential every other signals
read already uses.** That credential is verified at process start to lack
`signal.customer` (`library_client.FORBIDDEN_AGENT_CAPABILITY`), which is
what makes `/archives`'s existing browse safe to render server-side with no
per-viewer check of its own (ADR-0022 gives that wall to the Library). Using
the SAME credential here closes the gap `signals_build.py`'s disabled search
box named: that placeholder worried about the APPROVED-CLIENT credential,
which does hold `signal.customer` (ADR-0038) and would need a per-viewer wall
this repo does not have. Nothing here reaches for that credential, so no such
wall is needed — customer-class rows are excluded the same structural way
browse already excludes them, not by a check in this file.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

import archives_build
import crew_registry
import library_client
from access_identity import tier_for
from api_common import _iso, _json, api_guard as _api, resolve_identity

router = APIRouter(prefix="/api/signals")

# A shape's own rendered list is capped; the count next to it is never derived
# from this cap — it comes from `signal_facets`, a true total over the whole
# matching set. See `browse`'s docstring.
_BROWSE_CAP = 100
_SEARCH_CAP = 30


def _authorized(request: Request):
    """A verified human on the full tier, or the `(None, error-response)`
    pair every route below returns instead — `memory_api._authorized`'s exact
    shape: 401 with no identity, 403 for a non-human (an agent has no browser
    session to call this from), 404 — not 403 — for the wrong tier, so a
    contractor-tier identity is not told this surface exists at all, matching
    every full-tier page and per-id route in server.py."""
    ident = resolve_identity(request)
    if ident is None:
        return None, _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return None, _json(403, {"error": "only a human may browse the Archives"})
    if tier_for(ident.actor) != "full":
        return None, _json(404, {"error": "not found"})
    return ident, None


def _signal_json(sig: library_client.SignalRecord, *, distance: float | None = None) -> dict:
    """`sig.body` is already stripped of any leading `agentctl-review` stamp —
    `library_client` does that once, for every `SignalRecord` it hands back,
    rather than leaving each JSON producer (this one included) to remember to
    call a stripper of its own (`SignalRecord.body`'s own docstring)."""
    d = {
        "id": sig.id, "source": sig.source, "stream": sig.stream, "kind": sig.kind,
        "actor": sig.actor, "product": sig.product, "body": sig.body, "refs": sig.refs,
        "privacy_class": sig.privacy_class,
        "produced_at": _iso(sig.produced_at), "ingested_at": _iso(sig.ingested_at),
    }
    if distance is not None:
        d["distance"] = distance
    return d


@router.get("/browse")
@_api
async def browse(request: Request):
    """Every row of one `kind`, exactly — the complete path. `kind` is free
    text passed straight through to `library_client.list_signals`: nothing
    here validates it against a known-kind list, which is what makes a kind
    this page has never heard of reachable the moment a row of it exists.

    **Restricted to the viewer's own project scope** (E19·4,
    issue_01M35K5JQ7Z7MHYZGWRSB5F85M): the first pass of this epic scoped
    `/archives` itself but found this route — its own client-side JS calls
    it for every shape beyond the three `/archives` browses natively — still
    fanning out over the whole store. A scoped (non-whole-fleet) viewer's
    fetch is restricted to their own projects, the same per-product fan-out
    `archives_build.fan_out` gives `/archives/prs` and `/archives/changelogs`,
    rather than one flat `list_signals(kind=...)` call an out-of-scope
    product's rows could crowd a shared cap out of (that module's own
    docstring). `total` is `signal_facets(kind=[kind]).total`, a true
    whole-store count, ONLY for a whole-fleet viewer; a scoped viewer's total
    is `len(signals)` instead — narrower, matching the same rule
    `signals_build.live_html` already applies to `/archives`'s own header
    count, since `signal_facets` takes no multi-project filter to recompute
    a true scoped total from."""
    ident, err = _authorized(request)
    if err:
        return err
    kind = (request.query_params.get("kind") or "").strip()
    if not kind:
        return _json(400, {"error": "kind is required"})
    whole_fleet = crew_registry.covers_every_project(ident.products)
    try:
        if whole_fleet:
            rows = library_client.list_signals(kind=[kind], cap=_BROWSE_CAP)
            total = library_client.signal_facets(kind=[kind]).total
        else:
            rows = archives_build.fan_out(kind, cap_per_product=_BROWSE_CAP,
                                          products=sorted(ident.products))
            total = len(rows)
    except Exception:
        return _json(503, {"error": "the Library could not be reached; try again shortly"})
    return _json(200, {"kind": kind, "total": total, "cap": _BROWSE_CAP,
                       "signals": [_signal_json(s) for s in rows]})


@router.get("/search")
@_api
async def search(request: Request):
    """The N closest matches to `q` — the approximate path. Never a total:
    `library_client.search_signals` reports no whole-store count, by design
    (its own docstring), and this route passes `count`/`truncated` through
    unchanged rather than relabeling either as one. No `kind` parameter is
    accepted or forwarded — the Library refuses it 400 on this route, and
    offering it here would produce an error a reader cannot act on. `source`/
    `product` are likewise not accepted as a QUERY parameter: both would
    narrow the retrieved window rather than the store, the same defect `kind`
    has, and the page this route serves keeps search a pure text lookup
    rather than offering a control that looks like `/browse`'s but means
    something weaker.

    **A scoped (non-whole-fleet) viewer's results ARE filtered to their own
    project scope** (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) — post-fetch,
    since `library_client.search_signals` takes no product pre-filter at
    all (unlike `list_signals`'s `product=`), so there is no pre-filter to
    ask the Library for. A scoped viewer's `count` is `len(signals)`, never
    the Library's count for the unfiltered window: the gap between the two
    would tell a viewer that a project outside their scope holds rows
    matching their query (PR #375 security review, round 2). `truncated`
    stays the Library's own flag, since a nearest-neighbour window over a
    store larger than the cap comes back truncated whatever the query, so it
    says nothing about which projects matched."""
    ident, err = _authorized(request)
    if err:
        return err
    q = (request.query_params.get("q") or "").strip()
    if not q:
        # Matches memory_api.search's convention: an empty query is not a
        # library call to make, so answer the empty case directly rather than
        # asking the Library to refuse it.
        return _json(200, {"count": 0, "truncated": False, "signals": []})
    whole_fleet = crew_registry.covers_every_project(ident.products)
    try:
        resp = library_client.search_signals(q, cap=_SEARCH_CAP)
    except Exception:
        return _json(503, {"error": "the Library could not be reached; try again shortly"})
    results = resp.results if whole_fleet else [
        r for r in resp.results if r.signal.product in ident.products]
    count = resp.count if whole_fleet else len(results)
    return _json(200, {"count": count, "truncated": resp.truncated,
                       "signals": [_signal_json(r.signal, distance=r.distance)
                                  for r in results]})
