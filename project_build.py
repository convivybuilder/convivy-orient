#!/usr/bin/env python3.11
"""/projects/<name> — what's already readable about one project (epic
issue_01M0AK8CM3ADKDTG8PM6GRCP8H, item 5).

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
STANDALONE_PRODUCTS lists are the known project names (`KNOWN_PROJECTS`
below); a name outside that union 404s rather than shelling out to `gh` for a
repo nobody declared a project.

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

import time
from html import escape
from urllib.parse import quote

import build
import chart_house_build
import crew_registry
import design_system as ds
import library_client
import merge_queue_build
import waypoints
from url_safety import safe_http_url

ORG = "convivy"
_CACHE_TTL = merge_queue_build._CACHE_TTL
_MERGED_LIMIT = 8
_COMMIT_LIMIT = 8
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

# The known project/repo names — build.py's own area lists, which
# test_build_product_groups.py already pins as mutually exclusive and
# exhaustive of what the KB indexes. A name outside this union 404s.
KNOWN_PROJECTS = (set(build.COMPANY_PROJECTS) | set(build.PRODUCT_ORDER)
                  | set(build.ENGINEERING_PROJECTS) | set(build.STANDALONE_PRODUCTS))

# module-level TTL cache: {name: (fetched_at, data)}
_cache: dict[str, tuple[float, dict]] = {}


# --- fetch (live from the GitHub engine) --------------------------------------


def _repo_exists(name: str) -> bool:
    """Whether `convivy/<name>` is an actual GitHub repo.

    Reviewer finding on #184: seven of `KNOWN_PROJECTS`' nineteen entries
    (company, commercial, deck, nyh-studio, platform, engineering-practice,
    keel) are area/product labels from `build.py`'s grouping lists, not GitHub
    repos — `KNOWN_PROJECTS` gates "don't shell out for a name nobody
    declared", not "this name has a repo". Without this check those names
    rendered a page whose three sections were all empty and whose "View on
    GitHub" link 404s, indistinguishable from a real repo with no recent
    activity. `_page` uses this to render one honest note instead."""
    return isinstance(merge_queue_build._gh(["api", f"repos/{ORG}/{name}"]), dict)


def _open_prs(name: str) -> list[dict]:
    """Open PRs for one repo, classified exactly as the merge queue classifies
    them — reused rather than re-derived, so the two pages never disagree on
    whether a PR reads cleared/blocked/draft/in-cycle."""
    return [merge_queue_build._classify(pr) for pr in merge_queue_build._open_prs_for_repo(name)]


_MERGED_FIELDS = "number,title,url,mergedAt,author"


def _merged_prs(name: str, limit: int = _MERGED_LIMIT) -> list[dict]:
    data = merge_queue_build._gh(["pr", "list", "--repo", f"{ORG}/{name}", "--state", "merged",
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
    data = merge_queue_build._gh(["api", f"repos/{ORG}/{name}/commits?per_page={limit}"])
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
    }
    _cache[name] = (now, data)
    return data


# --- render ---------------------------------------------------------------


def _course_progress_for(name: str) -> waypoints.CourseProgress | None:
    """This project's live Course, matched by the Course's own `product` key
    — not always the same string as `name` (this page's GitHub repo name).
    `chart_house_build._REPO_ALIASES` already documents the split ("bosun"
    dispatches against the agent-platform repo; "selector" spans two repos)
    for its own PR-ribbon lookup; reused here in reverse rather than
    re-derived, so this page and Chart House can never disagree about which
    Course belongs to which repo."""
    for row in waypoints.course_progress():
        if name in chart_house_build._repo_names_for(row.product):
            return row
    return None


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


def _page(name: str, data: dict) -> str:
    label = build.PROJECT_LABEL.get(name, name)
    repo_url = f"https://github.com/{ORG}/{name}"
    repo_exists = data["repo_exists"]
    open_prs, merged_prs, commits = data["open_prs"], data["merged_prs"], data["commits"]

    about = (f'<a href="{escape(repo_url, quote=True)}" target="_blank" rel="noopener">'
             f'{escape(name)}</a>' if repo_exists else f'<strong>{escape(name)}</strong>')
    course_html = _course_progress_html(name)

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
            f"No GitHub repo named convivy/{name} — this project has no pull requests, "
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
    {activity_html}
  </div>
</div>'''
    return ds.page_shell(title=label,
                         nav_html=ds.nav_component(f"/projects/{name}", "full"),
                         body=body, extra_css=merge_queue_build._CSS + _CSS)


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
    `chart_house_build._REPO_ALIASES` already documents as not always equal.
    """
    if name not in KNOWN_PROJECTS:
        return None
    if not whole_fleet and name not in crew_registry.repos_in_scope(scope):
        return None
    return _page(name, _fetch(name))


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
"""
