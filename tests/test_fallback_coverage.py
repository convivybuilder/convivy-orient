"""Every static fallback server.py's page map names must have a writer.

issue_01M2KVHTVFF4MAQF94M2SP6TZ0: `/crews` named `crews.html` as its fallback
and nothing ever wrote it, so the first live-render failure (a Library
timeout) was an unrecoverable 500 — "crews.html missing — run its build" —
rather than a stale page. `/chart-house` had the identical gap, found by this
same sweep. A route whose fallback filename appears only in server.py's own
table, never as a write target in any builder, is dead on arrival: this test
enumerates the table against every builder's write targets so the next such
gap fails here, at test time, rather than during a live Library stall.

Deliberately static (source-scanning, no Library reads, no built site/
directory required): the whole point of the bug this guards is that the
mechanism failed on a Library stall, so the guard must not itself depend on
the Library or on a machine that happens to have already run serve.sh.
"""
from __future__ import annotations

import re
from pathlib import Path

import server as srv

REPO_ROOT = Path(srv.__file__).resolve().parent

# Filenames handled outside `_full_only_routes()` because their dispatch needs
# request-scoped arguments (server.py:673, 682, 729-747) — see that file's own
# comments on why each is pulled out of the map.
_EXTRA_FALLBACK_FILENAMES = {"home.full.html", "home.contractor.html", "corrections.html"}

# home_build.py writes `f"home.{tier}.html"` (a computed name, not a literal
# string) for both tiers in one loop — the one fallback filename in the whole
# set that a literal-string scan can't see. Confirmed once, here, rather than
# exempted silently: if this template ever stops appearing, the exemption
# below is no longer justified and should be revisited.
_HOME_BUILD = (REPO_ROOT / "home_build.py").read_text()
assert 'f"home.{tier}.html"' in _HOME_BUILD, (
    "home_build.py no longer writes the f'home.{tier}.html' template this "
    "test exempts from the literal-string scan below — update the scan "
    "instead of leaving home.*.html unchecked"
)
_KNOWN_WRITERS_NOT_LITERAL_SCANNED = {"home.full.html", "home.contractor.html"}

# A builder writes its output as `(OUT / "name.html")`, `(SITE / "name.html")`,
# `(out / "name.html")` (activity_build.py's own lowercase local), or via a
# name driven by a `("name.html", live_html_fn)` table walked in a loop
# (archives_build.py, ops_reports_build.py) — four spellings, no single
# adjacency pattern covers all of them. What every one of the 27 real
# fallbacks shares, and what crews.html/chart-house.html shared with nothing:
# the filename appears as a quoted literal SOMEWHERE in a builder module.
# A false negative here (a filename quoted for an unrelated reason, in a
# module that never actually writes it) would need a *specific* builder to
# quote a *specific* other route's exact filename by coincidence — cheap to
# fix if it ever happens, and far less likely than another silent gap.
def _quoted(name: str) -> re.Pattern[str]:
    return re.compile(r'["\']' + re.escape(name) + r'["\']')


def _fallback_filenames_from_route_map() -> set[str]:
    names = {page for page, _renderer in srv._full_only_routes().values()}
    names |= _EXTRA_FALLBACK_FILENAMES
    assert names, "no fallback filenames found — _full_only_routes() moved or emptied"
    return names


def _builder_source() -> str:
    paths = sorted(REPO_ROOT.glob("*_build.py")) + [REPO_ROOT / "build.py"]
    return "\n".join(p.read_text() for p in paths)


def test_every_fallback_filename_has_a_writer():
    required = _fallback_filenames_from_route_map()
    source = _builder_source()
    missing = sorted(
        name for name in required
        if name not in _KNOWN_WRITERS_NOT_LITERAL_SCANNED and not _quoted(name).search(source)
    )
    assert not missing, (
        "these routes name a static fallback that no builder writes — the "
        "crews.html-shaped bug (issue_01M2KVHTVFF4MAQF94M2SP6TZ0): a live-"
        "render failure for them is an unrecoverable 500, not a stale page: "
        + ", ".join(missing)
    )
