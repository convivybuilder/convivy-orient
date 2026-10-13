# convivy-orient

Orient is Builder's web view of the whole system. It shows the knowledge base's
documents, the work queue of epics and corrections, the crews and their
sessions, spending and alerts as pages a person can read and act on. Most pages
read their data live on each request. Each viewer gets a tier: the full tier
sees everything, and the contractor tier sees only the documents released to
contractors.

Builder's website is [convivybuilder.org](https://convivybuilder.org). It carries the documentation and the install pages.

## This deployment

Convivy runs Orient as its private team site at https://team.convivy.com,
behind Cloudflare Access, on a Mac Studio. The rest of this README describes
that deployment. (Substrate decision: ARCH-056, part of the KB-consolidation
initiative.)

## Pieces

- **`build.py`** — reads the KB index (`~/.knowledge-index/index.db`) and emits
  TWO bundles into `site/`: `index.full.html` (every doc) and
  `index.contractor.html` (contractor-visible subset — legal/company/strategy/
  platform-internal docs are physically absent, not hidden in-browser).
- **`server.py`** — identity-aware origin on `127.0.0.1:8099`. Reads the
  Cloudflare Access `Cf-Access-Authenticated-User-Email` header and serves the
  bundle for that email's tier. No header → 403.
- **`tiers.json`** — `{"full": [emails]}`. Listed emails get the full bundle;
  every other authenticated email gets contractor (least privilege). Re-read per
  request — add someone and it takes effect without a restart. It is gitignored,
  like `jwt.json` and `roles.json`; start each from its committed `.example`
  template.
- **`serve.sh`** — launchd entrypoint (`com.convivy.orient`): rebuilds both
  bundles + the dashboard, then runs `server.py`.
- **`dashboard_build.py`** — the agent **cost & quality dashboard** (ARCH-109).
  Normalizes the scorecard ledgers (`~/Convivy/agent-runs/scorecard/`:
  `scorecard.jsonl` + `hollis.jsonl` + `judge.jsonl`) into one fact
  per PR/run and emits two self-contained, standalone pages (ARCH-191, split so
  either can be screenshotted whole, each with its own date picker incl. a custom
  range): `site/spending.html` (**`/spending`** — TOTAL spend, every session) and
  `site/cost-performance.html` (**`/cost-performance`** — dev-crew cost-vs-quality
  scorecard). Both slice by product / agent / model / provider / time client-side.
  **Full tier ONLY** (cost/perf is sensitive — contractors get a plain 404, the
  routes aren't revealed); the old **`/dashboard`** 302-redirects to `/spending`.
  Refreshed daily at 07:40 by `com.convivy.orient-dashboard`
  (regenerates the HTML in place; the server reads it per request, no restart).
  Scope: dev-crew cost+quality. Hollis *runtime* LLM cost
  (`hollis/data/llm-spend.jsonl`) is a separate data-local feed, not
  folded in.
- **`code_health_build.py`** — the **code-health dashboard** (ARCH-111), sibling to
  the cost one. Harvests per-repo metrics live from `gh` + the local git checkouts
  (CI pass-rate, last CI status, open PRs + oldest age, merges/30d, reverts/30d,
  test count via a `def test_` grep heuristic) across the 3 products + 4 internal
  repos, and emits a self-contained `site/health.html` sliceable by repo/kind.
  Coverage is deferred (needs running coverage per repo). Served at **`/health`**,
  **full tier ONLY** (contractors 404). Refreshed daily at 07:45 by
  `com.convivy.code-health` (heavier gh harvest — kept off serve.sh so restarts
  stay fast; the server reads the file per request). `/cost-performance` ↔
  `/health` cross-link.
- **The signal-memory plane** (ARCH-349) — `signal_api.py` (POST/GET/search
  `/api/signals`: capture + scoped read + semantic search, with an
  internal/customer privacy wall), `embeddings.py` (local fastembed
  bge-small-en-v1.5 → sqlite-vec KNN, embedded at ingest, `embed_missing()`
  sweeps stragglers), and `github_capture.py` (org-wide GitHub review capture:
  review verdicts, review comments, PR/issue comments → signals, deduped by
  `gh:<repo>:pr<n>:<type>:<id>`). Incremental capture runs every 15 minutes via
  `com.convivy.orient-github-capture`; `--backfill` ignores the cursor and
  re-walks everything (safe — dedup absorbs it, and it also seeds the cursor —
  on a cold start with no cursor file, a repo's first incremental run is an
  unfiltered full pull, correct but slow). A repo's cursor advances only on a
  zero-error run; a failed gh fetch raises (never reads as an empty page), so
  no window is ever skipped past. Store: the control DB
  (`~/.orient-control/control.db`).

## Access path

```
browser → team.convivy.com (Cloudflare Access: Google SSO + email allowlist)
        → Cloudflare Tunnel `convivy-team` (launchd com.convivy.cloudflared)
        → 127.0.0.1:8099 (server.py) → per-tier bundle
```

The origin is loopback-only; the sole way in is the tunnel, which carries only
Access-authenticated traffic.

## Operate

- **Refresh content** (after KB changes): `launchctl kickstart -k gui/$(id -u)/com.convivy.orient` (rebuilds + restarts).
- **Add a full-team member:** add their email to `tiers.json` `full`, AND add them to the Cloudflare Access Allow policy (+ Google consent-screen test-users if the OAuth app is in Testing).
- **Add a contractor:** just add them to the Cloudflare Access allowlist — they default to the contractor bundle. (Don't add them to `tiers.json`.)

## UI

Company | Products top-level switch (Products fans out per non-shared KB
project — a 3rd product appears automatically), per-kind nav, client-side
search. Top-right badge shows the signed-in email + tier.

## Known limitations / follow-ups

- Markdown renders client-side via **marked.js from a CDN** → viewing needs internet. (Vendor it to go offline.)
- `server.py` **trusts** the Access email header rather than verifying the `Cf-Access-Jwt-Assertion` signature. Fine while only Access traffic reaches the loopback origin; verify the JWT when hardening.
- Tier split is a `kind`+`title` denylist in `build.py`, not real per-doc human-audience metadata.
