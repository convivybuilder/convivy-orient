#!/usr/bin/env python3.11
"""Roster model alias → resolved model id (issue_01M2HVGKSNFMHZF0FDN8FBT9N6).

`bin/agentctl` (agent-platform) turns a roster `model:` field like `sonnet`
into the id a spawn runs under, and the dispatch path looks up the Library's
deny-list by that id. So the policy page must write and read the same id, or
an entry binds nothing while displaying as if it does.

The table comes from `agentctl models --json` through `model_catalog`, the
dispatcher's own answer. This module used to hold a hand-kept copy, and it
drifted: by 2026-09-29 it still mapped `opus` to `claude-opus-5` and `fable`
to `claude-fable-5` after agentctl had moved them to `claude-opus-5-5` and
`claude-fable-5-1`, so every policy entry written for the 41 roster agents
pinned to either alias was keyed on an id no dispatch looked up.

Both Orient's write boundary (`policy_api.py`) and its read/lookup path
(`policy_build.py`) must resolve a `model` value through the same table
before it reaches the Library.
"""
from __future__ import annotations

import model_catalog


class ModelListUnavailable(Exception):
    """`agentctl models` gave no usable answer, so an alias cannot be resolved.
    A caller that would write or look up a policy key must stop rather than
    guess, since a guessed key is the silent-miss this module exists to close."""


def aliases() -> dict[str, str]:
    """Alias → resolved id, from agentctl. Raises `ModelListUnavailable`.

    Load it once per request and pass it to `resolve`; each load runs
    agentctl."""
    catalog = model_catalog.load()
    if not catalog:
        raise ModelListUnavailable("agentctl models gave no usable answer")
    return {m.alias: m.model for m in catalog if m.alias}


def resolve(alias_or_id: str, table: dict[str, str]) -> str:
    """`alias_or_id`'s resolved model id, or `alias_or_id` unchanged if it is
    not a known alias — matching agentctl's own `model_id()` fallback, so an
    operator who already typed a resolved id (the page's own placeholder
    example, `claude-sonnet-5`) writes the same key either way, and a value
    the table does not recognise is passed through rather than dropped."""
    return table.get(alias_or_id, alias_or_id)
