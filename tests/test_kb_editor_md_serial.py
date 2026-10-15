"""The knowledge-base editor over md-serial (issue_01M35KC8PZMZBMJKX4RZR9F1Y8).

These drive Orient's glue, `editor_js/kb-doc.js`, over the vendored md-serial on
md-serial's own DOM stub, through `tests/md_serial_stub/kb_doc_harness.mjs`. The
glue is the code the page runs: the controller in `kb_editor.py` imports the same
file from `/editor/js/kb-doc.js`. Two definition-of-done clauses of the editor
epic (issue_01M17HYDBQ4GPWHPFS56M6QQPH) are asserted here:

- a formatting change made through the editor's commands, with no Markdown
  typed, changes the stored Markdown correspondingly, and a table's cell is
  edited as rendered (knowledge#883) the same way;
- a document opened and saved with no change comes back byte-identical,
  frontmatter included, over a corpus of real documents.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "md_serial_stub" / "kb_doc_harness.mjs"
CORPUS = REPO / "tests" / "fixtures" / "kb_corpus"
KNOWLEDGE_PROJECTS = Path.home() / "Convivy" / "knowledge" / "projects"


def _node(*args: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to run md-serial")
    r = subprocess.run([node, str(HARNESS), *args], capture_output=True, text=True,
                       timeout=300, check=False)
    assert r.returncode == 0, f"harness failed:\n{r.stderr}"
    return r.stdout


def _corpus(root: Path) -> list[dict]:
    return [json.loads(line) for line in _node("corpus", str(root)).splitlines() if line.strip()]


def test_formatting_through_the_editor_changes_the_stored_markdown():
    """Heading, list item, emphasis and link, each applied through the command
    the toolbar dispatches to, on a selection, with no Markdown typed."""
    got = json.loads(_node("format"))
    assert all(got["changed"].values()), got["changed"]
    assert got["revisions"] == sorted(set(got["revisions"])), (
        "every command must advance revision(), the signal a remount carries")
    assert got["round_trip_ok"], got["failures"]
    body = got["draft"][len(got["frontmatter"]):]
    assert got["draft"].startswith(got["frontmatter"]), "the frontmatter is untouched"
    assert body == ("## Heading here\n\n"
                    "- Item here\n\n"
                    "**Bold** and *italic* words\n\n"
                    "[Link text](https://example.com/x) here\n")
    assert got["draft"] != got["original"]


def test_a_table_cell_edited_as_rendered_changes_the_stored_markdown():
    """The epic's clause names tables beside headings, lists, emphasis, links
    and code blocks. md-serial gives each cell of a top-level table its own
    editable island (knowledge#883); typing into one rewrites only the edited
    row (knowledge#895), keeping the separator line and every other row's
    source spelling, escaped pipes included, and leaving the frontmatter and
    the paragraphs around it byte for byte. The fixture's header and second
    row are spaced unlike md-serial's own output, so a regenerated row would
    show here."""
    got = json.loads(_node("table"))
    assert got["tag"] == "TABLE"
    assert (got["th_editable"], got["td_editable"]) == ("true", "true")
    assert got["revision_advanced"], "a cell edit must advance revision(), the unsaved-changes signal"
    assert got["round_trip_ok"], got["failures"]
    assert got["draft"].startswith(got["frontmatter"]), "the frontmatter is untouched"
    assert got["draft"] == got["original"].replace(
        "| alpha | 1 | `a\\|b` |\n", "| beta | 1 | `a\\|b` |\n")
    assert got["draft"][len(got["frontmatter"]):] == (
        "Before\n\n"
        "|Name|Count|Note|\n"
        "|:---|:---:|---:|\n"
        "| beta | 1 | `a\\|b` |\n"
        "|gamma|  2 |`c\\|d`|\n\n"
        "After\n")


def test_every_corpus_document_saves_unchanged_byte_for_byte():
    """The bullet that decides whether the editor is safe to ship: a formatter
    that rewrites what it did not understand corrupts documents nobody was
    editing. The corpus covers tables, nested and ordered lists, code fences,
    wikilinks, HTML blocks, blockquotes and footnotes (README.md beside it)."""
    rows = _corpus(CORPUS)
    names = {r["path"] for r in rows}
    assert len(rows) == 11, names
    assert "platform/contracts/session-page-markup.md" in names, "the HTML-block document"
    assert "synthetic/footnotes.md" in names
    bad = [r for r in rows if r.get("error") or not (
        r["split_lossless"] and r["has_frontmatter"] and r["identical"]
        and r["checked_identical"] and r["round_trip_ok"])]
    assert bad == [], bad


def test_the_round_trip_check_sees_a_document_that_does_not_survive(tmp_path):
    """The positive control for the corpus test: a harness that reported every
    document identical regardless would pass it just as well. md-serial drops a
    body's leading blank lines, so a file whose blank lines sit before the body
    with no frontmatter block to carry them must come back changed."""
    (tmp_path / "leading-blank.md").write_text("\n\nA paragraph.\n")
    rows = _corpus(tmp_path)
    assert len(rows) == 1
    assert rows[0]["identical"] is False


def test_the_whole_knowledge_tree_saves_unchanged_when_it_is_on_this_machine():
    """The same check over every document, where the knowledge checkout exists.
    CI has no clone of that repository, so there the vendored corpus above is
    the check."""
    if not KNOWLEDGE_PROJECTS.is_dir():
        pytest.skip(f"{KNOWLEDGE_PROJECTS} is not present")
    rows = _corpus(KNOWLEDGE_PROJECTS)
    assert len(rows) > 500
    bad = [r["path"] for r in rows if r.get("error") or not (
        r["split_lossless"] and r["identical"] and r["checked_identical"] and r["round_trip_ok"])]
    assert bad == [], f"{len(bad)} of {len(rows)} changed: {bad[:10]}"


def test_the_vendored_md_serial_names_the_commit_it_was_copied_from():
    source = (REPO / "vendor" / "md-serial" / "SOURCE.md").read_text()
    assert "a2dfdd200fbf7ba2667a65e27e71f220b44ad107" in source
    for name in ("md-serial.js", "render.js", "editor.js"):
        assert (REPO / "vendor" / "md-serial" / name).is_file()
