"""Render tests for /engineering/changelog (ARCH-359, ARCH-411).

Load-bearing: only kind=="changelog" signals render, only AUTHORED ones
(source=co) and never the git-derived merge rows, newest-first by produced_at,
chip-filterable by product/stream, the entry body renders safely (an XSS probe is
escaped, not injected), and the empty state.
"""
from __future__ import annotations

import pytest

import control_db
import library_fake
import changelog_build as c


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    # changelog_build now reads through library_client rather than control_db
    # directly; this fake serves the Library's exact response shape from the
    # same temp DB every test already seeds (library_fake's docstring).
    library_fake.patch(monkeypatch)
    yield


def _mk(**kw):
    """An AUTHORED entry (source=co) — what the page renders. The git-derived
    merge rows use source=github and are covered by their own test below."""
    base = dict(id=control_db.new_signal_id(), source="co", kind="changelog",
                stream="vivy", actor="vivy-co", ingested_by="agent-platform",
                product="vivy", body="**Merged:** a thing that does the thing. #1",
                refs={"pr": 1, "url": "https://github.com/convivy/vivy/pull/1",
                      "merged_at": "2026-07-15T00:00:00Z", "author": "operator-gh"},
                privacy_class="internal", produced_at=None)
    base.update(kw)
    return base


def _seed(*sigs):
    for sig in sigs:
        library_fake.seed_signal(**sig)


def test_only_changelog_kind_renders(db):
    _seed(_mk(body="keep this. #1"),
          dict(id=control_db.new_signal_id(), source="slack", kind="notification",
              actor="platform", ingested_by="agent-platform", product="vivy",
              stream="cost", body="NOT-A-CHANGELOG-ENTRY", privacy_class="internal"))
    html = c.changelog_live_html()
    assert "keep this" in html
    assert "NOT-A-CHANGELOG-ENTRY" not in html


def test_the_git_derived_merge_rows_do_not_render(db):
    """ARCH-411, the whole point. github_capture writes a `changelog` signal per
    merge whose body is the PR title + PR description — written for a reviewer,
    unusable read back as a changelog. Those rows stay in the store because they
    are how changelog-drain detects a merge at all; they are not the changelog."""
    _seed(_mk(source="co", body="**Merged:** the authored entry. #1"),
          _mk(source="github", actor="operator-gh",
              body="feat: raw pr title\n\n## Summary\n\nRAW-PR-BODY-TEXT"))
    html = c.changelog_live_html()
    assert "the authored entry" in html
    assert "RAW-PR-BODY-TEXT" not in html
    assert "raw pr title" not in html


def test_newest_first_by_produced_at_ordering(db):
    import datetime as dt
    _seed(_mk(body="OLDER-ENTRY #1", produced_at=dt.datetime(2026, 7, 1)),
          _mk(body="NEWER-ENTRY #2", produced_at=dt.datetime(2026, 7, 15)))
    html = c.changelog_live_html()
    assert html.index("NEWER-ENTRY") < html.index("OLDER-ENTRY")


def test_chip_filterable_by_product(db):
    _seed(_mk(product="vivy", stream="vivy", body="VIVY-ENTRY #1"),
          _mk(product="constance", stream="constance", body="CONSTANCE-ENTRY #2"))
    html = c.changelog_live_html()
    assert 'data-stream="vivy"' in html and 'data-stream="constance"' in html
    assert "VIVY-ENTRY" in html and "CONSTANCE-ENTRY" in html


def test_the_whole_entry_renders_as_markdown(db):
    """An authored entry is prose, not `title\\n\\ndescription`. The old card split
    on the first blank line and escaped the head as a plain-text title, which
    would show a one-paragraph entry's `**` markers literally."""
    _seed(_mk(body="**Merged:** the widget now does the *small thing* too. #1\n\n"
                   "- point one\n- point two"))
    html = c.changelog_live_html()
    assert "<strong>Merged:</strong>" in html     # bold, not literal asterisks
    assert "<em>small thing</em>" in html
    assert "<li>point one</li>" in html
    assert "**Merged:**" not in html


def test_body_is_escaped_xss_safe(db):
    _seed(_mk(body="<img src=x onerror=alert(1)> and <script>alert(2)</script> here #1"))
    html = c.changelog_live_html()
    assert "<img src=x" not in html
    assert "<script>alert(2)</script>" not in html
    assert "&lt;img src=x" in html
    assert "&lt;script&gt;" in html


def test_pr_link_rendered(db):
    _seed(_mk(refs={"pr": 99, "url": "https://github.com/convivy/vivy/pull/99",
                    "merged_at": "2026-07-15T00:00:00Z", "author": "operator-gh"}))
    html = c.changelog_live_html()
    assert '<a href="https://github.com/convivy/vivy/pull/99"' in html
    assert "PR #99" in html


def test_pr_link_rejects_non_http_scheme(db):
    _seed(_mk(refs={"pr": 1, "url": "javascript:alert(1)", "merged_at": "2026-07-15T00:00:00Z",
                    "author": "u"}))
    html = c.changelog_live_html()
    assert "javascript:" not in html


def test_pr_link_rejects_the_control_character_bypass(db):
    """The bypass the WEAK check actually fails on, which the scheme test above
    does not reach.

    `"http://\\njavascript:alert(1)"` passes `str.startswith("http://")`, and
    `html.escape` does not encode a raw newline, so under the old check it landed
    in the rendered `href` verbatim. A test that only tries a bare `javascript:`
    URL passes against the weak form and the safe one alike, which is why this
    site kept its bypass through two rounds of "fixing" it (found in review of
    PR #170)."""
    _seed(_mk(refs={"pr": 1, "url": "http://\njavascript:alert(1)",
                    "merged_at": "2026-07-15T00:00:00Z", "author": "u"}))
    html = c.changelog_live_html()
    assert "javascript:" not in html
    assert 'href="http://' not in html, "the bypassing URL reached the href"


def test_empty_state(db):
    assert "No entries written yet" in c.changelog_live_html()


def test_empty_state_when_only_git_derived_rows_exist(db):
    """The page must say it is empty rather than render the raw merge rows — the
    exact regression this change exists to prevent."""
    _seed(_mk(source="github", body="feat: raw\n\nRAW-PR-BODY-TEXT"))
    html = c.changelog_live_html()
    assert "No entries written yet" in html
    assert "RAW-PR-BODY-TEXT" not in html


def test_route_registered_full_tier():
    """/engineering/changelog moved out of `_full_only_routes()` into
    `server.SCOPED_ROUTES` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): that
    dispatcher calls its renderer with no arguments, and this page now needs
    the viewer's scope. `tests/test_server.py`'s
    `_MOVED_OUT_OF_FULL_ONLY_ROUTES` carries the full-tier/nav/contractor-leak
    coverage this move must not lose."""
    import server
    assert "/engineering/changelog" in server.SCOPED_ROUTES
    assert "/engineering/changelog" not in server._full_only_routes()


def test_nav_places_changelog_in_engineering():
    # Was asserted against nav.py, a stale unrendered copy; now against the nav
    # every page renders. Absorbs the old design_system-side test below it, which
    # this now supersedes. Roster moved to Crews' sub-nav
    # (issue_01M08YM8MTNSTAEG1NV7E2GYRV, 2026-08-21) — merge-queue is now the
    # sibling that pins "still in the Chart House sub-nav".
    import design_system as ds
    eng = ds.nav_component("/engineering/changelog", "full")
    assert 'href="/engineering/changelog" aria-current="page"' in eng and 'href="/engineering/merge-queue"' in eng


# --- build_static: the static fallback must never carry real entries -------


def test_build_static_seeds_an_empty_fallback_not_real_data(tmp_path, monkeypatch, db):
    """engineering-changelog.html is served to ANY viewer's failed render,
    scoped or not (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): a snapshot
    naming a real entry would leak to a scoped viewer. `build_static` never
    calls the live renderer at all now — the same zero-argument leak 26bb079
    found and fixed in `archives_build.build_static()`."""
    _seed(_mk(body="REAL-CHANGELOG-ENTRY"))
    monkeypatch.setattr(c, "SITE", tmp_path)
    c.build_static()
    html = (tmp_path / "engineering-changelog.html").read_text(encoding="utf-8")
    assert "REAL-CHANGELOG-ENTRY" not in html
    assert "Live view unavailable" in html
