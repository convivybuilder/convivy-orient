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
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

import blog_store
import journal_push
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
# Defined in `blog_store`, which is where a malformed slug must also be
# refused — one rule rather than a route-side copy that can drift from it.
SLUG_RE = blog_store.SLUG_RE

MAX_TITLE = 200
MAX_BODY = 200_000

# A writer may declare MORE assistance than the server saw, never less. `human`
# is absent on purpose: it is the value the server attests, not one a caller can
# claim (see rule 2).
DECLARABLE_AUTHORSHIP = ("collab", "llm")


def _check_slug(slug: str) -> str:
    """An empty string when the slug is usable, otherwise the reason.

    **This replaced `_resolve_draft`, which returned a filesystem path.** Drafts
    live in `blog_store` now, so there is no path to resolve and no containment
    check to make here — the store refuses a slug that is unsafe as a path
    component, and `blog_store.SLUG_RE` is the one definition both sides use.

    Kept as a route-side check anyway, because a caller deserves a 400 naming the
    naming rule rather than a 500 from a `ValueError` raised three layers down.
    """
    if not blog_store.SLUG_RE.match(slug or ""):
        return "slug must be lowercase letters, digits and hyphens (max 64)"
    return ""


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
    ident, err = await _authorized(request, writes=False)
    if err:
        return err
    out = [{"slug": p.slug, "title": p.title, "author": p.author}
           for p in blog_store.drafts()]
    return _json(200, {"drafts": out})


POSTS_GIT_TIMEOUT_S = 30


@router.get("/blog/posts")
@_api
async def list_posts(request: Request):
    """What is actually published, for the Writing Desk to show beside the
    drafts queue.

    **READS THE STORE, NOT `convivy-lab`'s GIT HEAD.** It used to shell to
    `git show HEAD:content/field-notes/...` against a checkout that is now
    deleted, which made two problems at once: this route and `/blog/corpus`
    were two sources of truth for "what is published" and would drift the moment
    a post was edited here, and this one would have broken outright the day the
    repo went — a Writing Desk listing no published posts, which is the exact
    defect the Writing Desk work was filed to fix. Found by `reviewer` on PR #194.

    Still human-only and still ungated on `blog.write`, both unchanged: this
    content is public, so requiring the write role to list it would hide a public
    page from a contractor-tier viewer for no security reason. A machine caller
    has no business here either — `journal_push.push_corpus` reads
    `blog_store.corpus()` directly, and no route serves this to a machine.
    """
    ident = resolve_identity(request)
    if ident is None:
        return _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return _json(403, {"error": "only a human may read the published list"})
    out = [{"slug": p.slug, "title": p.title, "author": p.author,
            "authorship": p.authorship, "date": _iso(p.published_at),
            "updated": _iso(p.updated_at)}
           for p in blog_store.published()]
    return _json(200, {"posts": out})


@router.get("/blog/drafts/{slug}")
@_api
async def get_draft(slug: str, request: Request):
    """One draft's full content, so an editor can load a draft that was created in
    an earlier session rather than only one just created in the current page load.
    Same gate and CSRF posture as the list route above — a read gains nothing from
    the origin check."""
    ident, err = await _authorized(request, writes=False)
    if err:
        return err
    why = _check_slug(slug)
    if why:
        return _json(400, {"error": why})
    post = blog_store.get(slug)
    if post is None:
        return _json(404, {"error": f"no draft named {slug}"})
    # **A PUBLISHED POST IS RETURNED TOO, and the first cut refused it** — a
    # regression `reviewer` caught on PR #197. `blog_store.save` deliberately
    # writes to a published row, because editing a live post and re-publishing is
    # the normal correction path, so refusing to LOAD one left the Writing Desk
    # able to save a post it could not open. `/blog/posts` omits the body, so
    # nothing else filled the gap. The file-backed version had no such check and
    # this worked on `main`.
    #
    # `status` is returned so the editor can say which it is rather than infer it.
    return _json(200, {
        "slug": post.slug,
        "title": post.title,
        "author": post.author,
        "authorship": post.authorship,
        "body": post.body,
        "status": post.status,
    })


@router.post("/blog/drafts")
@_api
async def create_draft(request: Request):
    """Start a post. The author is stamped from the verified session."""
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

    # THE CONFLICT IS DECIDED BY THE INSERT, not by a prior lookup. The file
    # version used `open(path, "x")` for the same reason (code review, #114):
    # two concurrent creates on one slug both passed an existence check, and the
    # loser's draft was overwritten while its author still received a 201 — gone
    # silently, under a header attesting somebody else. `blog_store.create` lets
    # the primary key refuse.
    try:
        blog_store.create(slug=slug, title=title,
                          body=str(body.get("body", ""))[:MAX_BODY],
                          author=ident.actor, authorship=authorship)
    except blog_store.SlugTaken:
        return _json(409, {"error": f"a draft named {slug} already exists"})
    return _json(201, {"slug": slug, "author": ident.actor, "authorship": authorship,
                       "status": "draft"})


@router.put("/blog/drafts/{slug}")
@_api
async def save_draft(slug: str, request: Request):
    """Save body/title. The attested header is PRESERVED, never re-taken from the
    request — otherwise a later save could rewrite the author of an earlier one."""
    ident, err = await _authorized(request)
    if err:
        return err
    why = _check_slug(slug)
    if why:
        return _json(400, {"error": why})

    body, err = await read_json_body(request, MAX_BODY + 8192)
    if err:
        return err

    # THE ATTESTED FIELDS ARE NOT IN THIS CALL AT ALL. The file version carried
    # the parsed header forward rather than re-reading the request, so a later
    # save could not rewrite who wrote an earlier one; `blog_store.save` takes no
    # author or authorship parameter, which is the same guarantee made structural
    # rather than remembered. `actor` is passed only to attribute the revision
    # this save appends — it does not touch the post's own `author` field.
    post = blog_store.save(slug=slug,
                           title=str(body.get("title", "")).strip()[:MAX_TITLE] or None,
                           body=str(body.get("body", ""))[:MAX_BODY],
                           actor=ident.actor)
    if post is None:
        return _json(404, {"error": f"no draft named {slug}"})
    return _json(200, {"slug": slug, "saved": True})


@router.post("/blog/drafts/{slug}/publish")
@_api
async def publish_draft(slug: str, request: Request):
    """Publish a draft into Orient's own store, then push the corpus to
    `convivy/swabbys-journal` and start Swabby's Journal's site build.

    **THIS NO LONGER SHELLS TO `lab-blog publish`, and that is the whole change.**
    That flow wrote into `convivy-lab` and ran a leak guard before anything went
    public. Both are gone: `convivy-lab` is deleted, and the codename guard
    was retired with the dashboard-publishing pipeline it existed for (the operator,
    2026-08-22) — what crosses to a public surface is now prose a human wrote and
    chose to publish, not an automated export of live internal state.

    **So the attested human action IS the gate.** There is no content check
    between this route and Swabby's Journal (convivybuilder.org), deliberately and with the operator's
    explicit agreement. `_authorized` requires a human identity holding
    `blog.write`, resolved from a verified Cloudflare Access email, and that
    verification is the only thing standing between a draft and the public web.
    It is a smaller set of controls than before and it guards a narrower thing:
    the old guard existed because dashboards exported real operational data, and
    they no longer do.

    **Publishing was writing to a place nothing reads.** Until this change the
    corpus route served `blog_store` while publish wrote to `convivy-lab`, so a
    newly published post reached the store never and the site never — the five
    posts on the old site were there purely from the one-time import. That is
    the defect this closes, and it was invisible because both halves worked.

    **ORIENT NOW PUSHES RATHER THAN WAITING TO BE PULLED**
    (`issue_01M0N650ZJHPQYPF54HD22RJEH`, step 2). `journal_push.push_corpus`
    writes the whole published corpus to `corpus.json` on `main` in
    `convivy/swabbys-journal`, then starts Shipwright's site build, so a publish
    goes live a couple of minutes later when that start succeeds. It is still best-effort and reported rather than
    raised: the post is published once the store says so, and a push that failed
    to land is a late post rather than a failed publish.

    **Only a publish reaches the journal now — a bare `save` on a live post
    does not**, which is a real behaviour change from the pull design: under the
    old cron pull, an edit saved (not re-published) to a live post still reached
    the site on the next daily build, because the pull read whatever the store
    held at build time. Under push, the site changes only when this route runs,
    matching `blog_store.save`'s own docstring ("`publish` is what moves the text
    to the public surface"). A publish still carries any saved edits to every
    other live post too, because it pushes the whole published corpus, not just
    this slug.
    """
    ident, err = await _authorized(request)
    if err:
        return err
    why = _check_slug(slug)
    if why:
        return _json(400, {"error": why})
    draft = blog_store.get(slug)
    if draft is None:
        return _json(404, {"error": f"no draft named {slug}"})
    if not (draft.body or "").strip():
        return _json(400, {"error": "refusing to publish an empty post"})

    # THE ATTESTED FIELDS COME OFF THE DRAFT'S HEADER, WHICH THE SERVER WROTE.
    # `create_draft` and `save_draft` set `author` and `authorship` from the
    # verified identity and preserve them across an edit, so the header is a
    # server record rather than caller input. Re-deriving `author` from the
    # publisher would be wrong in the one case that matters: a post one member
    # wrote and another published would be re-attributed on its way out.
    if not draft.author or not draft.authorship:
        return _json(409, {"error": "draft is missing its attested author; "
                                    "re-save it in the Writing Desk before publishing"})

    try:
        post = blog_store.publish(slug=slug, title=draft.title or slug,
                                  body=draft.body, author=draft.author,
                                  authorship=draft.authorship, actor=ident.actor)
    except ValueError as e:
        return _json(400, {"error": str(e)})

    # **OFF THE EVENT LOOP.** `push_corpus` makes several blocking `urlopen`
    # calls (the Git Data API) with a 10s timeout each,
    # and this server runs a single uvicorn worker — so a slow-but-not-hanging
    # GitHub would stall EVERY request it is serving, not just this caller, for
    # the duration. This repo already learned that once: the merge-queue warmer
    # routes its blocking subprocess through `to_thread` and its comment says
    # awaiting one here "would stall EVERY request for the duration — the
    # precise slowness this whole design avoids". Found by `reviewer` on PR
    # #195, reintroducing the same pattern the fix removed.
    journal = await asyncio.to_thread(journal_push.push_corpus)
    return _json(200, {"slug": slug, "published": True, "by": ident.actor,
                       "published_at": _iso(post.published_at),
                       "journal": journal})
