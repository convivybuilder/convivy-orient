#!/usr/bin/env python3.11
"""The models a dispatch can run on, read from `agentctl models --json`
(contract: platform/contracts/agentctl-models; issue_01M3NWDHCDQK6PE9NEXFQ37G9K).

The item page's model override offers exactly this list, and its route refuses
anything else. A free-text field let Deck store `claude-opus`, which no
dispatch accepts, and the item was then refused when a run took it. So the
list comes from the dispatcher itself, in the order it gives, and an answer
this module cannot trust counts as no list at all rather than falling back to
free text: a non-zero exit, stdout that does not parse, or a
`schema_version` other than the one this reader understands.

Read per call with no cache. The command took 0.08 s on macstudio (2026-09-29),
and only a queued item's page and a set request ask for it.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import NamedTuple

import paths

SCHEMA_VERSION = 1

_TIMEOUT_S = 10

# kb_commit's allowlist and for the same reason: the deploy-pinned agentctl
# needs PATH to find its python3.11 and HOME for its own config, and nothing
# else from Orient's environment should reach it.
_ENV_ALLOWLIST = ("HOME", "PATH")


class Model(NamedTuple):
    model: str
    alias: str | None
    provider: str


def _parse(stdout: str) -> list[Model] | None:
    try:
        doc = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
        return None
    rows = doc.get("models")
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        model, alias, provider = row.get("model"), row.get("alias"), row.get("provider")
        if not (isinstance(model, str) and model and (alias is None or isinstance(alias, str))
                and isinstance(provider, str)):
            return None
        out.append(Model(model=model, alias=alias, provider=provider))
    return out


def load() -> list[Model] | None:
    """The dispatchable models in agentctl's order, or None when the list is
    unavailable."""
    agentctl = paths.resolve_tool("agentctl")
    if agentctl is None:
        return None
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    try:
        r = subprocess.run([agentctl, "models", "--json"], capture_output=True,
                           text=True, timeout=_TIMEOUT_S, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return _parse(r.stdout)
