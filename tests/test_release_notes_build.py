"""Render tests for /products/release-notes (ARCH-360 follow-up, item 5b).

Load-bearing: only kind=="release_note" signals render, newest-first, the version
(signal stream) shows, the Markdown body renders safely (an XSS probe is escaped),
chip-filterable by product, the authoring form + its __CAN_CAPTURE__ gate, and the
route + nav registration.
"""
from __future__ import annotations

import datetime as dt

import pytest

import control_db
import library_fake
import release_notes_build as rn


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    # release_notes_build now reads through library_client rather than
    # control_db directly; this fake serves the Library's exact response shape
    # from the same temp DB every test already seeds (library_fake's docstring).
    library_fake.patch(monkeypatch)
    yield


def _mk(**kw):
    base = dict(id=control_db.new_signal_id(), source="release-notes", kind="release_note",
                stream="v1.0.0", actor="operator@example.com", ingested_by="operator@example.com",
                product="vivy", body="## What's new\n- A thing shipped",
                privacy_class="internal", produced_at=dt.datetime(2026, 7, 17))
    base.update(kw)
    return base


def _seed(*sigs):
    for sig in sigs:
        library_fake.seed_signal(**sig)


def test_only_release_note_kind_renders(db):
    _seed(_mk(body="KEEP-THIS release note"),
          dict(id=control_db.new_signal_id(), source="github", kind="changelog",
              actor="x", ingested_by="cap", product="vivy", stream="vivy",
              body="NOT-A-RELEASE-NOTE", privacy_class="internal"))
    html = rn.live_html()
    assert "KEEP-THIS" in html
    assert "NOT-A-RELEASE-NOTE" not in html


def test_version_and_markdown_body_render(db):
    _seed(_mk(stream="v2.3.1", body="## Highlights\n- **Bold** win\n- a `code` bit"))
    html = rn.live_html()
    assert "v2.3.1" in html
    assert "<h2>Highlights</h2>" in html
    assert "<strong>Bold</strong>" in html and "<code>code</code>" in html


def test_newest_first(db):
    _seed(_mk(stream="OLD", produced_at=dt.datetime(2026, 7, 1)),
          _mk(stream="NEW", produced_at=dt.datetime(2026, 7, 15)))
    html = rn.live_html()
    assert html.index("NEW") < html.index("OLD")


def test_body_is_xss_safe(db):
    _seed(_mk(body="<script>alert(1)</script> <img src=x onerror=alert(1)>"))
    html = rn.live_html()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html and "&lt;img" in html


def test_chip_filterable_by_product(db):
    _seed(_mk(product="vivy", body="VIVY-NOTE"),
          _mk(product="constance", body="CONSTANCE-NOTE"))
    html = rn.live_html()
    assert 'data-prod="vivy"' in html and 'data-prod="constance"' in html
    assert "VIVY-NOTE" in html and "CONSTANCE-NOTE" in html


def test_authoring_form_and_capture_gate(db):
    html = rn.live_html()
    assert "Write a release note" in html
    assert 'data-can-capture="__CAN_CAPTURE__"' in html  # server substitutes the role gate
    assert "release_note" in html  # the JS posts kind=release_note


def test_form_strips_tier_suffix_from_actor(db):
    # __WHOAMI__ substitutes to "<email> · <tier> view"; the form must store only the
    # email as the actor, not the whole UI-chrome string, and must not hardcode
    # actor:'__WHOAMI__' directly (the #82 review finding).
    html = rn.live_html()
    assert "'__WHOAMI__'.split(' · ')[0]" in html
    assert "actor:'__WHOAMI__'" not in html and 'actor:"__WHOAMI__"' not in html
    assert "dedup_key:'release_note:'" in html  # double-submit / duplicate-version guard


def test_empty_state(db):
    assert "No release notes yet" in rn.live_html()


def test_route_registered_full_tier():
    """/products/release-notes moved out of `_full_only_routes()` into
    `server.SCOPED_ROUTES` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): that
    dispatcher calls its renderer with no arguments, and this page now needs
    the viewer's scope. `tests/test_server.py`'s
    `_MOVED_OUT_OF_FULL_ONLY_ROUTES` carries the full-tier/nav/contractor-leak
    coverage this move must not lose."""
    import server
    assert "/products/release-notes" in server.SCOPED_ROUTES
    assert "/products/release-notes" not in server._full_only_routes()


def test_nav_places_release_notes_in_chart_house():
    # Products dissolved into the Chart House subnav (nav-reshape epic
    # issue_01M0TKX6DW2E1TXGN9QT6K61J5 step 2) — it held exactly this one page.
    import design_system as ds
    html = ds.nav_component("/products/release-notes", "full")
    assert 'href="/products/release-notes"' in html
    assert "Chart House" in html and "Release Notes" in html
    assert "Products" not in html


# --- build_static: the static fallback must never carry real notes ---------


def test_build_static_seeds_an_empty_fallback_not_real_data(tmp_path, monkeypatch, db):
    """products-release-notes.html is served to ANY viewer's failed render,
    scoped or not (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): a snapshot
    naming a real note would leak to a scoped viewer. `build_static` never
    calls the live renderer at all now — the same zero-argument leak 26bb079
    found and fixed in `archives_build.build_static()`."""
    _seed(_mk(body="REAL-RELEASE-NOTE"))
    monkeypatch.setattr(rn, "SITE", tmp_path)
    rn.build_static()
    html = (tmp_path / "products-release-notes.html").read_text(encoding="utf-8")
    assert "REAL-RELEASE-NOTE" not in html
