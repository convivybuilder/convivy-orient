---
id: platform/decisions/0040-a-dispatched-worker-may-not-install-into-a-shared-venv
kind: decision
project: platform
title: A dispatched worker may not install into a shared venv, because four live services are inside the blast radius
status: current
topics: [worktrees, dispatch, agentctl, venv, deployment, dependabot]
audience: [team, operator, coder, reviewer, security-reviewer, planning-orchestrator, overnight-orchestrator]
applies_to:
  - lib/agent-platform/worktrees.py
last_reviewed: 2026-08-13
authors: [keel-co]
---

# A dispatched worker may not install into a shared venv

## Decision

`agentctl` refuses `pip install` inside a worktree whose `.venv` is a symlink to
a primary checkout, and a worker that genuinely needs to install gets a real venv
of its own for that run rather than an exemption.

The sous-chef co routed this call here on 2026-08-10 with three options and a
recommendation for the per-product one. **The measurement changed the answer**,
so this takes the platform-wide option the note listed as (a).

## What the note had right, and the one fact it was missing

`_link_primary_venv` symlinks the primary checkout's `.venv` into every worktree
`agentctl` allocates. Its own docstring names the cost: every worktree of a repo
shares one writable venv, so a worker running `pip install` mutates every
concurrent worker's environment and the operator's interactive Python for that repo. That
was recorded deliberately, to close the interpreter-skew bug where a dispatched
worker resolved bare `pytest` to Homebrew's under a different Python.

The note observed that on sous-chef the shared venv is **also the live serving
environment**, because `deploy/autodeploy.conf` sets `VENV="${REPO_ROOT}/.venv"`
and the mini serves from the primary checkout. Verified: the running service is
`$HOME/Convivy/sous-chef/.venv/bin/uvicorn` on port 8300.

It then reasoned that a per-product fix "fixes only the product that happens to
serve from its checkout". **That is the fact to check, and it is wrong.** Four
services on this machine serve from a primary checkout's `.venv`, and all four
are `agentctl` dispatch targets:

| service | serving interpreter | port |
|---|---|---|
| sous-chef | `sous-chef/.venv/bin/uvicorn` | 8300 |
| selector | `selector/.venv/bin/uvicorn` | 8000 |
| convivy-services | `convivy-services/.venv/bin/uvicorn` | 4100 |
| library | `library/.venv/bin/python -m uvicorn` | 8100 |

Serving from the checkout is not a sous-chef quirk. It is the default shape of
every Python service Convivy runs, so the option that reads as a narrow patch is
four migrations that still leave the shape in place for the fifth service anybody
stands up.

`agent-platform` is a fifth instance of the same class by a different mechanism:
its installed bins are symlinks into the working tree, so a feature branch there
makes unmerged code live to cron. That one is already known and guarded
separately; it is named here because it shows the class is recurring rather than
incidental.

## Why the platform-wide guard rather than a venv per worktree

A real venv per worktree removes the sharing outright and needs no guard, which
is why the note listed it. It is refused on cost that is specific and measurable:
these repos carry heavy dependency trees — the Library's preflight alone imports
`fastembed` and an ONNX runtime — so seeding a venv per allocation costs minutes,
on every worktree, forever. The guard costs nothing until somebody installs.

## What the guard must not do

**It must not simply block the case that motivates most installs.** Reviewing a
dependency bump requires installing the bumped pins to test them, dependency PRs
arrive weekly, and a guard that refuses the main legitimate reason a worker
installs is a guard workers route around. So the refusal names the way through:
the worker materialises its own real venv for that run, pays the seeding cost
once, and installs into something nobody is serving from. Expensive only when
needed is the whole point of preferring this to a venv per worktree.

## How close this got

Reviewing sous-chef dependabot PR #439 installed `fastapi 0.141.1`, `alembic
1.19.0` and five others into the live serving venv. The reviewer noticed, put the
original pins back, and the restore was confirmed. Nothing broke, and the
recovery depended entirely on one reviewer choosing to undo its own side effect;
a worker that installs and exits leaves production on mutated dependencies with
no signal anywhere.

That this product breaks that way is not hypothetical. sous-chef PR #428 pruned
`sniffio` from the deployed venv and the app served HTTP 500 on every request for
about an hour, reverted by #429 with `Revert-reason: bug`.

## Consequences

- `lib/agent-platform/worktrees.py` keeps `_link_primary_venv`; the
  interpreter-skew fix it exists for is unaffected, and the link stays the
  default.
- The docstring's recorded blast radius is corrected where it stands. It says
  "every concurrent worker's environment and the operator's interactive Python"; on four
  repos it is also the running service, and the phrase "The operator's interactive Python"
  hides that, because on a product whose checkout is its deploy root they are one
  interpreter.
- Separating each service's deploy venv from its dev venv stays worth doing and
  is not what this decides. It is already filed for sous-chef
  (`issue_01KZA4Q1H6VQCH0AS1V8RVA11X`), it belongs to each product's crew, and it
  removes the consequence where this removes the surprise. Neither substitutes
  for the other.

## Related

- [[platform/decisions/0028-verify-in-the-state-the-change-produces]] — the
  instinct that settled this one: the note's recommendation rested on a claim
  about how many products were affected, and counting them answered it.
