---
id: platform/instruction-blocks/never-branch-in-a-shared-checkout
kind: instruction-block
project: platform
title: Never branch in a shared checkout
status: current
scope: fleet
audience: [team]
topics: [git]
last_reviewed: 2026-09-24
authors: [steward]
---

A checkout that something else reads from is shared. `$HOME/Convivy/knowledge` is what the knowledge index serves to every agent, and `agent-platform`'s `bin/` and `lib/` are symlinked into `~/.local` and run by scheduled jobs. A few files in `~/.local/lib/agent-platform` are regular files rather than symlinks, so neither a branch checkout nor a merge reaches them. Never check out a branch in a shared checkout. Do branch work in a worktree:

```
git -C <repo> worktree add <path> -b <branch> origin/main
```

A branch in a shared checkout publishes unmerged work as current, stops newly merged work from arriving, and gets counted as merged history by any tool that runs `git log` without a ref. Two sessions in one tree fight over it, and a session that finds changes it did not make tends to discard them.

A knowledge document is never changed on a branch at all: it changes through the Library's `kb` tools. In the knowledge repo, the worktree above is for reading a tree at `origin/main`, or for changing code outside `projects/`.

When you dispatch work that will branch, name the worktree in the prompt. A worker uses the only checkout it knows.

Never switch a branch another session is using. Ask that session to move to a worktree.

To rescue a branch already in a shared checkout, or to repair the knowledge index, read `platform/runbooks/rescue-a-branch-and-repair-the-knowledge-index` with `kb_get` first and follow its order. Before touching the shared checkout, run `git -C <repo> status --porcelain` against that checkout, never your own working directory; if it prints anything, stop and ask rather than checking out over it. Never use `agentctl kb sync --verify-only`: it reports whatever tree is checked out as healed.
