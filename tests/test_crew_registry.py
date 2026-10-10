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
