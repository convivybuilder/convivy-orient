"""Blog Posts: every Writing Desk post, readable in full (the operator, 2026-09-24).

Rendered against a real control.db, so the page reads the same store the Writing
Desk writes and convivy.com's corpus serves, rather than a stub of it.
"""
from __future__ import annotations

import pytest

import blog_posts_build
import blog_store
import control_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    yield


def _seed():
    blog_store.publish(slug="live-one", title="A Live Post", body="PUBLISHEDBODY **bold**",
                       author="operator@example.com", authorship="human", actor="operator@example.com")
    blog_store.create(slug="draft-one", title="A Draft", body="DRAFTBODY",
                      author="operator@example.com", authorship="llm")


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
    blog_store.publish(slug="xss", title="<script>alert(1)</script>",
                       body="<img src=x onerror=alert(2)>", author="The operator",
                       authorship="human", actor="operator@example.com")
    html = blog_posts_build.live_html("operator@example.com", can_see_drafts=False)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "onerror=alert(2)>" not in html


def test_a_byline_with_markup_characters_is_escaped_exactly_once(db):
    """A non-email author passes through `_display_author` unchanged, so an `&`
    or a quote reaches the byline. Escaping it twice would print `&amp;amp;`."""
    blog_store.publish(slug="duo", title="T", body="b", author="The operator & O'Brien <x>",
                       authorship="human", actor="operator@example.com")
    html = blog_posts_build.live_html("operator@example.com", can_see_drafts=False)
    assert "The operator &amp; O&#x27;Brien &lt;x&gt;" in html
    assert "&amp;amp;" not in html and "<x>" not in html


def test_it_is_a_documents_subnav_entry():
    import design_system as ds
    assert (blog_posts_build.ROUTE, "Blog Posts") in ds._DOCUMENTS_SUBNAV
