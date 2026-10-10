---
name: handoff-pickup
description: The operator invokes this at the start of a fresh OpenCode session to resume
  a prior session's work; pairs with handoff-leave. OpenCode-trial port of the
  co handoff-pickup skill (ARCH-133).
---

# handoff-pickup — resume from the most recent handoff

The operator started a fresh OpenCode session and wants to pick up where a prior
session left off.

## Finding the handoff

```bash
ls -t "$HOME"/Convivy/agent-runs/*/oc-convivy-orient-*/handoff.md 2>/dev/null | head -5
```

Filter to entries from the last 48 hours (parse the date directory). Take the
newest.

- **One match:** read it, surface as below.
- **Multiple in 48h:** the newest is almost always right — surface it as the
  default and list the other paths.
- **No match:** also check the co-side pattern
  `*/co-convivy-orient-*/handoff.md` (a prior session may have run under
  Claude Code). If still nothing: "No handoff found for convivy-orient in the
  last 48 hours. Starting fresh — what would you like to do?" Don't search
  older history.

## Surface to the operator

Brief structured ack — don't dump the handoff verbatim:

> Resuming from `<full absolute path>` (left ~N hours ago).
>
> **State:** <one sentence from the handoff>
> **In-flight:** <abbreviated bullets>
> **Recommended next move:** <verbatim from handoff>
>
> Proceeding with the recommended next move unless you redirect.

Paths surfaced to the operator are always full absolute form starting from the home directory (`/Users/<you>/...`),
never `~/` (ARCH-043).

## After pickup

Leave the handoff doc in place — it's an audit trail. If it references an
in-flight PR, check the PR's current state before proceeding; the handoff is
last-known state, not current reality.
