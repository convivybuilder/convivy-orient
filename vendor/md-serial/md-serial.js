// md-serial.js — Markdown parser/serializer with source offsets and hunk support.
// ES module — zero external dependencies.
//
// Consumer entry point: import { parse, serialize, markAllDirty, hunks } from './md-serial.js'
// Load as <script type="module"> with no bundler required.

// ─── line utilities ──────────────────────────────────────────────────────────

/** Position after the newline ending the line that starts at pos. */
function eol(src, pos) {
  const i = src.indexOf('\n', pos);
  return i < 0 ? src.length : i + 1;
}

/** True when every character on the line at pos is whitespace (or the line is empty). */
function isBlank(src, pos) {
  const e = eol(src, pos);
  // the line content is src[pos..e), which ends with '\n' if present
  for (let i = pos; i < e; i++) {
    const c = src[i];
    if (c !== ' ' && c !== '\t' && c !== '\r' && c !== '\n') return false;
  }
  return true;
}

/** Advance pos past all following blank lines. Returns {pos, trailing}. */
function consumeBlankLines(src, pos) {
  const start = pos;
  while (pos < src.length && isBlank(src, pos)) {
    pos = eol(src, pos);
  }
  return { pos, trailing: src.slice(start, pos) };
}

/** Return the line-ending bytes at lineE: '\r\n', '\n', or '' if no newline. */
function detectLineEnding(src, lineE) {
  if (lineE > 0 && src[lineE - 1] === '\n') {
    return (lineE > 1 && src[lineE - 2] === '\r') ? '\r\n' : '\n';
  }
  return '';
}

// ─── depth limits ────────────────────────────────────────────────────────────

// Maximum container-nesting depth before emitting an inert chip instead of
// recursing.  Measured on macOS (plain JS recursion in a JSContext, 2026-08-04):
// JSC max plain recursion depth ≈ 39 928 frames.  md-serial's mutual-recursion
// frames are heavier than the probe's single-function depth, so treat 39 928 as
// a floor, not a direct ratio; 100 nesting levels at ~3 frames each is ≈ 300
// frames against that budget.
const MAX_NESTING_DEPTH = 100;

// Maximum inline-recursion depth (parseInlines ↔ tryParseEmphasis, parseInlines
// ↔ tryParseLink) before emitting remaining content as verbatim text.  Measured
// on Node.js: a heading containing '*' x 10 000 overflows the call stack via
// the parseInlines→tryParseEmphasis→parseInlines chain (~2 500 frame pairs).
// 100 levels covers any real nested-inline structure while staying far inside
// the JSC budget above.
const MAX_INLINE_DEPTH = 100;

// Maximum characters scanned per findMatchingBracket call.  Measured: 16 K of
// '![' (958 ms) and '[' (225 ms) characters exhibit O(N²) scan without a bound;
// with MAX_BRACKET_SCAN = 1 000 the cost falls to O(N·1 000) ≈ 16 ms at the
// same input size.  Real link text and URL strings in this corpus are well under
// 1 000 characters.
const MAX_BRACKET_SCAN = 1000;

// Maximum characters scanned per findDelimiter call.  The '_'-specific closer
// rule rejects any candidate where the next character is alphanumeric, so in
// patterns like "_word _word _word …" every opener exhausts the full line
// before giving up — O(N²) total.  Capping each scan bounds the cost to
// O(N·bound).  Characters beyond the cap become verbatim text, preserving all
// bytes: this is a rendering bound, never a data-loss one.
//
// RAISED FROM 1 000 ON 2026-08-05, BECAUSE 1 000 WAS ALREADY BEING HIT.  The
// old comment said the corpus contained no emphasis spanning more than a few
// hundred characters.  That was true of spans that MATCH; it said nothing about
// a scan that fails and gives up early.  Measured over the 616 documents under
// projects/ at 551054f: 191 lines already exceed 1 000 characters, the longest
// is 3 017, the scan reached the bound twice, and on
// constance/specs/flow-2-shift-note-constance-task.md that cost one real `em`
// node — emphasis rendering as literal asterisks in the editor while the
// read-only Python renderer showed it correctly.  That is the two-renderer
// divergence this package exists to avoid, arriving through the bound.
//
// 8 000 clears the longest line in the corpus by 2.65×, so the bound no longer
// decides anything about a document that exists.  It costs 25 ms across the
// whole corpus (112 ms → 137 ms for 616 documents, about 0.04 ms each) and
// still caps the pathological shape: a single 100 KB line of "_word " parses in
// 322 ms rather than growing quadratically.
//
// THE BOUND IS A CONSTANT RATHER THAN DERIVED FROM THE CORPUS, deliberately.
// This parser runs inside Deck's editor against one document, with no corpus in
// reach, and a bound that varied with what OTHER documents contain would make a
// document parse differently in two places for reasons its author cannot see.
// What tracks the corpus instead is `boundStats()` below, which reports when a
// bound fires so the next crossing is visible on the first document that does
// it rather than the first one somebody notices.
const MAX_DELIMITER_SCAN = 8000;

// ─── bound reporting ─────────────────────────────────────────────────────────

// A bound firing is INVISIBLE TO EVERY EXISTING CHECK, which is why this
// exists.  Past a bound a construct stops parsing as a link or as emphasis and
// its characters become verbatim text; the bytes are unchanged, so the corpus
// test's byte-identity assertion passes either way, and the document renders
// differently in the editor with nothing anywhere saying so.
//
// These counters make that observable.  They are process-global and monotonic
// rather than per-parse, because the caller that cares is a batch check reading
// them once at the end; `resetBoundStats()` is there for a caller that wants a
// window.  Nothing in the parse path reads them, so they cannot change what a
// document parses to.
const boundHits = { inlineDepth: 0, bracketScan: 0, delimiterScan: 0 };

/** How many times each bound has truncated a scan since the last reset. */
export function boundStats() {
  return { ...boundHits };
}

/** Zero the counters, for a caller measuring one document rather than a run. */
export function resetBoundStats() {
  boundHits.inlineDepth = 0;
  boundHits.bracketScan = 0;
  boundHits.delimiterScan = 0;
}

// ─── block regexps ───────────────────────────────────────────────────────────

const RE_ATX          = /^(#{1,6})([ \t]|$)/;
const RE_FENCE        = /^(`{3,}|~{3,})(.*)/;
const RE_BQ           = /^( {0,3}>)([ \t]?)/;
const RE_BULLET       = /^( {0,3})([-*+])([ \t]+)/;
const RE_ORDERED      = /^( {0,3})(\d{1,9})([.)])([ \t]+)/;
const RE_THEMATIC     = /^[ \t]{0,3}([-*_])([ \t]*\1){2,}[ \t]*(\n|$)/;
const RE_SETEXT_H1    = /^=+[ \t]*$/;
const RE_SETEXT_H2    = /^-+[ \t]*$/;
const RE_FOOTNOTE_DEF = /^\[\^[^\]]+\]:/;

// ─── parse ───────────────────────────────────────────────────────────────────

/** Parse a Markdown source string into a document tree. */
export function parse(src) {
  return parseDoc(src, 0);
}

function parseDoc(src, depth) {
  let pos = 0;
  const children = [];

  // Frontmatter — chip, not editable body per spec.
  if (src.startsWith('---\n') || src.startsWith('---\r\n')) {
    const fm = parseFrontmatter(src, 0);
    if (fm) { children.push(fm.node); pos = fm.pos; }
  }

  while (pos < src.length) {
    const b = parseBlock(src, pos, 0, depth);
    if (b.node !== null) children.push(b.node);
    pos = b.pos;
  }

  return { type: 'document', start: 0, end: src.length, dirty: false, children };
}

/** Parse YAML frontmatter. Returns {node, pos} or null. */
function parseFrontmatter(src, pos) {
  const openEnd = eol(src, pos); // skip past opening ---\n
  let p = openEnd;
  while (p < src.length) {
    const lineE = eol(src, p);
    const line = src.slice(p, lineE).replace(/\r?\n$/, '');
    if (line === '---' || line === '...') {
      p = lineE; // consume the closing ---
      const { pos: np, trailing } = consumeBlankLines(src, p);
      return {
        node: { type: 'frontmatter', start: pos, end: np, dirty: false, trailing },
        pos: np,
      };
    }
    p = lineE;
  }
  return null;
}

/**
 * Parse one block starting at pos.
 * indent = the minimum indent that continues an enclosing container (0 at top level).
 */
function parseBlock(src, pos, indent, depth = 0) {
  // Skip truly blank lines — caller should already have consumed trailing, but be safe.
  while (pos < src.length && isBlank(src, pos)) {
    pos = eol(src, pos);
  }
  if (pos >= src.length) return { node: null, pos };

  // Strip the line ending so CRLF documents dispatch correctly
  // (RE_THEMATIC, RE_ATX use $ which doesn't match before \r\n).
  const line = src.slice(pos, eol(src, pos)).replace(/\r?\n$/, '');

  // ATX heading
  const atxM = RE_ATX.exec(line);
  if (atxM) return parseHeading(src, pos, atxM);

  // Fenced code block
  const fenceM = RE_FENCE.exec(line);
  if (fenceM) return parseFencedCode(src, pos, fenceM, indent);

  // Blockquote
  if (RE_BQ.test(line)) return parseBlockquote(src, pos, depth);

  // Thematic break — must test BEFORE list markers since '***' would otherwise look like italic
  // and '---' might match the bullet list (it doesn't, but be explicit).
  // A thematic break is 3+ of the same char with optional spaces, whole line.
  if (RE_THEMATIC.test(line)) return parseThematicBreak(src, pos);

  // GFM table — header line with pipes, next line is separator
  if (looksLikeTableHeader(src, pos)) return parseTable(src, pos);

  // Bullet list
  const bulletM = RE_BULLET.exec(line);
  if (bulletM) return parseList(src, pos, bulletM, 'bullet', depth);

  // Ordered list
  const orderedM = RE_ORDERED.exec(line);
  if (orderedM) return parseList(src, pos, orderedM, 'ordered', depth);

  // Block-level footnote definition — chip carrying verbatim source
  if (RE_FOOTNOTE_DEF.test(line)) return parseFootnoteDef(src, pos);

  // Default: paragraph
  return parseParagraph(src, pos);
}

function parseHeading(src, pos, atxM) {
  const start = pos;
  const lineE = eol(src, pos);
  const rawLine = src.slice(pos, lineE).replace(/\r?\n$/, '');
  const lineEnding = detectLineEnding(src, lineE);

  const level = atxM[1].length;
  // Content starts after "### " (or "###\t" or "###\n")
  let contentStart = atxM[1].length + (atxM[2] ? atxM[2].length : 0);
  // Strip optional closing sequence: trailing spaces + optional '#' + trailing spaces
  let rawContent = rawLine.slice(contentStart);
  // Remove trailing closing hashes: whitespace* + #* + whitespace*
  rawContent = rawContent.replace(/[ \t]+#+[ \t]*$/, '');
  // Strip plain trailing whitespace too, but keep it verbatim so a regenerated
  // heading matches the source byte-for-byte instead of trimming it away.
  const trailingWsM = /[ \t]+$/.exec(rawContent);
  const trailingSpace = trailingWsM ? trailingWsM[0] : '';
  rawContent = rawContent.slice(0, rawContent.length - trailingSpace.length);

  const { pos: np, trailing } = consumeBlankLines(src, lineE);
  const content = parseInlines(rawContent, 0, rawContent.length);

  return {
    node: {
      type: 'heading', start, end: np, dirty: false,
      level, content, trailingSpace, trailing, lineEnding,
    },
    pos: np,
  };
}

function parseFencedCode(src, pos, fenceM, outerIndent) {
  const start = pos;
  const lineE = eol(src, pos);
  const openLine = src.slice(pos, lineE);

  // Determine leading indent of the fence line
  const indentMatch = /^( *)/.exec(openLine);
  const fenceIndent = indentMatch ? indentMatch[1] : '';
  const fenceChar = fenceM[1][0]; // '`' or '~'
  const fenceLen = fenceM[1].length;
  const fence = fenceChar.repeat(fenceLen);
  const info = fenceM[2].trim();

  // Opening line: fenceIndent + fence + info + \n
  // We need to store the actual opening line for accurate dirty serialization.
  const fenceOpen = openLine.replace(/\r?\n$/, '');
  const fenceOpenLineEnding = detectLineEnding(src, lineE);

  let p = lineE;
  let contentStart = p;
  let content = '';
  let fenceClose = '';
  let fenceCloseLineEnding = fenceOpenLineEnding || '\n';
  let foundClose = false;

  while (p < src.length) {
    const lE = eol(src, p);
    const ln = src.slice(p, lE).replace(/\r?\n$/, '');

    // Closing fence: optional indent (up to 3 spaces) + fence chars (>= opening length)
    const closeRe = new RegExp(`^( {0,3})${fenceChar === '`' ? '`' : '~'}{${fenceLen},}[ \\t]*$`);
    if (closeRe.test(ln)) {
      fenceClose = ln;
      // A closing fence that IS the last byte of the document has no line
      // ending. `detectLineEnding` returns '' for that and for a detection it
      // could not make, and the `||` chain below used to treat the two the
      // same and substitute `\n`, so a document
      // ending `\`\`\`` with no trailing newline grew one the first time an
      // edit made this node dirty — a one-byte change nobody asked for, in a
      // document the editor was only meant to round-trip.
      const closeLE = detectLineEnding(src, lE);
      fenceCloseLineEnding = closeLE !== ''
        ? closeLE
        : (lE >= src.length ? '' : (fenceOpenLineEnding || '\n'));
      content = src.slice(contentStart, p);
      p = lE;
      foundClose = true;
      break;
    }
    p = lE;
  }

  if (!foundClose) {
    content = src.slice(contentStart, p);
    fenceClose = fence;
  }

  const { pos: np, trailing } = consumeBlankLines(src, p);
  return {
    node: {
      type: 'fenced_code', start, end: np, dirty: false,
      fenceOpen, fenceOpenLineEnding, fence, fenceChar, fenceLen, fenceIndent,
      info, content, fenceClose, fenceCloseLineEnding, hasClose: foundClose, trailing,
    },
    pos: np,
  };
}

function parseBlockquote(src, pos, depth = 0) {
  const start = pos;
  // Collect all consecutive blockquote lines (and lazy continuation).
  // We stop when we hit a line that doesn't start with '>' and isn't a lazy continuation.
  const bqLines = []; // {origPos, content, marker} per line

  let p = pos;
  while (p < src.length) {
    if (isBlank(src, p)) {
      // A raw blank line (no '>' prefix) ends the blockquote. CommonMark lazy continuation
      // does NOT extend across raw blank lines — those create separate blockquotes.
      break;
    } else {
      const lineE = eol(src, p);
      const ln = src.slice(p, lineE);
      const bqM = RE_BQ.exec(ln);
      if (bqM) {
        const marker = bqM[1]; // '>' or '  >'  etc.
        const space = bqM[2];  // optional space after '>'
        const content = ln.slice(marker.length + space.length);
        bqLines.push({ origPos: p, content, marker, space, isBlank: false });
        p = lineE;
      } else {
        // Lazy continuation — check if we're inside a paragraph-type blockquote
        // For simplicity, stop blockquote parsing here.
        break;
      }
    }
  }

  // Advance past trailing blank lines; compute this before the depth guard so
  // the chip node has the correct end position.
  const { pos: np, trailing } = consumeBlankLines(src, p);

  // Past the nesting limit: emit an inert chip rather than recursing.
  // The chip round-trips identically via source.slice(start, end).
  if (depth >= MAX_NESTING_DEPTH) {
    return { node: { type: 'chip', start, end: np, dirty: false }, pos: np };
  }

  // Build virtual source (stripped content) and parse it.
  let virtualSrc = '';
  const lineMap = []; // maps each virtualSrc line start to origPos and prefixLen
  // Leading indent before '>' on the first blockquote line (e.g. '  ' for '  > /rc').
  const firstMarker = bqLines.length > 0 ? bqLines[0].marker : '>';
  const gtPos = firstMarker.indexOf('>');
  const leadingIndent = gtPos > 0 ? firstMarker.slice(0, gtPos) : '';

  let blankWithSpace = 0;
  let blankNoSpace = 0;
  let contentWithSpace = 0;
  let contentNoSpace = 0;
  for (const bl of bqLines) {
    const virtStart = virtualSrc.length;
    if (bl.isBlank) {
      lineMap.push({ virtPos: virtStart, origPos: bl.origPos, prefixLen: 0 });
      virtualSrc += '\n';
    } else {
      const prefixLen = bl.marker.length + bl.space.length;
      lineMap.push({ virtPos: virtStart, origPos: bl.origPos, prefixLen });
      virtualSrc += bl.content;
      if (bl.content === '\n') {
        // Blank inner line style: "> \n" or ">\n"
        if (bl.space === ' ') blankWithSpace++;
        else blankNoSpace++;
      } else {
        // Content line style: "> text" or ">text"
        if (bl.space === ' ' || bl.space === '\t') contentWithSpace++;
        else contentNoSpace++;
      }
    }
  }
  // blankStyle: space between '>' and '\n' on blank inner lines.
  const blankStyle = blankNoSpace > blankWithSpace ? '' : ' ';
  // contentSpace: space between '>' and content on non-blank lines.
  const contentSpace = contentNoSpace > contentWithSpace ? '' : ' ';

  // Parse virtual source as blocks, incrementing depth so nested containers
  // can trigger their own limit.
  const innerDoc = parseDoc(virtualSrc, depth + 1);

  return {
    node: {
      type: 'blockquote', start, end: np, dirty: false,
      virtualSrc, children: innerDoc.children, lineMap,
      leadingIndent, blankStyle, contentSpace, trailing,
    },
    pos: np,
  };
}

function parseThematicBreak(src, pos) {
  const start = pos;
  const lineE = eol(src, pos);
  const chars = src.slice(pos, lineE).replace(/\r?\n$/, '');
  const lineEnding = detectLineEnding(src, lineE);
  const { pos: np, trailing } = consumeBlankLines(src, lineE);
  return {
    node: { type: 'thematic_break', start, end: np, dirty: false, chars, trailing, lineEnding },
    pos: np,
  };
}

function parseFootnoteDef(src, pos) {
  const start = pos;
  let p = eol(src, pos); // consume first line
  // Continuation lines must be indented by at least 4 spaces
  while (p < src.length && !isBlank(src, p)) {
    if (/^ {4}/.test(src.slice(p, eol(src, p)))) {
      p = eol(src, p);
    } else {
      break;
    }
  }
  const { pos: np, trailing } = consumeBlankLines(src, p);
  return {
    node: { type: 'footnote_def', start, end: np, dirty: false, trailing },
    pos: np,
  };
}

/** True when the line at pos has pipes AND the next line is a GFM table separator. */
function looksLikeTableHeader(src, pos) {
  const lineE = eol(src, pos);
  const headerLine = src.slice(pos, lineE);
  if (!headerLine.includes('|')) return false;

  const nextPos = lineE;
  if (nextPos >= src.length) return false;
  const nextLineE = eol(src, nextPos);
  const nextLine = src.slice(nextPos, nextLineE).replace(/\r?\n$/, '').trim();
  // Must be something like |---|---| or :---: | --- etc.
  return /^\|?[ \t]*:?-[-: \t]*\|/.test(nextLine);
}

function parseTable(src, pos) {
  const start = pos;
  const rows = [];
  let p = pos;
  let separatorLine = null;

  // Read header row
  const headerLineE = eol(src, p);
  const headerRaw = src.slice(p, headerLineE).replace(/\r?\n$/, '');
  p = headerLineE;

  // Read separator row
  const sepLineE = eol(src, p);
  separatorLine = src.slice(p, sepLineE).replace(/\r?\n$/, '');
  p = sepLineE;

  // Read data rows until we hit a blank line or non-table line
  while (p < src.length && !isBlank(src, p)) {
    const lineE = eol(src, p);
    const ln = src.slice(p, lineE).replace(/\r?\n$/, '');
    if (!ln.includes('|')) break;
    rows.push(ln);
    p = lineE;
  }

  const { pos: np, trailing } = consumeBlankLines(src, p);

  const headerCells = parseTableRow(headerRaw);
  const alignments = parseTableSeparator(separatorLine);
  const dataRows = rows.map(r => parseTableRow(r));

  return {
    node: {
      type: 'table', start, end: np, dirty: false,
      headerRaw, separatorLine, headerCells, alignments, dataRows, rows, trailing,
    },
    pos: np,
  };
}

function parseTableRow(raw) {
  // Split on |, trimming leading/trailing pipes
  const trimmed = raw.replace(/^\|/, '').replace(/\|$/, '');
  return trimmed.split('|').map(cell => {
    const trimmedCell = cell.trim();
    return { raw: cell, inlines: parseInlines(trimmedCell, 0, trimmedCell.length) };
  });
}

function parseTableSeparator(raw) {
  const trimmed = raw.replace(/^\|/, '').replace(/\|$/, '');
  return trimmed.split('|').map(col => {
    const t = col.trim();
    if (t.startsWith(':') && t.endsWith(':')) return 'center';
    if (t.endsWith(':')) return 'right';
    return 'left'; // default or starts with ':'
  });
}

function parseList(src, pos, markerMatch, listType, depth = 0) {
  const start = pos;
  const items = [];
  const afterBlanks = []; // afterBlanks[i] = true when item[i] was followed by a blank line

  // The marker match tells us: indent + marker + space
  const firstIndent = markerMatch[1];
  const marker = markerMatch[listType === 'bullet' ? 2 : 2];
  const markerSpace = markerMatch[listType === 'bullet' ? 3 : 4];
  const markerFull = markerMatch[0]; // the full matched prefix (indent + marker + space)
  const continuationIndent = markerFull.length; // width of the full prefix

  let p = pos;

  while (p < src.length) {
    const ln = src.slice(p, eol(src, p));
    let itemMatch;

    if (listType === 'bullet') {
      itemMatch = RE_BULLET.exec(ln);
      if (!itemMatch) break;
      // Must have same indent and same (or compatible) marker
      if (itemMatch[1].length !== firstIndent.length) break;
    } else {
      itemMatch = RE_ORDERED.exec(ln);
      if (!itemMatch) break;
      if (itemMatch[1].length !== firstIndent.length) break;
    }

    const itemResult = parseListItem(src, p, itemMatch, continuationIndent, depth);
    items.push(itemResult.node);
    afterBlanks.push(false); // default: no blank after this item
    p = itemResult.pos;

    // Check for blank lines between items
    if (p < src.length && isBlank(src, p)) {
      // Consume blank lines to check if list continues
      const blankEnd = consumeBlankLines(src, p);
      if (blankEnd.pos < src.length) {
        const nextLine = src.slice(blankEnd.pos, eol(src, blankEnd.pos));
        let nextMatch;
        if (listType === 'bullet') {
          nextMatch = RE_BULLET.exec(nextLine);
          if (nextMatch && nextMatch[1].length === firstIndent.length) {
            afterBlanks[afterBlanks.length - 1] = true; // this item was followed by a blank
            p = blankEnd.pos;
            continue;
          }
        } else {
          nextMatch = RE_ORDERED.exec(nextLine);
          if (nextMatch && nextMatch[1].length === firstIndent.length) {
            afterBlanks[afterBlanks.length - 1] = true;
            p = blankEnd.pos;
            continue;
          }
        }
      }
      // No continuation: list ends here; don't consume the blank lines (let caller's trailing do it)
      break;
    }
  }

  const tight = afterBlanks.every(b => !b);
  const { pos: np, trailing } = consumeBlankLines(src, p);
  return {
    node: {
      type: 'list', start, end: np, dirty: false,
      listType, tight, afterBlanks, items, firstIndent, trailing,
    },
    pos: np,
  };
}

function parseListItem(src, pos, markerMatch, continuationIndent, depth = 0) {
  const start = pos;
  const lineE = eol(src, pos);
  const lineEnding = detectLineEnding(src, lineE);

  const prefix = markerMatch[0]; // full marker prefix, e.g. "- " or "  - "
  const rawFirstLine = src.slice(pos + prefix.length, lineE).replace(/\r?\n$/, '');

  let p = lineE;
  const extraLines = [];
  let hadBlankLine = false;
  let blankLines = '';

  // Collect continuation lines
  while (p < src.length) {
    if (isBlank(src, p)) {
      // Possible blank line within a list item (loose item)
      const blankStart = p;
      let pp = p;
      while (pp < src.length && isBlank(src, pp)) pp = eol(src, pp);
      // Check if what follows is a continuation (indented at least continuationIndent)
      if (pp < src.length) {
        const nextLn = src.slice(pp, eol(src, pp));
        // Check if it's another list item at the SAME or LESSER indent — end of this item
        const bM = RE_BULLET.exec(nextLn);
        const oM = RE_ORDERED.exec(nextLn);
        const nextIndent = /^( *)/.exec(nextLn)[1].length;
        // Continuation: indented by at least continuationIndent AND not a sub-list item at start
        if (nextIndent >= continuationIndent && !bM && !oM) {
          hadBlankLine = true;
          blankLines = src.slice(blankStart, pp);
          extraLines.push({ type: 'blank', text: blankLines });
          p = pp;
          continue;
        } else if (nextIndent >= continuationIndent && (bM || oM)) {
          // Sub-list item — still part of this list item's continuation
          hadBlankLine = true;
          blankLines = src.slice(blankStart, pp);
          extraLines.push({ type: 'blank', text: blankLines });
          p = pp;
          continue;
        }
      }
      break;
    }

    const lE = eol(src, p);
    const ln = src.slice(p, lE);
    const indent = /^( *)/.exec(ln)[1].length;

    // Stop if this line is at a lesser or equal indent to the list marker AND is not a continuation
    if (indent < continuationIndent) break;

    extraLines.push({ type: 'content', text: src.slice(p, lE), indent });
    p = lE;
  }

  // Build the virtual content of this list item:
  // firstLine + stripped continuation lines
  let virtualContent = rawFirstLine + '\n';
  for (const el of extraLines) {
    if (el.type === 'blank') {
      virtualContent += el.text;
    } else {
      // Strip the continuation indent
      const stripped = el.text.slice(continuationIndent);
      virtualContent += stripped;
    }
  }

  // Parse the virtual content as blocks (for multi-paragraph items / nested lists).
  // Past the nesting limit emit a chip so the mutual recursion cannot overflow the stack.
  const innerDoc = depth >= MAX_NESTING_DEPTH
    ? { children: [{ type: 'chip', start: 0, end: virtualContent.length, dirty: false }] }
    : parseDoc(virtualContent, depth + 1);

  // For inline-only items (single paragraph), use inline content directly
  let content = null;
  let blocks = null;
  if (
    innerDoc.children.length === 1 &&
    innerDoc.children[0].type === 'paragraph' &&
    extraLines.filter(e => e.type !== 'blank').length === 0
  ) {
    // Simple single-line item: store inline content
    content = innerDoc.children[0].content;
    blocks = null;
  } else {
    // Multi-line or nested: store blocks
    content = null;
    blocks = innerDoc.children;
  }

  return {
    node: {
      type: 'list_item', start, end: p, dirty: false,
      prefix, continuationIndent, rawFirstLine, content, blocks,
      virtualContent, lineEnding,
    },
    pos: p,
    hadBlankLine,
  };
}

function parseParagraph(src, pos) {
  const start = pos;
  let p = pos;
  const lines = [];
  // Detected from the first content line; used when joining so that multi-line
  // CRLF blocks produce rawText with \r\n between lines.  parseInlines treats \r
  // as a regular text character, so text nodes carry "line\r" and the softbreak
  // emits "\n" — together they reproduce the original \r\n on dirty-serialize.
  let contentLineEnding = '\n';

  while (p < src.length) {
    if (isBlank(src, p)) break;

    const lE = eol(src, p);
    const ln = src.slice(p, lE).replace(/\r?\n$/, '');

    // Setext heading underlines are valid only after at least one content line.
    // Must be tested before RE_THEMATIC because '---' matches both.
    if (lines.length > 0) {
      if (RE_SETEXT_H1.test(ln)) {
        const { pos: np, trailing } = consumeBlankLines(src, lE);
        const rawText = lines.join(contentLineEnding);
        const content = parseInlines(rawText, 0, rawText.length);
        return {
          node: {
            type: 'heading', start, end: np, dirty: false,
            level: 1, content, trailing,
            setextUnderline: ln, lineEnding: detectLineEnding(src, lE),
          },
          pos: np,
        };
      }
      if (RE_SETEXT_H2.test(ln)) {
        const { pos: np, trailing } = consumeBlankLines(src, lE);
        const rawText = lines.join(contentLineEnding);
        const content = parseInlines(rawText, 0, rawText.length);
        return {
          node: {
            type: 'heading', start, end: np, dirty: false,
            level: 2, content, trailing,
            setextUnderline: ln, lineEnding: detectLineEnding(src, lE),
          },
          pos: np,
        };
      }
    }

    // Stop at block-level constructs (lazy continuation rules: paragraph is greedy)
    // ATX heading, thematic break, fenced code, blockquote, list — all interrupt paragraphs.
    if (RE_ATX.test(ln)) break;
    if (RE_FENCE.test(ln)) break;
    if (RE_BQ.test(ln)) break;
    if (RE_THEMATIC.test(ln)) break;
    if (RE_BULLET.test(ln) && lines.length > 0) break; // lists don't interrupt if first line
    if (RE_ORDERED.test(ln) && lines.length > 0) break;
    // Table header (with separator on next line) interrupts
    if (looksLikeTableHeader(src, p) && lines.length > 0) break;

    if (lines.length === 0) contentLineEnding = detectLineEnding(src, lE) || '\n';
    lines.push(ln);
    p = lE;
  }

  // Capture the line ending of the last content line (preserves CRLF on single-line paragraphs).
  const lineEnding = (p > pos && src[p - 1] === '\n')
    ? (p > 1 && src[p - 2] === '\r' ? '\r\n' : '\n')
    : '';

  // Build raw paragraph text (joined with soft breaks, preserving original inter-line endings).
  const rawText = lines.join(contentLineEnding);
  const content = parseInlines(rawText, 0, rawText.length);

  const { pos: np, trailing } = consumeBlankLines(src, p);
  return {
    node: { type: 'paragraph', start, end: np, dirty: false, content, trailing, lineEnding },
    pos: np,
  };
}

// ─── inline parsing ──────────────────────────────────────────────────────────

/** Parse inline content from src[start..end). Returns array of inline nodes. */
function parseInlines(src, start, end, depth = 0) {
  // Past the inline-recursion limit: preserve all bytes as a single text node.
  if (depth >= MAX_INLINE_DEPTH && start < end) {
    boundHits.inlineDepth++;
    return [{ type: 'text', start, end, content: src.slice(start, end) }];
  }

  const nodes = [];
  let pos = start;
  let textStart = pos;

  function flushText(to) {
    if (textStart < to) {
      nodes.push({ type: 'text', start: textStart, end: to, content: src.slice(textStart, to) });
      textStart = to;
    }
  }

  while (pos < end) {
    const ch = src[pos];

    // Soft break / hard break
    if (ch === '\n') {
      // Check for hard break: two or more spaces before \n
      let spaces = 0;
      let bp = pos - 1;
      while (bp >= start && src[bp] === ' ') { spaces++; bp--; }
      if (spaces >= 2) {
        flushText(pos - spaces);
        nodes.push({ type: 'hardbreak', start: pos - spaces, end: pos + 1 });
      } else {
        flushText(pos);
        nodes.push({ type: 'softbreak', start: pos, end: pos + 1 });
      }
      pos++;
      textStart = pos;
      continue;
    }

    // Wikilink: [[name]]
    if (ch === '[' && src[pos + 1] === '[') {
      const closeIdx = src.indexOf(']]', pos + 2);
      if (closeIdx !== -1 && closeIdx < end) {
        flushText(pos);
        const name = src.slice(pos + 2, closeIdx);
        nodes.push({ type: 'wikilink', start: pos, end: closeIdx + 2, name });
        pos = closeIdx + 2;
        textStart = pos;
        continue;
      }
    }

    // Inline footnote reference: [^label]
    if (ch === '[' && pos + 1 < end && src[pos + 1] === '^') {
      const closeIdx = src.indexOf(']', pos + 2);
      if (closeIdx !== -1 && closeIdx < end) {
        flushText(pos);
        nodes.push({ type: 'footnote_ref', start: pos, end: closeIdx + 1, raw: src.slice(pos, closeIdx + 1) });
        pos = closeIdx + 1;
        textStart = pos;
        continue;
      }
    }

    // Link: [text](url) or Image: ![alt](url)
    if (ch === '!' && src[pos + 1] === '[') {
      const r = tryParseImage(src, pos, end);
      if (r) {
        flushText(pos);
        nodes.push(r.node);
        pos = r.end;
        textStart = pos;
        continue;
      }
    }
    if (ch === '[') {
      const r = tryParseLink(src, pos, end, depth);
      if (r) {
        flushText(pos);
        nodes.push(r.node);
        pos = r.end;
        textStart = pos;
        continue;
      }
    }

    // Inline code: `code` or ``code``
    if (ch === '`') {
      const r = tryParseCode(src, pos, end);
      if (r) {
        flushText(pos);
        nodes.push(r.node);
        pos = r.end;
        textStart = pos;
        continue;
      }
    }

    // HTML comment: <!-- ... -->
    if (ch === '<' && src.slice(pos, pos + 4) === '<!--') {
      const closeIdx = src.indexOf('-->', pos + 4);
      if (closeIdx !== -1 && closeIdx + 3 <= end) {
        flushText(pos);
        nodes.push({
          type: 'html_comment', start: pos, end: closeIdx + 3,
          raw: src.slice(pos, closeIdx + 3),
        });
        pos = closeIdx + 3;
        textStart = pos;
        continue;
      }
    }

    // Bold/italic: ** __ * _
    if (ch === '*' || ch === '_') {
      const r = tryParseEmphasis(src, pos, end, depth);
      if (r) {
        flushText(pos);
        nodes.push(r.node);
        pos = r.end;
        textStart = pos;
        continue;
      }
      // tryParseEmphasis failed.  If we're inside a consecutive delimiter run
      // longer than MAX_DELIMITER_SCAN, positions up to run_end-MAX_DELIMITER_SCAN
      // all fail (their scan window is entirely within the run, so findDelimiter
      // can't reach the close).  Skip past them in one O(run_length) scan rather
      // than retrying each position individually at O(MAX_DELIMITER_SCAN) each.
      if (pos + 1 < end && src[pos + 1] === ch) {
        let runEnd = pos + 2;
        while (runEnd < end && src[runEnd] === ch) runEnd++;
        const runLen = runEnd - pos;
        if (runLen > MAX_DELIMITER_SCAN) {
          pos += runLen - MAX_DELIMITER_SCAN - 1; // skip to where close is reachable
        }
      }
    }

    pos++;
  }

  flushText(end);
  return nodes;
}

function tryParseCode(src, pos, end) {
  // Count opening backticks
  let tickCount = 0;
  let p = pos;
  while (p < end && src[p] === '`') { tickCount++; p++; }
  const tick = src.slice(pos, pos + tickCount);

  // Find matching closing sequence
  let cp = p;
  while (cp <= end - tickCount) {
    if (src.slice(cp, cp + tickCount) === tick && (cp + tickCount >= end || src[cp + tickCount] !== '`')) {
      // Found closing
      let content = src.slice(p, cp);
      // Single-space strip: if content starts and ends with space and isn't all spaces
      let wasSpaceStripped = false;
      if (content.length >= 2 && content[0] === ' ' && content[content.length - 1] === ' ' &&
          content.trim() !== '') {
        content = content.slice(1, -1);
        wasSpaceStripped = true;
      }
      return {
        node: { type: 'code_span', start: pos, end: cp + tickCount, tick, content, wasSpaceStripped },
        end: cp + tickCount,
      };
    }
    cp++;
  }
  return null;
}

function tryParseLink(src, pos, end, depth = 0) {
  // [text](url) or [text](url "title")
  // pos is at '['
  const textStart = pos + 1;
  const textEnd = findMatchingBracket(src, textStart, end, '[', ']');
  if (textEnd === -1) return null;

  const p = textEnd + 1;
  if (p >= end || src[p] !== '(') return null;

  const urlEnd = findMatchingBracket(src, p + 1, end, '(', ')');
  if (urlEnd === -1) return null;

  const urlPart = src.slice(p + 1, urlEnd);
  const { url, title } = parseLinkDestination(urlPart);
  const content = parseInlines(src, textStart, textEnd, depth + 1);

  return {
    node: { type: 'link', start: pos, end: urlEnd + 1, content, url, title },
    end: urlEnd + 1,
  };
}

function tryParseImage(src, pos, end) {
  // ![alt](url)
  // pos is at '!'
  const textStart = pos + 2;
  const textEnd = findMatchingBracket(src, textStart, end, '[', ']');
  if (textEnd === -1) return null;

  const p = textEnd + 1;
  if (p >= end || src[p] !== '(') return null;

  const urlEnd = findMatchingBracket(src, p + 1, end, '(', ')');
  if (urlEnd === -1) return null;

  const urlPart = src.slice(p + 1, urlEnd);
  const { url, title } = parseLinkDestination(urlPart);
  const alt = src.slice(textStart, textEnd);

  return {
    node: { type: 'image', start: pos, end: urlEnd + 1, alt, url, title },
    end: urlEnd + 1,
  };
}

function findMatchingBracket(src, pos, end, open, close) {
  let depth = 0;
  const limit = pos + MAX_BRACKET_SCAN;
  for (let i = pos; i < end && i < limit; i++) {
    if (src[i] === open) depth++;
    else if (src[i] === close) {
      if (depth === 0) return i;
      depth--;
    }
    // Don't cross newlines for links/images (multi-line not supported)
    if (src[i] === '\n') return -1;
  }
  // Reached here without finding a closer. Count it only when the BOUND is what
  // stopped the loop, rather than the end of the input — otherwise every
  // ordinary unmatched bracket would report as a truncation.
  if (limit < end) boundHits.bracketScan++;
  return -1;
}

function parseLinkDestination(raw) {
  const trimmed = raw.trim();
  // Check for title: url "title" or url 'title'
  const titleMatch = /^(.*?)\s+"(.*)"$|^(.*?)\s+'(.*)'$/.exec(trimmed);
  if (titleMatch) {
    return {
      url: (titleMatch[1] || titleMatch[3]).trim(),
      title: titleMatch[2] || titleMatch[4],
    };
  }
  return { url: trimmed, title: null };
}

function tryParseEmphasis(src, pos, end, depth = 0) {
  const ch = src[pos];

  // For '_': do not open if the preceding character is alphanumeric.
  // This prevents word-internal underscores (make_fail_fast) from being parsed as italic.
  if (ch === '_') {
    const prevChar = pos > 0 ? src[pos - 1] : ' ';
    if (/[a-zA-Z0-9]/.test(prevChar)) return null;
  }

  // Count delimiter chars — cap at 2: only the bold (≥2) vs italic (1) distinction matters.
  let delimLen = 0;
  let p = pos;
  while (p < end && src[p] === ch && delimLen < 2) { delimLen++; p++; }

  const isBold = delimLen >= 2;
  const isItalic = delimLen === 1 || (delimLen >= 2 && delimLen % 2 !== 0);

  // Try bold first (** or __)
  if (delimLen >= 2) {
    const delim = ch.repeat(2);
    const closeIdx = findDelimiter(src, pos + 2, end, delim);
    if (closeIdx !== -1) {
      const content = parseInlines(src, pos + 2, closeIdx, depth + 1);
      return {
        node: { type: 'strong', start: pos, end: closeIdx + 2, delimiter: delim, content },
        end: closeIdx + 2,
      };
    }
  }

  // Try italic (* or _)
  if (delimLen >= 1) {
    const delim = ch;
    const closeIdx = findDelimiter(src, pos + 1, end, delim);
    if (closeIdx !== -1) {
      // Make sure we're not consuming part of a longer delimiter run
      if (closeIdx + 1 < end && src[closeIdx + 1] === ch) {
        // Don't consume: closing delimiter is actually ** or __
        return null;
      }
      const content = parseInlines(src, pos + 1, closeIdx, depth + 1);
      return {
        node: { type: 'em', start: pos, end: closeIdx + 1, delimiter: ch, content },
        end: closeIdx + 1,
      };
    }
  }

  return null;
}

/** Find a closing delimiter string, skipping code spans and respecting _ word-boundary rules. */
function findDelimiter(src, pos, end, delim) {
  const ch = delim[0];
  const len = delim.length;
  let i = pos;
  const limit = pos + MAX_DELIMITER_SCAN;

  while (i <= end - len && i < limit) {
    const c = src[i];

    // Stop at line boundaries (emphasis doesn't span lines in this parser).
    if (c === '\n') return -1;

    // Skip code spans — a `_` inside backtick-code is not a delimiter.
    if (c === '`') {
      let tLen = 0;
      while (i + tLen < end && src[i + tLen] === '`') tLen++;
      const tick = src.slice(i, i + tLen);
      // Find matching close (same number of backticks, not immediately followed by another).
      let ci = i + tLen;
      let found = false;
      while (ci + tLen - 1 < end) {
        if (src.slice(ci, ci + tLen) === tick &&
            (ci + tLen >= end || src[ci + tLen] !== '`')) {
          i = ci + tLen;
          found = true;
          break;
        }
        ci++;
      }
      if (!found) i += tLen;
      continue;
    }

    if (src.slice(i, i + len) === delim) {
      const prevCh = i > 0 ? src[i - 1] : ' ';
      const nextCh = i + len < end ? src[i + len] : ' ';

      // Right-flanking check: must not be preceded by whitespace.
      if (prevCh === ' ' || prevCh === '\t') { i++; continue; }

      // Don't match if followed by the same char (*** or ___ runs).
      if (nextCh === ch) { i++; continue; }

      // For '_': word-internal underscore is not a valid closer.
      // A run is right-flanking only when NOT followed by alphanumeric
      // OR when preceded by Unicode punctuation — simplified: skip if both
      // sides are alphanumeric (i.e., it's inside a word).
      if (ch === '_') {
        if (/[a-zA-Z0-9]/.test(nextCh)) { i++; continue; }
        if (/[a-zA-Z0-9]/.test(prevCh) && !/[a-zA-Z0-9]/.test(nextCh)) {
          // Right-flanking but also left-flanking: still valid as a closer
          // only if not preceded by alphanumeric when the opener was also
          // preceded by non-alphanumeric. Accept: the opener check handles this.
        }
      }

      return i;
    }

    i++;
  }
  // Same test as findMatchingBracket: the bound fired only if it, rather than
  // the end of the line, is what the loop ran into. A newline returns above.
  if (limit <= end - len) boundHits.delimiterScan++;
  return -1;
}

// ─── serialize ───────────────────────────────────────────────────────────────

/**
 * Serialize a document tree back to a string.
 * Nodes with dirty=false emit source.slice(node.start, node.end).
 * Nodes with dirty=true are rebuilt from their model.
 */
export function serialize(doc, source) {
  if (doc.children.length === 0) {
    // No block nodes — return the original source verbatim (handles blank-only documents).
    return source.slice(doc.start, doc.end);
  }
  let out = '';
  for (const child of doc.children) {
    out += serializeNode(child, source);
  }
  return out;
}

function serializeNode(node, source) {
  if (!node.dirty) {
    return source.slice(node.start, node.end);
  }
  return serializeDirty(node, source);
}

function serializeDirty(node, source) {
  switch (node.type) {
    case 'frontmatter':
      // Frontmatter is always a chip — never rebuilds from model.
      return source.slice(node.start, node.end);

    case 'heading': {
      const lineEnding = node.lineEnding ?? '\n';
      const text = serializeInlines(node.content, source);
      if (node.setextUnderline !== undefined) {
        return text + lineEnding + node.setextUnderline + lineEnding + node.trailing;
      }
      const prefix = '#'.repeat(node.level);
      return prefix + ' ' + text + (node.trailingSpace ?? '') + lineEnding + node.trailing;
    }

    case 'paragraph': {
      const lineEnding = node.lineEnding ?? '\n';
      const text = serializeInlines(node.content, source);
      return text + lineEnding + node.trailing;
    }

    case 'fenced_code': {
      const openLE = node.fenceOpenLineEnding ?? '\n';
      if (!node.hasClose) {
        return node.fenceOpen + openLE + node.content + node.trailing;
      }
      const closeLE = node.fenceCloseLineEnding ?? '\n';
      return node.fenceOpen + openLE + node.content + node.fenceClose + closeLE + node.trailing;
    }

    case 'blockquote': {
      let content = '';
      for (const child of node.children) {
        if (child.dirty) {
          content += serializeDirty(child, node.virtualSrc);
        } else {
          content += node.virtualSrc.slice(child.start, child.end);
        }
      }
      return prefixBlockquoteLines(content, node.blankStyle, node.contentSpace, node.leadingIndent) + node.trailing;
    }

    case 'list': {
      let out = '';
      for (let idx = 0; idx < node.items.length; idx++) {
        out += serializeDirtyListItem(node.items[idx], source, node.tight);
        // Add blank line after this item only if the original source had one there.
        const hadBlank = node.afterBlanks ? node.afterBlanks[idx] : (!node.tight && idx < node.items.length - 1);
        if (hadBlank && idx < node.items.length - 1) {
          out += '\n';
        }
      }
      return out + node.trailing;
    }

    case 'list_item': {
      // Shouldn't be called directly; called via serializeDirtyListItem
      return serializeDirtyListItem(node, source, true);
    }

    case 'table': {
      return serializeDirtyTable(node, source);
    }

    case 'thematic_break': {
      const lineEnding = node.lineEnding ?? '\n';
      return node.chars + lineEnding + node.trailing;
    }

    case 'footnote_def':
      // Chip — always emit verbatim source regardless of dirty flag.
      return source.slice(node.start, node.end);

    case 'chip': {
      return source.slice(node.start, node.end);
    }

    default: {
      // Unknown node type — fall back to source
      return source.slice(node.start, node.end);
    }
  }
}

function serializeDirtyListItem(item, source, tight) {
  let out = item.prefix;

  if (item.content !== null) {
    // Simple inline content
    const lineEnding = item.lineEnding ?? '\n';
    out += serializeInlines(item.content, item.virtualContent) + lineEnding;
  } else if (item.blocks !== null) {
    // Multi-block content. When rawFirstLine is empty, the prefix row is blank and
    // all blocks are continuation lines.
    const blockSrc = item.virtualContent;
    let blockOut = '';
    for (const block of item.blocks) {
      if (block.dirty) {
        blockOut += serializeDirty(block, blockSrc);
      } else {
        blockOut += blockSrc.slice(block.start, block.end);
      }
    }

    if (item.rawFirstLine === '') {
      // Empty first line: prefix row is just the marker, blocks all go on continuation lines.
      out += '\n';
      const lines = blockOut.split('\n');
      for (let i = 0; i < lines.length; i++) {
        if (lines[i] === '' && i === lines.length - 1) continue; // skip trailing empty
        out += (lines[i] === '' ? '' : ' '.repeat(item.continuationIndent) + lines[i]) + '\n';
      }
    } else {
      // First line doesn't need indent (the prefix already does that)
      const lines = blockOut.split('\n');
      if (lines.length > 0) {
        out += lines[0] + '\n';
        for (let i = 1; i < lines.length; i++) {
          if (lines[i] === '' && i === lines.length - 1) continue; // skip trailing empty
          out += (lines[i] === '' ? '' : ' '.repeat(item.continuationIndent) + lines[i]) + '\n';
        }
      }
    }
  } else {
    out += item.rawFirstLine + '\n';
  }

  return out;
}

function serializeDirtyTable(node, source) {
  let out = node.headerRaw + '\n';
  out += node.separatorLine + '\n';
  for (const row of node.rows) {
    out += row + '\n';
  }
  return out + node.trailing;
}

/** Add `> ` prefix to each line of a blockquote's serialized content.
 *  blankStyle: '' for `>\n`, ' ' for `> \n`.
 *  contentSpace: '' for `>text`, ' ' for `> text`.
 *  leadingIndent: spaces before `>` (e.g. '  ' for an indented blockquote). */
function prefixBlockquoteLines(text, blankStyle, contentSpace, leadingIndent) {
  if (!text) return '';
  const bSpace = blankStyle === undefined ? ' ' : blankStyle;
  const cSpace = contentSpace === undefined ? ' ' : contentSpace;
  const lead = leadingIndent || '';
  const blankMarker = lead + '>' + bSpace + '\n';
  const lines = text.split('\n');
  let out = '';
  for (let i = 0; i < lines.length; i++) {
    // The last element after split is '' when text ends with \n — skip it.
    if (i === lines.length - 1 && lines[i] === '') continue;
    if (lines[i] === '') {
      out += blankMarker;
    } else {
      out += lead + '>' + cSpace + lines[i] + '\n';
    }
  }
  return out;
}

/** Serialize an array of inline nodes to a string. */
function serializeInlines(nodes, source) {
  if (!nodes) return '';
  let out = '';
  for (const node of nodes) {
    out += serializeInline(node, source);
  }
  return out;
}

function serializeInline(node, source) {
  switch (node.type) {
    case 'text':
      return node.content;
    case 'softbreak':
      return '\n';
    case 'hardbreak':
      return '  \n';
    case 'strong':
      return node.delimiter + serializeInlines(node.content, source) + node.delimiter;
    case 'em':
      return node.delimiter + serializeInlines(node.content, source) + node.delimiter;
    case 'code_span': {
      const inner = node.wasSpaceStripped ? ' ' + node.content + ' ' : node.content;
      return node.tick + inner + node.tick;
    }
    case 'link': {
      const text = serializeInlines(node.content, source);
      const title = node.title ? ` "${node.title}"` : '';
      return `[${text}](${node.url}${title})`;
    }
    case 'image': {
      const title = node.title ? ` "${node.title}"` : '';
      return `![${node.alt}](${node.url}${title})`;
    }
    case 'wikilink':
      return `[[${node.name}]]`;
    case 'html_comment':
      return node.raw;
    case 'footnote_ref':
      return node.raw;
    default:
      // Fall back to content if present, else empty
      return node.content || '';
  }
}

// ─── markAllDirty ────────────────────────────────────────────────────────────

/** Mark every node in the tree dirty so serialize() rebuilds each one from its model. */
export function markAllDirty(doc) {
  markNodeDirty(doc);
}

function markNodeDirty(node) {
  node.dirty = true;
  if (node.children) node.children.forEach(markNodeDirty);
  if (node.items) node.items.forEach(markNodeDirty);
  if (node.content && Array.isArray(node.content)) node.content.forEach(markNodeDirty);
  if (node.blocks) node.blocks.forEach(markNodeDirty);
  // Inline content inside headings, paragraphs, table cells
  if (node.headerCells) node.headerCells.forEach(cell => cell.inlines.forEach(markNodeDirty));
  if (node.dataRows) node.dataRows.forEach(row => row.forEach(cell => cell.inlines.forEach(markNodeDirty)));
}

// ─── hunks ───────────────────────────────────────────────────────────────────

/** True when node or any descendant is dirty. */
function isEffectivelyDirty(node) {
  if (node.dirty) return true;
  if (node.children) for (const c of node.children) { if (isEffectivelyDirty(c)) return true; }
  if (node.items) for (const c of node.items) { if (isEffectivelyDirty(c)) return true; }
  if (node.blocks) for (const c of node.blocks) { if (isEffectivelyDirty(c)) return true; }
  return false;
}

/**
 * Return anchored edits for all top-level blocks that are dirty or contain dirty descendants.
 * Each hunk: { start, end, replacement } — replace source[start..end) with replacement.
 */
export function hunks(doc, source) {
  const result = [];
  for (const child of doc.children) {
    if (isEffectivelyDirty(child)) {
      result.push({
        start: child.start,
        end: child.end,
        replacement: serializeDirty(child, source),
      });
    }
  }
  return result;
}
