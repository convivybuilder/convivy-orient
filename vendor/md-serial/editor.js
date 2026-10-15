// editor.js — WYSIWYG editing layer for md-serial documents.
// ES module — zero external dependencies.
//
// Consumer entry point:
//   import { mountEditor } from './editor.js'
//
// mountEditor(root, source, opts = {}) →
//   { save(), saveChecked(), isDirtySinceMount(), revision(), doc,
//     toggleBold(), toggleItalic(), insertLink(href, title),
//     toggleHeading(level), toggleBulletList(), toggleOrderedList(),
//     toggleCodeBlock(lang) }
//
//   root    An element the editor fills.  mountEditor sets contenteditable on
//           it and appends the rendered DOM.
//   source  The original Markdown string.
//
//   opts.resolveWikilink  Passed through to render(); same contract.
//   opts.document         The DOM document to build with.  Defaults to
//                         root.ownerDocument.
//   opts.revision         The value revision() starts from.  A non-negative
//                         integer, or omitted for a first mount (0).  Throws
//                         EditorError on anything else.  See revision() below
//                         for why a remount must pass one.
//
//   save()               Returns the new Markdown source string.
//   saveChecked()        Returns { source, roundTrip: { ok, failures } }.
//              `source` is exactly save()'s return value — the same call, not
//              a duplicate of its logic. `roundTrip` re-parses `source` and
//              compares it, block by block, against the live DOM (rootEl,
//              the document as this mount currently shows it, not the
//              mount-time `doc`), including every attribute inlineToMarkdown
//              and blockToMarkdown serialise into Markdown — a link's href
//              and title, a wikilink's or chip's target, an image's src/alt/
//              title, and a fenced code block's language — not just block
//              structure and visible text. `ok` is true exactly when `failures`
//              is empty; each failure is a human-readable string naming the
//              block and what differed. This is the backstop for a consumer
//              that signs and commits `source` as if it always matches what the
//              member was looking at — save() itself has no such check, and
//              still returns a plain string, unchanged. If save() throws,
//              saveChecked() throws the same error.
//   isDirtySinceMount()  True once any input event has fired on the editor
//              root, or a formatting command reported a change. Scoped to
//              THIS mount and deliberately not seeded: it answers "was this
//              instance edited", which is what the save path wants. Named for
//              that scope, not just for what it does — "isDirty" invites a
//              consumer to reach for it across a remount and read "not dirty"
//              against a baseline from before the edits, which is the exact
//              silent loss revision() exists to prevent. A consumer asking
//              "was the document edited since I opened it" wants revision()
//              against a stored baseline, which opts.revision carries across
//              remounts.
//   revision() Integer, starts at opts.revision (0 when omitted), increases by
//              one on every change the document undergoes — a native input
//              event, or a formatting command that reports it changed
//              something. A consumer decides "unsaved changes" by diffing a
//              draft it gets from save() against the text a lease checkout
//              returned, and a formatting command can change the document
//              without changing textContent (bold → italic over the same
//              characters), so nothing tells such a consumer to call save()
//              again at all. revision() is that signal. A counter rather than
//              a change callback: this consumer mounts and unmounts
//              constantly, and a counter needs no subscription to leak or
//              clean up across that lifecycle.
//
//              THE COUNTER DOES NOT SURVIVE A REMOUNT ON ITS OWN, and
//              opts.revision is how a consumer carries it across one. rev is
//              closure state per mountEditor call, and md-serial has no
//              remount concept — a remount is a fresh mount. A consumer that
//              remounts without seeding reads 0 against a baseline it stored
//              before the edits, concludes nothing changed, and discards the
//              draft: the exact silent loss revision() exists to prevent,
//              one layer over. So a remounting consumer reads the outgoing
//              instance's revision() and passes it here:
//
//                  const next = mountEditor(root, draft, { revision: prev.revision() });
//
//              Seeding rather than composing in the consumer is deliberate.
//              A consumer that remounts from a repaint path has many such
//              call sites, and one that forgets to carry the count fails
//              silently in the direction that loses work.
//   doc        The parsed md-serial document, exposed for tests.
//
//   Formatting commands read the current selection off opts.document
//   (ownerDoc.getSelection() / a Range's boundary-point properties — the real
//   browser API in production). Each reports whether it changed anything.
//   toggleBold(), toggleItalic() and insertLink(href, title) act on a
//   selection contained in a single text node — the common case of selecting
//   a run of text within one block — and no-op (return false) for a
//   selection spanning more than one node, rather than guess at a splice.
//   They also no-op inside a fenced code block: fenced code is unlocked (see
//   toggleCodeBlock below) but blockToMarkdown emits a PRE's code text
//   verbatim with no inline markup, so an inline command there would report
//   a change that save() then silently drops.
//   toggleHeading(level), toggleBulletList(), toggleOrderedList() and
//   toggleCodeBlock(lang) act on the top-level block containing the
//   selection. A locked block (frontmatter, table, chip, thematic_break)
//   refuses every command. toggleBulletList()/toggleOrderedList() also
//   refuse to unwrap a single-item list whose item holds block children
//   (a nested list, a loose second paragraph) rather than flatten them.
//   None of these render UI — the consumer wires its own toolbar or key
//   bindings to the same calls.
//
// HOW SAVE WORKS
//   At mount, toDOM returns Map<mdSerialNode, element>.  The map is inverted
//   once to get Map<element, mdSerialNode>.  Each top-level block's structural
//   signature (tag, attributes, and recursively its children — see
//   elementSignature()) is also recorded at mount.  save() walks the document
//   root element's top-level children in DOM order.  For each child:
//     - If the element's signature is unchanged from mount AND the inverted
//       map knows its node: source.slice(node.start, node.end) — verbatim.
//     - Otherwise (signature changed, or new block with no node): Markdown
//       generated from the element's DOM subtree.
//   Locked elements (frontmatter, chips) always emit their verbatim source
//   slice. A table is not locked for this purpose — see TABLE EDITING below —
//   so an untouched one still takes the verbatim path (its signature never
//   changed) and an edited one regenerates through tableToMarkdown.
//
// TABLE EDITING
//   A table's row/column structure stays locked (contenteditable="false", and
//   every formatting command above still refuses a selection inside one,
//   exactly as for any other locked block) — only each cell carries its own
//   contenteditable="true" island, nested inside the locked table, so its
//   text can be typed into directly. Moving or deleting the whole table
//   works the same as before. A table nested inside a list item is the one
//   exception: it stays fully locked, cells included, because liToMarkdown
//   always re-emits its recorded verbatim bytes (lockedListBlockSource) for
//   one regardless of what the DOM holds, so a cell edit there would be
//   silently dropped on save rather than persisted.
//
//   The signature is structural rather than textContent-based, because an
//   attribute-only edit — e.g. a formatting command wrapping unchanged
//   characters in a new element — leaves textContent identical. A
//   textContent-only comparison would take the source-slice path and
//   silently drop the new markup: save() would report success over the
//   untouched original bytes.
//
//   NOTE: save() does not read the data-dirty attribute.  Routing is decided
//   entirely by the signature comparison (content-authority) and the
//   locked-type check.  A data-dirty attribute on any element is inert in the
//   save path.

import { parse } from './md-serial.js';
import { render, toDOM, isSafeUrl } from './render.js';

// ─── public API ──────────────────────────────────────────────────────────────

export function mountEditor(root, source, opts = {}) {
  const startingRevision = readStartingRevision(opts.revision);
  const ownerDoc = opts.document || root.ownerDocument;
  const doc = parse(source);
  const tree = render(doc, source, { resolveWikilink: opts.resolveWikilink });
  const map = toDOM(tree, ownerDoc);
  const rootEl = map.get(doc);

  // Invert map: DOM element → md-serial node.
  const inverseMap = new Map();
  for (const [node, el] of map) {
    inverseMap.set(el, node);
  }

  // Precompute every list-nested <table>/<hr>'s verbatim bytes now, while each
  // one's DOM element is still under the list item it was actually parsed
  // from. See verbatimListBlock for why this can't be deferred to save time.
  // Computed before markLocked, below, which needs it to tell a list-nested
  // table (stays fully locked) from any other (gets a per-cell island).
  const lockedListBlockSource = new Map();
  collectLockedListBlockSource(doc, map, lockedListBlockSource);

  // Every table except a list-nested one (see TABLE EDITING, above) gets a
  // contenteditable island per cell.
  const editableTableEls = new Set();
  for (const [node, el] of map) {
    if (node.type === 'table' && !lockedListBlockSource.has(el)) editableTableEls.add(el);
  }

  // Record what each editable table row serialises to before anything is
  // edited, so tableRowToMarkdown can tell an untouched row from an edited one
  // by comparing serialiser output with serialiser output.
  for (const tableEl of editableTableEls) {
    for (const section of tableEl.children) {
      for (const row of section.children) {
        initialTableRowText.set(row, tableCellTexts(Array.from(row.children)));
      }
    }
  }

  // Mark locked elements contenteditable=false throughout the DOM tree, so the
  // browser treats them as inert objects that can only be moved or deleted —
  // except an editable table's own cells, carved out as islands within it.
  markLocked(rootEl, editableTableEls);

  // Record each top-level block's structural signature at mount.  save() uses
  // this as the authority for whether a block was edited, without relying on
  // the input event marker (which the browser may not fire for drag-drop or
  // Enter-split), and without relying on textContent, which an attribute-only
  // edit (e.g. a formatting command) leaves unchanged. See elementSignature().
  const initialContent = new Map();
  for (const child of rootEl.children) {
    initialContent.set(child, elementSignature(child));
  }

  // Set root as the editable surface and insert the rendered document.
  root.setAttribute('contenteditable', 'true');
  root.appendChild(rootEl);

  let dirty = false;
  let rev = startingRevision;

  // Track that any edit occurred (isDirtySinceMount() signal only — not used for save routing).
  root.addEventListener('input', () => { dirty = true; rev++; });

  // Every formatting command routes its DOM mutation through this, so a
  // command that changed something bumps revision() and isDirtySinceMount()
  // exactly like a native input event does.
  function applyCommand(fn) {
    const changed = fn();
    if (changed) { dirty = true; rev++; }
    return changed;
  }

  // Named rather than inlined as save()'s method body, so saveChecked() below
  // calls this exact function instead of duplicating it or reaching through
  // `this` (fragile for an object-literal method passed around by reference).
  function doSave() {
    // Empty document (no block children) — return source verbatim.
    if (doc.children.length === 0) return source.slice(doc.start, doc.end);
    return saveDocument(rootEl, inverseMap, source, initialContent, lockedListBlockSource);
  }

  return {
    doc,
    save: doSave,
    // Re-parses save()'s output and compares it against the live DOM (rootEl) —
    // the document as the member currently sees and edits it — rather than
    // against the closure's `doc`, which is the mount-time parse and never
    // updates as edits happen. See checkRoundTrip() below for why the DOM is
    // the right side of that comparison and roundTripCheck() for what "compares"
    // means, block by block.
    saveChecked() {
      const savedSource = doSave();
      const roundTrip = checkRoundTrip(savedSource, rootEl, ownerDoc, opts.resolveWikilink);
      return { source: savedSource, roundTrip };
    },
    isDirtySinceMount() { return dirty; },
    revision() { return rev; },

    toggleBold() {
      return applyCommand(() => toggleInlineWrap(ownerDoc, rootEl, inverseMap, 'strong'));
    },
    toggleItalic() {
      return applyCommand(() => toggleInlineWrap(ownerDoc, rootEl, inverseMap, 'em'));
    },
    insertLink(href, title) {
      return applyCommand(() => insertLinkImpl(ownerDoc, rootEl, inverseMap, href, title));
    },
    toggleHeading(level) {
      return applyCommand(() => toggleHeadingImpl(ownerDoc, rootEl, inverseMap, level));
    },
    toggleBulletList() {
      return applyCommand(() => toggleListImpl(ownerDoc, rootEl, inverseMap, 'UL'));
    },
    toggleOrderedList() {
      return applyCommand(() => toggleListImpl(ownerDoc, rootEl, inverseMap, 'OL'));
    },
    toggleCodeBlock(lang) {
      return applyCommand(() => toggleCodeBlockImpl(ownerDoc, rootEl, inverseMap, lang));
    },
  };
}

// ─── starting revision ───────────────────────────────────────────────────────

// Validate opts.revision and return the value revision() starts from.
// Omitted (or undefined) means a first mount: 0.
//
// Anything else throws rather than coercing: a coerced seed would make the
// counter answer "nothing changed" for the rest of the session, silently.
// Number(undefined) and parseInt('') are NaN, and NaN++ stays NaN, so
// revision() would return NaN forever and `current > baseline` would be
// false at every comparison. A negative seed is the same failure with a
// delay: it counts up through the baseline before any real edit does. A
// mount is the only moment this value can be checked, so it is checked
// before anything is built.
function readStartingRevision(value) {
  if (value === undefined) return 0;
  // Number.isInteger() is false for every non-number, NaN and Infinity
  // included, so it carries the type check as well as the integer one.
  if (!Number.isInteger(value) || value < 0) {
    throw new EditorError(
      `mountEditor: opts.revision must be a non-negative integer or omitted, got ${typeof value} ${String(value)}`,
    );
  }
  return value;
}

// ─── structural signature ─────────────────────────────────────────────────────

// A canonical string encoding of an element's tag, attributes and recursive
// children, used only to detect whether a block changed since mount — never
// as serialized output. Unlike textContent, it distinguishes a formatting
// command's tag/attribute-only edit (e.g. wrapping unchanged characters in a
// new <strong>) from a genuine no-op, which is the case a textContent
// comparison cannot see.
//
// Any non-element node (nodeType !== 1) — a text node, or a Comment
// (nodeType 8) a native contenteditable paste can produce — is treated the
// same opaque way: its .data stands in for content, since a non-element has
// no .attributes or .childNodes to walk. A Comment appearing where none
// existed at mount changes the signature, which is what forces the block to
// regenerate; inlineToMarkdown's matching non-element case (below) is what
// then drops it, since Markdown has no comment syntax to emit it as.
function elementSignature(node) {
  if (node.nodeType !== 1) return JSON.stringify(node.data ?? '');
  const attrs = Array.from(node.attributes)
    .map(a => `${a.name}=${JSON.stringify(a.value)}`)
    .sort()
    .join(' ');
  const children = Array.from(node.childNodes).map(elementSignature).join('');
  return `<${node.tagName} ${attrs}>${children}</${node.tagName}>`;
}

// ─── save ────────────────────────────────────────────────────────────────────

function saveDocument(rootEl, inverseMap, source, initialContent, lockedListBlockSource) {
  const parts = [];
  let lastWasGenerated = false;
  // .children is elements-only in the real DOM — no text nodes appear here.
  for (const child of rootEl.children) {
    const node = inverseMap.get(child);

    if (node != null && LOCKED_NODE_TYPES.has(node.type)) {
      // Locked block — always emit original bytes.
      parts.push(source.slice(node.start, node.end));
      lastWasGenerated = false;
      continue;
    }

    // Content-authority: use the source slice only when the block was present
    // at mount AND its structural signature has not changed since then.
    const contentUnchanged = initialContent.has(child)
      && initialContent.get(child) === elementSignature(child);

    if (node != null && contentUnchanged) {
      // Untouched block with a known node — verbatim original bytes.
      parts.push(source.slice(node.start, node.end));
      lastWasGenerated = false;
    } else {
      // Changed block, or new block with no parse node — regenerate from DOM.
      parts.push(blockToMarkdown(child, inverseMap, lockedListBlockSource));
      lastWasGenerated = true;
    }
  }

  const raw = parts.join('');

  // Normalize trailing newlines only when the final block was regenerated.
  // blockToMarkdown always emits \n\n; the source may end with a single \n.
  // Source-slice tails are verbatim and correct — no normalization needed.
  if (!lastWasGenerated) return raw;
  const srcTrailing = /\n*$/.exec(source)[0];
  return raw.replace(/\n*$/, srcTrailing);
}

// Node types whose save path is always the verbatim source slice.
// Note: thematic_break (rendered as <hr>) can also be regenerated by
// blockToMarkdown; locking it preserves the original separator style.
// table is deliberately not here — see TABLE EDITING, above — so an edited
// one takes the same content-authority path as a paragraph or heading and
// regenerates through tableToMarkdown; an untouched one still emits its
// verbatim slice because its signature never changed.
const LOCKED_NODE_TYPES = new Set([
  'frontmatter', 'footnote_def', 'chip', 'thematic_break',
]);

// ─── round-trip check (saveChecked) ───────────────────────────────────────────
//
// The ground truth this compares against is the live DOM (rootEl), not the
// closure's `doc`. `doc` is the parse from mount and never changes after
// that — comparing against it would miss every edit the member has made
// since, which is exactly the content a save is being asked to persist.
// rootEl is what save()'s own DOM-to-Markdown path (blockToMarkdown,
// liToMarkdown, inlineToMarkdown, above) reads from, so it is also the one
// place the known content-loss bug in liToMarkdown (see its own comment,
// below) would be visible: the live DOM still holds a dropped block's real
// content; only save()'s regenerated Markdown has lost it.
//
// Re-parsing and re-rendering `source` (rather than diffing text) means the
// comparison runs over the same DOM shape save() itself works with, so the
// same block-type/nesting cases below apply to both sides symmetrically.
function checkRoundTrip(source, rootEl, ownerDoc, resolveWikilink) {
  const reparsedDoc = parse(source);
  const reparsedTree = render(reparsedDoc, source, { resolveWikilink });
  const reparsedMap = toDOM(reparsedTree, ownerDoc);
  const reparsedRootEl = reparsedMap.get(reparsedDoc);
  return roundTripCheck(rootEl, reparsedRootEl);
}

// Compares two document-root elements top-level block by top-level block.
// Each failure names the top-level block's index — the unit save() itself
// routes independently (verbatim slice vs. regenerate) — and what differed.
function roundTripCheck(liveRootEl, reparsedRootEl) {
  const failures = [];
  const liveBlocks = Array.from(liveRootEl.children);
  const newBlocks = Array.from(reparsedRootEl.children);
  const count = Math.max(liveBlocks.length, newBlocks.length);
  for (let i = 0; i < count; i++) {
    const a = liveBlocks[i];
    const b = newBlocks[i];
    if (!a || !b) {
      failures.push(`block ${i} ${a ? 'lost by save' : 'added by save'}`);
      continue;
    }
    for (const msg of compareBlock(describeBlock(a), describeBlock(b))) {
      failures.push(`${msg} at block ${i}`);
    }
  }
  return { ok: failures.length === 0, failures };
}

// Block-level tags whose presence inside a list item this check must not
// miss, independent of editor.js's own BLOCK_TAGS (used by liToMarkdown,
// above, to decide what to emit — the very allowlist issue_01M33WT8QY99XF481S0EAZJBP4
// is filed against). Deliberately its own, wider set: a checker that reused
// BLOCK_TAGS would be blind to exactly the TABLE/HR case that bug drops,
// since that tag is the one BLOCK_TAGS is missing.
const BLOCK_LIKE_TAGS = new Set([
  'P', 'PRE', 'BLOCKQUOTE', 'UL', 'OL', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'TABLE', 'HR',
]);

// A structural description of a top-level or nested block element, used only
// for comparison — never as serialized output.
function describeBlock(el) {
  const tag = el.tagName;
  if (tag === 'PRE') {
    // blockToMarkdown's pre case reads the fence language from this same
    // class attribute (language-${lang}) — compare it too, not just the
    // code text, so a fence whose language changed doesn't read as a match.
    const codeEl = Array.from(el.children).find(c => c.tagName === 'CODE');
    const langClass = codeEl ? codeEl.getAttribute('class') || '' : '';
    const lang = langClass.startsWith('language-') ? langClass.slice(9) : '';
    return { tag, text: codeEl ? codeEl.textContent : '', lang };
  }
  if (tag === 'UL' || tag === 'OL') {
    const items = Array.from(el.children).filter(c => c.tagName === 'LI').map(describeListItem);
    return { tag, items, start: el.getAttribute('start') };
  }
  if (tag === 'BLOCKQUOTE') {
    return { tag, blocks: Array.from(el.children).map(describeBlock) };
  }
  if (tag === 'HR') {
    // Locked (LOCKED_NODE_TYPES) at the top level, so its bytes never
    // regenerate there — but an HR reaching here can also be a list item's
    // block child (describeListItem below), where liToMarkdown's drop bug
    // lives. textContent is enough to notice it changed (always '' for an
    // <hr> unless the tag disappeared); the interesting failure is the tag
    // disappearing entirely, handled by compareListItem's block-count
    // comparison rather than this text.
    return { tag, text: el.textContent };
  }
  if (tag === 'TABLE') {
    // Not locked at the top level (see TABLE EDITING in the module header) —
    // an edited table regenerates through tableToMarkdown, so this compares
    // structure and cell content rather than treating the whole table as an
    // opaque blob. A list-nested table (describeListItem below) stays fully
    // locked and never actually changes, but gets the same precise
    // comparison for free.
    const theadEl = findChildTag(el, 'THEAD');
    const tbodyEl = findChildTag(el, 'TBODY');
    const headerRow = theadEl ? findChildTag(theadEl, 'TR') : null;
    const headerCells = headerRow ? Array.from(headerRow.children) : [];
    const bodyRows = tbodyEl ? Array.from(tbodyEl.children) : [];
    return {
      tag,
      header: headerCells.map(describeTableCell),
      rows: bodyRows.map(row => Array.from(row.children).map(describeTableCell)),
    };
  }
  if (tag === 'P' || /^H[1-6]$/.test(tag)) {
    return { tag, tokens: inlineTokens(el.childNodes) };
  }
  // Locked/unrecognised block (frontmatter section, footnote/html-comment
  // chip) — compare its rendered text verbatim.
  return { tag, text: el.textContent };
}

// A table cell's alignment (the style attribute cellStyle, render.js, sets
// for center/right; absent for left) plus its inline content, flattened the
// same way a paragraph's is.
function describeTableCell(cell) {
  return { align: cell.getAttribute('style') || '', tokens: inlineTokens(cell.childNodes, true) };
}

// A list item is "simple" when every element child is inline (renderListItem
// used item.content) — compared as a flattened inline token stream, the same
// as a paragraph's. Otherwise it is "multi-block" (renderListItem used
// item.blocks) — compared as an ordered list of describeBlock() results.
function describeListItem(li) {
  const blockChildren = Array.from(li.children).filter(c => BLOCK_LIKE_TAGS.has(c.tagName));
  if (blockChildren.length === 0) {
    return { simple: true, tokens: inlineTokens(li.childNodes) };
  }
  return { simple: false, blocks: blockChildren.map(describeBlock) };
}

// Flattens a run of inline DOM nodes into an ordered token list, insensitive
// to how the browser (or a prior edit) split plain text across sibling text
// nodes — adjacent text merges into one token — while keeping CODE, BR and
// IMG as their own tokens rather than folding them into surrounding text.
// This is what lets an ordinary text edit (which can leave text split at an
// arbitrary boundary with no content change) compare equal, while a genuine
// code-span content change — the first known bug — still shows up as a
// distinct token whose text differs.
//
// `inTableCell` mirrors inlineToMarkdown's own flag: a table cell's save path
// turns a <br> and an embedded "\n" into a single space (tableRowToMarkdown,
// above), so comparing the live DOM's BR/raw-newline against the reparsed
// plain-text result with this flag off would report a mismatch for a
// transform save() made on purpose, not one it missed.
function inlineTokens(childNodesLike, inTableCell = false) {
  const tokens = [];
  for (const node of Array.from(childNodesLike)) collectInlineTokens(node, tokens, inTableCell);
  return tokens;
}

function collectInlineTokens(node, tokens, inTableCell = false) {
  if (node.nodeType === 3) {
    const text = node.data || '';
    pushTextToken(tokens, inTableCell ? text.replace(/\n/g, ' ') : text);
    return;
  }
  if (node.nodeType !== 1) return; // e.g. a Comment a contenteditable paste can produce
  const tag = node.tagName;
  if (tag === 'CODE') { tokens.push({ kind: 'code', text: node.textContent }); return; }
  if (tag === 'BR') {
    if (inTableCell) { pushTextToken(tokens, ' '); return; }
    tokens.push({ kind: 'br' });
    return;
  }
  if (tag === 'IMG') {
    tokens.push({
      kind: 'img',
      alt: node.getAttribute('alt') || '',
      src: node.getAttribute('src') || '',
      title: node.getAttribute('title'),
    });
    return;
  }
  const cls = tag === 'SPAN' ? (node.getAttribute('class') || '') : '';
  if (tag === 'SPAN' && cls.includes('md-chip')) {
    // inlineToMarkdown's chip case emits data-src verbatim and never reads
    // this element's children at all — match that: no recursion here, so a
    // text-only change (which inlineToMarkdown would never see either)
    // doesn't manufacture a mismatch of its own.
    tokens.push({ kind: 'chip', src: node.getAttribute('data-src') });
    return;
  }
  if (tag === 'SPAN' && cls.includes('md-unsafe-link')) {
    // inlineToMarkdown's md-unsafe-link case reads data-dest/data-dest-title
    // for the destination, but still serializes children for the link text.
    tokens.push({
      kind: 'unsafe-link',
      dest: node.getAttribute('data-dest'),
      title: node.getAttribute('data-dest-title'),
    });
    for (const child of Array.from(node.childNodes)) collectInlineTokens(child, tokens, inTableCell);
    return;
  }
  if (tag === 'A') {
    // inlineToMarkdown's a case reads href (and title, for a non-wikilink)
    // to serialize the destination; a resolved wikilink's href is what
    // opts.resolveWikilink resolved the [[name]] to. Either way, comparing
    // href/title here is what catches the destination silently changing
    // while the link text stays the same.
    tokens.push({ kind: 'link', href: node.getAttribute('href'), title: node.getAttribute('title') });
    for (const child of Array.from(node.childNodes)) collectInlineTokens(child, tokens, inTableCell);
    return;
  }
  // Transparent wrapper (STRONG/EM/B/I) — its own text and markup boundaries
  // aren't this check's business; only whether the characters, code spans,
  // links, images and chips inside survive.
  for (const child of Array.from(node.childNodes)) collectInlineTokens(child, tokens, inTableCell);
}

function pushTextToken(tokens, text) {
  if (text === '') return;
  const last = tokens[tokens.length - 1];
  if (last && last.kind === 'text') last.text += text;
  else tokens.push({ kind: 'text', text });
}

// Compares two token streams and names the first divergence, or returns null
// when they match. A missing/extra/retyped 'code' token is reported as a lost
// code span specifically — the shape the first known bug takes — rather than
// as generic changed text.
function compareTokens(a, b) {
  const count = Math.max(a.length, b.length);
  for (let i = 0; i < count; i++) {
    const x = a[i], y = b[i];
    if (!x || !y) return ((x || y).kind === 'code') ? 'code span lost' : 'text changed';
    if (x.kind !== y.kind) return (x.kind === 'code' || y.kind === 'code') ? 'code span lost' : 'text changed';
    if (x.kind === 'text' && x.text !== y.text) return 'text changed';
    if (x.kind === 'code' && x.text !== y.text) return 'code span lost';
    if (x.kind === 'img') {
      if (x.alt !== y.alt || x.src !== y.src) return 'image changed';
      if (x.title !== y.title) return 'image title changed';
    }
    if (x.kind === 'link') {
      if (x.href !== y.href) return 'link href changed';
      if (x.title !== y.title) return 'link title changed';
    }
    if (x.kind === 'chip' && x.src !== y.src) return 'chip source changed';
    if (x.kind === 'unsafe-link') {
      if (x.dest !== y.dest) return 'link href changed';
      if (x.title !== y.title) return 'link title changed';
    }
  }
  return null;
}

// Compares two describeBlock() results of the same top-level or nested
// position. Returns a list of failure messages (empty when they match).
function compareBlock(a, b) {
  if (a.tag !== b.tag) return [describeTagMismatch(a.tag, b.tag)];
  switch (a.tag) {
    case 'PRE': {
      const out = [];
      if (a.text !== b.text) out.push('code block content changed');
      if (a.lang !== b.lang) out.push('code block language changed');
      return out;
    }
    case 'UL':
    case 'OL': {
      const out = [];
      if (a.start !== b.start) out.push('list start number changed');
      const count = Math.max(a.items.length, b.items.length);
      for (let i = 0; i < count; i++) {
        const ai = a.items[i], bi = b.items[i];
        if (!ai || !bi) { out.push(`list item ${i} ${ai ? 'lost' : 'added'}`); continue; }
        for (const msg of compareListItem(ai, bi)) out.push(`${msg} in list item ${i}`);
      }
      return out;
    }
    case 'BLOCKQUOTE': {
      const out = [];
      const count = Math.max(a.blocks.length, b.blocks.length);
      for (let i = 0; i < count; i++) {
        const ai = a.blocks[i], bi = b.blocks[i];
        if (!ai || !bi) { out.push(`blockquote content ${ai ? 'lost' : 'added'}`); continue; }
        for (const msg of compareBlock(ai, bi)) out.push(`${msg} in blockquote`);
      }
      return out;
    }
    case 'TABLE': {
      const out = [];
      const headerCount = Math.max(a.header.length, b.header.length);
      for (let i = 0; i < headerCount; i++) {
        const msg = compareTableCell(a.header[i], b.header[i]);
        if (msg) out.push(`header ${msg}`);
      }
      const rowCount = Math.max(a.rows.length, b.rows.length);
      for (let i = 0; i < rowCount; i++) {
        const ar = a.rows[i], br = b.rows[i];
        if (!ar || !br) { out.push(`table row ${i} ${ar ? 'lost' : 'added'}`); continue; }
        const cellCount = Math.max(ar.length, br.length);
        for (let j = 0; j < cellCount; j++) {
          const msg = compareTableCell(ar[j], br[j]);
          if (msg) out.push(`${msg} at row ${i} col ${j}`);
        }
      }
      return out;
    }
    case 'HR':
      return [];
    default: {
      // P, H1-6, and the verbatim-text fallback all carry either .tokens or .text.
      if (a.tokens) {
        const msg = compareTokens(a.tokens, b.tokens);
        return msg ? [msg] : [];
      }
      return a.text === b.text ? [] : ['content changed'];
    }
  }
}

// Compares two describeTableCell() results. Returns a failure message, or
// null when they match.
function compareTableCell(a, b) {
  if (!a || !b) return a ? 'cell lost' : 'cell added';
  if (a.align !== b.align) return 'alignment changed';
  return compareTokens(a.tokens, b.tokens);
}

function describeTagMismatch(aTag, bTag) {
  if (aTag === 'TABLE' || bTag === 'TABLE') return 'table lost';
  if (aTag === 'HR' || bTag === 'HR') return 'thematic break lost';
  return `block type changed (<${aTag.toLowerCase()}> became <${bTag.toLowerCase()}>)`;
}

// Compares two describeListItem() results. A list item can change shape
// (simple inline <-> multi-block) across a save/reparse, which is exactly
// what liToMarkdown's drop bug does — a <p>+<table> item collapses to a
// simple item once the table is silently dropped — so this handles that
// mixed case explicitly rather than only comparing like with like.
function compareListItem(a, b) {
  if (a.simple && b.simple) {
    const msg = compareTokens(a.tokens, b.tokens);
    return msg ? [msg] : [];
  }
  if (!a.simple && !b.simple) {
    const out = [];
    const count = Math.max(a.blocks.length, b.blocks.length);
    for (let i = 0; i < count; i++) {
      const ai = a.blocks[i], bi = b.blocks[i];
      if (!ai || !bi) { out.push(droppedBlockMessage(ai || bi)); continue; }
      out.push(...compareBlock(ai, bi));
    }
    return out;
  }
  // One side is a simple inline item, the other carries block children — the
  // shape liToMarkdown's drop bug produces. Compare the multi-block side's
  // first block (when it is inline-shaped, i.e. a paragraph or heading)
  // against the simple side's tokens, and name every other block on the
  // multi-block side as lost, rather than reporting only "shape changed" and
  // leaving the reader to guess what content that covered.
  const multi = a.simple ? b : a;
  const simple = a.simple ? a : b;
  if (multi.blocks.length === 0) return ['list item structure changed'];
  const [first, ...rest] = multi.blocks;
  const out = [];
  if (first.tag === 'P' || /^H[1-6]$/.test(first.tag)) {
    const msg = compareTokens(first.tokens, simple.tokens);
    if (msg) out.push(msg);
  } else {
    out.push('list item structure changed');
  }
  for (const block of rest) out.push(droppedBlockMessage(block));
  return out;
}

function droppedBlockMessage(block) {
  if (block.tag === 'TABLE') return 'table lost';
  if (block.tag === 'HR') return 'thematic break lost';
  return 'block content lost';
}

// ─── formatting commands ──────────────────────────────────────────────────────
//
// Every command below is a plain function taking (ownerDoc, rootEl, inverseMap,
// ...) rather than a closure over mountEditor's locals, addressed explicitly to
// one mount's DOM rather than any shared or global state (mountEditor's
// returned methods are the only place these run). They read the current
// selection from ownerDoc.getSelection() and mutate rootEl directly with the
// same primitives a native edit would use (appendChild, insertBefore,
// removeChild) — never via innerHTML, and never via Range mutation methods
// (surroundContents, extractContents), so the one algorithm here is what
// actually runs in both a real browser and this file's tests.

function currentRange(ownerDoc) {
  const sel = ownerDoc.getSelection && ownerDoc.getSelection();
  if (!sel || sel.rangeCount === 0) return null;
  return sel.getRangeAt(0);
}

// True when node (element or text) is rootEl itself or a descendant of it.
function isWithin(node, rootEl) {
  let el = node && (node.nodeType === 1 ? node : node.parentElement);
  while (el) {
    if (el === rootEl) return true;
    el = el.parentElement;
  }
  return false;
}

// Nearest ancestor (starting at node itself if it is an element) with the
// given tagName, stopping at and excluding boundary. Returns null past it.
// tagName is matched case-insensitively — real DOM tagName is always
// uppercase, and callers here pass the lowercase form createElement() takes.
function closestMatching(node, tagName, boundary) {
  const upper = tagName.toUpperCase();
  let el = node && (node.nodeType === 1 ? node : node.parentElement);
  while (el && el !== boundary) {
    if (el.tagName === upper) return el;
    el = el.parentElement;
  }
  return null;
}

// The direct child of rootEl that contains the current selection's start
// point, or null when there is no selection or it sits outside rootEl.
function currentBlock(rootEl, ownerDoc) {
  const range = currentRange(ownerDoc);
  if (!range) return null;
  let el = range.startContainer.nodeType === 1
    ? range.startContainer
    : range.startContainer.parentElement;
  while (el && el.parentElement !== rootEl) el = el.parentElement;
  return (el && el.parentElement === rootEl) ? el : null;
}

function isLockedBlock(block, inverseMap) {
  const node = inverseMap.get(block);
  return (node != null && LOCKED_NODE_TYPES.has(node.type)) || isLockedEl(block);
}

// Insert newNode as the sibling immediately after refNode.
function insertAfter(parent, newNode, refNode) {
  const siblings = Array.from(parent.childNodes);
  const idx = siblings.indexOf(refNode);
  const next = idx === -1 ? null : (siblings[idx + 1] ?? null);
  parent.insertBefore(newNode, next);
}

// Replace el with its own children, in place, in its parent.
function unwrapElement(el) {
  const parent = el.parentElement;
  if (!parent) return;
  for (const child of Array.from(el.childNodes)) {
    parent.insertBefore(child, el);
  }
  parent.removeChild(el);
}

// Move block's direct children into a freshly created newTag element, and
// replace block with it in rootEl. Only valid between tags with the same
// content model (inline children): heading <-> paragraph, and <ul> <-> <ol>
// (whose direct children are <li> elements either way).
function retagBlock(ownerDoc, rootEl, block, newTag) {
  const replacement = ownerDoc.createElement(newTag);
  for (const child of Array.from(block.childNodes)) {
    replacement.appendChild(child);
  }
  rootEl.replaceChild(replacement, block);
  return replacement;
}

// ─── inline commands: toggleBold, toggleItalic, insertLink ───────────────────
//
// Scope: a selection contained in a single text node — the common case of
// selecting a run of text within one block. A selection spanning more than
// one node is a no-op (false) rather than a guessed splice across siblings.

function toggleInlineWrap(ownerDoc, rootEl, inverseMap, tagName, configure) {
  const range = currentRange(ownerDoc);
  if (!range || range.collapsed) return false;
  const { startContainer: sc, startOffset: so, endContainer: ec, endOffset: eo } = range;
  if (sc !== ec || sc.nodeType !== 3 || !isWithin(sc, rootEl)) return false;

  // PRE (fenced code) is deliberately not in LOCKED_NODE_TYPES/isLockedEl —
  // toggleCodeBlockImpl needs it unlocked to convert it back to a paragraph —
  // so isLockedBlock alone does not refuse a selection inside one. Refuse it
  // explicitly here: blockToMarkdown's pre case emits codeEl.textContent only,
  // so a <strong>/<em>/<a> spliced into the code text would report changed
  // and then vanish on save with no error.
  const block = currentBlock(rootEl, ownerDoc);
  if (block == null || isLockedBlock(block, inverseMap) || block.tagName === 'PRE') return false;

  const parent = sc.parentElement;
  if (!parent) return false;

  // Toggle off: the selection already sits inside a matching element.
  const existing = closestMatching(parent, tagName, rootEl);
  if (existing) {
    unwrapElement(existing);
    return true;
  }

  const text = sc.data;
  const before = text.slice(0, so);
  const selected = text.slice(so, eo);
  const after = text.slice(eo);

  const wrapper = ownerDoc.createElement(tagName);
  wrapper.appendChild(ownerDoc.createTextNode(selected));
  if (configure) configure(wrapper);

  sc.data = before;
  insertAfter(parent, wrapper, sc);
  if (after) insertAfter(parent, ownerDoc.createTextNode(after), wrapper);
  if (!before) parent.removeChild(sc);

  return true;
}

// Nearest ancestor span carrying the renderer's refused-destination marker
// (render.js's isSafeUrl rejected it — see the md-unsafe-link case in
// inlineToMarkdown), stopping at and excluding boundary.
function closestUnsafeLink(node, boundary) {
  let el = node && (node.nodeType === 1 ? node : node.parentElement);
  while (el && el !== boundary) {
    const cls = (el.getAttribute && el.getAttribute('class')) || '';
    if (cls.includes('md-unsafe-link')) return el;
    el = el.parentElement;
  }
  return null;
}

// linkToMarkdown (below) writes href and title into `[text](href "title")`
// by concatenation, so either one supplying a character that ends that
// syntax early — unescaped, since this file has no <...>-destination or
// backslash-escaping emitter — turns part of the value into live Markdown
// structure on the next save/reparse. isSafeUrl only checks href's scheme
// prefix, not the rest of the string, and title had no check at all. Refuse
// rather than escape: the same reasoning as VALID_FENCE_LANG above — an
// escaping scheme here is exactly the enumerate-and-miss shape this file
// keeps rediscovering. A destination needing whitespace, a newline or a
// paren, or a title needing a newline or a double quote, isn't
// representable in this syntax.
const VALID_LINK_HREF = /^[^\s()]*$/;
const VALID_LINK_TITLE = /^[^\r\n"]*$/;

function insertLinkImpl(ownerDoc, rootEl, inverseMap, href, title) {
  if (typeof href !== 'string' || href === '' || !isSafeUrl(href, false)) return false;
  if (!VALID_LINK_HREF.test(href)) return false;
  // null/undefined mean "no title"; anything else non-string is refused
  // outright rather than coerced, matching toggleCodeBlockImpl's lang check.
  if (title != null && typeof title !== 'string') return false;
  if (title && !VALID_LINK_TITLE.test(title)) return false;

  const range = currentRange(ownerDoc);
  if (!range || range.collapsed) return false;
  const { startContainer: sc, endContainer: ec } = range;
  if (sc !== ec || sc.nodeType !== 3 || !isWithin(sc, rootEl)) return false;

  // See toggleInlineWrap's matching PRE check: fenced code is deliberately
  // unlocked (toggleCodeBlockImpl needs it that way), so isLockedBlock alone
  // does not refuse a selection inside one — an <a> spliced into code text
  // would report changed and vanish silently on save.
  const block = currentBlock(rootEl, ownerDoc);
  if (block == null || isLockedBlock(block, inverseMap) || block.tagName === 'PRE') return false;

  // The selection already sits inside a link (at any depth, e.g. bolded link
  // text): edit that link in place rather than nesting a second one inside
  // its text, which Markdown cannot represent.
  const existingLink = closestMatching(sc, 'a', rootEl);
  if (existingLink) {
    const prevHref = existingLink.getAttribute('href');
    const prevTitle = existingLink.getAttribute('title');
    const nextTitle = title || null;
    // Confirm state changed before reporting one — this feeds revision(),
    // and a confirm on an already-current href/title is not an edit.
    if (prevHref === href && prevTitle === nextTitle) return false;
    existingLink.setAttribute('href', href);
    if (title) existingLink.setAttribute('title', title);
    else existingLink.removeAttribute('title');
    return true;
  }
  // Same reasoning for a refused-destination span: wrapping part of its text
  // in a new <a> would serialize as one link nested in another's text.
  if (closestUnsafeLink(sc, rootEl)) return false;

  return toggleInlineWrap(ownerDoc, rootEl, inverseMap, 'a', el => {
    el.setAttribute('href', href);
    if (title) el.setAttribute('title', title);
  });
}

// ─── block commands: toggleHeading, toggleBulletList/OrderedList, toggleCodeBlock

function toggleHeadingImpl(ownerDoc, rootEl, inverseMap, level) {
  if (!Number.isInteger(level) || level < 1 || level > 6) return false;
  const block = currentBlock(rootEl, ownerDoc);
  if (block == null || isLockedBlock(block, inverseMap)) return false;

  const targetTag = `H${level}`;
  if (block.tagName === targetTag) {
    retagBlock(ownerDoc, rootEl, block, 'p');
    return true;
  }
  if (block.tagName === 'P' || /^H[1-6]$/.test(block.tagName)) {
    retagBlock(ownerDoc, rootEl, block, targetTag.toLowerCase());
    return true;
  }
  return false;
}

// Move a lone paragraph's inline content into a new single-item list.
function pToList(ownerDoc, rootEl, block, listTag) {
  const li = ownerDoc.createElement('li');
  for (const child of Array.from(block.childNodes)) li.appendChild(child);
  const list = ownerDoc.createElement(listTag);
  list.appendChild(li);
  rootEl.replaceChild(list, block);
}

// Move a single-item list's one <li> content into a new paragraph. Returns
// false, mutating nothing, if the content can't be serialized — checked by
// running the real conversion on the item's own childNodes (read-only; it
// moves nothing) before anything is moved or replaceChild runs. This is the
// independent backstop for hasOnlyInlineChildren's gate above: if that gate
// is ever wrong, or removed, this is what stops a refused command from
// leaving rootEl already mutated with no way back to the source list — the
// failure mode the table bug this closes actually hit, since the old code
// called replaceChild first and discovered the throw only at save() time,
// by which point firstItem's children had already been moved into the
// spliced-in <p> with no path back to the original list.
function listToP(ownerDoc, rootEl, block, firstItem) {
  const children = Array.from(firstItem.childNodes);
  try {
    inlinesToMarkdown(children);
  } catch (_e) {
    return false;
  }
  const p = ownerDoc.createElement('p');
  for (const child of children) p.appendChild(child);
  rootEl.replaceChild(p, block);
  return true;
}

// True when every element child of li is a tag inlineToMarkdown/inlinesToMarkdown
// can serialize on its own (INLINE_TAGS, defined with inlineToMarkdown below).
// li.children is elements-only, so a text-node child never fails this.
function hasOnlyInlineChildren(li) {
  return Array.from(li.children).every(c => INLINE_TAGS.has(c.tagName.toLowerCase()));
}

function toggleListImpl(ownerDoc, rootEl, inverseMap, wantTag) {
  const block = currentBlock(rootEl, ownerDoc);
  if (block == null || isLockedBlock(block, inverseMap)) return false;

  const otherTag = wantTag === 'UL' ? 'OL' : 'UL';

  if (block.tagName === wantTag) {
    // Unwrap only a single-item list whose item is purely inline content —
    // this is the shape toggling-on produces, and it is the one case with an
    // unambiguous single-paragraph inverse. A parsed item with a continuation
    // line (a nested list, a second paragraph) puts its content in
    // item.blocks rather than item.content, and renderListItem (render.js)
    // renders that as element children of the <li> — a <p>, possibly
    // alongside a nested <ul>/<ol>. Splicing those block elements into a new
    // <p> would nest block content inside it, which is not valid Markdown and
    // is not this file's business to invent a flattening for (see the
    // module comment: commands refuse rather than guess at a splice).
    const items = Array.from(block.children).filter(c => c.tagName === 'LI');
    if (items.length !== 1) return false;
    const li = items[0];
    // Unwrap only when every element child is on INLINE_TAGS — known-safe to
    // carry into a <p>, because inlineToMarkdown (below) is the thing that
    // will actually have to serialize it. This is an allowlist of what's
    // SAFE rather than a denylist of what's BLOCK, deliberately: this gate
    // used to check against a denylist of "known block tags" (BLOCK_TAGS,
    // still used below in liToMarkdown for an unrelated purpose), which
    // refuses only a tag someone remembered to add to it — so a tag nobody
    // anticipated, <table> confirmed and <hr> also covered here though
    // unconfirmed, read as "not block" and got spliced into the new <p>
    // uncaught, reporting changed: true right before save() threw on it.
    // An allowlist refuses that same unanticipated tag instead, harmlessly,
    // because it was never granted rather than never denied.
    if (!hasOnlyInlineChildren(li)) return false;
    return listToP(ownerDoc, rootEl, block, li);
  }
  if (block.tagName === otherTag) {
    retagBlock(ownerDoc, rootEl, block, wantTag.toLowerCase());
    return true;
  }
  if (block.tagName === 'P') {
    pToList(ownerDoc, rootEl, block, wantTag.toLowerCase());
    return true;
  }
  return false;
}

// A fence info string is a single-line token. CommonMark forbids a backtick
// inside a backtick-fence's info string outright (it would be ambiguous with
// the fence itself), and any whitespace — a newline above all — would let
// `lang` terminate the fence's own line once written into
// class="language-${lang}" and read back out by blockToMarkdown's pre case,
// injecting the remainder of the string as sibling Markdown structure after
// the fence. Reject rather than strip: silently mangling the token is a
// worse surprise for a caller than a no-op.
const VALID_FENCE_LANG = /^[^\s`]*$/;

function toggleCodeBlockImpl(ownerDoc, rootEl, inverseMap, lang) {
  const block = currentBlock(rootEl, ownerDoc);
  if (block == null || isLockedBlock(block, inverseMap)) return false;

  if (block.tagName === 'PRE') {
    const codeEl = Array.from(block.children).find(c => c.tagName === 'CODE');
    // Strip the trailing newline blockToMarkdown's fenced-code case always
    // carries, so it doesn't become an embedded blank line inside the <p>.
    const text = (codeEl ? codeEl.textContent : '').replace(/\n+$/, '');

    // A fenced code block's content is inert inside its fence — arbitrary
    // bytes, including a line that reads as frontmatter, a heading, a list,
    // or a thematic break. Releasing it into a <p>'s unescaped text path
    // (blockToMarkdown's p case, inlineToMarkdown's text-node case — neither
    // escapes Markdown structure) can turn those bytes into live structure
    // the moment this new paragraph is regenerated on save. Reparse the
    // paragraph's own candidate Markdown and refuse the conversion unless it
    // comes back as the single inert paragraph it is meant to be. An empty
    // or whitespace-only body reparses to no blocks at all — that's the
    // harmless degenerate case, not a mismatch.
    const candidateMd = text + '\n\n';
    const reparsed = parse(candidateMd);
    const staysInert = reparsed.children.length === 0
      || (reparsed.children.length === 1 && reparsed.children[0].type === 'paragraph');
    if (!staysInert) return false;

    const p = ownerDoc.createElement('p');
    p.appendChild(ownerDoc.createTextNode(text));
    rootEl.replaceChild(p, block);
    return true;
  }
  if (block.tagName === 'P') {
    // Type-check before anything touches lang, the way insertLinkImpl checks
    // href: RegExp.test() and the template literal below each coerce lang to
    // a string independently via its own toString() call, so an object whose
    // toString() returns a different value each call could pass the regex
    // against one string and embed a different, unvalidated one. Checking the
    // type once and using that single value for both closes it. null/undefined
    // mean "no lang"; anything else non-string is refused outright rather than
    // coerced into a token.
    if (lang != null && typeof lang !== 'string') return false;
    if (lang && !VALID_FENCE_LANG.test(lang)) return false;
    // blockToMarkdown's fenced-code case emits content + fence with no
    // newline between them — content must supply its own trailing newline.
    const text = block.textContent;
    const content = text.endsWith('\n') ? text : text + '\n';
    const codeEl = ownerDoc.createElement('code');
    if (lang) codeEl.setAttribute('class', `language-${lang}`);
    codeEl.appendChild(ownerDoc.createTextNode(content));
    const pre = ownerDoc.createElement('pre');
    pre.appendChild(codeEl);
    rootEl.replaceChild(pre, block);
    return true;
  }
  return false;
}

// ─── locked-element marking ───────────────────────────────────────────────────

// True for every element a formatting command (toggleBold, toggleHeading,
// etc. — see isLockedBlock, above) must refuse, table included: a table's
// own structure stays off-limits to those commands even though its cells
// carry their own contenteditable island (see markLocked below) for direct
// typing.
function isLockedEl(el) {
  if (!el || el.nodeType !== 1) return false;
  const cls = el.getAttribute('class') || '';
  if (cls.includes('md-chip') || cls.includes('md-frontmatter')) return true;
  const tag = el.tagName.toLowerCase();
  if (tag === 'table' || tag === 'hr') return true;
  return false;
}

// Walk the DOM tree and set contenteditable="false" on every locked element.
// Stops recursing into a locked subtree — its children inherit the lock.
// A table in editableTableEls is the one exception: its own structure still
// locks, but markTableCellsEditable carves each cell out as its own
// editable island.
function markLocked(el, editableTableEls) {
  if (!el || el.nodeType !== 1) return;
  if (el.tagName === 'TABLE' && editableTableEls.has(el)) {
    el.setAttribute('contenteditable', 'false');
    markTableCellsEditable(el, editableTableEls);
    return;
  }
  if (isLockedEl(el)) {
    el.setAttribute('contenteditable', 'false');
    return;
  }
  // .children is elements-only; no nodeType guard needed inside.
  for (const child of el.children) {
    markLocked(child, editableTableEls);
  }
}

// Set contenteditable="true" on every header/data cell of an editable table —
// the nested-contenteditable pattern that lets a cell's text be typed into
// directly while the table's own row/column structure, locked by the caller,
// stays off-limits. A chip nested inside a cell (footnote_ref, html_comment)
// stays locked the same way it would inside a paragraph, via the ordinary
// markLocked recursion into each cell's own children.
function markTableCellsEditable(tableEl, editableTableEls) {
  for (const section of tableEl.children) { // thead, tbody
    for (const row of section.children) { // tr
      for (const cell of row.children) { // th, td
        cell.setAttribute('contenteditable', 'true');
        for (const child of cell.children) {
          markLocked(child, editableTableEls);
        }
      }
    }
  }
}

// ─── DOM-to-Markdown: blocks ─────────────────────────────────────────────────

/**
 * Convert a block DOM element to Markdown.
 * The result includes the trailing blank line separator (e.g. "\n\n") that
 * occupies node.trailing in the md-serial model.
 * Throws EditorError for locked elements or unrecognised shapes.
 */
function blockToMarkdown(el, inverseMap, lockedListBlockSource) {
  const tag = el.tagName.toLowerCase();
  const cls = el.getAttribute('class') || '';

  // Locked elements must never reach this converter.
  if (cls.includes('md-chip') || cls.includes('md-frontmatter')) {
    throw new EditorError(`blockToMarkdown called on locked element <${tag} class="${cls}">`);
  }
  if (tag === 'table') {
    // A <table> here is always one created by toDOM from a parsed 'table'
    // node (no command in this file inserts one fresh), so inverseMap always
    // has it — the same refuse-loudly guard as verbatimListBlock's below.
    const tableNode = inverseMap.get(el);
    if (tableNode == null) {
      throw new EditorError('blockToMarkdown: <table> element has no recorded parse node');
    }
    return tableToMarkdown(el, tableNode);
  }

  // Headings h1–h6.
  if (/^h[1-6]$/.test(tag)) {
    const level = parseInt(tag[1], 10);
    // childNodes includes text nodes; inlinesToMarkdown walks mixed content.
    return '#'.repeat(level) + ' ' + inlinesToMarkdown(Array.from(el.childNodes)) + '\n\n';
  }

  if (tag === 'p') {
    return inlinesToMarkdown(Array.from(el.childNodes)) + '\n\n';
  }

  if (tag === 'pre') {
    // .children is elements-only; find the <code> by tagName.
    const codeEl = Array.from(el.children).find(c => c.tagName === 'CODE');
    const langClass = codeEl ? codeEl.getAttribute('class') || '' : '';
    const lang = langClass.startsWith('language-') ? langClass.slice(9) : '';
    // Use native textContent — correct by definition for plain text extraction.
    const content = codeEl ? codeEl.textContent : '';
    // Fence must be longer than any backtick run inside the content to avoid
    // premature fence closure (e.g. a 3-backtick line closing a 3-backtick fence).
    const fenceLen = Math.max(3, longestBacktickRun(content) + 1);
    const fence = '`'.repeat(fenceLen);
    return fence + lang + '\n' + content + fence + '\n\n';
  }

  if (tag === 'blockquote') {
    // .children is elements-only — all block children, no filter needed.
    const blockChildren = Array.from(el.children);
    const inner = blockChildren.map(c => blockToMarkdown(c, inverseMap, lockedListBlockSource)).join('');
    // Strip trailing blank lines, then prefix each line with '> '.
    const content = inner.replace(/\n+$/, '\n');
    return prefixLines(content, '> ') + '\n';
  }

  if (tag === 'ul') {
    const items = Array.from(el.children).filter(c => c.tagName === 'LI');
    return items.map(li => liToMarkdown(li, '- ', inverseMap, lockedListBlockSource)).join('') + '\n';
  }

  if (tag === 'ol') {
    const items = Array.from(el.children).filter(c => c.tagName === 'LI');
    const startAttr = el.getAttribute('start');
    let n = startAttr ? parseInt(startAttr, 10) : 1;
    return items.map(li => liToMarkdown(li, `${n++}. `, inverseMap, lockedListBlockSource)).join('') + '\n';
  }

  if (tag === 'hr') {
    return '---\n\n';
  }

  throw new EditorError(`blockToMarkdown: unrecognised block element <${tag}>`);
}

// ─── DOM-to-Markdown: table ───────────────────────────────────────────────────

// Cell content comes from the live DOM, the table as the member edited it.
// The separator line is the original parse node's own source line, since no
// command in this file changes a column or its alignment (see TABLE EDITING).
// Keeping it, and every row whose cells are unchanged, means a one-cell edit
// changes one line of the saved source. Regenerating them respaced `|---|` as
// `| --- |` and showed every table edit as a separator change in the diff a
// reviewer reads.
function tableToMarkdown(tableEl, tableNode) {
  const theadEl = findChildTag(tableEl, 'THEAD');
  const tbodyEl = findChildTag(tableEl, 'TBODY');
  const headerRow = theadEl ? findChildTag(theadEl, 'TR') : null;
  const bodyRows = tbodyEl ? Array.from(tbodyEl.children) : [];

  const lines = [
    tableRowToMarkdown(headerRow, tableNode.headerRaw),
    tableNode.separatorLine,
    ...bodyRows.map((row, i) => tableRowToMarkdown(row, tableNode.rows[i])),
  ];
  return lines.join('\n') + '\n\n';
}

// Each editable table row's cell texts as tableCellTexts produced them at
// mount, keyed by the row's <tr>. Filled by mountEditor; read by
// tableRowToMarkdown.
const initialTableRowText = new WeakMap();

// The first element child matching tagName, or null. table/thead/tbody have
// no text-node children, so a plain loop over .children is enough — no
// querySelector, which the DOM stub this file is tested against doesn't
// implement.
function findChildTag(el, tagName) {
  for (const child of el.children) {
    if (child.tagName === tagName) return child;
  }
  return null;
}

// A cell's own inline content serialises exactly like a paragraph's
// (inlinesToMarkdown) — the same bold/italic/code/link/chip markup a table
// cell can hold — but with the `inTableCell` flag set: parseTableRow
// (md-serial.js) splits a row on raw "|" before any inline parsing runs, and
// reads only one physical line per row, so a literal "|" or "\n" anywhere in
// that output (plain text, a code span's backticks, a link's href/title, a
// chip's recorded source) has to be neutralized here or it silently cuts the
// cell in two, or escapes the table entirely, on the next parse. A chip
// holding a literal "\n" is the one case this can't neutralize without
// either rewriting its bytes or breaking the row, so inlineToMarkdown's chip
// case refuses it outright instead — see there.
function tableCellTexts(cells) {
  return cells.map(cell => inlinesToMarkdown(Array.from(cell.childNodes), true));
}

// A row whose cells serialise exactly as they did at mount returns its
// original line, `rawLine`, so its spacing and escapes stay as written. The
// comparison is serialiser output against serialiser output, so a cell holding
// an escaped pipe, a code span or a chip matches itself; comparing with the
// parsed text would not, because parseTableRow unescapes `\|`.
function tableRowToMarkdown(rowEl, rawLine) {
  const texts = tableCellTexts(Array.from(rowEl.children));
  const initial = initialTableRowText.get(rowEl);
  if (texts.length === initial.length && texts.every((text, i) => text === initial[i])) {
    return rawLine;
  }
  return '| ' + texts.join(' | ') + ' |';
}

// Tags render.js uses for block-level content inside a multi-block list item
// (item.blocks; see liToMarkdown). Deliberately its own list rather than
// INLINE_TAGS's complement — see liToMarkdown for why inverting this one
// is not this revision's fix to make.
const BLOCK_TAGS = new Set([
  'p', 'pre', 'blockquote', 'ul', 'ol', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
]);

// A <table> or <hr> continuing a list item (item.blocks) — not in BLOCK_TAGS,
// and not safe to add there: blockToMarkdown refuses a <table> outright
// (fixed issue_01M33WT8QY99XF481S0EAZJBP4's prior attempt at converting
// BLOCK_TAGS to an inline allowlist turned the drop below into that throw on
// two real corpus documents), and would normalize an <hr>'s separator style
// instead of preserving it, the one thing LOCKED_NODE_TYPES exists to avoid
// everywhere else a locked block appears. Kept as its own set so BLOCK_TAGS
// keeps meaning exactly "liToMarkdown can hand this to blockToMarkdown" —
// these two tags never go through that path; see verbatimListBlock below.
const LOCKED_LIST_BLOCK_TAGS = new Set(['table', 'hr']);

function liToMarkdown(li, prefix, inverseMap, lockedListBlockSource) {
  const blockChildren = Array.from(li.children)
    .filter(c => {
      const tag = c.tagName.toLowerCase();
      return BLOCK_TAGS.has(tag) || LOCKED_LIST_BLOCK_TAGS.has(tag);
    });

  if (blockChildren.length === 0) {
    // Simple inline list item — childNodes includes text nodes.
    return prefix + inlinesToMarkdown(Array.from(li.childNodes)) + '\n';
  }

  // Multi-block list item: first block takes the prefix, subsequent blocks are
  // continuation-indented under it.
  const indent = ' '.repeat(prefix.length);
  let out = '';
  for (let i = 0; i < blockChildren.length; i++) {
    const child = blockChildren[i];
    const tag = child.tagName.toLowerCase();
    const locked = LOCKED_LIST_BLOCK_TAGS.has(tag);
    const md = (locked ? verbatimListBlock(child, lockedListBlockSource) : blockToMarkdown(child, inverseMap, lockedListBlockSource))
      .replace(/\n+$/, '\n');

    // This loop joins sibling blocks with no blank line between them, which
    // is fine for every other BLOCK_TAGS/LOCKED_LIST_BLOCK_TAGS combination —
    // but a dash-only thematic break directly following a paragraph, with no
    // blank line between them, is exactly a setext H2 underline, and
    // md-serial.js's own parser reads it that way (RE_SETEXT_H2, checked
    // before RE_THEMATIC). Put a blank line back in front of it so a reparse
    // of this join doesn't swallow the paragraph and the break into one
    // heading — blank lines are never wrong around a thematic break.
    if (locked && tag === 'hr' && i > 0 && blockChildren[i - 1].tagName === 'P') {
      const hrNode = inverseMap.get(child);
      if (/^-+[ \t]*$/.test(hrNode.chars)) out += '\n';
    }

    const lines = md.split('\n');
    for (let j = 0; j < lines.length; j++) {
      if (j === lines.length - 1 && lines[j] === '') continue;
      if (i === 0 && j === 0) {
        out += prefix + lines[j] + '\n';
      } else {
        out += (lines[j] ? indent + lines[j] : '') + '\n';
      }
    }
  }
  return out;
}

// Walk the parse tree once, at mount, collecting every <table>/<hr> nested
// inside a list item's own virtualContent — keyed by its DOM element in
// `out`, so verbatimListBlock below can look a block's bytes up by element
// alone, independent of which <li> it sits under by the time save() runs.
// Mirrors markNodeDirty's generic node shape (children/items/blocks) in
// md-serial.js to reach every depth: nested lists (list.items), nested list
// items (list_item.blocks), and whatever a blockquote wraps (children).
function collectLockedListBlockSource(node, map, out) {
  if (!node || typeof node !== 'object') return;
  if (node.type === 'list_item' && node.blocks) {
    for (const block of node.blocks) {
      if (block.type === 'table' || block.type === 'thematic_break') {
        const el = map.get(block);
        if (el) out.set(el, node.virtualContent.slice(block.start, block.end));
      }
    }
  }
  if (node.children) node.children.forEach(c => collectLockedListBlockSource(c, map, out));
  if (node.items) node.items.forEach(c => collectLockedListBlockSource(c, map, out));
  if (node.blocks) node.blocks.forEach(c => collectLockedListBlockSource(c, map, out));
}

// Verbatim source for a <table>/<hr> continuing a list item — issue
// issue_01M33WT8QY99XF481S0EAZJBP4's fix. Both tags are locked
// (contenteditable=false; see markLocked), so the browser can only move or
// delete the element, never edit its content — there's no edit to detect,
// only a possible move. A move is exactly the case this can't read live off
// the DOM for: markLocked's own rationale means a user can drag the element
// to a *different* list item with no accompanying text edit, so the live
// parent's virtualContent is not necessarily the one this block was parsed
// from. collectLockedListBlockSource (above) already resolved that at mount
// time, once, while each block's element was still under its real parent —
// this is a pure lookup by element, wherever it sits now.
function verbatimListBlock(blockEl, lockedListBlockSource) {
  const src = lockedListBlockSource.get(blockEl);
  // A locked list-item block with no recorded source would mean one was
  // created with no source to read — something this file has no path to do
  // (no command inserts a table or hr) — so this is the same refuse-loudly
  // guard as blockToMarkdown's own locked-element throws, not a case this
  // save path is expected to actually hit.
  if (src == null) {
    throw new EditorError(`liToMarkdown: <${blockEl.tagName.toLowerCase()}> block has no recorded verbatim source`);
  }
  return src;
}

// Prefix each line of `text` with `prefix`.  Empty lines get a bare '>'.
// The last element of text.split('\n') is '' when text ends with '\n' — skip it.
function prefixLines(text, prefix) {
  const lines = text.split('\n');
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    if (i === lines.length - 1 && lines[i] === '') continue;
    out.push(lines[i] ? prefix + lines[i] : '>');
  }
  return out.join('\n') + '\n';
}

// ─── DOM-to-Markdown: inlines ─────────────────────────────────────────────────

// Every tag inlineToMarkdown knows how to serialize on its own — the gate at
// the top of inlineToMarkdown throws for anything not in this set, so it is
// this function's actual dispatch surface rather than a description of it.
// hasOnlyInlineChildren (used by toggleListImpl's single-item-list unwrap)
// reads the same Set, so a tag inlineToMarkdown gains or loses is
// automatically safe or refused to unwrap — the two cannot drift the way
// toggleListImpl's own separate, hand-maintained list (the previous
// BLOCK_TAGS-based check) did.
const INLINE_TAGS = new Set(['a', 'b', 'br', 'code', 'em', 'i', 'img', 'span', 'strong']);

function inlinesToMarkdown(children, inTableCell = false) {
  if (!children || !children.length) return '';
  return children.map(n => inlineToMarkdown(n, inTableCell)).join('');
}

// Neutralizes the two characters a table row's own encoding can't carry
// literally: "\n" (parseTableRow, md-serial.js, reads one physical line per
// row) and "|" (it splits that line into cells on an unescaped one). Applied
// to every table-cell leaf below except a locked chip's data-src, which is
// exempt — see inlineToMarkdown's chip case.
function tableCellText(text) {
  return text.replace(/\n/g, ' ').replace(/\|/g, '\\|');
}

function inlineToMarkdown(node, inTableCell = false) {
  if (!node) return '';
  // Text nodes — .data holds the text content.
  if (node.nodeType === 3) {
    const text = node.data || '';
    return inTableCell ? tableCellText(text) : text;
  }
  // Raw strings emitted by render.js before toDOM converts them.
  if (typeof node === 'string') return inTableCell ? tableCellText(node) : node;
  // Any other non-element node — a Comment (nodeType 8) a native
  // contenteditable paste can produce, chief among them — has no Markdown
  // representation and no .tagName to read. Drop it, matching
  // elementSignature() treating its appearance as a content change that
  // forces this block to regenerate in the first place.
  if (node.nodeType !== 1) return '';

  const tag = node.tagName.toLowerCase();
  const cls = node.getAttribute('class') || '';

  // Inline locked chips — read verbatim source from data-src. Checked before
  // the INLINE_TAGS gate: a chip's tag is always 'span', already inline, so
  // this only narrows which span case runs below.
  //
  // Outside a table cell, data-src is returned verbatim: it already came
  // from this document's own source, so re-emitting it byte for byte is
  // always correct there.
  //
  // Inside a table cell, data-src gets the same tableCellText escaping as
  // every other leaf (the <code> span case above does the same for its text
  // before re-wrapping it in backticks), with one exception: a chip can be
  // moved into a cell by a drag or paste after mount, and its data-src can
  // hold a literal "\n" an html_comment picked up while it was still inside
  // an ordinary paragraph (node.raw in render.js, there under no one-line
  // constraint). Escaping that "\n" to a space would silently rewrite bytes
  // the chip promises to reproduce exactly; leaving it in would split the
  // regenerated row across physical lines (parseTableRow, md-serial.js,
  // reads one physical line per row). Neither is a one-line fix a caller can
  // trust, so this refuses outright: inlineToMarkdown throws, save() throws
  // the same error (see mountEditor's doc comment), and saveChecked() cannot
  // return a corrupted `source` for a caller to persist without checking
  // roundTrip.ok at all.
  //
  // A moved chip's data-src can also already hold a literal "\" immediately
  // before a "|" — same origin as the "\n" case, a paragraph places no escape
  // meaning on "\" at all. tableCellText's pipe-escape there adds one more
  // backslash on top (one more than a hand-authored table cell would show),
  // but it is still exact: splitTableCells (md-serial.js) always consumes
  // exactly one of however many backslashes sit before a "|", so N backslashes
  // written here recover N-1 on the next parse — the precise inverse of what
  // tableCellText adds, for any N including zero. The chip's own bytes always
  // come back unchanged (security review, PR #883 round 3, confirmed this
  // empirically and found no case where they don't); only the saved row's
  // text, for this one cell, can show a backslash a person would not have
  // typed by hand.
  if (cls.includes('md-chip')) {
    const src = node.getAttribute('data-src') || '';
    if (inTableCell && src.includes('\n')) {
      throw new EditorError(
        'inlineToMarkdown: a chip whose recorded source spans multiple lines cannot be placed in a table cell — the row would no longer be one physical line'
      );
    }
    return inTableCell ? tableCellText(src) : src;
  }

  if (!INLINE_TAGS.has(tag)) {
    throw new EditorError(`inlineToMarkdown: unrecognised inline element <${tag}>`);
  }

  // childNodes includes text nodes for mixed-content elements.
  // <b>/<i> are accepted alongside <strong>/<em>: a browser's own paste or
  // execCommand output produces the presentational tag, and both mean the
  // same Markdown emphasis either way.
  if (tag === 'strong' || tag === 'b') return '**' + inlinesToMarkdown(Array.from(node.childNodes), inTableCell) + '**';
  if (tag === 'em' || tag === 'i') return '*' + inlinesToMarkdown(Array.from(node.childNodes), inTableCell) + '*';

  if (tag === 'code') {
    // Inline code span — native textContent is correct by definition.
    const text = node.textContent;
    return codeSpanToMarkdown(inTableCell ? tableCellText(text) : text);
  }

  if (tag === 'br') {
    // A hard break has no one-line form ("  \n" is itself two physical
    // lines), and GFM gives a table cell no line-break syntax at all, so a
    // member's Shift-Enter inside a cell island serializes as a single
    // space instead of the paragraph hard break.
    return inTableCell ? ' ' : '  \n';
  }

  if (tag === 'a') {
    const href = node.getAttribute('href') || '';
    if (href.startsWith('deck-open:')) {
      // Resolved wikilink: [[name]].
      const text = node.textContent;
      return `[[${inTableCell ? tableCellText(text) : text}]]`;
    }
    return linkToMarkdown(node, href, node.getAttribute('title'), inTableCell);
  }

  if (tag === 'img') {
    const alt = node.getAttribute('alt') || '';
    const src = node.getAttribute('src') || '';
    const title = node.getAttribute('title');
    const safeAlt = inTableCell ? tableCellText(alt) : alt;
    const safeSrc = inTableCell ? tableCellText(src) : src;
    const safeTitle = title != null && inTableCell ? tableCellText(title) : title;
    const titlePart = safeTitle ? ` "${safeTitle}"` : '';
    return `![${safeAlt}](${safeSrc}${titlePart})`;
  }

  if (tag === 'span') {
    // md-unsafe-link: the renderer refused the href but kept the destination in
    // data-dest. Write it back; dropping it would destroy the link on any edit.
    if (cls.includes('md-unsafe-link')) {
      const dest = node.getAttribute('data-dest');
      if (dest == null) return inlinesToMarkdown(Array.from(node.childNodes), inTableCell);
      return linkToMarkdown(node, dest, node.getAttribute('data-dest-title'), inTableCell);
    }
    throw new EditorError(`inlineToMarkdown: unrecognised span class "${cls}"`);
  }
}

function linkToMarkdown(node, href, title, inTableCell = false) {
  const safeHref = inTableCell ? tableCellText(href) : href;
  const safeTitle = title != null && inTableCell ? tableCellText(title) : title;
  const titlePart = safeTitle ? ` "${safeTitle}"` : '';
  return `[${inlinesToMarkdown(Array.from(node.childNodes), inTableCell)}](${safeHref}${titlePart})`;
}

// Return the longest run of consecutive backticks in text.
function longestBacktickRun(text) {
  let max = 0, cur = 0;
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '`') { cur++; if (cur > max) max = cur; }
    else cur = 0;
  }
  return max;
}

// CommonMark's code-span delimiter must be a run of backticks strictly longer
// than any run inside the content, or the content's own run would close the
// span early (a single backtick inside a single-backtick delimiter) or merge
// with the delimiter into a longer run the parser reads as one token (a
// three-run against a four-backtick delimiter). Content that begins or ends
// with a backtick gets one space of padding on each side — tryParseCode
// (md-serial.js) strips exactly one leading and trailing space back off in
// that case, so the padded form round-trips. tryParseCode applies that same
// strip whenever content starts and ends with a space and isn't all spaces,
// with no backtick required, so content like " abc " needs the same padding
// or the parser's strip eats both boundary spaces on reparse.
//
// Empty content has no delimited form at all: tryParseCode's opening-backtick
// count is greedy, so a delimiter immediately followed by the matching
// closing delimiter — nothing between them — reads as one longer opening run
// with no close, not as an empty span, and no padding fixes that (a space
// pad would reparse as content " ", not ""). Emit no markup at all rather
// than a bare backtick run a reparse would read as literal text.
function codeSpanToMarkdown(text) {
  if (text === '') return '';
  const delim = '`'.repeat(longestBacktickRun(text) + 1);
  const needsPadding = text.startsWith('`') || text.endsWith('`') ||
    (text[0] === ' ' && text[text.length - 1] === ' ' && text.trim() !== '');
  const content = needsPadding ? ` ${text} ` : text;
  return delim + content + delim;
}

// ─── error type ───────────────────────────────────────────────────────────────

class EditorError extends Error {
  constructor(message) {
    super(message);
    this.name = 'EditorError';
  }
}
