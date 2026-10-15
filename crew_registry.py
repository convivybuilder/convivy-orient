#!/usr/bin/env python3.11
"""The crew keys a dispatch looks up its tool policy under, read from the
project registry agentctl reads.

`agentctl spawn` resolves a run's crew with `projects.crew_for_repo`, which
returns a registry unit's `crew` field, and asks the Library for the policy of
(agent, THAT crew, model). The policy table is exact-match, so a row written
under any other string binds nothing. Project slugs are not crew keys: the
`agent-platform` repo's crew is `bosun`, and `convivy-deck`'s is `deck`.

**The same file agentctl reads.** The deployed agentctl has no `config/` of its
own, so `projects._registry_path()` falls back to the agent-platform working
checkout's `config/projects.json`. Reading anything else could disagree with
the key a dispatch uses, which is the one thing this module exists to match.
`PROJECTS_JSON` overrides it, as it does for agentctl.

**A private crew is never listed** (ADR-0043's black-box crews). A unit is
private when its `private` field is `True`, read the way
`projects.private_slugs()` reads it.

A missing registry (CI, or a machine without the agent-platform checkout)
raises `RegistryUnavailable`, so a caller can say the list is unavailable
rather than offer an empty one.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REGISTRY = Path(os.environ.get(
    "PROJECTS_JSON", str(Path.home() / "Convivy" / "agent-platform" / "config" / "projects.json")))


class RegistryUnavailable(Exception):
    """The registry file could not be read."""


def public_crews() -> list[str]:
    """Sorted, distinct crew keys of every active, non-private registry unit."""
    try:
        raw = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except OSError as e:
        raise RegistryUnavailable(str(e)) from e
    crews: set[str] = set()
    for slug, unit in raw.items():
        if slug.startswith("_"):
            continue
        if unit.get("private") is True or unit.get("lifecycle") != "active":
            continue
        crew = unit.get("crew")
        if isinstance(crew, str) and crew.strip():
            crews.add(crew.strip())
    return sorted(crews)


def covers_every_project(scope: frozenset[str]) -> bool:
    """Whether `scope` holds every project in the registry, `_`-prefixed keys
    (`_meta`, `_products`, `_scopes`) excluded and private units included.

    This gates the fleet-wide figures that carry no project of their own
    (E19·3, issue_01M35K5EAZD85FBYXEBQVRKVZV): a viewer sees them only when
    their Library scope already covers everything those figures sum over.
    Scope decides it, never a role (E19). An unreadable registry answers
    `False`, so a machine without the agent-platform checkout shows those
    figures to nobody."""
    try:
        raw = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except OSError:
        return False
    projects = {slug for slug in raw if not slug.startswith("_")}
    return bool(projects) and projects <= scope


def repos_in_scope(scope: frozenset[str]) -> frozenset[str]:
    """Every repo the registry lists under a project in `scope`.

    A repo's name is not its project's slug: `agent-platform` belongs to
    `bosun` and `knowledge` to `library`, so a per-repo row is filtered
    against this set rather than against `scope` itself (E19·3,
    issue_01M35K5EAZD85FBYXEBQVRKVZV). An unreadable registry answers the
    empty set, so no repo row reaches anyone."""
    try:
        raw = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except OSError:
        return frozenset()
    return frozenset(
        repo
        for slug, unit in raw.items()
        if not slug.startswith("_") and slug in scope
        for repo in unit.get("repos") or ())


def crews_in_scope(scope: frozenset[str]) -> frozenset[str]:
    """Every crew key the registry lists under a project in `scope` — the
    same `crew` field `public_crews()` collects, restricted the way
    `repos_in_scope` restricts repos (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M).
    A crew key is not its project's slug either (`agent-platform`'s crew is
    `bosun`), so this is filtered the same way: by the project slugs in
    `scope`, not by the crew keys themselves. A private unit's crew is
    included, matching `repos_in_scope`'s own choice not to special-case
    `private` — unlike `public_crews()`, which is a suggestion list over
    every NON-private unit regardless of viewer. An unreadable registry
    answers the empty set, so no crew name reaches anyone."""
    try:
        raw = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except OSError:
        return frozenset()
    return frozenset(
        unit["crew"].strip()
        for slug, unit in raw.items()
        if not slug.startswith("_") and slug in scope
        and isinstance(unit.get("crew"), str) and unit["crew"].strip())
