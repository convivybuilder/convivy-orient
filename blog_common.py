"""Shared blog-domain constants and helpers (CQ·170).

Split out of `blog_api.py` rather than folded into it because `journal_push.py`
needs `_display_author` and `blog_api.py` already imports `journal_push` (to
push the corpus after a publish) — a module importing the other way would
cycle. Everything here is read by `blog_api.py`, `blog_posts_build.py`,
`writing_desk_build.py` and `journal_push.py`.
"""
from __future__ import annotations

import re

# Lowercase kebab, 1-64 chars, no leading or trailing hyphen. Deliberately
# narrower than "a safe filename": a slug becomes a public URL, so it is a
# naming rule as much as a guard.
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

# The three values a post's authorship may carry. `human` is attested from
# the verified Cloudflare Access identity, never declared by a caller
# (`blog_api.py`'s rule 2); `collab` and `llm` are the only ones a caller may
# declare (`blog_api.DECLARABLE_AUTHORSHIP`).
AUTHORSHIP = ("human", "collab", "llm")

# WHERE A PUBLISHED POST LIVES ON THE PUBLIC WEB, in one place. It was written
# out three times independently — the corpus import's link rewriter, the
# Writing Desk's Published link, and the lab redirect map — and drifted twice
# in one day before `reviewer` flagged it on PR #196. No trailing slash: a
# post is served at `<FIELDNOTES_URL>/<slug>/`, and the trailing slash on the
# post URL is the one the site requires.
FIELDNOTES_URL = "https://convivybuilder.org/journal"

# The `meta["reason"]` value CQ·170 part A's one-time script wrote on its
# one-time `kind=post` dating marker (CQ·170 part A, issue_01M5GBJ3H5BFX0CR83019T737D):
# a slug's original `blog_posts.published_at`, carried into the Archives
# before Orient's post tables were dropped (part B), because the Archives copy
# (`bin/copy_posts_to_archives.py`) never recorded that date itself. It
# carries no revision position (`meta["revision"]` is `None`) and must never
# be read as a slug's latest revision — every reader of a slug's revisions
# filters it out before computing "latest"; `journal_push._build_corpus` is
# the one reader that still consults it, for `date` alone.
FIRST_PUBLISHED_REASON = "first-published"


def _display_author(author: str) -> str:
    """The stored `author` is the attested identity, which for a human is a
    verified email — right for an authz check, wrong for a byline. Everything
    public-facing (the Blog Posts page, the pushed `corpus.json`) gets a
    display name instead: the email's local part, capitalised. A non-email
    author (the pre-authz posts' plain "The operator", or an empty string)
    passes through unchanged."""
    if "@" in author:
        local = author.split("@", 1)[0]
        return local[:1].upper() + local[1:]
    return author
