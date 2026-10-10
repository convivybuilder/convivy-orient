---
id: platform/runbooks/rescue-a-branch-and-repair-the-knowledge-index
kind: runbook
project: platform
title: Runbook — rescue a branch from a shared checkout, and repair the knowledge index
status: current
audience: [team]
topics: [git]
last_reviewed: 2026-09-29
authors: [steward]
---

# Rescue a branch from a shared checkout, and repair the knowledge index

The rule this serves is `platform/instruction-blocks/never-branch-in-a-shared-checkout`, which every session loads. These steps moved here from that block on 2026-09-29 to shrink the startup load; the block keeps the stop-and-ask check and the `--verify-only` prohibition inline.

## Rescue a branch already in a shared checkout

The order matters, because `worktree add` refuses a branch still checked out somewhere:

1. `git -C <repo> status --porcelain`. If it prints anything, stop and ask rather than checking out over it.
2. `git -C <repo> checkout main`, which frees the branch.
3. `git -C <repo> worktree add <path> <branch>`.
4. For the knowledge repo, `agentctl kb sync`, which reports `up to date at <sha>, verified — N docs indexed`.

## Repair the knowledge index

Reconcile from a detached worktree at `origin/main`:

```
git -C ~/Convivy/knowledge worktree add -d <path> origin/main
KNOWLEDGE_ROOT=<path> python3 -c "import indexer; indexer.full_scan_and_reconcile()"
```

Never use `agentctl kb sync --verify-only`: it reconciles against whatever tree is checked out and reports that as healed. Confirm the repair against served content by querying `~/.knowledge-index/index.db` (table `docs`, column `body`) for text that exists only in the version you expect.
