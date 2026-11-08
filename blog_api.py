#!/usr/bin/env python3.11
"""Blog authoring on the team site, with the author ATTESTED rather than declared
(ARCH-363).

WHY THIS EXISTS, and it is about integrity rather than convenience. A lab post
carries `author:` and `authorship: human | llm | collab`, and the authorship mark
is enforced at build time — a page without one fails to publish. But both fields
are hand-typed, and `lab-blog publish` even DEFAULTS `authorship` to "human". So
the lab's public promise, that a reader can tell at a glance who wrote a page,
currently rests on the writer remembering to declare it honestly and on nobody
having lied. The CLI cannot do better: `lab-blog new` is a shell command with no
identity, so every post it scaffolds is implicitly the operator's.

The team site is the only surface that knows who is asking, because Cloudflare
Access hands it a verified email. So it can attest both fields from what it
OBSERVED. This is the ARCH-362 lesson one domain over: `claimed_by` was worthless
until the caller could be attributed, and a self-asserted author is the same
defect on a surface whose output is public.

FOUR RULES, each of which is a security property rather than a style choice:

1. THE AUTHOR COMES FROM THE VERIFIED IDENTITY, NEVER FROM THE REQUEST BODY. A
   caller-supplied author is exactly the self-assertion this replaces, so the
   field is ignored if sent rather than merely defaulted.
2. AUTHORSHIP IS ATTESTED FROM WHAT THE SERVER OBSERVED. A post composed by a
   verified human in a browser session is `human`. A caller may declare `collab`
   or `llm` — declaring MORE assistance than was observed is honest, and the one
   direction a writer has no incentive to lie in. Declaring `human` when the
   server did not observe a human is the lie that matters, and it is unreachable:
   `human` is not settable, only observed.
3. PUBLISHING IS GATED BY THE ATTESTED HUMAN ACTION. When this rule was written,
   publishing shelled to `lab-blog publish`, whose leak guard kept internal
   material off a public site. That flow went with `convivy-lab`, and the guard
   was retired with the dashboard pipeline it existed for. `publish_draft` now
   writes Orient's own store and pushes the corpus for the site to read within
   the hour, with no content check in between; its docstring says why that is
   deliberate.
4. THE SLUG IS THE ATTACK SURFACE. It is caller-authored text that becomes a file
   path, so it is validated against a strict charset and re-checked after
   resolution, the same posture `lab_content` took on the read side.

Deny-by-default throughout: `blog.write` is held only by admin and member, and
notably NOT by `agent` or `system`, so no automated caller can publish to a
public-facing site through this surface.

**THE ARCHIVES ARE THE STORE (CQ·168, issue_01M5GBHESAC479QFB2VHK5J6AK; CQ·170,
issue_01M5GBJ3H5BFX0CR83019T737D).** Every create, save and publish writes
one Library signal through `library_client.create_signal`, and nothing here
has ever written to a database row. Orient's own post tables were dropped
in CQ·170's second part (migration e0073e9d195b). A post's
identity is its `stream` (the slug); each signal is one revision, numbered
in `meta["revision"]`.

**ONE `kind=post` ENTRY PER SLUG CARRIES NO REVISION AT ALL AND MUST NEVER BE
READ AS LATEST: THE `reason="first-published"` DATING MARKER**
(written once on 2026-10-24 by CQ·170 part A's script, since removed). It exists only so the
public journal can show a migrated post's true original `published_at`
rather than the date the Archives copy happened to run; every function here
that reads a slug's revisions (`_group_by_slug`, `_slug_entries`) filters it
out before this module ever sees it as a `_Revision`.

**REVISION NUMBERING, STATED HERE BECAUSE IT HAS TO AGREE WITH THE MIGRATION.**
`bin/copy_posts_to_archives.py` copied every pre-existing post as real
revisions `1..N` (`dedup_key="post:<slug>:<n>"`) plus one `reason="current"`
mirror of the post's CURRENT text at copy time (`revision=None` in that row,
never a number). A new revision here takes `max(numbers already seen for this
slug) + 1` — `N+1` for a migrated post's first post-deploy save, `1` for a
slug the Archives have never seen. Two properties fall out of that one rule
rather than needing their own case: a new number can never collide with a
migrated `post:<slug>:<n>` key, and "latest" is decided by `produced_at`
(never by the number), so a migrated post's `reason="current"` entry — dated
after every real revision that preceded it — reads as latest until a
post-deploy save gives the slug an entry dated later still.

**A `kind: post_draft` ROW IS READABLE AND WRITABLE ONLY UNDER THE
APPROVED-CLIENT CREDENTIAL** (ADR-0038's `signal.post_draft`); every call this
module makes that touches that kind passes `human_reserved=True`. Every such
call here is already behind `_authorized`'s `blog.write` gate — `list_posts`
is the one route a viewer without that capability may call, and it is the one
function in this module that never asks for `kind=post_draft` at all, on
either credential, so there is no Library call to leak a draft through.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass

from fastapi import APIRouter, Request

import blog_common
import journal_push
import library_client
from api_common import (
    _iso, _json, api_guard as _api, read_json_body, resolve_identity, same_origin,
)
from authz import authorize

router = APIRouter(prefix="/api")

# Lowercase kebab, 1-64 chars, no leading or trailing hyphen. Deliberately
# narrower than "a safe filename": a slug becomes a public URL, so it is a naming
# rule as much as a guard. Mixed case is NORMALIZED rather than refused (the
# caller is typing a title, not a path), which is why the check runs after
# lowercasing — but the shape is enforced strictly from there.
# Defined in `blog_common`, shared with every other reader of a post's slug —
# one rule rather than a route-side copy that can drift from it.
SLUG_RE = blog_common.SLUG_RE

MAX_TITLE = 200
MAX_BODY = 200_000

# A writer may declare MORE assistance than the server saw, never less. `human`
# is absent on purpose: it is the value the server attests, not one a caller can
# claim (see rule 2).
DECLARABLE_AUTHORSHIP = ("collab", "llm")

# The Library product posts are filed under (platform decomposition on E26,
# issue_01M584RKNA5KKFXE00GSHKEEYD's thread): Field Notes are the company's
# journal, not Orient's.
PRODUCT = "company"
SOURCE = "writing_desk"
KIND_POST = "post"
KIND_DRAFT = "post_draft"
PREVIEW_LIMIT = 200


def _preview(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= PREVIEW_LIMIT else text[:PREVIEW_LIMIT].rstrip() + "…"


def _check_slug(slug: str) -> str:
    """An empty string when the slug is usable, otherwise the reason.

    Kept as a route-side check even though `library_client.create_signal` would
    happily accept a malformed slug as a `stream` value — a caller deserves a
    400 naming the naming rule rather than a row an editor can never find again.
    """
    if not blog_common.SLUG_RE.match(slug or ""):
        return "slug must be lowercase letters, digits and hyphens (max 64)"
    return ""


@dataclass
class _Revision:
    """One Archives entry for a Writing Desk post, as this module reads it
    back — a real numbered revision (`meta["revision"]` an int) or a migrated
    post's `reason="current"` mirror row, which carries none."""

    signal_id: str
    kind: str
    revision: int | None
    title: str
    author: str
    authorship: str
    reason: str
    produced_at: dt.datetime | None
    ingested_at: dt.datetime

    @property
    def at(self) -> dt.datetime:
        return self.produced_at or self.ingested_at


def _revision_from_signal(sig: library_client.SignalRecord) -> _Revision:
    meta = sig.meta or {}
    raw_revision = meta.get("revision")
    return _Revision(
        signal_id=sig.id, kind=sig.kind,
        revision=raw_revision if isinstance(raw_revision, int) else None,
        title=meta.get("title") or sig.stream or "", author=sig.actor,
        authorship=meta.get("authorship") or "human", reason=meta.get("reason") or "",
        produced_at=sig.produced_at, ingested_at=sig.ingested_at)


def _is_dating_marker(sig: library_client.SignalRecord) -> bool:
    """True for the `reason="first-published"` entry CQ·170 part A's
    one-time script wrote — a dating fact about a slug, never a revision of it. Every caller
    that builds `_Revision` objects filters a signal through this first, so
    the marker can never be "latest" here: see the module docstring's named
    case, a marker and a migrated post's `reason="current"` entry at the same
    `produced_at`."""
    return (sig.meta or {}).get("reason") == blog_common.FIRST_PUBLISHED_REASON


def _group_by_slug(sigs) -> dict[str, list[_Revision]]:
    by_slug: dict[str, list[_Revision]] = {}
    for sig in sigs:
        if _is_dating_marker(sig):
            continue
        by_slug.setdefault(sig.stream or "", []).append(_revision_from_signal(sig))
    for revs in by_slug.values():
        revs.sort(key=lambda r: r.at)
    return by_slug


def _slug_entries(slug: str) -> list[_Revision]:
    """Every Archives entry for `slug`, oldest first, across both kinds.

    Callers are always already gated on `blog.write` — this is the one reader
    in this module allowed to ask for `kind=post_draft`."""
    posts = library_client.list_signals(product=PRODUCT, kind=[KIND_POST], stream=slug)
    drafts = library_client.list_signals(product=PRODUCT, kind=[KIND_DRAFT], stream=slug,
                                         human_reserved=True)
    revs = [_revision_from_signal(s) for s in (*posts, *drafts) if not _is_dating_marker(s)]
    revs.sort(key=lambda r: r.at)
    return revs


def _marker_dates(sigs) -> dict[str, dt.datetime]:
    """Each slug's earliest `first-published` marker date. The marker is
    filtered out of every `_Revision` list, so a reader that shows when a
    post was first published asks for it here separately."""
    out: dict[str, dt.datetime] = {}
    for sig in sigs:
        if _is_dating_marker(sig):
            at = sig.produced_at or sig.ingested_at
            slug = sig.stream or ""
            if slug not in out or at < out[slug]:
                out[slug] = at
    return out


def _first_published_at(revs: list[_Revision],
                        marker: dt.datetime | None) -> dt.datetime | None:
    """The earliest of the slug's marker and its first `kind=post` entry, the
    same rule `journal_push._build_corpus` dates the public journal by. A
    migrated post's first `kind=post` entry is dated to the Archives copy, so
    without the marker it would read months late (PR #412 review)."""
    first_post = next((r for r in revs if r.kind == KIND_POST), None)
    dates = [d for d in (marker, first_post.at if first_post else None) if d is not None]
    return min(dates) if dates else None


def _latest(revs: list[_Revision]) -> _Revision | None:
    return revs[-1] if revs else None


def _next_revision_number(revs: list[_Revision]) -> int:
    nums = [r.revision for r in revs if r.revision is not None]
    return max(nums) + 1 if nums else 1


async def _authorized(request: Request, *, writes: bool = True):
    """The common gate. Returns (identity, None) or (None, error-response).

    `writes=False` skips the origin check for read-only routes, matching what
    `room_api` does. CSRF defends state changes; applying it to a GET protects
    nothing and refuses legitimate cross-origin reads, so the deviation was a
    cost with no benefit (code review, #114).
    """
    ident = resolve_identity(request)
    if ident is None:
        return None, _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        # Not merely "agents lack the capability" — stated here too, because the
        # thing being protected is a public-facing publication surface and a
        # future role change should have to defeat two checks, not one.
        return None, _json(403, {"error": "only a human may author a post"})
    if writes and not same_origin(request):
        return None, _json(403, {"error": "csrf: origin check failed"})
    if not authorize(ident, "blog.write", None):
        return None, _json(403, {"error": "blog.write required"})
    return ident, None


@router.get("/blog/drafts")
@_api
async def list_drafts(request: Request):
    """Every slug whose latest Archives entry is still a draft — a slug that
    has ever published (latest entry `kind=post`) belongs on `/blog/posts`
    instead, matching `list_posts_never_shows_a_draft`'s mirror property."""
    ident, err = await _authorized(request, writes=False)
    if err:
        return err
    posts = library_client.list_signals(product=PRODUCT, kind=[KIND_POST])
    drafts = library_client.list_signals(product=PRODUCT, kind=[KIND_DRAFT], human_reserved=True)
    out = []
    for slug, revs in _group_by_slug((*posts, *drafts)).items():
        latest = _latest(revs)
        if latest is not None and latest.kind == KIND_DRAFT:
            out.append({"slug": slug, "title": latest.title, "author": latest.author})
    out.sort(key=lambda d: d["slug"])
    return _json(200, {"drafts": out})


@router.get("/blog/posts")
@_api
async def list_posts(request: Request):
    """What is actually published, for the Writing Desk to show beside the
    drafts queue. Reads only `kind=post` signals, on the plain agent
    credential — the one route here readable by a viewer without
    `blog.write`, so it is also the one route that must never ask the
    Library for a `post_draft` row on that viewer's behalf.
    """
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return _json(403, {"error": "only a human may read the published list"})
    sigs = library_client.list_signals(product=PRODUCT, kind=[KIND_POST])
    markers = _marker_dates(sigs)
    out = []
    for slug, revs in _group_by_slug(sigs).items():
        if not revs:
            continue
        latest = revs[-1]
        out.append({
            "slug": slug, "title": latest.title, "author": latest.author,
            "authorship": latest.authorship,
            "date": _iso(_first_published_at(revs, markers.get(slug))),
            "updated": _iso(latest.at),
        })
    out.sort(key=lambda p: p["date"] or "", reverse=True)
    return _json(200, {"posts": out})


@dataclass
class PostView:
    """One post as a reading page renders it, its text read back from the
    Archives. `created_at` is the slug's first entry and `published_at` is
    `_first_published_at` (None for a draft)."""

    slug: str
    title: str
    author: str
    authorship: str
    body: str
    created_at: dt.datetime | None
    published_at: dt.datetime | None


def _view(slug: str, revs: list[_Revision], marker: dt.datetime | None = None) -> PostView:
    latest = revs[-1]
    body = library_client.get_signal_blob(
        latest.signal_id, human_reserved=(latest.kind == KIND_DRAFT))
    return PostView(slug=slug, title=latest.title, author=latest.author,
                    authorship=latest.authorship, body=body or "",
                    created_at=revs[0].at,
                    published_at=_first_published_at(revs, marker))


def published_views() -> list[PostView]:
    """Every published post, newest first, for a page any full-tier viewer
    reads (`blog_posts_build`). The same read as `list_posts`: `kind=post`
    rows on the plain agent credential, so it never asks the Library for a
    `post_draft` row."""
    sigs = library_client.list_signals(product=PRODUCT, kind=[KIND_POST])
    markers = _marker_dates(sigs)
    views = [_view(slug, revs, markers.get(slug))
             for slug, revs in _group_by_slug(sigs).items() if revs]
    views.sort(key=lambda v: v.published_at or v.created_at or dt.datetime.min, reverse=True)
    return views


def draft_views() -> list[PostView]:
    """Every slug whose latest entry is still a draft, as `list_drafts`
    selects them. Reads `post_draft` rows under the approved-client
    credential, so a caller must already have decided the viewer holds
    `blog.write`."""
    posts = library_client.list_signals(product=PRODUCT, kind=[KIND_POST])
    drafts = library_client.list_signals(product=PRODUCT, kind=[KIND_DRAFT], human_reserved=True)
    views = [_view(slug, revs) for slug, revs in _group_by_slug((*posts, *drafts)).items()
             if revs and revs[-1].kind == KIND_DRAFT]
    views.sort(key=lambda v: v.slug)
    return views


@router.get("/blog/drafts/{slug}")
@_api
async def get_draft(slug: str, request: Request):
    """One draft's full content, so an editor can load a draft that was created in
    an earlier session rather than only one just created in the current page load.
    Same gate and CSRF posture as the list route above — a read gains nothing from
    the origin check.

    **A PUBLISHED POST IS RETURNED TOO** — editing a live post and
    re-publishing is the normal correction path, and that stays true with the
    Archives as the store: `status` says which it is rather than leaving the
    Writing Desk to infer it.

    `?revision=N` loads an earlier numbered revision's own text instead of the
    latest — the DoD's "the earlier one reachable from it." `revisions` lists
    every revision number this slug has, so a reader knows what `N` to ask for;
    a migrated post's `reason="current"` entry carries no number and is never
    one of them, since it was never a position to return to.
    """
    ident, err = await _authorized(request, writes=False)
    if err:
        return err
    why = _check_slug(slug)
    if why:
        return _json(400, {"error": why})
    revs = _slug_entries(slug)
    latest = _latest(revs)
    if latest is None:
        return _json(404, {"error": f"no draft named {slug}"})

    chosen = latest
    want = request.query_params.get("revision")
    if want is not None:
        try:
            want_n = int(want)
        except ValueError:
            return _json(400, {"error": "revision must be an integer"})
        match = next((r for r in revs if r.revision == want_n), None)
        if match is None:
            return _json(404, {"error": f"no revision {want_n} for {slug}"})
        chosen = match

    body = library_client.get_signal_blob(
        chosen.signal_id, human_reserved=(chosen.kind == KIND_DRAFT))
    numbered = sorted({r.revision for r in revs if r.revision is not None})
    return _json(200, {
        "slug": slug,
        "title": chosen.title,
        "author": chosen.author,
        "authorship": chosen.authorship,
        "body": body or "",
        "status": "published" if latest.kind == KIND_POST else "draft",
        "revision": chosen.revision,
        "latest_revision": latest.revision,
        "revisions": numbered,
    })


@router.post("/blog/drafts")
@_api
async def create_draft(request: Request):
    """Start a post. The author is stamped from the verified session.

    **THE SLUG IS CHECKED AGAINST THE ARCHIVES BEFORE THE WRITE, AND AGAIN BY
    THE WRITE ITSELF.** The pre-check alone would race two concurrent creates
    the way a plain `get()`-then-`add()` once did (code review, #114) — both
    would see no prior entry and both would write. `dedup_key="post:<slug>:1"`
    closes that: a genuine race still produces two `create_signal` calls, but
    the Library resolves the collision to one winner and hands the loser back
    `idempotent=True`, which this route reads as "already exists" exactly like
    the pre-check's own refusal. The pre-check still earns its keep on its own:
    a slug the migration copied with zero real revisions (an old draft that
    was created but never saved) has no `post:<slug>:1` key for the dedup to
    catch, only a `reason="current"` entry — and the pre-check is what refuses
    a second create there.
    """
    ident, err = await _authorized(request)
    if err:
        return err
    body, err = await read_json_body(request, MAX_BODY + 8192)
    if err:
        return err
    slug = str(body.get("slug", "")).strip().lower()
    why = _check_slug(slug)
    if why:
        return _json(400, {"error": why})
    title = str(body.get("title", "")).strip()[:MAX_TITLE] or slug
    declared = str(body.get("authorship", "")).strip().lower()
    # See rule 2: more assistance is declarable, less is not.
    authorship = declared if declared in DECLARABLE_AUTHORSHIP else "human"
    text = str(body.get("body", ""))[:MAX_BODY]

    if _latest(_slug_entries(slug)) is not None:
        return _json(409, {"error": f"a draft named {slug} already exists"})

    _, idempotent, _ = library_client.create_signal(
        source=SOURCE, kind=KIND_DRAFT, actor=ident.actor, product=PRODUCT,
        stream=slug, body=_preview(text), content=text,
        meta={"title": title, "revision": 1, "authorship": authorship, "reason": "create"},
        dedup_key=f"post:{slug}:1", human_reserved=True)
    if idempotent:
        return _json(409, {"error": f"a draft named {slug} already exists"})
    return _json(201, {"slug": slug, "author": ident.actor, "authorship": authorship,
                       "status": "draft"})


@router.put("/blog/drafts/{slug}")
@_api
async def save_draft(slug: str, request: Request):
    """Save body/title. The attested author and authorship are PRESERVED,
    never re-taken from the request — otherwise a later save could rewrite
    who wrote an earlier one. Both are carried forward from the slug's own
    latest Archives entry rather than accepted as parameters here, the same
    guarantee a function taking no `author`/`authorship` parameters at all
    would make structural rather than remembered.

    **`kind` CONTINUES WHATEVER THE SLUG'S LATEST ARCHIVES ENTRY ALREADY IS.**
    `publish_draft` always writes a fresh `kind=post` entry the moment it
    publishes, so by the time a save can run against a published slug, the
    latest entry is already `post` — nothing else to read to tell a published
    slug from a draft one.
    """
    ident, err = await _authorized(request)
    if err:
        return err
    why = _check_slug(slug)
    if why:
        return _json(400, {"error": why})
    body, err = await read_json_body(request, MAX_BODY + 8192)
    if err:
        return err

    revs = _slug_entries(slug)
    latest = _latest(revs)
    if latest is None:
        return _json(404, {"error": f"no draft named {slug}"})

    kind = latest.kind
    title = str(body.get("title", "")).strip()[:MAX_TITLE] or latest.title
    text = str(body.get("body", ""))[:MAX_BODY]
    revision = _next_revision_number(revs)

    # Two saves that read the same latest entry compute the same revision
    # number; the Library keeps the first and answers the second `idempotent`,
    # so that writer is told to reload rather than told it saved.
    _, idempotent, _ = library_client.create_signal(
        source=SOURCE, kind=kind, actor=ident.actor, product=PRODUCT,
        stream=slug, body=_preview(text), content=text,
        meta={"title": title, "revision": revision, "authorship": latest.authorship,
              "reason": "save"},
        dedup_key=f"post:{slug}:{revision}", human_reserved=(kind == KIND_DRAFT))
    if idempotent:
        return _json(409, {"error": f"{slug} was saved elsewhere since you loaded it; "
                                    "reload it and save again"})
    return _json(200, {"slug": slug, "saved": True})


@router.post("/blog/drafts/{slug}/publish")
@_api
async def publish_draft(slug: str, request: Request):
    """Publish a draft — or re-publish an edited live post — by writing one
    more Archives entry, then push the corpus to `convivy/swabbys-journal`
    and start Swabby's Journal's site build.

    **THE TEXT BEING PUBLISHED COMES FROM THE ARCHIVES' LATEST REVISION.**
    `create_draft`/`save_draft` have written only to the Archives since
    CQ·168, so this is the only place a post's current text lives for any
    slug saved since.

    **THE ARCHIVES ENTRY BELOW, KIND `post`, IS THE WHOLE WRITE** (CQ·170 —
    there used to also be a parallel write into Orient's own post tables,
    which this route no longer touches; CQ·170's second part drops those
    tables outright). Without this entry, a post created and published with
    no intervening
    save would have no `kind=post` signal at all — not merely absent from
    `list_posts`, which must never read
    `kind=post_draft` on a plain viewer's behalf and so could never find it
    there either, but also read back by `save_draft`'s own `kind =
    latest.kind` as still a draft on the very next edit. The entry mirrors
    what `save_draft` writes, `reason="publish"` in place of `"save"` — the
    mark `journal_push`'s own corpus builder keys on to tell a publication
    from a plain save.

    **So the attested human action IS the gate** on everything downstream of
    this route, same as before: there is no content check between this route
    and Swabby's Journal (convivybuilder.org), deliberately and with the
    operator's explicit agreement.
    """
    ident, err = await _authorized(request)
    if err:
        return err
    why = _check_slug(slug)
    if why:
        return _json(400, {"error": why})

    revs = _slug_entries(slug)
    latest = _latest(revs)
    if latest is None:
        return _json(404, {"error": f"no draft named {slug}"})
    text = library_client.get_signal_blob(
        latest.signal_id, human_reserved=(latest.kind == KIND_DRAFT)) or ""
    if not text.strip():
        return _json(400, {"error": "refusing to publish an empty post"})
    if not latest.author or not latest.authorship:
        return _json(409, {"error": "draft is missing its attested author; "
                                    "re-save it in the Writing Desk before publishing"})

    # The Archives entry goes first: if a concurrent save already took this
    # revision number, the Library answers `idempotent` and nothing is
    # published, so the journal never carries text the Archives does not show
    # as the published revision.
    revision = _next_revision_number(revs)
    new_id, idempotent, _ = library_client.create_signal(
        source=SOURCE, kind=KIND_POST, actor=ident.actor, product=PRODUCT,
        stream=slug, body=_preview(text), content=text,
        meta={"title": latest.title, "revision": revision,
              "authorship": latest.authorship, "reason": "publish"},
        dedup_key=f"post:{slug}:{revision}")
    if idempotent:
        return _json(409, {"error": f"{slug} was saved elsewhere since you loaded it; "
                                    "reload it and publish again"})

    # **PUBLISHED_AT IS THE SLUG'S FIRST PUBLICATION, NEVER THE LATEST**
    # — re-publishing an edited post must not lift it to the top of the site
    # (CQ·170, item 3; the old post store's rule, carried over). `revs`
    # was read before this write and holds no marker, so the marker is read
    # separately; with neither a marker nor an earlier `kind=post` entry, this
    # publish is the first one and its own entry carries the date.
    marker = _marker_dates(library_client.list_signals(
        product=PRODUCT, kind=[KIND_POST], stream=slug)).get(slug)
    published_at = _first_published_at(revs, marker)
    if published_at is None:
        new_sig = library_client.get_signal(new_id)
        published_at = new_sig.produced_at or new_sig.ingested_at

    # **OFF THE EVENT LOOP.** `push_corpus` makes several blocking `urlopen`
    # calls (the Git Data API) with a 10s timeout each,
    # and this server runs a single uvicorn worker — so a slow-but-not-hanging
    # GitHub would stall EVERY request it is serving, not just this caller, for
    # the duration. Found by `reviewer` on PR #195, reintroducing a pattern a
    # prior fix elsewhere in this repo removed for the same reason.
    journal = await asyncio.to_thread(journal_push.push_corpus)
    return _json(200, {"slug": slug, "published": True, "by": ident.actor,
                       "published_at": _iso(published_at), "journal": journal})
