---
id: platform/contracts/agentctl-models
kind: reference
project: platform
title: "Contract — agentctl models --json, the list of models a dispatch accepts"
status: current
topics: [model-routing, agentctl, dispatch, deck, orient, contracts]
audience: [team, operator, platform-coder, deck-coder, orient-coder, reviewer, security-reviewer]
depends_on:
  - platform/contracts/work-item-model-override
last_reviewed: "2026-09-28"
authors: [bosun]
---

# Contract — `agentctl models --json`, the list of models a dispatch accepts

`agentctl models --json` prints the models this platform serves, so a surface that lets an
operator choose a model can offer only names a dispatch will accept. It exists because Deck's
Item pane took free text: on 2026-09-28 the operator entered `claude-opus`, the Library stored it, and
`agentctl spawn --issue` refused it at dispatch with "resolves to 'claude-opus', which this
platform does not serve" (`issue_01M3N0F35X7QQKMZKKHQ6AR1RG`).

What a stored choice means once a dispatch reads it is specified in
[[platform/contracts/work-item-model-override]] and is not restated here.

## Producer and consumers

| | |
|---|---|
| Producer | `agentctl models` in `bin/agentctl` (the Bosun) |
| Consumers | Deck's model picker on the Item pane (`issue_01M3N0FA1C1AKMTP83WW08E054`), over its existing SSH transport; Orient's queued-item model override (convivy-orient #334), run on the machine that dispatches |
| Transport | a process: run the command, read stdout, check the exit code |

## The command

`agentctl models --json` takes no other arguments. It is read-only: it reads two tables
compiled into `bin/agentctl` and nothing else. It makes no network call, reads no credential,
does not contact the Library or the gateway, and writes nothing.

On success it exits 0 and prints exactly one JSON object on stdout, followed by a newline. On
any failure it exits non-zero, prints nothing on stdout and says why on stderr. A consumer
treats a non-zero exit, or stdout that does not parse, as "list unavailable", and must not
fall back to accepting free text as though it were valid.

`agentctl models` without `--json` prints the same list as a table for a person. Its format is
not part of this contract.

## The output

```json
{
  "schema_version": 1,
  "models": [
    {"alias": "opus",     "model": "claude-opus-5",              "provider": "anthropic"},
    {"alias": "deepseek", "model": "deepseek/deepseek-v4-flash", "provider": "gateway"},
    {"alias": null,       "model": "claude-opus-4-8",            "provider": "anthropic"}
  ]
}
```

The three entries above illustrate the shape. The live list is whatever the command prints.

- `schema_version` is the integer `1`. A change that removes or renames a field, or changes a
  field's type or meaning, raises it. Adding a field does not. A consumer refuses a version it
  does not know, and ignores fields it does not know.
- `models` is an array with one object per selectable choice.
  - `alias` is the short name `MODEL_ALIASES` maps to this model, or `null` when the model has
    no alias.
  - `model` is the resolved model id, the value `model_id()` returns and `_model_known()`
    judges.
  - `provider` is `"anthropic"` for a model Claude Code runs directly, or `"gateway"` for one
    routed through the LiteLLM gateway (`is_gateway_model()` is true).

**The order is fixed.** Aliased models come first, in `MODEL_ALIASES` order. Then come the ids
in `_MODEL_TIERS` that no alias maps to, in `_MODEL_TIERS` order, with `alias: null`. Each
`model` appears exactly once. A consumer may show the list in this order without sorting.

## The guarantee, and its limits

**Every `model` in the list passes `_model_known`, and the list is built from the same tables
`_model_known` reads** (`MODEL_ALIASES` and `_MODEL_TIERS`), so the picker and the dispatch
check cannot disagree about a listed name. A test in the agent-platform suite pins that every
listed `model` passes `_model_known`, and that every alias value and every tier id appears.

The list is complete for those two tables, but it does not cover everything `_model_known`
accepts. `_model_known` also accepts any literal the gateway's prefix match recognises, even
with no alias or tier entry. The list names only the gateway models an alias maps to. A picker
built from it offers a subset of the accepted names, never a superset.

A listed model can still be refused at dispatch for reasons this list does not cover:

- a gateway model whose credential is missing, or whose provider is unreachable (conditions 2
  and 3 of the override preflight in [[platform/contracts/work-item-model-override]]);
- anything else that contract says the Bosun checks when it reads a stored choice.

The list says which names the platform serves. It says nothing about which models a roster may
pin an agent on, or about price.

## What a consumer stores

Store the entry's `model`, the resolved id. That is what the dispatch compares and records. An
alias can be repointed (`sonnet` moved from `claude-sonnet-4-6` to `claude-sonnet-5` on
2026-08-08), and a stored resolved id keeps meaning the model the operator picked.
