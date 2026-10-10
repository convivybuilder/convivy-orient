#!/usr/bin/env python3.11
"""The in-site KB document editor, shared by every surface that offers one.

The editor began on `/reference/doc/{id}`, where one document fills the whole
page (`reference_doc_build._editor_section`). The browse page at `/reference`
now offers it too, in the inline inset and in the drawer, so that a member
reading a document reaches the editor without the page navigating
(issue_01M17HYDBQ4GPWHPFS56M6QQPH, done-when bullet 1). This module holds the
one copy of the CSS and the JS both surfaces run.

**The JS is a controller, not a page script, and exactly one editor is open at
a time across the whole document.** `window.KbEditor.open(mount, docId)`
closes whatever was open first, which is what releases its checkout. That
single-instance rule is the mechanism behind the lease discipline below; it is
not a styling preference.

**WHY THE LEASE LIFECYCLE IS THE HARD PART HERE.** Opening the editor takes a
Library checkout — a lock with a four-hour TTL that blocks every other writer
on that document, the Library's own `kb_update` and `kb_propose` included. The
document's own page could be careless about releasing it, because that page
holds one document and navigating away ends the page. The browse page never
reloads while the reader clicks between documents, so without an explicit
release, clicking through ten documents would strand ten locks. Every path out
of an open editor therefore checks in:

- the Close button, and the host page's own close paths (see `close`);
- opening the editor on a different document (`open` closes the previous one);
- leaving the page, via the `pagehide` beacon below, which also fixes the
  stranded lock the document's own page has always left behind on navigation.

**The body is edited as the rendered document, through md-serial**
(issue_01M35KC8PZMZBMJKX4RZR9F1Y8). The checked-out file is split into its
frontmatter block, edited as YAML text above, and its Markdown body, which
md-serial renders and edits in place; a save joins them again
(`editor_js/kb-doc.js`). md-serial and that glue are ES modules served at
`/editor/js/` (`server.EDITOR_JS_FILES`) and imported the first time an editor
opens. A save goes through md-serial's `saveChecked`, and one whose Markdown does
not reparse to what the member was looking at is refused rather than committed.

**The draft lives in JS state, not in the DOM**, because the browse page
rebuilds its whole listing from state on every keystroke in the search box, and
an editor inside a row would be destroyed by that re-render. `remount` reads the
body out of the outgoing md-serial instance, mounts a fresh one into the new DOM
seeded with its revision count, and repaints the frontmatter text, so the draft,
the checkout and the heartbeat all survive a re-render the reader never asked
for and does not see.

**`dirty()` compares the draft against what the checkout returned**, so a
save moves the baseline and closing straight after a save prompts nobody.
"""
from __future__ import annotations


# Refreshed well inside any plausible grace period — the Library's own
# `lease.py` tunables govern the actual expiry and this file has no access to
# them; a few-minute cadence is cheap and the heartbeat call itself reports a
# lapsed lock immediately rather than silently, so a mistuned interval fails
# safe (a visible warning) rather than unsafe (a silent loss).
_HEARTBEAT_INTERVAL_MS = 4 * 60 * 1000

# Every mount renders this and then `paint()` fills it. The parts are addressed
# by `data-kb` rather than by id: the browse page mounts and unmounts the editor
# many times in one page life, and an id would be either duplicated or stale.
_TOOLBAR = (
    ("heading", "Heading"), ("bold", "Bold"), ("italic", "Italic"),
    ("bullets", "Bulleted list"), ("numbers", "Numbered list"),
    ("link", "Link"), ("code", "Code block"),
)
_BOX_HTML = (
    '<div class="kb-editor-box">'
    '<div class="kb-editor-head"><span class="kb-status" data-kb="status"></span></div>'
    '<label class="kb-fm-label">Frontmatter'
    '<textarea data-kb="fm" rows="6" spellcheck="false" aria-label="Frontmatter (YAML)"></textarea>'
    '</label>'
    '<div class="kb-toolbar" data-kb="toolbar" role="toolbar" aria-label="Formatting">'
    + "".join(f'<button type="button" data-kb-cmd="{cmd}">{label}</button>'
              for cmd, label in _TOOLBAR)
    + '</div>'
    '<div class="kb-doc" data-kb="body" aria-label="Document"></div>'
    '<div class="kb-editor-actions">'
    '<button type="button" class="primary" data-kb="save">Save</button>'
    '<button type="button" data-kb="close">Close</button>'
    '</div>'
    '<div class="kb-editor-note" data-kb="note" hidden></div>'
    '</div>'
)

EDITOR_JS = r"""
window.KbEditor = (function () {
  "use strict";

  var HEARTBEAT_MS = """ + str(_HEARTBEAT_INTERVAL_MS) + r""";
  var BOX_HTML = '""" + _BOX_HTML.replace("'", "\\'") + r"""';

  // The one open editor, or null. Everything the editor knows lives here and
  // not in the DOM, so a host that rebuilds its markup can call `remount` and
  // lose nothing.
  var cur = null;

  // md-serial and Orient's glue, imported on first use and shared by every
  // later open. A failed import is not cached, so the next open tries again.
  // `window.KbEditorLoadLibrary` replaces the import, and only the tests set it:
  // they drive this controller on a DOM md-serial cannot render into, and
  // md-serial itself is tested on its own DOM stub (tests/md_serial_stub/).
  var JS_BASE = "/editor/js/";
  var libPromise = null;
  function loadLib() {
    if (!libPromise) {
      var load = window.KbEditorLoadLibrary || function () {
        return Promise.all([import(JS_BASE + "editor.js"), import(JS_BASE + "kb-doc.js")])
          .then(function (m) { return { mountEditor: m[0].mountEditor, kb: m[1] }; });
      };
      libPromise = load().catch(function (e) { libPromise = null; throw e; });
    }
    return libPromise;
  }

  // The whole file as it stands: the frontmatter text joined to the body.
  // Before md-serial is mounted, and after a remount has read it out, the body
  // is the string held in `cur.body`.
  function draftNow() {
    return cur.ed ? cur.lib.kb.draft(cur.fm, cur.ed) : cur.fm + cur.body;
  }

  function apiPath(docId, action) {
    return "/api/kb/documents/" + encodeURIComponent(docId) + "/" + action;
  }

  function post(docId, action) {
    return fetch(apiPath(docId, action), { method: "POST", credentials: "same-origin" })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          return { ok: r.ok, status: r.status, body: j };
        });
      });
  }

  function stopHeartbeat() {
    if (cur && cur.timer) { clearInterval(cur.timer); cur.timer = null; }
  }

  function startHeartbeat() {
    stopHeartbeat();
    var forDoc = cur.docId;
    cur.timer = setInterval(function () {
      post(forDoc, "heartbeat").then(function (res) {
        // The editor may have been closed or switched between the request and
        // its answer; a reply about a document we no longer hold must not
        // repaint the one we do.
        if (!cur || cur.docId !== forDoc) return;
        if (res.ok) return;
        if (res.status === 409) {
          // The one answer that means the lock is genuinely gone: the route
          // returns 409 for a checkout that lapsed or was reclaimed.
          lockLost((res.body && res.body.error) ||
            "the edit lock lapsed; reopen the document to keep editing");
          return;
        }
        // **ANY OTHER FAILURE IS A FAILURE TO ASK, NOT EVIDENCE THAT THE LOCK
        // LAPSED**, and the route has at least two of them (503 when Library
        // writes are off, 5xx from the Library itself). Calling `lockLost` here
        // would clear `heldLock` on a lock that is still perfectly alive,
        // leaving `close` and the unload beacon with nothing to release and
        // stranding it for the Library's full TTL. Warn and keep editing: the
        // next tick, or the save itself, will report a lock that really has
        // gone.
        cur.note = { cls: "warn", text: (res.body && res.body.error) ||
          "Couldn't refresh the edit lock just now; it may lapse if this keeps failing." };
        paint();
      });
    }, HEARTBEAT_MS);
  }

  function lockLost(message) {
    cur.editable = false;
    cur.heldLock = false;
    stopHeartbeat();
    cur.status = "lock lost";
    cur.note = { cls: "warn", text: message };
    paint();
  }

  function part(name) {
    return cur && cur.mount ? cur.mount.querySelector('[data-kb="' + name + '"]') : null;
  }

  function paint() {
    if (!cur || !cur.mount) return;
    var fm = part("fm");
    var body = part("body");
    var toolbar = part("toolbar");
    var saveBtn = part("save");
    var status = part("status");
    var note = part("note");
    if (fm) {
      // Assigning an unchanged value still moves the caret to the end in some
      // browsers, and `paint` runs on events the reader did not cause (a
      // heartbeat answer, a save landing), so only write when it differs.
      if (fm.value !== cur.fm) fm.value = cur.fm;
      fm.hidden = !cur.editable;
      fm.disabled = !cur.editable;
    }
    if (toolbar) toolbar.hidden = !cur.editable;
    if (body) {
      // md-serial makes its root contenteditable; a lock that has gone takes
      // that back, so the member cannot type into a document no save will take.
      body.hidden = !cur.editable;
      body.disabled = !cur.editable;
      if (!cur.editable) body.setAttribute("contenteditable", "false");
    }
    if (saveBtn) {
      saveBtn.hidden = !cur.editable;
      saveBtn.disabled = !cur.editable || cur.saving;
    }
    if (status) {
      status.textContent = cur.status;
      status.hidden = !cur.status;
    }
    if (note) {
      note.className = "kb-editor-note " + (cur.note ? cur.note.cls : "");
      note.textContent = cur.note ? cur.note.text : "";
      note.hidden = !cur.note;
    }
  }

  // Mount md-serial over the body, seeded with the revision count a remount
  // carried over (md-serial's own contract: a remount is a fresh mount).
  function mountBody() {
    var root = part("body");
    if (!root || !cur.lib || !cur.editable) return;
    cur.ed = cur.lib.kb.mountBody(cur.lib.mountEditor, root, cur.body, { revision: cur.rev });
  }

  function runCommand(name) {
    if (!cur || !cur.ed || !cur.editable) return;
    var href;
    if (name === "link") {
      href = window.prompt("Link address");
      if (!href) return;
    }
    var changed = cur.lib.kb.COMMANDS[name](cur.ed, href);
    cur.note = changed ? null
      : { cls: "warn", text: "That formatting doesn't apply here; select some text in the document first." };
    paint();
  }

  function renderInto(mount) {
    // Read the body out of the outgoing md-serial instance before its DOM is
    // replaced, so the new mount starts from the member's draft.
    if (cur.ed) {
      cur.body = cur.ed.save();
      cur.rev = cur.ed.revision();
      cur.ed = null;
    }
    cur.mount = mount;
    mount.innerHTML = BOX_HTML;
    var fm = part("fm");
    var saveBtn = part("save");
    var closeBtn = part("close");
    if (fm) fm.addEventListener("input", function () { cur.fm = fm.value; });
    var buttons = mount.querySelectorAll("[data-kb-cmd]");
    for (var i = 0; i < buttons.length; i++) {
      (function (btn) {
        // Keep the selection in the document: a click would move focus to the
        // button and leave the command nothing selected to act on.
        btn.addEventListener("mousedown", function (e) { if (e.preventDefault) e.preventDefault(); });
        btn.addEventListener("click", function () { runCommand(btn.dataset.kbCmd); });
      })(buttons[i]);
    }
    if (saveBtn) saveBtn.addEventListener("click", save);
    if (closeBtn) closeBtn.addEventListener("click", function () { api.close(); });
    mountBody();
    paint();
  }

  function save() {
    if (!cur || !cur.editable || cur.saving || !cur.ed) return;
    var checked;
    try {
      checked = cur.lib.kb.checkedDraft(cur.fm, cur.ed);
    } catch (e) {
      checked = { ok: false, failures: [String((e && e.message) || e)] };
    }
    if (!checked.ok) {
      // Committing Markdown that does not say what the member saw would change
      // the document silently. The draft stays in the editor to be fixed.
      cur.note = { cls: "err", text: "Not saved: the editor couldn't write this change back " +
        "to Markdown faithfully (" + (checked.failures[0] || "unknown difference") +
        "). Your edits are still here." };
      paint();
      return;
    }
    var sent = checked.draft;
    cur.saving = true;
    paint();
    var forDoc = cur.docId;
    // The body is the draft and the sha is the one the CHECKOUT returned, so
    // the server refuses the save outright if origin/main has moved under the
    // open editor rather than committing over the newer revision.
    fetch(apiPath(forDoc, "save"), {
      method: "POST", credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ body: sent, content_sha: cur.openedSha }),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        return { ok: r.ok, status: r.status, body: j };
      });
    }).then(function (res) {
      if (!cur || cur.docId !== forDoc) return;
      cur.saving = false;
      if (res.ok) {
        // The save route checks the document back in, so this editor no longer
        // holds the lock and `close` must not post a second checkin. The draft
        // is what was committed, so the dirty baseline moves with it and
        // closing now prompts nobody.
        cur.original = sent;
        cur.heldLock = false;
        stopHeartbeat();
        cur.note = res.body.checked_in === false
          ? { cls: "warn", text: res.body.warning || "Saved, but the checkout could not be released." }
          : { cls: "ok", text: "Saved." };
      } else if (res.status === 422) {
        cur.note = { cls: "err", text: "Frontmatter does not validate: " +
          ((res.body.errors || []).join("; ") || res.body.error) };
      } else if (res.status === 409) {
        lockLost((res.body && res.body.error) || "you no longer hold this document's checkout");
        return;
      } else {
        cur.note = { cls: "err", text: (res.body && res.body.error) || "Couldn't save." };
      }
      paint();
    }).catch(function () {
      if (!cur || cur.docId !== forDoc) return;
      cur.saving = false;
      cur.note = { cls: "err", text: "Couldn't reach the server." };
      paint();
    });
  }

  var api = {
    // The document the open editor is on, or null. A host renders its mount
    // point for exactly this id and for no other row.
    docId: function () { return cur ? cur.docId : null; },

    // True when the draft differs from what the checkout returned. A failed
    // checkout is never dirty: there is nothing to lose.
    // A body md-serial cannot serialise counts as unsaved, so the reader is
    // asked rather than the draft dropped without a word.
    dirty: function () {
      if (!(cur && cur.editable)) return false;
      try { return draftNow() !== cur.original; } catch (e) { return true; }
    },

    // Open `docId` in `mount`, closing and checking in whatever was open.
    // Resolves false when a dirty editor was open and the reader declined to
    // discard it, which is the host's signal to abort the navigation it was
    // about to make.
    open: function (mount, docId, opts) {
      opts = opts || {};
      if (cur && cur.docId === docId) { renderInto(mount); return Promise.resolve(true); }
      if (cur && !api.close()) return Promise.resolve(false);
      cur = {
        docId: docId, mount: null, fm: "", body: "", ed: null, lib: null, rev: 0,
        original: "", openedSha: "", editable: false, heldLock: false, saving: false,
        status: "opening…", note: null, timer: null,
        onClosed: opts.onClosed || null,
      };
      renderInto(mount);
      var forDoc = docId;
      return post(docId, "checkout").then(function (res) {
        // **A CHECKOUT STILL IN FLIGHT WHEN THE EDITOR CLOSES IS THE ONE LOCK
        // NOTHING ELSE WILL EVER RELEASE.** `close` posts a checkin only when
        // `heldLock` is set, and it is only set below, when this answer
        // arrives — so Edit-then-Close, Edit-then-Escape, or Edit-then-Edit on
        // another document, all before the response lands, used to leave the
        // Library holding the lock for its full four hours with no editor on
        // screen and no state left pointing at it. The checkin has to be
        // issued from here, for the document this request was about.
        if (!cur || cur.docId !== forDoc) {
          if (res.ok) post(forDoc, "checkin");
          return false;
        }
        if (res.ok) {
          // Held from here, so a close or a switch while the library loads
          // checks it in through `close`, and the callbacks below, finding the
          // editor gone, leave it alone rather than post a second checkin.
          cur.heldLock = true;
          return loadLib().then(function (lib) {
            if (!cur || cur.docId !== forDoc) return false;
            // The stored file, frontmatter block first, exactly as the save
            // route will validate it. Never the rendered page's copy of the
            // body, which the Library serves with the frontmatter stripped.
            var parts = lib.kb.splitFrontmatter(res.body.content);
            cur.lib = lib;
            cur.fm = parts.frontmatter;
            cur.body = parts.body;
            cur.original = res.body.content;
            cur.openedSha = res.body.content_sha;
            cur.editable = true;
            cur.status = "editing";
            cur.note = null;
            mountBody();
            paint();
            startHeartbeat();
            return true;
          }, function () {
            if (!cur || cur.docId !== forDoc) return false;
            // The lock is held and nothing can edit under it, so it goes back.
            post(forDoc, "checkin");
            cur.heldLock = false;
            cur.status = "read-only";
            cur.note = { cls: "err", text: "Couldn't load the editor. Reload the page to try again." };
            paint();
            return false;
          });
        }
        // No stored file arrives on a refusal, and the host already shows the
        // document, so the box carries only the note.
        cur.editable = false;
        cur.heldLock = false;
        cur.status = "read-only";
        if (res.status === 409 && res.body && res.body.checkout) {
          cur.note = { cls: "warn", text: "Checked out by " + res.body.checkout.holder +
            (res.body.checkout.expires_at ? " until " + res.body.checkout.expires_at : "") + "." };
        } else {
          cur.note = { cls: "err",
            text: (res.body && res.body.error) || "Couldn't check out this document." };
        }
        paint();
        return false;
      }).catch(function () {
        // **A REQUEST THAT FAILED ON THIS SIDE IS NOT PROOF THE SERVER NEVER
        // TOOK THE LOCK** — the checkout may have completed and the answer been
        // lost. Either way this editor is not going to edit anything, so hand
        // the lock back unconditionally. A checkin is scoped to its own holder,
        // so at worst the Library refuses one that was never taken.
        post(forDoc, "checkin");
        if (!cur || cur.docId !== forDoc) return false;
        cur.editable = false;
        cur.heldLock = false;
        cur.status = "read-only";
        cur.note = { cls: "err", text: "Couldn't reach the server." };
        paint();
        return false;
      });
    },

    // Close the open editor, checking the document back in. Returns false, and
    // closes nothing, when the draft has unsaved changes and the reader
    // declines to discard them. Pass `{confirmDirty: false}` only where there
    // is no navigation left to abort.
    close: function (opts) {
      opts = opts || {};
      if (!cur) return true;
      if (opts.confirmDirty !== false && api.dirty() &&
          !window.confirm("This document has unsaved changes. Discard them?")) {
        return false;
      }
      stopHeartbeat();
      if (cur.heldLock) post(cur.docId, "checkin");
      var mount = cur.mount, onClosed = cur.onClosed;
      cur = null;
      if (mount) mount.innerHTML = "";
      if (onClosed) onClosed();
      return true;
    },

    // Paint the open editor into a fresh element after the host rebuilt its
    // markup. The draft, the checkout and the heartbeat are untouched.
    remount: function (mount) {
      if (!cur) return false;
      renderInto(mount);
      return true;
    },
  };

  // Leaving the page releases the lock rather than stranding it for the
  // Library's full TTL. `sendBeacon` is the only request shape that survives
  // unload; it posts same-origin with credentials, and the checkin route reads
  // no body. A browser without it simply falls back to the old behaviour.
  window.addEventListener("pagehide", function () {
    if (cur && cur.heldLock && navigator.sendBeacon) {
      navigator.sendBeacon(apiPath(cur.docId, "checkin"));
    }
  });

  // An unsaved draft is lost on unload with nothing to recover it from, so the
  // browser's own prompt is the last guard. It fires before `pagehide`, so a
  // reader who chooses to leave still releases the lock.
  window.addEventListener("beforeunload", function (e) {
    if (api.dirty()) { e.preventDefault(); e.returnValue = ""; }
  });

  return api;
})();
"""


# The visual vocabulary was borrowed from writing_desk_build's `.room-blog-editor`
# rules, under a `kb-` prefix since that module's CSS is private and page-scoped
# rather than shared. It now lives here rather than on the document page, because
# the browse page renders the same box (issue_01M17HYDBQ4GPWHPFS56M6QQPH).
EDITOR_CSS = r"""
.kb-editor { margin-top: 28px; padding-top: 20px; border-top: 1px solid var(--line); }
.kb-editor[data-can-kb-write="false"] { display: none; }
.kb-editor-bar { display: flex; align-items: center; gap: .6rem; }
.kb-edit-btn { padding: .4rem .9rem; border: 1px solid var(--line); border-radius: 8px;
               background: var(--surface); color: var(--brand-ink); font: inherit;
               font-weight: 600; cursor: pointer; }
.kb-status { font-size: .72rem; font-weight: 640; letter-spacing: .02em; text-transform: uppercase;
             padding: .1rem .4rem; border-radius: 4px; border: 1px solid var(--line); color: var(--ink-2); }
.kb-editor-box { margin-top: .75rem; background: var(--surface-2); border: 1px solid var(--line);
                 border-radius: 10px; padding: 1rem; display: flex; flex-direction: column; gap: .6rem; }
.kb-editor-head { display: flex; align-items: center; gap: .6rem; }
.kb-fm-label { display: flex; flex-direction: column; gap: .3rem; font-size: .72rem; font-weight: 640;
                letter-spacing: .02em; text-transform: uppercase; color: var(--ink-2); }
.kb-editor-box textarea { font: inherit; font-family: var(--mono); font-size: .8rem; text-transform: none;
                          letter-spacing: 0; font-weight: 400;
                          padding: .5rem .7rem; border: 1px solid var(--line); border-radius: 6px;
                          background: var(--surface); color: var(--ink); min-height: 6rem; resize: vertical; }
.kb-toolbar { display: flex; flex-wrap: wrap; gap: .35rem; }
.kb-toolbar button { padding: .3rem .6rem; border-radius: 6px; border: 1px solid var(--line);
                     background: var(--surface); color: var(--ink); font: inherit; font-size: .8rem; cursor: pointer; }
.kb-doc { min-height: 20rem; padding: .8rem 1rem; border: 1px solid var(--line); border-radius: 6px;
          background: var(--surface); color: var(--ink); line-height: 1.55; overflow-x: auto; }
.kb-doc:focus-within { outline: 2px solid var(--brand); outline-offset: 1px; }
.kb-doc h1, .kb-doc h2, .kb-doc h3 { line-height: 1.25; margin: 1rem 0 .4rem; }
.kb-doc p, .kb-doc ul, .kb-doc ol, .kb-doc blockquote, .kb-doc pre, .kb-doc table { margin: 0 0 .7rem; }
.kb-doc blockquote { border-left: 3px solid var(--line); padding-left: .8rem; color: var(--ink-2); }
.kb-doc code { font-family: var(--mono); font-size: .85em; }
.kb-doc pre { font-family: var(--mono); font-size: .8rem; padding: .6rem .7rem; border-radius: 6px;
              background: var(--surface-2); overflow-x: auto; }
.kb-doc table { border-collapse: collapse; }
.kb-doc th, .kb-doc td { border: 1px solid var(--line); padding: .3rem .5rem; text-align: left; }
.kb-doc [contenteditable="false"] { cursor: default; }
.kb-editor-actions { display: flex; gap: .5rem; align-items: center; }
.kb-editor-actions button { padding: .45rem .9rem; border-radius: 8px; border: 1px solid var(--line);
                            background: var(--surface); font: inherit; font-weight: 600; cursor: pointer; }
.kb-editor-actions button.primary { background: var(--brand); border-color: var(--brand); color: #fff; }
.kb-editor-actions button:disabled { opacity: .6; cursor: default; }
.kb-editor-note { font-size: .8rem; padding: .5rem .7rem; border-radius: 6px; }
.kb-editor-note.ok { background: #e4f5ea; color: #1c7a41; }
.kb-editor-note.err { background: #fbe4e0; color: #b23c1c; }
.kb-editor-note.warn { background: #fdf1dc; color: #8a5a12; }
"""
