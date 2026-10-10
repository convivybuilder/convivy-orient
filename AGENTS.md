# convivy-orient — agent orientation

This repo builds and serves **team.convivy.com**, the private team orientation
site: a read-only browse view of the Convivy knowledge base plus three
full-tier-only dashboards (`/dashboard` cost+quality, `/health` code health,
`/kb` retrieval quality). It is generated Python + self-contained HTML/JS,
served by `server.py` on the mini behind a Cloudflare Access tunnel. Read
`README.md` for the full piece-by-piece map before changing anything.

## Working rules (binding)

- **Never merge a PR, and never push to `main`.** Land every change on a
  feature branch and open a PR; CI (ruff + pytest with coverage) gates it and
  the operator merges in the GitHub UI. A plugin enforces this at the bash layer — if a
  command is blocked, surface the PR URL instead of retrying.
- **Run the full CI set locally before pushing a branch:** `ruff check .` and
  `python -m pytest`. Both must be green.
- **The site is live.** Six of the seven launchd jobs, including the server
  itself, run CODE from `~/.local/deploy/convivy-orient` — a separate area
  `bin/install-orient` builds, not this checkout (ADR-0059) — but `serve.sh`
  still reads/writes CONFIG, STATE, and this working tree's `site/` output
  here, via `ORIENT_CONFIG_ROOT`/`ORIENT_STATE_ROOT`. Do not run destructive
  cleanup on `site/`, and do not restart launchd jobs unless the task is
  specifically about them.
- **Secrets:** `jwt.json`, `tiers.json` and `roles.json` hold access-control
  data. They are gitignored; never `git add -f` them, and never print their
  contents into logs, commits, or PR bodies.
- Dashboards are self-contained single-file HTML by design — keep new
  features dependency-free and client-side unless the task says otherwise.

## Context

- The KB index this site renders lives at `~/.knowledge-index/index.db`; the
  `kb` MCP server (`kb_search`, `kb_get`, `kb_list_topics`) queries the same
  knowledge base — use it to look up Convivy docs, ADRs, and agent profiles.
- Tests live in `tests/`; every dashboard builder has a sibling test file.
  Match the existing test style when adding coverage.
- This repo is part of an OpenCode trial (substrate ARCH-133). If you hit a
  capability gap or harness friction, say so explicitly in your reply so it
  lands in the trial journal.
