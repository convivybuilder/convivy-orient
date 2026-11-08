# The editor's round-trip corpus

Real knowledge documents, copied from `convivy/knowledge` at commit
`d31622cbaeb4fb0e637b61beffbf05842b83e56d` under the same paths below
`projects/`. They were chosen to cover every construct the knowledge base uses:
tables, nested and ordered lists, code fences, wikilinks, HTML blocks and
blockquotes. `synthetic/footnotes.md` is the one constructed file, because no
knowledge document defines a footnote.

Three of the `platform/decisions` documents (0040, 0044, 0093) have had real
Convivy crew/product names replaced with the 0.7.0 name gate's fictional
names (`alpha`, `delta` and so on), so they are no longer
byte-identical to the commit above; every other construct the round-trip test
checks is untouched.

`tests/test_kb_editor_md_serial.py` opens each one in the editor's code path
(`editor_js/kb-doc.js` over the vendored md-serial) and asserts that saving with
no change returns the file byte for byte. When `$HOME/Convivy/knowledge`
is present, the same test also runs over its whole `projects/` tree.

This README is not a corpus document; the test skips it by name.
