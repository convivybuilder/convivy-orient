// render.js — Element-tree renderer for md-serial documents.
// ES module — zero external dependencies.
//
// Consumer entry point:
//   import { render, toHTML, toDOM, isSafeUrl } from './render.js'
//
// render(doc, src, opts = {})  →  element tree  { tag, attrs, children, node }
//   children:  array of elements and raw text strings (not yet HTML-escaped)
//   node:      back-reference to the md-serial node, or null for structural wrappers
//              load-bearing for editor: maps an element back to the node whose
//              offsets must be marked dirty when the element is edited
//
//   opts.resolveWikilink  optional  (name: string) => string | null
//     Resolves a [[name]] to its KB doc id (e.g. "deck/overview/deck-overview").
//     Returns null when the target is unknown or ambiguous. Any falsy return is
//     treated the same way, so '' means unresolved rather than "link to nothing".
//     The renderer has no KB index, so the caller owns resolution entirely.
//     With a resolver: resolved targets emit a deck-open anchor keyed by doc id;
//     unresolvable targets render as literal [[name]] text with no anchor.
//     Without a resolver (the default): all wikilinks render as literal text.
//     This default is intentionally inert — a caller that forgets to supply a
//     resolver gets visible-but-unclickable text rather than broken links.
//
// toHTML(tree)           →  HTML string; all text and attribute values are escaped
// toDOM(tree, document)  →  Map<mdSerialNode, HTMLElement>
//   The document root element is map.get(doc) where doc is the md-serial document node.
//   Uses createTextNode and setAttribute — never innerHTML.
//
// isSafeUrl(url, forImage)  →  boolean
//   The scheme allowlist a link or image destination must pass to render live.
//   Exposed so a consumer creating a destination the renderer never parsed
//   from source (editor.js's insertLink command) can enforce the same policy.

// ─── public API ──────────────────────────────────────────────────────────────

/** Render an md-serial document into an element tree. src is the original source string. */
export function render(doc, src, opts = {}) {
  return renderBlock(doc, src, opts);
}

/**
 * Serialize an element tree to an HTML string.
 * All text content and attribute values are HTML-escaped.
 */
export function toHTML(tree) {
  return elemToHTML(tree);
}

/**
 * Build real DOM nodes from an element tree.
 * Returns a Map from each md-serial node to the DOM element it produced.
 * Uses document.createElement, createTextNode, and setAttribute — never innerHTML.
 * The root DOM element is map.get(doc) where doc was passed to render().
 */
export function toDOM(tree, document) {
  const map = new Map();
  buildDOM(tree, document, map);
  return map;
}

// ─── element construction ─────────────────────────────────────────────────────

function el(tag, attrs, children, node = null) {
  return { tag, attrs, children, node };
}

// ─── block rendering ─────────────────────────────────────────────────────────

function renderBlock(node, src, opts = {}) {
  switch (node.type) {
    case 'document':
      return el('div', { class: 'md-doc' },
        node.children.map(c => renderBlock(c, src, opts)), node);

    case 'frontmatter':
      return renderFrontmatter(node, src);

    case 'heading':
      return el(`h${node.level}`, {}, renderInlines(node.content, opts), node);

    case 'paragraph':
      return el('p', {}, renderInlines(node.content, opts), node);

    case 'fenced_code': {
      // Info string's first word becomes the language class for later highlighting.
      // Content is escaped text, never interpreted.
      const lang = node.info ? node.info.split(/\s+/)[0] : '';
      const codeAttrs = lang ? { class: `language-${lang}` } : {};
      return el('pre', {}, [el('code', codeAttrs, [node.content], null)], node);
    }

    case 'blockquote':
      // Children's offsets are relative to virtualSrc, not the outer src.
      return el('blockquote', {},
        node.children.map(c => renderBlock(c, node.virtualSrc, opts)), node);

    case 'thematic_break':
      return el('hr', {}, [], node);

    case 'list': {
      const tag = node.listType === 'ordered' ? 'ol' : 'ul';
      const attrs = {};
      if (node.listType === 'ordered') {
        // Always emit start so the number is explicit even when start === 1.
        attrs.start = String(olStart(node));
      }
      return el(tag, attrs, node.items.map(item => renderListItem(item, opts)), node);
    }

    case 'list_item':
      // The parser only ever emits list_item inside list.items[i], so this
      // arm is unreachable today. It stays because a list_item reaching
      // renderBlock is a shape change worth rendering rather than throwing on.
      return renderListItem(node, opts);

    case 'table':
      return renderTable(node, opts);

    case 'footnote_def': {
      // Inert locked chip — verbatim source in data attribute (fidelity rule 3).
      // Offsets are into the src that was active when this node was parsed.
      const raw = src != null ? src.slice(node.start, node.end) : '';
      return el('div', {
        class: 'md-chip md-footnote-def',
        'data-src': raw,
      }, [], node);
    }

    case 'chip': {
      // Emitted when nesting exceeds MAX_NESTING_DEPTH — inert locked chip.
      const raw = src != null ? src.slice(node.start, node.end) : '';
      return el('div', {
        class: 'md-chip',
        'data-src': raw,
      }, [], node);
    }

    default:
      throw new RenderError(`Unknown block node type: "${node.type}"`);
  }
}

// List-item blocks' offsets are relative to item.virtualContent, not the outer src.
// Simple inline content uses the inline nodes directly (no src slice needed).
function renderListItem(item, opts = {}) {
  const children = [];
  if (item.content != null) {
    children.push(...renderInlines(item.content, opts));
  } else if (item.blocks != null) {
    for (const block of item.blocks) {
      children.push(renderBlock(block, item.virtualContent, opts));
    }
  }
  return el('li', {}, children, item);
}

function renderTable(node, opts = {}) {
  const { headerCells, dataRows, alignments } = node;

  function cellStyle(i) {
    const align = alignments[i];
    return (align && align !== 'left') ? { style: `text-align: ${align}` } : {};
  }

  const headerRow = el('tr', {},
    headerCells.map((cell, i) =>
      el('th', cellStyle(i), renderInlines(cell.inlines, opts), null)),
    null);

  const bodyRows = dataRows.map(row =>
    el('tr', {},
      row.map((cell, i) =>
        el('td', cellStyle(i), renderInlines(cell.inlines, opts), null)),
      null));

  return el('table', {}, [
    el('thead', {}, [headerRow], null),
    el('tbody', {}, bodyRows, null),
  ], node);
}

function renderFrontmatter(node, src) {
  // Parse YAML fields from the raw source slice.
  // The node's end includes trailing blank lines; parseFrontmatterFields stops
  // at the closing --- so trailing whitespace is harmless here.
  const raw = src != null ? src.slice(node.start, node.end) : '';
  const fields = parseFrontmatterFields(raw);
  const rows = fields.map(({ key, value }) =>
    el('div', { class: 'md-fm-field' }, [
      el('span', { class: 'md-fm-key' }, [key], null),
      el('span', { class: 'md-fm-value' }, [value], null),
    ], null));
  return el('section', { class: 'md-frontmatter' }, rows, node);
}

function parseFrontmatterFields(raw) {
  // Minimal key:value extractor — no full YAML parser needed for display.
  const fields = [];
  const lines = raw.split('\n');
  let pastOpen = false;
  for (const line of lines) {
    const stripped = line.replace(/\r$/, '');
    if (!pastOpen) { pastOpen = true; continue; } // skip opening ---
    if (stripped === '---' || stripped === '...') break;
    if (!stripped) continue;
    const colon = stripped.indexOf(':');
    if (colon !== -1) {
      fields.push({
        key: stripped.slice(0, colon).trim(),
        value: stripped.slice(colon + 1).trim(),
      });
    }
  }
  return fields;
}

// ─── inline rendering ─────────────────────────────────────────────────────────

function renderInlines(nodes, opts = {}) {
  if (!nodes || !nodes.length) return [];
  return nodes.map(n => renderInline(n, opts));
}

function renderInline(node, opts = {}) {
  switch (node.type) {
    case 'text':
      // Raw string — toHTML escapes it; toDOM uses createTextNode.
      return node.content;

    case 'softbreak':
      // A wrapped line in the source becomes a space in rendered HTML.
      return ' ';

    case 'hardbreak':
      return el('br', {}, [], node);

    case 'strong':
      return el('strong', {}, renderInlines(node.content, opts), node);

    case 'em':
      return el('em', {}, renderInlines(node.content, opts), node);

    case 'code_span':
      return el('code', {}, [node.content], node);

    case 'link': {
      if (!isSafeUrl(node.url, false)) {
        // Reject unsafe schemes (e.g. javascript:) — render text with no href.
        // The destination rides along in an inert data attribute so the editor
        // can write it back on save; without it, editing the block would drop
        // the destination of every link this check refuses, which includes
        // relative paths, #anchors and mailto: as well as javascript:.
        const attrs = { class: 'md-unsafe-link', 'data-dest': node.url };
        if (node.title) attrs['data-dest-title'] = node.title;
        return el('span', attrs, renderInlines(node.content, opts), node);
      }
      const attrs = { href: node.url };
      if (node.title) attrs.title = node.title;
      // `inAnchor` tells a nested wikilink not to emit its own <a>. An anchor
      // inside an anchor is invalid HTML, and a browser recovers by closing the
      // outer one early, which leaves the outer link an empty unclickable stub
      // and moves its text under the inner href.
      return el('a', attrs, renderInlines(node.content, { ...opts, inAnchor: true }), node);
    }

    case 'image': {
      // data: is allowed for images so embedded base64 images work.
      const attrs = { alt: node.alt || '' };
      if (isSafeUrl(node.url, true)) attrs.src = node.url;
      if (node.title) attrs.title = node.title;
      return el('img', attrs, [], node);
    }

    case 'wikilink': {
      // Deck fetches by KB doc id, not by the name in the source — the caller
      // must resolve [[name]] → docId via opts.resolveWikilink. Without a
      // resolver, or when the target is unresolvable, render as literal text.
      // Any falsy return counts as unresolved, not just null. A lookup miss is
      // as likely to surface as '' as it is as null, and `docId == null` let the
      // empty string through to href="deck-open:?origin=kb&ref=" — a link that
      // looks clickable and goes nowhere.
      const docId = opts.resolveWikilink ? opts.resolveWikilink(node.name) : null;
      if (!docId) return `[[${node.name}]]`;
      // Inside a link, emit the display name as plain text. The alternative is
      // an <a> inside an <a>, which no browser keeps: the parser closes the
      // outer anchor before opening this one, so the outer link silently
      // becomes an empty stub and its text ends up navigating here instead.
      // Losing the wikilink's own navigation is the smaller loss, and it is the
      // one the author asked for by writing the outer link.
      if (opts.inAnchor) return node.name;
      const href = `deck-open:?origin=kb&ref=${encodeURIComponent(docId)}`;
      return el('a', { href }, [node.name], node);
    }

    case 'html_comment':
      // Inert locked chip — verbatim source in data attribute (fidelity rule 3).
      return el('span', {
        class: 'md-chip md-html-comment',
        'data-src': node.raw,
      }, [], node);

    case 'footnote_ref':
      // Inert locked chip — render the raw reference text so it is visible;
      // verbatim source also in data attribute for the editor.
      return el('span', {
        class: 'md-chip md-footnote-ref',
        'data-src': node.raw,
      }, [node.raw], node);

    default:
      throw new RenderError(`Unknown inline node type: "${node.type}"`);
  }
}

// ─── URL safety ──────────────────────────────────────────────────────────────

// Scheme allowlist: http/https for external links; deck-open for in-app
// navigation; data: for images only (never for links, to limit exfiltration).
// This page runs in a WKWebView with script enabled — a javascript: href is
// code execution in a pane that holds a capability token.
//
// Exported so editor.js's insertLink() command enforces the same policy on a
// link a member creates live, not only on one the renderer redraws from saved
// Markdown.
export function isSafeUrl(url, forImage) {
  if (!url) return false;
  const lower = url.toLowerCase().trimStart();
  if (lower.startsWith('https:') || lower.startsWith('http:')) return true;
  if (lower.startsWith('deck-open:')) return true;
  if (forImage && lower.startsWith('data:image/')) return true;
  return false;
}

// ─── ordered list start ───────────────────────────────────────────────────────

function olStart(list) {
  if (!list.items || !list.items.length) return 1;
  // item.prefix is the full marker prefix, e.g. "3. " or "  1) " — extract the number.
  const m = /(\d+)/.exec(list.items[0].prefix);
  return m ? parseInt(m[1], 10) : 1;
}

// ─── toHTML ───────────────────────────────────────────────────────────────────

const VOID_TAGS = new Set(['br', 'hr', 'img']);

function elemToHTML(node) {
  if (typeof node === 'string') {
    return escapeHtml(node);
  }
  const attrStr = Object.entries(node.attrs)
    .map(([k, v]) => ` ${k}="${escapeHtml(String(v))}"`)
    .join('');
  if (VOID_TAGS.has(node.tag)) {
    return `<${node.tag}${attrStr}>`;
  }
  const inner = node.children.map(elemToHTML).join('');
  return `<${node.tag}${attrStr}>${inner}</${node.tag}>`;
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ─── toDOM ────────────────────────────────────────────────────────────────────

function buildDOM(node, doc, map) {
  if (typeof node === 'string') {
    return doc.createTextNode(node);
  }
  const domEl = doc.createElement(node.tag);
  for (const [k, v] of Object.entries(node.attrs)) {
    domEl.setAttribute(k, String(v));
  }
  for (const child of node.children) {
    domEl.appendChild(buildDOM(child, doc, map));
  }
  // node.node is the back-reference to the md-serial node; null for structural wrappers.
  if (node.node != null) {
    map.set(node.node, domEl);
  }
  return domEl;
}

// ─── error type ───────────────────────────────────────────────────────────────

class RenderError extends Error {
  constructor(message) {
    super(message);
    this.name = 'RenderError';
  }
}
