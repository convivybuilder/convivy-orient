// kb_doc_harness.mjs — drives editor_js/kb-doc.js over the vendored md-serial,
// on md-serial's own DOM stub, for tests/test_kb_editor_md_serial.py.
//
//   node kb_doc_harness.mjs corpus <dir>   one JSON line per .md file under dir
//   node kb_doc_harness.mjs format         one JSON object for the formatting run
//
// Paths are resolved from this file, so the test needs no install step.
import { readFileSync, readdirSync } from 'node:fs';
import { join, dirname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, '..', '..');
const { mountEditor } = await import(join(repo, 'vendor', 'md-serial', 'editor.js'));
const { makeDOMStub, selectRange } = await import(join(here, 'test-helpers.js'));
const kb = await import(join(repo, 'editor_js', 'kb-doc.js'));

function open(content) {
  const stub = makeDOMStub();
  const root = stub.createElement('div');
  const { frontmatter, body } = kb.splitFrontmatter(content);
  const ed = kb.mountBody(mountEditor, root, body, { document: stub });
  return { stub, root, frontmatter, ed };
}

function findDocs(dir) {
  const out = [];
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) out.push(...findDocs(p));
    else if (e.name.endsWith('.md') && e.name !== 'README.md') out.push(p);
  }
  return out.sort();
}

function corpus(dir) {
  for (const path of findDocs(dir)) {
    const content = readFileSync(path, 'utf8');
    const row = { path: relative(dir, path) };
    try {
      const { frontmatter, ed } = open(content);
      const checked = kb.checkedDraft(frontmatter, ed);
      row.split_lossless = frontmatter + kb.splitFrontmatter(content).body === content;
      row.has_frontmatter = frontmatter !== '';
      row.identical = kb.draft(frontmatter, ed) === content;
      row.checked_identical = checked.draft === content;
      row.round_trip_ok = checked.ok;
      row.failures = checked.failures.slice(0, 3);
    } catch (e) {
      row.error = String(e && e.message || e);
    }
    console.log(JSON.stringify(row));
  }
}

// Each command applied through kb-doc.js's COMMANDS table, the one the toolbar
// buttons dispatch through, with a selection set the way a reader's would be.
function format() {
  const fm = '---\nid: synthetic/format\nkind: reference\ntitle: Formatting\nstatus: current\n---\n\n';
  const content = fm + 'Heading here\n\nItem here\n\nBold and italic words\n\nLink text here\n';
  const { stub, root, frontmatter, ed } = open(content);
  // mountEditor renders the document into one wrapper element inside root.
  const blocks = () => Array.from(root.children[0].children);
  const text = (el) => Array.from(el.childNodes)[0];
  const sel = (el, a, b) => { const t = text(el); selectRange(stub, t, a, t, b); };
  const changed = {};
  const revisions = [ed.revision()];

  sel(blocks()[0], 0, 4); changed.heading = kb.COMMANDS.heading(ed); revisions.push(ed.revision());
  sel(blocks()[1], 0, 4); changed.bullets = kb.COMMANDS.bullets(ed); revisions.push(ed.revision());
  sel(blocks()[2], 0, 4); changed.bold = kb.COMMANDS.bold(ed); revisions.push(ed.revision());
  const p3 = blocks()[2];
  const after = Array.from(p3.childNodes)[1];
  selectRange(stub, after, 5, after, 11); changed.italic = kb.COMMANDS.italic(ed); revisions.push(ed.revision());
  sel(blocks()[3], 0, 9); changed.link = kb.COMMANDS.link(ed, 'https://example.com/x'); revisions.push(ed.revision());

  const checked = kb.checkedDraft(frontmatter, ed);
  console.log(JSON.stringify({
    original: content, frontmatter, draft: checked.draft, round_trip_ok: checked.ok,
    failures: checked.failures, changed, revisions,
  }));
}

const [mode, arg] = process.argv.slice(2);
if (mode === 'corpus') corpus(arg);
else if (mode === 'format') format();
else { console.error('usage: kb_doc_harness.mjs corpus <dir> | format'); process.exit(2); }
