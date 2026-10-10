---
name: handoff-leave
description: The operator invokes this when ending an OpenCode session that has work in
  flight or context worth preserving for a future session; pairs with
  handoff-pickup. OpenCode-trial port of the co handoff-leave skill (ARCH-133).
---

# handoff-leave — write a handoff doc before exit

The operator is about to exit this OpenCode session. Capture session state in a
structured doc so a future session (OpenCode or co) can pick up and continue.
Keep it tight — this is a handoff to yourself, not a deliverable.

## Where to write

```
$HOME/Convivy/agent-runs/<YYYY-MM-DD>/oc-convivy-orient-<HHmm>/handoff.md
```

(Absolute path. Do not surface `~/` paths to the operator — their cross-device launcher
misexpands `~`, per ARCH-043.)

- `<YYYY-MM-DD>` from `date +%Y-%m-%d` (local time).
- `<HHmm>` from `date +%H%M`.
- Create the directory with `mkdir -p`. The `oc-` prefix marks an
  OpenCode-trial handoff; never reuse an existing run-id directory.

## What to write

Use this exact section structure so handoff-pickup knows where to look. Be
specific in each section; vague "we worked on stuff" is the failure mode.

```markdown
# OpenCode session handoff

**Product:** convivy-orient (OpenCode trial, ARCH-133)
**Started:** <approximate session start time>
**Left:** <date +%Y-%m-%d %H:%M %Z>
**Why exiting:** <one line>

## State summary

<2–4 sentences. Goal of the session, current state, most actionable framing.>

## In-flight items

<For each: what it is, where it lives (PR URL / file path), what step it's at.>

## Decisions made this session

<Meaningful choices only; "None." if none.>

## Open questions / blockers

<"None." if none.>

## Recommended next move

<One short paragraph, specific.>

## Trial friction observed

<OpenCode-specific: any capability gap, workaround, or "Claude Code would
have done X" moment from this session, for the steward's trial journal.
"None." if none.>
```

## On completion

Surface a one-line ack to the operator with the full path. Don't ask if the doc looks
right; they can edit it. Don't post to Slack.

## When NOT to leave a handoff

The session has no in-flight work and no non-obvious context. Say "no handoff
needed — nothing to carry forward" and exit cleanly.
