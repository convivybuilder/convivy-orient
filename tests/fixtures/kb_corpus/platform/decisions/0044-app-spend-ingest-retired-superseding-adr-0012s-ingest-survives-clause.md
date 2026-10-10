---
id: platform/decisions/0044-app-spend-ingest-retired-superseding-adr-0012s-ingest-survives-clause
kind: decision
project: platform
title: "App spend moves to the Chart House; /ingest/llm-spend is retired, superseding ADR-0012's ingest-survives clause"
status: current
topics: [llm-spend, ingest, chart-house, convivy-orient, metering, keel]
audience: [team, operator, architect, platform-architect, platform-planner, platform-coder, coder, reviewer, security-reviewer]
applies_to: []
depends_on:
  - platform/decisions/0012-server-side-llm-metering-plane
last_reviewed: "2026-08-18"
authors: [orient-co]
---

## The decision

**Orient's `/ingest/llm-spend` route is retired** (the operator, 2026-08-18), superseding
ADR-0012's consequence that it "survives for Constance and Vivy." App-level LLM
spend moves to the Chart House instead, joining the ledgers a project already
draws on (`company/overview/convivy-builder`), rather than staying a
standalone file-based ingest Orient holds.

## Why the earlier call no longer holds

ADR-0012 framed Constance's and Vivy's non-adoption of the endpoint as a gap
to close, not a reason to remove it. Two months on, checked live on
2026-08-18: the endpoint has taken 14 requests in its whole lifetime, none
since 2026-06-17, and the one row it ever wrote was a setup test. No app
depends on it today — Sous Chef reports through the kit's own ledger now
(`app/ai/recipe_extract.py:373`), Constance no-ops without three unset
environment variables and is about to be re-architected, and Vivy has no
in-app LLM calls yet.

Orient's own retirement (`substrate/keel-build-plan.md`) is the other half of
the reason: Orient should hold no write path over vault-adjacent data at all.
This was the second of two ingest endpoints it held for exactly that reason —
the first, CI results, is `issue_01M0B92VVV7NEH3NK068ZJ33Y2`.

## What changes

- `POST /ingest/llm-spend` is deleted from `convivy-orient/server.py`
  (`issue_01M0B8Q15RAFEWRVT0MFRKNGG1`). It answers 404, like any other
  unmounted path.
- The write path an app should eventually use is the Library's Chart House,
  once it exists — not built yet, and not Orient's to build.
- `platform/reference/llm-spend-ingest-contract` and
  `platform/runbooks/llm-spend-ingest-setup` are marked superseded by this
  document: both name Orient as the ingest endpoint and would otherwise
  mislead a reader into re-provisioning something retired.
- The spending dashboard's app lane keeps reading whatever rows already
  landed in `~/Convivy/agent-runs/llm-spend/*.jsonl` (frozen, not growing)
  until it can read from the Library instead.

## Relates to

Supersedes the `/ingest/llm-spend` clause of
[[platform/decisions/0012-server-side-llm-metering-plane]] only — the rest of
that ADR (server-side gateway metering for the device-app suite: Selector,
Hollis, Sous Chef) is unaffected.
