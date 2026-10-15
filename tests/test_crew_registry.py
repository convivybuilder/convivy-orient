"""crew_registry.public_crews: the crew keys a dispatch looks its tool policy
up under, from the registry agentctl reads."""
from __future__ import annotations

import json

import pytest

import crew_registry


def _registry(tmp_path, monkeypatch, units):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"_meta": {"doc": "x"}, "_products": {}, **units}))
    monkeypatch.setattr(crew_registry, "REGISTRY", path)


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
        "vivy": {"crew": "vivy", "lifecycle": "active"},
        "ea": {"crew": "ea", "lifecycle": "active", "private": True},
    })
    assert crew_registry.public_crews() == ["vivy"]


def test_an_inactive_unit_and_a_null_crew_are_skipped(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "vivy": {"crew": "vivy", "lifecycle": "active"},
        "old": {"crew": "old", "lifecycle": "sunset"},
        "nocrew": {"crew": None, "lifecycle": "active"},
    })
    assert crew_registry.public_crews() == ["vivy"]


def test_a_crew_shared_by_two_projects_is_listed_once(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch, {
        "selector": {"crew": "selector", "lifecycle": "active"},
        "selector-backend": {"crew": "selector", "lifecycle": "active"},
    })
    assert crew_registry.public_crews() == ["selector"]


def test_a_missing_registry_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(crew_registry, "REGISTRY", tmp_path / "absent.json")
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
    monkeypatch.setattr(crew_registry, "REGISTRY", tmp_path / "absent.json")
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
    monkeypatch.setattr(crew_registry, "REGISTRY", tmp_path / "absent.json")
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
    monkeypatch.setattr(crew_registry, "REGISTRY", tmp_path / "absent.json")
    assert crew_registry.crews_in_scope(frozenset({"bosun"})) == frozenset()
