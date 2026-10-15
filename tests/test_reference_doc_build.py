"""Tests for reference_doc_build.py — /reference/doc/{id}
(issue_01M08YBCDTEFJ6N9S798ERF89P step 3; issue_01M0TKX6DW2E1TXGN9QT6K61J5 §3).

Mocks `library_client.get_reference_document` directly (no fake-Library
double for `/reference/*` exists yet in `tests/library_fake.py`), matching
how `test_library_client.py` verifies the client's own contract separately
from how a builder uses it.
"""
from __future__ import annotations

import json
import re

import library_client
import reference_doc_build


def _doc(*, doc_id="proj/kind/doc-1", title="A Document", audience=None,
         body="# A Document\n\nSome body text.", status="current",
         project="proj", kind="reference", topics=None,
         last_reviewed="2026-08-01", source_ref="projects/proj/kind/doc-1.md"):
    return library_client.ReferenceDocument(
        id=doc_id, source_ref=source_ref, kind=kind, project=project, title=title,
        status=status, topics=topics if topics is not None else [],
        audience=audience if audience is not None else ["team"],
        last_reviewed=last_reviewed, updated_at=None, body=body)


def test_a_team_audience_document_renders(monkeypatch):
    # get_reference_document now returns None for a non-team document itself
    # (the gate moved into library_client, PR #205 review), so the builder sees
    # exactly what it sees for a missing one.
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: _doc())
    html = reference_doc_build.detail_html("proj/kind/doc-1", frozenset(), True)
    assert html is not None
    assert "A Document" in html
    assert "Some body text." in html


def test_the_page_shows_project_kind_status_topics_and_last_reviewed(monkeypatch):
    doc = _doc(status="draft", topics=["security", "keel"])
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: doc)
    html = reference_doc_build.detail_html("proj/kind/doc-1", frozenset(), True)
    assert "proj" in html
    assert "reference" in html
    assert "draft" in html
    assert "security" in html and "keel" in html
    assert "2026-08-01" in html
    assert "projects/proj/kind/doc-1.md" in html


def test_an_unknown_id_is_none(monkeypatch):
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: None)
    assert reference_doc_build.detail_html("proj/kind/missing", frozenset(), True) is None


# --- scope (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) -------------------------


def test_a_document_outside_the_viewers_scope_is_none(monkeypatch):
    """A document id can be guessed or enumerated (the module docstring's own
    reasoning for the audience check) — so a scoped viewer who is not
    entitled to "proj" gets the same answer as a missing id, not the
    document."""
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: _doc())
    assert reference_doc_build.detail_html("proj/kind/doc-1", frozenset({"other"}), False) is None


def test_a_document_inside_the_viewers_scope_still_renders(monkeypatch):
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: _doc())
    html = reference_doc_build.detail_html("proj/kind/doc-1", frozenset({"proj"}), False)
    assert html is not None
    assert "A Document" in html


def test_a_whole_fleet_viewer_skips_the_scope_check(monkeypatch):
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: _doc())
    html = reference_doc_build.detail_html("proj/kind/doc-1", frozenset(), True)
    assert html is not None


def test_the_page_asks_for_the_team_audience_and_nothing_else(monkeypatch):
    """The entitlement check itself now lives in `library_client` — see
    `test_get_reference_document_gates_on_audience` there, which exercises it
    against the real function rather than a stub. What this page is still
    responsible for is ASKING for the right audience, and a stub cannot catch
    that being wrong, so it is asserted directly."""
    seen = {}

    def fake(doc_id, *, audience):
        seen["audience"] = audience
        return _doc()

    monkeypatch.setattr(library_client, "get_reference_document", fake)
    reference_doc_build.detail_html("proj/kind/doc-0001", frozenset(), True)
    assert seen["audience"] == "team"


def test_a_gated_document_reads_exactly_like_a_missing_one(monkeypatch):
    """`get_reference_document` collapses "no such document" and "not your
    audience" into None, so this page cannot tell them apart even in principle
    — which is the point: a distinguishable "forbidden" lets a guessed id
    confirm a document exists."""
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: None)
    assert reference_doc_build.detail_html("proj/kind/agent-doc", frozenset(), True) is None


def test_a_library_failure_renders_error_state_not_a_traceback(monkeypatch):
    def _boom(doc_id, **k):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "get_reference_document", _boom)
    html = reference_doc_build.detail_html("proj/kind/doc-1", frozenset(), True)
    assert html is not None
    assert "error-state" in html
    assert "RuntimeError" in html


def test_the_clean_body_policy_strips_frontmatter_and_provenance(monkeypatch):
    """`build._clean_body` is the site's one policy for what a document
    SHOWS a reader — strip leading frontmatter, drop any `## Provenance`
    section — and the rendered `doc-content` region must apply it exactly
    like `/reference` and `/room` do, or it disagrees with the rest of the site
    for the third of the corpus that carries a provenance heading.

    **The editor is a different surface, and it is not seeded from this page.**
    (issue_01M0TKX6DW2E1TXGN9QT6K61J5 §4): it opens on the stored file the
    checkout route returns, so a save round-trips the whole file, including the
    section the reading view hides. The page therefore embeds no document text
    for it at all, and the hidden section appears nowhere in the HTML."""
    body = ("---\ntitle: A Document\n---\n"
            "# A Document\n\nVisible text.\n\n"
            "## Provenance\n\nHidden text that must not render.\n")
    doc = _doc(body=body)
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: doc)
    html = reference_doc_build.detail_html("proj/kind/doc-1", frozenset(), True)
    content_start = html.index('<div class="doc-content">')
    content_end = html.index('</div>', content_start)
    display = html[content_start:content_end]
    assert "Visible text." in display
    assert "Hidden text that must not render." not in display
    assert "title: A Document" not in display  # the raw frontmatter line itself
    assert "Hidden text that must not render." not in html


def test_the_body_is_rendered_as_markdown_not_escaped_source(monkeypatch):
    doc = _doc(body="# Heading\n\nSome **bold** text.")
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: doc)
    html = reference_doc_build.detail_html("proj/kind/doc-1", frozenset(), True)
    assert "<h1>Heading</h1>" in html
    assert "<strong>bold</strong>" in html


def test_the_page_ships_the_shared_editor_controller(monkeypatch):
    """The editor moved to `kb_editor.py` when the browse page started offering
    one too (issue_01M17HYDBQ4GPWHPFS56M6QQPH). What this page still has to do
    is SHIP it: the trigger below calls `window.KbEditor.open`, so a page that
    emitted the button without the controller would answer a click with a
    ReferenceError.

    What the controller then does — seeding the draft from the checkout,
    posting the checkout's sha back with the save, releasing the lock — is
    tested against the running code in `test_kb_editor.py`, not against this
    page's HTML."""
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc())
    html = reference_doc_build.detail_html("proj/kind/doc-1", frozenset(), True)
    assert "window.KbEditor = (function" in html, "the controller itself must be on the page"
    assert "window.KbEditor.open(mount, data.docId" in html
    controller = html.index("window.KbEditor = (function")
    assert html.index("window.KbEditor.open(mount, data.docId") > controller, (
        "the controller has to be defined before the trigger that calls it")


def test_the_editor_has_no_draft_only_mode(monkeypatch):
    """Opening the editor reads the file through `agentctl`, so a deployment
    without it answers Edit with a 502 and never opens a box to type into. The
    "saving isn't wired up, copy what you want to keep" banner, and the
    `data-save-configured` attribute that gated it, could not fire and are
    gone."""
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **k: _doc())
    html = reference_doc_build.detail_html("proj/kind/doc-0001", frozenset(), True)
    assert "data-save-configured" not in html
    assert "saveConfigured" not in html
    assert "copy anything you want to keep" not in html


def test_the_editor_affordance_carries_the_can_kb_write_placeholder(monkeypatch):
    """`__CAN_KB_WRITE__` is server.py's job to substitute (matching
    `__CAN_BLOG_WRITE__`) — this page's job is to emit the placeholder and the
    gated section, not to know the viewer's role."""
    doc = _doc()
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: doc)
    html = reference_doc_build.detail_html("proj/kind/doc-1", frozenset(), True)
    assert 'data-can-kb-write="__CAN_KB_WRITE__"' in html
    assert 'id="kbEditBtn"' in html


def test_the_editor_box_is_not_on_the_page_until_edit_is_clicked(monkeypatch):
    """The third instance of the drawer's cascade defect used to live here:
    `.kb-editor-box` sets `display:flex` and the markup shipped `hidden`, so
    before the shared `[hidden]` rule the editor rendered open on every
    reference doc page for anyone who can write.

    **The box is now built by the controller when Edit is clicked, so the
    cascade has nothing to get wrong** — there is no box in the served HTML at
    all. The `[hidden]` rule still has to hold, because the controller hides the
    textarea and the Save button with that attribute over `.kb-editor-box`'s own
    `display:flex` children."""
    doc = _doc()
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: doc)
    html = reference_doc_build.detail_html("proj/kind/doc-1", frozenset(), True)
    editor_section = html[html.index('<section class="kb-editor"'):
                          html.index("</section>", html.index('<section class="kb-editor"'))]
    assert "<textarea" not in editor_section, (
        "the textarea exists only once a checkout has been taken")
    assert 'id="kbEditorMount"></div>' in editor_section, (
        "the controller needs an empty element to paint into")
    # Scoped to `.kb-editor-box`'s own block: a bare `"display: flex" in html`
    # passes on `.doc-meta`, which every render of this page emits, so it would
    # hold even with the declaration under test deleted.
    box_rule = re.search(r"\.kb-editor-box \{[^}]*\}", html)
    assert box_rule and "display: flex" in box_rule.group(0)
    assert "[hidden] { display: none !important; }" in html


def _editor_payload(html: str) -> dict:
    m = re.search(r'<script id="kbEditorData" type="application/json">(.*?)</script>', html, re.S)
    assert m, "the editor's data block is missing"
    return json.loads(m.group(1))


def test_the_editor_payload_carries_the_doc_id_and_no_body(monkeypatch):
    """The editor is filled from the checkout response, which returns the
    stored file with its frontmatter. `doc.body` has the frontmatter stripped,
    so an editor seeded from it could never save."""
    doc = _doc(doc_id="proj/kind/doc-9", body="# Heading\n\nUnique-body-marker text.")
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: doc)
    html = reference_doc_build.detail_html("proj/kind/doc-9", frozenset(), True)
    assert _editor_payload(html) == {"docId": "proj/kind/doc-9"}
    # The reader still gets the document, rendered, so the marker is on the
    # page. It must not also ride in the editor's data block.
    assert "Unique-body-marker" in html
    data_block = re.search(r'id="kbEditorData".*?</script>', html, re.S).group(0)
    assert "Unique-body-marker" not in data_block


def test_the_editor_fills_from_the_checkout_response_not_the_page(monkeypatch):
    """The page hands the controller an id and nothing else. That the
    controller then fills the textarea from the checkout response is asserted
    against the running code in
    `test_kb_editor.test_opening_checks_the_document_out_and_seeds_the_draft_from_it`;
    what this page must not do is offer it a body to use instead."""
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: _doc())
    html = reference_doc_build.detail_html("proj/kind/doc-1", frozenset(), True)
    assert "data.body" not in html


def test_the_editor_payload_escapes_script_close_tags(monkeypatch):
    """The house convention for embedding text in a `<script>` block
    (`engineering_build.render`'s own `.replace(\"</\", \"<\\\\/\")`), kept for
    the one value still embedded. A `</script>` in it must not close the data
    block early."""
    doc = _doc(doc_id="proj/</script><script>alert(1)</script>/doc")
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: doc)
    html = reference_doc_build.detail_html("proj/kind/doc-1", frozenset(), True)
    assert "</script><script>alert(1)</script>" not in html
    assert "<\\/script><script>alert(1)<\\/script>" in html
    assert _editor_payload(html) == {"docId": "proj/</script><script>alert(1)</script>/doc"}


def test_a_title_written_as_markup_is_escaped(monkeypatch):
    doc = _doc(title="<script>alert(1)</script>")
    monkeypatch.setattr(library_client, "get_reference_document", lambda doc_id, **k: doc)
    html = reference_doc_build.detail_html("proj/kind/doc-1", frozenset(), True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
