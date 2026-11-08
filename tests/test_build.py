"""build.load_docs() — the Library reader that feeds build.build()'s static
bundle (serve.sh runs it, unconditionally, before every other builder).

Moved off a direct `~/.knowledge-index/index.db` read onto `library_client`'s
reference readers (issue_01M1MFJRR46TMQ6DSJSD7DGX7C) — the entitlement gate
(`"team" in audience`) now lives in `library_client._entitled`, exercised
here through `library_fake` rather than reimplemented, so these tests seed
`tests/library_fake.py` instead of building a real `index.db` in `tmp_path`.
The malformed/non-list-audience fail-closed cases that used to live here
moved with the gate — `tests/test_library_client.py` covers them for the
route both `list_reference_documents` and `get_reference_document` share.
"""
import dataclasses
import json

import pytest

import build
import library_client
import library_fake


def test_load_docs_returns_only_team_entitled_docs(monkeypatch):
    """`list_reference_documents(audience="team")` already applies the
    entitlement gate — a doc the Library doesn't consider team-audience
    (a bare `["llm"]` tag, no `"team"` at all) must not reach the bundle."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="shared/reference/good", title="Good",
                                    audience=["team"], body="b\n")
    library_fake.seed_reference_doc(id="shared/reference/llm-only", title="LLM only",
                                    audience=["llm"], body="b\n")
    docs = build.load_docs()
    ids = {d["id"] for d in docs}
    assert ids == {"shared/reference/good"}


def test_load_docs_contractor_flag_from_audience(monkeypatch):
    """`load_docs()` computes `contractor_ok` from the fetched document's
    parsed `audience` list — a team doc with "contractor" is flagged, a team
    doc without it isn't."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="shared/reference/team-only", title="TEAM-ONLY",
                                    audience=["team"], body="b\n")
    library_fake.seed_reference_doc(id="shared/reference/team-and-contractor",
                                    title="TEAM-AND-CONTRACTOR",
                                    audience=["team", "contractor"], body="b\n")
    docs = {d["id"]: d for d in build.load_docs()}
    assert docs["shared/reference/team-only"]["contractor"] is False
    assert docs["shared/reference/team-and-contractor"]["contractor"] is True


def test_load_docs_cleans_the_body_and_carries_metadata(monkeypatch):
    """Each row carries the fields `render()` needs, and the body has been
    through `_clean_body` (frontmatter/HIDE_SECTIONS stripped) — the one
    piece of site policy that stays in Orient rather than moving to the
    Library (epic issue_01M08YBCDTEFJ6N9S798ERF89P)."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(
        id="alpha/runbooks/deploy", project="alpha", kind="runbook",
        title="Deploy runbook", audience=["team"],
        body="---\nfoo: bar\n---\n## Provenance\nsecret\n## Steps\ndo it\n")
    docs = build.load_docs()
    assert len(docs) == 1
    doc = docs[0]
    assert doc["id"] == "alpha/runbooks/deploy"
    assert doc["project"] == "alpha"
    assert doc["kind"] == "runbook"
    assert doc["title"] == "Deploy runbook"
    assert doc["status"] == "current"
    assert "secret" not in doc["body"]
    assert "do it" in doc["body"]


def test_load_docs_sorted_by_project_kind_title(monkeypatch):
    """The Library's listing order is not guaranteed; load_docs() re-sorts to
    match the old SQL's `ORDER BY project, kind, title` so the rendered
    bundle doesn't reshuffle on every build."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="b/z", project="b", kind="reference",
                                    title="Z", audience=["team"], body="x\n")
    library_fake.seed_reference_doc(id="a/z", project="a", kind="reference",
                                    title="Z", audience=["team"], body="x\n")
    library_fake.seed_reference_doc(id="a/a", project="a", kind="overview",
                                    title="A", audience=["team"], body="x\n")
    ids = [d["id"] for d in build.load_docs()]
    assert ids == ["a/a", "a/z", "b/z"]


def test_load_docs_respects_browse_exclude_ids(monkeypatch):
    """A doc listed in BROWSE_EXCLUDE_IDS is dropped even though it is
    otherwise team-entitled — its canonical home is a curated section
    elsewhere on the site (ARCH-308)."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="excluded/doc", title="Excluded",
                                    audience=["team"], body="x\n")
    library_fake.seed_reference_doc(id="kept/doc", title="Kept",
                                    audience=["team"], body="x\n")
    monkeypatch.setattr(build, "BROWSE_EXCLUDE_IDS", {"excluded/doc"})
    ids = {d["id"] for d in build.load_docs()}
    assert ids == {"kept/doc"}


def test_load_docs_raises_on_a_library_failure(monkeypatch):
    """A batch site build must fail loudly rather than write a bundle
    silently missing every document — the worse failure, since a half-built
    site that looks complete gives no sign anything is wrong."""
    def _boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_reference_documents", _boom)
    with pytest.raises(RuntimeError):
        build.load_docs()


def test_load_docs_drops_a_doc_that_vanishes_between_the_list_and_the_fetch(monkeypatch):
    """`load_docs()` fetches each listed document individually, so a doc can be
    deleted or re-tagged in the window between the two calls and come back
    `None` — a real race against a live Library during a 373-document batch,
    not a hypothetical. That row is DROPPED and the rest of the bundle builds
    (`build.py:278-283`).

    Pinned because the branch is the whole difference between losing one stale
    reference and losing the build: a regression turning the `continue` into a
    raise would abort every restart over a single mid-build edit, and one
    turning it into a silent pass-through would put a `None` body into
    `_clean_body`. Neither is visible without this test (reviewer, PR #262)."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="shared/reference/stays", title="Stays",
                                    audience=["team"], body="b\n")
    library_fake.seed_reference_doc(id="shared/reference/vanishes", title="Vanishes",
                                    audience=["team"], body="b\n")
    listed = library_client.get_reference_document

    def _gone_for_one(doc_id, **kw):
        return None if doc_id == "shared/reference/vanishes" else listed(doc_id, **kw)

    monkeypatch.setattr(library_client, "get_reference_document", _gone_for_one)
    docs = build.load_docs()
    assert {d["id"] for d in docs} == {"shared/reference/stays"}


def test_load_docs_drops_a_doc_whose_body_is_none(monkeypatch):
    """The same branch's other half. `full.body is None` is checked alongside
    `full is None` because `_clean_body` would raise on it, turning one
    body-less document into a failed build — the outcome the drop exists to
    avoid."""
    library_fake.patch(monkeypatch)
    library_fake.seed_reference_doc(id="shared/reference/stays", title="Stays",
                                    audience=["team"], body="b\n")
    library_fake.seed_reference_doc(id="shared/reference/bodyless", title="Bodyless",
                                    audience=["team"], body="b\n")
    listed = library_client.get_reference_document

    def _bodyless_for_one(doc_id, **kw):
        doc = listed(doc_id, **kw)
        if doc is not None and doc_id == "shared/reference/bodyless":
            return dataclasses.replace(doc, body=None)
        return doc

    monkeypatch.setattr(library_client, "get_reference_document", _bodyless_for_one)
    docs = build.load_docs()
    assert {d["id"] for d in docs} == {"shared/reference/stays"}


# --- PROJECT_LABEL comes from the install's registry -----------------------
#
# Captured on main against the live registry (2026-10-22), before this dict
# read the registry at all — the full literal that line 72's `PROJECT_LABEL =
# {...}` held. This test pins the registry-derived dict to those exact
# values, so a behaviour change here is a regression, not a readjustment.
_PROJECT_LABEL_CAPTURED_ON_MAIN = {
    "company": "Convivy & Company", "commercial": "Commercial",
    "alpha": "Alpha", "beta": "Beta",
    "gamma": "Gamma", "delta": "Delta", "epsilon": "Epsilon",
    "agent-platform": "Agent Platform",
    "convivy-app-kit": "Lambda Kit", "convivy-services": "Services",
    "zeta-studio": "Zeta Studio", "deck": "Deck",
    "platform": "Platform", "engineering-practice": "Engineering Practice",
    "knowledge": "Knowledge Base", "convivy-orient": "Orient",
    "substrate": "Substrate",
    "keel": "The Keel",
    "bosun": "Bosun",
    "convivy-deck": "Deck",
    "library": "Library",
    "write-better": "Write Better",
    "beta-landing": "Beta Landing",
    "theta": "Theta",
    "signals": "Signals",
    "eta": "Eta",
    "shipwright": "Shipwright",
}


def test_project_label_matches_the_values_captured_on_main():
    for slug, label in _PROJECT_LABEL_CAPTURED_ON_MAIN.items():
        assert build.PROJECT_LABEL.get(slug) == label


def test_project_label_comes_from_a_made_up_registry(tmp_path, monkeypatch):
    """No Convivy name: a made-up project's own registry `display` reaches
    PROJECT_LABEL, and a project the registry has never heard of keeps its
    fallback label untouched — entirely from the fixture registry."""
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "beta-studio": {"display": "Beta Studio"},
        "_products": {"beta-studio": {"display": "Beta Studio", "owner": "acme"}},
    }))
    monkeypatch.setenv("PROJECTS_JSON", str(registry))
    assert build.PROJECT_LABEL["beta-studio"] == "Beta Studio"
    assert build.PROJECT_LABEL["company"] == "Convivy & Company"


def test_project_label_keeps_only_the_fallback_labels_on_an_unreadable_registry(tmp_path, monkeypatch):
    """Never falls back to a stale literal for a registry-backed slug — an
    unreadable registry simply drops those entries rather than guessing."""
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    assert "alpha" not in build.PROJECT_LABEL
    assert build.PROJECT_LABEL["company"] == "Convivy & Company"


# --- a repo's own `repo_display` label (CQ-171 slice 6, replacing the
# landing-repo literal `_PROJECT_LABEL_FALLBACK` used to carry) ------


def test_project_label_reads_a_repos_own_repo_display(tmp_path, monkeypatch):
    """A repo under a unit's `repos` with no label of its own (the standing
    gap: a product's marketing-site repo) now gets one from that unit's
    `repo_display` map — no hardcoded fallback needed."""
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "beta": {"display": "Beta", "repos": ["beta", "beta-landing"],
                "repo_display": {"beta-landing": "Beta Landing"}},
    }))
    monkeypatch.setenv("PROJECTS_JSON", str(registry))
    assert build.PROJECT_LABEL["beta-landing"] == "Beta Landing"
    assert build.PROJECT_LABEL["beta"] == "Beta"


def test_project_label_ignores_a_repo_display_key_outside_the_units_own_repos(tmp_path, monkeypatch):
    """A stale or mistyped `repo_display` key must not hand a label to a
    repo nobody claims."""
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "beta": {"display": "Beta", "repos": ["beta"],
                "repo_display": {"not-betas-repo": "Nope"}},
    }))
    monkeypatch.setenv("PROJECTS_JSON", str(registry))
    assert "not-betas-repo" not in build.PROJECT_LABEL


def test_project_label_repo_display_wins_over_the_units_own_display(tmp_path, monkeypatch):
    """`repo_display[repo]` is the registry's most specific statement about
    that one repo — it wins even when the SAME repo name is also, somehow,
    a unit's own top-level `display` elsewhere in the fallback/registry
    merge."""
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "beta": {"display": "Beta", "repos": ["beta", "beta-landing"],
                "repo_display": {"beta-landing": "Beta Landing Page"}},
        "beta-landing": {"display": "Wrong Label"},
    }))
    monkeypatch.setenv("PROJECTS_JSON", str(registry))
    assert build.PROJECT_LABEL["beta-landing"] == "Beta Landing Page"


# --- PRODUCT_ORDER / STANDALONE_PRODUCTS come from the registry's _products
# and _kb_projects maps (CQ-171 revise round 2, the steward's registry-fields
# ruling) ---------------------------------------------------------------------
#
# The live registry isn't in the Bosun's rearranged shape yet (platform
# decision 0098: a `_products` key order matching Orient's shipped
# PRODUCT_ORDER, and a new top-level `_kb_projects` map carrying each
# standalone KB project's own `standalone: true`), so these tests build that
# shape as their own fixture rather than reading the live file or
# `_pinned_project_registry`'s mirror of it.
_PRODUCTS_AFTER_BOSUN_REARRANGE = {
    "alpha": {"display": "Alpha", "owner": "convivy"},
    "beta": {"display": "Beta", "owner": "convivy"},
    "gamma": {"display": "Gamma", "owner": "convivy"},
    "delta": {"display": "Delta", "owner": "convivy"},
    "epsilon": {"display": "Epsilon", "owner": "convivy"},
    "zeta-studio": {"display": "Zeta Studio", "owner": "omega"},
    "eta": {"display": "Eta", "owner": "convivy"},
}
# The eleven standalone KB projects the steward named, in the order the ruling
# listed them: zeta-studio and eta also have a `_products` entry above, which
# PRODUCT_ORDER must skip because this map marks them standalone; the other
# nine name no `_products` entry at all.
# The order main shipped as the STANDALONE_PRODUCTS literal, which is also the
# order agent-platform#992 writes `_kb_projects` in. The pill row renders in
# this order, so the test below pins it explicitly.
_STANDALONE_ORDER_SHIPPED_ON_MAIN = [
    "agent-platform", "convivy-app-kit", "convivy-services", "deck",
    "zeta-studio", "theta", "substrate", "beta-landing", "signals",
    "eta", "shipwright"]
_KB_PROJECTS_AFTER_BOSUN_REARRANGE = {
    slug: {"standalone": True} for slug in _STANDALONE_ORDER_SHIPPED_ON_MAIN
}


def _set_rearranged_products_registry(tmp_path, monkeypatch, products, kb_projects=None):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"_products": products, "_kb_projects": kb_projects or {}}))
    monkeypatch.setenv("PROJECTS_JSON", str(path))


def test_product_order_matches_the_order_shipped_on_main(tmp_path, monkeypatch):
    """Once the Bosun's rearrange lands, PRODUCT_ORDER is the `_products` keys
    in the map's own order, skipping any key `_kb_projects` marks standalone
    — pinned here to the exact order main shipped as a literal, so a
    behaviour change is a regression, not a readjustment."""
    _set_rearranged_products_registry(tmp_path, monkeypatch, _PRODUCTS_AFTER_BOSUN_REARRANGE,
                                       _KB_PROJECTS_AFTER_BOSUN_REARRANGE)
    assert build.PRODUCT_ORDER == ["alpha", "beta", "gamma", "delta", "epsilon"]


def test_standalone_products_is_exactly_the_kb_projects_marked_standalone(tmp_path, monkeypatch):
    """STANDALONE_PRODUCTS is the `_kb_projects` keys marked `standalone: true`,
    in the map's own written order, and nothing else — no fallback list."""
    _set_rearranged_products_registry(tmp_path, monkeypatch, _PRODUCTS_AFTER_BOSUN_REARRANGE,
                                       _KB_PROJECTS_AFTER_BOSUN_REARRANGE)
    assert build.STANDALONE_PRODUCTS == [
        "agent-platform", "convivy-app-kit", "convivy-services", "deck",
        "zeta-studio", "theta", "substrate", "beta-landing", "signals",
        "eta", "shipwright"]


def test_product_order_comes_from_a_made_up_registry(tmp_path, monkeypatch):
    """No Convivy name: a made-up `_products` map drives PRODUCT_ORDER and a
    made-up `_kb_projects` map drives STANDALONE_PRODUCTS, entirely from the
    fixture registry."""
    _set_rearranged_products_registry(
        tmp_path, monkeypatch,
        products={"beta-studio": {"display": "Beta Studio", "owner": "acme"},
                  "gamma": {"display": "Gamma", "owner": "acme"}},
        kb_projects={"gamma": {"standalone": True}})
    assert build.PRODUCT_ORDER == ["beta-studio"]
    assert build.STANDALONE_PRODUCTS == ["gamma"]


def test_product_order_is_empty_on_an_unreadable_registry(tmp_path, monkeypatch):
    """Never falls back to a stale literal order or list — an unreadable
    registry simply answers no products and no standalone products, with
    nothing left to fall back to."""
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    assert build.PRODUCT_ORDER == []
    assert build.STANDALONE_PRODUCTS == []


# --- COMPANY_PROJECTS comes from the registry's _scopes map (CQ-171 ruling 4) --


def test_company_projects_matches_the_values_shipped_on_main(tmp_path, monkeypatch):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"_scopes": {
        "company": {"display": "Convivy"}, "commercial": {"display": "Commercial"},
        "platform": {"display": "Platform"},
    }}))
    monkeypatch.setenv("PROJECTS_JSON", str(path))
    assert build.COMPANY_PROJECTS == ["company", "commercial"]


def test_company_projects_comes_from_a_made_up_registry(tmp_path, monkeypatch):
    """A made-up registry carrying only one of the two scopes yields only
    that one — never a stale guess at the other."""
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"_scopes": {"company": {"display": "Acme & Co"}}}))
    monkeypatch.setenv("PROJECTS_JSON", str(path))
    assert build.COMPANY_PROJECTS == ["company"]


def test_company_projects_is_empty_on_an_unreadable_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    assert build.COMPANY_PROJECTS == []


# --- ENGINEERING_PROJECTS comes from the registry's `_kb_projects` map
# (CQ-171 slice 5, the steward's registry-fields ruling) --------------------
#
# `_kb_projects` carries no `section` field on the live registry at all yet
# (the Bosun's agent-platform#992 adds it), so there is no captured live
# order to pin against the way PRODUCT_GROUPS below has one — these tests
# build the map from scratch, in the order main shipped ENGINEERING_PROJECTS.


def test_engineering_projects_matches_the_values_shipped_on_main(tmp_path, monkeypatch):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"_kb_projects": {
        "platform": {"section": "engineering"},
        "engineering-practice": {"section": "engineering"},
        "knowledge": {"section": "engineering"},
        "convivy-orient": {"section": "engineering"},
        "keel": {"section": "engineering"},
        "library": {"section": "engineering"},
        "write-better": {"section": "engineering"},
        "eta": {"standalone": True},  # a differently-marked key must not leak in
    }}))
    monkeypatch.setenv("PROJECTS_JSON", str(path))
    assert build.ENGINEERING_PROJECTS == [
        "platform", "engineering-practice", "knowledge", "convivy-orient",
        "keel", "library", "write-better"]


def test_engineering_projects_comes_from_a_made_up_registry(tmp_path, monkeypatch):
    """No Convivy name: a made-up `_kb_projects` map drives ENGINEERING_PROJECTS
    entirely from the fixture registry."""
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"_kb_projects": {
        "beta-tools": {"section": "engineering"},
        "gamma": {"section": "products"},
    }}))
    monkeypatch.setenv("PROJECTS_JSON", str(path))
    assert build.ENGINEERING_PROJECTS == ["beta-tools"]


def test_engineering_projects_is_empty_on_an_unreadable_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    assert build.ENGINEERING_PROJECTS == []


# --- PRODUCT_GROUPS comes from `_products[<product>].group` (CQ-171 slice 5,
# the steward's registry-fields ruling) --------------------------------------
#
# Built in the Bosun's rearranged `_products` shape, same as the
# PRODUCT_ORDER tests above (`_PRODUCTS_AFTER_BOSUN_REARRANGE`), rather than
# `_pinned_project_registry`'s mirror of the live file's own, differently
# ordered `_products` map.


def test_product_groups_matches_the_value_shipped_on_main(tmp_path, monkeypatch):
    """A group's position is where its first member appears in `_products`;
    a group's own members stay in `_products` order — pinned here to the
    exact value main shipped as a `PRODUCT_GROUPS` literal."""
    products = {
        **_PRODUCTS_AFTER_BOSUN_REARRANGE,
        "gamma": {**_PRODUCTS_AFTER_BOSUN_REARRANGE["gamma"], "group": "Mobile App Suite"},
        "delta": {**_PRODUCTS_AFTER_BOSUN_REARRANGE["delta"], "group": "Mobile App Suite"},
        "epsilon": {**_PRODUCTS_AFTER_BOSUN_REARRANGE["epsilon"], "group": "Mobile App Suite"},
    }
    _set_rearranged_products_registry(tmp_path, monkeypatch, products)
    assert build.PRODUCT_GROUPS == [("Mobile App Suite", ["gamma", "delta", "epsilon"])]


def test_product_groups_orders_groups_by_first_member_in_products_order(tmp_path, monkeypatch):
    """Two groups, with the second group's first member appearing before the
    first group's first member in `_products` — the output orders by that
    first-appearance position, not by the groups' own written order."""
    _set_rearranged_products_registry(tmp_path, monkeypatch, {
        "alpha": {"display": "Alpha", "owner": "acme", "group": "Group One"},
        "beta": {"display": "Beta", "owner": "acme", "group": "Group Two"},
        "gamma": {"display": "Gamma", "owner": "acme", "group": "Group One"},
    })
    assert build.PRODUCT_GROUPS == [
        ("Group One", ["alpha", "gamma"]), ("Group Two", ["beta"])]


def test_product_groups_is_empty_on_an_unreadable_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    assert build.PRODUCT_GROUPS == []
