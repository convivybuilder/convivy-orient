#!/usr/bin/env python3.11
"""/reference/doc/{id} — one knowledge-base document, its own URL, read
through the Library (issue_01M08YBCDTEFJ6N9S798ERF89P step 3;
issue_01M0TKX6DW2E1TXGN9QT6K61J5 §3).

Before this page existed, a document had no address: Home's recent-docs panel
deep-linked into `/reference#doc=<id>`, which loads `/reference`'s whole 10 MB
bundle (all 694 bodies) before the drawer can open. This page fetches exactly
one document — `GET /reference/documents/{id}` — and renders it server-side,
with no dependency on the KB-browse bundle at all.

**Full-tier only, like `/reference`** (server.py gates the `/reference/doc/`
prefix the same way it gates the `/archives/`, `/engineering/issues/` and
`/projects/` prefixes). `/reference` is not touched — its own bundle, its own
drawer, its own 10 MB payload all keep working exactly as they do today; that
rebuild is a separate, larger change this dispatch deliberately leaves alone.

**The audience check is the security boundary, not the tier gate alone.**
`build.load_docs()` and `kb_index.team_docs()` both skip any document whose
`audience` does not include `"team"` — everything else in the KB is
LLM-facing (an agent/skill prompt, a role definition) and has never been
shown on the human site, full tier included. A document id can be guessed or
enumerated (`GET /reference/documents` lists every id), so this page applies
the same `"team" not in audience` skip before rendering anything, treating a
non-team document exactly like a missing one (404) rather than distinguishing
the two — the same no-existence-leak `get_issue_item`/`get_signal` already
use for out-of-scope rows. A contractor never reaches this code at all (the
tier gate 404s first, in server.py); full tier is where `audience` is the
only wall left, and it is the one this page enforces itself.

**`_clean_body` applies the site's own display policy** — strip frontmatter,
drop any section headed `provenance` — the same function `build.py` and
`kb_index.py` already call, so a document renders identically here and on
`/reference`/`/room`. 234 of 694 documents carried a provenance heading when
last measured; skipping this would disagree with the rest of the site for a
third of the corpus.

**A Library failure renders `ds.error_state` inside the page shell, not a
bare 500.** This page's whole content IS the one Library read (there is
nothing else on it to preserve), so degrading the page rather than a section
of it is correct — but `server.py`'s generic per-id fallback is an unstyled
`<h1>500</h1>` with no nav, which is what "a blank" means below. Catching the
failure here instead means a reader who hits a Library blip still gets the
site chrome and a legible message, not a stub page. A bug in this module
itself (not a Library failure) still propagates to server.py's fallback,
matching every other per-id page — only the Library call is caught."""
from __future__ import annotations

import json
from html import escape

import design_system as ds
import kb_editor
import library_client
from markdown_render import md_to_html
from build import _clean_body


def _meta_row(doc: "library_client.ReferenceDocument") -> str:
    badges = [f'<span class="doc-tag proj">{escape(doc.project)}</span>',
              f'<span class="doc-tag">{escape(doc.kind)}</span>']
    if doc.status != "current":
        badges.append(f'<span class="doc-tag draft">{escape(doc.status)}</span>')
    for topic in doc.topics:
        badges.append(f'<span class="doc-tag topic">{escape(str(topic))}</span>')
    meta_bits = []
    if doc.last_reviewed:
        meta_bits.append(f"last reviewed {escape(str(doc.last_reviewed))}")
    if doc.source_ref:
        meta_bits.append(f'<code>{escape(doc.source_ref)}</code>')
    meta_line = f'<div class="doc-submeta">{" · ".join(meta_bits)}</div>' if meta_bits else ""
    return f'<div class="doc-meta">{"".join(badges)}</div>{meta_line}'


def _editor_section(doc: "library_client.ReferenceDocument") -> str:
    """The edit affordance (issue_01M0TKX6DW2E1TXGN9QT6K61J5 §4, "option C").

    **The editor itself lives in `kb_editor.py` and is shared with the browse
    page**, which now mounts the same box in its inline inset and its drawer
    (issue_01M17HYDBQ4GPWHPFS56M6QQPH). What stays here is this page's own
    trigger: the Edit button, the document id, and the element the controller
    paints into. The box, the textarea, the buttons and the whole
    checkout/heartbeat/save/checkin protocol are the shared controller's.

    **The page embeds no document text for the editor.** It carries only the
    document id; the editor is filled from the checkout response, which returns
    the stored file with its frontmatter. `doc.body` cannot seed it, because the
    Library serves that with the frontmatter stripped and a save must submit the
    whole file (see `kb_write_api.checkout_document`). Embedding the body as
    well would ship every document's text twice to every reader, including the
    ones who cannot edit. The id is a JSON payload with `</` escaped to `<\\/`
    the same way `engineering_build.render` embeds text into a page — the house
    convention for this exact hazard, not a new one.

    **There is no "saving is not configured" state.** Whether a save can land
    used to be embedded here (a `kb_commit` check that `agentctl` resolved) so
    a deployment without `agentctl` opened a draft-only editor. Opening the
    editor now needs
    `agentctl` to read the file, so that deployment answers Edit with a 502
    before any draft exists and the banner could never show.

    `data-can-kb-write` is the `__CAN_KB_WRITE__` substitution `server.py`
    applies from the viewer's role, matching `__CAN_BLOG_WRITE__`: cosmetic,
    since every route the buttons call enforces its own gate regardless."""
    payload = json.dumps({"docId": doc.id},
                         ensure_ascii=False).replace("</", "<\\/")
    return f'''
<section class="kb-editor" id="kbEditor" data-can-kb-write="__CAN_KB_WRITE__">
  <script id="kbEditorData" type="application/json">{payload}</script>
  <div class="kb-editor-bar">
    <button type="button" class="kb-edit-btn" id="kbEditBtn">Edit</button>
  </div>
  <div id="kbEditorMount"></div>
</section>
<script>{kb_editor.EDITOR_JS}</script>
<script>{_PAGE_JS}</script>'''


# This page holds one document and shows the editor under it, so the trigger is
# all it owns. Hiding the Edit button while the editor is open, and restoring it
# from `onClosed`, is the whole of the page-specific interaction — every other
# close path (Save, the lock lapsing, leaving the page) runs through the shared
# controller and comes back out here.
_PAGE_JS = r"""
(function () {
  var section = document.getElementById("kbEditor");
  if (!section || section.dataset.canKbWrite !== "true") return;
  var data = JSON.parse(document.getElementById("kbEditorData").textContent);
  var editBtn = document.getElementById("kbEditBtn");
  var mount = document.getElementById("kbEditorMount");
  editBtn.addEventListener("click", function () {
    editBtn.hidden = true;
    window.KbEditor.open(mount, data.docId, {
      onClosed: function () { editBtn.hidden = false; },
    });
  });
})();
"""


def _doc_page(doc: "library_client.ReferenceDocument") -> str:
    content = md_to_html(_clean_body(doc.body or ""))
    body = f'''<div class="doc-detail">
  <header class="doc-head">
    <a class="back" href="/reference">← Documents</a>
    <h1>{escape(doc.title)}</h1>
    {_meta_row(doc)}
  </header>
  <div class="doc-content">{content}</div>
  {_editor_section(doc)}
</div>'''
    return ds.page_shell(
        title=doc.title,
        nav_html=ds.nav_component("/reference", "full"),
        body=body,
        extra_css=_CSS,
    )


def _error_page(reason: str) -> str:
    body = f'''<div class="doc-detail">
  <header class="doc-head">
    <a class="back" href="/reference">← Documents</a>
    <h1>This document is unavailable</h1>
  </header>
  {ds.error_state(reason)}
</div>'''
    return ds.page_shell(
        title="document unavailable",
        nav_html=ds.nav_component("/reference", "full"),
        body=body,
        extra_css=_CSS,
    )


# The human site shows team-audience documents and nothing else, the rule
# `build.load_docs` and `kb_index.team_docs` already apply. Named here rather
# than passed as a literal so the one place this page decides who it serves is
# greppable.
_AUDIENCE = "team"


def detail_html(doc_id: str) -> str | None:
    """Render one document's page, or None when there is no such document —
    including a document that exists in the Library but is not `"team"`
    audience, which this page treats identically to a missing one.

    **The entitlement check is `library_client`'s, not this page's.**
    `get_reference_document` takes a required `audience` and returns None for
    a document outside it, so the two cases collapse before they reach here.
    It moved there because the Library serves the whole corpus unfiltered and
    the gate had lived only in this one page — meaning the next page built on
    those functions would have had to know to rebuild it (PR #205 review).

    A Library failure does not propagate: it is caught here and rendered as
    `ds.error_state` inside the normal page shell, in keeping with this being
    a single-purpose page whose whole content is the one Library read it
    makes. A bug in this function itself is not caught and reaches
    server.py's generic per-id fallback, same as every other detail page."""
    try:
        doc = library_client.get_reference_document(doc_id, audience=_AUDIENCE)
    except Exception as exc:
        return _error_page(
            f"The Library could not be reached ({type(exc).__name__}). "
            f"Try again shortly.")
    if doc is None:
        return None
    return _doc_page(doc)


# ---------------------------------------------------------------------------
# Page-local CSS — mirrors engineering_build's `.eng-doc-content` markdown
# typography for parity with the /memory drawer's rendered output, under this
# page's own class names rather than importing engineering_build's CSS
# wholesale (which also carries sidebar/pill rules this page has no elements
# for).
# ---------------------------------------------------------------------------

_CSS = r"""
.doc-detail { max-width: 760px; margin: 0 auto; padding: 8px 0 40px; }
.doc-head { margin-bottom: 20px; }
.doc-head h1 { font-size: 26px; font-weight: 700; letter-spacing: -.01em; margin: 6px 0 12px; }
.back { display: inline-block; font-size: 13px; font-weight: 700; color: var(--brand-ink); text-decoration: none; }
.back:hover { text-decoration: underline; }
.doc-meta { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.doc-tag {
  background: var(--surface-2);
  color: var(--ink-2);
  font-size: 11px;
  font-weight: 600;
  padding: 2px 9px;
  border-radius: 999px;
}
.doc-tag.proj { background: var(--brand-wash); color: var(--brand-ink); }
.doc-tag.draft { background: var(--brand-wash); color: var(--brand-ink); }
.doc-tag.topic { background: var(--surface-2); color: var(--ink-3); }
.doc-submeta { color: var(--ink-3); font-size: 12px; margin-top: 8px; }

.doc-content { max-width: 100%; overflow-wrap: anywhere; margin-top: 22px; }
.doc-content h1:first-child { margin-top: 0; }
.doc-content h1, .doc-content h2 { letter-spacing: -.01em; color: var(--ink); }
.doc-content h1 { font-size: 22px; font-weight: 700; }
.doc-content h2 { font-size: 17px; font-weight: 650; }
.doc-content h3 { font-size: 14px; font-weight: 650; }
.doc-content p { margin: 12px 0; }
.doc-content a { color: var(--brand-ink); text-underline-offset: 2px; }
.doc-content pre {
  background: var(--surface-2);
  border: 1px solid var(--line);
  padding: 14px;
  border-radius: var(--radius);
  overflow-x: auto;
  max-width: 100%;
  font-size: 13px;
  font-family: var(--mono);
}
.doc-content img { max-width: 100%; height: auto; }
[data-theme="dark"] .doc-content pre { background: #0E0C09; border-color: var(--line); color: #E2DAD0; }
.doc-content code {
  background: var(--brand-wash);
  color: var(--brand-ink);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 12.5px;
  font-family: var(--mono);
}
.doc-content pre code { background: none; padding: 0; color: inherit; }
.doc-content table { display: block; overflow-x: auto; max-width: 100%; border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 13.5px; }
.doc-content th, .doc-content td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; }
.doc-content th { background: var(--surface-2); font-weight: 650; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; color: var(--ink-2); }
.doc-content blockquote {
  border-left: 3px solid var(--brand);
  background: var(--brand-wash);
  border-radius: 0 var(--radius) var(--radius) 0;
  margin: 14px 0;
  padding: 8px 16px;
  color: var(--ink-2);
}
""" + kb_editor.EDITOR_CSS
