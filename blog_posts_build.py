"""Blog Posts — every Writing Desk post, readable in full inside Orient.

The operator, 2026-09-24: "what about a new user that doesn't yet have an outside place
to publish a blog? It should be visible to them internally until they make it.
... a new pane under Documents that is 'Blog Posts' that shows all the blog
posts from the Writing Desk, and has a note saying these can be published
elsewhere."

The Writing Desk is where a post is written; this is where it is read. It is
not another store: every post comes from `blog_store`, the one store of truth
(`issue_01M0N650ZJHPQYPF54HD22RJEH`), and every other surface, convivy.com's
Field Notes included, renders the same rows.

**Full tier only, like the rest of Documents**, and gated per viewer inside
it the same way the Writing Desk's own routes are. Published posts are shown
to any full-tier viewer, because `GET /api/blog/posts` already lists them to
any human. Drafts are shown only to a viewer holding `blog_write`, because
`GET /api/blog/drafts` refuses everyone else, and this page must not show
drafts more widely than the desk does. The drafts decision is made here on
the server from the viewer's role, never by hiding markup a viewer
could still read, which is why this route is handled outside
`server._full_only_routes()`: that table renders a page with no viewer and can
fall back to a static copy, and a drafts list depends on who is asking.
"""
from __future__ import annotations

from html import escape

import blog_store
import design_system as ds
from access_identity import tier_for
from markdown_render import md_to_html

ROUTE = "/reference/blog-posts"

_PAGE_CSS = """
.bp-note { margin:0 0 1.25rem; padding:.75rem 1rem; border:1px solid var(--line);
           border-radius:10px; background:var(--surface); color:var(--muted);
           font-size:.92rem; }
.bp-note a { color:var(--brand-ink); }
.bp-group h2 { font-size:1rem; margin:1.5rem 0 .6rem; }
.bp-post { border:1px solid var(--line); border-radius:10px; background:var(--surface);
           margin:0 0 .6rem; }
.bp-post > summary { cursor:pointer; padding:.7rem 1rem; list-style:none;
                     display:flex; flex-wrap:wrap; align-items:baseline; gap:.35rem .75rem; }
.bp-post > summary::-webkit-details-marker { display:none; }
.bp-title { font-weight:650; color:var(--ink); }
.bp-meta { color:var(--muted); font-size:.85rem; }
.bp-status { font-size:.75rem; font-weight:600; padding:1px 7px; border-radius:999px;
             border:1px solid var(--line); color:var(--muted); }
.bp-status.draft { border-style:dashed; }
.bp-body { padding:0 1rem 1rem; overflow-wrap:anywhere; }
.bp-body pre { overflow-x:auto; }
.bp-empty { color:var(--muted); font-size:.92rem; margin:.25rem 0 0; }
"""


# `blog_store.AUTHORSHIP` is `human | collab | llm`. A human post needs no note;
# the other two say a model had a hand in it, as the byline mark does elsewhere.
_AUTHORSHIP_LABEL = {"collab": "written with a model", "llm": "written by a model"}


def _date(value) -> str:
    return value.strftime("%Y-%m-%d") if value else ""


def _post_html(post, *, draft: bool) -> str:
    """One post, collapsed to its title line and opened to read. Title, byline
    and slug are escaped; the body renders through `md_to_html`, the same
    CommonMark engine every other Orient page uses, which escapes raw HTML and
    drops unsafe link schemes."""
    when = _date(post.created_at if draft else post.published_at)
    label = "started" if draft else "published"
    status = ('<span class="bp-status draft">draft</span>' if draft
              else '<span class="bp-status">published</span>')
    # Unescaped here: `meta` is escaped once, as a whole, below.
    byline = blog_store._display_author(post.author or "")
    meta = " · ".join(bit for bit in (
        byline,
        f"{label} {when}" if when else "",
        _AUTHORSHIP_LABEL.get(post.authorship or "", ""),
    ) if bit)
    title = escape(post.title or post.slug)
    return (f'<details class="bp-post" id="post-{escape(post.slug, quote=True)}">'
            f'<summary><span class="bp-title">{title}</span>{status}'
            f'<span class="bp-meta">{escape(meta)}</span></summary>'
            f'<div class="bp-body">{md_to_html(post.body or "")}</div>'
            '</details>')


def _group(heading: str, posts, *, draft: bool, empty: str) -> str:
    rows = "".join(_post_html(p, draft=draft) for p in posts)
    inner = rows or f'<p class="bp-empty">{escape(empty)}</p>'
    return f'<section class="bp-group"><h2>{escape(heading)}</h2>{inner}</section>'


def live_html(email: str, *, can_see_drafts: bool) -> str:
    """The page. `can_see_drafts` is the caller's server-side decision from the
    viewer's role (`blog_write`), and a viewer without it gets no drafts
    section at all rather than an empty one, so the page never says whether
    drafts exist."""
    tier = tier_for(email)
    parts = [
        '<header class="hero"><div class="hero-inner">'
        '<h1>Blog Posts</h1>'
        '<p class="hero-sub">Every post written on the Writing Desk, to read here.</p>'
        '</div></header>',
        '<section class="home-section">',
        '<p class="bp-note">These posts are yours to publish elsewhere, on your own '
        'site or blog, whenever you have one. Until then they live here. Write and '
        'edit them on the <a href="/writing-desk">Writing Desk</a>.</p>',
        _group("Published", blog_store.published(), draft=False,
               empty="No published posts yet."),
    ]
    if can_see_drafts:
        parts.append(_group("Drafts", blog_store.drafts(), draft=True,
                            empty="No drafts."))
    parts.append('</section>')
    return ds.page_shell(
        title="Blog Posts",
        nav_html=ds.nav_component(ROUTE, tier),
        body="".join(parts),
        extra_css=_PAGE_CSS,
    )
