"""Tests for kb_editor.py — the shared in-site KB document editor
(issue_01M17HYDBQ4GPWHPFS56M6QQPH).

**These run the controller for real in node against a DOM and fetch stub**,
rather than grepping the emitted source for expected strings. The thing under
test is a lease protocol — who holds a four-hour Library checkout and when it
goes back — and a string check cannot tell a released lock from a stranded one:
both spellings of the code look equally plausible on the page. What each test
asserts is the sequence of requests the controller actually made.

The stub models exactly the DOM surface the controller uses: `innerHTML` and a
`querySelector` that answers for an attribute selector when the assigned markup
contains it. Parsing is the browser's job, not this harness's; what the harness
is for is the ORDER of checkout, heartbeat, save and checkin.

**md-serial is a fake here, and Orient's glue is real.** The controller loads
both through `window.KbEditorLoadLibrary`, which this harness sets: the glue is
`editor_js/kb-doc.js` itself, imported from disk, and `mountEditor` is a fake
whose body a test "types" into by setting `_src`. md-serial cannot render into
this stub DOM, and is tested on its own in `test_kb_editor_md_serial.py`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import kb_editor

_KB_DOC_URL = (Path(kb_editor.__file__).resolve().parent / "editor_js" / "kb-doc.js").as_uri()

# Every element the controller touches. `querySelector` answers by looking for
# the selector's attribute text in the markup that was assigned, which is enough
# for `[data-kb="body"]` and friends and keeps a real parser out of the harness.
_DOM_STUB = r"""
function makeEl(attrs) {
  const el = {
    _html: '', _children: {}, _listeners: {}, _attrs: attrs || {}, value: '', hidden: false,
    disabled: false, className: '',
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = v; this._children = {}; },
    get textContent() { return this._text === undefined ? '' : this._text; },
    set textContent(v) { this._text = v; },
    querySelector(sel) {
      const marker = sel.replace(/^\[/, '').replace(/\]$/, '');
      if (!this._html.includes(marker)) return null;
      if (sel.startsWith('[data-kb-cmd=')) return this.querySelectorAll('[data-kb-cmd]')
        .find(b => sel === '[data-kb-cmd="' + b._attrs['data-kb-cmd'] + '"]') || null;
      if (!this._children[sel]) this._children[sel] = makeEl();
      return this._children[sel];
    },
    // The toolbar is the one place the controller asks for several elements:
    // one per `data-kb-cmd` button in the assigned markup, each carrying its name.
    querySelectorAll(sel) {
      if (sel !== '[data-kb-cmd]') { const one = this.querySelector(sel); return one ? [one] : []; }
      if (!this._children[sel]) {
        this._children[sel] = Array.from(this._html.matchAll(/data-kb-cmd="([a-z]+)"/g))
          .map(m => { const b = makeEl({ 'data-kb-cmd': m[1] }); b.dataset = { kbCmd: m[1] }; return b; });
      }
      return this._children[sel];
    },
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    fire(type, ev) { (this._listeners[type] || []).forEach(fn => fn(ev || {})); },
    getAttribute(name) { return name in this._attrs ? this._attrs[name] : null; },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    removeAttribute(name) { delete this._attrs[name]; }, focus() {},
  };
  return el;
}

// md-serial's mountEditor, faked. The body's text lives on the root element as
// `_src`; `typeBody` is what a member typing does to it. `_rtFail` makes the
// round-trip check fail, and `_throw` makes save() throw, as md-serial's do.
function fakeMountEditor(root, src, opts) {
  root._src = src;
  root._rev = (opts && opts.revision) || 0;
  root._seededRevision = opts ? opts.revision : undefined;
  root._cmds = [];
  const cmd = (name) => function (arg) { root._cmds.push(arg === undefined ? name : name + ':' + arg); root._rev++; return true; };
  return {
    save() { if (root._throw) throw new Error(root._throw); return root._src; },
    saveChecked() {
      const source = this.save();
      return { source, roundTrip: { ok: !root._rtFail, failures: root._rtFail ? [root._rtFail] : [] } };
    },
    revision() { return root._rev; },
    toggleBold: cmd('bold'), toggleItalic: cmd('italic'), toggleHeading: cmd('heading'),
    toggleBulletList: cmd('bullets'), toggleOrderedList: cmd('numbers'),
    toggleCodeBlock: cmd('code'), insertLink: cmd('link'),
  };
}
function typeBody(mount, text) {
  const body = mount.querySelector('[data-kb="body"]');
  body._src = text; body._rev++;
}
let libraryLoads = 0;
let libraryFails = false;
let libraryDelayMs = 0;

// Each entry is {action, method, body}. `action` is the last path segment, which
// for these routes is exactly the protocol step: checkout, heartbeat, save,
// checkin.
const calls = [];
// action -> { ok, status, body }. Anything unlisted answers 200 with {}.
let responses = {};
let confirmAnswer = true;
let confirmCount = 0;

global.fetch = function (url, init) {
  const action = String(url).split('/').pop();
  calls.push({ action: action, url: String(url), method: (init && init.method) || 'GET',
               body: init && init.body ? JSON.parse(init.body) : null });
  const r = responses[action] || { ok: true, status: 200, body: {} };
  if (r.reject) return Promise.reject(new Error('network down'));
  const answer = { ok: r.ok, status: r.status, json: () => Promise.resolve(r.body) };
  // `delayMs` is what lets a test close or switch the editor while a request is
  // still in flight — the window in which a checkout can be taken by a server
  // that no client state is left pointing at.
  if (!r.delayMs) return Promise.resolve(answer);
  return new Promise(resolve => setTimeout(() => resolve(answer), r.delayMs));
};
// Recorded rather than dropped: the unload handlers are lease paths, and a stub
// that swallowed them would leave a test green with both deleted.
const windowHandlers = {};
let promptAnswer = null;
global.window = {
  addEventListener(type, fn) { (windowHandlers[type] = windowHandlers[type] || []).push(fn); },
  fire(type, ev) { (windowHandlers[type] || []).forEach(fn => fn(ev || {})); },
  confirm() { confirmCount++; return confirmAnswer; },
  prompt() { return promptAnswer; },
  KbEditorLoadLibrary() {
    libraryLoads++;
    if (libraryFails) return Promise.reject(new Error('module failed to load'));
    const load = import(KB_DOC_URL).then(kb => ({ kb: kb, mountEditor: fakeMountEditor }));
    if (!libraryDelayMs) return load;
    return new Promise(resolve => setTimeout(() => resolve(load), libraryDelayMs));
  },
};
// node ships a read-only `navigator` global with no sendBeacon, so the feature
// test in the controller would take the fallback branch and the beacon would
// never be exercised. Defined here so it is.
const beacons = [];
Object.defineProperty(globalThis, 'navigator', {
  value: { sendBeacon(url) { beacons.push(String(url)); return true; } },
  configurable: true, writable: true,
});
// Captured rather than run on a timer: a test asserts what the heartbeat DOES
// when the lock has gone, and waiting out the real interval would only assert
// that node's clock works. `setTimeout` stays real — the drivers below use it
// to let promises settle.
const setIntervalCalls = [];
global.setInterval = function (fn) { setIntervalCalls.push(fn); return setIntervalCalls.length; };
global.clearInterval = function () {};

function actions() { return calls.map(c => c.action); }
"""


def _run(driver_js: str) -> dict:
    """Run the editor controller plus `driver_js` in node, returning the JSON
    object the driver printed. Skips when node is absent, matching the other
    node-dependent checks in this suite."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to exercise the editor JS")
    src = (f"'use strict';\nconst KB_DOC_URL = {json.dumps(_KB_DOC_URL)};\n"
           f"{_DOM_STUB}\n{kb_editor.EDITOR_JS}\n{driver_js}\n")
    r = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"editor JS harness failed:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


_CHECKOUT_OK = """
responses.checkout = { ok: true, status: 200,
  body: { content: '---\\ntitle: A\\n---\\n\\nbody text\\n', content_sha: 'sha-abc' } };
"""


def test_opening_checks_the_document_out_and_seeds_the_draft_from_it():
    """The editor opens on the STORED file, frontmatter block and all, because
    that is what the save route validates. The Library serves the reading view
    with the frontmatter stripped, so an editor seeded from the page could
    never save. The frontmatter block, with the blank line after it, goes to the
    YAML text box, and the body to md-serial."""
    result = _run(_CHECKOUT_OK + r"""
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  console.log(JSON.stringify({
    actions: actions(),
    checkoutUrl: calls[0].action,
    method: calls[0].method,
    frontmatter: mount.querySelector('[data-kb="fm"]').value,
    body: mount.querySelector('[data-kb="body"]')._src,
    docId: window.KbEditor.docId(),
    dirty: window.KbEditor.dirty(),
  }));
});
""")
    assert result["actions"] == ["checkout"]
    assert result["method"] == "POST"
    assert result["frontmatter"] == "---\ntitle: A\n---\n\n"
    assert result["body"] == "body text\n"
    assert result["docId"] == "proj/kind/doc-1"
    assert result["dirty"] is False, "a freshly opened editor has nothing unsaved"


def test_a_save_posts_back_the_sha_the_checkout_returned():
    """The save is gated on the revision the editor opened at: the server
    refuses it outright if origin/main moved under the open editor, rather than
    committing over the newer revision. The sha must therefore be the CHECKOUT
    response's, carried through untouched."""
    result = _run(_CHECKOUT_OK + r"""
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  typeBody(mount, 'edited text\n');
  mount.querySelector('[data-kb="save"]').fire('click');
  setTimeout(() => {
    const save = calls.find(c => c.action === 'save');
    console.log(JSON.stringify({ sentSha: save.body.content_sha, sentBody: save.body.body }));
  }, 20);
});
""")
    assert result["sentSha"] == "sha-abc", (
        "the save must carry the sha the checkout returned, not a fresh read")
    assert result["sentBody"] == "---\ntitle: A\n---\n\nedited text\n", (
        "the save sends the frontmatter joined to the edited body, the whole file")


def test_opening_a_second_document_checks_the_first_one_back_in():
    """**The rule the browse page exists to satisfy.** A reader clicking between
    documents must not accumulate checkouts: each one is a four-hour lock that
    blocks every other writer, the Library's own kb_update and kb_propose
    included. Ten documents read in a row used to mean ten stranded locks."""
    result = _run(_CHECKOUT_OK + r"""
const first = makeEl(), second = makeEl();
window.KbEditor.open(first, 'proj/kind/doc-1').then(() =>
  window.KbEditor.open(second, 'proj/kind/doc-2')).then(() => {
  setTimeout(() => console.log(JSON.stringify({
    actions: actions(),
    checkinUrl: (calls.find(c => c.action === 'checkin') || {}).url,
    docId: window.KbEditor.docId(),
  })), 20);
});
""")
    assert result["actions"] == ["checkout", "checkin", "checkout"], (
        "the first document's checkout must go back BEFORE the second is taken")
    assert result["docId"] == "proj/kind/doc-2"
    assert result["checkinUrl"] == "/api/kb/documents/proj%2Fkind%2Fdoc-1/checkin", (
        "the checkin must name the document being left, not the one being opened")


def test_reopening_the_same_document_does_not_take_a_second_checkout():
    """The browse page rebuilds the drawer's markup whenever the body lands or
    the reader re-opens the same row, and calls `open` each time. A second
    checkout on a lock this editor already holds would be a wasted round trip at
    best; releasing and re-taking it would be a window in which another writer
    could take it."""
    result = _run(_CHECKOUT_OK + r"""
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() =>
  window.KbEditor.open(mount, 'proj/kind/doc-1')).then(() => {
  setTimeout(() => console.log(JSON.stringify({ actions: actions() })), 20);
});
""")
    assert result["actions"] == ["checkout"]


def test_remount_keeps_the_draft_and_the_checkout():
    """The host rebuilds its markup around the editor — the drawer repaints when
    the document body arrives — so the controller paints its state into the new
    element. A draft lost to a repaint the reader never asked for is silent data
    loss, and a re-checkout would be a gap another writer could step into."""
    result = _run(_CHECKOUT_OK + r"""
const first = makeEl(), second = makeEl();
window.KbEditor.open(first, 'proj/kind/doc-1').then(() => {
  typeBody(first, 'half-written sentence');
  const revBefore = first.querySelector('[data-kb="body"]')._rev;
  window.KbEditor.remount(second);
  const body = second.querySelector('[data-kb="body"]');
  console.log(JSON.stringify({
    actions: actions(),
    draftAfterRemount: body._src,
    revBefore: revBefore,
    seededRevision: body._seededRevision,
    dirty: window.KbEditor.dirty(),
  }));
});
""")
    assert result["actions"] == ["checkout"], "a remount must not re-checkout"
    assert result["draftAfterRemount"] == "half-written sentence"
    assert result["seededRevision"] == result["revBefore"] == 1, (
        "md-serial's revision count must carry across the remount, or a consumer "
        "comparing it to a baseline reads 'no changes' after one")
    assert result["dirty"] is True


def test_closing_a_dirty_editor_releases_nothing_when_the_reader_declines():
    """`close` returning false is the host's signal to abort whatever navigation
    it was about to make. Releasing the checkout anyway would leave the reader
    looking at an editor they can no longer save."""
    result = _run(_CHECKOUT_OK + r"""
confirmAnswer = false;
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  typeBody(mount, 'unsaved work');
  const closed = window.KbEditor.close();
  console.log(JSON.stringify({
    closed: closed, actions: actions(), confirmCount: confirmCount,
    stillOpen: window.KbEditor.docId(),
  }));
});
""")
    assert result["closed"] is False
    assert result["confirmCount"] == 1
    assert result["actions"] == ["checkout"], "nothing may be checked in on a declined close"
    assert result["stillOpen"] == "proj/kind/doc-1"


def test_closing_a_clean_editor_checks_in_without_prompting():
    result = _run(_CHECKOUT_OK + r"""
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  const closed = window.KbEditor.close();
  setTimeout(() => console.log(JSON.stringify({
    closed: closed, actions: actions(), confirmCount: confirmCount,
    docId: window.KbEditor.docId(),
  })), 20);
});
""")
    assert result["closed"] is True
    assert result["confirmCount"] == 0, "an unedited editor must not ask about discarding"
    assert result["actions"] == ["checkout", "checkin"]
    assert result["docId"] is None


def test_a_refused_checkout_checks_nothing_in_on_close():
    """A document already held by someone else opens read-only. This editor
    never took the lock, so posting a checkin for it would be a request about
    another holder's lease."""
    result = _run(r"""
responses.checkout = { ok: false, status: 409,
  body: { checkout: { holder: 'library', expires_at: '2026-09-21T20:00:00Z' } } };
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  // Read before closing: `close` empties the mount, which is itself the point
  // — nothing of the editor is left behind for the reader to type into.
  const note = mount.querySelector('[data-kb="note"]').textContent;
  window.KbEditor.close();
  setTimeout(() => console.log(JSON.stringify({ actions: actions(), note: note })), 20);
});
""")
    assert result["actions"] == ["checkout"], (
        "a refused checkout leaves nothing to check in")
    assert "library" in result["note"], "the reader is told who holds it"


def test_a_successful_save_releases_the_lock_so_close_posts_no_second_checkin():
    """The save route checks the document back in itself. A close that posted
    another checkin would be a request about a lease this editor no longer
    holds — and by then possibly somebody else's."""
    result = _run(_CHECKOUT_OK + r"""
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  mount.querySelector('[data-kb="save"]').fire('click');
  setTimeout(() => {
    const note = mount.querySelector('[data-kb="note"]').textContent;
    window.KbEditor.close();
    setTimeout(() => console.log(JSON.stringify({
      actions: actions(), confirmCount: confirmCount, note: note,
    })), 20);
  }, 20);
});
""")
    assert result["actions"] == ["checkout", "save"], (
        "a saved document is already checked in; closing must not post another")
    assert result["note"] == "Saved."
    assert result["confirmCount"] == 0, (
        "a save moves the unsaved-changes baseline, so closing right after one "
        "must not ask the reader to discard anything")


def test_a_save_that_could_not_release_the_checkout_says_so():
    result = _run(_CHECKOUT_OK + r"""
responses.save = { ok: true, status: 200,
  body: { checked_in: false, warning: 'Saved, but the checkout could not be released.' } };
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  mount.querySelector('[data-kb="save"]').fire('click');
  setTimeout(() => console.log(JSON.stringify({
    note: mount.querySelector('[data-kb="note"]').textContent,
  })), 20);
});
""")
    assert "could not be released" in result["note"]


def test_the_four_save_failures_are_each_distinguishable_to_the_member():
    """DoD (issue_01M17HYDBQ4GPWHPFS56M6QQPH): a member must be able to tell
    WHICH way a save failed. Frontmatter that does not validate, a lock that
    lapsed, a server error and an unreachable server each produce their own
    message — a single "couldn't save" for all four would leave the member with
    no idea whether to fix the document or reopen it."""
    result = _run(_CHECKOUT_OK + r"""
const notes = {};
function saveWith(key, resp, next) {
  responses.save = resp;
  const mount = makeEl();
  window.KbEditor.open(mount, 'proj/kind/' + key).then(() => {
    mount.querySelector('[data-kb="save"]').fire('click');
    setTimeout(() => {
      notes[key] = mount.querySelector('[data-kb="note"]').textContent;
      window.KbEditor.close({ confirmDirty: false });
      next();
    }, 20);
  });
}
saveWith('invalid', { ok: false, status: 422, body: { errors: ['audience is required'] } }, () =>
saveWith('lock', { ok: false, status: 409, body: { error: 'the checkout lapsed' } }, () =>
saveWith('server', { ok: false, status: 500, body: { error: 'commit failed' } }, () => {
  // The unreachable case is the fetch itself rejecting, which no response
  // fixture can express — it is the `.catch` branch, and the only one of the
  // four a response-shaped stub would miss.
  responses.save = null;
  const realFetch = global.fetch;
  const mount = makeEl();
  window.KbEditor.open(mount, 'proj/kind/offline').then(() => {
    global.fetch = (url, init) => String(url).endsWith('/save')
      ? Promise.reject(new Error('network down')) : realFetch(url, init);
    mount.querySelector('[data-kb="save"]').fire('click');
    setTimeout(() => {
      notes.offline = mount.querySelector('[data-kb="note"]').textContent;
      console.log(JSON.stringify(notes));
    }, 20);
  });
})));
""")
    assert "audience is required" in result["invalid"]
    assert "Frontmatter" in result["invalid"]
    assert "checkout lapsed" in result["lock"]
    assert "commit failed" in result["server"]
    assert "reach the server" in result["offline"]
    assert len(set(result.values())) == 4, (
        f"each failure needs its own message; got {result}")


def test_a_lapsed_heartbeat_stops_the_editor_and_says_why():
    """The heartbeat is the backstop for a lock that went away — reclaimed, or
    simply expired. It must stop the editor rather than leave a textarea that
    accepts typing no save will ever accept."""
    result = _run(_CHECKOUT_OK + r"""
responses.heartbeat = { ok: false, status: 409, body: { error: 'the checkout lapsed or was reclaimed' } };
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  // Rather than waiting out the real interval, drive the tick the controller
  // registered: this asserts the heartbeat's HANDLING, and the interval itself
  // is a constant checked separately below.
  setIntervalCalls[setIntervalCalls.length - 1]();
  setTimeout(() => console.log(JSON.stringify({
    actions: actions(),
    note: mount.querySelector('[data-kb="note"]').textContent,
    disabled: mount.querySelector('[data-kb="body"]').disabled,
    contenteditable: mount.querySelector('[data-kb="body"]').getAttribute('contenteditable'),
    saveDisabled: mount.querySelector('[data-kb="save"]').disabled,
  })), 20);
});
""")
    assert result["actions"] == ["checkout", "heartbeat"]
    assert "lapsed" in result["note"]
    assert result["disabled"] is True, "a lapsed lock must stop accepting typing"
    assert result["contenteditable"] == "false", (
        "md-serial's root is contenteditable; a lapsed lock must take that back")
    assert result["saveDisabled"] is True


def test_the_heartbeat_interval_is_well_inside_any_plausible_lease_grace():
    """The Library's `lease.py` owns the real expiry and this module cannot read
    it, so the cadence is chosen to be comfortably short rather than tuned. What
    matters is that it is minutes, not tens of minutes."""
    assert 60_000 <= kb_editor._HEARTBEAT_INTERVAL_MS <= 10 * 60_000


# --- a checkout still in flight when the editor goes away --------------------
# The lock nothing else will ever release: `close` gates its checkin on
# `heldLock`, which is only set when the checkout ANSWERS. Found in review of
# PR #315.

def test_closing_before_the_checkout_answers_still_checks_it_in():
    """Edit then Close — or Escape, or the X — before the response lands used to
    leave the Library holding a four-hour lock with no editor on screen and no
    state left pointing at it."""
    result = _run(r"""
responses.checkout = { ok: true, status: 200, delayMs: 30,
  body: { content: 'stored file', content_sha: 'sha-1' } };
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1');
window.KbEditor.close();   // before the checkout has answered
setTimeout(() => console.log(JSON.stringify({
  actions: actions(),
  checkinUrl: (calls.find(c => c.action === 'checkin') || {}).url,
  docId: window.KbEditor.docId(),
})), 80);
""")
    assert result["actions"] == ["checkout", "checkin"], (
        "the lock the in-flight checkout took must still be handed back")
    assert result["checkinUrl"] == "/api/kb/documents/proj%2Fkind%2Fdoc-1/checkin"
    assert result["docId"] is None


def test_switching_before_the_checkout_answers_checks_the_first_one_in():
    """The same hole reached by clicking Edit on a second document while the
    first one's checkout is still in flight."""
    result = _run(r"""
responses.checkout = { ok: true, status: 200, delayMs: 30,
  body: { content: 'stored file', content_sha: 'sha-1' } };
const first = makeEl(), second = makeEl();
window.KbEditor.open(first, 'proj/kind/doc-1');
window.KbEditor.open(second, 'proj/kind/doc-2');
setTimeout(() => console.log(JSON.stringify({
  checkins: calls.filter(c => c.action === 'checkin').map(c => c.url),
  docId: window.KbEditor.docId(),
})), 120);
""")
    assert "/api/kb/documents/proj%2Fkind%2Fdoc-1/checkin" in result["checkins"], (
        "the first document's in-flight lock must be released")
    assert result["docId"] == "proj/kind/doc-2"


def test_a_refused_in_flight_checkout_is_not_checked_in():
    """The mirror of the case above: a checkout the server REFUSED took no lock,
    so a checkin for it would be a request about another holder's lease."""
    result = _run(r"""
responses.checkout = { ok: false, status: 409, delayMs: 30,
  body: { checkout: { holder: 'library' } } };
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1');
window.KbEditor.close();
setTimeout(() => console.log(JSON.stringify({ actions: actions() })), 80);
""")
    assert result["actions"] == ["checkout"]


def test_a_checkout_request_that_failed_on_this_side_hands_the_lock_back():
    """A rejected fetch does not prove the server never took the lock — the
    checkout may have completed and the answer been lost. The editor is not
    going to edit anything either way, so it hands the lock back; a checkin is
    scoped to its own holder, so at worst the Library refuses one never taken."""
    result = _run(r"""
responses.checkout = { reject: true };
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  setTimeout(() => console.log(JSON.stringify({
    actions: actions(),
    note: mount.querySelector('[data-kb="note"]').textContent,
  })), 20);
});
""")
    assert result["actions"] == ["checkout", "checkin"]
    assert "reach the server" in result["note"]


# --- what the heartbeat may and may not conclude -----------------------------

def test_a_heartbeat_that_could_not_ask_keeps_the_lock():
    """**A FAILURE TO ASK IS NOT EVIDENCE THE LOCK LAPSED.** The route answers
    503 when Library writes are off, and a 5xx when the Library itself fails;
    treating either as a lost lock would clear the flag `close` and the unload
    beacon use, stranding a lock that is still perfectly alive."""
    result = _run(_CHECKOUT_OK + r"""
responses.heartbeat = { ok: false, status: 503, body: { error: 'locking is not turned on' } };
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  setIntervalCalls[setIntervalCalls.length - 1]();
  setTimeout(() => {
    const note = mount.querySelector('[data-kb="note"]').textContent;
    const stillEditable = mount.querySelector('[data-kb="body"]').disabled === false;
    window.KbEditor.close();
    setTimeout(() => console.log(JSON.stringify({
      actions: actions(), note: note, stillEditable: stillEditable,
    })), 20);
  }, 20);
});
""")
    assert result["actions"] == ["checkout", "heartbeat", "checkin"], (
        "a lock the heartbeat merely failed to reach must still be handed back on close")
    assert result["stillEditable"] is True, (
        "a transient heartbeat failure must not stop the member typing")
    assert result["note"] == "locking is not turned on", (
        "the member sees the server's own reason, and is warned rather than cut off")


def test_a_lapsed_lock_is_not_checked_in_again_on_close():
    """A 409 heartbeat means the lock is genuinely gone — reclaimed, or expired.
    Posting a checkin for it afterwards would be a request about a lease that
    may by then be somebody else's."""
    result = _run(_CHECKOUT_OK + r"""
responses.heartbeat = { ok: false, status: 409, body: { error: 'the checkout lapsed' } };
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  setIntervalCalls[setIntervalCalls.length - 1]();
  setTimeout(() => {
    window.KbEditor.close();
    setTimeout(() => console.log(JSON.stringify({ actions: actions() })), 20);
  }, 20);
});
""")
    assert result["actions"] == ["checkout", "heartbeat"]


# --- leaving the page --------------------------------------------------------

def test_leaving_the_page_releases_the_lock_by_beacon():
    """`sendBeacon` is the only request shape that survives unload. Without it,
    closing the tab over an open editor strands the lock for the Library's full
    TTL — which is what the document's own page has always done."""
    result = _run(_CHECKOUT_OK + r"""
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  window.fire('pagehide');
  console.log(JSON.stringify({ beacons: beacons }));
});
""")
    assert result["beacons"] == ["/api/kb/documents/proj%2Fkind%2Fdoc-1/checkin"]


def test_leaving_the_page_with_nothing_open_sends_no_beacon():
    """The positive control's mirror: a handler that fired unconditionally would
    pass the test above just as well."""
    result = _run(_CHECKOUT_OK + r"""
window.fire('pagehide');
console.log(JSON.stringify({ beacons: beacons }));
""")
    assert result["beacons"] == []


def test_an_unsaved_draft_prompts_before_the_page_unloads():
    """The browser's own prompt is the last guard on a draft that nothing can
    recover once the page is gone. It fires before `pagehide`, so a member who
    chooses to leave still releases the lock."""
    result = _run(_CHECKOUT_OK + r"""
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  const clean = { prevented: false, preventDefault() { this.prevented = true; } };
  window.fire('beforeunload', clean);
  typeBody(mount, 'unsaved work');
  const dirty = { prevented: false, preventDefault() { this.prevented = true; } };
  window.fire('beforeunload', dirty);
  console.log(JSON.stringify({ cleanPrevented: clean.prevented, dirtyPrevented: dirty.prevented }));
});
""")
    assert result["dirtyPrevented"] is True
    assert result["cleanPrevented"] is False, (
        "an editor with nothing unsaved must not prompt on every navigation")


# --- the body is md-serial's (issue_01M35KC8PZMZBMJKX4RZR9F1Y8) ----------------

def test_a_save_whose_markdown_does_not_round_trip_is_refused_and_keeps_the_draft():
    """md-serial has lost content silently on regeneration before, and one such
    case is still open (sig_01M3V19ATQC4JQX3QXRHMG9F7P). `saveChecked` reparses
    what it produced against what the member was looking at; a mismatch must
    stop the save, say why, and leave the edits in place."""
    result = _run(_CHECKOUT_OK + r"""
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  typeBody(mount, 'two paragraphs\n\nmerged on save\n');
  mount.querySelector('[data-kb="body"]')._rtFail = 'block 2: paragraph merged into block 1';
  mount.querySelector('[data-kb="save"]').fire('click');
  setTimeout(() => console.log(JSON.stringify({
    actions: actions(),
    note: mount.querySelector('[data-kb="note"]').textContent,
    dirty: window.KbEditor.dirty(),
  })), 20);
});
""")
    assert result["actions"] == ["checkout"], "nothing may be posted when the round trip fails"
    assert "Not saved" in result["note"]
    assert "paragraph merged" in result["note"], "the member is told what differed"
    assert result["dirty"] is True


def test_a_body_md_serial_cannot_serialise_is_not_saved_and_counts_as_unsaved():
    """md-serial's save() throws on some shapes. A throw must not escape the Save
    button silently, and `dirty` must say "unsaved" so closing asks first."""
    result = _run(_CHECKOUT_OK + r"""
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  mount.querySelector('[data-kb="body"]')._throw = 'unsupported nesting';
  mount.querySelector('[data-kb="save"]').fire('click');
  setTimeout(() => console.log(JSON.stringify({
    actions: actions(),
    note: mount.querySelector('[data-kb="note"]').textContent,
    dirty: window.KbEditor.dirty(),
  })), 20);
});
""")
    assert result["actions"] == ["checkout"]
    assert "unsupported nesting" in result["note"]
    assert result["dirty"] is True


def test_an_edited_frontmatter_block_is_what_the_save_sends():
    """The frontmatter is edited as text above the body, and the save commits the
    block the member submitted, never a stored copy."""
    result = _run(_CHECKOUT_OK + r"""
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  const fm = mount.querySelector('[data-kb="fm"]');
  fm.value = '---\ntitle: B\nstatus: draft\n---\n\n';
  fm.fire('input');
  const dirty = window.KbEditor.dirty();
  mount.querySelector('[data-kb="save"]').fire('click');
  setTimeout(() => console.log(JSON.stringify({
    dirty: dirty,
    sent: calls.find(c => c.action === 'save').body.body,
  })), 20);
});
""")
    assert result["dirty"] is True, "a frontmatter edit alone is an unsaved change"
    assert result["sent"] == "---\ntitle: B\nstatus: draft\n---\n\nbody text\n"


def test_each_toolbar_button_runs_its_md_serial_command():
    """The buttons dispatch through kb-doc.js's COMMANDS table to md-serial's
    commands. Link asks for the address and passes it on."""
    result = _run(_CHECKOUT_OK + r"""
promptAnswer = 'https://example.com/a';
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  const names = [];
  for (const btn of mount.querySelectorAll('[data-kb-cmd]')) {
    names.push(btn.getAttribute('data-kb-cmd'));
    btn.fire('click');
  }
  const body = mount.querySelector('[data-kb="body"]');
  console.log(JSON.stringify({ names: names, cmds: body._cmds, dirty: window.KbEditor.dirty() }));
});
""")
    assert result["names"] == ["heading", "bold", "italic", "bullets", "numbers", "link", "code"]
    assert result["cmds"] == ["heading:2", "bold", "italic", "bullets", "numbers",
                              "link:https://example.com/a", "code:"]


def test_a_cancelled_link_prompt_changes_nothing():
    result = _run(_CHECKOUT_OK + r"""
promptAnswer = null;
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then(() => {
  mount.querySelector('[data-kb-cmd="link"]').fire('click');
  console.log(JSON.stringify({ cmds: mount.querySelector('[data-kb="body"]')._cmds }));
});
""")
    assert result["cmds"] == []


def test_an_editor_that_cannot_load_hands_the_lock_back():
    """The checkout took a four-hour lock. If md-serial cannot be loaded nothing
    can edit under it, so it goes straight back and the member is told."""
    result = _run(_CHECKOUT_OK + r"""
libraryFails = true;
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1').then((opened) => {
  setTimeout(() => console.log(JSON.stringify({
    opened: opened,
    actions: actions(),
    note: mount.querySelector('[data-kb="note"]').textContent,
    dirty: window.KbEditor.dirty(),
  })), 20);
});
""")
    assert result["opened"] is False
    assert result["actions"] == ["checkout", "checkin"]
    assert "Couldn't load the editor" in result["note"]
    assert result["dirty"] is False


def test_closing_while_the_editor_loads_checks_in_exactly_once():
    """The lock is held from the moment the checkout answers, so a close during
    the library load releases it through `close`; the load's own callback, finding
    the editor gone, must not post a second checkin about a lease that may by
    then be somebody else's."""
    result = _run(_CHECKOUT_OK + r"""
libraryDelayMs = 40;
const mount = makeEl();
window.KbEditor.open(mount, 'proj/kind/doc-1');
setTimeout(() => {
  window.KbEditor.close();   // checkout answered, library still loading
  setTimeout(() => console.log(JSON.stringify({ actions: actions() })), 80);
}, 10);
""")
    assert result["actions"] == ["checkout", "checkin"]
