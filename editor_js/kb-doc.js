// kb-doc.js — Orient's glue between a knowledge document's stored file and
// md-serial's editor. ES module, no imports: the caller passes md-serial's
// `mountEditor` in, so the page (which loads it from /editor/js/) and the tests
// (which load the vendored copy from disk) drive exactly this code.
//
// The stored file is a YAML frontmatter block followed by a Markdown body. The
// member edits the frontmatter as text and the body as a rendered document, so
// the two are split on open and joined on save. `splitFrontmatter` recognises
// the block exactly as md-serial's own parser does (opening `---`, closing
// `---` or `...`), and the split is lossless: frontmatter + body is always the
// original string, byte for byte.

export function splitFrontmatter(content) {
  if (!(content.startsWith('---\n') || content.startsWith('---\r\n'))) {
    return { frontmatter: '', body: content };
  }
  let p = content.indexOf('\n') + 1;
  while (p < content.length) {
    const nl = content.indexOf('\n', p);
    const end = nl === -1 ? content.length : nl + 1;
    const line = content.slice(p, end).replace(/\r?\n$/, '');
    if (line === '---' || line === '...') {
      // The blank lines after the closing fence go with the frontmatter, as
      // md-serial's own frontmatter node takes them. Left on the body they
      // would be its leading blank lines, which md-serial does not reproduce
      // on save, so an untouched document would lose them.
      let q = end;
      while (q < content.length) {
        const n = content.indexOf('\n', q);
        const e = n === -1 ? content.length : n + 1;
        if (!/^[ \t\r\n]*$/.test(content.slice(q, e))) break;
        q = e;
      }
      return { frontmatter: content.slice(0, q), body: content.slice(q) };
    }
    p = end;
  }
  // An opening fence with no close is not a frontmatter block to md-serial
  // either; the whole file is body, and the save route refuses it.
  return { frontmatter: '', body: content };
}

// Mount md-serial over `body` in `root`. `revision` carries the change counter
// across a remount (md-serial's own contract: a remount is a fresh mount, and a
// consumer that does not seed it reads 0 against a baseline taken before the
// edits).
export function mountBody(mountEditor, root, body, opts = {}) {
  const o = {};
  if (opts.revision != null) o.revision = opts.revision;
  if (opts.document) o.document = opts.document;
  return mountEditor(root, body, o);
}

// The file as it stands now: the frontmatter text joined to the editor's body.
export function draft(frontmatter, editor) {
  return frontmatter + editor.save();
}

// The file to commit, plus md-serial's own check that the Markdown it produced
// reparses to what the member was looking at. A save is refused when that check
// fails: md-serial has lost content silently on regeneration before
// (issue_01M33T2TS7EEVB3935HQCB1Y4J, issue_01M33WT8QY99XF481S0EAZJBP4), and a
// paragraph-merge case is still open (sig_01M3V19ATQC4JQX3QXRHMG9F7P).
export function checkedDraft(frontmatter, editor) {
  const { source, roundTrip } = editor.saveChecked();
  return { draft: frontmatter + source, ok: roundTrip.ok, failures: roundTrip.failures };
}

// The toolbar's commands, by the name its buttons carry in `data-kb-cmd`. Each
// returns whether it changed the document, as md-serial's commands do.
export const COMMANDS = {
  bold: (ed) => ed.toggleBold(),
  italic: (ed) => ed.toggleItalic(),
  heading: (ed) => ed.toggleHeading(2),
  bullets: (ed) => ed.toggleBulletList(),
  numbers: (ed) => ed.toggleOrderedList(),
  code: (ed) => ed.toggleCodeBlock(''),
  link: (ed, href) => ed.insertLink(href),
};
