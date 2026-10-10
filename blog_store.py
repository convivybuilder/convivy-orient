#!/usr/bin/env python3.11
"""The Field Notes corpus, which lives here rather than in a repository.

**Orient is the store of truth and every public surface is a renderer** (the operator,
2026-08-22, `issue_01M0N650ZJHPQYPF54HD22RJEH`). Before this module the corpus
was files under `convivy-lab/content/field-notes/`, published by a private
repository's Action force-pushing built HTML. `convivy-lab` is gone, so
"Orient holds the posts" had to stop being a description of intent.

**Every write that REPLACES text appends a revision, and that is the point
rather than a nicety.** Git answered "what did this post say before" until now,
and it stops answering the moment the repo is gone. A published post is a public
artifact; an edit to one changes what people read, and without history nothing
records what it replaced.

**`create` is the one write that appends none, because it replaces nothing.** It
is not an oversight, and the invariant's own rationale is why: a brand-new draft
has no prior text for a revision to preserve. Today every route out of a created
draft records the created text before it can be lost, and the two routes do not
hold it equally firmly. `save` snapshots BEFORE mutation, so the first edit's
revision holds exactly what `create` wrote whoever calls it — that one is
structural. `publish` snapshots AFTER, so it preserves the created text only
because its one production caller, `publish_draft` in `blog_api.py`, re-passes
the draft's own just-read body; a future caller passing a different body would
lose the created text with nothing here to stop it. There is no delete or
unpublish path. Read as the stronger "every write appends a revision", this
paragraph has already produced one filing claiming a draft's first version is
unrecoverable; it is not (`issue_01M1R5MKDBFBWPY4PTKXKDX3GK`, wontfix, with the
reproduction on its thread). `ingest` DOES append on its `is_new` branch, and
that asymmetry is deliberate too: an imported post's text predates this store,
so the revision is the only record that the migration carried it.

**Nothing here scrubs content.** The codename tokens and the leak denylist were
removed with the dashboard-publishing pipeline they existed for (the operator,
2026-08-22): what crosses to a public surface is now prose a human wrote and
published through an attested action, not an automated export of live internal
state. This module stores and serves markdown as authored.
"""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

import control_db
from models import BlogPost, BlogPostRevision, utcnow

# The four values a post's authorship may carry, and the reason the field exists
# at all: The operator wants it on every piece of prose the public site serves, as a
# demonstration of one way to classify human-facing writing (2026-08-22).
#
# `human` IS NOT DECLARABLE. It is what the server attests from the verified
# Cloudflare Access identity, and a writer may declare MORE assistance than the
# server saw, never less (`blog_api.py`'s rule 2). That asymmetry is the whole
# integrity property: a provenance label anyone can set about themselves claims
# nothing, and one that can only be revised toward more assistance can be read.
AUTHORSHIP = ("human", "collab", "llm")

PUBLISHED = "published"
DRAFT = "draft"

# WHERE A PUBLISHED POST LIVES ON THE PUBLIC WEB, in one place.
#
# It was written out three times — the corpus import's link rewriter, the Writing
# Desk's Published link, and the lab redirect map — and reviewer flagged on PR
# #196 that nothing kept them in step. That is not hypothetical drift: the
# Writing Desk copy was wrong twice in one day, pointing at the retired host and
# then carrying a `.html` suffix the site does not use. A public URL that has
# already been wrong once should have exactly one definition.
#
# No trailing slash: a post is served at `<FIELDNOTES_URL>/<slug>/`, and the
# trailing slash on the post URL is the one the site requires.
FIELDNOTES_URL = "https://convivybuilder.org/journal"

# THE SLUG RULE, ENFORCED WHERE SLUGS ARE STORED rather than only where they
# arrive. Lowercase kebab, 1-64 characters, no leading or trailing hyphen —
# deliberately narrower than "a safe filename", because a slug becomes a public
# URL and is a naming rule as much as a guard.
#
# It lived only in `blog_api`, so the route validated and the store accepted
# anything (`security-reviewer`, PR #196). Nothing could reach the store with a
# bad slug today — the API checks, and the one-time import derives slugs from
# `Path.stem` — but the column is an unconstrained `String(64)`, so a repair
# script or a second ingest path could store `../x` or `/etc/x`, and a consumer
# joining that into a filesystem path would write outside its output directory:
# `Path("/a") / "/etc/x"` is `Path("/etc/x")`, the prefix silently discarded.
#
# A store that cannot hold a malformed slug removes that for every consumer,
# rather than each one re-deriving the guard.
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


def _check_slug(slug: str) -> None:
    if not SLUG_RE.match(slug or ""):
        raise ValueError(
            f"{slug!r} is not a valid slug: lowercase letters, digits and "
            f"hyphens, 1-64 characters, no leading or trailing hyphen. A slug "
            f"is a public URL component and a filesystem path component.")


def _revision(post: BlogPost, *, reason: str, actor: str | None) -> BlogPostRevision:
    """A snapshot of the post as it stands, appended before or after a write by
    the caller's choice — never derived later, since a revision reconstructed
    from a diff is a guess about what somebody read."""
    return BlogPostRevision(
        slug=post.slug, title=post.title, body=post.body,
        author=post.author, authorship=post.authorship,
        reason=reason, actor=actor)


def get(slug: str) -> BlogPost | None:
    """**Expunged before the session closes, like every other reader here.**
    Returning an instance still attached to a session that is about to commit and
    close hands the caller an object whose attributes expire under it — the next
    read raises `DetachedInstanceError` rather than returning the value. Detach
    it while its state is loaded and the caller gets a plain snapshot."""
    with control_db.session() as s:
        post = s.get(BlogPost, slug)
        if post is not None:
            s.expunge(post)
        return post


def published() -> list[BlogPost]:
    """Every published post, newest first.

    Ordered by `published_at` rather than `updated_at`, deliberately: a blog is
    ordered by when a piece went out, and fixing a typo in a two-month-old post
    must not move it to the top of the site.
    """
    with control_db.session() as s:
        rows = s.execute(
            select(BlogPost)
            .where(BlogPost.status == PUBLISHED)
            .order_by(BlogPost.published_at.desc(), BlogPost.slug)
        ).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def _iso(value: dt.datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None  # stored naive UTC


def _display_author(author: str) -> str:
    """The stored `author` is the attested identity, which for a human is a
    verified email — right for an authz check, wrong for a byline. Everything
    the corpus reaches (the public Swabby's Journal page, and the `corpus.json`
    its repository holds) gets a display name instead: the email's local part,
    capitalised. A non-email author (the pre-authz posts' plain "The operator", or an
    empty string) passes through unchanged."""
    if "@" in author:
        local = author.split("@", 1)[0]
        return local[:1].upper() + local[1:]
    return author


def corpus() -> dict:
    """The published corpus, in the exact shape a rendering surface consumes:
    `{"posts": [...], "count": N}`.

    **THE ONE FUNCTION THAT BUILDS THIS SHAPE.** `journal_push.push_corpus`
    calls this to build the `corpus.json` it writes to `main` in
    `convivy/swabbys-journal` (`issue_01M0N650ZJHPQYPF54HD22RJEH`, the push
    design). It used to also serve `GET /api/blog/corpus`, which pulled from
    this same function so a pulled and a pushed corpus could never diverge; that
    route is retired now that the site no longer pulls from Orient.

    **`author` here is a display name, never the stored identity.** The
    column holds a verified email for a human post; `corpus()` is what both a
    public HTML page and a public git history serve it to, and an email
    address must not ride along to either (`security-reviewer`, PR #289).
    """
    posts = [
        {
            "slug": p.slug,
            "title": p.title,
            "date": _iso(p.published_at),
            "updated": _iso(p.updated_at),
            "author": _display_author(p.author),
            "authorship": p.authorship,
            "body": p.body,
        }
        for p in published()
    ]
    return {"posts": posts, "count": len(posts)}


def revisions(slug: str) -> list[BlogPostRevision]:
    with control_db.session() as s:
        rows = s.execute(
            select(BlogPostRevision)
            .where(BlogPostRevision.slug == slug)
            .order_by(BlogPostRevision.id)
        ).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def ingest(*, slug: str, title: str, body: str, author: str, authorship: str,
           published_at: dt.datetime | None, updated_at: dt.datetime | None = None,
           status: str = PUBLISHED) -> BlogPost:
    """Import one post that already existed before this store did.

    **`reason="ingest"` and `actor=None`, and neither is laziness.** An imported
    post predates the store and has no attested actor to name; writing an
    inferred one would be precisely the fabrication the attestation rule exists
    to prevent. `ingest` appearing on a revision is also what distinguishes a
    post carried over from `convivy-lab` from one authored here, which stops
    mattering the day nobody remembers the migration happened.

    **`updated_at` is the post's own, not the moment of import.** Defaulting it
    to now would have every imported post claim it was revised at migration time,
    which is false and is a date the public site displays. The caller passes what
    the source actually says; `None` means the post has never been revised, and
    `published_at` stands in.

    Idempotent by slug: re-running replaces the current row and appends another
    revision rather than raising, so a re-ingest after a corrected export is a
    normal operation rather than a manual cleanup.
    """
    _check_slug(slug)
    if authorship not in AUTHORSHIP:
        raise ValueError(f"authorship must be one of {AUTHORSHIP}, got {authorship!r}")
    now = utcnow()
    with control_db.session() as s:
        post = s.get(BlogPost, slug)
        is_new = post is None
        if is_new:
            post = BlogPost(slug=slug, created_at=now)
            s.add(post)
        else:
            # Snapshotted BEFORE mutation, like `save`: a re-ingest can carry a
            # corrected body (this function's own docstring calls that "a
            # normal operation"), so the revision must record what the post
            # replaced, not what it was corrected to. Snapshotting after would
            # record the new, corrected content as its own replacement and
            # silently lose the pre-correction text the revision exists to
            # preserve. A brand-new post has no prior state to preserve, so it
            # snapshots after mutating, same as before — its first revision is
            # the ingested content itself.
            s.add(_revision(post, reason="ingest", actor=None))
        post.title = title
        post.body = body
        post.author = author
        post.authorship = authorship
        post.status = status
        post.published_at = published_at
        post.updated_at = updated_at or published_at or now
        if is_new:
            s.add(_revision(post, reason="ingest", actor=None))
        s.flush()
        # **`onupdate=utcnow` HAS TO BE DEFEATED HERE, and it is not obvious.**
        # Assigning `post.updated_at` the value it already holds is not a change
        # SQLAlchemy tracks, so the column stays out of the UPDATE's SET clause,
        # and `onupdate` then fires and stamps NOW. Re-ingesting a post therefore
        # dated every one of them to the moment of migration — wrong, and wrong
        # in a field the public site displays. Found by running the import
        # against the real corpus rather than a fixture: only the two posts that
        # took a second revision showed it. A Core UPDATE naming the column puts
        # it in the SET clause, which is what suppresses `onupdate`.
        stamp = updated_at or published_at
        if stamp is not None:
            s.execute(update(BlogPost)
                      .where(BlogPost.slug == slug)
                      .values(updated_at=stamp))
            post.updated_at = stamp
        s.expunge_all()
        return post


def publish(*, slug: str, title: str, body: str, author: str, authorship: str,
            actor: str) -> BlogPost:
    """Publish a post, or re-publish an edited one, under an attested human.

    **THE COUNTERPART OF `ingest`, AND THE DIFFERENCES ARE THE POINT.** `ingest`
    carries no actor because an imported post predates this store and has no
    attested identity. This one REQUIRES an actor: it is called from the Writing
    Desk's publish route, where a verified Cloudflare Access identity resolved
    the author, and a publish with nobody attached is the thing that must not be
    possible.

    `author` and `authorship` are the server's values, never a caller's claim
    (`blog_api.py`'s rule 2). This function does not re-derive them; it stores
    what the route attested, and the route is where the identity is read.

    **`published_at` is set once and never moved.** A blog is ordered by when a
    piece went out, so re-publishing an edited post updates its text and its
    `updated_at` while leaving its position alone — fixing a typo in a
    two-month-old post must not lift it to the top of the site.

    Every call appends a revision, so an edit to a live post records what it
    replaced. That matters more here than on import: this is the path that
    changes what the public reads.
    """
    _check_slug(slug)
    if authorship not in AUTHORSHIP:
        raise ValueError(f"authorship must be one of {AUTHORSHIP}, got {authorship!r}")
    if not actor:
        raise ValueError("publish requires the attested actor; refusing to "
                         "record a publication with nobody attached")
    now = utcnow()
    with control_db.session() as s:
        post = s.get(BlogPost, slug)
        if post is None:
            post = BlogPost(slug=slug, created_at=now)
            s.add(post)
        post.title = title
        post.body = body
        post.author = author
        post.authorship = authorship
        post.status = PUBLISHED
        if post.published_at is None:
            post.published_at = now
        post.updated_at = now
        s.add(_revision(post, reason="publish", actor=actor))
        s.flush()
        s.expunge_all()
        return post


class SlugTaken(Exception):
    """A draft already exists at this slug."""


def drafts() -> list[BlogPost]:
    """Every unpublished post, newest first."""
    with control_db.session() as s:
        rows = s.execute(
            select(BlogPost)
            .where(BlogPost.status == DRAFT)
            .order_by(BlogPost.created_at.desc(), BlogPost.slug)
        ).scalars().all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def create(*, slug: str, title: str, body: str, author: str,
           authorship: str) -> BlogPost:
    """Start a draft, or refuse because the slug is taken.

    **THE CONFLICT IS DECIDED BY THE INSERT, NOT BY A PRIOR LOOKUP.** The file
    version used `open(path, "x")` for exactly this reason, recorded in code
    review #114: two concurrent creates on one slug both passed an existence
    check, and the loser's draft was overwritten by the winner while its author
    still received a 201 — a draft silently gone, under a header attesting
    somebody else. A `get()`-then-`add()` here would reintroduce that race with a
    different storage layer. The primary key is what refuses.

    `author` and `authorship` are the server's values, resolved by the route from
    a verified identity. This function stores what it is given and does not
    re-derive them.
    """
    _check_slug(slug)
    if authorship not in AUTHORSHIP:
        raise ValueError(f"authorship must be one of {AUTHORSHIP}, got {authorship!r}")
    now = utcnow()
    post = BlogPost(slug=slug, title=title, body=body, author=author,
                    authorship=authorship, status=DRAFT,
                    created_at=now, updated_at=now)
    try:
        with control_db.session() as s:
            s.add(post)
            s.flush()
            # RETURNED FROM THE OBJECT JUST INSERTED, not re-read. The first cut
            # ended with `return get(slug)`, which is a second query for a row
            # this function already has — and it made the mechanism untestable,
            # because a spy asserting "create never reads a BlogPost" could not
            # tell that read from the prior lookup the whole design exists to
            # avoid. Not reading at all is both cheaper and unambiguous.
            s.expunge(post)
    except IntegrityError as e:
        raise SlugTaken(slug) from e
    return post


def save(*, slug: str, title: str | None, body: str,
         actor: str | None = None) -> BlogPost | None:
    """Save a draft's text. Returns None when no such post exists.

    **The attested fields are never touched here**, which is the property the
    file version protected by carrying the parsed header forward rather than
    re-reading the request: a later save must not be able to rewrite who wrote an
    earlier one. `author`, `authorship`, `status` and `published_at` are all
    absent from this function's signature for that reason, rather than being
    accepted and ignored.

    **It works on a published post too**, deliberately: editing a live post and
    re-publishing is the normal correction path, and `publish` is what moves the
    text to the public surface. Saving does not unpublish — and because it writes
    straight into the published row, it changes what the public site renders
    immediately, on a draft as well as a post already live.

    **Every call appends a revision, recording the post as it stood before this
    edit.** A draft's prior text was never public, so that revision is cheap
    insurance rather than the point; a published post's prior text is what
    `corpus()` was serving a moment ago, and it is gone from the live row the
    instant this function returns. One rule for both rather than a
    status check keeps the history complete regardless of when a post happens to
    go live. `actor` is the server's attested identity when the caller has one in
    hand; a caller with none passes `None`, the same as `ingest`, rather than
    guessing.
    """
    with control_db.session() as s:
        post = s.get(BlogPost, slug)
        if post is None:
            return None
        # Snapshotted BEFORE mutation, unlike `publish`, which snapshots after:
        # `publish`'s only caller (`publish_draft`) always re-passes the row's
        # own just-read `title`/`body`, so order doesn't change what's recorded
        # there. `save`'s incoming `body` differs from the stored one by
        # construction — snapshotting after would record the new text as its
        # own replacement. `ingest`, re-ingesting an existing post, snapshots
        # before mutation for the same reason as here.
        s.add(_revision(post, reason="save", actor=actor))
        post.body = body
        if title:
            post.title = title
        post.updated_at = utcnow()
        s.flush()
        s.expunge_all()
        return post
