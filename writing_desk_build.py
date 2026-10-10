"""The Writing Desk renderer — the lab blog authoring page (epic
issue_01M0EHQHQ7M39K811XKX1BAG98).

The blog-drafts panel (ARCH-363 UI) used to render at the bottom of the
Reading Room (PR #182). This is a UI relocation only: the panel's markup, its
JS, and the blog_api.py routes it calls are unchanged — only the page it
renders on and the nav entry that reaches it moved. `data-can-blog` is still
substituted by server.py from the viewer's role (`__CAN_BLOG_WRITE__`), the
same way `__CAN_CAPTURE__`/`__CAN_LOG_ISSUE__` gate the /signals and
/engineering/issues forms — cosmetic; every blog_api route enforces its own
authz regardless.

Reachable by BOTH tiers, like the Reading Room it moved out of: the panel
gates on authz role, not on site tier, so the nav link itself is never hidden
by tier.
"""
from __future__ import annotations

import blog_store
import design_system as ds
from access_identity import tier_for

_PAGE_CSS = """
.room-blog { margin-top:0; }
.room-blog-head { display:flex; align-items:center; justify-content:space-between;
                  gap:1rem; margin-bottom:.75rem; }
.room-blog-head h3 { margin:0; font-size:.95rem; }
.room-blog-new { padding:.4rem .8rem; border:1px solid var(--line); border-radius:8px;
                 background:var(--surface); color:var(--brand-ink); font:inherit;
                 font-weight:600; cursor:pointer; }
.room-blog-new[aria-expanded="true"] { background:var(--brand-wash); }
/* **[hidden] MUST WIN OVER THE display RULES BELOW.** Both the new-post form and
   the editor are `display:flex` AND are shown/hidden through the `hidden`
   attribute, and an author `display` declaration beats the UA stylesheet's
   `[hidden]{display:none}` outright — so without this the attribute is inert and
   both panels render on page load. That is not cosmetic here: the editor opens
   only via `openEditor`, which is also what sets `currentSlug`, so a visible
   editor over a null `currentSlug` puts Save and Publish on their
   `if (!slug) return;` guard. Both buttons then do nothing at all — no request,
   no error, no draft — which is exactly what the operator hit on 2026-09-07.
   This is the /signals lesson a second time; `roster_build.py` carries the same
   guard and the same comment, and eight other build modules carry the rule. */
[hidden] { display: none !important; }

.room-blog-newform, .room-blog-editor { background:var(--surface-2); border:1px solid var(--line);
                 border-radius:10px; padding:1rem; margin-bottom:1rem; display:flex;
                 flex-direction:column; gap:.6rem; }
.room-blog-newform label, .room-blog-editor label { display:flex; flex-direction:column;
                 gap:.3rem; font-size:.8rem; color:var(--ink-2); }
.room-blog-newform input, .room-blog-editor input, .room-blog-editor textarea {
                 font:inherit; padding:.5rem .6rem; border:1px solid var(--line);
                 border-radius:6px; background:var(--surface); color:var(--ink); }
.rb-status { font-size:.72rem; font-weight:640; letter-spacing:.02em;
  text-transform:uppercase; padding:.1rem .4rem; border-radius:4px;
  border:1px solid var(--line); color:var(--ink-2); }
.rb-status.is-live { color:var(--ok, #2c7a4b); border-color:currentColor; }
.room-blog-editor textarea { min-height:16rem; resize:vertical;
                 font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85rem; }
.room-blog-editor-head { display:flex; align-items:baseline; gap:.6rem; }
.room-blog-actions { display:flex; gap:.5rem; align-items:center; }
.room-blog-actions button { padding:.45rem .9rem; border-radius:8px; border:1px solid var(--line);
                 background:var(--surface); font:inherit; font-weight:600; cursor:pointer; }
.room-blog-actions button.primary { background:var(--brand); border-color:var(--brand); color:#fff; }
.room-blog-actions button:disabled { opacity:.6; cursor:default; }
.room-blog-note { font-size:.8rem; padding:.5rem .7rem; border-radius:6px; }
.room-blog-note.ok { background:#e4f5ea; color:#1c7a41; }
.room-blog-note.err { background:#fbe4e0; color:#b23c1c; }
.room-blog-note.warn { background:#fdf1dc; color:#8a5a12; }
.room-blog-list { display:flex; flex-direction:column; gap:.4rem; }
.room-blog-row { display:flex; align-items:center; justify-content:space-between; gap:.75rem;
                 padding:.5rem .7rem; border:1px solid var(--line); border-radius:8px;
                 background:var(--surface); }
.room-blog-row .rb-title { font-weight:600; }
.room-blog-row .rb-meta { font-size:.76rem; color:var(--ink-3); }
.room-blog-row button { padding:.35rem .7rem; border-radius:6px; border:1px solid var(--line);
                 background:var(--surface-2); font:inherit; cursor:pointer; }
.room-blog-empty { color:var(--ink-3); font-size:.85rem; }
.room-blog-row a.rb-title { color:inherit; text-decoration:none; }
.room-blog-row a.rb-title:hover { text-decoration:underline; }
"""

# WHERE A PUBLISHED POST ACTUALLY LIVES. It moved with the corpus: publish now
# writes Orient's own store and pushes it to `convivy/swabbys-journal`, which
# Swabby's Journal renders at convivybuilder.org/journal, so nothing writes
# `lab.convivy.com` for a post published here any more and that host is being
# switched off (issue_01M0N650ZJHPQYPF54HD22RJEH).
#
# **Caught by `reviewer` on PR #195 as a regression that PR caused**, in a file
# it did not touch: the publish path changed underneath this link, so an
# operator publishing a post and clicking through to check it — the entire
# purpose of this list — would have landed on a 404, or on stale content if the
# slug collided with something the old pipeline had written. A link is part of
# a publish path even when it lives in another module.
#
# No `.html` suffix. The journal serves each post at `/journal/<slug>/`, with the
# trailing slash the site requires; the link builder below adds it.
FIELD_NOTES_URL = blog_store.FIELDNOTES_URL


def _published_section() -> str:
    """The posts live in Orient's store and are rendered on Swabby's Journal at convivybuilder.org/journal — read-only here:
    title, byline, and a link out to the published page. Added 2026-08-20
    (issue found in session): the page previously showed only the drafts
    queue, which is not what the operator meant by "the blog", so there was no way to
    tell from Writing Desk what was actually published."""
    return '''
<section class="room-blog" id="roomPublished">
  <div class="room-blog-head"><h3>Published</h3></div>
  <div class="room-blog-list" id="publishedList">
    <div class="room-blog-empty" id="publishedListEmpty">Loading…</div>
  </div>
</section>
<script>
(function() {
  const list = document.getElementById("publishedList");
  const empty = document.getElementById("publishedListEmpty");
  function load() {
    fetch("/api/blog/posts", { credentials: "same-origin" })
      .then(function(r) { return r.json(); })
      .then(function(j) {
        const posts = j.posts || [];
        list.querySelectorAll(".room-blog-row").forEach(function(r) { r.remove(); });
        empty.hidden = posts.length > 0;
        if (!posts.length) { empty.textContent = "No published posts."; return; }
        posts.forEach(function(p) {
          const row = document.createElement("div");
          row.className = "room-blog-row";
          const left = document.createElement("div");
          const t = document.createElement("a");
          t.className = "rb-title"; t.textContent = p.title;
          t.href = "''' + FIELD_NOTES_URL + '''/" + encodeURIComponent(p.slug) + "/";
          t.target = "_blank"; t.rel = "noopener";
          const m = document.createElement("div");
          m.className = "rb-meta";
          // `updated` has been on this route since it was written and was
          // never rendered. It is the only signal that a live post was
          // corrected after it went out, which now happens from this very
          // panel — so shipping the Edit button without it would make an
          // edit invisible on the page that made it.
          //
          // Compared by DAY rather than by timestamp: `save` stamps
          // `updated_at` on every write, so a post published and corrected
          // within the same day would otherwise read as "edited" on the day
          // it came out, which tells a reader nothing.
          const day = function(v) { return v ? v.slice(0, 10) : ""; };
          const edited = day(p.updated) && day(p.date)
            && day(p.updated) !== day(p.date);
          m.textContent = (p.author || "unknown author") +
            (p.date ? " \\u00b7 " + day(p.date) : "") +
            (edited ? " \\u00b7 edited " + day(p.updated) : "");
          left.appendChild(t); left.appendChild(m);
          row.appendChild(left);
          // **EDIT A LIVE POST.** The API has always allowed it — the load
          // route returns a published post and `blog_store.save` writes to
          // one on purpose, because correcting a live post is the normal
          // path. But a published post is absent from the drafts list, so
          // until this button existed there was no way in from the UI at
          // all (issue_01M0R9XR0SFP9360C73P60RR5Q).
          //
          // The opener is the permission check as well as the mechanism:
          // the drafts IIFE assigns it only for a viewer holding
          // `blog.write`, so a read-tier viewer gets the link and no
          // button, with nothing here repeating a role rule that would
          // then have two places to drift.
          if (window.__writingDesk && window.__writingDesk.open) {
            const btn = document.createElement("button");
            btn.type = "button"; btn.textContent = "Edit";
            btn.addEventListener("click", function() {
              window.__writingDesk.open(p.slug);
            });
            row.appendChild(btn);
          }
          list.appendChild(row);
        });
      })
      .catch(function() { empty.textContent = "Couldn't load published posts."; });
  }

  // Shared with the drafts IIFE so publishing can refresh this panel; see
  // the handle's own comment there for why it exists at all.
  window.__writingDesk = window.__writingDesk || {};
  window.__writingDesk.reloadPublished = load;
  load();
})();
</script>'''


def _blog_section() -> str:
    """The blog-drafts panel: list, a "new post" prompt (title + slug straight
    into POST /api/blog/drafts — the ARCH-363-UI scope decision; `lab-new` runs
    on the operator's laptop and Orient cannot reach it), an editor (title + body ->
    PUT), and a Publish button. `data-can-blog` is substituted by server.py from
    the viewer's role; every route underneath enforces its own authz regardless."""
    return f'''
<section class="room-blog" id="roomBlog" data-can-blog="__CAN_BLOG_WRITE__">
  <div class="room-blog-head">
    <h3>Blog drafts</h3>
    <button class="room-blog-new" id="blogNewToggle" type="button" aria-expanded="false">+ New post</button>
  </div>
  <form class="room-blog-newform" id="blogNewForm" hidden>
    <label>Title
      <input name="title" id="blogNewTitle" required maxlength="200" placeholder="Post title">
    </label>
    <label>Slug
      <input name="slug" id="blogNewSlug" required maxlength="64"
             pattern="[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?" placeholder="post-slug">
    </label>
    <div class="room-blog-actions">
      <button type="submit" class="primary">Create draft</button>
    </div>
    <div class="room-blog-note" id="blogNewNote" hidden></div>
  </form>
  <div class="room-blog-list" id="blogList">
    <div class="room-blog-empty" id="blogListEmpty">No drafts yet.</div>
  </div>
  <div class="room-blog-editor" id="blogEditor" hidden>
    <div class="room-blog-editor-head">
      <strong id="blogEditorTitle"></strong>
      <span class="rb-status" id="blogEditorStatus" hidden></span>
      <span class="rb-meta" id="blogEditorMeta"></span>
    </div>
    <label>Title
      <input id="blogEditTitle" maxlength="200">
    </label>
    <label>Body
      <textarea id="blogEditBody" rows="16"></textarea>
    </label>
    <div class="room-blog-actions">
      <button type="button" id="blogSaveBtn" class="primary">Save</button>
      <button type="button" id="blogPublishBtn">Publish</button>
      <button type="button" id="blogCloseBtn">Close</button>
    </div>
    <div class="room-blog-note" id="blogEditNote" hidden></div>
  </div>
</section>
<script>{_BLOG_JS}</script>'''


# The blog panel's own IIFE, self-contained. Bails immediately when
# data-can-blog is not "true" — the CSS already hides the panel for that
# viewer, so this also skips firing an API call that role would only be
# 403'd on.
_BLOG_JS = r"""
(function() {
  const section = document.getElementById("roomBlog");
  if (!section || section.dataset.canBlog !== "true") return;
  const newToggle = document.getElementById("blogNewToggle");
  const newForm = document.getElementById("blogNewForm");
  const newNote = document.getElementById("blogNewNote");
  const newTitleInput = document.getElementById("blogNewTitle");
  const slugInput = document.getElementById("blogNewSlug");
  const list = document.getElementById("blogList");
  const listEmpty = document.getElementById("blogListEmpty");
  const editor = document.getElementById("blogEditor");
  const editorTitle = document.getElementById("blogEditorTitle");
  const editorMeta = document.getElementById("blogEditorMeta");
  const editTitle = document.getElementById("blogEditTitle");
  const editBody = document.getElementById("blogEditBody");
  const editNote = document.getElementById("blogEditNote");
  const editorStatus = document.getElementById("blogEditorStatus");
  const saveBtn = document.getElementById("blogSaveBtn");
  const publishBtn = document.getElementById("blogPublishBtn");
  const closeBtn = document.getElementById("blogCloseBtn");
  let currentSlug = null;

  function setNote(el, cls, text) {
    el.className = "room-blog-note " + cls;
    el.textContent = text;
    el.hidden = false;
  }

  function slugify(s) {
    return (s || "").toLowerCase().trim()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
  }
  let slugTouched = false;
  slugInput.addEventListener("input", function() { slugTouched = true; });
  newTitleInput.addEventListener("input", function(e) {
    if (!slugTouched) slugInput.value = slugify(e.target.value);
  });

  newToggle.addEventListener("click", function() {
    const open = newForm.hidden;
    newForm.hidden = !open;
    newToggle.setAttribute("aria-expanded", open ? "true" : "false");
  });

  function renderList(drafts) {
    list.querySelectorAll(".room-blog-row").forEach(function(r) { r.remove(); });
    listEmpty.hidden = drafts.length > 0;
    drafts.forEach(function(d) {
      const row = document.createElement("div");
      row.className = "room-blog-row";
      const left = document.createElement("div");
      const t = document.createElement("div");
      t.className = "rb-title"; t.textContent = d.title;
      const m = document.createElement("div");
      m.className = "rb-meta"; m.textContent = d.slug + " · " + (d.author || "unknown author");
      left.appendChild(t); left.appendChild(m);
      const btn = document.createElement("button");
      btn.type = "button"; btn.textContent = "Edit";
      btn.addEventListener("click", function() { openEditor(d.slug); });
      row.appendChild(left); row.appendChild(btn);
      list.appendChild(row);
    });
  }

  function loadList() {
    fetch("/api/blog/drafts", { credentials: "same-origin" })
      .then(function(r) { return r.json(); })
      .then(function(j) { renderList(j.drafts || []); })
      .catch(function() {});
  }

  function openEditor(slug) {
    fetch("/api/blog/drafts/" + encodeURIComponent(slug), { credentials: "same-origin" })
      .then(function(r) {
        return r.json().catch(function() { return {}; }).then(function(j) {
          if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
          return j;
        });
      })
      .then(function(j) {
        currentSlug = j.slug;
        editorTitle.textContent = j.title || j.slug;
        editorMeta.textContent = "by " + (j.author || "unknown author");
        editTitle.value = j.title || "";
        editBody.value = j.body || "";
        // The route returns `status` precisely so the editor can SAY which it
        // is rather than leave the writer to infer it.
        //
        // **THERE IS NO DRAFT COPY OF A LIVE POST, AND THE WARNING HAS TO SAY
        // SO** (security-reviewer, PR #199). An early cut said saving was safe
        // and only Publish reached the public page. That is false:
        // `blog_store.save` writes straight into the published row.
        //
        // **THE CORRECTION THEN POINTED AT THE WRONG BUTTON**
        // (issue_01M0T979V44QGHSXD2TF5CP2G8). It said Publish "only asks the
        // site to rebuild sooner", which was true of a Publish that sent no
        // body — and steered a reader who wanted their change live toward the
        // one control that discarded it. The operator lost an edit that way on
        // 2026-08-24.
        //
        // **THE WARNING CHANGED AGAIN WHEN ORIENT STARTED PUSHING**
        // (issue_01M0N650ZJHPQYPF54HD22RJEH, step 2). Under the old pull, the
        // site fetched the corpus on its own schedule, so a Save eventually
        // reached it regardless of which button sent it. Under push, nothing
        // reaches Swabby's Journal until something is Published:
        // `journal_push.push_corpus` runs only from `publish_draft`. A Save to
        // a live post now changes what `blog_store.corpus()` holds but does NOT
        // reach the journal on its own, so the warning has to say that plainly
        // rather than repeat the old "either button, eventually" claim.
        const live = j.status === "published";
        editorStatus.textContent = live ? "published" : "draft";
        editorStatus.className = "rb-status " + (live ? "is-live" : "is-draft");
        editorStatus.hidden = false;
        if (live) {
          setNote(editNote, "warn",
            "This post is live and there is no draft copy. Either button "
            + "writes what you see here to the published post, but only "
            + "Publish sends that change to Swabby's Journal (convivybuilder.org), "
            + "where it goes live a couple of minutes later. Save leaves the journal "
            + "as it was until something is next published.");
        } else {
          editNote.hidden = true;
        }
        editor.hidden = false;
        editor.scrollIntoView({ block: "start", behavior: "smooth" });
      })
      .catch(function(err) { setNote(newNote, "err", "Couldn't open " + slug + ": " + err.message); });
  }

  newForm.addEventListener("submit", function(e) {
    e.preventDefault();
    const title = newTitleInput.value.trim();
    const slug = slugInput.value.trim();
    if (!slug) { setNote(newNote, "err", "Slug is required."); return; }
    const btn = newForm.querySelector("button[type=submit]");
    btn.disabled = true;
    fetch("/api/blog/drafts", {
      method: "POST", credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ title: title, slug: slug, body: "" }),
    }).then(function(r) {
      return r.json().catch(function() { return {}; }).then(function(j) {
        if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
        return j;
      });
    }).then(function(j) {
      newForm.reset(); slugTouched = false; newForm.hidden = true;
      newToggle.setAttribute("aria-expanded", "false");
      newNote.hidden = true;
      loadList();
      openEditor(j.slug);
    }).catch(function(err) {
      setNote(newNote, "err", "Couldn't create draft: " + err.message);
    }).finally(function() { btn.disabled = false; });
  });

  function saveEditor(slug) {
    // **ONE REQUEST, TWO CALLERS, AND THAT IS THE POINT.** Publish used to send
    // no body at all, so it republished whatever the store held and silently
    // discarded anything typed since the last Save — then said "Published."
    // (issue_01M0T979V44QGHSXD2TF5CP2G8; the operator lost an edit to a live post on
    // 2026-08-24, and the published revision's body hash was byte-identical to
    // the version imported the day before).
    //
    // Sharing the save means Publish cannot drift back into publishing a
    // different body from the one on screen.
    //
    // **THE SLUG IS A PARAMETER, NOT A READ OF `currentSlug`** (reviewer,
    // PR #201). Both callers capture it at click time; taking it here would
    // read a variable another click can reassign while a request is in flight.
    return fetch("/api/blog/drafts/" + encodeURIComponent(slug), {
      method: "PUT", credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ title: editTitle.value, body: editBody.value }),
    }).then(function(r) {
      return r.json().catch(function() { return {}; }).then(function(j) {
        if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
        return j;
      });
    });
  }

  saveBtn.addEventListener("click", function() {
    const slug = currentSlug;
    // **DIAGNOSTIC, NOT A USER-FACING MESSAGE — and the distinction is the
    // point.** With the `[hidden]` rule above in place the editor cannot be
    // visible without a slug, so this branch only fires on a race (Close, or a
    // failed open, while a click is in flight). `editNote` lives INSIDE
    // `#blogEditor`, and `currentSlug` is only ever nulled in lockstep with
    // `editor.hidden = true`, so in that branch this note is written into a
    // `display:none` ancestor and the reader does not see it — it is legible in
    // DevTools and in a DOM snapshot, nothing more (reviewer, PR #274). Kept
    // because a silent `return` is what made 2026-09-07 undiagnosable, and a
    // note in the DOM is strictly more evidence than none; do not mistake it
    // for something a user will read.
    if (!slug) { setNote(editNote, "err", "No draft is open — open one from the list first."); return; }
    saveBtn.disabled = true;
    saveEditor(slug).then(function() {
      if (currentSlug === slug) { setNote(editNote, "ok", "Saved."); }
      loadList();
    }).catch(function(err) {
      if (currentSlug === slug) {
        setNote(editNote, "err", "Couldn't save: " + err.message);
      }
    }).finally(function() { saveBtn.disabled = false; });
  });

  publishBtn.addEventListener("click", function() {
    // **CAPTURED AT CLICK TIME, AND EVERY LATER STEP USES THIS** (reviewer,
    // PR #201). `currentSlug` is reassigned by `openEditor`, and the drafts
    // list stays clickable while a publish is in flight — only these two
    // buttons are disabled. Before this PR the publish fetch fired
    // synchronously off one read, so there was no window; awaiting the save
    // ahead of it is what opened one. Re-reading the variable after the await
    // would publish whichever post was opened DURING the wait, under a click
    // made against this one, and report success.
    const slug = currentSlug;
    if (!slug) { setNote(editNote, "err", "No draft is open — open one from the list first."); return; }
    publishBtn.disabled = true; saveBtn.disabled = true;
    setNote(editNote, "ok", "Saving, then publishing…");
    // **SAVE FIRST, AND DO NOT PUBLISH IF THE SAVE FAILS.** The publish route
    // takes no body and republishes what the store holds, so without this the
    // button ships the last saved text rather than the text on screen. A
    // failed save that still published would put the OLD body on the public
    // site under a success message, which is the failure this replaces rather
    // than a new one.
    let saved = false;
    saveEditor(slug).then(function() {
      saved = true;
      return fetch("/api/blog/drafts/" + encodeURIComponent(slug) + "/publish", {
        method: "POST", credentials: "same-origin",
      }).then(function(r) {
        return r.json().catch(function() { return {}; }).then(function(j) {
          if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
          return j;
        });
      });
    }).then(function(j) {
      // Only stamp the editor if it still holds the post that was published —
      // otherwise a reader who moved on sees "Published." over a different
      // post's text.
      //
      // **A PUBLISH THAT NEVER REACHED THE JOURNAL SAYS SO** (reviewer, PR
      // #355). The route answers 200 once the store holds the post, and the
      // push is best-effort, so `journal.corpus_sha` null means nothing was
      // written to the journal repository (no credential, or GitHub refused):
      // the post is not going live, and "Published." would
      // say it was.
      //
      // **A NEW COMMIT STARTS THE SITE BUILD, AND SAYS WHETHER IT DID.**
      // `journal.site_build` is set only when the publish made a commit. A
      // build that didn't start leaves the post in the journal repository but
      // not on the site until its next scheduled run. Publishing again would
      // not help: the corpus is unchanged, so it makes no commit and sends no
      // dispatch.
      const journal = j && j.journal;
      const stranded = journal && journal.corpus_sha == null;
      const build = journal && journal.site_build;
      if (currentSlug === slug) {
        if (stranded) {
          setNote(editNote, "warn",
            "Published in Orient, but it didn't reach Swabby's Journal ("
            + (journal.reason || "unknown reason")
            + "). Publish again once that's fixed.");
        } else if (build && build.dispatched) {
          setNote(editNote, "ok",
            "Published. Swabby's Journal is updating; that takes a couple of minutes.");
        } else if (build) {
          setNote(editNote, "warn",
            "Published, but the journal's site build didn't start ("
            + (build.reason || "unknown reason")
            + "). The post goes live with the site's next scheduled build.");
        } else {
          setNote(editNote, "ok", "Published.");
        }
        editorStatus.textContent = "published";
        editorStatus.className = "rb-status is-live";
        editorStatus.hidden = false;
      }
      loadList();
      // The post has just left the drafts list (or had its text changed while
      // already on the published one), so the panel above is now stale either
      // way. It renders in its own IIFE, hence the shared handle.
      if (window.__writingDesk && window.__writingDesk.reloadPublished) {
        window.__writingDesk.reloadPublished();
      }
    }).catch(function(err) {
      // **WHICH STEP FAILED CHANGES WHAT THE WRITER SHOULD DO** (reviewer,
      // PR #201). A save that never landed means the text is still only in
      // this browser and closing the tab loses it; a publish that failed after
      // a good save means the text is safe and the post did not go out.
      // One message for both would leave a writer guessing on a control that
      // has already cost an edit.
      if (currentSlug === slug) {
        setNote(editNote, "err", saved
          ? "Saved, but publishing failed: " + err.message
          : "Couldn't save, so nothing was published: " + err.message);
      }
    }).finally(function() { publishBtn.disabled = false; saveBtn.disabled = false; });
  });

  closeBtn.addEventListener("click", function() {
    editor.hidden = true; currentSlug = null;
  });

  // **THE PUBLISHED LIST RENDERS IN A DIFFERENT IIFE AND NEEDS THIS OPENER.**
  // A post leaves the drafts list the moment it publishes (`blog_store.drafts()`
  // is `status == DRAFT`), so without a way to open one from the published
  // panel a post becomes uneditable as soon as it goes out — which is the
  // defect this handle exists to fix (issue_01M0R9XR0SFP9360C73P60RR5Q).
  //
  // Assigned only when this IIFE runs, and it returns early for a viewer
  // without `blog.write`. So the published panel testing for this handle is
  // also its permission check: no writer role, no opener, no Edit button.
  window.__writingDesk = window.__writingDesk || {};
  window.__writingDesk.open = openEditor;

  loadList();
})();
"""


def live_html(email: str) -> str:
    """Render the Writing Desk for the verified caller. `data-can-blog` is left
    as the `__CAN_BLOG_WRITE__` placeholder for server.py to substitute from
    the viewer's role — the same mechanism `/room` used before the panel moved
    here."""
    tier = tier_for(email)
    body = (
        '<header class="hero"><div class="hero-inner">'
        '<h1>Writing Desk</h1>'
        '<p class="hero-sub">Draft, edit, and publish posts for the lab blog.</p>'
        '</div></header>'
        '<section class="home-section">'
        + _published_section()
        + '</section>'
        '<section class="home-section">'
        + _blog_section()
        + '</section>')
    return ds.page_shell(
        title="Writing Desk",
        nav_html=ds.nav_component("/writing-desk", tier),
        body=body,
        extra_css=_PAGE_CSS,
    )
