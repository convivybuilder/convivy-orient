"""KB-index reads for the Reading Room (ARCH-357), through the Library
(issue_01M1MFM06D7EK15RV5QB57PCVK) rather than a local index file.

The room's GRANT — the set of KB docs a member may see — is computed here, live
from the Library + the identity wall, never stored. Two layers, kept distinct
because conflating them is the leak vector (design §3), and the move to the
Library does not change which layer does which job:

  - `team_doc_meta()` / `team_docs()` read every team-audience doc, current or
    draft, through `library_client`. Both delegate the AUDIENCE half of the
    gate to `library_client.list_reference_documents`/`get_reference_document`
    (`audience="team"`), which apply the Library's own `_entitled` — the
    isinstance-guarded membership test that keeps `audience in aud` from
    becoming a substring match on a bare string (PR #205 review). This module
    does not reimplement that check; it only asserts the STATUS half itself
    (`_STATUSES` below), because nothing in the client's contract promises
    the Library only ever serves current/draft rows, the same reasoning
    `project_build._BEAD_STATUSES` documents (PR #263 review).

    **The two are split on whether a consumer needs a body** (PR #264 round 2
    review). `team_doc_meta()` is one `list_reference_documents` call, no
    per-doc fetch — everything an id-only or metadata-only consumer needs.
    `team_docs()` additionally fetches every entitled doc's own body, one
    Library round trip per doc, for a consumer that actually embeds it. A
    consumer that only reduces the result to a set of ids (`room_entitlement`)
    must never route through the body-fetching path — doing so was paying for
    373 network fetches to throw away everything but the id on every room
    create and every room add.
  - `entitled_docs(email, products)` / `entitled_doc_ids(email, products)` /
    `room_entitlement(email, products)` apply the IDENTITY rule:
        full tier       → every team-audience doc
        contractor tier → contractor-audience docs whose project is in
                          `products`
    reusing tier_for (tiers.json); `products` is the caller's own
    Library-resolved scope (`library_client.person_scope`, E19·1
    issue_01M35K54QHJYYPXWS28YWEHJP3) — this module never resolves it and
    never reads roles.json, so there is no materialized grant to drift
    against those files. This half is NOT the Library's reference-document
    concern — `library_client._entitled` answers "may this AUDIENCE see this
    document", never "what is this EMAIL granted" — and must not be rebuilt
    against, or replaced by, the client's gate.

Body cleaning (frontmatter + hidden-section strip) reuses build._clean_body so
a doc renders identically here and everywhere else on the site.

**Fails closed, and a failure is not an empty grant.** The old sqlite read
returned `[]` for a missing/locked index; a Library call raises instead, and
nothing here catches it. An unreachable Library and "this member is entitled
to nothing" are different facts — the first is an outage, the second is a
real grant — and collapsing them would let a write-time validator
(`room_api.py`) or a render (`room_build.py`) treat a service blip as "this
member may see nothing," which for `room_api.py`'s provisioning routes could
also read as "revoke everything" if a caller mistook the empty result for
authoritative. Every caller of this module already has, or now needs, an
answer for that raise — see `entitled_docs`/`room_entitlement`'s own
docstrings for how it propagates, and each consumer's own module for how it
is handled: `room_build.live_html` has no static fallback and lets it 500
(server.py); `room_api.py`'s write routes are wrapped in `api_common.api_guard`,
which turns any unhandled exception into a terse 500 rather than completing a
write against a partial or empty grant.
"""
from __future__ import annotations

import library_client
from access_identity import tier_for
from build import _clean_body


# The human site's audience gate — the rule `build.load_docs`,
# `project_build` and `reference_doc_build` already apply, named here rather
# than passed as a literal so the one place this module decides who it serves
# is greppable (matches those modules' own `_AUDIENCE`).
_AUDIENCE = "team"

# The statuses this module serves, ASSERTED here rather than inherited from
# whatever `GET /reference/documents` happens to serve by default. The old
# SQL said `WHERE status IN ('current','draft')` outright; the Library
# applies the same two today (measured against the live service: 338
# current, 35 draft, nothing else), but nothing in its client's contract
# promises it — a widened server default must not silently widen who the
# Reading Room hands documents to, with no error and no failing test
# (`project_build._BEAD_STATUSES`, PR #263 review).
_STATUSES = ("current", "draft")


def team_doc_meta() -> list[dict]:
    """Every team-audience, current/draft doc's METADATA, read through the
    Library with no per-doc fetch. Each dict: id, project, kind, title,
    status, contractor (bool) — everything an id-only or metadata-only
    consumer needs, and nothing this costs more than one `list_reference_documents`
    call regardless of corpus size (contrast `team_docs()` below, one Library
    round trip per doc).

    Raises on any Library failure — see the module docstring for why nothing
    here catches it, unlike the old sqlite read this replaces."""
    docs = [{
        "id": m.id, "project": m.project, "kind": m.kind, "title": m.title,
        "status": m.status,
        # Default-deny: contractor-visible only if explicitly opted in
        # (ARCH-228). "contractor" always co-occurs with "team".
        "contractor": "contractor" in (m.audience or []),
    } for m in library_client.list_reference_documents(audience=_AUDIENCE)
      if m.status in _STATUSES]
    # The Library's listing order is not guaranteed; match the old SQL's
    # `ORDER BY project, kind, title` so consumers don't see it reshuffle.
    docs.sort(key=lambda d: (d["project"], d["kind"], d["title"]))
    return docs


def team_docs() -> list[dict]:
    """Every team-audience, current/draft doc, cleaned for display, read
    through the Library. Each dict: id, project, kind, title, status, body,
    contractor (bool).

    **Fetches every entitled doc's own body.** `list_reference_documents`
    omits it — the listing route is metadata-only — and a consumer that
    actually embeds a doc's full body, not just a preview, needs its own
    per-doc fetch (`build.load_docs`'s shape, not `project_build`'s
    metadata-only listing). Measured against the live Library on
    2026-09-03: 373 team-audience current/draft documents, listing (~0.45s)
    plus all 373 body fetches (~0.43s), ~0.88s total. Call `team_doc_meta()`
    instead if the consumer only needs ids or metadata — `room_entitlement`
    used to route through this function and pay the full body-fetch cost on
    every room create and every room add to throw every body away but the id
    (PR #264 round 2 review); it no longer does.

    Raises on any Library failure — see the module docstring for why nothing
    here catches it, unlike the old sqlite read this replaces."""
    docs = []
    for meta in library_client.list_reference_documents(audience=_AUDIENCE):
        if meta.status not in _STATUSES:
            continue
        full = library_client.get_reference_document(meta.id, audience=_AUDIENCE)
        # `full.status`, not `meta.status`, is the authoritative check: the
        # listing and the per-doc fetch are two separate network calls, so a
        # document can move out of _STATUSES in the window between them (up
        # to the length of the whole loop, worse for a doc fetched later) —
        # checking only the listing snapshot above would serve a doc carrying
        # its new, excluded status with no filter ever applied to the copy
        # actually returned (PR #264 round 2 review). The check above stays
        # as a cheap early skip; it must never be the only one.
        if full is None or full.body is None or full.status not in _STATUSES:
            # Entitlement, status, or existence changed between the list and
            # the fetch (a doc edited or re-tagged mid-request) — drop the
            # row rather than raise over one stale reference.
            continue
        docs.append({
            "id": full.id, "project": full.project, "kind": full.kind,
            "title": full.title, "status": full.status,
            "body": _clean_body(full.body),
            "contractor": "contractor" in (full.audience or []),
        })
    # The Library's listing order is not guaranteed; match the old SQL's
    # `ORDER BY project, kind, title` so consumers don't see it reshuffle.
    docs.sort(key=lambda d: (d["project"], d["kind"], d["title"]))
    return docs


def _by_identity(email: str, docs: list[dict], products: frozenset[str]) -> list[dict]:
    """The IDENTITY half of the grant, applied to whichever doc list a caller
    already has (`team_docs()`'s bodies or `team_doc_meta()`'s metadata —
    both dicts carry `project` and `contractor`, so the rule is the same
    either way). Full tier sees every doc; a contractor sees only
    contractor-audience docs whose project is in `products`, the member's own
    Library-resolved scope ("*" = all — the Library's own answer for an
    administrator, never something this module decides)."""
    if tier_for(email) == "full":
        return docs
    all_products = "*" in products
    return [d for d in docs if d["contractor"] and (all_products or d["project"] in products)]


def entitled_docs(email: str, products: frozenset[str]) -> list[dict]:
    """The docs a member MAY see, with bodies, by the identity rule. Full
    tier sees every team-audience doc; a contractor sees only
    contractor-audience docs whose project is in `products`, the member's own
    Library-resolved scope ("*" = all).

    Raises exactly when `team_docs()` does — the identity rule adds no I/O of
    its own, so there is nothing here to catch that isn't already the
    Library's own failure."""
    return _by_identity(email, team_docs(), products)


def entitled_doc_ids(email: str, products: frozenset[str]) -> list[dict]:
    """The docs a member MAY see, metadata only — same identity rule as
    `entitled_docs()`, applied to `team_doc_meta()` instead of `team_docs()`,
    for a consumer that only needs ids (`room_entitlement` below).

    Raises exactly when `team_doc_meta()` does."""
    return _by_identity(email, team_doc_meta(), products)


def room_entitlement(email: str, products: frozenset[str]) -> frozenset[str]:
    """The set of doc ids a member may see — the wall a room render and the
    provisioning/curation API validate every entry against. Metadata-only:
    no body is ever fetched to compute this (PR #264 round 2 review).

    Raises exactly when `entitled_doc_ids()` does."""
    return frozenset(d["id"] for d in entitled_doc_ids(email, products))
