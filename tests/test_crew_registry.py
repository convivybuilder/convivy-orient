"""crew_registry.public_crews: the crew keys a dispatch looks its tool policy
up under, from the registry agentctl reads."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

import crew_registry


def _registry(tmp_path, monkeypatch, units, products=None):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"_meta": {"doc": "x"}, "_products": products or {}, **units}))
    monkeypatch.setenv("PROJECTS_JSON", str(path))


def test_lists_crew_keys_not_project_slugs(tmp_path, monkeypatch):
    """The reason this module exists: a project slug is not always its crew
    key, and the policy table is exact-match on the crew key."""
    _registry(tmp_path, monkeypatch, {
        "bosun": {"crew": "bosun", "lifecycle": "active", "repos": ["agent-platform"]},
        "convivy-deck": {"crew": "deck", "lifecycle": "active"},
    })
    assert crew_registry.public_crews() == ["bosun", "deck"]


def test_a_private_crew_is_never_listed(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "alpha": {"crew": "alpha", "lifecycle": "active"},
        "kappa": {"crew": "kappa", "lifecycle": "active", "private": True},
    })
    assert crew_registry.public_crews() == ["alpha"]


def test_an_inactive_unit_and_a_null_crew_are_skipped(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "alpha": {"crew": "alpha", "lifecycle": "active"},
        "old": {"crew": "old", "lifecycle": "sunset"},
        "nocrew": {"crew": None, "lifecycle": "active"},
    })
    assert crew_registry.public_crews() == ["alpha"]


def test_a_crew_shared_by_two_projects_is_listed_once(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "delta": {"crew": "delta", "lifecycle": "active"},
        "delta-backend": {"crew": "delta", "lifecycle": "active"},
    })
    assert crew_registry.public_crews() == ["delta"]


def test_a_missing_registry_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.public_crews()


def test_a_scope_holding_every_project_covers_the_registry(tmp_path, monkeypatch):
    """`_`-prefixed keys are not projects, and a private unit is one."""
    _registry(tmp_path, monkeypatch, {"alpha": {"crew": "a"},
                                      "hidden": {"crew": "h", "private": True}})
    assert crew_registry.covers_every_project(frozenset({"alpha", "hidden"}))
    assert crew_registry.covers_every_project(frozenset({"alpha", "hidden", "extra"}))


def test_a_scope_missing_one_project_does_not_cover_the_registry(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {"crew": "a"},
                                      "hidden": {"crew": "h", "private": True}})
    assert not crew_registry.covers_every_project(frozenset({"alpha"}))
    assert not crew_registry.covers_every_project(frozenset())


def test_a_missing_registry_covers_nobody(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    assert not crew_registry.covers_every_project(frozenset({"alpha"}))


def test_repos_in_scope_maps_projects_to_their_repos(tmp_path, monkeypatch):
    """A repo's name is not its project's slug: `agent-platform` is `bosun`'s."""
    _registry(tmp_path, monkeypatch, {"bosun": {"crew": "bosun", "repos": ["agent-platform"]},
                                      "library": {"crew": "library", "repos": ["library", "knowledge"]},
                                      "bare": {"crew": "b"}})
    assert crew_registry.repos_in_scope(frozenset({"bosun", "library"})) == {
        "agent-platform", "library", "knowledge"}
    assert crew_registry.repos_in_scope(frozenset({"bosun"})) == {"agent-platform"}
    assert crew_registry.repos_in_scope(frozenset({"bare", "unknown"})) == frozenset()


def test_a_missing_registry_puts_no_repo_in_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    assert crew_registry.repos_in_scope(frozenset({"bosun"})) == frozenset()


def test_crews_in_scope_maps_projects_to_their_crew_keys(tmp_path, monkeypatch):
    """A crew key is not its project's slug either: `agent-platform`'s is
    `bosun`. A private unit's crew counts, an underscore key is metadata, and
    a missing, non-string or blank `crew` contributes nothing."""
    _registry(tmp_path, monkeypatch, {"agent-platform": {"crew": "bosun"},
                                      "secret": {"crew": " hush ", "private": True},
                                      "_meta": {"crew": "nobody"},
                                      "nocrew": {},
                                      "numcrew": {"crew": 7},
                                      "blank": {"crew": "  "}})
    everything = frozenset({"agent-platform", "secret", "_meta", "nocrew", "numcrew", "blank"})
    assert crew_registry.crews_in_scope(everything) == {"bosun", "hush"}
    assert crew_registry.crews_in_scope(frozenset({"agent-platform"})) == {"bosun"}
    assert crew_registry.crews_in_scope(frozenset({"bosun"})) == frozenset()


def test_a_missing_registry_puts_no_crew_in_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    assert crew_registry.crews_in_scope(frozenset({"bosun"})) == frozenset()


# --- where the registry comes from (platform decision 0098) -----------------


class _FakeResponse:
    def __init__(self, body):
        self._raw = json.dumps(body).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A stub Library behind `urlopen`, with `$PROJECTS_JSON` unset and the
    builder root holding the two files the reader used to fall back to, so a
    test passes only by asking the Library. `answer` is what the next
    `GET /registry` gets: a dict to serve, or an exception to raise."""
    import library_client

    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    monkeypatch.setenv("BUILDER_ROOT", str(tmp_path))
    for stale in (tmp_path / "install" / "projects.json",
                  tmp_path / "agent-platform" / "config" / "projects.json"):
        stale.parent.mkdir(parents=True)
        stale.write_text(json.dumps({"stale": {"crew": "stale", "lifecycle": "active"}}))
    monkeypatch.setenv("LIBRARY_CLIENT_TOKEN", "test-token")
    monkeypatch.setattr(library_client, "_AGENT_CREDENTIAL_VERIFIED", True)
    crew_registry.clear_cache()
    state = {"answer": None, "requests": []}

    def fake_urlopen(req, timeout=None):
        state["requests"].append(req)
        answer = state["answer"]
        if isinstance(answer, Exception):
            raise answer
        return _FakeResponse(answer)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    yield state
    crew_registry.clear_cache()


def _http_error(code):
    return urllib.error.HTTPError("http://library/registry", code, "refused", {}, None)


def test_the_registry_is_read_from_the_librarys_get_registry(library):
    import library_client

    library["answer"] = {"_meta": {}, "alpha": {"crew": "alpha", "lifecycle": "active"}}
    assert crew_registry.public_crews() == ["alpha"]
    (req,) = library["requests"]
    assert urllib.parse.urlsplit(req.full_url).path == "/registry"
    assert req.get_method() == "GET"
    assert req.get_header("Authorization") == "Bearer test-token"
    assert req.get_header(library_client.PROTOCOL_HEADER.capitalize()) == str(
        library_client.PROTOCOL_VERSION)


def test_an_unreachable_library_is_registry_unavailable(library):
    library["answer"] = urllib.error.URLError("connection refused")
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.units()


@pytest.mark.parametrize("code", [503, 403, 500])
def test_any_answer_but_200_is_registry_unavailable(library, code):
    """503 is the Library's answer while its table is empty, 403 a credential
    without `registry.read`. Neither falls back to the stale files the fixture
    leaves where the reader used to look."""
    library["answer"] = _http_error(code)
    with pytest.raises(crew_registry.RegistryUnavailable, match=str(code)):
        crew_registry.units()


def test_a_body_that_is_not_an_object_is_registry_unavailable(library):
    library["answer"] = ["alpha"]
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.units()


def test_the_library_read_is_cached_and_refetched_after_the_ttl(library, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(crew_registry.time, "monotonic", lambda: clock["now"])
    library["answer"] = {"alpha": {"crew": "alpha", "lifecycle": "active"}}
    for _ in range(5):
        crew_registry.units()
    assert len(library["requests"]) == 1

    library["answer"] = {"beta": {"crew": "beta", "lifecycle": "active"}}
    clock["now"] += crew_registry._REGISTRY_TTL_S
    assert list(crew_registry.units()) == ["beta"]
    assert len(library["requests"]) == 2


def test_a_failed_read_is_cached_too(library, monkeypatch):
    """An unreachable Library costs one request per window, not one per read:
    a single /overview render reads the registry about 1,800 times."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(crew_registry.time, "monotonic", lambda: clock["now"])
    library["answer"] = urllib.error.URLError("connection refused")
    for _ in range(3):
        with pytest.raises(crew_registry.RegistryUnavailable):
            crew_registry.units()
    assert len(library["requests"]) == 1

    library["answer"] = {"alpha": {"crew": "alpha", "lifecycle": "active"}}
    clock["now"] += crew_registry._REGISTRY_TTL_S
    assert list(crew_registry.units()) == ["alpha"]


def test_a_caller_editing_its_copy_does_not_change_the_cache(library):
    library["answer"] = {"alpha": {"crew": "alpha", "lifecycle": "active"}}
    crew_registry.units()["alpha"]["crew"] = "edited"
    assert crew_registry.units()["alpha"]["crew"] == "alpha"


def test_projects_json_overrides_the_library_and_is_read_fresh(library, tmp_path, monkeypatch):
    path = tmp_path / "override.json"
    path.write_text(json.dumps({"alpha": {"crew": "alpha", "lifecycle": "active"}}))
    monkeypatch.setenv("PROJECTS_JSON", str(path))
    assert crew_registry.resolve_registry_path() == path
    assert crew_registry.public_crews() == ["alpha"]
    path.write_text(json.dumps({"beta": {"crew": "beta", "lifecycle": "active"}}))
    assert crew_registry.public_crews() == ["beta"]
    assert library["requests"] == []


def test_without_projects_json_there_is_no_registry_path(library):
    assert crew_registry.resolve_registry_path() is None


# --- display_names ------------------------------------------------------

def test_display_names_merges_units_and_products(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch,
              {"alpha": {"display": "Alpha"}, "nodisplay": {}},
              products={"beta-studio": {"display": "Beta Studio", "owner": "acme"}})
    assert crew_registry.display_names() == {"alpha": "Alpha", "beta-studio": "Beta Studio"}


def test_a_missing_registry_raises_for_display_names(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.display_names()


# --- telemetry_aliases ----------------------------------------------------

def test_telemetry_aliases_folds_a_units_own_aliases(tmp_path, monkeypatch):
    """A unit with no siblings and no aliases of its own passes through
    unfolded — the baseline most projects stay at."""
    _registry(tmp_path, monkeypatch, {
        "alpha": {"display": "Alpha", "aliases": ["legacy-alpha", "a"]},
        "beta": {"display": "Beta", "aliases": []},
    })
    assert crew_registry.telemetry_aliases() == {
        "alpha": "Alpha", "legacy-alpha": "Alpha", "a": "Alpha"}


def test_telemetry_aliases_folds_siblings_sharing_one_product(tmp_path, monkeypatch):
    """Three repos building one product each report under their own name and
    each needs its own fold to its own display, even with no alias of its own."""
    _registry(tmp_path, monkeypatch, {
        "wing-east": {"display": "East Wing", "products": ["studio"]},
        "wing-west": {"display": "West Wing", "products": ["studio"]},
        "solo": {"display": "Solo", "products": ["solo"]},
    })
    assert crew_registry.telemetry_aliases() == {
        "wing-east": "East Wing", "wing-west": "West Wing"}


def test_a_missing_registry_raises_for_telemetry_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.telemetry_aliases()


# --- scopes -----------------------------------------------------------------

def test_scopes_reads_the_scopes_map_doc_key_excluded(tmp_path, monkeypatch):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({
        "alpha": {"crew": "a"},
        "_scopes": {
            "_doc": "explains the map",
            "company": {"display": "Convivy"},
            "commercial": {"display": "Commercial"},
        },
    }))
    monkeypatch.setenv("PROJECTS_JSON", str(path))
    assert crew_registry.scopes() == {
        "company": {"display": "Convivy"}, "commercial": {"display": "Commercial"}}


def test_scopes_is_empty_with_no_scopes_map(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {"crew": "a"}})
    assert crew_registry.scopes() == {}


def test_a_missing_registry_raises_for_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.scopes()


# --- kb_projects (CQ-171 revise round 2, the steward's registry-fields ruling) --

def test_kb_projects_reads_the_map_in_its_written_order(tmp_path, monkeypatch):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({
        "alpha": {"crew": "a"},
        "_kb_projects": {
            "beta-studio": {"standalone": True},
            "gamma": {"standalone": True},
        },
    }))
    monkeypatch.setenv("PROJECTS_JSON", str(path))
    assert list(crew_registry.kb_projects()) == ["beta-studio", "gamma"]
    assert crew_registry.kb_projects()["gamma"] == {"standalone": True}


def test_kb_projects_is_empty_with_no_kb_projects_map(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {"crew": "a"}})
    assert crew_registry.kb_projects() == {}


def test_a_missing_registry_raises_for_kb_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.kb_projects()


# --- kb_projects_in_section (CQ-171 slice 5, the steward's registry-fields
# ruling) ---------------------------------------------------------------------

def test_kb_projects_in_section_filters_to_the_matching_section(tmp_path, monkeypatch):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({
        "_kb_projects": {
            "platform": {"section": "engineering"},
            "beta-studio": {"standalone": True},
            "gamma": {"section": "engineering"},
        },
    }))
    monkeypatch.setenv("PROJECTS_JSON", str(path))
    assert crew_registry.kb_projects_in_section("engineering") == ["platform", "gamma"]
    assert crew_registry.kb_projects_in_section("products") == []


def test_kb_projects_in_section_is_empty_with_no_kb_projects_map(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {"crew": "a"}})
    assert crew_registry.kb_projects_in_section("engineering") == []


def test_a_missing_registry_raises_for_kb_projects_in_section(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.kb_projects_in_section("engineering")


# --- product_groups (CQ-171 slice 5, the steward's registry-fields ruling) --

def test_product_groups_reads_each_products_own_group_field(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {}}, products={
        "gamma": {"display": "Gamma", "owner": "convivy", "group": "Mobile App Suite"},
        "beta": {"display": "Beta", "owner": "convivy"},
        "delta": {"display": "Delta", "owner": "convivy", "group": "Mobile App Suite"},
    })
    assert crew_registry.product_groups() == {
        "gamma": "Mobile App Suite", "delta": "Mobile App Suite"}


def test_product_groups_is_empty_with_no_products_map(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {}})
    assert crew_registry.product_groups() == {}


def test_a_missing_registry_raises_for_product_groups(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.product_groups()


# --- code_health_repos (CQ-171 slice 5, the steward's registry-fields
# ruling) ---------------------------------------------------------------------

def test_code_health_repos_flattens_each_units_own_array(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "alpha": {"repos": ["alpha-app", "alpha-landing"], "code_health": ["alpha-app"]},
        "beta": {"repos": ["beta-app"], "code_health": ["beta-app"]},
        "gamma": {"repos": ["gamma-app"]},  # no code_health array at all
    })
    assert crew_registry.code_health_repos() == [("alpha-app", "alpha"), ("beta-app", "beta")]


def test_code_health_repos_drops_an_entry_outside_the_units_own_repos(tmp_path, monkeypatch):
    """A `code_health` entry naming a repo the unit doesn't itself list under
    `repos` is dropped — a repo enters the code-health harvest only through
    its unit's own declared `repos`, never through `code_health` alone."""
    _registry(tmp_path, monkeypatch, {
        "alpha": {"repos": ["alpha-app"], "code_health": ["alpha-app", "not-alphas-repo"]},
    })
    assert crew_registry.code_health_repos() == [("alpha-app", "alpha")]


def test_code_health_repos_is_empty_with_no_unit_carrying_the_field(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {"repos": ["alpha-app"]}})
    assert crew_registry.code_health_repos() == []


def test_a_missing_registry_raises_for_code_health_repos(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.code_health_repos()


# --- unit_rollup_keys / product_rollup_keys ---------------------------------

def test_unit_rollup_keys_filters_to_units_carrying_the_kind(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "alpha": {"rollup": ["repos"]},
        "beta": {"rollup": ["repos", "cost"]},
        "gamma": {"rollup": ["cost"]},
        "delta": {},
    })
    assert crew_registry.unit_rollup_keys("repos") == ["alpha", "beta"]
    assert crew_registry.unit_rollup_keys("cost") == ["beta", "gamma"]


def test_product_rollup_keys_filters_to_products_carrying_the_kind(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {}},
              products={"beta-studio": {"display": "Beta Studio", "rollup": ["cost"]},
                        "gamma": {"display": "Gamma"}})
    assert crew_registry.product_rollup_keys("cost") == ["beta-studio"]
    assert crew_registry.product_rollup_keys("repos") == []


def test_a_missing_registry_raises_for_unit_rollup_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.unit_rollup_keys("repos")


def test_a_missing_registry_raises_for_product_rollup_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.product_rollup_keys("cost")


# --- resolve_home_dir --------------------------------------------------------

def test_resolve_home_dir_expands_the_root_prefix(monkeypatch):
    monkeypatch.setenv("BUILDER_ROOT", "/srv/convivy")
    assert crew_registry.resolve_home_dir("<root>/alpha") == Path("/srv/convivy/alpha")


def test_resolve_home_dir_returns_an_absolute_path_unresolved(monkeypatch):
    monkeypatch.setenv("BUILDER_ROOT", "/srv/convivy")
    assert crew_registry.resolve_home_dir("/tmp/some/repo") == Path("/tmp/some/repo")


def test_resolve_home_dir_is_none_for_anything_else():
    assert crew_registry.resolve_home_dir("relative/path") is None
    assert crew_registry.resolve_home_dir("") is None
    assert crew_registry.resolve_home_dir(None) is None
    assert crew_registry.resolve_home_dir(7) is None


# --- repo_github_owner / DEFAULT_GITHUB_ORG ---------------------------------

def test_repo_github_owner_is_the_default_with_no_owner_fields(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {"repos": ["alpha-app"]}})
    assert crew_registry.repo_github_owner("alpha-app") == crew_registry.DEFAULT_GITHUB_ORG
    assert crew_registry.DEFAULT_GITHUB_ORG == "convivy"


def test_repo_github_owner_reads_the_units_github_owner(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "omega-widgets": {"repos": ["omega-widgets"], "github_owner": "someone-else"},
    })
    assert crew_registry.repo_github_owner("omega-widgets") == "someone-else"


def test_repo_owners_override_wins_over_the_units_github_owner(tmp_path, monkeypatch):
    """A per-repo `repo_owners` entry answers before the unit-wide
    `github_owner` — the gap `github_owner` alone can't close for a unit
    whose repos don't all share one owner (shipwright's `homebrew-tap`)."""
    _registry(tmp_path, monkeypatch, {
        "shipwright": {
            "repos": ["shipwright", "homebrew-tap"],
            "repo_owners": {"homebrew-tap": "convivybuilder"},
        },
    })
    assert crew_registry.repo_github_owner("shipwright") == crew_registry.DEFAULT_GITHUB_ORG
    assert crew_registry.repo_github_owner("homebrew-tap") == "convivybuilder"


def test_repo_github_owner_ignores_the_business_ownership_owner_field(tmp_path, monkeypatch):
    """`_products[*].owner` ("convivy" | "omega" | "tbd") is business metadata,
    not a GitHub account — the confusion a past review caught (three Omega
    repos 404ing). `repo_github_owner` must never read it, org-wide or
    per-repo."""
    _registry(tmp_path, monkeypatch, {
        "omega-widgets": {"repos": ["omega-widgets"]},
    }, products={"widgets": {"display": "Widgets", "owner": "omega"}})
    assert crew_registry.repo_github_owner("omega-widgets") == crew_registry.DEFAULT_GITHUB_ORG


def test_repo_github_owner_is_the_default_for_an_unregistered_repo(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "omega-widgets": {"repos": ["omega-widgets"], "github_owner": "someone-else"},
    })
    assert crew_registry.repo_github_owner("not-a-registered-repo") == crew_registry.DEFAULT_GITHUB_ORG


def test_repo_github_owner_is_the_default_when_the_registry_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    assert crew_registry.repo_github_owner("anything") == crew_registry.DEFAULT_GITHUB_ORG


def test_an_org_wide_call_never_takes_a_units_github_owner(tmp_path, monkeypatch):
    """The literal `DEFAULT_GITHUB_ORG` a call site uses for an org-scoped
    query (`/orgs/<org>/repos`, `search prs --owner`) is never a function of
    any unit's own `github_owner` — unlike `repo_github_owner`, which is
    per-repo, `DEFAULT_GITHUB_ORG` is a plain constant the registry can't
    change."""
    _registry(tmp_path, monkeypatch, {
        "omega-widgets": {"repos": ["omega-widgets"], "github_owner": "someone-else"},
    })
    assert crew_registry.DEFAULT_GITHUB_ORG == "convivy"


# --- activation_lanes_for_repo (CQ-171 slice 6) -----------------------------

def test_activation_lanes_for_repo_reads_the_owning_units_field(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "alpha": {"repos": ["alpha"], "activation_lanes": ["daytime", "commercial", "overnight"]},
    })
    assert crew_registry.activation_lanes_for_repo("alpha") == ["daytime", "commercial", "overnight"]


def test_activation_lanes_for_repo_row_key_differs_from_the_unit_key(tmp_path, monkeypatch):
    """A roster row's repo is not always named after the unit that owns it
    — the same shape `repo_github_owner` already covers for GitHub
    ownership. Three real shapes, each a repo whose own registry unit is
    keyed differently."""
    _registry(tmp_path, monkeypatch, {
        "lambda-kit": {"repos": ["convivy-app-kit", "convivy-services"],
                       "activation_lanes": ["daytime"]},
        "bosun": {"repos": ["agent-platform"], "activation_lanes": ["daytime"]},
        "steward": {"repos": ["substrate"], "activation_lanes": []},
    })
    assert crew_registry.activation_lanes_for_repo("convivy-app-kit") == ["daytime"]
    assert crew_registry.activation_lanes_for_repo("agent-platform") == ["daytime"]
    assert crew_registry.activation_lanes_for_repo("substrate") == []


def test_activation_lanes_for_repo_is_empty_for_an_unclaimed_repo(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {"repos": ["alpha"]}})
    assert crew_registry.activation_lanes_for_repo("not-a-repo") == []


def test_activation_lanes_for_repo_is_empty_when_the_field_is_absent(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {"repos": ["alpha"]}})
    assert crew_registry.activation_lanes_for_repo("alpha") == []


def test_activation_lanes_for_repo_is_empty_on_an_unreadable_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    assert crew_registry.activation_lanes_for_repo("alpha") == []


# --- company_workspace (CQ-171 slice 6) -------------------------------------

def test_company_workspace_is_the_one_units_primary_repo(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "example-commercial": {"repos": ["example-commercial"], "company_workspace": True},
        "alpha": {"repos": ["alpha"]},
    })
    assert crew_registry.company_workspace() == "example-commercial"


def test_company_workspace_is_none_with_no_match(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {"repos": ["alpha"]}})
    assert crew_registry.company_workspace() is None


def test_company_workspace_is_none_with_more_than_one_match(tmp_path, monkeypatch):
    """Ambiguous — a caller should apply no alias rather than guess."""
    _registry(tmp_path, monkeypatch, {
        "alpha": {"repos": ["alpha"], "company_workspace": True},
        "beta": {"repos": ["beta"], "company_workspace": True},
    })
    assert crew_registry.company_workspace() is None


def test_company_workspace_is_none_when_the_marked_unit_owns_no_repo(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {"company_workspace": True}})
    assert crew_registry.company_workspace() is None


def test_company_workspace_is_none_on_an_unreadable_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    assert crew_registry.company_workspace() is None


# --- repo_display_names (CQ-171 slice 6) ------------------------------------

def test_repo_display_names_reads_a_repo_display_map(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "beta": {"repos": ["beta", "beta-landing"],
                 "repo_display": {"beta-landing": "Beta Landing"}},
    })
    assert crew_registry.repo_display_names() == {"beta-landing": "Beta Landing"}


def test_repo_display_names_ignores_a_key_outside_the_units_own_repos(tmp_path, monkeypatch):
    """A stale or mistyped `repo_display` key must not hand a label to a
    repo nobody claims."""
    _registry(tmp_path, monkeypatch, {
        "beta": {"repos": ["beta"], "repo_display": {"not-betas-repo": "Nope"}},
    })
    assert crew_registry.repo_display_names() == {}


def test_repo_display_names_merges_across_units(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "alpha": {"repos": ["alpha", "alpha-landing"],
                  "repo_display": {"alpha-landing": "Alpha Landing"}},
        "beta": {"repos": ["beta", "beta-landing"],
                 "repo_display": {"beta-landing": "Beta Landing"}},
    })
    assert crew_registry.repo_display_names() == {
        "alpha-landing": "Alpha Landing", "beta-landing": "Beta Landing"}


def test_repo_display_names_is_empty_with_no_repo_display_map(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {"repos": ["alpha"]}})
    assert crew_registry.repo_display_names() == {}


def test_a_missing_registry_raises_for_repo_display_names(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.repo_display_names()


# --- sandbox_repos (CQ-171 slice 6) -----------------------------------------

def test_sandbox_repos_includes_a_repo_also_listed_in_repos(tmp_path, monkeypatch):
    """Today's shape for one of the two real sandbox repos: a repo that is
    ALSO a declared member of its unit's own `repos`."""
    _registry(tmp_path, monkeypatch, {
        "alpha": {"repos": ["alpha", "alpha-ownerrez-sandbox-tools"],
                  "sandbox_repos": ["alpha-ownerrez-sandbox-tools"]},
    })
    assert crew_registry.sandbox_repos() == frozenset({"alpha-ownerrez-sandbox-tools"})


def test_sandbox_repos_includes_a_marker_outside_repos(tmp_path, monkeypatch):
    """Today's shape for the other real sandbox repo: a bare marker naming
    a repo no unit's `repos` otherwise claims."""
    _registry(tmp_path, monkeypatch, {
        "beta": {"repos": ["beta"], "sandbox_repos": ["beta-sevenshifts-sandbox-tools"]},
    })
    assert crew_registry.sandbox_repos() == frozenset({"beta-sevenshifts-sandbox-tools"})


def test_sandbox_repos_unions_across_units(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "alpha": {"repos": ["alpha"], "sandbox_repos": ["alpha-sandbox"]},
        "beta": {"repos": ["beta"], "sandbox_repos": ["beta-sandbox"]},
    })
    assert crew_registry.sandbox_repos() == frozenset({"alpha-sandbox", "beta-sandbox"})


def test_sandbox_repos_is_empty_with_no_unit_carrying_the_field(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {"alpha": {"repos": ["alpha"]}})
    assert crew_registry.sandbox_repos() == frozenset()


def test_a_missing_registry_raises_for_sandbox_repos(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_JSON", str(tmp_path / "absent.json"))
    with pytest.raises(crew_registry.RegistryUnavailable):
        crew_registry.sandbox_repos()
