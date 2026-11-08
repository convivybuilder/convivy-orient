"""Blog Posts: every Writing Desk post, readable in full (the operator, 2026-09-24).

Rendered against `library_fake`, seeded with the same Archives entries the
Writing Desk writes (`blog_api`), so the page reads what the desk stores.
"""
from __future__ import annotations

import pytest

import blog_api
import blog_posts_build
import library_client
import library_fake


@pytest.fixture
def db(monkeypatch):
    library_fake.patch(monkeypatch)
    yield


def _entry(slug, *, title, body, author="operator@example.com", authorship="human",
           kind=blog_api.KIND_POST, revision=1):
    library_client.create_signal(
        source=blog_api.SOURCE, kind=kind, actor=author, product=blog_api.PRODUCT,
        stream=slug, body=body[:200], content=body,
        meta={"title": title, "revision": revision, "authorship": authorship,
              "reason": "publish" if kind == blog_api.KIND_POST else "create"},
        dedup_key=f"post:{slug}:{revision}",
        human_reserved=(kind == blog_api.KIND_DRAFT))


def _seed():
    _entry("live-one", title="A Live Post", body="PUBLISHEDBODY **bold**")
    _entry("draft-one", title="A Draft", body="DRAFTBODY", authorship="llm",
           kind=blog_api.KIND_DRAFT)


def test_a_published_post_renders_in_full_with_its_byline(db):
    _seed()
    html = blog_posts_build.live_html("operator@example.com", can_see_drafts=False)
    assert "A Live Post" in html
    assert "PUBLISHEDBODY <strong>bold</strong>" in html
    # The byline is the display name, never the attested email.
    assert "Operator" in html and "operator@example.com" not in html.split("<main", 1)[-1]


def test_drafts_are_shown_only_when_the_viewer_may_see_them(db):
    """The drafts decision is the server's, so a viewer without it receives no
    draft text and no Drafts heading at all, not a hidden one."""
    _seed()
    writer = blog_posts_build.live_html("operator@example.com", can_see_drafts=True)
    reader = blog_posts_build.live_html("operator@example.com", can_see_drafts=False)
    assert "DRAFTBODY" in writer and ">Drafts<" in writer
    assert "written by a model" in writer
    assert "DRAFTBODY" not in reader and ">Drafts<" not in reader
    assert "A Draft" not in reader


def test_the_page_says_posts_can_be_published_elsewhere(db):
    html = blog_posts_build.live_html("operator@example.com", can_see_drafts=True)
    assert "publish elsewhere" in html
    assert 'href="/writing-desk"' in html


def test_an_empty_store_says_so_rather_than_rendering_nothing(db):
    html = blog_posts_build.live_html("operator@example.com", can_see_drafts=True)
    assert "No published posts yet." in html
    assert "No drafts." in html


def test_a_title_and_body_written_as_markup_are_escaped(db):
    _entry("xss", title="<script>alert(1)</script>", body="<img src=x onerror=alert(2)>",
           author="The operator")
    html = blog_posts_build.live_html("operator@example.com", can_see_drafts=False)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "onerror=alert(2)>" not in html


def test_a_byline_with_markup_characters_is_escaped_exactly_once(db):
    """A non-email author passes through `_display_author` unchanged, so an `&`
    or a quote reaches the byline. Escaping it twice would print `&amp;amp;`."""
    _entry("duo", title="T", body="b", author="The operator & O'Brien <x>")
    html = blog_posts_build.live_html("operator@example.com", can_see_drafts=False)
    assert "The operator &amp; O&#x27;Brien &lt;x&gt;" in html
    assert "&amp;amp;" not in html and "<x>" not in html


def test_the_latest_revision_is_shown_and_a_reader_causes_no_draft_read(db, monkeypatch):
    """A post saved twice shows its latest text. A viewer without drafts makes
    the page ask the Library for no `post_draft` row, on any credential."""
    _entry("twice", title="Twice", body="FIRSTTEXT", revision=1)
    _entry("twice", title="Twice", body="SECONDTEXT", revision=2)
    _seed()
    asked = []
    real = library_client.list_signals

    def spy(**kw):
        asked.append(kw)
        return real(**kw)

    monkeypatch.setattr(library_client, "list_signals", spy)
    html = blog_posts_build.live_html("operator@example.com", can_see_drafts=False)
    assert "SECONDTEXT" in html and "FIRSTTEXT" not in html
    assert asked and all(blog_api.KIND_DRAFT not in (kw.get("kind") or []) for kw in asked)
    assert not any(kw.get("human_reserved") for kw in asked)


def test_it_is_a_documents_subnav_entry():
    import design_system as ds
    assert (blog_posts_build.ROUTE, "Blog Posts") in ds._DOCUMENTS_SUBNAV
