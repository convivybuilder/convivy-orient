#!/usr/bin/env python3.11
"""/projects/<name> — what's already readable about one project (epic
issue_01M0AK8CM3ADKDTG8PM6GRCP8H, item 5). Also renders the `/projects`
index (CQ·160, issue_01M57NRMZSZW5B5VNWCM4HVZPV), which lists every project
the viewer can open.

Open pull requests, recently merged ones and recent commits are all readable
live from the GitHub engine, and the project's bead doc is linked directly
rather than computed. The lede states the project's live Course position too
— waypoints passed of total, and the trailing-7-day pace
(`waypoints.course_progress()`, dashboards epic issue_01M2F3Q1B8J9E0WC5J1F1CMWA1,
item issue_01M2F4MZQ9NV94KGKPRDN2SY3Y) — and links to `/chart-house`, Orient's
own web surface for a Course, rather than duplicating that page's view here.

A project's name IS its GitHub repo name under the `convivy` org
(merge_queue_build.py's own convention for its org-wide PR search).
`build.py`'s COMPANY_PROJECTS, PRODUCT_ORDER, ENGINEERING_PROJECTS and
STANDALONE_PRODUCTS lists are the known project names (`known_projects()`
below); a name outside that union 404s rather than shelling out to `gh` for a
repo nobody declared a project. `known_projects()` is a function, not a
module constant, because three of those four lists are registry-derived
(CQ-171) and resolve fresh per read — caching their union at this module's
own import time would freeze it at whatever the registry held then, the
same reason `build.PROJECT_LABEL` is read live rather than copied.

Open PRs reuse merge_queue_build's fetch, classify and row-render
(`_open_prs_for_repo`, `_classify`, `_row`) so a PR reads "cleared / blocked /
draft / in-cycle" identically on both pages — one CI-rollup reading, not a
second copy of it. Merged PRs and commits are fetched here with the same
`_gh` helper (reused, not re-implemented) and the same discipline: any single
`gh` call failing returns an empty list, never raises, so one repo's fetch
problem degrades one section of the page, never the whole thing.

TTL-cached per project, matching merge_queue_build's `_CACHE_TTL` (180s), so a
reload doesn't re-shell three `gh` calls; each call carries the same 20s
timeout (`merge_queue_build._GH_TIMEOUT`). This is a dedicated per-project
page, not Home, so a cold cache still fetches live on first visit — the operator is
visiting specifically to see this project's state.

Full-tier only (server.py's `_full_only_routes` posture) — this shells out to
`gh`, same reasoning as /engineering/merge-queue.
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from html import escape
from urllib.parse import quote

import archives_build
import build
import changelog_build
import chart_house_build
import crew_registry
from control_db import ISSUE_ACTIVE, ISSUE_TERMINAL, ISSUE_TYPE_EPIC
import design_system as ds
import issue_build
import library_client
import merge_queue_build
import orient_time
import paths
import release_notes_build
import waypoints
from logutil import clean_log_field
from models import utcnow
from url_safety import safe_http_url

_CACHE_TTL = merge_queue_build._CACHE_TTL
_MERGED_LIMIT = 8
_COMMIT_LIMIT = 8
# Counts COMMITS, not rows (library_client.list_knowledge_history's own
# docstring) — matches the code-commit section's own _COMMIT_LIMIT in spirit,
# not in row count: a commit touching several documents can render more than
# this many document lines.
_KB_HISTORY_LIMIT = 8
# The bead link 404s outside `team` audience (reference_doc_build.py's own
# gate), so this is the audience the bead lookup entitles against — named
# here rather than passed as a literal so the one place this module decides
# who it serves is greppable, matching home_build._AUDIENCE/build._AUDIENCE.
_AUDIENCE = "team"
# The statuses a bead may render from, ASSERTED here rather than inherited from
# whatever `GET /reference/documents` happens to serve by default. The old SQL
# said `AND status IN ('current', 'draft')` outright; the Library applies the
# same two today, but nothing in its client's contract promises it — so a
# widened server default would put a retired or superseded bead into
# `min(docs, key=id)` below, where a lower id silently outranks the current
# one. No error, no failing test, a wrong link on the page (reviewer, PR #263).
# A caller that needs the other answer states it too: `ops_reports_build` wants
# every status, deliberately, and cannot get it from this route at all.
_BEAD_STATUSES = ("current", "draft")

def known_projects() -> frozenset[str]:
    """The known project/repo names — build.py's own area lists, which
    test_build_product_groups.py already pins as mutually exclusive and
    exhaustive of what the KB indexes. A name outside this union 404s.

    Resolved fresh on every call, not cached at import: `build.COMPANY_PROJECTS`,
    `PRODUCT_ORDER` and `STANDALONE_PRODUCTS` are registry-derived (CQ-171) and
    read live on every access, so caching their union here once would freeze
    it at whatever the registry held at this module's own import time —
    collection time in a test run, not render time."""
    return (frozenset(build.COMPANY_PROJECTS) | frozenset(build.PRODUCT_ORDER)
            | frozenset(build.ENGINEERING_PROJECTS) | frozenset(build.STANDALONE_PRODUCTS))

# module-level TTL cache: {name: (fetched_at, data)}
_cache: dict[str, tuple[float, dict]] = {}


# --- fetch (live from the GitHub engine) --------------------------------------


def _repo_exists(name: str) -> bool:
    """Whether `<owner>/<name>` (`crew_registry.repo_github_owner(name)`) is
    an actual GitHub repo.

    Reviewer finding on #184: seven of `known_projects()`'s nineteen entries
    (company, commercial, deck, platform, engineering-practice, keel, and a
    product several sibling repos build together with no unit of its own)
    are area/product labels from `build.py`'s grouping lists, not GitHub
    repos — `known_projects()` gates "don't shell out for a name nobody
    declared", not "this name has a repo". Without this check those names
    rendered a page whose three sections were all empty and whose "View on
    GitHub" link 404s, indistinguishable from a real repo with no recent
    activity. `_page` uses this to render one honest note instead."""
    return isinstance(
        merge_queue_build._gh(["api", f"repos/{crew_registry.repo_github_owner(name)}/{name}"]), dict)


def _open_prs(name: str) -> list[dict]:
    """Open PRs for one repo, classified exactly as the merge queue classifies
    them — reused rather than re-derived, so the two pages never disagree on
    whether a PR reads cleared/blocked/draft/in-cycle."""
    return [merge_queue_build._classify(pr) for pr in merge_queue_build._open_prs_for_repo(name)]


_MERGED_FIELDS = "number,title,url,mergedAt,author"


def _merged_prs(name: str, limit: int = _MERGED_LIMIT) -> list[dict]:
    data = merge_queue_build._gh(["pr", "list", "--repo", f"{crew_registry.repo_github_owner(name)}/{name}",
                                   "--state", "merged",
                                   "--limit", str(limit), "--json", _MERGED_FIELDS])
    if not isinstance(data, list):
        return []
    rows = [{
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "url": pr.get("url") or "",
        "merged_at": pr.get("mergedAt") or "",
        "author": (pr.get("author") or {}).get("login") or "unknown",
    } for pr in data]
    # `gh pr list --state merged` is not documented to guarantee recency order.
    rows.sort(key=lambda r: r["merged_at"], reverse=True)
    return rows


def _recent_commits(name: str, limit: int = _COMMIT_LIMIT) -> list[dict]:
    data = merge_queue_build._gh(
        ["api", f"repos/{crew_registry.repo_github_owner(name)}/{name}/commits?per_page={limit}"])
    if not isinstance(data, list):
        return []
    out = []
    for c in data:
        commit = c.get("commit") or {}
        author = commit.get("author") or {}
        out.append({
            "sha": (c.get("sha") or "")[:7],
            "message": (commit.get("message") or "").split("\n", 1)[0],
            "author": author.get("name") or "",
            "date": author.get("date") or "",
            "url": c.get("html_url") or "",
        })
    return out


def _bead_doc(name: str) -> dict | None:
    """The project's bead doc (one per project, ADR-0024), linked to its own
    `/reference/doc/{id}` page — the epic asks for it linked, not embedded.
    Before issue_01M17HYDBQ4GPWHPFS56M6QQPH this pointed at the convivy-open
    Typora bridge instead, which never checked audience because opening a
    file locally has no such gate; `/reference/doc/{id}` does (it 404s
    outside `team` audience, reference_doc_build.py), so this withholds the
    link rather than risk a 404 behind it.

    Read through `library_client.list_reference_documents`, not a direct
    index read (issue_01M1MWSCCJP7THRV8YG1KDZ2YE) — no per-doc body fetch is
    needed, since the bead block only ever shows an id/title/link.

    **Raises on a Library failure rather than returning `None` for it.**
    The three-way collapse this used to make — DB unreachable, no bead for
    the project, bead not `team`-audience — conflated a real outage with an
    ordinary empty state. The caller (`_fetch`) catches the exception and
    renders that distinctly from "no bead on file", the same way
    `home_build._recent_docs` (PR #261) does for a request path.

    Still collapses the other two, deliberately: `None` means either no
    current/draft bead exists for `name`, or one does but isn't
    `team`-audience. `list_reference_documents`'s own `_entitled` gate
    (isinstance-guarded against the non-list-audience substring bypass,
    PR #205 review) makes that call; telling a reader a bead exists but
    isn't theirs would leak the document's existence, the same reasoning
    `library_client.get_reference_document`'s docstring gives for the
    identical collapse."""
    docs = [d for d in library_client.list_reference_documents(audience=_AUDIENCE)
            if d.project == name and d.kind == "bead" and d.status in _BEAD_STATUSES]
    if not docs:
        return None
    doc = min(docs, key=lambda d: d.id)
    return {"id": doc.id, "title": doc.title or doc.id,
            "url": f"/reference/doc/{quote(doc.id, safe='/')}"}


def _kb_entitled_ids() -> set[str]:
    """Document ids `_AUDIENCE` may open at `/reference/doc/<id>`, matching
    that route's own gate (reference_doc_build.py's `detail_html`) exactly —
    `library_client.get_reference_document` applies only the audience check,
    no status filter of its own, so this reads `status="all"` rather than the
    `list_reference_documents` default of current+draft. Passing the default
    would drop an archived or deprecated id the route still serves, and a row
    on this page would then render unlinked text for a document a reader
    could in fact open.

    Used to filter the knowledge-changes section (CQ·165,
    issue_01M57NS8PJRTWJ9H6ZHHYB3BFN security review, comment 6253822132):
    `_kb_doc_line` used to render a title and link for every row the history
    route returned, with no audience check at all — a non-`team` document's
    title rode along on a page gated only on tier."""
    return {d.id for d in library_client.list_reference_documents(audience=_AUDIENCE, status="all")}


def _knowledge_history(name: str) -> list[library_client.KnowledgeHistoryRow]:
    """This project's recent knowledge-document commits (CQ·165,
    issue_01M57NS8PJRTWJ9H6ZHHYB3BFN), read through `library_client.
    list_knowledge_history` — never a direct index read, same discipline
    `_bead_doc` follows for the bead — with any row whose `doc_id` is not
    entitled to `_AUDIENCE` (`_kb_entitled_ids`) dropped before it ever
    reaches the render layer. A row with a null `doc_id` (no index entry for
    the changed path) is kept as it is: it already renders from `source_ref`
    and `status` alone, with no title or link to leak.

    `detail_html` has already confirmed the viewer's scope reaches `name`
    before `_fetch` (and so this) ever runs — `list_knowledge_history`'s own
    docstring is explicit that it does not re-check this, because Orient's
    Library token is unbound and the route itself applies no viewer-scope
    filter (library#296). The audience filter here is a separate boundary
    from that scope check: a project in scope can still hold a document
    outside `team` audience.

    **Raises on a Library failure rather than returning `[]` for it**, same
    reasoning `_bead_doc` gives: an outage — history or entitlement read,
    either one — must render as "unavailable", never as "no knowledge
    history" or an unfiltered list. `_fetch` is what catches this and turns
    it into a distinctly rendered state."""
    rows = library_client.list_knowledge_history(name, limit=_KB_HISTORY_LIMIT)
    entitled = _kb_entitled_ids()
    return [r for r in rows if r.doc_id is None or r.doc_id in entitled]


def _fetch(name: str, force: bool = False) -> dict:
    now = time.time()
    cached = _cache.get(name)
    if not force and cached is not None and (now - cached[0]) < _CACHE_TTL:
        return cached[1]
    repo_exists = _repo_exists(name)
    # `_bead_doc` raises on a Library failure (issue_01M1MWSCCJP7THRV8YG1KDZ2YE)
    # rather than returning None for it, so that state can render distinctly
    # from "no bead on file" — caught here rather than left to 500 the page,
    # the same shape home_build._full_body/_contractor_body use around
    # `_recent_docs` (PR #261).
    try:
        bead = _bead_doc(name)
        bead_error = None
    except Exception as exc:
        bead = None
        bead_error = f"The Library could not be reached ({type(exc).__name__}). Try again shortly."
    try:
        kb_history = _knowledge_history(name)
        kb_history_error = None
    except Exception as exc:
        kb_history = []
        kb_history_error = f"The Library could not be reached ({type(exc).__name__}). Try again shortly."
    data = {
        "repo_exists": repo_exists,
        # No GitHub repo means no PRs/commits to fetch — three more `gh` calls
        # that would only ever come back empty, on a page that already knows
        # why.
        "open_prs": _open_prs(name) if repo_exists else [],
        "merged_prs": _merged_prs(name) if repo_exists else [],
        "commits": _recent_commits(name) if repo_exists else [],
        "bead": bead,
        "bead_error": bead_error,
        "kb_history": kb_history,
        "kb_history_error": kb_history_error,
    }
    _cache[name] = (now, data)
    return data


# --- render ---------------------------------------------------------------


def _course_progress_for(name: str) -> waypoints.CourseProgress | None:
    """This project's live Course, matched by the Course's own `product` key
    — not always the same string as `name` (this page's GitHub repo name).
    `chart_house_build._repo_aliases` already documents the split ("bosun"
    dispatches against the agent-platform repo; another product spans two
    repos) for its own PR-ribbon lookup; reused here in reverse rather than
    re-derived, so this page and Chart House can never disagree about which
    Course belongs to which repo."""
    for row in waypoints.course_progress():
        if name in chart_house_build._repo_names_for(row.product):
            return row
    return None


def _log_swallowed(what: str, ex: BaseException) -> None:
    """Print a swallowed exception to stderr, never raising doing it — the
    same discipline `chart_house_build._log_swallowed` uses for its own
    per-course degrade. Reused here rather than re-imported so this module's
    render path stays free of a chart_house_build import at module top."""
    try:
        print(clean_log_field(f"project_build: {what} — {type(ex).__name__}: {ex}"),
              file=sys.stderr)
    except Exception:
        pass


def _course_for(name: str) -> library_client.CourseRecord | None:
    """This project's live Course detail, with each entry's state and
    maturity resolved, or None when the project has no live Course.

    Reuses `chart_house_build._repo_names_for` to match the page's GitHub
    repo name against the Course's own `product` key (the two vocabularies
    that module's `_repo_aliases` already documents as not always equal),
    the same match `_course_progress_for` makes one layer up for the lede's
    position clause.

    Raises on a `list_courses` failure, like every Library read in this
    codebase; `_course_section` renders that distinctly from "no Course."
    A single course's `get_course` detail call degrades to its
    `list_courses` record (entries with no state/maturity) rather than
    taking the section down, the same discipline
    `chart_house_build._course_rows` applies."""
    for course in library_client.list_courses(status="live"):
        if name in chart_house_build._repo_names_for(course.product):
            try:
                return library_client.get_course(course.id) or course
            except Exception as ex:
                _log_swallowed(f"course {course.id} detail unavailable", ex)
                return course
    return None


# Maturity label for a Course step: the same state-then-maturity priority
# `chart_house_build._step_class` and `issue_build._course_entry_row` apply.
# Terminal state wins regardless of maturity (a resolved epic is done, even
# if its maturity field was never cleared).
def _step_label(entry: library_client.CourseEntry) -> str:
    if entry.state in ISSUE_TERMINAL:
        return "done"
    return entry.maturity or "unspecified"


def _course_step_row(entry: library_client.CourseEntry) -> str:
    """One step in the Course's sequence, linked to its issue page — the
    same shape `issue_build._course_entry_row` renders on /engineering/course,
    reused here so the two pages never disagree on what a step looks like."""
    cls = _step_label(entry)
    title = escape(entry.title or entry.issue_id)
    href = escape(f"/engineering/issues/{quote(entry.issue_id, safe='')}", quote=True)
    return (f'<a class="course-step step-{escape(cls, quote=True)}" href="{href}">{title}</a>')


def _course_section(name: str) -> str:
    """The project's live Course, each entry in plan order with its state
    marked. Renders "unavailable" on a Library failure (never a false empty
    list), "no Course" when the project has none, and the track otherwise —
    the three states `_course_progress_html` already distinguishes for the
    lede's one-line summary, now drawn in full below it.

    The section links out to `/engineering/course#course-<product>` for the
    full Course page (the anchor `issue_build.course_anchor_href` builds),
    rather than duplicating that page's per-project section here."""
    try:
        course = _course_for(name)
    except Exception:
        return ds.error_state("Course unavailable — the Library could not be reached.")
    if course is None:
        return '<p class="proj-note">No live Course for this project yet.</p>'
    steps = "".join(_course_step_row(e) for e in course.entries) or (
        '<p class="proj-note">This Course has no entries yet.</p>')
    href = escape(issue_build.course_anchor_href(course.product), quote=True)
    return (f'<div class="proj-course">'
            f'<div class="course-track">{steps}</div>'
            f'<p class="course-link"><a href="{href}">View on the Course page &rarr;</a></p>'
            f'</div>')


# How many epics to render per state before the page truncates — matches
# `_RESOLVED_LIMIT` in issue_build, bounded so a project with dozens of
# epics does not make this section dwarf the rest of the page.
_EPIC_LIMIT = 12


def _project_epics(name: str, scope: frozenset[str]) -> tuple[list, list, dict[str, int]]:
    """The project's open and recently-resolved epics, filtered to the
    viewer's scope before any grouping. Reuses `library_client.
    list_issue_items` (the same read `issue_build.live_html` makes) rather
    than a parallel fetch, then filters by `type == "epic"` and product,
    the way `issue_build._epic_and_correction_groups` would for the Epics
    page.

    An epic's `product` is a project slug, not always the same string as
    `name` (this page's GitHub repo) — "bosun" is the Bosun's product key
    but the page is `/projects/agent-platform`. The match goes through
    `chart_house_build._repo_names_for`, the same project-to-repo mapping
    `_course_for` uses one section up, so the two never disagree about
    which epics belong to which repo. `issue_build._in_scope` stays in
    front of it, filtering by the same scope (project-slug space) every
    other page filters by.

    Returns `(open, resolved, child_counts)`: open epics (active states)
    oldest-first, recently-resolved epics (terminal states) newest-first,
    and a map of epic id to the number of active work items under it
    (counted from the same items list, the way
    `issue_build._epic_and_correction_groups` groups by `parent_id`).
    The lists are UNCAPPED — `_epics_section` applies `_EPIC_LIMIT` and
    renders the "N more not rendered" note, the same place issue_build's
    `live_html` applies its own cap. A resolved epic past the cap is still
    reachable at its own issue page, the same way a capped queue card is."""
    all_items = [it for it in library_client.list_issue_items()
                 if issue_build._in_scope(it.product, scope)
                 and name in chart_house_build._repo_names_for(it.product)]
    epics = [it for it in all_items if it.type == ISSUE_TYPE_EPIC]
    child_counts: dict[str, int] = {}
    for it in all_items:
        if it.parent_id and it.state in ISSUE_ACTIVE and it.type != ISSUE_TYPE_EPIC:
            child_counts[it.parent_id] = child_counts.get(it.parent_id, 0) + 1
    open_epics = sorted((it for it in epics if it.state in ISSUE_ACTIVE),
                        key=lambda it: it.created_at)
    resolved = sorted((it for it in epics if it.state in ISSUE_TERMINAL),
                      key=lambda it: (it.resolved_at or dt.datetime.min, it.created_at),
                      reverse=True)
    return open_epics, resolved, child_counts


def _epic_row(epic: library_client.IssueRecord, child_counts: dict[str, int], now) -> str:
    """One epic in the epics section: title linked to its issue page, with
    its maturity badge and a count of active work items under it."""
    maturity = (epic.maturity or "").strip()
    badge = (f'<span class="badge maturity maturity-{escape(maturity, quote=True)}">'
             f'{escape(maturity)}</span>' if maturity
             else '<span class="badge maturity maturity-unset">maturity not set</span>')
    href = escape(f"/engineering/issues/{quote(epic.id, safe='')}", quote=True)
    children = child_counts.get(epic.id, 0)
    progress_str = (f'<span class="epic-progress">{children} '
                    f'work item{"s" if children != 1 else ""}</span>'
                    if children else "")
    age = issue_build._age(epic.created_at, now)
    return (f'<div class="proj-row epic-row">'
            f'<a class="epic-title" href="{href}">{escape(epic.title)}</a>'
            f'{badge}{progress_str}'
            f'<span class="proj-row-meta">{escape(age)} ago</span></div>')


def _epics_section(name: str, scope: frozenset[str], now) -> str:
    """The project's open and recently-resolved epics with maturity and
    progress. Renders "unavailable" on a Library failure (never a false
    empty list) and an empty state when the project has no epics at all.

    Each state's list is capped at `_EPIC_LIMIT`; a truncated list carries
    an "N more not rendered" note, the same shape issue_build's Epics page
    renders when its cap drops entries, so a reader sees the true count
    rather than the rendered one."""
    try:
        open_epics, resolved, child_counts = _project_epics(name, scope)
    except Exception:
        return ds.error_state("Epics unavailable — the Library could not be reached.")
    if not open_epics and not resolved:
        return ds.empty_state("No epics for this project.")
    open_rendered = open_epics[:_EPIC_LIMIT]
    resolved_rendered = resolved[:_EPIC_LIMIT]
    open_hidden = len(open_epics) - len(open_rendered)
    resolved_hidden = len(resolved) - len(resolved_rendered)
    open_html = ("".join(_epic_row(e, child_counts, now) for e in open_rendered)
                 or '<p class="proj-note">No open epics.</p>')
    open_note = (f'<p class="proj-note">{open_hidden} more not rendered.</p>'
                 if open_hidden else "")
    resolved_html = ("".join(_epic_row(e, child_counts, now) for e in resolved_rendered)
                    or '<p class="proj-note">No recently resolved epics.</p>')
    resolved_note = (f'<p class="proj-note">{resolved_hidden} more not rendered.</p>'
                     if resolved_hidden else "")
    return (f'<div class="proj-list">{open_html}</div>{open_note}'
            f'<h3 class="epic-subhead">Recently resolved</h3>'
            f'<div class="proj-list">{resolved_html}</div>{resolved_note}')


# How many changelog/release-note entries to show per project — the same cap
# `_MERGED_LIMIT`/`_COMMIT_LIMIT`/`_KB_HISTORY_LIMIT` already use for every
# other "recent activity" section on this page.
_CHANGELOG_LIMIT = 8
_RELEASE_NOTES_LIMIT = 8


def _products_for_repo(name: str) -> list[str]:
    """Project slug(s) for this GitHub repo — the reverse of
    `chart_house_build._repo_names_for`. Release notes are filed by hand
    under whatever product the author types, and the fleet-wide
    `/products/release-notes` fetches a scoped viewer's notes by project
    slug, so this section keys by slug too (`bosun` for the repo
    `agent-platform`). The changelog is keyed by repo name instead; see
    `_changelog_entries`."""
    aliased = [product for product in chart_house_build._repo_aliases()
               if name in chart_house_build._repo_names_for(product)]
    return aliased or [name]


def _changelog_entries(name: str) -> list[library_client.SignalRecord]:
    """This project's most recent changelog entries, through the same
    fan-out and author-source filter `/engineering/changelog` uses
    (`changelog_build.changelog_live_html`), restricted to `name` so no
    other project's entries are ever fetched, let alone rendered.

    Changelog signals carry the repo name as their `product`, so the page's
    own name is the key. The Bosun's entries are filed under
    `agent-platform`, not under `bosun`, its key for work items; keying this
    section through `_products_for_repo` showed an empty changelog on
    `/projects/agent-platform` over 163 live entries."""
    rows = archives_build.fan_out(
        "changelog", cap_per_product=_CHANGELOG_LIMIT,
        source=sorted(changelog_build.CHANGELOG_AUTHORED_SOURCES),
        products=[name])
    rows.sort(key=lambda s: s.produced_at or s.ingested_at, reverse=True)
    return rows[:_CHANGELOG_LIMIT]


def _release_note_entries(name: str) -> list[library_client.SignalRecord]:
    """This project's most recent release notes, through the same fan-out
    `/products/release-notes` uses (`release_notes_build.live_html`),
    restricted to `name`'s own project slug(s)."""
    rows = archives_build.fan_out(
        release_notes_build._KIND, cap_per_product=_RELEASE_NOTES_LIMIT,
        products=_products_for_repo(name))
    rows.sort(key=lambda s: s.produced_at or s.ingested_at, reverse=True)
    return rows[:_RELEASE_NOTES_LIMIT]


def _changelog_section(name: str) -> str:
    """The project's recent changelog entries, rendered with
    `changelog_build._card` — the same card the fleet-wide page renders, so
    the two never disagree on what a changelog entry looks like. Renders
    "unavailable" on a Library failure, never a false empty list, and links
    to the fleet-wide page filtered to this product."""
    try:
        entries = _changelog_entries(name)
    except Exception:
        return ds.error_state("Changelog unavailable — the Library could not be reached.")
    href = escape(f"/engineering/changelog?product={quote(name, safe='')}", quote=True)
    link = f'<p class="section-link"><a href="{href}">View the full changelog &rarr;</a></p>'
    if not entries:
        return ds.empty_state("No changelog entries for this project yet.") + link
    cards = "".join(changelog_build._card(sig) for sig in entries)
    return f'<div class="chglog"><div class="feed">{cards}</div></div>{link}'


def _release_notes_section(name: str) -> str:
    """The project's recent release notes, rendered with
    `release_notes_build._card` — the same card the fleet-wide page renders.
    Renders "unavailable" on a Library failure, never a false empty list,
    and links to the fleet-wide page filtered to this product."""
    try:
        entries = _release_note_entries(name)
    except Exception:
        return ds.error_state("Release notes unavailable — the Library could not be reached.")
    product = _products_for_repo(name)[0]
    href = escape(f"/products/release-notes?product={quote(product, safe='')}", quote=True)
    link = f'<p class="section-link"><a href="{href}">View all release notes &rarr;</a></p>'
    if not entries:
        return ds.empty_state("No release notes for this project yet.") + link
    cards = "".join(release_notes_build._card(sig) for sig in entries)
    return f'<div class="rn"><div class="feed">{cards}</div></div>{link}'


def _course_progress_html(name: str) -> str:
    """The lede's Course-position clause. Three states: a live Course's
    position (waypoints passed of total, plus the trailing-7-day pace), no
    live Course, or "unavailable" on a Library failure —
    `waypoints.course_progress()` raises on that rather than returning `[]`,
    precisely so a caller can tell a Course-less project from one it cannot
    ask about right now (that module's own docstring); a false "0 of 0
    passed" would erase the distinction this function exists to keep. Every
    value interpolated here is an int, never a Library-sourced string, so
    nothing needs escaping."""
    try:
        progress = _course_progress_for(name)
    except Exception:
        return "Course progress unavailable."
    if progress is None:
        return 'No live Course yet — see the <a href="/chart-house">Chart House</a>.'
    window = progress.passed_within.get(waypoints.WINDOWS[0], 0)
    return (f'{progress.passed} of {progress.total} waypoints passed &middot; '
            f'{window} in the last {waypoints.WINDOWS[0]} days on its '
            f'<a href="/chart-house">Course</a>.')


def _merged_row(r: dict) -> str:
    safe_url = safe_http_url(r["url"])
    label = escape(f"#{r['number']}")
    ref = (f'<a href="{escape(safe_url, quote=True)}" target="_blank" rel="noopener">{label}</a>'
           if safe_url is not None else label)
    when = merge_queue_build._short_date(r["merged_at"])
    return (f'<div class="proj-row"><span class="proj-row-ref">{ref}</span>'
            f'<span class="proj-row-title">{escape(r["title"])}</span>'
            f'<span class="proj-row-meta">{escape(r["author"])} · merged {escape(when)}</span></div>')


def _commit_row(c: dict) -> str:
    safe_url = safe_http_url(c["url"])
    label = escape(c["sha"] or "———")
    ref = (f'<a href="{escape(safe_url, quote=True)}" target="_blank" rel="noopener">{label}</a>'
           if safe_url is not None else label)
    when = merge_queue_build._short_date(c["date"])
    return (f'<div class="proj-row"><span class="proj-row-ref mono">{ref}</span>'
            f'<span class="proj-row-title">{escape(c["message"])}</span>'
            f'<span class="proj-row-meta">{escape(c["author"])} · {escape(when)}</span></div>')


def _bead_block(bead: dict | None, bead_error: str | None = None) -> str:
    # bead["url"] is a same-site path this module built itself
    # (`/reference/doc/{id}`), not an external URL read off GitHub — the
    # `safe_http_url` scheme+netloc check the PR/commit rows below need
    # doesn't apply, and would reject a path with no scheme.
    if bead is not None:
        return (f'<a class="proj-bead" href="{escape(bead["url"], quote=True)}" '
                f'target="_blank" rel="noopener">{escape(bead["title"])} — open ↗</a>')
    # A Library failure renders distinctly from "no bead on file"
    # (issue_01M1MWSCCJP7THRV8YG1KDZ2YE) — the two used to collapse into the
    # same None.
    if bead_error is not None:
        return ds.error_state(bead_error)
    return '<p class="proj-note">No bead on file for this project yet.</p>'


def _kb_short_date(when: object) -> str:
    """`committed_at` (a naive-UTC `datetime`, `library_client._iso`'s own
    convention) to a short 'Jul 18' date in MST — `merge_queue_build.
    _short_date`'s rendering, reused in substance, built from a `datetime`
    rather than a raw ISO string since that is the shape `KnowledgeHistoryRow`
    already carries."""
    if when is None:
        return "—"
    mst = orient_time.to_mst(when, assume_utc=True)
    return mst.strftime("%b %-d") if mst else "—"


def _group_by_commit(rows: list) -> list[tuple[str, list]]:
    """`rows` (newest-first, one row per document changed in one commit) to
    `(commit, [row, ...])` pairs, preserving order. Assumes every commit's
    rows arrive contiguously, which is how the route serves them — a commit
    is the route's own paging unit (`library_client.list_knowledge_history`'s
    docstring), so its rows are never interleaved with another commit's."""
    groups: list[tuple[str, list]] = []
    for row in rows:
        if groups and groups[-1][0] == row.commit:
            groups[-1][1].append(row)
        else:
            groups.append((row.commit, [row]))
    return groups


def _kb_doc_line(row) -> str:
    status = escape(row.status)
    if row.doc_id is not None:
        href = f"/reference/doc/{quote(row.doc_id, safe='/')}"
        label = escape(row.title or row.doc_id)
        doc = f'<a href="{escape(href, quote=True)}">{label}</a>'
    else:
        # No index row for this path (a deleted document, the old half of a
        # rename, or a kind the index never holds) — shown as plain text from
        # `source_ref`, never linked, and never called a deletion unless
        # `status` itself says so (library_client.KnowledgeHistoryRow's own
        # docstring).
        doc = escape(row.source_ref or "(unknown path)")
    return f'<div class="kb-doc"><span class="kb-doc-status">{status}</span> {doc}</div>'


def _kb_commit_block(commit: str, rows: list) -> str:
    when = _kb_short_date(rows[0].committed_at)
    subject = escape(rows[0].subject)
    docs_html = "".join(_kb_doc_line(r) for r in rows)
    return (f'<div class="proj-row kb-commit">'
            f'<div class="kb-commit-head"><span class="proj-row-ref mono">{escape(commit[:7])}</span>'
            f'<span class="proj-row-title">{subject}</span>'
            f'<span class="proj-row-meta">{when}</span></div>'
            f'<div class="kb-docs">{docs_html}</div></div>')


def _kb_history_block(rows: list, error: str | None) -> str:
    # A Library failure (503, or any other transport problem) renders
    # distinctly from "no knowledge history" — the same collapse `_bead_block`
    # avoids for the bead section, and for the same reason: an outage must not
    # read as an empty queue.
    if error is not None:
        return ds.error_state(error)
    groups = _group_by_commit(rows)
    if not groups:
        # Also what a 400 (unknown project, or one out of this credential's
        # view) renders as — `library_client.list_knowledge_history` collapses
        # that into an empty list, so this is the one message both cases need.
        return ds.empty_state("No knowledge history for this project.")
    return '<div class="proj-list">' + "".join(
        _kb_commit_block(c, rs) for c, rs in groups) + '</div>'


def _page(name: str, data: dict, scope: frozenset[str]) -> str:
    label = build.PROJECT_LABEL.get(name, name)
    org = crew_registry.repo_github_owner(name)
    repo_url = f"https://github.com/{org}/{name}"
    repo_exists = data["repo_exists"]
    open_prs, merged_prs, commits = data["open_prs"], data["merged_prs"], data["commits"]

    about = (f'<a href="{escape(repo_url, quote=True)}" target="_blank" rel="noopener">'
             f'{escape(name)}</a>' if repo_exists else f'<strong>{escape(name)}</strong>')
    course_html = _course_progress_html(name)
    now = utcnow()
    course_section_html = _course_section(name)
    epics_section_html = _epics_section(name, scope, now)
    changelog_section_html = _changelog_section(name)
    release_notes_section_html = _release_notes_section(name)

    if repo_exists:
        activity_html = f'''<section class="proj-section">
      <h2>Open pull requests <span class="note">· {len(open_prs)}</span></h2>
      <div class="feed">{"".join(merge_queue_build._row(r) for r in open_prs)
                          or ds.empty_state("No open pull requests.")}</div>
    </section>
    <section class="proj-section">
      <h2>Recently merged <span class="note">· {len(merged_prs)}</span></h2>
      <div class="proj-list">{"".join(_merged_row(r) for r in merged_prs)
                               or ds.empty_state("No recently merged pull requests.")}</div>
    </section>
    <section class="proj-section">
      <h2>Recent commits <span class="note">· {len(commits)}</span></h2>
      <div class="proj-list">{"".join(_commit_row(c) for c in commits)
                               or ds.empty_state("No recent commits.")}</div>
    </section>
    <div class="footnote">Open PRs, merged PRs and commits read live from the GitHub engine
    (20s timeout per call, cached {int(_CACHE_TTL)}s). Signed in as <strong>__WHOAMI__</strong>.</div>'''
    else:
        no_repo_note = ds.empty_state(
            f"No GitHub repo named {org}/{name} — this project has no pull requests, "
            "merges or commits to read.")
        activity_html = (
            f'<section class="proj-section">{no_repo_note}</section>'
            '<div class="footnote">Signed in as <strong>__WHOAMI__</strong>.</div>'
        )

    body = f'''<div class="mq proj">
  <header class="mq-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / projects / {escape(label)}</div>
    <h1>{escape(label)}</h1>
    <p class="lede">What's readable today about {about}:
      open work, what shipped recently, and its bead. {course_html}</p>
  </header>
  <div class="wrap">
    <section class="proj-section">
      <h2>Bead</h2>
      {_bead_block(data["bead"], data.get("bead_error"))}
    </section>
    <section class="proj-section">
      <h2>Course</h2>
      {course_section_html}
    </section>
    {activity_html}
    <section class="proj-section">
      <h2>Epics</h2>
      {epics_section_html}
    </section>
    <section class="proj-section">
      <h2>Changelog</h2>
      {changelog_section_html}
    </section>
    <section class="proj-section">
      <h2>Release notes</h2>
      {release_notes_section_html}
    </section>
    <section class="proj-section">
      <h2>Knowledge changes</h2>
      {_kb_history_block(data["kb_history"], data.get("kb_history_error"))}
    </section>
  </div>
</div>'''
    return ds.page_shell(title=label,
                         nav_html=ds.nav_component(f"/projects/{name}", "full"),
                         body=body, extra_css=(merge_queue_build._CSS + changelog_build._CSS
                                                + release_notes_build._CSS + _CSS))


def detail_html(name: str, scope: frozenset[str], whole_fleet: bool) -> str | None:
    """One project's page, or None when `name` is not a known project, or
    (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) is not a repo the viewer's
    scope reaches — the 404 the caller (server.py's `/projects/<name>`
    route) must serve rather than shelling out to `gh` for a repo nobody
    declared a project, or one the viewer is not entitled to.

    `name` is a GitHub repo, the same identity space `crew_registry.
    repos_in_scope` resolves a viewer's project scope into (see that
    function's own docstring on why a project slug is not always a repo
    name) — reused here rather than a second alias table, the same choice
    `crews_build._visible_repos` makes. A project name can be guessed (it is
    a short, literal repo name), so this check is this page's own rather
    than left to the caller.

    The lede links to `/chart-house`, Orient's own web surface for a Course
    (added by the same dashboards epic this page's Course-position line
    comes from) — `_course_progress_for` matches this page's `name` (a
    GitHub repo) against the Course's own `product` key, the two vocabularies
    `chart_house_build._repo_aliases` already documents as not always equal.
    """
    if name not in known_projects():
        return None
    if not whole_fleet and name not in crew_registry.repos_in_scope(scope):
        return None
    return _page(name, _fetch(name), scope)


# --- project index (/projects) ---------------------------------------------


def _project_order() -> list[str]:
    """The display order for the index, matching build.py's own grouping:
    products, then standalones, then company, then engineering. Reused
    rather than re-derived so the index and the KB browse agree on what
    leads. A function, not a module constant, for the reason `known_projects()`
    above is: three of these four lists are registry-derived (CQ-171)."""
    return build.PRODUCT_ORDER + build.STANDALONE_PRODUCTS + build.COMPANY_PROJECTS + build.ENGINEERING_PROJECTS


def _visible_projects(scope: frozenset[str], whole_fleet: bool) -> list[str]:
    """The projects the viewer can open a project page for, in display order.

    A whole-fleet viewer sees every known project; a scoped viewer sees only
    the repos their scope reaches (`crew_registry.repos_in_scope`), the same
    access check `detail_html` applies per project. Intersected with
    `known_projects()` so an index entry always resolves to a real project
    page rather than a name the registry lists but build.py does not.

    Projects not in `_project_order()` (a new repo added since the last
    deploy, or a test fixture's synthetic project) sort after the known set,
    the same fallback `build.render` applies for the KB browse."""
    known = known_projects()
    if whole_fleet:
        accessible = known
    else:
        accessible = crew_registry.repos_in_scope(scope) & known
    order = _project_order()
    ordered = [p for p in order if p in accessible]
    ordered += sorted(accessible - set(ordered))
    return ordered


def _index_row(name: str) -> str:
    label = escape(build.PROJECT_LABEL.get(name, name))
    href = escape(f"/projects/{name}", quote=True)
    return f'<a class="proj-index-row" href="{href}"><span class="proj-index-name">{label}</span>' \
           f'<span class="proj-index-slug">{escape(name)}</span></a>'


def live_html(scope: frozenset[str], whole_fleet: bool) -> str:
    """The project index at `/projects` (CQ·160, issue_01M57NRMZSZW5B5VNWCM4HVZPV):
    every project the viewer can open, each linking to its own
    `/projects/<name>` page. Applies the viewer's project scope the same way
    `detail_html` does — a project outside the viewer's scope never appears on
    the list. Full-tier only, live per request like every other builder."""
    projects = _visible_projects(scope, whole_fleet)
    if projects:
        rows = "".join(_index_row(p) for p in projects)
        body_list = f'<div class="proj-index-list">{rows}</div>'
    else:
        body_list = ds.empty_state("No projects in your scope.")
    body = f'''<div class="mq proj proj-index">
  <header class="mq-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / projects</div>
    <h1>Projects</h1>
    <p class="lede">Every project you can see, each with its own page for
    pull requests, recent merges, commits, bead and Course.</p>
  </header>
  <div class="wrap">
    {body_list}
  </div>
</div>'''
    return ds.page_shell(title="Projects",
                         nav_html=ds.nav_component("/projects", "full"),
                         body=body, extra_css=_CSS)


def build_static() -> None:
    """Seed the static fallback for `/projects` (CQ·160): rendered for an
    empty scope so it names no project, the same discipline
    `chart_house_build.build_static` and `issue_build.main` follow — a
    scoped viewer served this fallback during a Library stall must not see
    another project's name."""
    out = paths.STATE_ROOT / "site"
    out.mkdir(parents=True, exist_ok=True)
    fallback = live_html(frozenset(), False).replace(
        "No projects in your scope.",
        "Live view unavailable — reload in a moment.")
    (out / "projects.html").write_text(fallback, encoding="utf-8")
    print("Wrote site/projects.html (static fallback)")


_CSS = r"""
.proj .proj-section{margin:0 0 28px;}
.proj .proj-section h2{font-size:1.05rem;font-weight:640;margin:0 0 10px;letter-spacing:-.01em;}
.proj .proj-section h2 .note{font-weight:500;font-size:.8rem;color:var(--ink-3);opacity:.7;}
.proj .proj-list{display:flex;flex-direction:column;gap:7px;}
.proj .proj-row{display:flex;flex-wrap:wrap;align-items:baseline;gap:9px;background:var(--surface);
border:1px solid var(--line);border-radius:10px;padding:9px 13px;font-size:.86rem;}
.proj .proj-row-ref{font-family:var(--mono);font-size:.76rem;color:var(--brand-ink);flex:0 0 auto;}
.proj .proj-row-ref a{color:inherit;text-decoration:none;}
.proj .proj-row-ref a:hover{text-decoration:underline;}
.proj .proj-row-title{flex:1;min-width:200px;overflow-wrap:anywhere;}
.proj .proj-row-meta{font-family:var(--mono);font-size:.72rem;color:var(--ink-3);white-space:nowrap;}
.proj .proj-bead{display:inline-block;background:var(--surface);border:1px solid var(--line);
border-radius:10px;padding:9px 13px;font-size:.88rem;color:var(--brand-ink);text-decoration:none;}
.proj .proj-bead:hover{text-decoration:underline;}
.proj .proj-note{color:var(--ink-3);font-size:.86rem;}
.proj .kb-commit{flex-direction:column;align-items:stretch;gap:6px;}
.proj .kb-commit-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:9px;}
.proj .kb-docs{display:flex;flex-direction:column;gap:3px;padding-left:15px;}
.proj .kb-doc{font-size:.82rem;color:var(--ink-2);overflow-wrap:anywhere;}
.proj .kb-doc a{color:var(--brand-ink);text-decoration:none;}
.proj .kb-doc a:hover{text-decoration:underline;}
.proj .kb-doc-status{font-family:var(--mono);font-size:.7rem;color:var(--ink-3);
text-transform:uppercase;margin-right:4px;}
.proj .proj-course{display:flex;flex-direction:column;gap:8px;}
.proj .course-track{display:flex;flex-wrap:wrap;gap:6px;}
.proj .course-step{font-family:var(--mono);font-size:.74rem;padding:6px 10px;border-radius:8px;
border:1px solid var(--line);background:var(--surface);color:var(--ink-2);
text-decoration:none;}
.proj .course-step:hover{border-color:var(--brand);color:var(--brand-ink);}
.proj .course-step.step-done{background:var(--ok-wash);color:var(--ok);border-color:transparent;}
.proj .course-step.step-approved{background:var(--brand-wash);color:var(--brand-ink);border-color:transparent;}
.proj .course-step.step-proposed{background:var(--warn-wash);color:var(--warn);border-color:transparent;}
.proj .course-link{margin:4px 0 0;font-size:.78rem;}
.proj .course-link a{color:var(--brand-ink);text-decoration:underline dotted;text-underline-offset:3px;}
.proj .section-link{margin:8px 0 0;font-size:.78rem;}
.proj .section-link a{color:var(--brand-ink);text-decoration:underline dotted;text-underline-offset:3px;}
.proj .epic-subhead{font-size:.82rem;font-weight:600;color:var(--ink-3);margin:14px 0 6px;}
.proj .epic-row{align-items:center;}
.proj .epic-title{flex:1;min-width:200px;font-size:.86rem;color:var(--brand-ink);
text-decoration:none;overflow-wrap:anywhere;}
.proj .epic-title:hover{text-decoration:underline;}
.proj .badge.maturity{font-family:var(--mono);font-size:.68rem;letter-spacing:.05em;
text-transform:uppercase;padding:3px 8px;border-radius:6px;font-weight:600;
background:var(--surface-2);color:var(--ink-3);border:1px solid var(--line);}
.proj .badge.maturity-approved{background:var(--ok-wash);color:var(--ok);border-color:transparent;}
.proj .badge.maturity-proposed{background:var(--brand-wash);color:var(--brand-ink);border-color:transparent;font-weight:600;}
.proj .badge.maturity-unset{background:var(--surface-2);color:var(--ink-3);border-color:transparent;}
.proj .epic-progress{font-family:var(--mono);font-size:.72rem;color:var(--ink-3);}
.proj .proj-index-list{display:flex;flex-direction:column;gap:7px;}
.proj .proj-index-row{display:flex;align-items:baseline;gap:9px;background:var(--surface);
border:1px solid var(--line);border-radius:10px;padding:9px 13px;font-size:.88rem;
color:var(--brand-ink);text-decoration:none;}
.proj .proj-index-row:hover{border-color:var(--brand);text-decoration:none;}
.proj .proj-index-name{flex:1;font-weight:580;}
.proj .proj-index-slug{font-family:var(--mono);font-size:.72rem;color:var(--ink-3);}
"""


if __name__ == "__main__":
    build_static()
