"""Orient holds no handle on the knowledge index.

The closing proof of `issue_01M08YBCDTEFJ6N9S798ERF89P` — the epic that moved
every Reference Room read onto the Library. It exists so the property survives
the epic: a future module that reaches for `~/.knowledge-index/index.db`
directly fails here rather than in production on a machine that has no index.

**The epic's own definition of done asked for a different check, and that check
cannot work on this machine.** It said to move the index aside and render every
knowledge surface. Tried on 2026-09-11: every surface failed with HTTP 503 —
because the LIBRARY serves the corpus from that same file, so moving it aside
breaks the thing Orient reads through rather than testing whether Orient reads
the file itself. The two are only separable when the Library lives on another
machine, which is the deployment the epic exists to enable and not one that can
be stood up in a test.

So the property is asserted directly instead: render everything with any open
against the knowledge index raising. That has distinguishable pass and fail
states, which the file-move version did not.
"""
from __future__ import annotations

import pathlib
import sqlite3

import build
import engineering_build
import kb_index
import library_fake
import ops_reports_build
import project_build

_KB_DIR = str(pathlib.Path.home() / ".knowledge-index")

# Directories the path sweep below does not govern. Same shape as
# `test_url_safety.py`'s `_SWEEP_SKIP`, and named rather than implied so a
# future reader can see what is NOT covered.
_SWEEP_SKIP = {"tests", "alembic", "vendor", "__pycache__", ".venv", "venv",
               "site", ".git", ".ruff_cache", ".pytest_cache", ".mypy_cache"}


def test_no_knowledge_surface_opens_the_index(monkeypatch):
    real_connect = sqlite3.connect
    opened: list[str] = []

    def guard(target, *a, **k):
        if _KB_DIR in str(target):
            opened.append(str(target))
            raise AssertionError(f"opened the knowledge index directly: {target}")
        return real_connect(target, *a, **k)

    monkeypatch.setattr(sqlite3, "connect", guard)

    # The Library is faked so this runs hermetically — a live call needs a
    # credential the test environment deliberately withholds. **The fake does
    # not weaken what is under test:** it replaces `library_client`'s functions,
    # so a module that reached past them to a sqlite file would still hit the
    # guard. What is asserted is which FILES a render opens, not where its rows
    # come from.
    library_fake.patch(monkeypatch)
    for kind in ("decision", "brief", "meeting-notes", "bead"):
        library_fake.seed_reference_doc(
            id=f"vivy/{kind}/x", title="T", kind=kind, project="vivy",
            body="A line.\n", audience=["team"])

    build.load_docs()
    engineering_build.load_docs()
    ops_reports_build._load_kb_docs(tuple(ops_reports_build._MEMORY_KIND_LABEL))
    kb_index.team_docs()
    project_build._bead_doc("vivy")

    assert opened == [], f"a knowledge surface still opens the index: {opened}"


def test_no_module_defines_a_knowledge_index_path():
    """The other half: no constant pointing at the index, and no
    `ORIENT_KB_INDEX` override, anywhere outside tests.

    This is the epic's `grep` written down, so it runs rather than being
    remembered. A path constant with no reader today is how the last four slices
    each kept a dead seam alive for the next one.

    **Recursive, with an explicit skip set** — matching `test_url_safety.py`'s
    sweep, which exists for the same reason. The first version walked
    `glob("*.py")`, top-level only, while its docstring claimed "no module
    outside tests, anywhere": a reader reintroduced inside a package directory
    would have evaded it while the test stayed green. Both reviewers caught the
    mismatch between the claim and the coverage (PR #281).

    `tests` is skipped because test prose legitimately quotes the old path while
    explaining what moved; `alembic` and `vendor` are not Orient's code to
    govern.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for py in sorted(root.rglob("*.py")):
        if _SWEEP_SKIP & set(py.relative_to(root).parts):
            continue
        text = py.read_text(errors="ignore")
        # `index.db` and the env override, which is the epic's own grep — NOT
        # the whole `.knowledge-index` directory. `home_build` and `kb_build`
        # legitimately read `kb-queries.jsonl` and `kb-materializations.jsonl`
        # from it: those are query logs the Library does not serve, and the
        # epic's thread records them as explicitly out of scope. A broader
        # marker flags them and reads as a miss.
        for marker in ("index.db", "ORIENT_KB_INDEX"):
            if marker in text:
                offenders.append(f"{py.name}: {marker}")
    assert offenders == [], f"a knowledge-index path survives: {offenders}"
