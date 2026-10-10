#!/usr/bin/env python3.11
"""The Chart House base page — /chart-house.

**Redesign (issue_01M15C4G6YJHH73KJCHGK7B0J9, operator-approved mockup):** one row
per product's live Course, drawn at true scale — one track cell per Course
step, coloured by maturity (done / approved-ahead / proposed-awaiting-operator /
not-yet-specified). The operator's own framing: "The Chart House is for understanding
the path of a project and how it is progressing... Crew mechanics — rosters,
models, dispatch counts — do not belong on this page." This replaces the
prior two-section layout (a Course-progress table above a flat table of
every `project_build.KNOWN_PROJECTS` entry with its open-PR count) with the
single unified table the mockup shows; the flat all-projects/open-PR table is
retired (see the PR body for why).

**Round two (same issue, the operator's follow-up ask 2026-08-28):** a PR ribbon under
each row — the open queue by state, then a week of merges — because "the
Chart House should show open PRs... and also recently merged PRs." See
`_ribbon_html`'s docstring for the one rule that matters: a broken or cold PR
feed must render as "unavailable," never as a false zero.

Reads five sources:

- Courses, live from the Library over `library_client` (`list_courses` +
  `get_course` per course) — the plan itself, and per-entry `state` +
  `maturity`, which is what lets a step be classified into the four track
  colours (`_step_class`). **Uncaught on failure**, like every other Library
  read in this codebase: this page's ONLY data section is the Course view
  now, so there is no independent section left for a Library outage to spare
  — server.py's stale-static fallback is the right degrade, not a
  half-rendered page. (The prior version special-cased this section to
  degrade in isolation, because it led a page that had other,
  Library-independent sections below it. Those sections are gone, so the
  isolation is no longer buying anything and would only hide a real outage
  behind an empty-looking page.) A single course's `get_course` detail call
  still degrades to its `list_courses` record on its own failure rather than
  taking every other row down with it — same discipline `project_build.py`
  applies per `gh` call, one call site over — and that one failure is logged
  to stderr rather than raised.
- The project's bead doc id, live from the Library over
  `library_client.list_reference_documents` (`kind == "bead"`), read fresh
  each render rather than hardcoded — a project that gains a bead is picked
  up on the next render with no code change. Uncaught on failure, for the
  same reason the Course read is.
- Cost comes from `dashboard_build.normalize()`, the same per-run fact rows
  Spending/Cost-Performance already read from local `~/Convivy/agent-runs`
  ledgers — summed per product over a trailing 30 days. No network calls;
  local file reads, so this one still can't take the page down.
  Cost attribution is broken upstream (issue_01M15C50ESYBPTDWV5FHZZXVH5):
  most ledger rows currently carry no product, so `_cost_for` returning
  `None` (not `$0.00`) for a project is the expected, common case, and the
  page must never render that `None` as a zero-dollar figure — a real zero
  and "the ledger cannot say" would otherwise look identical.
- The open-PR queue, from `merge_queue_build.cached_open_prs()` — the
  read-only cache accessor, never the fetching `_fetch_open_prs()`, so this
  page never shells out to `gh` on its own account; it rides whatever is
  already warm from a recent visit to /engineering/merge-queue or Home. Cold
  or absent, it returns `None`, which this page renders as "PR feed
  unavailable," not as an empty queue.
- Merge counts over the trailing week, from `changelog`-kind signals with
  `source=github` — one per merged PR (`github_capture.signal_from_merge`),
  read live over `library_client.list_signals`. Uncaught on failure, for the
  same reason the Course read is.
"""
from __future__ import annotations

import datetime as dt
import sys
from html import escape
from urllib.parse import quote

import build
# A terminal epic is not awaiting anything, whatever its `maturity` still
# says — the same state-then-maturity priority `_step_class` applies.
from control_db import ISSUE_TERMINAL
import dashboard_build
import design_system as ds
import issue_build
import library_client
from logutil import clean_log_field
import merge_queue_build
import waypoints
from paths import STATE_ROOT

OUT = STATE_ROOT / "site"

WINDOW_DAYS = 30
MERGE_WINDOW_DAYS = 7
# Rows scanned to find a week's org-wide merges. Live-measured 2026-08-28: 265
# `changelog`-kind, `source=github` signals fell inside a 7-day cutoff, found
# entirely within the newest 2000 (the oldest of those 2000 was already a month
# old) — this cap leaves ample headroom without paging the ~19,700-row table.
_MERGE_SCAN_CAP = 2000


# --- data (Courses live, cost local) ---------------------------------------


def _cost_rollup() -> dict[str, dict]:
    """{product: {"cost_usd": float, "runs": int}} over the trailing WINDOW_DAYS,
    from the same fact rows Spending/Cost-Performance read — local file reads,
    no network."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=WINDOW_DAYS)).date().isoformat()
    out: dict[str, dict] = {}
    for f in dashboard_build.normalize():
        if (f.get("date") or "") < cutoff:
            continue
        product = f.get("product") or "—"
        row = out.setdefault(product, {"cost_usd": 0.0, "runs": 0})
        cost = f.get("cost_usd")
        if isinstance(cost, (int, float)):
            row["cost_usd"] += cost
        row["runs"] += 1
    return out


# Known cases where a Course's `product` key doesn't match the cost ledger's
# canonicalized product name 1:1 (review finding, PR #190). Two distinct
# causes, both hand-maintained here the same way `dashboard_build._canon_product`
# itself hand-maintains `_NYH_DISPLAY` — "an explicit table rather than a
# prefix match... a new building should appear as itself or not at all":
#   - "selector" spans two real GitHub repos (convivy/selector and
#     convivy/selector-backend, confirmed live 2026-08-21); _canon_product
#     doesn't fold the second into the first, so its runs report under
#     "selector-backend" and would otherwise never match "selector".
#   - "nyh-studio" isn't a live Course product today, but the alias is kept
#     for the same reason it was added: _canon_product deliberately SPLITS
#     an umbrella name into per-property buckets, summed back here since this
#     page shows one row per product, not per property.
# A product absent from this table that turns out to need an alias just shows
# no cost, same honest degrade as before this table existed — never a crash.
_COST_ALIASES = {
    "hollis": ("Hollis",),
    "selector": ("selector", "selector-backend"),
    "nyh-studio": ("NYH Arch Tools", "East Texas Circle", "Park Villa", "nyh-studio"),
}


def _cost_for(name: str, rollup: dict) -> dict | None:
    """A project's cost row, summed across every ledger product name it's
    known to report under. None (not a zeroed row) when none of them appear
    in the rollup, so the page can still say "no data" rather than a
    misleading "$0.00" for a project that simply had no runs this window."""
    total = {"cost_usd": 0.0, "runs": 0}
    found = False
    for n in _COST_ALIASES.get(name, (name,)):
        row = rollup.get(n)
        if row:
            found = True
            total["cost_usd"] += row["cost_usd"]
            total["runs"] += row["runs"]
    return total if found else None


def _log_swallowed(what: str, ex: BaseException) -> None:
    """Print a swallowed exception to stderr, and never raise doing it.

    The `try` around the print is not defensive padding. `print` to a closed
    or broken stream raises `ValueError: I/O operation on closed file`, and
    this helper is called from inside the one handler that exists so a single
    course's detail failure cannot take every other row down with it — so an
    unguarded print would reintroduce, one line lower, exactly the failure
    that handler catches (PR #203).

    stderr rather than a logger because this repo has no logging setup on the
    render path; launchd captures it into the serve log, the same
    guaranteed-visible local path `deploy_watch` uses.

    Exception text is caller-influenced — a malformed id can shape a Library
    error message — so it goes through `logutil.clean_log_field`, the same
    control-character rule `server.py._log_field` applies (security review,
    PR #180, and again on PR #203)."""
    try:
        print(clean_log_field(f"chart_house: {what} — {type(ex).__name__}: {ex}"),
              file=sys.stderr)
    except Exception:
        pass


def _step_class(entry: library_client.CourseEntry) -> str:
    """One Course step's track colour: "d" done, "a" approved ahead, "p"
    proposed and awaiting the operator, or "u" not yet specified.

    Priority is state-then-maturity, in that order: a terminal issue (fixed,
    wontfix, duplicate, redirected) counts as done regardless of what its
    `maturity` field still says, because `maturity` is set at proposal/
    approval time and nothing clears it on resolution. Verified against the
    live bosun Course (2026-08-28): 10 entries, all `state == "open"` but for
    one `pr-open`, and it is `maturity` alone — 1 `"approved"`, 5
    `"proposed"`, 4 `None` — that produces the 1/5/4 split the approved
    mockup shows.

    The terminal test itself is `waypoints.is_passed` — a waypoint on
    /velocity and a "d" step here are the same fact read two ways, so both
    read it off one definition rather than two that could drift."""
    if waypoints.is_passed(entry):
        return "d"
    if entry.maturity == "approved":
        return "a"
    if entry.maturity == "proposed":
        return "p"
    return "u"


def _awaits_signoff(entry: library_client.CourseEntry) -> bool:
    """True only for a step genuinely blocked on the operator: proposed, and a
    specification actually written for them to sign off on. A whitespace-only
    specification counts as none — it is not something to sign off on either.

    `maturity == "proposed"` alone is not this: that's the crew's claim about
    where the step stands, not evidence the step has anything for the operator to read.
    Conflating the two is issue_01M1FN5GDQGHR1PXY4AG9QTBWX — the page told the operator
    5 specs awaited their sign-off when 4 of them didn't exist."""
    return entry.maturity == "proposed" and bool((entry.specification or "").strip())


def _proposed_without_spec(entry: library_client.CourseEntry) -> bool:
    """The other half of the same split: proposed, with nothing written yet.
    This step is blocked on the crew writing a specification, not on the operator."""
    return entry.maturity == "proposed" and not (entry.specification or "").strip()


def _course_rows() -> list[dict]:
    """One row per product's live Course: title, version, and its steps
    classified by `_step_class`.

    Raises on a `list_courses` failure — see the module docstring for why
    that's now correct here. `get_course` per course still degrades to the
    `list_courses` record (entries with no state/maturity resolved, which
    `_step_class` reads as "u", the conservative read) rather than dropping
    the whole page over one course's timeout."""
    courses = sorted(library_client.list_courses(status="live"), key=lambda c: c.product)
    rows = []
    for course in courses:
        try:
            detail = library_client.get_course(course.id) or course
        except Exception as ex:
            _log_swallowed(f"course {course.id} detail unavailable", ex)
            detail = course
        steps = [_step_class(e) for e in detail.entries]
        rows.append({
            "product": course.product,
            "title": course.title,
            "version": course.version,
            "steps": steps,
            "total": len(steps),
            "done": steps.count("d"),
            "approved": steps.count("a"),
            "proposed": steps.count("p"),
            "unspecified": steps.count("u"),
            "awaiting_signoff": sum(1 for e in detail.entries if _awaits_signoff(e)),
            "proposed_unspecified": sum(1 for e in detail.entries if _proposed_without_spec(e)),
        })
    return rows


# Two DIFFERENT reasons a product's bead is filed under another project's key,
# and the distinction matters to anyone editing this table.
#
# A NAMING MISMATCH. The Library's Course `product` key and its
# reference-document `project` key are two independent vocabularies for the same
# real-world project, confirmed live 2026-08-28 — the same shape `_COST_ALIASES`
# above documents for the cost ledger's own product spelling:
#   - "bosun" is the Course's product key (what the operator dispatches to); its bead
#     is filed under the agent-platform repo's project key.
#   - "convivy-deck" is the Course's product key (and the GitHub repo); its
#     bead is filed under "deck".
# Both name one project's OWN bead. Rename either vocabulary and the entry moves.
#
# NO BEAD OF ITS OWN, DELIBERATELY. `shipwright` is not a naming mismatch: it has
# no bead and is not meant to get one. The operator, 2026-09-05, recorded in §5 of
# `company/bead/convivy-builder`: a crew built to work an existing destination
# keeps no bead of its own, because shipwright packages this product rather than
# heading somewhere else, and "a second bead there would describe the same
# endpoint twice and drift from it, so shipwright works this document instead."
# So this entry points at a bead shipwright SHARES, and it stands until that
# decision changes rather than until a key is renamed. Without it the row
# rendered "no bead on file ... yet", stating the opposite of a settled call
# while Deck resolved the same crew correctly.
#
# A product not listed here is looked up under its own Course product key
# directly, which is already correct for convivy-orient/library/selector/vivy.
_BEAD_PROJECT_ALIASES = {
    "bosun": "agent-platform",
    "convivy-deck": "deck",
    "shipwright": "company",
}


def _bead_ids() -> dict[str, str]:
    """{reference-document project: doc id} for every live "bead" document —
    read fresh each render rather than hardcoded, so a project that gains a
    bead is picked up on the next render with no code change. Uncaught on a
    Library failure, like every other read on this page now (module
    docstring)."""
    return {d.project: d.id for d in library_client.list_reference_documents(audience="team")
            if d.kind == "bead"}


def _bead_href(product: str, bead_ids: dict[str, str]) -> str | None:
    """The project's bead URL, or None if it has no bead on file — the caller
    renders that as an unlinked name with a visible reason, never as a link to
    a document that doesn't exist.

    **None means "nothing found", not "this project is meant to have none".**
    A crew that deliberately works another project's bead belongs in
    `_BEAD_PROJECT_ALIASES` above and resolves to a link here; shipwright is
    the case that taught us the difference."""
    doc_id = bead_ids.get(_BEAD_PROJECT_ALIASES.get(product, product))
    # safe="/" keeps a doc id's own hierarchical slashes readable in the URL
    # while still escaping anything else (same convention as home_build.py's
    # identical /reference/doc/ href).
    return f"/reference/doc/{quote(doc_id, safe='/')}" if doc_id else None


# The Course's `product` key and the GitHub repo a project's PRs actually live
# under are, again, two independent vocabularies (module docstring's alias
# tables above document the same split for cost and bead lookups):
#   - "bosun" dispatches against the agent-platform repo — confirmed live
#     2026-08-28 (its one open PR is agent-platform#464).
#   - "selector" spans two real GitHub repos, the same pair _COST_ALIASES
#     documents for the run ledger — its open PRs and merges are split across
#     both and must be summed, not read from "selector" alone.
# A product absent here is looked up under its own Course product key, already
# correct for convivy-orient/library/convivy-deck/vivy.
_REPO_ALIASES = {
    "bosun": ("agent-platform",),
    "selector": ("selector", "selector-backend"),
}


def _repo_names_for(product: str) -> tuple[str, ...]:
    return _REPO_ALIASES.get(product, (product,))


def _merge_counts_by_repo() -> dict[str, int]:
    """{repo: merges in the trailing MERGE_WINDOW_DAYS}, from `changelog`-kind
    signals with `source=github` — one per merged PR
    (`github_capture.signal_from_merge`), keyed by repo the same way that
    writer sets `product`. Uncaught on a Library failure, like every other read
    on this page (module docstring): a broken feed must raise, not report a
    quiet zero.

    `list_signals` has no server-side date filter, so this scans the most
    recent `_MERGE_SCAN_CAP` rows and filters `produced_at` locally — the same
    trade `dashboard_build`'s cost rollup makes for its own trailing window."""
    cutoff = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=MERGE_WINDOW_DAYS)
    counts: dict[str, int] = {}
    for sig in library_client.list_signals(source=["github"], kind=["changelog"], cap=_MERGE_SCAN_CAP):
        if sig.produced_at and sig.produced_at >= cutoff:
            counts[sig.product] = counts.get(sig.product, 0) + 1
    return counts


def _merged_count(product: str, merge_counts: dict[str, int]) -> int:
    return sum(merge_counts.get(repo, 0) for repo in _repo_names_for(product))


def _project_open_prs(product: str, open_prs: list[dict]) -> list[dict]:
    repos = set(_repo_names_for(product))
    return [pr for pr in open_prs if pr.get("repo") in repos]


# Cross-PR aggregation order, most attention-worthy first — the same triage
# instinct `_state_chip` already applies (a step awaiting the operator outranks a step
# that's merely done): if anything in the project's open queue is broken, that
# beats a draft, which beats "nothing further needed but a click." A project
# with PRs in more than one bucket shows only the worst one; the merge-queue
# link is where the rest of the detail lives.
_GROUP_ORDER = ("blocked", "in-cycle", "draft", "cleared")
_GROUP_DOT = {"blocked": "err", "in-cycle": "warn", "draft": "draft", "cleared": "ok"}
_CI_LABEL = {"passing": "CI passing", "failing": "CI failing",
             "pending": "CI pending", "none": "CI not run"}
_CI_PRIORITY = {"failing": 0, "pending": 1, "none": 2, "passing": 3}


def _worst_ci(members: list[dict]) -> str:
    return min((m["ci"] for m in members), key=lambda c: _CI_PRIORITY.get(c, 9))


def _bucket_label(group: str, members: list[dict]) -> str:
    n = len(members)
    if group == "cleared":
        return f"{n} ready to merge"
    if group == "draft":
        noun = "draft" if n == 1 else "drafts"
        return f"{n} {noun} — {_CI_LABEL[_worst_ci(members)]}"
    if group == "in-cycle":
        # ci is always "pending" or "none" here — "passing" with no draft/
        # conflict would already be "cleared" (merge_queue_build._classify).
        label = "CI running" if _worst_ci(members) == "pending" else "in review"
        return f"{n} {label}"
    # blocked: CI failing takes priority over a conflict, same order
    # merge_queue_build._ci_status applies within a single PR's own rollup.
    reason = "CI failing" if any(m["ci"] == "failing" for m in members) else "merge conflict"
    return f"{n} blocked — {reason}"


def _open_pr_summary(prs: list[dict]) -> tuple[str, str] | None:
    """(dot class, label text) for the worst state among a project's open PRs,
    or None if it has none."""
    by_group: dict[str, list[dict]] = {}
    for pr in prs:
        by_group.setdefault(pr["group"], []).append(pr)
    for group in _GROUP_ORDER:
        members = by_group.get(group)
        if members:
            return _GROUP_DOT[group], _bucket_label(group, members)
    return None


def _ribbon_html(product: str, open_prs: list[dict] | None, merge_counts: dict[str, int]) -> str:
    """The PR ribbon: the open queue first (a link, an unavailable notice, or
    the "no PRs open" sentence), then a week of merges — never on a shared
    scale, per the mockup's own footnote.

    `open_prs is None` means `merge_queue_build.cached_open_prs()` has nothing
    warm — "cannot say," not "zero" (server.py's design, extended here): the
    ribbon must never let a cold or broken feed read as an empty queue."""
    if open_prs is None:
        open_html = '<span class="rb-none">PR feed unavailable</span>'
    else:
        prs = _project_open_prs(product, open_prs)
        summary = _open_pr_summary(prs)
        if summary is None:
            open_html = '<span class="rb-none">no PRs open</span>'
        else:
            dot_class, text = summary
            open_html = (f'<a class="rb-open" href="/engineering/merge-queue">'
                         f'<span class="rdot {dot_class}"></span>{escape(text)}</a>')
    merged = _merged_count(product, merge_counts)
    merged_html = f'<span class="rb-merged">{merged} merged, {MERGE_WINDOW_DAYS} days</span>'
    return f'<div class="ribbon num">{open_html}<span class="rb-sep">&middot;</span>{merged_html}</div>'


def _state_chip(done: int, approved: int, awaiting_signoff: int, proposed_unspecified: int,
                 cost: dict | None) -> tuple[str, str]:
    """(CSS class, label) for a row's state chip, in priority order:

    1. A step genuinely awaiting the operator's sign-off (proposed, and a
       specification actually written — `_awaits_signoff`) outranks
       everything else: the Course cannot move until they look. A step that's
       merely `maturity == "proposed"` with nothing written yet does NOT
       qualify here — that was issue_01M1FN5GDQGHR1PXY4AG9QTBWX, the chip
       telling the operator they were the blocker for steps that had nothing for them to
       read.
    2. Any step done means real progress is shipping — "underway".
    3. Approved steps with nothing done yet: charted, not started.
    4. Nothing awaiting sign-off, done or approved, but a step has been
       proposed with no specification written yet: blocked on the crew
       writing it, not on the operator — say so rather than going silent about it.
    5. Nothing charted at all (no done, approved or proposed step) but real
       spend this window: "running uncharted" — the anomaly the mockup's
       vivy row names, work happening against a plan that was never written.
    6. Nothing charted and no spend either: genuinely idle."""
    if awaiting_signoff:
        return "chip-warn", "awaits your sign-off"
    if done:
        return "chip-ok", "underway"
    if approved:
        return "chip-muted", "charted, not started"
    if proposed_unspecified:
        return "chip-muted", "proposed, no spec yet"
    if cost is not None:
        return "chip-err", "running uncharted"
    return "chip-muted", "not yet specified"


# --- render ------------------------------------------------------------------


def _join_and(items: list[str]) -> str:
    """["Deck", "Bosun", "Selector"] -> "Deck, Bosun and Selector"."""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _track_aria(r: dict) -> str:
    parts = []
    if r["done"]:
        parts.append(f'{r["done"]} done')
    if r["approved"]:
        parts.append(f'{r["approved"]} approved')
    if r["proposed"]:
        parts.append(f'{r["proposed"]} proposed')
    if r["unspecified"]:
        parts.append(f'{r["unspecified"]} unspecified')
    total = r["total"]
    return f'{total} step{"s" if total != 1 else ""}: ' + ", ".join(parts)


def _counts_text(r: dict) -> str:
    """Splits the proposed bucket the same way the banner and chip now do:
    steps genuinely awaiting the operator's sign-off (a specification actually
    written) get one clause, steps merely proposed with nothing written yet
    get another — never one combined "N proposed, awaiting you" that claims
    the operator is blocking all of them (issue_01M1FN5GDQGHR1PXY4AG9QTBWX)."""
    done, approved, proposed, unspecified, total = (
        r["done"], r["approved"], r["proposed"], r["unspecified"], r["total"])
    awaiting_signoff, proposed_unspecified = r["awaiting_signoff"], r["proposed_unspecified"]
    if done == 0 and approved == 0 and proposed == 0:
        return f'{total} step{"s" if total != 1 else ""} &middot; never specified'
    parts = [f"{done} of {total} done"]
    if approved:
        parts.append(f"{approved} approved ahead" if not proposed and not unspecified
                      else f"{approved} approved")
    if awaiting_signoff:
        href = escape(issue_build.course_anchor_href(r["product"]), quote=True)
        parts.append(f'<b class="wait"><a href="{href}">{awaiting_signoff} '
                      f'spec{"s" if awaiting_signoff != 1 else ""} awaiting you</a></b>')
    if proposed_unspecified:
        parts.append(f"{proposed_unspecified} proposed, no spec yet")
    if unspecified:
        parts.append(f"{unspecified} unspecified")
    return " &middot; ".join(parts)


def _anomaly_note(r: dict) -> str:
    combined = " &mdash; more than every other project combined" if r["dominant"] else ""
    return (f'<p class="anomaly-note">{r["cost"]["runs"]} runs and ${r["cost"]["cost_usd"]:,.2f} '
            f'in {WINDOW_DAYS} days{combined} &mdash; against a {r["total"]}-step course that has '
            f'never been specified. The fleet&rsquo;s spend is here; its plan is not.</p>')


def _row_html(r: dict) -> str:
    attn = ""
    if r["chip_class"] == "chip-warn":
        attn = " attn-warn"
    elif r["chip_class"] == "chip-err":
        attn = " attn-err row-anomaly"

    label = escape(r["label"])
    if r["href"]:
        proj_html = f'<a class="name" href="{escape(r["href"], quote=True)}">{label}</a>'
        ver_extra = ""
    else:
        proj_html = (f'<span class="name name-unlinked" '
                     f'title="No bead on file for this project yet.">{label}</span>')
        ver_extra = " &middot; no bead on file"

    track = "".join(f'<span class="sg {c}"></span>' for c in r["steps"])
    if r["cost"]:
        cost = r["cost"]
        spend = (f'<span class="cost num">${cost["cost_usd"]:,.2f}</span>'
                 f'<span class="runs num">{cost["runs"]} run{"s" if cost["runs"] != 1 else ""}</span>')
    else:
        spend = ('<a class="cost-na" href="#fn-cost" title="Attribution is broken upstream; '
                 'see the note below.">no attributed cost&#8224;</a>'
                 '<span class="runs">runs unknown</span>')
    anomaly = _anomaly_note(r) if r["chip_class"] == "chip-err" else ""

    # "awaits your sign-off" is a request for the operator's input, not just a status
    # word — it has to lead somewhere they can act on it, the proposed steps
    # themselves on /engineering/course, rather than leave a reader to guess
    # where a sign-off happens (issue_01M17W7DRZ3FJNRNYCAQFWHS1F). Every other
    # chip state is a status report with no action to take, so only this one
    # renders as a link.
    if r["chip_class"] == "chip-warn":
        chip_href = escape(issue_build.course_anchor_href(r["product"]), quote=True)
        chip_html = (f'<a class="chip {r["chip_class"]}" href="{chip_href}">'
                     f'{escape(r["chip_label"])}</a>')
    else:
        chip_html = f'<span class="chip {r["chip_class"]}">{escape(r["chip_label"])}</span>'

    return f'''<div class="row g{attn}">
  <div class="proj">
    {proj_html}
    <span class="ver num">course v{escape(str(r["version"]))}{ver_extra}</span>
  </div>
  <div class="course">
    <p class="ctitle">{escape(r["title"])}</p>
    <div class="track" role="img" aria-label="{escape(_track_aria(r), quote=True)}">{track}</div>
    <p class="counts num">{_counts_text(r)}</p>
  </div>
  <div class="state">{chip_html}</div>
  <div class="spend">{spend}</div>
  {anomaly}
  {r["ribbon_html"]}
</div>'''


def _signals_html(rows: list[dict]) -> str:
    lines: list[tuple[str, str]] = []
    for r in rows:
        if r["awaiting_signoff"]:
            n = r["awaiting_signoff"]
            href = escape(issue_build.course_anchor_href(r["product"]), quote=True)
            lines.append(("warn", f'<strong>{escape(r["label"])}</strong> has <a href="{href}">{n} '
                          f'spec{"s" if n != 1 else ""} awaiting your sign-off</a> &mdash; its Course '
                          f'cannot move until you look.'))
    for r in rows:
        if r["proposed_unspecified"]:
            n = r["proposed_unspecified"]
            lines.append(("mut", f'<strong>{escape(r["label"])}</strong> has {n} '
                          f'step{"s" if n != 1 else ""} proposed with no specification written yet '
                          f'&mdash; waiting on the crew, not on you.'))
    for r in rows:
        if r["chip_class"] == "chip-err":
            combined = (" &mdash; more than the rest of the fleet combined"
                        if r["dominant"] else "")
            lines.append(("err", f'<strong>{escape(r["label"])}</strong> ran '
                          f'{r["cost"]["runs"]} times for ${r["cost"]["cost_usd"]:,.2f}{combined} '
                          f'&mdash; with no approved plan.'))
    no_cost = [r["label"] for r in rows if not r["cost"]]
    if no_cost:
        lines.append(("mut", "Cost attribution is broken upstream: "
                      + escape(_join_and(no_cost))
                      + ' show <a href="#fn-cost">no attributed cost</a>, which is not the same '
                      'thing as zero.'))
    if not lines:
        return ""
    items = "".join(f'<p class="sig"><span class="sigdot {cls}"></span><span>{text}</span></p>'
                     for cls, text in lines)
    return f'<div class="signals">{items}</div>'


# --- the operator's approve control (issue_01M32CB6A9T04NVYPARGDT2GWQ) ------
#
# **The operator, 2026-09-21:** "all epics stay as proposed, and are displayed as
# proposed, until the operator presses the approve button in either Deck or
# Orient, and that is the path to mark epics as approved." Pressing it appends
# the epic to its product's Course, in the same Library transaction.
#
# **THIS SECTION EXISTS BECAUSE THE COURSE ROWS ABOVE CANNOT CARRY THE
# CONTROL.** They render step COUNTS and coloured track segments, naming no
# individual epic, so there is nothing there to attach a per-epic button to.
# And under the new design the case that matters most — an epic proposed and
# not yet on any Course — produces no step at all, because approval is what
# places it. A control hung off the Course rows would therefore be missing for
# exactly the epics it exists to approve.


def _proposed_epics() -> list[library_client.IssueRecord]:
    """Every epic awaiting approval, product-sorted then title-sorted.

    Read from `/items` rather than from the Courses above, for the reason the
    section comment gives: an unapproved epic need not be on a Course, and
    after the Library's change a newly filed one never is. The list route
    serves `specification`, confirmed against the live Library on 2026-09-21,
    so whether an epic has one is known without a per-epic detail fetch.

    Uncaught on a Library failure, like every other read on this page (module
    docstring): a page that silently showed no epics awaiting approval would
    tell the operator there is nothing to approve, which is the one wrong answer this
    section must never give.

    **AN EPIC IS LISTED WHEN SOMEBODY HAS PUT IT FORWARD, WHICH IS NOT THE SAME
    AS `maturity == "proposed"`.** Two ways an epic gets here:

    - the crew marked it `proposed`, whatever it has written. A missing
      specification on one of those is an anomaly worth seeing rather than
      hiding: the Library refuses that approval, the refusal names the cause,
      and the row is flagged "no specification yet" before the operator presses
      anything.
    - its maturity is unset but a specification exists. **This is the case a
      strict `== "proposed"` filter strands, and it is not hypothetical**: of
      93 non-terminal epics on 2026-09-21, 18 carried no maturity at all and
      two of those carried a full specification. `POST /items` has never
      accepted `maturity`, so an epic is created with none and something later
      sets it — an epic whose specification was written without that step is
      invisible to a filter that demands the word, and approving is now the
      only way anything reaches `approved`, so nobody could ever move it.
      `issue_build` badges the same state for the same reason: unset is not
      approved, and a silent absence is exactly what used to read as approved.

    An epic with neither a maturity nor a specification has been put forward by
    nobody, and the other 16 stay out: the Library refuses an approval with no
    specification, so every one of those rows would be a button the operator cannot
    press.

    **WHAT THOSE 16 GET INSTEAD, STATED ACCURATELY BECAUSE AN EARLIER DRAFT OF
    THIS COMMENT GOT IT WRONG.** They are NOT reported by
    `_proposed_without_spec`, which requires `maturity == "proposed"` and so
    never sees them; a reviewer caught that claim here. What they actually get
    is `_step_class`'s `"u"` bucket — a "not yet specified" segment on their
    product's track, plus the per-row counts — and only if the epic sits on a
    live Course at all. Nothing puts them on a signal line, and an unset-
    maturity epic on no Course is surfaced nowhere on this page.

    That gap is named rather than closed. Nobody has reported being unable to
    find such an epic, and the section this docstring belongs to is for the
    operator's own action list rather than for crew backlog, so widening it on
    a possibility would put 16 unpressable rows in front of the operator to solve a
    problem no one has had."""
    items = library_client.list_issue_items()
    awaiting = [i for i in items
                if i.type == "epic" and i.state not in ISSUE_TERMINAL
                and issue_build.put_forward(i)]
    return sorted(awaiting, key=lambda i: (i.product, i.title.lower()))


def _approvals_html(epics: list[library_client.IssueRecord]) -> str:
    """The Approve control on every proposed epic.

    **An epic with no specification keeps its button.** `_proposed_without_spec`
    above already tells the operator that step waits on the crew rather than on them, and
    it is flagged here too — but the Library refuses an approval for an epic
    carrying no specification, and that refusal is the one the page must be
    able to show. Disabling the button would hide the refusal path behind a
    state nobody can reach, which is the opposite of what the definition of
    done asks for.

    `data-can-approve` is the `__CAN_APPROVE_EPIC__` substitution server.py
    applies from the viewer's role, matching `__CAN_KB_WRITE__`: cosmetic,
    since approve_api's own gate (and the Library's `epic.approve` capability
    behind it) refuses a contractor whatever the page renders."""
    if not epics:
        return ""
    cards = []
    for e in epics:
        href = escape(f"/engineering/issues/{quote(e.id, safe='')}", quote=True)
        label = escape(build.PROJECT_LABEL.get(e.product, e.product))
        nospec = ("" if (e.specification or "").strip() else
                  '<span class="ap-nospec">no specification yet</span>')
        cards.append(
            f'<div class="ap-row" data-epic="{escape(e.id, quote=True)}">'
            f'<div class="ap-main">'
            f'<span class="ap-prod">{label}</span>'
            f'<a class="ap-title" href="{href}">{escape(e.title)}</a>'
            f'{nospec}</div>'
            f'<div class="ap-act">'
            f'<button type="button" class="ap-btn" data-approve="{escape(e.id, quote=True)}">'
            f'Approve</button></div>'
            f'<p class="ap-note" hidden></p>'
            f'</div>')
    n = len(epics)
    return (f'<section class="approvals" data-can-approve="__CAN_APPROVE_EPIC__">'
            f'<h2 class="ap-head">{n} epic{"s" if n != 1 else ""} awaiting your approval</h2>'
            f'<p class="ap-lede">Approving an epic puts it on its project&rsquo;s Course, '
            f'at the end. Nothing else here changes what an epic is; read it first.</p>'
            f'{"".join(cards)}</section>'
            f'<script>{_APPROVE_JS}</script>')


_APPROVE_JS = r"""
(function () {
  var section = document.querySelector('.approvals');
  if (!section || section.dataset.canApprove !== 'true') return;
  section.querySelectorAll('[data-approve]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var row = btn.closest('.ap-row');
      var note = row.querySelector('.ap-note');
      btn.disabled = true;
      note.hidden = true;
      fetch('/api/epics/' + encodeURIComponent(btn.dataset.approve) + '/approve',
            { method: 'POST', credentials: 'same-origin' })
        .then(function (r) {
          return r.json().catch(function () { return {}; })
            .then(function (j) { return { ok: r.ok, body: j }; });
        })
        .then(function (res) {
          if (res.ok) {
            // The epic is approved and on the Course; the row stops being an
            // action and becomes a record of one, since the page behind it is
            // now stale and re-rendering the whole thing to move one row would
            // lose every other note on screen.
            row.classList.add('ap-done');
            btn.remove();
            note.className = 'ap-note ok';
            note.textContent = res.body.position
              ? 'Approved — placed at position ' + res.body.position + ' on the Course. '
                + 'Reload to see the Course update.'
              : 'Approved. Reload to see the Course update.';
            note.hidden = false;
            return;
          }
          // The epic stays proposed and keeps its control, so the button comes
          // back. The Library's own wording is what is shown: the causes —
          // no specification, no live Course for the product, held by another
          // session — are different 409s that a status code cannot tell apart.
          btn.disabled = false;
          note.className = 'ap-note err';
          note.textContent = (res.body && res.body.error) || "Couldn't approve this epic.";
          note.hidden = false;
        })
        .catch(function () {
          btn.disabled = false;
          note.className = 'ap-note err';
          note.textContent = "Couldn't reach the server.";
          note.hidden = false;
        });
    });
  });
})();
"""


def _footnote_html(rows: list[dict]) -> str:
    no_cost = [r["label"] for r in rows if not r["cost"]]
    cost_fn = ""
    if no_cost:
        cost_fn = (f'<p id="fn-cost">&#8224; <strong>No attributed cost</strong> &mdash; cost '
                   f'attribution is broken upstream (issue_01M15C50ESYBPTDWV5FHZZXVH5): most '
                   f'ledger rows currently carry no product. {escape(_join_and(no_cost))} show '
                   f'no figure here for that reason; a blank means the ledger cannot say, never '
                   f'that nothing was spent.</p>')
    return f'''<div class="foot">
    {cost_fn}
    <p>The PR ribbon reads the merge queue&rsquo;s feed. The open queue comes first
    &mdash; each pull request is a draft, in review, in CI, or ready to merge &mdash;
    because it is work someone can act on; the trailing count is merges over
    {MERGE_WINDOW_DAYS} days, throughput already behind the ship. The two are different
    kinds of number and are never drawn to a shared scale. When the feed cannot be read,
    the ribbon says <em>PR feed unavailable</em> rather than a zero.</p>
    <p>Cost and runs come from local run ledgers under <code>~/Convivy/agent-runs</code>,
    trailing {WINDOW_DAYS} days. The course version counts approved revisions of the plan.
    Each track draws the Course at true scale &mdash; one cell per step &mdash; so a wide
    empty cell is a short, unspecified plan, not a rendering quirk. A project name links to
    its bead, the document describing where it is going and what is missing; a project with
    no bead on file renders unlinked, with the reason stated on the row.</p>
  </div>'''


def live_html() -> str:
    course_rows = _course_rows()
    bead_ids = _bead_ids()
    rollup = _cost_rollup()
    open_prs = merge_queue_build.cached_open_prs()
    merge_counts = _merge_counts_by_repo()

    rows = []
    for r in course_rows:
        cost = _cost_for(r["product"], rollup)
        chip_class, chip_label = _state_chip(
            r["done"], r["approved"], r["awaiting_signoff"], r["proposed_unspecified"], cost)
        rows.append({
            **r,
            "label": build.PROJECT_LABEL.get(r["product"], r["product"]),
            "href": _bead_href(r["product"], bead_ids),
            "cost": cost,
            "chip_class": chip_class,
            "chip_label": chip_label,
            "ribbon_html": _ribbon_html(r["product"], open_prs, merge_counts),
        })
    total_known_cost = sum(r["cost"]["cost_usd"] for r in rows if r["cost"])
    for r in rows:
        r["dominant"] = bool(r["cost"]) and r["cost"]["cost_usd"] > (
            total_known_cost - r["cost"]["cost_usd"])

    approvals_html = _approvals_html(_proposed_epics())
    rows_html = "".join(_row_html(r) for r in rows) or ds.empty_state("No live Courses.")
    tally = (f'{len(rows)} project{"s" if len(rows) != 1 else ""} &middot; '
             f'{sum(r["total"] for r in rows)} steps charted &middot; '
             f'{sum(r["done"] for r in rows)} done')

    body = f'''<div class="ch">
  <header class="ch-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / chart house</div>
    <h1>Chart House</h1>
    <p class="lede">Where each project is along its Course, and whether it is moving.
    A step is <em>done</em>, <em>approved</em> ahead of the ship, <em>proposed</em> and
    waiting on you, or not yet specified. Spend and runs are a trailing {WINDOW_DAYS}-day
    window; the ribbon under each row is the project&rsquo;s pull requests &mdash; the
    open queue by state, then a week of merges.</p>
  </header>
  <div class="wrap">
    {_signals_html(rows)}
    {approvals_html}
    <div class="legendbar">
      <span class="tally num">{tally}</span>
      <div class="legend">
        <span class="lg"><span class="sw d"></span>done</span>
        <span class="lg"><span class="sw a"></span>approved ahead</span>
        <span class="lg"><span class="sw p"></span>proposed &mdash; awaits you</span>
        <span class="lg"><span class="sw u"></span>not yet specified</span>
      </div>
    </div>
    <div class="scroll"><div class="inner">
      <div class="g head">
        <span>Project</span><span>Course</span><span>State</span><span>{WINDOW_DAYS} days</span>
      </div>
      {rows_html}
    </div></div>
    {_footnote_html(rows)}
  </div>
</div>'''
    return ds.page_shell(title="Chart House",
                         nav_html=ds.nav_component("/chart-house", "full"),
                         body=body, extra_css=_CSS)


_CSS = r"""
/* The operator's approve control (issue_01M32CB6A9T04NVYPARGDT2GWQ). Sits
   between the signals and the Course table: it is the one thing on this page
   that is an ACTION rather than a read, so it leads. */
.approvals { margin: 0 0 18px; }
.approvals[data-can-approve="false"] { display: none; }
.ap-head { font-size: 15px; font-weight: 700; letter-spacing: -.01em; margin: 0 0 4px; }
.ap-lede { font-size: 13px; color: var(--ink-3); margin: 0 0 10px; max-width: 62ch; }
.ap-row { display: grid; grid-template-columns: 1fr auto; gap: 8px 14px; align-items: center;
          padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px;
          background: var(--surface); margin-bottom: 8px; }
.ap-main { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; min-width: 0; }
.ap-prod { font-size: 11px; font-weight: 600; padding: 2px 9px; border-radius: 999px;
           background: var(--brand-wash); color: var(--brand-ink); white-space: nowrap; }
.ap-title { font-size: 14px; font-weight: 600; color: var(--ink); text-decoration: none;
            overflow-wrap: anywhere; }
.ap-title:hover { text-decoration: underline; }
/* An epic with nothing written to approve. It keeps its button on purpose —
   the Library refuses it, and that refusal is what the page has to be able to
   show (see _approvals_html). */
.ap-nospec { font-size: 11px; font-weight: 600; padding: 2px 9px; border-radius: 999px;
             background: var(--surface-2); color: var(--ink-3); white-space: nowrap; }
.ap-btn { padding: .4rem .95rem; border: 1px solid var(--brand); border-radius: 8px;
          background: var(--brand); color: #fff; font: inherit; font-weight: 600;
          font-size: 13px; cursor: pointer; }
.ap-btn:hover { filter: brightness(1.06); }
.ap-btn:disabled { opacity: .6; cursor: default; }
/* The note spans both columns, under the row it belongs to, so a refusal
   renders WHERE THE CONTROL IS rather than somewhere else on the page. */
.ap-note { grid-column: 1 / -1; margin: 0; font-size: 12.5px; padding: .45rem .65rem;
           border-radius: 6px; }
.ap-note.ok { background: #e4f5ea; color: #1c7a41; }
.ap-note.err { background: #fbe4e0; color: #b23c1c; }
.ap-row.ap-done { opacity: .75; }
@media (max-width: 560px) {
  .ap-row { grid-template-columns: 1fr; }
  .ap-act { justify-self: start; }
}
.ch{--muted:var(--ink-2);--faint:var(--ink-3);--accent:var(--brand-ink);--accent-soft:var(--brand-wash);}
:root{
  --seg-done:#2E8A5C;--seg-approved:#BFE3D0;--seg-approved-edge:#5D9F7D;
  --seg-proposed:#EAA93F;--seg-proposed-edge:#B67D1F;--seg-unset:#C9BCAC;
}
[data-theme="dark"]{
  --seg-done:#4FB07E;--seg-approved:#2C4A38;--seg-approved-edge:#4F8A67;
  --seg-proposed:#D99A3E;--seg-proposed-edge:#8A6323;--seg-unset:#56473A;
}
.ch .ch-head{margin-bottom:18px;}
.ch .eyebrow-line{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;
color:var(--faint);display:flex;align-items:center;gap:8px;}
.ch .eyebrow-line .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);
box-shadow:0 0 0 4px var(--accent-soft);}
.ch .lede{color:var(--ink-2);max-width:70ch;margin:10px 0 0;}
.ch .signals{margin-top:18px;display:flex;flex-direction:column;gap:7px;}
.ch .sig{margin:0;display:flex;align-items:baseline;gap:10px;font-size:.86rem;color:var(--ink-2);}
.ch .sig strong{color:var(--ink);font-weight:600;}
.ch .sig a{color:inherit;}
.ch .sigdot{flex:none;width:8px;height:8px;border-radius:50%;transform:translateY(.5px);}
.ch .sigdot.warn{background:var(--warn);}
.ch .sigdot.err{background:var(--err);}
.ch .sigdot.mut{background:var(--ink-3);}
.ch .legendbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;
gap:10px 18px;margin-top:22px;}
.ch .tally{font-family:var(--mono);font-size:.72rem;color:var(--ink-3);}
.ch .legend{display:flex;flex-wrap:wrap;gap:14px;}
.ch .lg{display:flex;align-items:center;gap:6px;font-size:.72rem;color:var(--ink-2);}
.ch .sw{width:15px;height:9px;border-radius:2px;}
.ch .sw.d{background:var(--seg-done);}
.ch .sw.a{background:var(--seg-approved);box-shadow:inset 0 0 0 1px var(--seg-approved-edge);}
.ch .sw.p{background:var(--seg-proposed);box-shadow:inset 0 0 0 1px var(--seg-proposed-edge);}
.ch .sw.u{background:transparent;border:1.5px dashed var(--seg-unset);}
.ch .scroll{overflow-x:auto;margin-top:14px;}
.ch .inner{min-width:860px;}
.ch .g{display:grid;grid-template-columns:190px minmax(340px,1fr) 168px 150px;gap:16px;align-items:center;}
.ch .g.head{padding:0 18px;}
.ch .g.head span{font-family:var(--mono);font-size:.65rem;font-weight:600;text-transform:uppercase;
letter-spacing:.06em;color:var(--ink-3);}
.ch .g.head span:last-child{text-align:right;}
.ch .row{background:var(--surface);border:1px solid var(--line);border-radius:10px;
box-shadow:var(--shadow);padding:14px 18px;margin-top:8px;}
.ch .row.attn-warn{border-color:color-mix(in srgb, var(--warn) 45%, var(--line));}
.ch .row.attn-err{border-color:color-mix(in srgb, var(--err) 45%, var(--line));}
.ch .name{font-weight:600;font-size:.92rem;color:var(--ink);text-decoration:underline;
text-decoration-color:color-mix(in srgb, var(--brand) 50%, transparent);text-underline-offset:3px;}
.ch a.name:hover{color:var(--brand-ink);text-decoration-color:var(--brand);}
.ch .name-unlinked{font-weight:600;font-size:.92rem;color:var(--ink);cursor:help;
text-decoration:underline dotted;text-decoration-color:var(--ink-3);text-underline-offset:3px;}
.ch .ver{display:block;margin-top:4px;font-family:var(--mono);font-size:.7rem;color:var(--ink-3);}
.ch .ctitle{margin:0 0 8px;font-size:.86rem;line-height:1.4;color:var(--ink);}
.ch .track{display:flex;gap:2px;height:12px;margin-bottom:7px;}
.ch .sg{flex:1;min-width:4px;border-radius:2.5px;}
.ch .sg.d{background:var(--seg-done);}
.ch .sg.a{background:var(--seg-approved);box-shadow:inset 0 0 0 1px var(--seg-approved-edge);}
.ch .sg.p{background:var(--seg-proposed);box-shadow:inset 0 0 0 1px var(--seg-proposed-edge);}
.ch .sg.u{background:transparent;border:1.5px dashed var(--seg-unset);border-radius:3px;}
.ch .counts{margin:0;font-family:var(--mono);font-size:.72rem;color:var(--ink-2);}
.ch .counts .wait{color:var(--warn);font-weight:600;}
.ch .counts .wait a{color:inherit;text-decoration:underline dotted;text-underline-offset:2px;}
.ch .chip{display:inline-block;white-space:nowrap;font-family:var(--mono);font-size:.66rem;
font-weight:600;text-transform:uppercase;letter-spacing:.05em;padding:4px 10px;border-radius:999px;
text-decoration:none;}
.ch a.chip{cursor:pointer;}
.ch a.chip:hover,.ch a.chip:focus-visible{box-shadow:inset 0 0 0 1px currentColor;}
.ch .chip-ok{background:var(--ok-wash);color:var(--ok);}
.ch .chip-warn{background:var(--warn-wash);color:var(--warn);}
.ch .chip-muted{background:var(--surface-2);color:var(--ink-2);}
.ch .chip-err{background:var(--err-wash);color:var(--err);}
.ch .spend{text-align:right;}
.ch .cost{display:block;font-family:var(--mono);font-size:.92rem;font-weight:600;color:var(--ink);}
.ch .runs{display:block;margin-top:3px;font-family:var(--mono);font-size:.7rem;color:var(--ink-3);}
.ch .cost-na{display:block;white-space:nowrap;font-family:var(--mono);font-size:.7rem;color:var(--ink-3);
text-decoration:underline dotted;text-underline-offset:3px;}
.ch a.cost-na:hover{color:var(--ink-2);}
.ch .row-anomaly .cost{color:var(--err);font-size:1.05rem;}
.ch .row-anomaly .runs{color:var(--err);font-weight:600;}
.ch .anomaly-note{grid-column:1 / -1;margin:10px 0 0;padding-top:10px;
border-top:1px dashed color-mix(in srgb, var(--err) 35%, var(--line));font-size:.76rem;
line-height:1.5;color:var(--err);}
.ch .ribbon{grid-column:1 / -1;margin:10px 0 0;padding-top:9px;border-top:1px solid var(--line);
display:flex;align-items:baseline;flex-wrap:wrap;gap:8px;font-family:var(--mono);font-size:.7rem;
color:var(--ink-3);}
.ch .rb-open{display:inline-flex;align-items:center;gap:6px;color:var(--ink-2);font-weight:500;
text-decoration:underline dotted;text-underline-offset:3px;}
.ch a.rb-open:hover{color:var(--ink);}
.ch .rdot{flex:none;width:7px;height:7px;border-radius:50%;}
.ch .rdot.ok{background:var(--ok);}
.ch .rdot.draft{background:var(--ink-3);}
.ch .rdot.warn{background:var(--warn);}
.ch .rdot.err{background:var(--err);}
.ch .rb-none,.ch .rb-sep,.ch .rb-merged{color:var(--ink-3);}
.ch .foot{margin-top:24px;font-size:.76rem;line-height:1.6;color:var(--ink-3);max-width:76ch;}
.ch .foot p{margin:0 0 8px;}
.ch .foot code{font-family:var(--mono);font-size:.7rem;}
"""


def build_static() -> None:
    """Seed the static fallback (issue_01M2KVHTVFF4MAQF94M2SP6TZ0): /chart-house
    had no builder at all, the same gap /crews had — a live-render throw
    during a Library stall had nothing to fall back to and server.py returned
    500 naming this file rather than a stale page. `live_html()` reads only
    the Library and `merge_queue_build`'s in-memory cache (no `gh` calls), so
    seeding is just calling it once at deploy time."""
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "chart-house.html").write_text(live_html(), encoding="utf-8")
    print("Wrote site/chart-house.html")


if __name__ == "__main__":
    build_static()
