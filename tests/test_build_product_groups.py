"""Tests for the Orientation Products-tab grouping + browse exclusion (ARCH-308).

The taste-map duplicate is removed by keeping it out of the by-project browse, and
the mobile apps render under a "Mobile App Suite" heading. The security-relevant
bit is the tier gate: a suite member with no docs is a full-tier-only pill, never
shipped in the contractor bundle (an empty pill would leak an unreleased product's
name — ARCH-228 default-deny).
"""
import json
import re

import pytest

import build


def _doc(id_, project, kind="reference", title="T", contractor=False):
    return {"id": id_, "project": project, "kind": kind, "title": title,
            "status": "current", "body": "b\n", "contractor": contractor}


def _payload(docs, tier):
    html = build.render(docs, tier)
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.DOTALL)
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_taste_map_is_browsable_again():
    """ARCH-366. ARCH-308 excluded the taste-map from browse because its
    "canonical home is Reference Docs"; ARCH-357 then retired Reference Docs,
    so the doc was excluded from browse AND unreachable at its old route — the
    exclusion outlived the home that justified it. Inverted rather than deleted,
    because the property worth pinning is that the doc IS reachable."""
    assert "company/reference/taste-map" not in build.BROWSE_EXCLUDE_IDS


def test_browse_exclusions_name_a_home_that_resolves():
    """Any future browse exclusion must not repeat ARCH-308's failure. An
    excluded doc is invisible in browse, so it needs some other way to be
    reached; there is no such alternate surface today, which is why this set is
    empty. Adding an entry here is a decision to hide a doc — make it deliberately."""
    assert build.BROWSE_EXCLUDE_IDS == set(), (
        "a browse exclusion hides a doc from the only surface that lists it; "
        "confirm the doc is reachable some other way before adding one")


def test_full_tier_shows_all_suite_members_even_without_docs():
    # epsilon has a doc; gamma/delta have none — full tier still shows all three.
    data = _payload([_doc("epsilon/prd/x", "epsilon")], "full")
    groups = {g["label"]: g["members"] for g in data["productGroups"]}
    assert groups["Mobile App Suite"] == ["gamma", "delta", "epsilon"]
    assert data["standaloneProducts"] == []          # no alpha/beta docs here


def test_contractor_tier_hides_docless_suite_members():
    # A contractor bundle must not carry gamma/delta (no docs) — only epsilon.
    data = _payload([_doc("epsilon/prd/x", "epsilon", contractor=True)], "contractor")
    groups = {g["label"]: g["members"] for g in data["productGroups"]}
    assert groups["Mobile App Suite"] == ["epsilon"]
    assert "gamma" not in data["projectLabel"]
    assert "delta" not in data["projectLabel"]


def test_contractor_drops_a_fully_empty_suite():
    # No mobile-app docs at all → the suite heading doesn't render for a contractor.
    data = _payload([_doc("alpha/prd/x", "alpha", contractor=True)], "contractor")
    assert data["productGroups"] == []
    assert data["standaloneProducts"] == ["alpha"]


def test_standalone_products_stay_out_of_the_suite():
    # Set, not list: PRODUCT_ORDER is registry-derived (CQ-171 ruling 1) and
    # the pinned fixture mirrors today's real `_products` map order, not the
    # Bosun's not-yet-landed rearrange — so alpha/beta's relative order
    # here is a registry-rearrange detail, not the invariant this test pins.
    data = _payload([_doc("alpha/prd/x", "alpha"), _doc("beta/prd/y", "beta")], "full")
    assert set(data["standaloneProducts"]) == {"alpha", "beta"}
    members = [m for g in data["productGroups"] for m in g["members"]]
    assert "alpha" not in members and "beta" not in members


def test_project_label_only_ships_referenced_projects():
    # projectLabel must not leak labels for projects the bundle doesn't surface.
    data = _payload([_doc("alpha/prd/x", "alpha")], "full")
    # alpha is referenced; the suite members are referenced in full tier; unreferenced
    # company projects (utility) that have no docs must not appear.
    assert "alpha" in data["projectLabel"]
    assert "utility" not in data["projectLabel"]


# --- area placement (substrate ARCH-436, PR G) -------------------------------


def test_the_area_lists_name_only_live_scopes():
    """Every scope in the three area lists must be one the KB vocabulary still has.

    `COMPANY_PROJECTS` and `ENGINEERING_PROJECTS` named `shared`, `utility` and
    `engineering` until PR G. Phase 0 step 5 emptied all three, so both areas
    rendered empty and their documents' successors fell through `render`'s
    unknown-project fallback onto the Products tab. Nothing failed — the fallback
    is silent by design and the only symptom is a page laid out wrongly.

    Checked against PROJECT_LABEL rather than against the KB, so it runs in CI:
    PROJECT_LABEL is the map F updated in the same pass that missed these lists,
    which makes their disagreement the exact drift to pin.
    """
    placed = set(build.COMPANY_PROJECTS) | set(build.ENGINEERING_PROJECTS) \
        | set(build.PRODUCT_ORDER) | set(build.STANDALONE_PRODUCTS)
    unlabelled = sorted(p for p in placed if p not in build.PROJECT_LABEL)
    assert not unlabelled, (
        f"area lists name scopes with no label: {unlabelled}. Either the scope is "
        f"retired and the list is stale, or it is live and PROJECT_LABEL will "
        f"render it as a bare slug.")


def test_a_scope_can_only_be_in_one_area():
    """A project in two areas renders in both, which reads as two different docs."""
    lists = {"company": build.COMPANY_PROJECTS, "products": build.PRODUCT_ORDER,
             "engineering": build.ENGINEERING_PROJECTS,
             "standalone": build.STANDALONE_PRODUCTS}
    seen: dict[str, str] = {}
    for area, members in lists.items():
        for p in members:
            assert p not in seen, f"{p} is in both {seen[p]} and {area}"
            seen[p] = area


@pytest.mark.needs_kb
def test_every_indexed_project_is_placed():
    """Every project holding an indexed document must be placed in an area.

    `render` falls back to a Products pill for anything unplaced, deliberately, so
    a new scope renders rather than vanishing. That fallback is why the step-5
    breakage was invisible: `company`, `platform`, `engineering-practice`,
    `knowledge`, `convivy-orient` and `convivy-lab` all appeared as products and
    the page looked populated. This is the check the fallback cannot be.

    Reads the live index, so it needs the KB and runs under `check-kb-guards`
    rather than in CI. Check the index count before trusting it (ARCH-433) — a
    watcher holding a stale schema drops whole scopes, and an absent project would
    read here as a project correctly placed nowhere.
    """
    import pathlib
    import sqlite3

    # **This test's own path, not a constant borrowed from production.** Orient
    # holds no knowledge-index path any more — the Reference Room epic
    # (issue_01M08YBCDTEFJ6N9S798ERF89P) removed the last one — and this guard
    # reads the LIVE index on purpose, so it names the file itself rather than
    # keeping a production constant alive for a test's benefit.
    db = pathlib.Path.home() / ".knowledge-index" / "index.db"
    assert db.is_file(), f"no knowledge index at {db}"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        indexed = {r[0] for r in con.execute("select distinct project from docs")}
    finally:
        con.close()
    assert indexed, "the index reported no projects at all; it is empty or stale"

    placed = set(build.COMPANY_PROJECTS) | set(build.ENGINEERING_PROJECTS) \
        | set(build.PRODUCT_ORDER) | set(build.STANDALONE_PRODUCTS)
    unplaced = sorted(indexed - placed)
    assert not unplaced, (
        f"indexed projects nobody has placed in an area: {unplaced}. They render "
        f"as Products pills by fallback. Add each to COMPANY_PROJECTS, "
        f"PRODUCT_ORDER, ENGINEERING_PROJECTS or STANDALONE_PRODUCTS.")
