"""Writing Desk tests (epic issue_01M0EHQHQ7M39K811XKX1BAG98).

The blog-drafts panel used to render at the bottom of the Reading Room
(/room, PR #182); this epic moves it to its own top-level page. The panel's
content, its JS, and the __CAN_BLOG_WRITE__ role gate are unchanged — these
are the same three checks that used to live in test_reading_room.py,
retargeted at the new /writing-desk route.
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

import access_identity
import authz
import library_client
import server as srv


@pytest.fixture
def client(tmp_path, monkeypatch):
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "mem@example.com": {"role": "member", "products": ["vivy"]},
        "con@example.com": {"role": "contractor", "products": ["vivy"]},
    }))
    monkeypatch.setattr(authz, "ROLES", roles)
    # The project scope now comes from the Library (E19·1,
    # issue_01M35K54QHJYYPXWS28YWEHJP3), never from roles.json's `products` —
    # stubbed by re-reading the same roles.json file on every call, the way
    # the pre-E19·1 `human_identity` re-read it fresh.
    monkeypatch.setattr(
        library_client, "person_scope",
        lambda email: frozenset(
            json.loads(authz.ROLES.read_text()).get(email, {}).get("products", [])))

    # tiers: only operator is full; mem + con are contractor tier (tier is distinct
    # from role — mem is a "member" by role but not full-tier here). The
    # Writing Desk is reachable by both tiers, same as the Reading Room it
    # split out of.
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": ["operator@example.com"]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)

    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)

    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


def _human(email):
    return {srv.EMAIL_HEADER: email}


def test_writing_desk_reachable_by_both_tiers(client):
    for email in ("operator@example.com", "con@example.com"):
        r = client.get("/writing-desk", headers=_human(email))
        assert r.status_code == 200, email
        assert "Writing Desk" in r.text


def test_writing_desk_nav_marks_current_page(client):
    r = client.get("/writing-desk", headers=_human("operator@example.com"))
    assert r.status_code == 200
    assert 'href="/writing-desk" aria-current="page"' in r.text


def test_blog_panel_visible_for_admin_and_member(client):
    # admin and member both hold blog_write; the panel's cosmetic gate must say
    # so — the __WHOAMI__-style substitution wired in server.py.
    for email in ("operator@example.com", "mem@example.com"):
        r = client.get("/writing-desk", headers=_human(email))
        assert r.status_code == 200
        assert 'data-can-blog="true"' in r.text, email
        assert "__CAN_BLOG_WRITE__" not in r.text, email


def test_blog_panel_hidden_for_contractor(client):
    # con holds only the read-tier bundle, not blog_write.
    r = client.get("/writing-desk", headers=_human("con@example.com"))
    assert r.status_code == 200
    assert 'data-can-blog="false"' in r.text


def test_published_section_renders_and_calls_the_posts_route(client):
    """Issue found 2026-08-20: the page showed only the drafts queue rather than
    what was actually published — this pins that a "Published" panel exists and
    fetches the real listing route."""
    r = client.get("/writing-desk", headers=_human("operator@example.com"))
    assert r.status_code == 200
    assert "Published" in r.text
    assert 'id="publishedList"' in r.text
    assert '"/api/blog/posts"' in r.text


def test_a_published_post_links_to_where_it_actually_lives(client):
    """**The link broke twice before anything pinned it** (reviewer, PR #195).
    It pointed at `lab.convivy.com/field-notes/<slug>.html` — the host publish
    stopped writing to — so an operator clicking through to check a post they
    had just published would land on a 404. Nothing asserted the href, which is
    why it shipped unnoticed and why the fix needed a second round.

    The URL is baked into the returned HTML as literal JavaScript source, so it
    is assertable server-side without executing anything. The `.html` check is
    scoped to this line rather than the whole page: the suffix is the half of
    the bug that survived the first fix attempt, and the page legitimately
    contains the string elsewhere."""
    r = client.get("/writing-desk", headers=_human("operator@example.com"))
    href = next(line for line in r.text.splitlines() if "t.href" in line)

    assert '"https://convivybuilder.org/journal/"' in href
    assert '+ "/";' in href, "the post URL ends with the trailing slash the site requires"
    assert "convivy.com" not in href
    assert "lab.convivy.com" not in href
    assert ".html" not in href


def test_published_section_is_not_gated_by_the_blog_write_role(client):
    # Unlike the drafts panel, a contractor without blog_write must still see
    # the Published section markup — it's a list of already-public content.
    r = client.get("/writing-desk", headers=_human("con@example.com"))
    assert r.status_code == 200
    assert 'id="publishedList"' in r.text


def test_a_published_post_can_be_opened_in_the_editor(client):
    """**The operator hit this on 2026-08-23: "we can't edit existing blog posts now"**
    (issue_01M0R9XR0SFP9360C73P60RR5Q).

    Every backend piece already worked — `GET /api/blog/drafts/{slug}` returns a
    published post (a refusal `reviewer` caught on PR #197) and
    `blog_store.save` writes to a published row on purpose, because correcting a
    live post is the normal path. But the two panels read disjoint queries
    (`drafts()` is `status == DRAFT`, `published()` is `status == PUBLISHED`), so
    a post left the only list carrying an Edit button the instant it went out.

    Asserted on the rendered source rather than by driving a browser: the wiring
    is literal JavaScript in the returned HTML, and the gap was that no such
    wiring existed at all."""
    r = client.get("/writing-desk", headers=_human("operator@example.com"))
    assert r.status_code == 200
    assert "window.__writingDesk.open(p.slug)" in r.text
    assert "window.__writingDesk.open = openEditor" in r.text


def test_the_editor_says_whether_a_post_is_live_rather_than_leaving_it_implied(client):
    """`GET /blog/drafts/{slug}` returns `status` for exactly this, and the
    editor ignored it. A writer who cannot see which they are editing cannot
    know what saving will do."""
    r = client.get("/writing-desk", headers=_human("operator@example.com"))
    assert 'id="blogEditorStatus"' in r.text
    assert 'j.status === "published"' in r.text


def test_the_live_post_warning_says_publish_is_what_reaches_the_journal(client):
    """Under push, nothing reaches Swabby's Journal until something is Published:
    `journal_push.push_corpus` runs only from `publish_draft`. A Save to a live
    post changes what `blog_store.corpus()` holds but does not itself reach the
    journal, so the warning must say that plainly, and say when a publish shows
    up.

    Asserted against the exact rendered sentence. A JS comment earlier in the
    same file quotes the OLD phrasing for historical context, so a loose
    substring match on that phrase alone would pass against the old wording.
    The `\\u2014` check catches a double-escaped character in a JS string."""
    r = client.get("/writing-desk", headers=_human("operator@example.com"))
    assert (
        "Publish sends that change to Swabby's Journal (convivybuilder.org), " in r.text
    )
    assert "where it goes live a couple of minutes later. Save leaves the journal " in r.text
    assert "within the hour" not in r.text
    assert "pushes that change to convivy.com" not in r.text
    assert "\\u2014" not in r.text
    # A warning styled as a success note reads as reassurance, which is the
    # tone the finding was about.
    assert '.room-blog-note.warn' in r.text
    assert 'setNote(editNote, "warn"' in r.text


def test_a_reader_without_blog_write_gets_no_edit_button_on_a_published_post(client):
    """The opener IS the permission check, and the gate is RUNTIME rather than
    server-side: `_BLOG_JS` ships to every viewer identically, and the IIFE
    returns at `section.dataset.canBlog !== "true"` before assigning
    `window.__writingDesk.open`. So the published panel's `if` finds nothing and
    renders no button — one role rule, not a second copy here that could drift.

    Asserted as the two halves that produce that outcome, since a source-level
    "the opener is absent" assertion would be false: the text is there for
    everyone and the data attribute is what decides whether it runs.

    A contractor still gets the published list itself, which is public content;
    what must not appear is a way to edit it."""
    r = client.get("/writing-desk", headers=_human("con@example.com"))
    assert r.status_code == 200
    assert 'id="publishedList"' in r.text
    assert 'data-can-blog="false"' in r.text
    assert 'if (!section || section.dataset.canBlog !== "true") return;' in r.text
    assert "if (window.__writingDesk && window.__writingDesk.open)" in r.text


def test_publishing_refreshes_the_published_panel(client):
    """The two lists render in separate IIFEs. Without the shared handle, an
    edit to a live post left the panel above showing the pre-edit state until a
    reload — the page contradicting the store it had just written to."""
    r = client.get("/writing-desk", headers=_human("operator@example.com"))
    assert "window.__writingDesk.reloadPublished = load" in r.text
    assert "window.__writingDesk.reloadPublished()" in r.text


def test_a_published_row_shows_that_a_live_post_was_edited(client):
    """**This shipped as a claim before it shipped as code** (reviewer, PR #199
    round 1): the description said published rows showed an edited date and no
    such code existed.

    It earns its place because this PR is what makes editing a live post
    possible from this panel — an edit with no mark on the page that made it is
    a change nobody can see happened. `updated` has been on `/api/blog/posts`
    since that route was written and was never rendered.

    Compared by DAY, not timestamp: `blog_store.save` stamps `updated_at` on
    every write, so a post published and corrected the same day would otherwise
    read as "edited" on the day it came out, which tells a reader nothing."""
    r = client.get("/writing-desk", headers=_human("operator@example.com"))
    assert 'const day = function(v) { return v ? v.slice(0, 10) : ""; };' in r.text
    assert "day(p.updated) !== day(p.date)" in r.text
    # Both halves guarded, so a null on either side renders no edited marker
    # rather than "edited " with nothing after it.
    assert "day(p.updated) && day(p.date)" in r.text


def test_the_editor_status_chip_has_styling_to_render_with(client):
    """The chip's classes shipped without their CSS in round 1 (reviewer,
    PR #199), so it rendered as unstyled text. Pinned because the failure is
    invisible to every other assertion here — the markup and the logic were
    both correct and the thing simply looked like nothing."""
    r = client.get("/writing-desk", headers=_human("operator@example.com"))
    assert ".rb-status {" in r.text
    assert ".rb-status.is-live" in r.text


def test_publish_saves_the_editor_before_publishing(client):
    """**The operator lost an edit to a live post on 2026-08-24 this way**
    (issue_01M0T979V44QGHSXD2TF5CP2G8). Publish sent no body at all, so it
    republished whatever the store held and silently discarded anything typed
    since the last Save — then reported "Published." The published revision's
    body hash came back byte-identical to the version imported the day before.

    Asserted on the rendered source because the wiring is literal JavaScript in
    the page and the defect was that no such wiring existed: the publish
    handler's fetch had no `body` key anywhere in it."""
    r = client.get("/writing-desk", headers=_human("operator@example.com"))
    assert r.status_code == 200
    js = r.text
    # One save request, shared by both buttons, so Publish cannot drift back
    # into sending a different body from the one on screen.
    assert "function saveEditor(slug)" in js
    assert js.count("function saveEditor(slug)") == 1
    # Publish goes through it before it reaches the publish route.
    publish_block = js.split('publishBtn.addEventListener')[1]
    assert "saveEditor(slug)" in publish_block
    assert publish_block.index("saveEditor(slug)") < publish_block.index("/publish")


def test_a_failed_save_does_not_publish(client):
    """Publishing over a failed save would put the OLD body on the public site
    under a success message — the same failure being fixed, wearing a different
    hat. The publish fetch is chained inside `saveEditor().then(...)`, so a
    rejected save skips it and lands in the catch."""
    js = client.get("/writing-desk", headers=_human("operator@example.com")).text
    publish_block = js.split('publishBtn.addEventListener')[1].split('closeBtn')[0]
    assert "saveEditor(slug).then(function() {" in publish_block
    # The two failures are reported apart, because they call for different
    # actions: an unsaved edit lives only in the browser, a failed publish
    # after a good save does not (reviewer, PR #201).
    assert "Couldn't save, so nothing was published: " in publish_block
    assert "Saved, but publishing failed: " in publish_block


def test_the_live_post_warning_does_not_single_out_publish_as_the_safe_button(
        client):
    """The round-2 copy on PR #199 said Publish "only asks the site to rebuild
    sooner" — true of a Publish that sent no body, and it pointed a reader who
    wanted their change live at the one control that threw it away.

    Now that both buttons write what is on screen, the copy says that instead of
    distinguishing them."""
    js = client.get("/writing-desk", headers=_human("operator@example.com")).text
    assert "Either button" in js
    assert "only asks the site to rebuild sooner" not in js
    assert "no draft copy" in js


def test_publish_uses_the_slug_captured_at_click_not_the_live_variable(client):
    """**A race this PR's own fix introduced** (reviewer, PR #201).

    `currentSlug` is reassigned by `openEditor`, and the drafts list stays
    clickable while a publish is in flight — only Save and Publish are disabled.
    Before this PR the publish fetch fired synchronously off one read of that
    variable, so there was no window. Awaiting the save ahead of it opened one:
    open post A, click Publish, click Edit on post B while A's save is in
    flight, and the publish URL built afterwards would name B — publishing B
    under a click made against A, and reporting success.

    So the handler captures `const slug = currentSlug` at click time and every
    later step uses it, including the save, which now takes it as a parameter
    rather than reading the variable itself."""
    js = client.get("/writing-desk", headers=_human("operator@example.com")).text
    publish_block = js.split('publishBtn.addEventListener')[1].split('closeBtn')[0]
    assert "const slug = currentSlug;" in publish_block
    # The publish URL is built from the capture, never from the live variable.
    assert 'encodeURIComponent(slug) + "/publish"' in publish_block
    assert "encodeURIComponent(currentSlug)" not in publish_block
    # And the save it chains through takes the slug rather than reading it.
    assert "function saveEditor(slug)" in js
    assert "encodeURIComponent(currentSlug)" not in js


def test_a_completed_publish_does_not_stamp_a_different_post_now_in_the_editor(
        client):
    """If the reader moved on to another post while the publish was in flight,
    the success note and the "published" chip must not land on that post's
    editor — it would report a state that is not true of what is on screen."""
    js = client.get("/writing-desk", headers=_human("operator@example.com")).text
    publish_block = js.split('publishBtn.addEventListener')[1].split('closeBtn')[0]
    assert "if (currentSlug === slug) {" in publish_block


def test_hidden_wins_over_the_panels_display_rules(client):
    """The editor and the new-post form are `display:flex` AND are shown and
    hidden through the `hidden` attribute. An author `display` declaration beats
    the UA stylesheet's `[hidden]{display:none}` outright, so without an explicit
    `[hidden]` rule the attribute is inert and both panels render on page load.

    That is not cosmetic. `openEditor` is the only thing that sets
    `currentSlug`, and it is also the only thing that reveals the editor — so a
    visible editor over a null `currentSlug` puts Save and Publish on their
    `if (!slug) return;` guard, where both do nothing at all: no request, no
    error message, no draft. The operator hit exactly that on 2026-09-07 and reported
    that neither button had any effect.

    Asserted as a relationship rather than a string match: for every class that
    the page both gives a `display:` and hides with the attribute, the `[hidden]`
    override has to exist.
    """
    r = client.get("/writing-desk", headers=_human("operator@example.com"))
    assert r.status_code == 200
    page = r.text

    # The two panels really are hidden-by-attribute and display-styled.
    assert 'id="blogEditor" hidden' in page
    assert 'id="blogNewForm" hidden' in page
    assert re.search(r"\.room-blog-newform,\s*\.room-blog-editor\s*\{[^}]*display:\s*flex",
                     page), "panels no longer display:flex — re-check this guard still applies"

    # ...so the override must be present, and must carry !important, since the
    # class rule would otherwise win on the cascade regardless of order.
    m = re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important", page)
    assert m, (
        "no `[hidden]{display:none!important}` rule on /writing-desk: the hidden "
        "attribute is inert on the editor and the new-post form, so both render "
        "on load and Save/Publish silently no-op against a null currentSlug"
    )


def test_a_publish_that_never_reached_the_journal_warns_rather_than_says_published(client):
    """The publish route answers 200 once the store holds the post, and the
    journal push is best-effort, so a push that wrote nothing (`journal.corpus_sha`
    null: no credential, or GitHub refused) must not read "Published." under copy
    that promises the post goes live (reviewer, PR #355)."""
    r = client.get("/writing-desk", headers=_human("operator@example.com"))
    assert "journal.corpus_sha == null" in r.text
    assert "Published in Orient, but it didn't reach Swabby's Journal (" in r.text
    assert 'setNote(editNote, "warn",\n            "Published in Orient' in r.text


def test_a_publish_says_whether_the_site_build_started(client):
    """A new commit starts Shipwright's site build. When it started, the post is
    a couple of minutes from live; when it didn't, the post waits for the site's
    next scheduled run, and "Published." alone would hide that. Publishing again
    cannot help, because an unchanged corpus makes no commit, so the warning
    names the scheduled build instead. It names no time: the schedule is
    Shipwright's to change."""
    r = client.get("/writing-desk", headers=_human("operator@example.com"))
    assert "const build = journal && journal.site_build;" in r.text
    assert "} else if (build && build.dispatched) {" in r.text
    assert "Published. Swabby's Journal is updating; that takes a couple of minutes." in r.text
    assert 'setNote(editNote, "warn",\n            "Published, but the journal\'s site build didn\'t start ("' in r.text
    assert "The post goes live with the site's next scheduled build." in r.text
    assert "06:17" not in r.text
