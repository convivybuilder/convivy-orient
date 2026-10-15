#!/usr/bin/env python3.11
"""/engineering — the Engineering area: human docs + the Greater KB browse (ARCH-194).

Renders LIVE per request (ARCH-182 pattern): live_html() re-reads the KB index each
time, so the browse never lags the knowledge-watcher. Two layers (the operator 2026-06-19):

  - **Human-readable docs** — docs opted into the `team` audience (the same set the
    Orientation library shows). Featured at the top of the Engineering landing so a
    human lands on them, rather than burying them in the LLM-facing corpus.
  - **The Greater KB** — every current/draft doc of a human-browsable kind, organised
    by doc type with a project filter. A row is `id · title · blurb`; clicking it
    expands the formatted doc — inline (accordion) for short docs, in a right-side
    drawer for long ones. A `team`-audience doc carries **Edit**, which opens the
    checkout-guarded editor in that drawer, under the document it is editing, and
    **Open ↗**, the document's own page at `/reference/doc/{id}`. Editing where
    the document is read is the point (issue_01M17HYDBQ4GPWHPFS56M6QQPH): Edit
    used to be a link that navigated away, so the reader lost their place in the
    listing to fix a typo. A non-`team` doc gets neither control:
    `/reference/doc/{id}` 404s for one and the write API refuses it (the same
    audience gate the Library enforces), so showing a control there would be a
    broken one rather than a missing feature.

    **The editor is `kb_editor.py`'s**, shared with the document's own page, and
    exactly one is open at a time. This page is responsible for releasing its
    Library checkout — `openDoc` in `_BROWSE_JS` carries the reasoning, and
    `releaseEditor` is what every path that has to release it calls.

arch_log_html() renders the substrate ARCH log (substrate/decisions.md), which is
NOT in the KB index — surfaced here as the authoritative platform decision log.

All of this is full-tier-only (server.py gates /engineering*); a contractor never
receives the page.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import build
import design_system as ds
import kb_editor
import library_client

MARKED_JS = build.MARKED_JS
DOMPURIFY_JS = build.DOMPURIFY_JS  # ARCH-244: sanitize marked output before innerHTML
OUT = build.OUT
ARCH_LOG = Path.home() / "Convivy" / "substrate" / "decisions.md"

# Doc kinds a human browses on the team site (designer 2026-06-19). The rest
# (meeting-notes, legal, observation, proposal, feature-request, marketing) are
# operational noise for a human; the KB/MCP still hold them for agents. `arch` is
# the substrate ARCH platform-decision log, surfaced as its own browsable type
# (ARCH-196) — kept distinct from product ADRs, which it is.
VISIBLE_KINDS = ["decision", "arch", "architecture", "reference", "runbook",
                 "spec", "prd", "skill", "agent", "overview", "brand",
                 # field-notes (ARCH-289) was added to the KB schema and to
                 # agent-platform's kind list, but never here — so the skip at
                 # `kind not in VISIBLE_KINDS` below silently dropped every doc
                 # using it. The kind had no members until ARCH-366 moved the
                 # agentic system-design doc in, which is what surfaced it.
                 "field-notes",
                 # `brief` LEFT this list on 2026-08-26 (the operator): Archives holds
                 # memories — records of what happened that nobody maintains as
                 # current truth — and a brief is exactly that, a dated snapshot
                 # of what was read that week, not a living reference. It moved to
                 # /reference/briefings (ops_reports_build.py), which now reads this
                 # same KB index directly instead of the signal store it used to
                 # read. `brief` is listed in DELIBERATELY_NOT_BROWSABLE
                 # (tests/test_engineering_build.py) so this exclusion reads as a
                 # decision, not the accidental-omission shape the two notes below
                 # describe.
                 #
                 # `bead` (ADR-0024, knowledge#169) is the FOURTH kind to reach the
                 # KB schema and agent-platform without reaching this list. The
                 # field-notes note above counts itself second; the third (brief)
                 # has since left deliberately, as explained above, and the first
                 # is one whose note has since been cleaned up. Four
                 # occurrences of one shape is a defect in the process rather than
                 # four oversights: the KB side
                 # needs `ALLOWED_KINDS` + validate.py + the schema doc + a
                 # watcher restart, and nothing in that checklist points here. The
                 # guard below is the only thing that catches it, and it is
                 # marker-gated, so CI stays green and only the deploy host's
                 # daily `bin/check-kb-guards` run fails — which is why this sat
                 # red from the 2026-08-04 merge until someone ran the suite
                 # locally on 2026-08-05.
                 "bead",
                 # `instruction-block` (knowledge 732263c, steward epic
                 # issue_01M2MBQ8Q7YX34DDF8H9YM5T58) is the fifth. A block is a
                 # rule assembled into a session's CLAUDE.md, and it is also a
                 # reviewed Reference Room document a person reads to see what
                 # the agents are told, so it is browsable.
                 "instruction-block",
                 # `build-plan` (knowledge validate.py, Library issue
                 # issue_01M2TWRWHJMA6MF9NKZ9Z05KXN) is the sixth: the
                 # implementation checklist between an ADR and the code, read by
                 # the people approving what an agent will build from it.
                 "build-plan"]
# Descriptive left-nav labels for every browsable kind — terse codes (DEC/ARCH)
# read poorly, and "decision" vs "arch" vs "architecture" need disambiguating
# (the operator 2026-06-19, ARCH-200). The KIND_TOOLTIP below carries the full explanation
# on hover; the badge on each row still shows the citable code (ADR-NNNN/ARCH-NNN).
KIND_LABEL = {
    "decision": "Decisions (ADR)",
    "arch": "Platform Decisions (ARCH)",
    "architecture": "Architecture",
    "reference": "Reference",
    "runbook": "Runbooks",
    "spec": "Specs",
    "prd": "PRDs",
    "skill": "Skills",
    "agent": "Agent Roster",
    "overview": "Overviews",
    "brand": "Brand",
    "field-notes": "Field Notes",
    "bead": "Beads",
    "instruction-block": "Instruction Blocks",
    "build-plan": "Build Plans",
}
# Hover tooltip per kind — what the category is, and how the easily-confused ones
# differ (ADR vs ARCH vs Architecture).
KIND_TOOLTIP = {
    "decision": "Architecture Decision Records (ADR-NNNN) — a point-in-time design decision for a product or the company. One file per decision.",
    "arch": "ARCH-NNN — decisions about Convivy's agentic platform/substrate itself (the steward's engineering decision log). Distinct from the product ADRs above.",
    "architecture": "How a product is built: durable system architecture & design docs — the lasting 'how it works', not a point-in-time decision (those are ADRs).",
    "reference": "Factual reference — APIs, configs, integrations, how a thing works.",
    "runbook": "Operational procedures — how to run, deploy, release, recover.",
    "spec": "Feature and flow specifications.",
    "prd": "Product requirements documents.",
    "skill": "Reusable agent skills and procedures.",
    "agent": "Definitions of the agent crew (reviewer, coder, planner, …).",
    "overview": "High-level orientation docs.",
    "brand": "Brand, voice, and positioning.",
    "field-notes": "Conceptual references explaining how we think about part of the system — no code, written to be readable outside the team.",
    "bead": "One per project — the diagram of where that product is going as currently known, and how far from there it is. Its accounting is what keeps the ADRs and the to-do queue honest.",
    "instruction-block": "One rule for agents, stated once. Its frontmatter names the crews, agents and topics it binds, and a session's CLAUDE.md is assembled from the blocks that bind it.",
    "build-plan": "The implementation checklist between a decision and the code: what gets built, in what order, from which work items. Its topics bind it to the work items a coder is dispatched on.",
}
# Project section order on every category page — "Convivy & Shared" first
# (the operator 2026-06-19), then the products, then the engineering-internal projects.
# Projects not listed sort after these, alphabetically.
PROJECT_ORDER = ["company", "vivy", "constance",
                 "hollis", "selector", "sous-chef",
                 "platform", "engineering-practice", "agent-platform",
                 # `substrate` carries the ARCH log entries surfaced as `arch`
                 # docs (arch_docs below), which had no place in this order while
                 # they claimed the retired `engineering` scope.
                 "substrate", "knowledge", "convivy-orient"]
PROJECT_LABEL = {**build.PROJECT_LABEL, "agent-platform": "Agent Platform"}
# Bodies longer than this open in the right-side drawer rather than inline, so a
# long decision doc doesn't push the list off-screen (designer 2026-06-19).
DRAWER_THRESHOLD = 3000

_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_MARK = re.compile(r"[*_`>#]")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _blurb(body: str) -> str:
    """First real prose line of a doc, stripped of simple markdown, as the row
    one-liner (the KB has no dedicated description field — the title carries the
    summary for many docs, and this adds context for the rest).

    HTML comments (e.g. `<!-- unsupported notion block: file -->`) are stripped
    before any line is considered, so they never surface as the excerpt."""
    # Strip HTML comments first — they can span lines, so handle before splitting.
    body = _HTML_COMMENT.sub("", body)
    for line in body.split("\n"):
        s = line.strip()
        # skip blanks, headings, rules, tables, code fences, quotes, and list
        # items — but NOT an emphasised prose line (`**bold** …`), so require the
        # trailing space that distinguishes a `- ` bullet from `**`.
        if (not s or s.startswith(("#", "---", "|", "```", ">"))
                or s[:2] in ("- ", "* ", "+ ") or re.match(r"\d+\.\s", s)):
            continue
        s = _MD_LINK.sub(r"\1", s)
        s = _MD_MARK.sub("", s)
        return s[:220]
    return ""


_DECISION_NUM = re.compile(r"^(\d+)-")


def _ref_label(kind: str, id_: str) -> str:
    """The short reference shown on each doc's card/row (ARCH-196). Decisions →
    `ADR-0021` (the zero-padded number from the id); everything else → its id
    slug (the citable name). ARCH entries carry their own `ARCH-189` label."""
    leaf = id_.rsplit("/", 1)[-1]
    if kind == "decision":
        m = _DECISION_NUM.match(leaf)
        if m:
            return f"ADR-{m.group(1)}"
    return leaf


def load_docs() -> list[dict]:
    """The Greater KB browse's rows, read through the Library rather than a
    local index file (issue_01M1MFKD2KX9CPBVCQKJP1R003 — the last direct
    knowledge-index read Orient held).

    **NO BODIES ARE FETCHED, and that is the whole reason this slice waited.**
    This page renders live per request; one body fetch measured 3.7ms against
    the running Library, so pulling 644 of them would add 2.4 seconds to every
    render. `blurb` and `body_len` now arrive on the listing rows themselves
    (convivy/library#107), which is the capability that made the move possible.
    Nothing here reads a body: `render` drops the key, and the drawer and
    inline expansion fetch one on demand through `memory_api`.

    **The listing is deliberately UNFILTERED — by audience AND by status — and
    dropping the audience half would silently halve the page.** The status
    filter below is applied here rather than inherited from the route's default,
    so it is an intentional choice this page makes rather than one it happens to
    receive. This is the Greater KB — every current/draft document of a
    human-browsable kind — and `human` marks per row whether the reader may open
    it. Measured on 2026-09-11: 644 rows, of which 300 are team-audience and 344
    are titles a full-tier human can see exist but not read.
    `list_reference_documents(audience="team")` returns exactly those 300, so a
    port that reached for the obvious function would have dropped 344 documents
    and looked like a clean migration.

    **`body_len` is the STORED length where this used to compute a cleaned one.**
    Nothing renders it; `clickRow` compares it against the 3000-character drawer
    threshold. The two differ on the 238 documents carrying a provenance
    section, which moves 12 documents of 763 from expanding inline to opening in
    a drawer. The operator accepted that on 2026-09-11 rather than have the page fetch
    every body to compute its own.
    """
    docs = []
    for meta in library_client.list_reference_documents_unfiltered():
        if meta.kind not in VISIBLE_KINDS:
            continue
        if meta.status not in ("current", "draft"):
            continue
        # A per-doc browse exclusion. ARCH-308 used it to keep the taste-map out
        # of the by-project browse because its "canonical home is Reference Docs";
        # ARCH-357 retired Reference Docs, which left the doc unreachable, so
        # ARCH-366 cleared the entry. The mechanism stays for future use.
        if meta.id in build.BROWSE_EXCLUDE_IDS:
            continue
        docs.append({
            "id": meta.id, "project": meta.project, "kind": meta.kind,
            "title": meta.title, "reflabel": _ref_label(meta.kind, meta.id),
            "status": meta.status, "reviewed": meta.last_reviewed or "",
            "ref": meta.source_ref or "",
            # `_entitled` rather than `"team" in meta.audience`: the client's
            # check is isinstance-guarded, and without that guard a truthy
            # non-list audience makes this a SUBSTRING test — "steam-team"
            # would read as team-audience and link a document the reader may
            # not open. That is the bypass PR #205's review found, and
            # `test_load_docs_human_false_for_non_list_audience_substring_match`
            # is here because of it. The first draft of this port had the naive
            # form and that test caught it.
            "human": library_client._entitled(meta, "team"),
            "blurb": meta.blurb or "", "len": meta.body_len or 0,
        })
    # The old SQL carried `ORDER BY project, kind, id`; the Library's listing
    # order is not guaranteed, so it is restated here rather than inherited —
    # the same correction `build.load_docs` needed when it moved (PR #262).
    docs.sort(key=lambda d: (d["project"], d["kind"], d["id"]))
    return docs


# mtime+size of ARCH_LOG the cached `docs` list below was built from, plus the
# path itself (tests repoint ARCH_LOG at a fresh tmp file per test, and a
# stale cache keyed on mtime alone could collide across paths on a coarse
# filesystem clock). `None` until the first call.
_ARCH_DOCS_CACHE: dict[str, object] = {}


def arch_docs() -> list[dict]:
    """The substrate ARCH log surfaced as browsable KB docs of kind `arch`
    (ARCH-196). The canonical log stays in substrate/decisions.md (the steward
    keeps appending there); these are parsed from it at render time, so an
    entry is not a Library document with its own id — `human` is hardcoded
    False below for exactly this reason, which is what keeps `editHref` in
    `_BROWSE_JS` from offering an edit link that `/reference/doc/{id}` would
    have nothing to serve.

    The parse is cached, invalidated on the log file's (path, mtime, size), so
    a steward's append is picked up on the next call. Search
    (issue_01M0Y7GZED2THYETHCT8NPHHDS) made this keystroke-driven behind a
    250ms debounce — measured at ~10-15ms uncached against the live ~1.5 MB
    log, which is real cost to pay on every debounce tick for a file that
    changes only when the steward appends to it."""
    try:
        st = ARCH_LOG.stat()
    except FileNotFoundError:
        return []
    cache_key = (str(ARCH_LOG), st.st_mtime, st.st_size)
    if _ARCH_DOCS_CACHE.get("key") == cache_key:
        return _ARCH_DOCS_CACHE["docs"]
    entries = parse_arch_log(ARCH_LOG.read_text(encoding="utf-8"))
    out = []
    for e in entries:
        out.append({
            # `substrate`, matching the id. It read `engineering` until PR G — a
            # scope Phase 0 step 5 retired, so every ARCH entry sorted last and
            # rendered a bare `engineering` slug where a label belongs. The id has
            # always said `substrate`; an id and a project that disagree is the
            # same fixture defect PR F found three of.
            "id": f"substrate/arch/{e['anchor']}", "project": "substrate",
            "kind": "arch", "title": e["title"], "reflabel": e["ref"],
            "status": "current", "reviewed": "", "ref": "substrate/decisions.md",
            "human": False, "blurb": _blurb(e["body"]), "len": len(e["body"]),
            "body": e["body"], "date": e["date"],
        })
    _ARCH_DOCS_CACHE["key"] = cache_key
    _ARCH_DOCS_CACHE["docs"] = out
    return out


# ---------------------------------------------------------------------------
# Page-local CSS: Browse (/engineering)
# ---------------------------------------------------------------------------

_BROWSE_CSS = r"""
/* ---- Two-pane app shell ------------------------------------------------ */
.eng-layout {
  display: flex;
  align-items: stretch;
  height: calc(100vh - 90px);
  overflow: hidden;
}

/* ---- Kind sidebar ------------------------------------------------------ */
.eng-sidebar {
  width: 240px;
  min-width: 240px;
  height: 100%;
  overflow-y: auto;
  border-right: 1px solid var(--line);
  background: var(--surface);
  flex-shrink: 0;
  padding: 14px 10px 60px;
}
html[data-theme="dark"] .eng-sidebar {
  border-right-width: 2px;
  box-shadow: 1px 0 0 rgba(0, 0, 0, .35);
}

/* Kind filter buttons — real <button>s with aria-pressed */
.eng-sidebar .kind {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 6px 10px;
  border-radius: 8px;
  border: none;
  background: transparent;
  font: inherit;
  font-size: 13.5px;
  color: var(--ink);
  cursor: pointer;
  text-align: left;
  transition: background 120ms, color 120ms;
}
.eng-sidebar .kind:hover { background: var(--surface-2); }
.eng-sidebar .kind[aria-pressed="true"] {
  background: var(--brand-wash);
  color: var(--brand-ink);
  font-weight: 650;
}
.eng-sidebar .kind:focus-visible { outline: 2px solid var(--brand); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { .eng-sidebar .kind { transition: none; } }
.eng-sidebar .kind .ct { font-size: 11px; opacity: .7; }
.eng-sidebar .kind[aria-pressed="true"] .ct { opacity: .9; }
.eng-sidebar .grouphdr {
  font-weight: 700;
  font-size: 11px;
  letter-spacing: .04em;
  text-transform: uppercase;
  color: var(--ink-3);
  margin: 10px 10px 4px;
}

/* ---- Main content pane ------------------------------------------------- */
.eng-main {
  flex: 1;
  height: 100%;
  overflow-y: auto;
  padding: 24px 40px 80px;
  background: var(--bg);
}

/* ---- Controls row ------------------------------------------------------ */
.eng-controls {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.eng-controls input {
  font: inherit;
  font-size: 13px;
  padding: 7px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--ink);
  width: 340px;
  transition: border-color 120ms, box-shadow 120ms;
}
.eng-controls input:focus {
  outline: none;
  border-color: var(--brand);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--brand) 18%, transparent);
}
@media (prefers-reduced-motion: reduce) { .eng-controls input { transition: none; } }

/* ---- Project filter pills — real <button>s with aria-pressed ----------- */
.eng-pills { display: flex; gap: 6px; flex-wrap: wrap; }
.eng-pill {
  padding: 5px 12px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface);
  font: inherit;
  font-size: 12.5px;
  color: var(--ink-2);
  cursor: pointer;
  transition: background 120ms, color 120ms, border-color 120ms;
}
.eng-pill:hover { border-color: var(--brand); background: var(--brand-wash); color: var(--brand-ink); }
.eng-pill[aria-pressed="true"] {
  background: var(--brand-wash);
  border-color: color-mix(in srgb, var(--brand) 40%, var(--line));
  color: var(--brand-ink);
  font-weight: 650;
}
.eng-pill:focus-visible { outline: 2px solid var(--brand); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { .eng-pill { transition: none; } }

/* ---- Doc area ---------------------------------------------------------- */
h2.area { font-size: 18px; font-weight: 700; color: var(--ink); margin: .2em 0 .4em; }
.grp {
  margin: 22px 0 6px;
  font-weight: 700;
  font-size: 13px;
  color: var(--brand-ink);
  border-bottom: 1px solid var(--line);
  padding-bottom: 4px;
}
.grp-link { color: inherit; text-decoration: none; }
.grp-link:hover { text-decoration: underline; }
.row {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
  margin: 6px 0;
  padding: 10px 14px;
  cursor: pointer;
  transition: border-color 120ms;
}
.row:hover { border-color: var(--brand); }
@media (prefers-reduced-motion: reduce) { .row { transition: none; } }
.row .rref { margin-bottom: 5px; }
.ref {
  font-family: var(--mono);
  font-size: 10.5px;
  font-weight: 700;
  color: var(--brand-ink);
  background: var(--brand-wash);
  padding: 1px 8px;
  border-radius: 999px;
  letter-spacing: .03em;
}
.row .top { display: flex; align-items: baseline; gap: 10px; }
.row .title { font-weight: 700; flex: 1; color: var(--ink); }
.row .tags { display: flex; gap: 6px; align-items: center; white-space: nowrap; flex-shrink: 0; }
.tag {
  background: var(--surface-2);
  color: var(--ink-2);
  font-size: 11px;
  font-weight: 600;
  padding: 2px 9px;
  border-radius: 999px;
}
.tag.proj { background: var(--brand-wash); color: var(--brand-ink); }
.tag.draft { background: var(--brand-wash); color: var(--brand-ink); }
.row .blurb { color: var(--ink-3); font-size: 13px; margin-top: 4px; }
/* Edit is a <button> and Open an <a>, sharing this class so the pair reads as
   one control group. The button needs the UA's own chrome reset to match. */
.row .edit { font-size: 12px; font-weight: 700; color: var(--brand-ink); text-decoration: none; opacity: 0; }
button.edit { background: none; border: 0; padding: 0; font-family: inherit; cursor: pointer; }
/* **:focus-within IS LOAD-BEARING NOW THAT ONE OF THESE IS FOCUSABLE.** At
   opacity 0 a keyboard user tabbing into the row would move focus onto a
   control they cannot see. Hover alone was enough while both were links nobody
   tabbed to in practice; it is not enough for a button. */
.row:hover .edit, .row:focus-within .edit { opacity: 1; }
.inset { margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--line); }

/* ---- Feature card grid ------------------------------------------------- */
.feat { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 10px; margin: 8px 0 22px; }
.card {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
  padding: 15px 17px;
  cursor: pointer;
  transition: border-color 120ms, box-shadow 120ms;
}
.card:hover { border-color: var(--brand); box-shadow: var(--shadow); }
@media (prefers-reduced-motion: reduce) { .card { transition: none; } }
.card .cref {
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 700;
  color: var(--brand-ink);
  background: var(--brand-wash);
  padding: 1px 7px;
  border-radius: 999px;
  display: inline-block;
  margin-bottom: 6px;
  letter-spacing: .03em;
}
.card .t { font-weight: 700; color: var(--ink); }
.card .b {
  color: var(--ink-3);
  font-size: 13px;
  margin-top: 5px;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  overflow: hidden;
}

/* ---- Stat chips on the landing ----------------------------------------- */
.eng-stat {
  display: inline-block;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 8px 14px;
  margin: 4px 8px 4px 0;
  cursor: pointer;
  box-shadow: var(--shadow);
  transition: border-color 120ms;
}
.eng-stat:hover { border-color: var(--brand); }
.eng-stat b { color: var(--brand-ink); font-weight: 800; }

/* ---- Misc text helpers ------------------------------------------------- */
.note { color: var(--ink-3); font-size: 13px; }
/* A body search that couldn't reach the Library — distinct from "no body
   match", the same distinction the ARCH-log's `.arch-closed` banner draws
   between a closed log and a broken one. */
.eng-search-warn { border-left: 3px solid var(--warn, #b4700e); padding-left: 10px; }
.empty { color: var(--ink-3); padding: 24px 4px; }
.archlink { display: inline-block; margin: 6px 0 0; font-size: 13px; font-weight: 700; color: var(--brand-ink); text-decoration: none; }
.archlink:hover { text-decoration: underline; }

/* ---- Drawer content (markdown rendering, mirrors kb-content) ----------- */
/* max-width:100% (not a fixed 720px) so content never assumes a canvas wider
   than the drawer; overflow-wrap breaks long unbroken strings (file paths,
   URLs) instead of forcing the drawer to scroll sideways (the operator 2026-07-17). */
.eng-doc-content { max-width: 100%; overflow-wrap: anywhere; }
.eng-doc-content h1:first-child { margin-top: 0; }
.eng-doc-content h1, .eng-doc-content h2 { letter-spacing: -.01em; color: var(--ink); }
.eng-doc-content h1 { font-size: 22px; font-weight: 700; }
.eng-doc-content h2 { font-size: 17px; font-weight: 650; }
.eng-doc-content h3 { font-size: 14px; font-weight: 650; }
.eng-doc-content p { margin: 12px 0; }
.eng-doc-content a { color: var(--brand-ink); text-underline-offset: 2px; }
.eng-doc-content pre {
  background: var(--surface-2);
  border: 1px solid var(--line);
  padding: 14px;
  border-radius: var(--radius);
  overflow-x: auto;   /* a wide code block scrolls within itself, not the drawer */
  max-width: 100%;
  font-size: 13px;
  font-family: var(--mono);
}
.eng-doc-content img { max-width: 100%; height: auto; }
[data-theme="dark"] .eng-doc-content pre { background: #0E0C09; border-color: var(--line); color: #E2DAD0; }
.eng-doc-content code {
  background: var(--brand-wash);
  color: var(--brand-ink);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 12.5px;
  font-family: var(--mono);
}
.eng-doc-content pre code { background: none; padding: 0; color: inherit; }
/* display:block + overflow-x makes a too-wide table (many columns / long
   inline-code cells) scroll WITHIN its own box instead of spilling the drawer,
   which was cutting off the last column + scrolling the whole body (the operator
   2026-07-17). */
.eng-doc-content table { display: block; overflow-x: auto; max-width: 100%; border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 13.5px; }
.eng-doc-content th, .eng-doc-content td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; }
.eng-doc-content th { background: var(--surface-2); font-weight: 650; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; color: var(--ink-2); }
.eng-doc-content blockquote {
  border-left: 3px solid var(--brand);
  background: var(--brand-wash);
  border-radius: 0 var(--radius) var(--radius) 0;
  margin: 14px 0;
  padding: 8px 16px;
  color: var(--ink-2);
}

/* Inline-expanded accordion content reuses the same markdown CSS via .content alias */
.inset .content { max-width: 680px; }
.content h1, .content h2, .content h3 { color: var(--ink); }
.content a { color: var(--brand-ink); text-underline-offset: 2px; }
.content pre {
  background: var(--surface-2);
  border: 1px solid var(--line);
  padding: 14px;
  border-radius: var(--radius);
  overflow: auto;
  font-size: 13px;
  font-family: var(--mono);
}
[data-theme="dark"] .content pre { background: #0E0C09; color: #E2DAD0; }
.content code { background: var(--brand-wash); color: var(--brand-ink); padding: 1px 5px; border-radius: 4px; font-size: 12.5px; font-family: var(--mono); }
.content pre code { background: none; padding: 0; color: inherit; }
.content table { border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 13.5px; }
.content th, .content td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; }
.content th { background: var(--surface-2); font-weight: 650; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; color: var(--ink-2); }
.content blockquote { border-left: 3px solid var(--brand); background: var(--brand-wash); border-radius: 0 var(--radius) var(--radius) 0; margin: 14px 0; padding: 8px 16px; color: var(--ink-2); }

/* Drawer metadata row */
.doc-meta { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; align-items: center; }
.eng-edit-link { font-size: 12px; font-weight: 700; color: var(--brand-ink); text-decoration: none; margin-left: auto; }
.eng-edit-link:hover { text-decoration: underline; }
/* The drawer's Edit control sits at the right end of the meta row, with Open
   beside it. Whichever comes first takes the `margin-left: auto` that pushes
   the pair over; the adjacent-sibling rule stops the second one claiming a
   second share of the same free space and splitting them apart. */
.eng-edit-btn { margin-left: auto; padding: .25rem .7rem; border: 1px solid var(--line);
                border-radius: 8px; background: var(--surface); color: var(--brand-ink);
                font: inherit; font-size: 12px; font-weight: 700; cursor: pointer; }
.eng-edit-btn:hover { background: var(--brand-wash); }
.eng-edit-btn + .eng-edit-link { margin-left: 0; }

/* ---- Mobile sidebar toggle (<640px) ------------------------------------ */
.eng-sidebar-toggle {
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
.eng-sidebar-toggle:focus-visible { outline: 2px solid var(--brand); outline-offset: -2px; }

@media (max-width: 640px) {
  .eng-sidebar-toggle { display: flex; }
  .eng-layout { flex-direction: column; height: auto; overflow: visible; }
  .eng-sidebar {
    width: 100%;
    min-width: 0;
    height: auto;
    max-height: 0;
    overflow: hidden;
    border-right: none;
    border-bottom: 1px solid var(--line);
    transition: max-height 250ms ease;
    padding: 0 10px;
  }
  .eng-sidebar.open { max-height: 80vh; overflow-y: auto; padding: 14px 10px 60px; }
  .eng-main { height: auto; min-height: calc(100vh - 90px); padding: 20px 16px 48px; overflow: visible; }
  .eng-controls input { width: 100%; }
}
@media (prefers-reduced-motion: reduce) { .eng-sidebar { transition: none; } }
@media (min-width: 641px) and (max-width: 1024px) {
  .eng-sidebar { width: 200px; min-width: 200px; }
  .eng-main { padding: 20px 24px 64px; }
}
"""

# The client-side Browse JS — factored out so render() can embed it cleanly.
_BROWSE_JS = r"""
const DATA = JSON.parse(document.getElementById('data').textContent);
const docs = DATA.docs; docs.forEach((d,i)=> d._i=i);
const L = DATA.projectLabel;
const KL = DATA.kindLabel || {};      // descriptive labels for kinds
const TT = DATA.kindTooltip || {};    // hover tooltip explaining each kind
function kindLabel(k){ return KL[k] || k; }
function kindTip(k){ return TT[k] || ''; }
marked.setOptions({ gfm:true, breaks:false });

// Escape any field interpolated into innerHTML. KB content is ours and this page
// is full-tier-only, but a title with `<`, `&`, or a quote would otherwise break
// the row markup (or inject). Doc bodies go through marked.parse() then
// DOMPurify.sanitize() (ARCH-244) — they are not assumed safe.
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

const sideEl=document.getElementById('engSidebar'),
      viewEl=document.getElementById('view'),
      qEl=document.getElementById('q'),
      pillsEl=document.getElementById('projpills');
const docDrawer=document.getElementById('doc-drawer');

let kind=null;              // selected doc type (null = landing)
let project='__all';        // project filter
let openRow=null;           // inline-expanded doc index

const kindsPresent = DATA.kindOrder.filter(k => docs.some(d=>d.kind===k));
// Project section order: PROJECT_ORDER first (Convivy & Shared leads), then any
// others alphabetically — applied to both the grouped listing and the pills.
const PORD = DATA.projectOrder;
function projRank(p){ const i = PORD.indexOf(p); return i === -1 ? PORD.length : i; }
const projsPresent = [...new Set(docs.map(d=>d.project))]
  .sort((a,b)=> (projRank(a)-projRank(b)) || a.localeCompare(b));
function kindCount(k){ return docs.filter(d=>d.kind===k).length; }
function docHref(d){
  // /reference/doc/{id} 404s for a document outside the "team" audience —
  // the Library's own entitlement check, which that page enforces itself
  // (reference_doc_build.py). `d.human` already tracks exactly that
  // ("team" in audience, set in load_docs()/arch_docs() below) — a doc this
  // page lists but that flag can't serve gets no Open link, so the link
  // shown is never a 404 behind a click. ARCH-log entries (kind 'arch') are
  // parsed from a flat file at render time rather than being separate
  // Library documents, and are marked human:false for exactly that reason,
  // so they fall out of this same check.
  if(!d.human) return null;
  return '/reference/doc/' + encodeURIComponent(d.id);
}
// The same two conditions the Edit control needs: a role holding kb_write
// (server.py's substitution, cosmetic — kb_write_api's own gate is the wall)
// and a document the write API will actually serve. `d.human` is the audience
// flag, and an ARCH-log entry is not a Library document at all, so neither a
// checkout nor a save could ever land for one.
function canEdit(d){ return DATA.canKbWrite === 'true' && !!d.human; }
function q(){ return qEl.value.trim().toLowerCase(); }

// Body text is no longer in the payload (issue_01M0Y7GZED2THYETHCT8NPHHDS —
// bodies were 94% of a 10.5 MB page), so a body-only match comes from
// `/api/memory/search` instead, fetched by `runBodySearch` below and cached
// in `bodyMatchIds` for the query it was fetched for. `matchQ` ORs the two:
// the instant client-side match on fields that stay in the thin payload,
// plus whatever the last body search found for the CURRENT query — a stale
// body-match set (from a query the user has since changed) must not apply,
// which is why this checks `bodyMatchQuery === s` rather than just presence.
function matchQ(d){
  const s=q(); if(!s) return true;
  // include the reference (ADR-0021 / ARCH-139 / slug) and id so an agent's
  // citation finds the doc, not just title/body text.
  if (d.reflabel.toLowerCase().includes(s) || d.id.toLowerCase().includes(s)
      || d.title.toLowerCase().includes(s) || d.blurb.toLowerCase().includes(s)) return true;
  return bodyMatchQuery === s && bodyMatchIds !== null && bodyMatchIds.has(d.id);
}

let bodyMatchIds = null;    // Set of ids matching the current query's body search
let bodyMatchQuery = '';    // the query bodyMatchIds is valid for ('' = none run yet)
// The query a body search last FAILED for (non-2xx or network error), or null.
// A failure must not resolve to an empty bodyMatchIds — that would read as
// "confirmed zero body matches", indistinguishable from a real empty result
// set, which is the same silent-loss failure mode this issue exists to close
// (server.py's memory_api 503 on a Library outage), just moved onto the
// client. `listing()` below renders this state instead of hiding it.
let bodySearchError = null;
let bodySearchSeq = 0;      // guards a stale response from clobbering a newer query
let bodySearchTimer = null;
const MIN_BODY_SEARCH_LEN = 2;   // below this, don't fire a round trip per keystroke

function runBodySearch(s){
  const seq = ++bodySearchSeq;
  fetch('/api/memory/search?q=' + encodeURIComponent(s))
    .then(r => { if (!r.ok) throw new Error('search failed: ' + r.status); return r.json(); })
    .then(j => {
      if (seq !== bodySearchSeq) return;   // a newer query has since started
      bodyMatchIds = new Set(j.ids || []);
      bodyMatchQuery = s;
      bodySearchError = null;
      renderCurrent();
    })
    .catch(() => {
      if (seq !== bodySearchSeq) return;
      // Leave bodyMatchIds/bodyMatchQuery untouched — they still describe
      // whatever the last SUCCESSFUL search found, not this failure. The
      // instant field match still stands; `bodySearchError` is what tells
      // `listing()`/`searchLanding()` the body half of this query's results
      // may be incomplete.
      bodySearchError = s;
      renderCurrent();
    });
}

// 'none' (query too short to search), 'pending' (debounced/in flight),
// 'error' (last attempt for this exact query failed), or 'ok' (a result,
// possibly empty, is in hand for this exact query).
function bodySearchStatus(s){
  if (s.length < MIN_BODY_SEARCH_LEN) return 'none';
  if (bodySearchError === s) return 'error';
  if (bodyMatchQuery === s) return 'ok';
  return 'pending';
}

const bodyCache = new Map();   // doc id -> fetched markdown body

function fetchBody(d){
  if (bodyCache.has(d.id)) return Promise.resolve(bodyCache.get(d.id));
  return fetch('/api/memory/documents/' + encodeURIComponent(d.id) + '/body')
    .then(r => { if (!r.ok) throw new Error('fetch failed'); return r.json(); })
    .then(j => { const body = j.body || ''; bodyCache.set(d.id, body); return body; });
}
function inProj(d){ return project==='__all' || d.project===project; }

function buildSide(){
  // Home button — aria-pressed="true" when on the landing (kind===null)
  let h = `<button type="button" class="kind" data-k="__home" aria-pressed="${kind===null?'true':'false'}">🏠 Engineering home</button>`;
  h += `<div class="grouphdr">Browse by type</div>`;
  for(const k of kindsPresent){
    h += `<button type="button" class="kind" data-k="${esc(k)}" aria-pressed="${kind===k?'true':'false'}" title="${esc(kindTip(k))}"><span>${esc(kindLabel(k))}</span><span class="ct">${kindCount(k)}</span></button>`;
  }
  sideEl.innerHTML = h;
  sideEl.querySelectorAll('.kind').forEach(el=>{
    // The typed query survives a kind switch — matchQ doesn't care which kind
    // is selected, so clearing it here would discard the reader's query at
    // the exact moment they try to narrow it with a type pill.
    el.onclick = ()=>{ const k=el.dataset.k; kind = (k==='__home')?null:k; openRow=null; render(); };
  });
}

function buildPills(){
  if(kind===null){ pillsEl.innerHTML=''; return; }
  let h = `<button type="button" class="eng-pill" data-p="__all" aria-pressed="${project==='__all'?'true':'false'}">All projects</button>`;
  for(const p of projsPresent){
    h += `<button type="button" class="eng-pill" data-p="${esc(p)}" aria-pressed="${project===p?'true':'false'}">${esc(L[p]||p)} <span style="opacity:.6">${docs.filter(d=>d.kind===kind&&d.project===p).length}</span></button>`;
  }
  pillsEl.innerHTML = h;
  pillsEl.querySelectorAll('.eng-pill').forEach(el=>{ el.onclick=()=>{ project=el.dataset.p; openRow=null; render(); }; });
}

function rowHtml(d){
  const draft = d.status!=='current' ? `<span class="tag draft">${esc(d.status)}</span>` : '';
  const rev = d.reviewed ? `<span class="tag">${esc(d.reviewed)}</span>` : '';
  const href = docHref(d);
  // Edit opens the editor in the drawer, on this page, which is the whole
  // point (issue_01M17HYDBQ4GPWHPFS56M6QQPH) — it used to be the Open link
  // below, so fixing a typo cost the reader their place in the listing. Open
  // is the document's own permalink, kept because this row is the only place
  // on the site that offers one.
  const edit = canEdit(d)
    ? `<button type="button" class="edit" data-edit="${d._i}">Edit</button>` : '';
  const open = href ? `<a class="edit" href="${esc(href)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Open ↗</a>` : '';
  // The body is fetched on demand (fetchBody, driven by clickRow) rather than
  // read off `d`, which no longer carries one. `bodyCache` holds it once the
  // fetch lands; until then this row shows a loading placeholder rather than
  // nothing, so the toggle reads as "in progress" instead of broken.
  let inset = '';
  if (openRow === d._i) {
    if (d._bodyError) {
      inset = `<div class="inset"><div class="content note">Couldn't load this document. Try again.</div></div>`;
    } else if (bodyCache.has(d.id)) {
      inset = `<div class="inset"><div class="content">${DOMPurify.sanitize(marked.parse(bodyCache.get(d.id)))}</div></div>`;
    } else {
      inset = `<div class="inset"><div class="content note">Loading…</div></div>`;
    }
  }
  // Reference on its own line above the title, so every title starts at the same
  // left edge (no per-row L-R jitter from a variable-width leading element).
  return `<div class="row" role="button" tabindex="0" data-i="${d._i}">
    <div class="rref"><span class="ref">${esc(d.reflabel)}</span></div>
    <div class="top"><span class="title">${esc(d.title)}</span>
      <span class="tags"><span class="tag proj">${esc(L[d.project]||d.project)}</span>${rev}${draft}${edit}${open}</span></div>
    <div class="blurb">${esc(d.blurb)}</div>${inset}</div>`;
}

// The row's Edit control. `stopPropagation` keeps the click off the row, which
// would otherwise toggle the inset underneath the editor being opened. Assigned
// rather than written as an inline `onclick` attribute, since assigning
// `el.onclick` replaces an inline handler outright.
function wireEditButtons(){
  viewEl.querySelectorAll('[data-edit]').forEach(el=>{
    el.onclick = e => { e.stopPropagation(); startEditing(+el.dataset.edit); };
  });
}

function landing(){
  // The landing page is where the reader arrives, so it's where the search
  // box has to work too — a query used to only filter once a kind was
  // selected, which meant the box accepted text and ran a real body search
  // (runBodySearch, below) that nothing ever rendered (issue_01M17443PJXY9TACWDGMMMTA9Z).
  // Any non-empty query goes to searchLanding, same as listing() applies
  // matchQ unconditionally — MIN_BODY_SEARCH_LEN gates the round trip
  // (qEl.oninput, below), not the render, since the instant field match is
  // local and free even for a single keystroke. Below that length matchQ
  // just won't have a body-match set to OR in yet.
  const s = q();
  if (s) { searchLanding(s); return; }
  const human = docs.filter(d=>d.human);
  let h = `<h2 class="area">Knowledge Base</h2>
    <p class="note">The company knowledge base, human-readable. Start with the ones written for people, or browse by type on the left. Click any doc to read it; long docs open in a side panel. <b>Edit</b> opens the editor in that panel, where you are reading it.</p>
    <a class="archlink" href="/reference/arch-log">→ ARCH platform decision log (substrate)</a>`;
  if(human.length){
    h += `<h3 style="margin-top:22px">Human-readable docs</h3><div class="feat">`;
    for(const d of human.sort((a,b)=>a.title.localeCompare(b.title))){
      h += `<div class="card" role="button" tabindex="0" data-i="${d._i}"><div class="cref">${esc(d.reflabel)}</div><div class="t">${esc(d.title)}</div><div class="b">${esc(d.blurb)}</div></div>`;
    }
    h += `</div>`;
  }
  h += `<h3>Browse the full KB</h3><div>`;
  for(const k of kindsPresent){ h += `<span class="eng-stat" role="button" tabindex="0" data-k="${esc(k)}" title="${esc(kindTip(k))}"><b>${kindCount(k)}</b> ${esc(kindLabel(k))}</span>`; }
  h += `</div>`;
  viewEl.innerHTML = h;
  viewEl.querySelectorAll('.card').forEach(el=> {
    el.onclick=()=> openDoc(+el.dataset.i);
    el.onkeydown=e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); openDoc(+el.dataset.i); } };
  });
  viewEl.querySelectorAll('.eng-stat').forEach(el=> {
    el.onclick=()=>{ kind=el.dataset.k; openRow=null; render(); };
    el.onkeydown=e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); kind=el.dataset.k; openRow=null; render(); } };
  });
}

function searchLanding(s){
  // A query on the landing page has no kind selected yet, so it searches the
  // whole corpus rather than one type — matchQ (title/reflabel/id/blurb, plus
  // a body match once runBodySearch resolves for this exact query) doesn't
  // care which kind a doc is, and results group by kind here instead of by
  // project the way a single-kind listing() does.
  const shown = docs.filter(d=>matchQ(d));
  const bstat = bodySearchStatus(s);
  const searchNote = bstat === 'error'
    ? `<p class="note eng-search-warn" role="status">Couldn't check document text just now — showing title/reference matches only.</p>`
    : bstat === 'pending'
      ? `<p class="note" role="status">Checking document text…</p>`
      : '';
  if(!shown.length){ viewEl.innerHTML = `<h2 class="area">Search</h2>${searchNote}<div class="empty">Nothing matches.</div>`; return; }
  let h = `<h2 class="area">Search <span class="note">· ${shown.length}</span></h2>${searchNote}`;
  const byk = {}; shown.forEach(d=>{ (byk[d.kind] ||= []).push(d); });
  for(const k of kindsPresent.filter(k=>byk[k])){
    h += `<div class="grp">${esc(kindLabel(k))} <span style="font-weight:600;opacity:.6">(${byk[k].length})</span></div>`;
    h += byk[k].map(rowHtml).join('');
  }
  viewEl.innerHTML = h;
  viewEl.querySelectorAll('.row').forEach(el=> {
    el.onclick=()=> clickRow(+el.dataset.i);
    el.onkeydown=e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); clickRow(+el.dataset.i); } };
  });
  wireEditButtons();
}

function listing(){
  const shown = docs.filter(d=>d.kind===kind && inProj(d) && matchQ(d));
  // A body-search failure or an in-flight search must be visible, not just
  // absorbed into whatever the instant field match already found — otherwise
  // "couldn't check body text" and "genuinely no body match" render
  // identically, which is the failure mode `bodySearchError` exists to avoid.
  const bstat = bodySearchStatus(q());
  const searchNote = bstat === 'error'
    ? `<p class="note eng-search-warn" role="status">Couldn't check document text just now — showing title/reference matches only.</p>`
    : bstat === 'pending'
      ? `<p class="note" role="status">Checking document text…</p>`
      : '';
  if(!shown.length){ viewEl.innerHTML = `<h2 class="area">${esc(kindLabel(kind))}</h2>${searchNote}<div class="empty">Nothing matches.</div>`; return; }
  let h = `<h2 class="area">${esc(kindLabel(kind))} <span class="note">· ${shown.length}</span></h2>${searchNote}`;
  if(kindTip(kind)) h += `<p class="note" style="margin:-6px 0 16px;max-width:680px">${esc(kindTip(kind))}</p>`;
  if(project==='__all'){
    // group by project
    const byp={}; shown.forEach(d=>{ (byp[d.project] ||= []).push(d); });
    for(const p of projsPresent.filter(p=>byp[p])){
      // Links to /projects/<name> (epic issue_01M0AK8CM3ADKDTG8PM6GRCP8H, item 5):
      // open PRs, recently merged PRs, recent commits and the bead, all in one
      // place — this project group header is where a project name already
      // renders as a label on this page, so it's the entry point rather than a
      // new nav item. Unknown/unplaced project slugs 404 there, same as any
      // other per-id link on this site (issues, signals).
      h += `<div class="grp"><a class="grp-link" href="/projects/${encodeURIComponent(p)}">${esc(L[p]||p)}</a> <span style="font-weight:600;opacity:.6">(${byp[p].length})</span></div>`;
      h += byp[p].map(rowHtml).join('');
    }
  } else {
    h += shown.map(rowHtml).join('');
  }
  viewEl.innerHTML = h;
  viewEl.querySelectorAll('.row').forEach(el=> {
    el.onclick=()=> clickRow(+el.dataset.i);
    el.onkeydown=e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); clickRow(+el.dataset.i); } };
  });
  wireEditButtons();
}

function clickRow(i){
  const d=docs[i];
  if(d.len > DATA.drawerThreshold){ openDoc(i); return; }   // long → drawer
  // A row can come from listing() (a kind selected) or searchLanding() (still
  // on the landing page) — renderCurrent() re-renders whichever produced it.
  if (openRow === i) { openRow = null; renderCurrent(); return; }  // already open → collapse
  openRow = i;
  d._bodyError = false;
  renderCurrent();   // renders the loading placeholder immediately
  fetchBody(d).then(() => {
    if (openRow === i) renderCurrent();          // re-render now the body is cached
  }).catch(() => {
    if (openRow === i) { d._bodyError = true; renderCurrent(); }
  });
}

// **THE EDITOR MOUNTS IN THE DRAWER AND NOWHERE ELSE, AND THAT IS A LEASE
// DECISION RATHER THAN A LAYOUT ONE.** Opening it takes a Library checkout: a
// four-hour lock that blocks every other writer on the document, the Library's
// own kb_update and kb_propose included. The listing is rebuilt from state on
// every keystroke in the search box, so an editor mounted inside a row would be
// destroyed by a stray character — textarea gone, checkout still held, nothing
// left on screen to release it with. The drawer is outside `viewEl`, so no
// listing re-render can reach it, and it has exactly one way to shut
// (`ds:drawer-close`, covering the button, the backdrop and Escape), which is
// the one place the checkin has to be wired.
//
// Every path that takes the drawer away from an open editor therefore comes
// through `releaseEditor` first, and a reader with unsaved changes who cancels
// the prompt stops that path dead rather than losing the draft.
function releaseEditor(){
  if(!window.KbEditor || window.KbEditor.docId() === null) return true;
  return window.KbEditor.close();
}

function drawerEditorMount(){
  return document.querySelector('#doc-drawer [data-kb-mount]');
}

// The document the drawer is currently showing. `fetchBody` is a round trip, so
// document A's body can land after the reader has already moved the drawer to
// B; repainting then would overwrite B's markup with A's and leave B's live
// editor painted into a node no longer in the page — draft invisible, checkout
// still held. Every deferred repaint checks this first.
let drawerDocId = null;

// Open `docs[i]` in the drawer. `opts.edit` also opens the editor on it, which
// is what the row's Edit button and the drawer's own Edit button both call.
function openDoc(i, opts){
  opts = opts || {};
  const d=docs[i];
  const editing = window.KbEditor ? window.KbEditor.docId() : null;
  // Re-opening the SAME document keeps its editor: the markup below is rebuilt
  // either way, and the controller is remounted into the fresh element at the
  // end. A DIFFERENT document means this drawer is about to stop showing the
  // one being edited, so the checkout goes back first.
  if(editing !== null && editing !== d.id && !releaseEditor()) return;
  // Update the ds drawer title (set via the labelledby id)
  const titleEl = document.getElementById('doc-drawer-title');
  if(titleEl) titleEl.textContent = d.title;
  const href = docHref(d);
  const openHtml = href
    ? `<a class="eng-edit-link" href="${esc(href)}" target="_blank" rel="noopener">Open ↗</a>`
    : '';
  const editHtml = canEdit(d)
    ? `<button type="button" class="eng-edit-btn" data-drawer-edit="${d._i}">Edit</button>` : '';
  const metaHtml = `<div class="doc-meta">
    <span class="tag proj">${esc(L[d.project]||d.project)}</span>
    <span class="tag">${esc(kindLabel(d.kind))}</span>
    ${d.status!=='current'?`<span class="tag draft">${esc(d.status)}</span>`:''}
    ${editHtml}${openHtml}
  </div>`;
  // The editor is painted into this element, under the rendered document, so a
  // member edits the text they are reading without the page moving.
  const mountHtml = `<div data-kb-mount></div>`;
  const bodyEl = document.querySelector('#doc-drawer .drawer-body');
  drawerDocId = d.id;
  function paintDrawer(contentHtml){
    if(!bodyEl || drawerDocId !== d.id) return;
    bodyEl.innerHTML = metaHtml + contentHtml + mountHtml;
    const btn = bodyEl.querySelector('[data-drawer-edit]');
    if(btn) btn.onclick = ()=> startEditing(+btn.dataset.drawerEdit);
    // The document's own markup was just replaced, so an editor that is still
    // legitimately open (same document) needs painting into the new element.
    // Its draft, its checkout and its heartbeat are untouched by this.
    if(window.KbEditor && window.KbEditor.docId() === d.id){
      const mount = drawerEditorMount();
      if(mount) window.KbEditor.remount(mount);
    }
  }
  // Open the ds drawer immediately with a loading placeholder — the body is
  // fetched on demand (fetchBody) rather than read off `d`, which no longer
  // carries one — then swap in the rendered content once it lands.
  paintDrawer(`<div class="eng-doc-content note">Loading…</div>`);
  if(docDrawer && docDrawer._dsOpen) docDrawer._dsOpen();
  // Mounted directly rather than back through startEditing: the drawer is now
  // showing this document and the mount exists, so re-testing that would only
  // risk bouncing between the two functions.
  if(opts.edit){
    const mount = drawerEditorMount();
    if(mount) window.KbEditor.open(mount, d.id);
  }
  fetchBody(d).then(body => {
    paintDrawer(`<div class="eng-doc-content">${DOMPurify.sanitize(marked.parse(body))}</div>`);
  }).catch(() => {
    paintDrawer(`<div class="eng-doc-content note">Couldn't load this document. Try again.</div>`);
  });
}

// Open the editor on `docs[i]`, bringing the drawer to that document first if
// it is showing another one.
//
// **`drawerDocId !== d.id` IS WHAT STOPS THE EDITOR OPENING ON THE WRONG
// DOCUMENT.** The mount element exists whenever the drawer has ever been
// painted, so a reader who opens document A in the drawer and then clicks
// Edit on document B's row would otherwise get B's editor painted under A's
// text — editing one document while reading another, which is the exact
// failure this feature exists to avoid.
function startEditing(i){
  const d=docs[i];
  if(!canEdit(d)) return;
  const mount = drawerEditorMount();
  if(!mount || !docDrawer || !docDrawer.classList.contains('open')
     || drawerDocId !== d.id
     || (window.KbEditor && window.KbEditor.docId() !== null
         && window.KbEditor.docId() !== d.id)){
    openDoc(i, {edit: true});
    return;
  }
  window.KbEditor.open(mount, d.id);
}

// **CLOSING THE DRAWER CHECKS THE DOCUMENT BACK IN.** This is the one event
// that covers all three ways it shuts — the X, the backdrop and Escape — which
// is why design_system's drawer dispatches it. Without this, hitting Escape
// over an open editor would leave a four-hour lock held with the editor gone
// from the screen.
//
// The drawer has already closed by the time this fires, so a reader who
// declines to discard an unsaved draft gets it reopened rather than prevented;
// `close()` returning false means nothing was released.
if(docDrawer){
  docDrawer.addEventListener('ds:drawer-close', function(){
    if(!window.KbEditor || window.KbEditor.docId() === null) return;
    if(!window.KbEditor.close() && docDrawer._dsOpen) docDrawer._dsOpen();
  });
}

// The content half of render() — no side/pills rebuild, so this is cheap
// enough to call on every keystroke via qEl.oninput below.
function renderCurrent(){ if(kind===null){ landing(); } else { listing(); } }
function render(){ buildSide(); buildPills(); renderCurrent(); if(kind!==null) qEl.focus(); }
qEl.oninput = ()=>{
  renderCurrent();   // instant match: field-level within a kind, or the
                      // landing/search-results split (landing(), above) on the landing page
  const s = q();
  clearTimeout(bodySearchTimer);
  bodySearchError = null;
  if (s.length < MIN_BODY_SEARCH_LEN) { bodyMatchIds = null; bodyMatchQuery = ''; bodySearchSeq++; return; }
  bodySearchTimer = setTimeout(() => runBodySearch(s), 250);
};
render();

// Deep link: /reference#doc=<url-encoded id> opens that doc's drawer — the home
// page's "Recent Docs" items link here. Unknown/missing id is a silent no-op
// (the browse landing renders as usual).
function openFromHash(){
  const m = (location.hash||'').match(/^#doc=(.+)$/);
  if(!m) return;
  let id; try{ id = decodeURIComponent(m[1]); }catch(e){ return; }
  const i = docs.findIndex(d=>d.id===id);
  if(i>=0) openDoc(i);
}
window.addEventListener('hashchange', openFromHash);
// The initial (page-load) deep link must fire AFTER the drawer is initialized:
// openDoc() calls docDrawer._dsOpen, which the shared drawer JS wires on
// DOMContentLoaded. Running openFromHash synchronously here (parse time) beat
// that init, so the drawer content was set but never opened — a deep-linked
// doc silently dumped the viewer on /reference top-level (the operator 2026-07-17). window
// 'load' fires after DOMContentLoaded, so _dsOpen exists by then. hashchange
// (above) already runs post-load, so it was never affected.
if(document.readyState==='complete'){ openFromHash(); }
else{ window.addEventListener('load', openFromHash); }

// Mobile sidebar toggle
const engToggle = document.getElementById('engSidebarToggle');
const engSidebar = document.getElementById('engSidebar');
if(engToggle && engSidebar) {
  engToggle.addEventListener('click', function() {
    const open = engSidebar.classList.toggle('open');
    engToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    engToggle.textContent = open ? '✕ Close' : '☰ Browse kinds';
  });
}
"""


def render(docs: list[dict], scope: frozenset[str], whole_fleet: bool) -> str:
    # `body` is dropped here, not upstream in `load_docs()`/`arch_docs()` —
    # bodies are 94% of this payload across 1158 rows, 10.5 MB of a page that
    # renders on every request (issue_01M0Y7GZED2THYETHCT8NPHHDS). The drawer
    # and inline expansion fetch a body on demand from `memory_api` instead
    # (`/api/memory/documents/{id}/body`, covering both the Library-backed KB
    # docs and the in-process ARCH entries); search for a body-only phrase
    # goes through `/api/memory/search`, which the client's `matchQ` calls
    # for the term client-side matching (reflabel/id/title/blurb) can't
    # cover. `blurb` and `len` stay — the drawer threshold at `clickRow`
    # reads `d.len`, not the body, so that logic is unaffected.
    #
    # `scope`/`whole_fleet` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): a doc
    # whose project is outside `scope` is dropped before `thin_docs` is even
    # built, so it never reaches the inline JSON this page embeds — the
    # "filter before grouping" rule `issue_build` applies, applied here to
    # the payload rather than a server-rendered list. `projectLabel`/
    # `projectOrder` are then narrowed to the projects actually PRESENT in
    # what survives, rather than shipped as the full fleet-wide maps
    # regardless of scope — the same "an empty pill discloses an unreleased
    # product's existence" rule ARCH-228 states for the contractor bundle,
    # applied here to a scoped full-tier viewer instead of a tier.
    docs = docs if whole_fleet else [d for d in docs if d["project"] in scope]
    thin_docs = [{k: v for k, v in d.items() if k != "body"} for d in docs]
    present_projects = {d["project"] for d in thin_docs}
    project_label = {p: label for p, label in PROJECT_LABEL.items() if p in present_projects}
    project_order = [p for p in PROJECT_ORDER if p in present_projects]
    payload = json.dumps({
        "docs": thin_docs,
        "projectLabel": project_label,
        "projectOrder": project_order,
        "kindOrder": VISIBLE_KINDS,
        "kindLabel": KIND_LABEL,
        "kindTooltip": KIND_TOOLTIP,
        "drawerThreshold": DRAWER_THRESHOLD,
        # server.py substitutes this from the viewer's role, the same
        # `__CAN_KB_WRITE__` token the document's own page carries. Cosmetic:
        # kb_write_api's own authz gate, plus this page's full-tier gate, are
        # the wall. It rides in the payload rather than on an attribute because
        # the rows this gates are built client-side, from `DATA`.
        "canKbWrite": "__CAN_KB_WRITE__",
    }, ensure_ascii=False).replace("</", "<\\/")
    marked_src = MARKED_JS.read_text(encoding="utf-8")
    dompurify_src = DOMPURIFY_JS.read_text(encoding="utf-8")
    drawer = ds.drawer_html("doc-drawer", "", "")
    body = (
        f'<script>{marked_src}</script>\n'
        f'<script>{dompurify_src}</script>\n'
        f'<button class="eng-sidebar-toggle" id="engSidebarToggle"'
        f' aria-expanded="false" aria-controls="engSidebar">&#9776; Browse kinds</button>\n'
        f'<div class="eng-layout">\n'
        f'  <nav class="eng-sidebar" id="engSidebar" aria-label="Document kind browser"></nav>\n'
        f'  <main class="eng-main">\n'
        f'    <div class="eng-controls">\n'
        f'      <label for="q" class="sr-only">Filter documents</label>\n'
        f'      <input id="q" type="search" placeholder="Filter by title or text…" autocomplete="off">\n'
        f'      <div class="eng-pills" id="projpills" role="group" aria-label="Project filter"></div>\n'
        f'    </div>\n'
        f'    <div id="view"></div>\n'
        f'  </main>\n'
        f'</div>\n'
        f'{drawer}\n'
        f'<script id="data" type="application/json">{payload}</script>\n'
        # The shared editor controller has to define window.KbEditor before the
        # browse JS runs: `render()` at the end of that script paints rows whose
        # Edit buttons call straight into it.
        f'<script>{kb_editor.EDITOR_JS}</script>\n'
        f'<script>\n{_BROWSE_JS}\n</script>'
    )
    return ds.page_shell(
        title="Knowledge Base",
        nav_html=ds.nav_component("/reference", "full"),
        body=body,
        extra_css=_BROWSE_CSS + kb_editor.EDITOR_CSS,
    )


def live_html(scope: frozenset[str], whole_fleet: bool) -> str:
    # The KB browse = indexed KB docs + the substrate ARCH log surfaced as `arch`
    # docs (ARCH-196), so a cited ARCH-NNN is browsable alongside the ADRs.
    return render(load_docs() + arch_docs(), scope, whole_fleet)


def get_arch_body(doc_id: str) -> str | None:
    """One ARCH entry's body, by the id `arch_docs()` mints for it
    (`substrate/arch/{anchor}`), or `None`. Backs `memory_api.document_body`
    for the half of `/reference`'s corpus the Library does not serve
    (issue_01M0Y7GZED2THYETHCT8NPHHDS) — `arch_docs()` caches the parse, so
    this doesn't re-read and re-parse the log on every call."""
    for e in arch_docs():
        if e["id"] == doc_id:
            return e["body"]
    return None


def search_arch_bodies(query: str) -> list[str]:
    """Ids of every ARCH entry whose body contains `query` (case-insensitive
    substring) — the same test the client's own `matchQ` used to run against
    `d.body` before it was dropped from the payload. The Library's
    `/reference/search` covers the KB half of the corpus; this covers the
    half the Library does not serve at all (issue_01M0Y7GZED2THYETHCT8NPHHDS)."""
    q = query.lower()
    return [e["id"] for e in arch_docs() if q in e["body"].lower()]


# An ARCH entry heading: `## ARCH-139: title` or `## ARCH-139 — title` (the
# separator varies across the log's history). Captures the number and the title.
_ARCH_HEAD = re.compile(r"^#{2,6}\s+ARCH-0*(\d+)\s*[:—.\-]?\s*(.*)$")

# Old ARCH-log entries head with the id and title on one line
# ("## ARCH-218: team-site redesign Phase 1") and carry a separate
# "**Date:**" line. ARCH-371 moved the format to a bare heading with the
# title as the body's opening bold line instead ("## ARCH-441" then
# "**title** (2026-08-03)"). `_ARCH_HEAD` above captures the title from the
# heading either way, but only the old form has one there — 69 of 442
# entries, including every one newer than ARCH-370, come back with an empty
# title. `_arch_title_and_date` below is the fallback for those.
_ARCH_DATE_LINE = re.compile(r"\*\*Date:\*\*\s*(\d{4}-\d{2}-\d{2})")
# Anchored to the very start of the paragraph being searched (default `^`,
# no MULTILINE, matches only the string's first position) and the title
# capture excludes `*` so it can't cross into a second `**` pair. A
# non-anchored, unbounded `.+?` here used to let the match start at the
# entry's FIRST bold marker and skip ahead to whatever bold run happened to
# precede a later, unrelated parenthesis, producing a garbled title spanning
# multiple sentences. If the paragraph doesn't open with "**title** (...)",
# there is no title to recover — the search returns no match and the caller
# falls back to the ref rather than guessing.
_ARCH_TITLE_PAREN = re.compile(r"^\s*\*\*([^*]+?)\*\*\s*\(([^)]*)\)", re.DOTALL)
_ARCH_DATE_IN_PAREN = re.compile(r"\d{4}-\d{2}-\d{2}")


def _arch_title_and_date(ref: str, title: str, body: str) -> tuple[str, str]:
    """Title and date for one parsed ARCH entry, filling in what the new
    (post-ARCH-370) format leaves out of the heading-captured `title`.

    Falls back to the body's opening "**title** (date, ...)" line for the
    title, skipping a leading blockquote paragraph first — some entries open
    with a "> **[SUPERSEDED]** ..." pointer before their own title (e.g.
    ARCH-425), and that pointer would otherwise get taken for the title.
    """
    title = title.strip()
    date = ""
    dm = _ARCH_DATE_LINE.search(body)
    if dm:
        date = dm.group(1)
    if not title:
        paras = body.split("\n\n")
        idx = 0
        while idx < len(paras) and paras[idx].strip().startswith(">"):
            idx += 1
        tm = _ARCH_TITLE_PAREN.search("\n\n".join(paras[idx:]))
        if tm:
            title = re.sub(r"\s+", " ", tm.group(1)).strip()
            if not date:
                pm = _ARCH_DATE_IN_PAREN.search(tm.group(2))
                if pm:
                    date = pm.group(0)
    return title or ref, date


def parse_arch_log(text: str) -> list[dict]:
    """Split the ARCH log into entries, each {num, ref, anchor, title, date,
    body}, ordered ascending by ARCH number. `num` is the integer (139); `ref`
    is the display form "ARCH-139"; `anchor` is "arch-139" so #arch-139 jumps
    to it. `title` is never empty — see `_arch_title_and_date` for the
    post-ARCH-370 fallback; `date` is "" when the entry's body carries none."""
    entries: list[dict] = []
    cur: dict | None = None
    for line in build._strip_frontmatter(text).split("\n"):
        m = _ARCH_HEAD.match(line)
        if m:
            if cur is not None:
                entries.append(cur)
            n = int(m.group(1))
            cur = {"num": n, "ref": f"ARCH-{n}", "anchor": f"arch-{n}",
                   "title": m.group(2).strip(), "_body": []}
        elif cur is not None:
            cur["_body"].append(line)
    if cur is not None:
        entries.append(cur)
    entries.sort(key=lambda e: e["num"])
    for e in entries:
        e["body"] = "\n".join(e["_body"]).strip()
        del e["_body"]
        e["title"], e["date"] = _arch_title_and_date(e["ref"], e["title"], e["body"])
    return entries


# ---------------------------------------------------------------------------
# Page-local CSS: ARCH Log (/engineering/arch-log)
# ---------------------------------------------------------------------------

_ARCH_LOG_CSS = r"""
/* The closed-log banner. Styled as a NOTE rather than an error: nothing here is
   broken, and the entries below are still correct about what they decided. It
   only has to stop a reader taking a frozen log for a live one — which the page
   cannot otherwise convey, since it renders exactly as it did while open. */
.arch-closed { background:var(--accent-wash, rgba(200,150,40,.10));
               border:1px solid var(--line); border-left:3px solid var(--warn, #b4700e);
               border-radius:var(--radius, 6px); padding:10px 14px; margin:0 0 14px;
               font-size:13px; line-height:1.55; color:var(--ink); }
.arch-closed a { color:inherit; }

/* ---- Two-pane app shell ------------------------------------------------ */
.arch-layout {
  display: flex;
  align-items: stretch;
  height: calc(100vh - 90px);
  overflow: hidden;
}

/* ---- TOC sidebar ------------------------------------------------------- */
.arch-sidebar {
  width: 280px;
  min-width: 280px;
  height: 100%;
  overflow-y: auto;
  border-right: 1px solid var(--line);
  background: var(--surface);
  flex-shrink: 0;
}
html[data-theme="dark"] .arch-sidebar {
  border-right-width: 2px;
  box-shadow: 1px 0 0 rgba(0, 0, 0, .35);
}
.arch-jump {
  display: flex;
  gap: 6px;
  padding: 8px 8px 10px;
  position: sticky;
  top: 0;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
  z-index: 1;
}
.arch-jump input {
  font: inherit;
  font-size: 13px;
  padding: 6px 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--ink);
  width: 100%;
  transition: border-color 120ms, box-shadow 120ms;
}
.arch-jump input:focus {
  outline: none;
  border-color: var(--brand);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--brand) 18%, transparent);
}
@media (prefers-reduced-motion: reduce) { .arch-jump input { transition: none; } }
#toc { padding: 4px 8px 48px; }
a.toc {
  display: flex;
  gap: 8px;
  padding: 5px 9px;
  border-radius: 8px;
  color: var(--ink);
  text-decoration: none;
  font-size: 13px;
  transition: background 120ms;
}
a.toc:hover { background: var(--surface-2); }
a.toc.target { background: var(--brand-wash); color: var(--brand-ink); }
a.toc:focus-visible { outline: 2px solid var(--brand); outline-offset: -1px; }
@media (prefers-reduced-motion: reduce) { a.toc { transition: none; } }
a.toc .n { font-family: var(--mono); font-weight: 700; white-space: nowrap; color: var(--ink-2); }
a.toc.target .n { color: var(--brand-ink); }
a.toc .t { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
a.toc.target .t { color: var(--brand-ink); opacity: .9; }

/* ---- Main content pane ------------------------------------------------- */
.arch-main {
  flex: 1;
  height: 100%;
  overflow-y: auto;
  padding: 22px 40px 120px;
  background: var(--bg);
}
.arch-bar { display: flex; align-items: center; gap: 14px; margin-bottom: 8px; }
.arch-bar a { font-size: 13px; font-weight: 700; color: var(--brand-ink); text-decoration: none; }
.arch-bar a:hover { text-decoration: underline; }

/* ---- Entry blocks ------------------------------------------------------ */
.entry {
  scroll-margin-top: 14px;
  border-top: 1px solid var(--line);
  padding: 18px 0 6px;
  max-width: 760px;
}
.entry h2 { font-size: 18px; margin: 0 0 4px; color: var(--ink); scroll-margin-top: 14px; }
.entry:target h2 { color: var(--brand-ink); }
.entry .ref { font-family: var(--mono); font-size: 12px; color: var(--brand-ink); font-weight: 700; }
/* Entry body markdown */
.entry .content h1, .entry .content h2, .entry .content h3 { color: var(--ink); }
.entry .content a { color: var(--brand-ink); text-underline-offset: 2px; }
.entry .content p { margin: 12px 0; }
.entry .content pre {
  background: var(--surface-2);
  border: 1px solid var(--line);
  padding: 14px;
  border-radius: var(--radius);
  overflow: auto;
  font-size: 13px;
  font-family: var(--mono);
}
[data-theme="dark"] .entry .content pre { background: #0E0C09; color: #E2DAD0; }
.entry .content code { background: var(--brand-wash); color: var(--brand-ink); padding: 1px 5px; border-radius: 4px; font-size: 12.5px; font-family: var(--mono); }
.entry .content pre code { background: none; padding: 0; color: inherit; }
.entry .content table { border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 13.5px; }
.entry .content th, .entry .content td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; }
.entry .content th { background: var(--surface-2); font-weight: 650; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; color: var(--ink-2); }
.entry .content blockquote { border-left: 3px solid var(--brand); background: var(--brand-wash); border-radius: 0 var(--radius) var(--radius) 0; margin: 14px 0; padding: 8px 16px; color: var(--ink-2); }

/* ---- Mobile TOC toggle (≤640px only) ------------------------------------ */
#archSidebarToggle {
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
#archSidebarToggle:focus-visible { outline: 2px solid var(--brand); outline-offset: -2px; }

/* ---- Mobile ------------------------------------------------------------ */
@media (max-width: 640px) {
  #archSidebarToggle { display: flex; }
  .arch-layout { flex-direction: column; height: auto; overflow: visible; }
  .arch-sidebar {
    width: 100%;
    min-width: 0;
    height: auto;
    max-height: 0;
    overflow: hidden;
    border-right: none;
    border-bottom: 1px solid var(--line);
    transition: max-height 250ms ease;
  }
  .arch-sidebar.open { max-height: 80vh; overflow-y: auto; }
  .arch-main { height: auto; min-height: calc(100vh - 90px); padding: 16px 16px 80px; overflow: visible; }
  .arch-jump { position: relative; }
}
@media (prefers-reduced-motion: reduce) { .arch-sidebar { transition: none; } }
@media (min-width: 641px) and (max-width: 1024px) {
  .arch-sidebar { width: 220px; min-width: 220px; }
  .arch-main { padding: 18px 28px 80px; }
}
"""

# The client-side ARCH log JS — kept verbatim from the original.
_ARCH_LOG_JS = r"""
const DATA = JSON.parse(document.getElementById('data').textContent);
const E = DATA.entries;
marked.setOptions({ gfm:true, breaks:false });
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

// Render every entry with an anchor (#arch-139) so a citation jumps straight here.
document.getElementById('entries').innerHTML = E.map(e =>
  `<section class="entry" id="${esc(e.anchor)}">
     <h2><span class="ref">${esc(e.ref)}</span> — ${esc(e.title)}</h2>
     <div class="content">${DOMPurify.sanitize(marked.parse(e.body))}</div></section>`).join('') ||
  '<p class="note">The substrate ARCH log was not found on this host.</p>';

const tocEl = document.getElementById('toc'), jumpEl = document.getElementById('jump');
// Normalize what the user typed to a bare ARCH number: drop an optional "arch-"
// prefix AND leading zeros — the log's first 99 entries are cited padded
// ("ARCH-072") but parse to num 72, so the padded form must still match.
function normNum(s){ return s.trim().toLowerCase().replace(/^arch-?/, '').replace(/^0+(?=\d)/, ''); }
function buildToc(filter){
  const f = (filter||'').trim().toLowerCase();
  const fnum = normNum(filter);   // "ARCH-072", "072", "72" all → "72"
  tocEl.innerHTML = E.filter(e =>
      !f || String(e.num)===fnum || (fnum && String(e.num).includes(fnum)) || e.title.toLowerCase().includes(f) || e.ref.toLowerCase().includes(f)
    ).map(e => `<a class="toc" href="#${esc(e.anchor)}"><span class="n">${esc(e.ref)}</span><span class="t">${esc(e.title)}</span></a>`).join('')
    || '<div class="note" style="padding:10px">No match.</div>';
}
buildToc('');

jumpEl.oninput = () => buildToc(jumpEl.value);
// Enter on an exact number jumps straight to that entry (padded or not).
jumpEl.onkeydown = (ev) => {
  if(ev.key!=='Enter') return;
  const n = normNum(jumpEl.value);
  if(/^\d+$/.test(n) && E.some(e=>String(e.num)===n)){ location.hash = 'arch-'+n; }
};
// Highlight the current target in the TOC as the hash changes.
function markTarget(){
  const a = (location.hash||'').slice(1);
  tocEl.querySelectorAll('a.toc').forEach(el => el.classList.toggle('target', el.getAttribute('href')==='#'+a));
}
window.addEventListener('hashchange', markTarget); markTarget();
// A deep-link (/reference/arch-log#arch-139) lands before the entries are
// rendered, so the browser's native scroll-to-hash finds nothing. Scroll once
// now that the anchors exist.
if(location.hash){ const t=document.getElementById(decodeURIComponent(location.hash.slice(1))); if(t) t.scrollIntoView(); }
const archToggle = document.getElementById('archSidebarToggle');
const archSidebarEl = document.getElementById('archSidebar');
if(archToggle && archSidebarEl) {
  archToggle.addEventListener('click', function() {
    const open = archSidebarEl.classList.toggle('open');
    archToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    archToggle.textContent = open ? '✕ Close' : '☰ Jump to entry';
  });
}
"""


def arch_log_html() -> str:
    """Render the substrate ARCH log (substrate/decisions.md) — CLOSED at ARCH-441
    on 2026-08-03, so this page is HISTORY rather than the live decision surface.

    It stays because the log stays: 9,328 ARCH citations across five repos still
    point here, and each entry gets an anchor (#arch-139) plus a filterable jump
    index so a citation is reachable in one keystroke (the operator 2026-06-19). What
    changed is the claim this docstring used to make — "the authoritative platform
    decision log" — which stopped being true the moment the log closed.

    The banner says so on the page, because a human browsing a decision log with no
    end-marker has no way to tell a frozen one from a live one, and this one renders
    exactly as it always did (platform/decisions/0018)."""
    try:
        entries = parse_arch_log(ARCH_LOG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        entries = []
    payload = json.dumps({"entries": entries},
                         ensure_ascii=False).replace("</", "<\\/")
    marked_src = MARKED_JS.read_text(encoding="utf-8")
    dompurify_src = DOMPURIFY_JS.read_text(encoding="utf-8")
    body = (
        f'<script>{marked_src}</script>\n'
        f'<script>{dompurify_src}</script>\n'
        f'<button class="eng-sidebar-toggle" id="archSidebarToggle"'
        f' aria-expanded="false" aria-controls="archSidebar">&#9776; Jump to entry</button>\n'
        f'<div class="arch-layout">\n'
        f'  <nav class="arch-sidebar" id="archSidebar" aria-label="ARCH log navigation">\n'
        f'    <div class="arch-jump">\n'
        f'      <label for="jump" class="sr-only">Jump to ARCH entry</label>\n'
        f'      <input id="jump" type="search" placeholder="Jump to # or filter…" autocomplete="off">\n'
        f'    </div>\n'
        f'    <div id="toc"></div>\n'
        f'  </nav>\n'
        f'  <main class="arch-main">\n'
        f'    <div class="arch-closed" role="note">\n'
        f'      <b>This log is closed.</b> The ARCH sequence ended at '
        f'<a href="#arch-441">ARCH-441</a> on 2026-08-03 and nothing is appended to '
        f'it. Every entry below stands and every citation still resolves — but a '
        f'decision made after that date is an <b>ADR</b>, in the knowledge base '
        f'under its project. ARCH-441 says why.\n'
        f'    </div>\n'
        # No edit affordance here: an ARCH entry is not its own Library
        # document (arch_docs() parses it from substrate/decisions.md at
        # render time), and the log is closed besides, so there is nothing
        # for /reference/doc/{id} to serve and nothing left to append to.
        f'    <div class="arch-bar">\n'
        f'      <a href="/reference">← Knowledge Base</a>\n'
        f'    </div>\n'
        f'    <div id="entries"></div>\n'
        f'  </main>\n'
        f'</div>\n'
        f'<script id="data" type="application/json">{payload}</script>\n'
        f'<script>\n{_ARCH_LOG_JS}\n</script>'
    )
    return ds.page_shell(
        title="ARCH log",
        nav_html=ds.nav_component("/reference/arch-log", "full"),
        body=body,
        extra_css=_ARCH_LOG_CSS,
    )


def build_static() -> None:
    """Write static fallbacks (server.py serves these if a live render throws).

    **`engineering.html` is seeded empty, not `live_html()` of some scope**
    (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): this file is served to
    whichever viewer's live render failed, scoped or not, so a snapshot
    naming every project's documents would leak to a scoped viewer — the
    same reasoning `chart_house_build.build_static` and `issue_build.main`
    already apply to their own fallbacks. `engineering-arch-log.html` carries
    no per-project scoping (the ARCH log is a platform-wide, cross-project
    decision record, not filtered by viewer scope — see `arch_log_html`'s
    docstring), so it keeps seeding the real content."""
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "engineering.html").write_text(render([], frozenset(), False), encoding="utf-8")
    (OUT / "engineering-arch-log.html").write_text(arch_log_html(), encoding="utf-8")
    print("Wrote site/engineering.html (empty static fallback) + "
          "site/engineering-arch-log.html.")


if __name__ == "__main__":
    build_static()
