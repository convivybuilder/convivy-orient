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
