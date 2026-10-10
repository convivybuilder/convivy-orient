---
id: platform/decisions/0083-the-review-cycle-runs-five-rounds-with-no-carve-out
kind: decision
project: platform
title: "The review cycle runs seven rounds with no carve-out, and the cap still hands the call to the operator"
status: current
topics: [review, review-cycle, reviewer, security-reviewer, pull-requests, agentctl]
audience: [team, operator, coder, reviewer, security-reviewer, planning-orchestrator]
applies_to:
  - bin/agentctl
  - lib/agent-platform/kb_merge.py
  - projects/platform/policy/agent-policy.yaml
supersedes:
  - platform/decisions/0037-the-review-cycle-hard-stops-at-three-rounds-and-only-a-behavioural-finding-earns-a-fourth
last_reviewed: "2026-10-01"
authors: [operator, steward]
---

**A PR's review/revise/review cycle runs at most SEVEN rounds** (the operator, 2026-10-01, in the steward
session: "let's bump our 5 round limit to 7 across all projects"). From 2026-09-14 the cap was five
(the operator, in the steward session: "i think I'd like to up the 3 round review limit to 5 rounds", then
"5 rounds, no carve-out" and "same rule, unchanged at 5"); only the number changed on 2026-10-01.

**There is no carve-out above the cap.** A behavioural finding earns no extra round, and neither does
a security claim. Seven rounds, then the cycle stops.

**At the cap the operator decides whether the next round happens, exactly as at three.** When round
seven returns a finding, stop: bring it to the operator with the fix ready and a recommendation, and dispatch
nothing until they say "one more", "merge as is", or "close". A round past the cap says so in the PR
body and names whose authorization it runs on.

> **AMENDED on 2026-09-19 by `platform/decisions/0086`.** The paragraph above holds for a PR that
> changes code. **For a PR that changes only documents, the operator's option is withdrawn**: there
> is no round past the cap and nobody may authorize one. That withdrawal is the operator's, stated 2026-09-18. The
> cap itself is the same for both, seven since 2026-10-01. **`0086` also defines which PRs count as document-only,
> and that definition is the steward's rather than the operator's and is not ratified** — a file an agent is
> told to act on is outside the class, this record included.

## What this replaces

`platform/decisions/0037` set the cap at three and admitted a fourth round for a finding that changes
what the code does, plus a carve-out for a claim that materially misstates a security control. Both
exceptions are gone. **The reason to drop them is that each needed a judgment call — is this finding
behavioural? does this comment misstate a control? — made at the moment everyone is most tired of the
PR.** A number with nothing above it needs no classification and cannot be argued with. A higher cap
gives what the carve-outs were reaching for, and gives it before the cap rather than after.

Everything 0037 said about what a late finding becomes still holds: a comment, a docstring, a doc
claim, a naming preference or a test's wording that survives to the cap becomes a follow-up, filed
and shipped separately, subject to the filing bar in `platform/decisions/0068`.

## What enforces it

**One place in code, and it covers one operation.** `lib/agent-platform/kb_merge.py` refuses
`agentctl kb merge` when a role's round count exceeds the cap, which gates the Library merging its
own `kb/` PRs on `convivy/knowledge` (`platform/decisions/0078`). **The number is
`review_rounds_per_role` in `projects/platform/policy/agent-policy.yaml`, read at origin/main**;
when that file is missing or breaks its own rules the reader falls back to 5
(`FALLBACK_REVIEW_ROUNDS_PER_ROLE` in `lib/agent-platform/agent_policy.py`). The check is
`n > cap`, so it refuses at eight rounds and not at seven; at exactly seven what refuses a capped PR
is `review-status` not reading PASS.

**Every other repository's cycle has no code gate at all.** `agentctl review-status` computes a
verdict from stamps and CI and counts no rounds. The cap holds on those PRs because each co honours
it, and saying so plainly is better than implying a guard that is not there.
