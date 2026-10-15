#!/usr/bin/env python3.11
"""convivy-orient — generate the orientation site from the KB index.

Emits TWO physically-separate bundles into site/:
  - index.full.html       — every doc (full-team view)
  - index.contractor.html — contractor-visible subset only (Company/legal/
                            strategy/platform-internal docs are NOT in the file)

`server.py` picks which bundle to serve per request from the authenticated
email (Cloudflare Access `Cf-Access-Authenticated-User-Email` header). The tier
is therefore enforced server-side — the contractor literally never receives the
sensitive docs, not merely hidden in-browser.

Each bundle: Company | Products | Engineering top-level switch, per-kind nav,
client-side search. Markdown rendered client-side via marked.js (CDN). Build:
python3.11 build.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import design_system as ds
import library_client
from paths import STATE_ROOT

OUT = STATE_ROOT / "site"
# Vendored marked.js (ARCH-056 follow-up): inlined into each bundle so the site
# renders markdown with no CDN dependency — fully offline / self-contained.
MARKED_JS = Path(__file__).resolve().parent / "vendor" / "marked.min.js"
# Vendored DOMPurify (ARCH-244): KB/orientation doc bodies render raw HTML via
# marked → innerHTML, so a doc body containing a <script> would execute for a
# full-tier viewer. Sanitize every marked.parse() output before it reaches the DOM.
DOMPURIFY_JS = Path(__file__).resolve().parent / "vendor" / "dompurify.min.js"

# The two AREA lists below name KB scopes, and Phase 0 step 5 replaced the scope
# vocabulary (ARCH-426, ARCH-432). They read `shared`, `utility` and `engineering`
# until PR G — all three emptied by the re-file, so the Company and Engineering
# areas rendered EMPTY while `company`, `platform`, `engineering-practice`,
# `knowledge` and `convivy-orient` fell through `render`'s
# unknown-project fallback and appeared as PRODUCT pills.
#
# Nothing failed. The fallback is deliberate and silent by design, `PROJECT_LABEL`
# ten lines below was updated in the same pass that missed these, and the break
# only shows as a page laid out wrongly. `test_every_indexed_project_is_placed`
# is what makes the next scope loud instead.
COMPANY_PROJECTS = ["company", "commercial"]
PRODUCT_ORDER = ["vivy", "constance", "hollis", "selector", "sous-chef"]
# Engineering is a third top-level area (peer to Company | Products), rendering
# the cross-cutting engineering/platform-practice docs (ARCH-190). It was driven
# by the single `engineering` KB project; step 5 split that intent across the
# Keel's platform scopes plus the infrastructure repos' own scopes, which are
# engineering documentation by every reading and products by none.
# `keel` joined on 2026-08-06, when the steward was retired and the platform
# became one project over agent-platform, substrate and knowledge. It is here
# rather than under Products for the reason `platform` is: the Keel is what the
# products are built ON. Without a placement it reached the page through
# `render`'s unknown-project fallback and rendered as a Products pill, which
# `test_every_indexed_project_is_placed` exists to catch — and did, locally,
# since that test is `needs_kb` and orient's CI has no knowledge checkout.
ENGINEERING_PROJECTS = ["platform", "engineering-practice", "knowledge",
                        "convivy-orient", "keel",
                        "library", "write-better"]
# library is the Keel's implementation, alongside platform and knowledge above.
# write-better is a practice standard, the same shape as engineering-practice.
# Phase 0 step 5 (ARCH-426, ARCH-432) replaced the scope vocabulary. `shared`,
# `utility` and `engineering` are emptied or gone and their labels retired with
# them; every scope that now holds documents is labelled, because the browse UI
# falls back to `L[p]||p` and renders a bare slug for anything missing — which is
# how `company`, the scope leading every category page, would have shown up.
PROJECT_LABEL = {
    "company": "Convivy & Company", "commercial": "Commercial",
    "vivy": "Vivy", "constance": "Constance",
    "hollis": "Hollis", "selector": "Selector", "sous-chef": "Sous Chef",
    "agent-platform": "Agent Platform",
    "convivy-app-kit": "App Kit", "convivy-services": "Services",
    "nyh-studio": "NYH Studio", "deck": "Deck",
    "platform": "Platform", "engineering-practice": "Engineering Practice",
    "knowledge": "Knowledge Base", "convivy-orient": "Orient",
    "substrate": "Substrate",
    "keel": "The Keel",
    # The Library's Course `product` key, not a KB scope — added for the
    # Chart House redesign (issue_01M15C4G6YJHH73KJCHGK7B0J9), which found
    # this crew rendered as "bosun" in one table and "Agent Platform"
    # (this dict's own `agent-platform` entry, the KB/repo slug) in
    # another. "Bosun" is what the operator calls it; this key makes that the one
    # spelling this dict serves for the Course's own product key.
    "bosun": "Bosun",
    # Same reason, same issue: the Course's product key is the GitHub repo
    # name `convivy-deck`, distinct from this dict's existing `deck` (the
    # KB scope slug) — both real spellings of the same project.
    "convivy-deck": "Deck",
    "library": "Library",
    "write-better": "Write Better",
    "constance-landing": "Constance Landing",
    "placard": "Placard",
    "signals": "Signals",
    "archy": "Archy",
    "shipwright": "Shipwright",
}
# Product super-groups on the Orientation "Products" tab (ARCH-308). The three
# mobile apps share a "Mobile App Suite" heading; their pills always render — even
# before an app has any KB docs — so the suite is visible as it fills in. Every
# member must also appear in PRODUCT_ORDER.
PRODUCT_GROUPS = [
    ("Mobile App Suite", ["hollis", "selector", "sous-chef"]),
]
# Projects that render as their own product pill, outside PRODUCT_ORDER's curated
# ordering and outside any suite. These reached the page through `render`'s
# unknown-project fallback and were never declared anywhere; listing them changes
# nothing about what renders (the order matches the fallback's `sorted()`) and
# makes `test_every_indexed_project_is_placed` able to tell "this belongs here"
# from "nobody has decided where this belongs" — which the fallback cannot.
#
# The fallback stays. A project appearing in the index between here and the next
# deploy should still render rather than vanish; the test is where the omission
# is meant to be loud, not the page.
STANDALONE_PRODUCTS = ["agent-platform", "convivy-app-kit", "convivy-services",
                       "deck", "nyh-studio",
                       # placard: QR-code property pages for the Now You're Here
                       # properties, not yet deployed. A product with no curated
                       # position yet, so it is placed where the fallback put it.
                       "placard", "substrate",
                       # constance-landing: no other product-marketing-site scope
                       # is indexed to set a precedent either way (issue_01KZ9DDRBNHHT4V61YR8NR7A2E).
                       # Placed here for lack of one; move it next to `constance`
                       # in PRODUCT_ORDER if a precedent shows up later.
                       "constance-landing",
                       # signals: the Signals product, which took over Orient's
                       # gmail capture. After constance-landing, because the
                       # fallback bucket in `render` follows this whole list, and
                       # that is where signals rendered before it was declared. Inserting it
                       # at its sorted position would move it ahead of substrate
                       # and constance-landing (issue_01M2QY6YCFA4GVG3NRKRANJ9PT).
                       "signals",
                       # archy and shipwright: indexed without a placement, so
                       # the fallback rendered them after signals in sorted
                       # order. Placed in that same order, which keeps the
                       # product order. Placing them also gives each its label
                       # and makes its full-tier /projects/<name> page render,
                       # since project_build.KNOWN_PROJECTS derives from these
                       # lists (issue_01M41AWFTJAN95YTYJTFRF4VBB).
                       "archy", "shipwright"]
# KB docs whose canonical home is a curated section elsewhere on the site, so they
# are kept OUT of the by-project browse to avoid a duplicate listing. The Selector
# taste-map lives in Reference Docs; without this it also renders under a lone
# "Convivy" project group in the Products browse (the operator 2026-07-02, ARCH-308).
# Per-doc browse exclusion. Empty since ARCH-366: the taste-map (Selector
# Explainer) was excluded by ARCH-308 because its "canonical home is Reference
# Docs", and ARCH-357 then retired Reference Docs — leaving the doc excluded
# from browse AND unreachable at its old route. The mechanism is kept; an entry
# here now needs a home that actually resolves.
BROWSE_EXCLUDE_IDS: set[str] = set()
# `bead` sits next to `overview` because it is what ADR-0024 built to replace the
# stale-overview failure: one per project, saying where that product is going.
# A kind missing here is not dropped, it sorts at position 99 under its raw slug —
# quieter than the `VISIBLE_KINDS` miss this fix is mainly about, and the same
# defect one list over (reviewer, PR #156). `brief` is also absent; left alone
# here rather than fixed in passing, since this PR is about `bead`.
KIND_ORDER = ["overview", "bead", "brand", "prd", "spec", "build-plan", "decision", "architecture",
              "reference", "field-notes", "runbook", "skill", "instruction-block",
              "meeting-notes",
              "legal", "agent"]

# Contractor visibility is a per-doc opt-in (ARCH-228 Phase 5): a doc is
# contractor-visible iff its `audience` includes "contractor" — default-deny.
# This replaces the prior CONTRACTOR_HIDDEN_* blocklist; a new team doc is hidden
# from contractors until explicitly tagged. The marker is set in the knowledge
# repo frontmatter, and knowledge.py + scripts/validate.py carry "contractor" in
# the audience roster.

# Explicit Start Here ordering for the Company tab, keyed by doc id. The navlist
# groups docs by kind and orders within a kind by title; the three Company
# `overview` docs would otherwise read How we work → Products at a glance → What
# is Convivy, which is backwards. This pins the intended reading order. Docs not
# listed fall back to title order within their kind.
START_HERE_ORDER = [
    "company/overview/what-is-convivy",
    "company/overview/products-at-a-glance",
    "company/overview/how-we-work",
    "company/brand/brand-and-voice",
    "company/reference/people",
]

# Section headings (matched case-insensitively, any heading level) stripped from
# the rendered site. These are editorial/meta sections a human orienting on the
# company or products does not need. The KB and MCP still hold them in full; only
# the site hides them. Add a heading here to hide that section site-wide.
HIDE_SECTIONS = {"provenance"}

# The human site shows team-audience documents and nothing else — the rule
# `home_build`'s own `_AUDIENCE` and `kb_index.team_docs` already apply. Named
# here rather than passed as a literal so the one place this module decides
# who it serves is greppable.
_AUDIENCE = "team"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _strip_frontmatter(body: str) -> str:
    """Drop a leading YAML frontmatter block if present. Defensive: the indexer
    normally stores the body without frontmatter, but this guarantees it never
    reaches the site even if that changes."""
    if body.startswith("---\n"):
        end = body.find("\n---", 4)
        if end != -1:
            nl = body.find("\n", end + 1)
            return body[nl + 1:] if nl != -1 else ""
    return body


def _strip_hidden_sections(body: str) -> str:
    """Remove every section whose heading title is in HIDE_SECTIONS — the heading
    plus everything under it, until the next heading of the same or higher level
    (or end of doc)."""
    out: list[str] = []
    skip_level: int | None = None  # inside a hidden section: skip until level <= this
    for line in body.split("\n"):
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            if skip_level is not None and level <= skip_level:
                skip_level = None  # a sibling/parent heading ends the hidden region
            if skip_level is None and m.group(2).strip().lower() in HIDE_SECTIONS:
                skip_level = level  # drop this heading and its body
                continue
        if skip_level is not None:
            continue
        out.append(line)
    return "\n".join(out)


def _clean_body(body: str) -> str:
    """Site-only body cleanup: strip leading frontmatter and any HIDE_SECTIONS."""
    return _strip_hidden_sections(_strip_frontmatter(body)).strip() + "\n"


def load_docs() -> list[dict]:
    """Team-audience documents for the orientation site, read through the
    Library rather than a local index file (issue_01M1MFJRR46TMQ6DSJSD7DGX7C).

    `list_reference_documents` already applies the entitlement gate and gives
    every field this needs except `body` — the listing route omits it — so
    each entitled doc gets its own `get_reference_document` fetch. Measured
    against the live Library on 2026-09-03: 373 team-audience documents (up
    from 338 on 2026-08-23), listing plus all 373 body fetches in ~0.4s
    total, against ~0.03s for the old direct sqlite read. Affordable inside a
    batch site build; feeding a request path this way would not be (that is
    exactly what separates this call site from `/memory`'s).

    **Raises on any Library failure — deliberately, and this is a batch site
    build, not a request path.** `serve.sh` runs `build.py` as an
    unconditional, fatal step (unlike every builder after it, which is
    `||`-guarded), so a caller reaching this point that swallowed the error
    would still go on to write a bundle silently missing every document —
    the worse failure, since a half-built site that looks complete gives no
    sign anything is wrong. Letting the exception propagate means the build
    aborts instead, which is the outcome `serve.sh`'s existing preflight
    (below, and in `serve.sh`) already treats as fatal."""
    docs = []
    for meta in library_client.list_reference_documents(audience=_AUDIENCE):
        # Kept out of the by-project browse — canonical home is a curated section
        # (e.g. the taste-map lives in Reference Docs), ARCH-308.
        if meta.id in BROWSE_EXCLUDE_IDS:
            continue
        full = library_client.get_reference_document(meta.id, audience=_AUDIENCE)
        if full is None or full.body is None:
            # Entitlement or existence changed between the list and the fetch
            # (a doc edited or re-tagged mid-build) — drop the row rather than
            # raise over a single stale reference in a 373-document batch.
            continue
        # Default-deny: contractor-visible only if explicitly opted in (ARCH-228
        # Phase 5). "contractor" always co-occurs with "team" in the frontmatter.
        contractor_ok = "contractor" in (full.audience or [])
        docs.append({
            "id": full.id, "project": full.project, "kind": full.kind,
            "title": full.title, "status": full.status,
            "body": _clean_body(full.body), "contractor": contractor_ok,
        })
    # The Library's listing order is not guaranteed; match the old SQL's
    # `ORDER BY project, kind, title` so the rendered bundle doesn't reshuffle.
    docs.sort(key=lambda d: (d["project"], d["kind"], d["title"]))
    return docs


def render(docs: list[dict], tier: str = "full") -> str:
    """Render a self-contained HTML bundle embedding exactly `docs`. `tier`
    selects the cross-page nav: full sees the dashboard links, a contractor sees
    none (they 404 on the dashboards, so the nav is suppressed for them)."""
    present = {d["project"] for d in docs}
    known = (set(COMPANY_PROJECTS) | set(PRODUCT_ORDER) | set(ENGINEERING_PROJECTS)
             | set(STANDALONE_PRODUCTS))
    group_members = [m for _, members in PRODUCT_GROUPS for m in members]
    # Standalone product pills: present products (in order) that aren't in a suite,
    # then the declared standalones, then any project still unplaced — the fallback,
    # kept so a scope minted since the last deploy renders rather than vanishing
    # (excluded ids already filtered in load_docs).
    standalone = (
        [p for p in PRODUCT_ORDER if p in present and p not in group_members]
        + [p for p in STANDALONE_PRODUCTS if p in present and p not in group_members]
        + sorted(p for p in present if p not in known and p not in group_members))
    # Full tier shows every suite member as a pill even before it has docs (so the
    # suite is visible as it fills in). The contractor bundle must NOT — an empty
    # pill would leak an unreleased product's name to a contractor (ARCH-228 is
    # default-deny), so there it shows only members that have a visible doc, and a
    # suite with no visible members drops out entirely.
    product_groups = [
        {"label": lbl, "members": [m for m in members if tier == "full" or m in present]}
        for lbl, members in PRODUCT_GROUPS
    ]
    product_groups = [g for g in product_groups if g["members"]]
    products = standalone + [m for g in product_groups for m in g["members"]]  # drives default selection
    company = [p for p in COMPANY_PROJECTS if p in present]
    engineering = [p for p in ENGINEERING_PROJECTS if p in present]
    # Strip the build-time-only `contractor` flag from the embedded payload.
    clean = [{k: v for k, v in d.items() if k != "contractor"} for d in docs]
    # Ship labels only for projects this bundle actually surfaces, so an unreferenced
    # (e.g. unreleased-product) name never rides along in a contractor payload.
    referenced = set(company) | set(standalone) | set(engineering) \
        | {m for g in product_groups for m in g["members"]}
    project_label = {p: PROJECT_LABEL[p] for p in referenced if p in PROJECT_LABEL}
    payload = json.dumps({
        "docs": clean,
        "companyProjects": company,
        "productProjects": products,
        "standaloneProducts": standalone,
        "productGroups": product_groups,
        "engineeringProjects": engineering,
        "projectLabel": project_label,
        "kindOrder": KIND_ORDER,
        "startHereOrder": START_HERE_ORDER,
    }, ensure_ascii=False).replace("</", "<\\/")
    marked_js = MARKED_JS.read_text(encoding="utf-8")
    dompurify_js = DOMPURIFY_JS.read_text(encoding="utf-8")
    nav_html = ds.nav_component("/orientation", tier)
    return (HTML_TEMPLATE
            .replace("/*__MARKED__*/", marked_js)
            .replace("/*__DOMPURIFY__*/", dompurify_js)
            .replace("/*__DATA__*/", payload)
            .replace("__NAV__", nav_html))


def build() -> None:
    docs = load_docs()
    contractor = [d for d in docs if d["contractor"]]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.full.html").write_text(render(docs, "full"), encoding="utf-8")
    (OUT / "index.contractor.html").write_text(render(contractor, "contractor"), encoding="utf-8")
    print(f"Wrote site/index.full.html ({len(docs)} docs) + "
          f"site/index.contractor.html ({len(contractor)} docs).")


# ---------------------------------------------------------------------------
# Knowledge page — page-local CSS (two-pane app shell, doc browser components).
# Inherits design-system tokens; no per-page :root block.
# ---------------------------------------------------------------------------

_KNOWLEDGE_CSS = r"""
/* ---- Two-pane app shell ------------------------------------------------ */
/*
  The standard ds nav is 52px tall. The sidebar and main pane each fill the
  remaining viewport height (100vh - 52px). On mobile, the sidebar becomes
  a toggleable drawer; the ds hamburger opens the global nav at ≤767px, so
  the doc-browser sidebar gets its own toggle below 640px.
*/
.kb-layout {
  display: flex;
  align-items: stretch;
  height: calc(100vh - 52px);
  overflow: hidden;
}

/* ---- Sidebar ----------------------------------------------------------- */
.kb-sidebar {
  width: 300px;
  min-width: 300px;
  height: 100%;
  overflow-y: auto;
  border-right: 1px solid var(--line);
  background: var(--surface);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
/* In dark mode --surface and --bg sit close together; a 1px line can collapse
   into one dark field on low-brightness screens, so strengthen the seam. */
html[data-theme="dark"] .kb-sidebar {
  border-right-width: 2px;
  box-shadow: 1px 0 0 rgba(0, 0, 0, .35);
}

/* ---- Area bar (tabs + product pills + search) -------------------------- */
.kb-areabar {
  position: sticky;
  top: 0;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
  padding: 10px 12px 8px;
  z-index: 2;
  flex-shrink: 0;
}
.kb-tabs {
  display: flex;
  gap: 4px;
}
.kb-tab {
  flex: 1;
  text-align: center;
  padding: 7px 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface-2);
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  color: var(--ink-2);
  transition: background 120ms, color 120ms, border-color 120ms;
  /* Real button semantics applied via role=button + keyboard handler in JS */
}
.kb-tab:hover {
  background: var(--brand-wash);
  color: var(--brand-ink);
  border-color: color-mix(in srgb, var(--brand) 30%, var(--line));
}
.kb-tab.active {
  background: var(--brand-wash);
  color: var(--brand-ink);
  border-color: color-mix(in srgb, var(--brand) 40%, var(--line));
  font-weight: 700;
}
.kb-tab:focus-visible {
  outline: 2px solid var(--brand);
  outline-offset: 2px;
}
@media (prefers-reduced-motion: reduce) {
  .kb-tab { transition: none; }
}

/* ---- Product pills ----------------------------------------------------- */
.kb-prods {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 8px;
}
/* Suite heading: a full-width label that breaks the pill row so its member pills
   sit on their own line under it (ARCH-308). */
.kb-prod-group {
  flex-basis: 100%;
  margin: 6px 0 1px;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: .04em;
  text-transform: uppercase;
  color: var(--ink-3);
}
.kb-prod-group:first-child { margin-top: 0; }
.kb-prod {
  padding: 4px 12px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface-2);
  cursor: pointer;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--ink-2);
  transition: background 120ms, color 120ms, border-color 120ms;
}
.kb-prod:hover {
  background: var(--brand-wash);
  color: var(--brand-ink);
  border-color: color-mix(in srgb, var(--brand) 30%, var(--line));
}
.kb-prod.active {
  background: var(--brand-wash);
  color: var(--brand-ink);
  border-color: color-mix(in srgb, var(--brand) 40%, var(--line));
  font-weight: 650;
}
.kb-prod:focus-visible {
  outline: 2px solid var(--brand);
  outline-offset: 2px;
}
@media (prefers-reduced-motion: reduce) {
  .kb-prod { transition: none; }
}

/* ---- Search (in sidebar area bar) ------------------------------------- */
.kb-search-wrap {
  margin-top: 8px;
}
.kb-search {
  width: 100%;
  font: inherit;
  font-size: 13px;
  padding: 6px 11px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--ink);
  transition: border-color 120ms, box-shadow 120ms;
}
.kb-search:focus {
  outline: none;
  border-color: var(--brand);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--brand) 18%, transparent);
}
@media (prefers-reduced-motion: reduce) {
  .kb-search { transition: none; }
}

/* ---- Doc navlist ------------------------------------------------------- */
.kb-navlist {
  padding: 6px 8px 48px;
  flex: 1;
  overflow-y: auto;
}
.kb-kind {
  font-weight: 700;
  font-size: 11px;
  letter-spacing: .05em;
  text-transform: uppercase;
  color: var(--ink-3);
  margin: 14px 8px 3px;
}
.kb-navlist a {
  display: block;
  padding: 5px 9px;
  border-radius: 8px;
  color: var(--ink);
  text-decoration: none;
  font-size: 13px;
  transition: background 100ms;
}
.kb-navlist a:hover { background: var(--surface-2); }
.kb-navlist a.active {
  background: var(--brand-wash);
  color: var(--brand-ink);
  font-weight: 600;
}
.kb-navlist a:focus-visible {
  outline: 2px solid var(--brand);
  outline-offset: -1px;
}
@media (prefers-reduced-motion: reduce) {
  .kb-navlist a { transition: none; }
}
.kb-empty {
  color: var(--ink-3);
  font-size: 13px;
  padding: 14px 8px;
}

/* ---- Main content pane ------------------------------------------------- */
.kb-main {
  flex: 1;
  height: 100%;
  overflow-y: auto;
  padding: 32px 48px 80px;
  background: var(--bg);
}
.kb-content {
  max-width: 760px;
  margin: 0 auto;
  font-size: 15px;
  line-height: 1.65;
  color: var(--ink);
}
.kb-content > :first-child { margin-top: 0; }
.kb-content h1, .kb-content h2 {
  letter-spacing: -.01em;
  color: var(--ink);
}
.kb-content h1 { font-size: 22px; font-weight: 700; margin: 0 0 16px; line-height: 1.3; }
.kb-content h2 { font-size: 18px; font-weight: 650; margin: 30px 0 10px; line-height: 1.35; }
.kb-content h3 { font-size: 15px; font-weight: 650; margin: 24px 0 8px; line-height: 1.4; }
.kb-content p { margin: 14px 0; }
/* Lists were unspaced (no ul/ol/li rules) — the crunched checklists. Give items
   room to breathe and nested lists a tighter-but-clear rhythm. */
.kb-content ul, .kb-content ol { margin: 14px 0; padding-left: 26px; }
.kb-content li { margin: 7px 0; }
.kb-content li > ul, .kb-content li > ol { margin: 7px 0 5px; }
.kb-content li > p { margin: 6px 0; }
.kb-content li::marker { color: var(--ink-3); }
.kb-content a { color: var(--brand-ink); text-underline-offset: 2px; }
.kb-content a:hover { text-decoration: underline; }
.kb-content pre {
  background: var(--surface-2);
  border: 1px solid var(--line);
  padding: 14px;
  border-radius: var(--radius);
  overflow: auto;
  font-size: 13px;
  font-family: var(--mono);
}
[data-theme="dark"] .kb-content pre {
  background: #0E0C09;
  border-color: var(--line);
  color: #E2DAD0;
}
.kb-content code {
  background: var(--brand-wash);
  color: var(--brand-ink);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 12.5px;
  font-family: var(--mono);
}
.kb-content pre code {
  background: none;
  padding: 0;
  color: inherit;
}
.kb-content table {
  border-collapse: collapse;
  width: 100%;
  margin: 14px 0;
  font-size: 13.5px;
}
.kb-content th, .kb-content td {
  border: 1px solid var(--line);
  padding: 6px 10px;
  text-align: left;
}
.kb-content th {
  background: var(--surface-2);
  font-weight: 650;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: .03em;
  color: var(--ink-2);
}
.kb-content blockquote {
  border-left: 3px solid var(--brand);
  background: var(--brand-wash);
  border-radius: 0 var(--radius) var(--radius) 0;
  margin: 14px 0;
  padding: 8px 16px;
  color: var(--ink-2);
}

/* ---- Metadata chips ---------------------------------------------------- */
.kb-meta {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 0 0 20px;
}
.kb-chip {
  background: var(--surface-2);
  color: var(--ink-2);
  font-size: 11.5px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--line);
}
.kb-chip.status {
  background: var(--brand-wash);
  color: var(--brand-ink);
  border-color: color-mix(in srgb, var(--brand) 25%, var(--line));
}

/* ---- Landing panel ----------------------------------------------------- */
.kb-landing {
  padding-top: 8px;
}
.kb-landing-title {
  font-size: 22px;
  font-weight: 700;
  color: var(--ink);
  letter-spacing: -.02em;
  margin-bottom: 10px;
}
/* One gradient signature moment — the landing headline accent. */
.kb-landing-title span {
  background: var(--grad);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.kb-landing p { color: var(--ink-2); font-size: 14px; margin: 8px 0; }
.kb-stat {
  display: inline-block;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 10px 16px;
  margin: 8px 8px 8px 0;
  box-shadow: var(--shadow);
}
.kb-stat b {
  font-size: 22px;
  font-weight: 700;
  display: block;
  color: var(--brand-ink);
  font-variant-numeric: tabular-nums;
}
.kb-stat span { font-size: 12px; color: var(--ink-3); }
.kb-note {
  color: var(--ink-3);
  font-size: 12.5px;
  margin-top: 28px;
  border-top: 1px solid var(--line);
  padding-top: 14px;
}

/* ---- Mobile sidebar toggle (<640px) ------------------------------------ */
.kb-sidebar-toggle {
  display: none;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  background: var(--surface);
  font-size: 13px;
  font-weight: 600;
  color: var(--ink-2);
  cursor: pointer;
  width: 100%;
  border: none;
  border-bottom: 1px solid var(--line);
  text-align: left;
}
.kb-sidebar-toggle:focus-visible {
  outline: 2px solid var(--brand);
  outline-offset: -2px;
}

@media (max-width: 640px) {
  .kb-sidebar-toggle { display: flex; }
  .kb-layout {
    flex-direction: column;
    height: auto;
    overflow: visible;
  }
  .kb-sidebar {
    width: 100%;
    min-width: 0;
    height: auto;
    max-height: 0;
    overflow: hidden;
    border-right: none;
    border-bottom: 1px solid var(--line);
    transition: max-height 250ms ease;
  }
  .kb-sidebar.open {
    max-height: 80vh;
    overflow-y: auto;
  }
  .kb-main {
    height: auto;
    min-height: calc(100vh - 52px);
    padding: 20px 16px 48px;
    overflow: visible;
  }
}
@media (prefers-reduced-motion: reduce) {
  .kb-sidebar { transition: none; }
}

@media (min-width: 641px) and (max-width: 1024px) {
  .kb-sidebar { width: 240px; min-width: 240px; }
  .kb-main { padding: 24px 28px 64px; }
}
"""

HTML_TEMPLATE = (r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Orient · Convivy Builder</title>
<link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="32x32" href="/assets/favicon-32.png">
<link rel="manifest" href="/assets/site.webmanifest">
<meta name="theme-color" content="#E8542F">
<meta name="apple-mobile-web-app-title" content="Orient">
<script>/*__MARKED__*/</script>
<script>/*__DOMPURIFY__*/</script>
<style>
"""
+ ds.TOKENS
+ ds.BASE_CSS
+ ds.COMPONENTS_CSS
+ _KNOWLEDGE_CSS
+ r"""
</style>
<script>
"""
+ ds.THEME_TOGGLE_JS
+ r"""
</script>
</head>
<body>
__NAV__
<button class="kb-sidebar-toggle" id="kbSidebarToggle"
        aria-expanded="false" aria-controls="kbSidebar">
  ☰ Browse docs
</button>
<div class="kb-layout">
  <aside class="kb-sidebar" id="kbSidebar" aria-label="Document browser">
    <div class="kb-areabar">
      <div class="kb-tabs" role="tablist" aria-label="Orientation areas">
        <div class="kb-tab" id="tab-company" role="tab"
             tabindex="0" data-area="company"
             aria-selected="true" aria-controls="kbNavlist">Company</div>
        <div class="kb-tab" id="tab-products" role="tab"
             tabindex="-1" data-area="products"
             aria-selected="false" aria-controls="kbNavlist">Products</div>
        <div class="kb-tab" id="tab-engineering" role="tab"
             tabindex="-1" data-area="engineering"
             aria-selected="false" aria-controls="kbNavlist">Engineering</div>
      </div>
      <div class="kb-prods" id="kbProds" aria-label="Product filter"></div>
      <div class="kb-search-wrap">
        <label for="kbQ" class="sr-only">Search this area</label>
        <input id="kbQ" class="kb-search" type="search"
               placeholder="Search this area…" autocomplete="off">
      </div>
    </div>
    <div class="kb-navlist" id="kbNavlist" role="tabpanel" aria-labelledby="tab-company"></div>
  </aside>
  <main class="kb-main" id="kbMain">
    <div id="kbContent" class="kb-content"></div>
  </main>
</div>
<script id="data" type="application/json">/*__DATA__*/</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const docs = DATA.docs;
docs.forEach((d, i) => d._i = i);
const navlistEl = document.getElementById('kbNavlist');
const prodsEl   = document.getElementById('kbProds');
const contentEl = document.getElementById('kbContent');
const qEl       = document.getElementById('kbQ');

let area    = 'company';
let product = DATA.productProjects[0] || null;
let active  = null;

marked.setOptions({ gfm: true, breaks: false });

// ---- Area helpers --------------------------------------------------------

function inArea(d) {
  if (area === 'company')     return DATA.companyProjects.includes(d.project);
  if (area === 'engineering') return DATA.engineeringProjects.includes(d.project);
  return d.project === product;
}

function visible(d) {
  if (!inArea(d)) return false;
  const q = qEl.value.trim().toLowerCase();
  if (q && !(d.title.toLowerCase().includes(q) || d.body.toLowerCase().includes(q))) return false;
  return true;
}

// ---- Area tabs (roving tabindex + keyboard nav) -------------------------

const tabs = Array.from(document.querySelectorAll('.kb-tab'));

function activateTab(tabEl) {
  const newArea = tabEl.dataset.area;
  area = newArea;
  tabs.forEach(t => {
    const isActive = t === tabEl;
    t.classList.toggle('active', isActive);
    t.setAttribute('aria-selected', isActive ? 'true' : 'false');
    t.setAttribute('tabindex', isActive ? '0' : '-1');
  });
  // Tell ATs which tab controls the doc-list panel.
  navlistEl.setAttribute('aria-labelledby', tabEl.id);
  afterAreaChange();
}

// Find the next visible tab from idx in the given direction, wrapping around
// and skipping hidden tabs (the Engineering tab is hidden in contractor tier).
function adjacentVisibleTab(idx, dir) {
  for (let step = 1; step <= tabs.length; step++) {
    const t = tabs[(idx + dir * step + tabs.length * step) % tabs.length];
    if (t.style.display !== 'none') return t;
  }
  return null;
}

tabs.forEach(tab => {
  tab.addEventListener('click', () => activateTab(tab));
  tab.addEventListener('keydown', e => {
    const idx = tabs.indexOf(e.currentTarget);
    if (e.key === 'ArrowRight') {
      const next = adjacentVisibleTab(idx, 1);
      if (next) { next.focus(); activateTab(next); }
      e.preventDefault();
    } else if (e.key === 'ArrowLeft') {
      const prev = adjacentVisibleTab(idx, -1);
      if (prev) { prev.focus(); activateTab(prev); }
      e.preventDefault();
    } else if (e.key === 'Enter' || e.key === ' ') {
      activateTab(e.currentTarget);
      e.preventDefault();
    }
  });
});

// ---- Render areabar (engineering tab visibility, product pills) ----------

function renderAreabar() {
  const engTab = document.getElementById('tab-engineering');
  // Hide the Engineering tab when the bundle carries no engineering docs (e.g.
  // the contractor tier, where they're filtered out) so it never opens empty.
  engTab.style.display = DATA.engineeringProjects.length ? '' : 'none';
  prodsEl.style.display = area === 'products' ? 'flex' : 'none';
  prodsEl.innerHTML = '';
  if (area === 'products') {
    // Standalone product pills first, then each suite as a heading + member pills.
    // Suite members render even with no docs (the pill shows an empty view) so the
    // Mobile App Suite is visible as it fills in (ARCH-308).
    const makePill = p => {
      const b = document.createElement('div');
      b.className = 'kb-prod' + (p === product ? ' active' : '');
      b.textContent = DATA.projectLabel[p] || p;
      b.setAttribute('role', 'button');
      b.setAttribute('tabindex', '0');
      b.setAttribute('aria-pressed', p === product ? 'true' : 'false');
      b.onclick = () => { product = p; afterAreaChange(); };
      b.onkeydown = e => {
        if (e.key === 'Enter' || e.key === ' ') { product = p; afterAreaChange(); e.preventDefault(); }
      };
      prodsEl.appendChild(b);
    };
    (DATA.standaloneProducts || []).forEach(makePill);
    for (const g of (DATA.productGroups || [])) {
      const h = document.createElement('div');
      h.className = 'kb-prod-group';
      h.textContent = g.label;
      prodsEl.appendChild(h);
      g.members.forEach(makePill);
    }
  }
}

// ---- Doc navlist ---------------------------------------------------------

function buildList() {
  navlistEl.innerHTML = '';
  const shown = docs.filter(visible);
  if (!shown.length) {
    navlistEl.innerHTML = '<div class="kb-empty">Nothing here for this view.</div>';
    return;
  }
  const kinds = [...new Set(shown.map(d => d.kind))]
    .sort((a, b) => (DATA.kindOrder.indexOf(a) + 1 || 99) - (DATA.kindOrder.indexOf(b) + 1 || 99));
  for (const kind of kinds) {
    const kh = document.createElement('div');
    kh.className = 'kb-kind';
    kh.textContent = kind;
    navlistEl.appendChild(kh);
    const sho = DATA.startHereOrder || [];
    const ordered = shown.filter(x => x.kind === kind).sort((a, b) =>
      ((sho.indexOf(a.id) + 1 || 99) - (sho.indexOf(b.id) + 1 || 99))
      || a.title.localeCompare(b.title));
    for (const d of ordered) {
      const a = document.createElement('a');
      a.textContent = d.title;
      a.href = '#' + d._i;
      if (active === d._i) { a.className = 'active'; a.setAttribute('aria-current', 'true'); }
      a.onclick = e => {
        e.preventDefault();
        show(d._i);
        // Mobile master-detail: the doc renders in the main column *below* the
        // open sidebar (normal flow, not an overlay), so a tall open sidebar can
        // leave the doc below the fold — worst in Products, where the wrapped
        // product-pill row makes the sidebar tallest, so the tap reads as "does
        // nothing". Collapse the browser and bring the doc into view.
        const sb = document.getElementById('kbSidebar');
        if (sb && sb.classList.contains('open')) {
          sb.classList.remove('open');
          const tb = document.getElementById('kbSidebarToggle');
          if (tb) { tb.setAttribute('aria-expanded', 'false'); tb.textContent = '☰ Browse docs'; }
          document.getElementById('kbMain').scrollIntoView({ block: 'start' });
        }
      };
      navlistEl.appendChild(a);
    }
  }
}

function render() { renderAreabar(); buildList(); }

// ---- Doc content render -------------------------------------------------

// Escape frontmatter fields interpolated into innerHTML (project/kind/status);
// the body goes through DOMPurify, but these scalar fields do not (ARCH-244).
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function show(i) {
  active = i;
  const d = docs[i];
  const statusChip = d.status !== 'current'
    ? `<span class="kb-chip status">${esc(d.status)}</span>` : '';
  contentEl.innerHTML =
    `<div class="kb-meta">`
    + `<span class="kb-chip">${esc(DATA.projectLabel[d.project] || d.project)}</span>`
    + `<span class="kb-chip">${esc(d.kind)}</span>`
    + statusChip
    + `</div>`
    + DOMPurify.sanitize(marked.parse(d.body));
  contentEl.parentElement.scrollTop = 0;
  buildList();
}

// ---- Landing panel ------------------------------------------------------

function landing() {
  active = null;
  const areaName = area === 'company'     ? 'Company'
    : area === 'engineering' ? (DATA.projectLabel['engineering'] || 'Engineering')
    : (DATA.projectLabel[product] || product);
  const n = docs.filter(inArea).length;
  contentEl.innerHTML = `<div class="kb-landing">
    <div class="kb-landing-title">
      <span>${esc(areaName)}</span> orientation
    </div>
    <p>Browse the left panel or search to find docs in this area. Use the
    Company / Products / Engineering tabs to switch areas.</p>
    <div>
      <div class="kb-stat">
        <b>${n}</b>
        <span>docs in ${esc(areaName)}</span>
      </div>
    </div>
    <p class="kb-note">What you can see here reflects your access level (shown top-right).</p>
  </div>`;
  buildList();
}

// ---- Wire events ---------------------------------------------------------

activateTab(document.getElementById('tab-company'));
qEl.oninput = () => buildList();

// Mobile sidebar toggle
const toggleBtn = document.getElementById('kbSidebarToggle');
const sidebarEl = document.getElementById('kbSidebar');
if (toggleBtn && sidebarEl) {
  toggleBtn.addEventListener('click', function() {
    const open = sidebarEl.classList.toggle('open');
    toggleBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggleBtn.textContent = open ? '✕ Close' : '☰ Browse docs';
  });
}

function afterAreaChange() { qEl.value = ''; render(); landing(); }

render();
landing();
</script>
</body>
</html>
""")

if __name__ == "__main__":
    build()
