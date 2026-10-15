#!/usr/bin/env python3.11
"""The human-facing surface of the issue-queue plane (ARCH-359 step 2a;
ARCH-361), now three pages (the operator, 2026-08-29 —
issue_01M1518ZFBGHAWC4Z46W893H2S):

- **Epics** (`/engineering/issues`, `live_html`) — an epic per project, each
  expanding into its active work items.
- **Corrections** (`/engineering/corrections`, `live_html_corrections`) — the
  corrections queue per project (active work items whose parent is null or
  that queue),
  each expanding into its work items.
- **Course** (`/engineering/course`, `live_html_course`) — each project's
  Course, its steps rendered in sequence, read-only.

Every card — an epic's own row, a work item under it, or a correction — is
`_card`, unchanged from before the split: the same status banner linking a
crew-drain outcome's PR or decision queue, the same claim banner, the same
resolve actions ("Won't do" the drain gate, plus Fixed / Duplicate /
Redirected for a manual close — a claim is taken by a working session through
the API, not a button here). Epics and Corrections both carry the "Log an
issue" manual-capture form (the /archives capture-form pattern) that POSTs to
the issue API as the viewer's own CF Access identity; Corrections also keeps
the recently-resolved strip the old single page had.

A claimed issue shows who holds it and when that hold expires (ARCH-361 Part
B). That is a reversal: ARCH-359 hid `claimed` as a worker-internal
concurrency primitive. It is now the coordination signal that keeps the
overnight drain off work a daytime session already has in hand, so it
belongs in the human view.

An item still carries its `type` (bug, nit, improvement, feature, task,
epic) — ADR-0060 makes a work item untyped but does not remove the field yet
(the review-escalation path still requires it), and `type == "epic"` is how
this module tells an epic from a work item, which ADR-0060 does not touch.

Rendered LIVE from the Library per request (server.py registers each
live_html in _full_only_routes, full-tier only), so every page is never
staler than the data. Resolve POSTs to the Library's issue API directly; the
browser carries the CF Access cookie + a same-origin Origin, so the write
authenticates as the human identity and passes the CSRF check. This module
only READS, through library_client; every mutation goes through the
Library's own authz-gated API. All agent/human free text is escaped on
render.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
import time
from html import escape
from urllib.parse import quote

import library_client
import model_catalog
import design_system as ds
from logutil import clean_log_field
from markdown_render import md_to_html
from url_safety import safe_http_url
from control_db import (
    ISSUE_ACTIVE, ISSUE_CLAIMED, ISSUE_OPEN, ISSUE_PR_OPEN,
    ISSUE_DEFAULT_NEEDS, ISSUE_NEEDS_OPERATOR,
    ISSUE_TERMINAL, ISSUE_TYPE_EPIC, prerequisite_met,
)
from models import utcnow
from paths import STATE_ROOT

SITE = STATE_ROOT / "site"

# --- the two lanes ---------------------------------------------------------------
# One queue held three kinds of work, and only one of them was what the queue is
# for. The issue queue is the inbox an overnight agent drains: atomic, findable,
# fixable in a night. One other kind kept landing in it because there was nowhere
# else to put it —
#
#   BLOCKERS — things only the operator can do (a console login, a key revocation, a
#   sandbox run). The server already excludes `needs=operator` from `claimable`,
#   so no drain ever touched these; they were correctly handled and invisible,
#   sitting in a count that read as agent backlog.
#
# A third lane, Backlog (feature-level work), was lifted out the same way and
# then retired (the operator, 2026-08-28): it was tied to structures the Chart House
# replaced. `type == feature` no longer routes anywhere special — a feature
# falls through to Issues like any other item, so nothing filed there is
# stranded by the retirement.
#
# LANE_OF IS THE ONLY PLACE THIS IS DECIDED. Every view derives from it, so the
# two pages PARTITION the active set: each issue appears on exactly one, and
# the counts add up to the total. Two views with two hand-written WHERE clauses
# would drift the first time a rule changed, and the failure is silent — an
# issue in no lane is invisible everywhere, and one in two lanes is
# double-counted. `test_the_lanes_partition_the_active_set` pins it.
LANE_ISSUES = "issues"
LANE_BLOCKERS = "blockers"

# The default for a caller that does not filter by viewer (most of this
# module's own tests, which exercise rendering rather than scoping) — every
# product, the same value the Library sends back for an administrator
# (authz.in_scope's own "*"). Every production caller (server.py) passes the
# viewer's Library-resolved scope explicitly instead of relying on this.
_ALL_PRODUCTS: frozenset[str] = frozenset({"*"})


def _in_scope(product: str, scope: frozenset[str]) -> bool:
    """Whether `product` is in the viewer's Library-resolved scope. "*" means
    every product — the Library's own answer for an administrator, never a
    flag this module branches on, matching kb_index._by_identity's rule."""
    return "*" in scope or product in scope


def _ref_in_scope(item_id, scope: frozenset[str]) -> bool:
    """Whether an item another item points at (its `found_during`, its
    `refs.duplicate_of`) is in the viewer's scope. Those references can name
    an item in any product, and the resolve API accepts a duplicate_of
    without a same-product check, so printing the id unchecked would confirm
    that an item exists in a project the viewer cannot see (review rounds 1
    and 2 on PR #372). An id the Library does not return reads as out of
    scope."""
    target = library_client.get_issue_item(str(item_id))
    return target is not None and _in_scope(target.product, scope)


def lane_of(item) -> str:
    """Which of the two queues an issue belongs to. Total over active issues."""
    if (item.needs or ISSUE_DEFAULT_NEEDS) == ISSUE_NEEDS_OPERATOR:
        return LANE_BLOCKERS
    return LANE_ISSUES


# --- the Issues lane's own split: Epics vs Corrections ----------------------
# The operator, 2026-08-29 (issue_01M1518ZFBGHAWC4Z46W893H2S): the flat Issues lane
# becomes two pages — Epics (an epic per project, expanding into its work
# items) and Corrections (the corrections queue per project, expanding into
# its work items) — with Blockers untouched above. This split is INSIDE
# LANE_ISSUES; it never reaches into LANE_BLOCKERS, which stays governed by
# `lane_of` alone.


def _is_correction(item, queue_ids: set[str]) -> bool:
    """A work item whose home is its product's corrections queue: its parent
    is null, or names a corrections queue (`queue_ids`, from
    `library_client.corrections_queue_ids`).

    The Library is moving every correction's parent from null to its
    product's queue id (library/specs/one-home-per-work-item). Both readings
    hold here so the page is right before, during and after that migration;
    the null branch can go once no row carries a null parent."""
    return item.type != ISSUE_TYPE_EPIC and (not item.parent_id or item.parent_id in queue_ids)


def _queue_parents(items, by_id) -> set[str]:
    """The corrections queues that `items` name as a parent. Only a parent
    that is not a known epic is looked up, so an epic's children cost no
    request."""
    unknown = {it.parent_id for it in items
               if it.type != ISSUE_TYPE_EPIC and it.parent_id
               and getattr(by_id.get(it.parent_id), "type", None) != ISSUE_TYPE_EPIC}
    return library_client.corrections_queue_ids(unknown)


def _epic_ids_to_read(all_items) -> list[str]:
    """The epics whose cross-product relations the Epics page reads: each
    active epic, and each epic an active item names as its parent. Those are
    the epics `_epic_and_correction_groups` can list, so a resolved epic with
    no active children is not read."""
    by_id = {it.id: it for it in all_items}
    lane = _active_in_lane(all_items, LANE_ISSUES)
    named = {it.id for it in lane if it.type == ISSUE_TYPE_EPIC}
    named |= {it.parent_id for it in lane if it.parent_id}
    return sorted(eid for eid in named
                  if eid in by_id and by_id[eid].type == ISSUE_TYPE_EPIC)


def _read_related_items(all_items, clock=time.monotonic) -> tuple[dict[str, list[str]], set[str]]:
    """Each epic's cross-product relation, as `(related, failed)`. `related`
    maps an epic id to the item ids the Library names for it; `failed` holds
    the epics whose read raised. A failed epic is left out of `related`, not
    mapped to `[]`, because an empty list reads as "nothing related".

    The reads are sequential, one per epic, and none starts once one client
    `TIMEOUT` has passed since the first. A read already started runs to its
    own timeout, so a stall costs at most two timeouts in all, where it
    would otherwise cost one per epic. Library reads have timed out under load
    (access.log, RENDER-FAILED issues.html 2026-08-19; serve.log's three
    course-detail timeouts in a row). An epic not read before the budget runs
    out counts as failed, so its list says unavailable."""
    related: dict[str, list[str]] = {}
    failed: set[str] = set()
    deadline = clock() + library_client.TIMEOUT
    for eid in _epic_ids_to_read(all_items):
        if clock() >= deadline:
            failed.add(eid)
            continue
        try:
            related[eid] = library_client.list_related_item_ids(eid)
        except Exception as ex:
            _log_swallowed(f"relation read for {eid}", ex)
            failed.add(eid)
    return related, failed


def _epic_and_correction_groups(all_items, related=None):
    """Split the Issues lane's active items into epic groups (an epic paired
    with its active children) and corrections (active work items whose
    parent is null or a corrections queue, `_is_correction`).

    `related` maps an epic id to the item ids related to it across products
    (`_read_related_items`). An active item in another product joins that
    epic's children, so it renders and counts under the epic. A related item
    in the epic's own product is left out, because a same-product item reaches
    an epic only through `parent_id`; one whose `parent_id` names this epic
    would otherwise be listed and counted twice.

    An epic is included whenever it is itself active OR has at least one
    active child still under it, so a work item whose epic has since
    resolved still renders under that epic rather than being silently
    dropped. A child whose `parent_id` names no item in `all_items` at all —
    a dangling reference — or names an item that is not itself an epic, has
    no epic to render under and falls back to Corrections instead of
    vanishing (the latter is not reachable under ADR-0060, which makes a
    work item's parent always an epic or the corrections queue, but the
    check costs nothing and the alternative is a non-epic item silently
    rendered as an epic's own section header); every live work item is
    reachable on some page is the property this whole change exists to
    hold.

    Returns `(epics, children_by_parent, corrections)`, every list carrying
    `_active_in_lane`'s own order (unworked before crew-drain outcomes,
    oldest-first within each) rather than a plain `created_at` re-sort, which
    would throw that ordering away. `LANE_OF IS THE ONLY PLACE` the Blockers
    split is decided (above); this is the only place the Epics/Corrections
    split is."""
    lane = _active_in_lane(all_items, LANE_ISSUES)
    by_id = {it.id: it for it in all_items}
    queue_ids = _queue_parents(lane, by_id)

    children_by_parent: dict[str, list] = {}
    corrections = []
    epics = []
    seen_epic_ids: set[str] = set()
    for it in lane:
        if it.type == ISSUE_TYPE_EPIC:
            epics.append(it)
            seen_epic_ids.add(it.id)
        elif _is_correction(it, queue_ids):
            corrections.append(it)
        else:
            children_by_parent.setdefault(it.parent_id, []).append(it)

    lane_by_id = {it.id: it for it in lane}
    lane_pos = {it.id: i for i, it in enumerate(lane)}
    for eid, rel_ids in (related or {}).items():
        joined = False
        for rid in rel_ids:
            item = lane_by_id.get(rid)
            if item is not None and item.product != by_id[eid].product:
                children_by_parent.setdefault(eid, []).append(item)
                joined = True
        if joined:
            # back into `lane`'s order, which the appends above broke
            children_by_parent[eid].sort(key=lambda it: lane_pos[it.id])

    # A child whose own epic has resolved (or is otherwise no longer in
    # `lane`) still needs an epic block to render under — appended after the
    # active epics above, in no particular order, since this is the rare
    # fallback case rather than the common path `lane`'s own order serves.
    for eid in list(children_by_parent):
        if eid in seen_epic_ids:
            continue
        epic = by_id.get(eid)
        if epic is None or epic.type != ISSUE_TYPE_EPIC:
            corrections.extend(children_by_parent.pop(eid))
            continue
        epics.append(epic)
        seen_epic_ids.add(eid)

    return epics, children_by_parent, corrections


def _project_section(product: str, inner_html: str, count: int) -> str:
    """One project's section — the per-project grouping every page composes
    with its own Epics/Corrections split, rather than replacing it."""
    return (f'<section class="proj-group" data-prod="{escape(product, quote=True)}">'
            f'<h2 class="proj-head">{escape(product)} <span class="n">{count}</span></h2>'
            f'{inner_html}'
            f'</section>')


# The label for items the backfill has not reached. Only vivy's queue carries
# topics today, so on every other project this is the whole group — it says
# "not recorded" rather than "other" because the difference between "this item
# belongs to no area" and "nobody has said which area yet" is the whole
# question a reader of a grouped queue is asking.
NO_TOPIC_LABEL = "no area recorded"


def _topic_groups(items: list) -> list[tuple[str, list]]:
    """`items` split by topic, as `[(label, items)]`.

    GROUPING SITS ABOVE THE LANE ORDER RATHER THAN REPLACING IT. `items`
    arrives in `_active_in_lane`'s order — unworked before the crew-drain
    outcomes, oldest-first within each — and each group keeps that order
    internally, because what is workable now is still what a reader wants to
    see first inside an area.

    Groups are ordered largest first, then by label, matching how this page
    already orders its project sections. The untopiced group sorts last
    whatever its size: on every project but vivy it holds everything, and
    leading with it would bury the areas that are the point of the view.

    An item carrying several topics appears under each of them, so a card is
    never hidden from an area it belongs to. No item carries more than one
    today (146 of 772 active items carry exactly one), so this is a rule for
    the shape of the field rather than a case anyone has hit — which is why
    the counts below are group sizes and the page's own total stays the item
    count."""
    grouped: dict[str, list] = {}
    for it in items:
        for topic in (it.topics or [NO_TOPIC_LABEL]):
            grouped.setdefault(topic, []).append(it)
    return sorted(grouped.items(),
                  key=lambda kv: (kv[0] == NO_TOPIC_LABEL, -len(kv[1]), kv[0]))


# `?sort=severity`'s ranking (issue_01M2P9385P23PMZCZFB1BZW9SN, the operator,
# 2026-09-16: "yeah let's just do severity sort for now" — no priority field
# on a work item, and none is to be built toward one). Anything not a key here
# — `None`, the literal string `"unset"` some rows carry, or any other value —
# sorts last via `.get(..., 3)` rather than being folded into `"low"`:
# inventing a severity would be the same mistake `policy_build._DECISION_PILL`
# avoids on its own read side, where an unrecognised value gets its own state
# rather than being read as a benign one.
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _severity_sorted(items: list) -> list:
    """`items`, worst severity first. `sorted` is stable, and no tiebreaker is
    added on top, so items sharing a severity keep their existing (lane)
    order."""
    return sorted(items, key=lambda it: _SEVERITY_RANK.get(it.severity, 3))


def _epic_block(epic: "library_client.IssueRecord", children: list, now,
                cross_product_unavailable: bool = False) -> tuple[str, int]:
    """One epic's card, plus its work items behind a closed `<details>`
    toggle — "any epic should expand into its work items" (the operator, 2026-08-29).
    Reuses `_card` unmodified for the epic and every child, so a change to
    how an item is displayed (badges, resolve actions, the thread) reaches
    every page that renders one instead of drifting between a queue card and
    a special "epic" rendering of its own.

    A child in another product than the epic is a cross-product relation and
    carries a "from <product>" marker. When its relation read failed,
    `cross_product_unavailable` says so in the summary, because a silent
    absence reads as "nothing related".

    Children are capped at `_QUEUE_LIMIT`, same as the old flat queue's own
    cap, for the pathological case of one epic holding far more than a page
    should render at once; the label states the true count and how many are
    not rendered, and the caller sums the returned hidden count into the
    page's own header so the true total is never read off a rendered count.
    Returns `(html, hidden)`."""
    n = len(children)
    rendered = children[:_QUEUE_LIMIT]
    hidden = n - len(rendered)
    trunc = f", showing {len(rendered)} — {hidden} more not rendered" if hidden else ""
    label = f"{n} work item{'' if n == 1 else 's'}{trunc}" if n else "No open work items"
    if cross_product_unavailable:
        label += " · cross-product list unavailable"
    children_html = "".join(
        _card(c, now) if c.product == epic.product else
        f'<div class="cross-product"><span class="badge xprod">from {escape(c.product)}</span>'
        f'{_card(c, now)}</div>'
        for c in rendered)
    html = (f'<div class="task-group">'
            f'{_card(epic, now)}'
            f'<details class="children-toggle"><summary>{escape(label)}</summary>'
            f'<div class="queue children">{children_html}</div></details>'
            f'</div>')
    return html, hidden


_QUEUE_LIMIT = 100
_RESOLVED_LIMIT = 12
_SEVERITIES = ("high", "medium", "low")
_RESOLUTION_LABELS = {"fixed": "Fixed", "wontfix": "Won't do", "duplicate": "Duplicate", "redirected": "Redirected"}


def _age(created, now) -> str:
    """A compact human age from a naive-UTC created_at to now."""
    secs = max(0, int((now - created).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h {mins % 60:02d}m"
    return f"{hrs // 24}d"


_KIND_LABEL = {"diagnosis": "Diagnosis", "question": "Question",
               "decline": "Declined", "note": "Note"}
# How many non-diagnosis comments to embed. The API caps a thread at 200 and each
# body may be ~100KB, so rendering all of them is a multi-megabyte page. EVERY
# diagnosis renders regardless of this — it is the contract, and truncating it to
# save bytes would defeat the point of the table.
_THREAD_RENDER_LIMIT = 12


# The detail page's own limit. A single-issue page is the one surface whose whole
# job is this thread, so eliding it there to save bytes defeats the page — but the
# API caps a thread at 200 comments of up to ~100KB each, so an unbounded render
# is still a multi-megabyte response. High enough that no real thread is clipped,
# bounded so a pathological one cannot be a denial of service against the viewer.
_THREAD_DETAIL_LIMIT = 200


def _thread(item, limit: int = _THREAD_RENDER_LIMIT) -> str:
    """The issue's thread (ARCH-373).

    The DIAGNOSIS leads regardless of when it was posted, because it is the
    contract bug-fixer consumes — burying it under three later notes would make
    the page agree with the API only by accident. Everything else stays
    chronological, which is what makes a back-and-forth readable.

    Bodies render as Markdown through the shared safe renderer, like issue
    bodies: an agent writes these, and a diagnosis is mostly code references.
    """
    comments = list(getattr(item, "comments", None) or [])
    if not comments:
        return ""
    diagnoses = [c for c in comments if c.kind == "diagnosis"]
    rest = [c for c in comments if c.kind != "diagnosis"]
    # Keep the MOST RECENT of the rest: an old clarifying question matters less
    # than the note someone left an hour ago.
    hidden = max(0, len(rest) - limit)
    rest = rest[-limit:] if hidden else rest
    rows = []
    if hidden:
        # Say what is missing rather than silently showing a partial thread —
        # a reader who cannot tell the difference will assume they saw it all.
        rows.append(f'<div class="cmt cmt-elided">{hidden} earlier '
                    f'{"comment" if hidden == 1 else "comments"} not shown</div>')
    for c in diagnoses + rest:
        label = _KIND_LABEL.get(c.kind, c.kind)
        rows.append(
            f'<div class="cmt cmt-{escape(c.kind, quote=True)}">'
            f'<div class="cmt-top"><span class="cmt-kind">{escape(label)}</span>'
            f'<span class="cmt-who">{escape(c.actor)}</span></div>'
            f'<div class="cmt-body">{md_to_html(c.body)}</div>'
            f'</div>')
    return f'<div class="thread">{"".join(rows)}</div>'


def _ref_chips(refs, scope: frozenset[str] = _ALL_PRODUCTS) -> str:
    """Render the refs dict as escaped chips; a value that is a clean http(s)
    URL links. `refs` is agent-written, so the link check is an allowlist
    (`url_safety.safe_http_url`), not a scheme prefix — `str.startswith` does
    not require an authority and survives an escaped control character, so a
    stored `"http://\\njavascript:alert(1)"` would otherwise pass through.

    `duplicate_of` names another item, which can be in any product, so its
    chip is dropped unless that item is in `scope` (`_ref_in_scope`)."""
    if not isinstance(refs, dict) or not refs:
        return ""
    chips = []
    for k, v in list(refs.items())[:8]:
        if k == "duplicate_of" and not _ref_in_scope(v, scope):
            continue
        key = escape(str(k))
        val = str(v)
        safe = safe_http_url(val)
        if safe is not None:
            chips.append(f'<a class="ref" href="{escape(safe, quote=True)}" '
                         f'target="_blank" rel="noopener">{key}</a>')
        else:
            chips.append(f'<span class="ref">{key}: {escape(val)}</span>')
    return '<div class="refs">' + "".join(chips) + "</div>"


def _claim_banner(item: library_client.IssueRecord, now) -> str:
    """Who is holding this issue, and until when.

    `claimed` used to be hidden here, on the ARCH-359 reasoning that it was an
    internal worker-concurrency primitive of no interest to a human. ARCH-361
    Part B changes that premise: a claim is now the coordination signal that
    stops the overnight drain from duplicating work a daytime session is already
    doing, so who holds an issue — and whether that hold has gone stale — is
    exactly what a human needs to see to trust the queue.

    An expired claim is called out rather than quietly ignored: it means a
    session died holding this, and the next drain will take it over.
    """
    if item.state != ISSUE_CLAIMED:
        return ""
    who = escape(item.claimed_by or "unknown")
    # Lease-aware and computed by the Library itself, from its own lease.py —
    # the item arrives with its own claim already resolved, so this reads it
    # rather than re-deriving it through a session of its own.
    if item.claim_expired:
        return ('<div class="outcome claim-stale"><span class="oc-label">Claim expired</span>'
                f'held by {who} · reclaimable by the next drain</div>')
    expiry = item.claim_expires_at
    left = _age(now, expiry) if expiry else ""
    tail = f" · frees in {escape(left)}" if left else ""
    return ('<div class="outcome claimed"><span class="oc-label">In flight</span>'
            f'{who}{tail}</div>')


def _outcome_banner(item: library_client.IssueRecord) -> str:
    """For a crew-drain outcome state, a status line with a link to where the issue
    went — the PR (pr-open). An open issue shows nothing here; a claimed one is
    covered by _claim_banner."""
    refs = item.refs or {}
    if item.state == ISSUE_PR_OPEN:
        url = str(refs.get("url") or "")
        pr = refs.get("pr")
        label = f"PR #{escape(str(pr))}" if pr is not None else "PR"
        safe = safe_http_url(url)
        link = (f'<a href="{escape(safe, quote=True)}" target="_blank" rel="noopener">{label}</a>'
                if safe is not None else label)
        return f'<div class="outcome pr-open"><span class="oc-label">PR open</span>{link}</div>'
    return ""


def _prerequisite_line(entry: dict, scope: frozenset[str] = _ALL_PRODUCTS) -> str:
    """One prerequisite, as a reader needs to see it.

    Three shapes, and the third is the one worth getting right. A `{text:}`
    entry is a condition somebody must judge. An `{item:}` entry names work that
    clears it automatically. An item entry the reader is not entitled to see
    arrives as `{"item": id, "missing": True}` with no state and no title —
    **shown as a blocker they cannot open, rather than hidden or rendered as an
    empty link.** Hiding it would say the item is blocked by nothing; an empty
    link would say the blocker is untitled. Both read as a defect in the page.

    An `{item:}` entry in a project outside the viewer's scope can name work in
    any product (platform/decisions/0074's own example is a Deck item waiting
    on a Bosun item), so it is shown as a blocker in a project the viewer
    cannot see, with no id and no title (review round 2 on PR #372). It still
    counts toward "blocked", so the item never reads as blocked by nothing.
    """
    if "text" in entry:
        met = bool(entry.get("met"))
        cls, label = ("met", "met") if met else ("unmet", "not yet")
        who = entry.get("judged_by")
        by = (f' <span class="pq-by">judged {escape(str(who))}</span>'
              if met and who else "")
        return (f'<li class="pq {cls}"><span class="pq-mark">{label}</span>'
                f'{escape(entry.get("text") or "")}{by}</li>')
    raw_id = str(entry.get("item") or "?")
    iid = escape(raw_id)
    if not entry.get("missing") and not _ref_in_scope(raw_id, scope):
        return ('<li class="pq unmet missing"><span class="pq-mark">blocked</span>'
                'Waiting on an item in a project you cannot see. '
                '<span class="pq-by">not visible to you</span></li>')
    if entry.get("missing"):
        return ('<li class="pq unmet missing"><span class="pq-mark">blocked</span>'
                f'Waiting on <code>{iid}</code>, which you cannot see. '
                '<span class="pq-by">not visible to you</span></li>')
    state = str(entry.get("state") or "?")
    # `title or raw_id` rather than `or iid`: falling back to the already-escaped
    # id would escape it twice and render `&amp;`-style artifacts if ids ever
    # stop being plain ULIDs.
    title = escape(str(entry.get("title") or raw_id))
    done = state in ISSUE_TERMINAL
    cls, label = ("met", "done") if done else ("unmet", state)
    # quote() then escape(), matching every other href built from an id in this
    # file — escape() alone neutralises the attribute break, but the convention
    # is what stays right if an id format ever carries a URL-special character.
    href = escape(_DETAIL_PREFIX + quote(raw_id, safe=""))
    return (f'<li class="pq {cls}"><span class="pq-mark">{escape(label)}</span>'
            f'Waiting on <a href="{href}">{title}</a></li>')


def _blocked_banner(item: library_client.IssueRecord,
                   scope: frozenset[str] = _ALL_PRODUCTS) -> str:
    """Why this item cannot proceed, listed rather than counted.

    Prerequisites replaced parking (platform/decisions/0074) because a blocker
    that lives in a state plus a prose condition is only legible to whatever
    reads that state. Rendering the list is what makes it
    legible to a person, so every entry is named, met ones included: a reader
    needs to see which conditions have cleared to judge how close the item is.
    """
    prereqs = item.prerequisites or []
    if not prereqs:
        return ""
    unmet = sum(1 for p in prereqs if not prerequisite_met(p))
    if not item.blocked:
        head = '<span class="oc-label ok">Unblocked</span>All prerequisites met'
    else:
        head = (f'<span class="oc-label">Blocked</span>'
                f'{unmet} of {len(prereqs)} '
                f'{"prerequisite" if len(prereqs) == 1 else "prerequisites"} not yet met')
    rows = "".join(_prerequisite_line(p, scope) for p in prereqs)
    return f'<div class="outcome blocked">{head}<ul class="pq-list">{rows}</ul></div>'


def _terminal_banner(item: library_client.IssueRecord,
                     scope: frozenset[str] = _ALL_PRODUCTS) -> str:
    """How a terminal issue ended, for the detail page.

    **The queue cannot show this and that is the point of the page.** The active
    queue holds only non-terminal issues and the resolved strip keeps the last
    twelve, so an issue resolved a month ago has no row anywhere — its id is still
    quoted in commit messages, PR bodies and other issues' text, and following it
    used to land on a 404. `_resolved_row`'s content, restated as a banner because
    a row belongs in a list and this page holds one issue.
    """
    if item.state not in ISSUE_TERMINAL:
        return ""
    label = _RESOLUTION_LABELS.get(item.resolution or "", item.resolution or "?")
    cls = {"fixed": "done", "wontfix": "wontfix", "duplicate": "duplicate",
           "redirected": "redirected"}.get(item.resolution or "", "done")
    bits = [escape(item.resolved_by or "?")]
    dupe_of = (item.refs or {}).get("duplicate_of") if isinstance(item.refs, dict) else None
    if dupe_of and _ref_in_scope(dupe_of, scope):
        # The original is a LINK now that an issue id resolves to a page. A
        # duplicate whose banner names an id the reader cannot open is the same
        # dead end this whole page exists to close.
        href = escape(f"{_DETAIL_PREFIX}{quote(str(dupe_of), safe='')}", quote=True)
        bits.append(f'of <a href="{href}"><code>{escape(str(dupe_of))}</code></a>')
    if (item.outcome_note or "").strip():
        bits.append(escape(item.outcome_note))
    return (f'<div class="outcome resolved-{cls}"><span class="oc-label">{escape(label)}</span>'
            f'{" · ".join(bits)}</div>')


def put_forward(item: library_client.IssueRecord) -> bool:
    """Whether anyone has put an epic forward for approval.

    **ONE RULE, READ BY BOTH SURFACES THAT OFFER APPROVE**, so the Chart House's
    Approve section and an epic's own action row cannot disagree about which
    epics can be approved. It lives here rather than in `chart_house_build`
    because that module already imports this one; the other direction would be
    a cycle.

    Two ways an epic qualifies, and why a strict `maturity == "proposed"` is
    not the test:

    - the crew marked it `proposed`, whatever it has written. A missing
      specification on one of those is an anomaly worth seeing: the Library
      refuses that approval, and the refusal is shown rather than hidden.
    - its maturity is unset but a specification exists. `POST /items` has
      never accepted `maturity`, so an epic is created with none and something
      later sets it; one whose specification was written without that step was
      invisible to the word-matching test, and approving is the only way
      anything now reaches `approved`. Of 93 non-terminal epics on 2026-09-21,
      18 had no maturity and two of those had a full specification.

    Callers check `type` and terminal state themselves; this answers only
    whether the epic has been put forward."""
    maturity = (item.maturity or "").strip()
    if maturity == "proposed":
        return True
    return not maturity and bool((item.specification or "").strip())


def _actions(item: library_client.IssueRecord) -> str:
    """The card's action row, which depends on what the item IS.

    **AN EPIC PUT FORWARD FOR APPROVAL GETS DECLINE AND APPROVE, AND NOTHING
    ELSE** (the operator, 2026-09-22: "the existing buttons shouldn't be there for
    proposed epics, just Decline, Approve"). "Fixed", "Duplicate" and
    "Redirected" describe work that has been done or moved; a proposal has had
    neither, so offering them invited an outcome that could not be true.

    - **Decline is the resolve route's `wontfix`**, relabelled for what the
      act means on a proposal. The Library has no separate decline route, and
      closing a proposal nobody will build is exactly what `wontfix` records.
      It opens the same outcome-note panel, so the reason travels with it.
    - **Approve is the operator's act**, on `approve_api`'s route — the same
      one the Chart House control calls, so there is one approval path rather
      than two. It is gated on `__CAN_APPROVE_EPIC__`, which the server fills
      from the viewer's role; a role without `epic_approve` sees Decline alone.
      Cosmetic, like every such token: `approve_api` refuses them regardless.

    - **Edit opens the epic's own editor** (the operator: "if it's possible to send it
      to the edit pane we should do that too"). It is a link to the epic's page
      with `#edit`, because the text lives there and not on the queue's card:
      from the queue it navigates, and on the epic's page `_SPEC_EDIT_JS`
      catches the click and opens the editor in place. Gated with Approve,
      since `epic.revise` is the same capability.

    Which epics qualify is `put_forward`, the rule the Chart House reads too.
    Every other item — a bug, a correction, an approved epic that is now real
    work — keeps the four outcomes it had."""
    if item.type == ISSUE_TYPE_EPIC and put_forward(item):
        edit_href = escape(f"{_DETAIL_PREFIX}{quote(item.id, safe='')}#edit", quote=True)
        return ('<div class="actions" data-can-approve="__CAN_APPROVE_EPIC__">'
                '<button class="btn wontfix" data-act="resolve" data-resolution="wontfix"'
                ' data-kind="decline">Decline</button>'
                f'<a class="btn edit" href="{edit_href}">Edit</a>'
                '<button class="btn approve" data-act="approve">Approve</button>'
                '<p class="approve-note" hidden></p>'
                '</div>')
    return ('<div class="actions">'
            '<button class="btn wontfix" data-act="resolve" data-resolution="wontfix">Won\'t do</button>'
            '<button class="btn fixed" data-act="resolve" data-resolution="fixed">Fixed</button>'
            '<button class="btn duplicate" data-act="resolve" data-resolution="duplicate">Duplicate</button>'
            '<button class="btn redirected" data-act="resolve" data-resolution="redirected">Redirected</button>'
            '</div>')


def _body_block(item: library_client.IssueRecord) -> str:
    """An item's body, collapsed under a closed "Background" disclosure when the
    item carries a specification or a definition of done.

    the operator, 2026-09-22: "can we somehow move the spec and definition of done to
    the top of every epic? All the narration that accompanies each epic is not
    useful and it takes up a ton of space." Those two fields are what an
    approval approves, so they lead; the body is how the epic came to be
    written, kept one click away rather than dropped. An item with neither
    field keeps its body open, because for a correction or a work item the
    body IS the content. Display only: the stored body is unchanged.
    """
    if not (item.body or "").strip():
        return ""
    body = f'<div class="body">{md_to_html(item.body)}</div>'
    if not ((item.specification or "").strip() or (item.definition_of_done or "").strip()):
        return body
    return f'<details class="background"><summary>Background</summary>{body}</details>'


def _card(item: library_client.IssueRecord, now, comment_count: int = 0,
          thread_limit: int = _THREAD_RENDER_LIMIT) -> str:
    """One active issue card (open / claimed / pr-open). Every
    dynamic value is escaped. The human action set is "Won't do" (the only gate
    the operator keeps — the crew auto-drains everything else), plus Fixed / Duplicate for
    a manual close; there is no "Claim" button, because a claim belongs to a
    working session rather than to a person reading the queue."""
    badges = [f'<span class="badge prod">{escape(item.product)}</span>']
    # A work item carries no type, so only an item that still has one (an epic,
    # or a closed item keeping the type it closed with) gets a type badge.
    if item.type:
        badges.append(f'<span class="badge type type-{escape(item.type, quote=True)}">'
                      f'{escape(item.type)}</span>')
    # Suppressed for `agent`: it is the default and the overwhelming case, so
    # badging every row is noise. What a reader needs to see is the row
    # NOTHING WILL PICK UP — an operator issue is
    # otherwise indistinguishable from drainable work, and finding it would take a
    # separate ?needs=operator query the queue should not require (reviewer, #118).
    needs = item.needs or ISSUE_DEFAULT_NEEDS
    if needs != ISSUE_DEFAULT_NEEDS:
        label = "needs you" if needs == ISSUE_NEEDS_OPERATOR else needs
        badges.append(f'<span class="badge needs needs-{escape(needs, quote=True)}">'
                      f'{escape(label)}</span>')
    if item.severity and item.severity != "unset":
        badges.append(f'<span class="badge sev sev-{escape(item.severity, quote=True)}">{escape(item.severity)}</span>')
    # **AN EPIC'S MATURITY BADGE, BECAUSE PROPOSED AND APPROVED MUST NEVER READ
    # ALIKE** (issue_01M32CB6A9T04NVYPARGDT2GWQ). Approval is now the operator's
    # own act and the thing that puts an epic on a Course, so "has the operator approved
    # this" is a fact about every epic on this queue — and until this badge the
    # queue card showed severity, type and needs but not maturity, so the two
    # were indistinguishable here while the detail page distinguished them.
    #
    # Epics only: `maturity` is an epic field, and badging every bug "maturity
    # not set" would be the noise the type and needs badges are suppressed to
    # avoid. An epic with the field UNSET is badged too, rather than skipped —
    # unset is not approved, and a silent absence is exactly the case that used
    # to read as approved.
    if item.type == ISSUE_TYPE_EPIC:
        maturity = (item.maturity or "").strip()
        badges.append(
            f'<span class="badge maturity maturity-{escape(maturity, quote=True)}">{escape(maturity)}</span>'
            if maturity else
            '<span class="badge maturity maturity-unset">maturity not set</span>')
    age = _age(item.created_at, now)
    # Markdown, not plain text. Issue bodies come from GitHub issue capture and
    # from agents pasting reviewer findings, both of which are GFM — so `**bold**`
    # and `code` were rendering literally, as asterisks and backticks, on the one
    # page meant to make a finding readable. md_to_html escapes raw HTML and drops
    # unsafe link schemes, so this does NOT relax the "all free text is escaped"
    # rule in the module docstring; it renders through a safe engine instead of
    # escaping wholesale.
    body = _body_block(item)
    meta_bits = [f'<span class="who">{escape(item.created_by)}</span>', f"opened {age} ago"]
    ph = escape("why it won't be done, or what fixed it (optional)", quote=True)
    return f'''
    <article class="card" data-prod="{escape(item.product, quote=True)}" data-id="{escape(item.id, quote=True)}" data-state="{escape(item.state, quote=True)}">
      <div class="card-top">
        {"".join(badges)}
        <span class="age">{escape(age)}</span>
      </div>
      <h2>{escape(item.title)}</h2>
      {_claim_banner(item, now)}
      {_outcome_banner(item)}
      {body}
      {_ref_chips(item.refs)}
      {_thread(item, limit=thread_limit)}
      <div class="meta">{" · ".join(meta_bits)}</div>
      {_actions(item)}
      <div class="directive">
        <div class="dupe-row">
          <label>Duplicate of — the issue this repeats</label>
          <input class="dupeid" type="text" placeholder="the original issue's id, e.g. issue_01K…" />
        </div>
        <label class="note-label">Outcome note — why it won't be done, or what fixed it (optional)</label>
        <textarea placeholder="{ph}"></textarea>
        <div class="send-row">
          <button class="send" data-act="send">Confirm</button>
          <button class="cancel" data-act="cancel">Cancel</button>
        </div>
      </div>
    </article>'''


def _resolved_row(item: library_client.IssueRecord,
                  scope: frozenset[str] = _ALL_PRODUCTS) -> str:
    """One row in the recently-resolved strip. A duplicate's original is
    named only when it is in the viewer's scope (`_ref_in_scope`)."""
    icon = {"fixed": "✓", "wontfix": "✕", "duplicate": "⧉", "redirected": "↪"}.get(item.resolution or "", "·")
    status_cls = {"fixed": "done", "wontfix": "wontfix", "duplicate": "duplicate", "redirected": "redirected"}.get(item.resolution or "", "done")
    label = _RESOLUTION_LABELS.get(item.resolution or "", item.resolution or "?")
    outcome = escape(item.outcome_note) if (item.outcome_note or "").strip() else "no note"
    # Surface the original on the row. A resolved-duplicate whose row says only
    # "Duplicate" tells the reader the one thing they already knew.
    dupe_of = (item.refs or {}).get("duplicate_of") if isinstance(item.refs, dict) else None
    dupe_bit = (f' · of <code>{escape(str(dupe_of))}</code>'
                if dupe_of and _ref_in_scope(dupe_of, scope) else "")
    sub = (f'<span class="verdict">{escape(label)}</span>{dupe_bit} · '
           f'{escape(item.resolved_by or "?")} · {outcome}')
    return f'''
    <div class="row">
      <div class="status {status_cls}">{icon}</div>
      <div class="row-main">
        <div class="row-title">{escape(item.title)}</div>
        <div class="row-sub">{sub}</div>
      </div>
      <span class="row-prod">{escape(item.product)}</span>
    </div>'''


def _chips(products, count: int) -> str:
    out = [f'<button class="chip" data-prod="all" aria-pressed="true">All '
           f'<span class="n">{count}</span></button>']
    for prod, n in products:
        out.append(f'<button class="chip" data-prod="{escape(prod, quote=True)}" '
                   f'aria-pressed="false">{escape(prod)} <span class="n">{n}</span></button>')
    return "".join(out)


def _log_issue_form(known_products: list[str]) -> str:
    """The "Log an issue" panel, mirroring /archives' "Log a signal" form. Visibility
    gates on data-can-log (the server substitutes __CAN_LOG_ISSUE__ from the
    viewer's role) — cosmetic; the POST /api/issues authz gate is the wall."""
    prods = "".join(f'<option value="{escape(p, quote=True)}">' for p in known_products)
    sev_opts = ('<option value="" disabled selected>— severity —</option>' +
                "".join(f'<option value="{s}">{s}</option>' for s in _SEVERITIES))
    return f'''
  <div class="capture" data-can-log="__CAN_LOG_ISSUE__">
    <button class="capture-toggle" id="logToggle" type="button" aria-expanded="false">＋ Log an issue</button>
    <form class="capture-form card" id="logForm" hidden>
      <div class="cf-row">
        <label>Product
          <input name="product" list="prodList" required maxlength="64" placeholder="constance">
          <datalist id="prodList">{prods}</datalist>
        </label>
        <label>Severity
          <select name="severity" required>{sev_opts}</select>
        </label>
      </div>
      <label class="cf-body">Title
        <input name="title" required maxlength="300" placeholder="what needs doing, in one line">
      </label>
      <label class="cf-body">Details <span class="opt">optional</span>
        <textarea name="body" rows="6" placeholder="repro steps or context, expected vs actual, links"></textarea>
      </label>
      <div class="cf-actions">
        <button type="submit" class="cf-submit">Log issue</button>
      </div>
      <div class="cf-note" id="logNote" hidden></div>
    </form>
  </div>'''


def _epics_page(sections_html: str, chips_html: str, count: int,
                known_products: list[str] | None = None, hidden: int = 0,
                comment_note: str = "", epics_trunc: str = "") -> str:
    """Assemble the Epics page through the shared Atelier shell (ARCH-355).
    __WHOAMI__ is substituted by the server with the viewer's identity label,
    as on every other full-tier page.

    `count` is the TRUE number of active epics, which may exceed how many get
    their own section — the page itself is capped at `_QUEUE_LIMIT` epics
    (`live_html`), and `epics_trunc` (built the same way `_epic_block` builds
    its own children's truncation clause) states that in the header when the
    cap was hit, empty otherwise. Each rendered epic's own work items are a
    second, independent cap behind a closed `<details>` toggle (`_epic_block`),
    each individually capped at _QUEUE_LIMIT with its own true-count-vs-rendered
    note. `hidden` is the sum of what every RENDERED epic block left
    unrendered plus one for every epic the page-level cap dropped entirely,
    carried on `.wrap[data-hidden]` for the client recount() to add back —
    same contract the old flat queue's single cap used, generalized to N
    per-epic caps instead of one. `comment_note` (`_comment_cap_note`) is the
    separate, page-level "some cards render without a thread" note — empty
    when the comment budget was never hit."""
    body = f'''<div class="ops">
  <header class="ops-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / engineering / epics</div>
    <h1>Epics</h1>
    <p class="lede"><span class="count" id="waitCount">{count}</span> active {"epic" if count == 1 else "epics"}{epics_trunc}, one section per project. Expand an epic for its work items. <a class="parked-link" href="/engineering/corrections">Corrections →</a></p>
    <div class="chips" id="chips">{chips_html}</div>
  </header>
  <div class="wrap" data-hidden="{hidden}">
    {_log_issue_form(known_products or [])}
    <!-- __CAN_LOG_ISSUE__ is substituted by the server (like __WHOAMI__) from the
         viewer's role: "false" hides the Log-an-issue form via CSS for a role
         lacking create. Cosmetic only — the create API's authz gate is the wall. -->
    <div class="queue" id="queue">{sections_html or '<div class="allclear">No open epics.</div>'}
      <div class="empty" id="empty">Nothing in this product.</div>
    </div>
    <div class="footnote">
      Signed in as <strong>__WHOAMI__</strong>. Each epic's own resolve buttons close the epic; the
      work items behind its toggle close independently, the same way, through
      <code>POST /api/issues/&lt;id&gt;/resolve</code>.{comment_note}
    </div>
  </div>
  <div class="toast" id="toast"></div>
</div>
<script>{_JS}</script>'''
    return ds.page_shell(
        title="Epics",
        nav_html=ds.nav_component("/engineering/issues", "full"),
        body=body,
        extra_css=_CSS + _GROUP_CSS,
    )


def _corrections_page(sections_html: str, chips_html: str, count: int,
                      known_products: list[str] | None = None,
                      resolved_html: str = "",
                      hidden: int = 0, comment_note: str = "") -> str:
    """Assemble the Corrections page — the corrections queue per project,
    adjacent to Epics (the operator, 2026-08-29). Same shell, same card/resolve
    machinery, own membership test (`_is_correction`).

    Carries the "Recently resolved" strip the old single Issues page had —
    global, not per-project, same as before — because Corrections is this
    page's closest descendant: an epic's own resolve history lives under its
    own expand toggle instead. `hidden` is the sum of every project's own
    per-queue cap (`live_html_corrections`), same `data-hidden` contract
    `_epics_page` uses. `comment_note` is that function's own page-level
    comment-budget note, same contract."""
    resolved_block = (
        '<div class="sec">Recently resolved</div>'
        '<div class="closed">' + resolved_html + '</div>'
    ) if resolved_html else ""
    body = f'''<div class="ops">
  <header class="ops-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / engineering / corrections</div>
    <h1>Corrections</h1>
    <p class="lede"><span class="count" id="waitCount">{count}</span> active {"correction" if count == 1 else "corrections"}, one queue per project. Expand a project for its work items. <a class="parked-link" href="/engineering/issues">Epics →</a></p>
    <div class="chips" id="chips">{chips_html}</div>
  </header>
  <div class="wrap" data-hidden="{hidden}">
    {_log_issue_form(known_products or [])}
    <div class="queue" id="queue">{sections_html or '<div class="allclear">No corrections queued.</div>'}
      <div class="empty" id="empty">Nothing in this product.</div>
    </div>
    {resolved_block}
    <div class="footnote">
      Signed in as <strong>__WHOAMI__</strong>. A work item lands here when its parent is its
      project's corrections queue, or when it has no parent, which is how the Library recorded a
      correction before it gave each project a queue.
      Your buttons map to <code>POST /api/issues/&lt;id&gt;/resolve</code>.{comment_note}
    </div>
  </div>
  <div class="toast" id="toast"></div>
</div>
<script>{_JS}</script>'''
    return ds.page_shell(
        title="Corrections",
        nav_html=ds.nav_component("/engineering/corrections", "full"),
        body=body,
        extra_css=_CSS + _GROUP_CSS,
    )


def _active_in_lane(all_items, lane: str):
    """Every active issue in `lane`, ordered exactly as the SQL read did:
    unworked (open/claimed) before the crew-drain outcome (pr-open),
    oldest-first within each so nothing starves.

    `lane_of` decides the lane in Python because the Library's `/items` route
    has no filter to express "one of these states, and this lane", so the
    whole table is fetched and filtered here instead of in a WHERE clause."""
    matching = [it for it in all_items
                if it.state in ISSUE_ACTIVE and lane_of(it) == lane]
    matching.sort(key=lambda it: (it.state == ISSUE_PR_OPEN, it.created_at))
    return matching


def _product_counts(items):
    """Per-product counts, matching the SQL `GROUP BY product` these lanes used
    to run — over the FULL matching set, never the page rendered from it, for
    the same reason `total_active` below is not `len(active)`."""
    counts: dict[str, int] = {}
    for it in items:
        counts[it.product] = counts.get(it.product, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _attach_comments(items) -> None:
    """Fill in each card's thread (ARCH-373) by fetching it, one item at a
    time.

    The Library's list route does not inline a thread on every row of a page —
    deliberately: a 200-row page would otherwise carry every comment in the
    queue, and the API already caps a single thread at 200 comments of up to
    100,000 characters each. Only the single-item route serves it. So this
    fetches it per rendered card rather than per row of the table — bounded by
    _QUEUE_LIMIT (the number of cards a page ever shows), not by how large the
    table is."""
    for it in items:
        fetched = library_client.get_issue_item(it.id)
        it.comments = fetched.comments if fetched is not None else []


def _attach_comments_within_budget(items, budget: int) -> tuple[int, int]:
    """`_attach_comments` for at most `budget` of `items`, returning `(spent,
    hidden)` — how much of the budget this call used, and how many of `items`
    got no thread because the budget ran out first (they still render as
    cards; `_thread` already degrades gracefully when `.comments` was never
    attached at all).

    The caller subtracts `spent` from its running budget before the next
    call, so a page with many epics/products spends ONE shared cap across all
    of them rather than reapplying the full cap at every call site — the
    per-epic/per-product amplification round 2 review found (issue
    01M1518ZFBGHAWC4Z46W893H2S): 51 epics each capped at 100 children is up to
    ~5100 potential Library round trips for comments alone, not the 100 total
    the old flat queue's single cap gave its comment fan-out."""
    take = items[:budget]
    _attach_comments(take)
    return len(take), len(items) - len(take)


def _comment_cap_note(fetched: int, total: int) -> str:
    """The same "truncation is visible, not silent" contract `_epic_block` and
    `live_html_corrections` already give card counts, for the page-level
    comment-fetch cap. Empty when the cap was never hit, matching those two
    callers' own "no note when nothing is hidden" convention."""
    hidden = total - fetched
    if not hidden:
        return ""
    return (f' Comment threads are fetched for the first {fetched} items on this '
            f'page load — {hidden} more render without one, so no page load costs '
            f'more than {_QUEUE_LIMIT} Library round trips for comments.')


def live_html(scope: frozenset[str] = _ALL_PRODUCTS) -> str:
    """Render the Epics page from the Library: an epic per project, each
    expanding into its active work items (the operator, 2026-08-29 —
    issue_01M1518ZFBGHAWC4Z46W893H2S). Full-tier only (server-gated).

    Visibility model (E19·2, issue_01M35K5AB4PRTJ71DMQW7Q4D3B): the page shows
    only the epics (and their children) of a project in `scope`, the viewer's
    Library-resolved project scope — an administrator's scope is simply the
    full project set the Library returns for them, so this filter makes no
    role branch of its own. `all_items` is filtered to scope before any
    grouping or counting runs, so every count, chip, project section and
    cross-product child derived from it is already scoped; `known_products`
    (the "Log an issue" datalist) and the per-product chips can therefore
    never name a project outside `scope`.

    Reuses `_card` unchanged for both an epic's own row and each child card,
    so the resolve actions, claim banner, thread and escaping stay exactly the
    queue's rather than a second rendering that could drift from the first.

    The number of epics RENDERED is capped at `_QUEUE_LIMIT`, same as every
    other list this module renders (an epic's own children, a project's
    corrections, the resolved strip) and same as the old flat queue's own
    single cap on its entire active set, epics included. Round-2 review
    (issue 01M1518ZFBGHAWC4Z46W893H2S) found this list was the one the split
    left uncapped: comments were bounded but `epics` itself was iterated in
    full, so an arbitrary number of epics — created through the standard
    issue-create API by any full-tier identity with create rights, not just
    the operator — made every page load build and serialize a section plus a
    full `_epic_block()` per epic with no cutoff. An epic past the cutoff is
    not listed here, the same way a child past `_epic_block`'s own cutoff
    isn't — either is still reachable at its own `_DETAIL_PREFIX + id` page
    for a reader who already holds the id (e.g. from a linked PR or a
    comment), it simply is not listed on this page.

    Comments are the part that amplifies further: fetched
    (`_attach_comments_within_budget`) against ONE page-level budget of
    `_QUEUE_LIMIT`, spent on the (already-capped) epics first and then on each
    epic's rendered children in section order, and never exceeded regardless
    of how many epics or children exist. This replaces a false claim this
    docstring used to make — that capping each epic's children at
    `_QUEUE_LIMIT` gave "the same bound the old flat queue's own single cap
    gave its comment fan-out". It did not: the old cap applied BEFORE any
    comment fetch, so it bounded the page at 100 Library round trips total; a
    per-epic cap multiplies by epic count instead of bounding the page
    (round-2 review, issue 01M1518ZFBGHAWC4Z46W893H2S: ~600 round trips at
    2026-08-30's 51 live epics / 584 work items). An item past the comment
    budget still renders as a card; it just has no thread, the same graceful
    degrade `_thread` gives an item whose comments were never fetched at
    all."""
    now = utcnow()
    all_items = library_client.list_issue_items()
    all_items = [it for it in all_items if _in_scope(it.product, scope)]
    related, related_failed = _read_related_items(all_items)
    epics, children_by_parent, _ = _epic_and_correction_groups(all_items, related)

    total_epics = len(epics)
    epics = epics[:_QUEUE_LIMIT]
    epics_hidden = total_epics - len(epics)
    epics_trunc = (f", showing {len(epics)} — {epics_hidden} more not rendered"
                  if epics_hidden else "")

    comment_budget = _QUEUE_LIMIT
    comment_total = len(epics)
    spent, comment_hidden = _attach_comments_within_budget(epics, comment_budget)
    comment_budget -= spent

    by_product: dict[str, list] = {}
    for ep in epics:
        by_product.setdefault(ep.product, []).append(ep)
    products = sorted(((p, len(v)) for p, v in by_product.items()),
                       key=lambda kv: (-kv[1], kv[0]))

    sections = []
    total_hidden = epics_hidden
    for prod, n in products:
        blocks = []
        for ep in by_product[prod]:
            children = children_by_parent.get(ep.id, [])
            rendered_children = children[:_QUEUE_LIMIT]
            comment_total += len(rendered_children)
            spent, hidden = _attach_comments_within_budget(rendered_children, comment_budget)
            comment_budget -= spent
            comment_hidden += hidden
            html, epic_hidden = _epic_block(
                ep, children, now, cross_product_unavailable=ep.id in related_failed)
            blocks.append(html)
            total_hidden += epic_hidden
        sections.append(_project_section(prod, "".join(blocks), n))

    known_products = sorted({it.product for it in all_items})
    chips = _chips(products, len(epics))
    return _epics_page("".join(sections), chips, total_epics, known_products,
                       hidden=total_hidden, epics_trunc=epics_trunc,
                       comment_note=_comment_cap_note(comment_total - comment_hidden, comment_total))


def live_html_corrections(query: dict | None = None,
                          scope: frozenset[str] = _ALL_PRODUCTS) -> str:
    """Render the Corrections page from the Library: the corrections queue
    per project, each expanding into its active work items (the operator, 2026-08-29 —
    issue_01M1518ZFBGHAWC4Z46W893H2S). Full-tier only (server-gated).

    `scope` is the viewer's Library-resolved project scope (E19·2,
    issue_01M35K5AB4PRTJ71DMQW7Q4D3B) — applied to `all_items` before any
    grouping, sort or count, the same `live_html` filters by, so a project
    outside it has no section, chip or datalist entry on this page either.

    `?sort=topic` groups each project's items by area (`_topic_groups`).
    `?sort=severity` (issue_01M2P9385P23PMZCZFB1BZW9SN) reorders each
    project's items worst-first (`_severity_sorted`), applied to the FULL
    per-project list before the `_QUEUE_LIMIT` cap runs — see the comment in
    the loop below for why that order is the opposite of `?sort=topic`'s.
    Anything else, including no query at all, renders the flat lane-ordered
    queue this page has always shown. Neither sort is a different fetch:
    `IssueRecord` has carried `topics` and `severity` since those fields
    existed, so nothing here asks the Library to sort. The `sort` parameter
    `GET /items` grew is for a caller that pages; this page reads the whole
    table and orders it in Python, as it always has.

    Membership is `_is_correction`: a work item whose parent is null or its
    project's corrections queue.

    Card rendering stays capped per project at `_QUEUE_LIMIT`, unchanged.
    Comments are fetched (`_attach_comments_within_budget`) against ONE
    page-level budget of `_QUEUE_LIMIT`, spent across every project's rendered
    cards in the same order they render — not a fresh `_QUEUE_LIMIT` allowance
    per project, which is the same per-caller multiplication `live_html`'s
    docstring documents fixing for Epics (round-2 review, issue
    01M1518ZFBGHAWC4Z46W893H2S)."""
    sort = (query or {}).get("sort")
    by_topic = sort == "topic"
    by_severity = sort == "severity"
    now = utcnow()
    all_items = library_client.list_issue_items()
    all_items = [it for it in all_items if _in_scope(it.product, scope)]
    _, _, corrections = _epic_and_correction_groups(all_items)

    by_product: dict[str, list] = {}
    for it in corrections:
        by_product.setdefault(it.product, []).append(it)
    products = sorted(((p, len(v)) for p, v in by_product.items()),
                       key=lambda kv: (-kv[1], kv[0]))

    comment_budget = _QUEUE_LIMIT
    comment_total = 0
    comment_hidden = 0

    sections = []
    total_hidden = 0
    for prod, n in products:
        items = by_product[prod]
        # `?sort=severity` reorders the WHOLE per-project list here, before
        # the cap below runs — the opposite order from `?sort=topic`'s
        # cap-then-group two lines down, and deliberately so. Measured against
        # the live Library (issue_01M2P9385P23PMZCZFB1BZW9SN): Bosun holds 178
        # corrections against a `_QUEUE_LIMIT` of 100. Capping in lane order
        # first and sorting only the 100 survivors would let a high-severity
        # item sitting at position 140 never render on a page whose whole
        # promise is worst-first — indistinguishable from a working page to
        # anyone who can't see the items being hidden.
        if by_severity:
            items = _severity_sorted(items)
        rendered_items = items[:_QUEUE_LIMIT]
        comment_total += len(rendered_items)
        spent, hidden_comments = _attach_comments_within_budget(rendered_items, comment_budget)
        comment_budget -= spent
        comment_hidden += hidden_comments
        hidden = n - len(rendered_items)
        total_hidden += hidden
        trunc = f", showing {len(rendered_items)} — {hidden} more not rendered" if hidden else ""
        label = f"{n} work item{'' if n == 1 else 's'}{trunc}"
        # For `?sort=topic`, the cap is applied BEFORE grouping (above), so the
        # grouped view renders exactly the cards the flat view renders and no
        # more — the same items, rearranged. Capping per group instead would
        # quietly raise the page's card count with the number of areas, and
        # the comment budget below is spent against this same set.
        if by_topic:
            inner = "".join(
                f'<h3 class="topic-head">{escape(topic)} '
                f'<span class="n">{len(group)}</span></h3>'
                f'<div class="queue">{"".join(_card(it, now) for it in group)}</div>'
                for topic, group in _topic_groups(rendered_items))
            body = f'<div class="children by-topic">{inner}</div>'
        else:
            # Byte-identical to what this page has always emitted: one
            # `queue children` div, not a nested pair. The grouped view needs
            # the extra level for its headings; the flat view must not pay for
            # it, since `.children` and `.queue` both carry layout.
            body = (f'<div class="queue children">'
                    f'{"".join(_card(it, now) for it in rendered_items)}</div>')
        block = (f'<details class="children-toggle"><summary>{escape(label)}</summary>'
                 f'{body}</details>')
        sections.append(_project_section(prod, block, n))

    resolved = sorted(
        (it for it in all_items if it.state in ISSUE_TERMINAL),
        # resolved_at descending, NULLS LAST — same ordering `live_html` used
        # to carry, kept here as the closest descendant of the old flat page.
        key=lambda it: (it.resolved_at or dt.datetime.min, it.created_at),
        reverse=True)[:_RESOLVED_LIMIT]
    resolved_html = "".join(_resolved_row(it, scope) for it in resolved)

    known_products = sorted({it.product for it in all_items})
    chips = _chips(products, len(corrections))
    return _corrections_page("".join(sections), chips, len(corrections), known_products,
                             resolved_html=resolved_html,
                             hidden=total_hidden,
                             comment_note=_comment_cap_note(comment_total - comment_hidden, comment_total))


_DETAIL_PREFIX = "/engineering/issues/"


def _detail_page(item: library_client.IssueRecord, now,
                 scope: frozenset[str] = _ALL_PRODUCTS) -> str:
    """One issue's page, assembled through the shared Atelier shell.

    **The card is the queue's card, not a second rendering of one.** It carries
    the same markup, the same data attributes and the same resolve JS, so there is
    one resolve path rather than two that drift. What the page adds around it is
    what a list cannot hold: the whole thread rather than its last twelve, and a
    banner for an issue the queue has stopped showing at all.

    `scope` reaches the two found-during pieces: the "found during" link in the
    meta line and `_found_during_section`. A found-during link joins items
    across products (that cross-product case is the feature), so those are
    the pieces of this page that can name an item in a project outside the
    viewer's scope.
    """
    badges = [f'<span class="badge prod">{escape(item.product)}</span>']
    if item.type:
        badges.append(f'<span class="badge type">{escape(item.type)}</span>')
    if item.severity and item.severity != "unset":
        badges.append(f'<span class="badge sev sev-{escape(item.severity, quote=True)}">'
                      f'{escape(item.severity)}</span>')
    needs = item.needs or ISSUE_DEFAULT_NEEDS
    if needs != ISSUE_DEFAULT_NEEDS:
        label = "needs you" if needs == ISSUE_NEEDS_OPERATOR else needs
        badges.append(f'<span class="badge needs needs-{escape(needs, quote=True)}">'
                      f'{escape(label)}</span>')
    meta_bits = [f'<span class="who">{escape(item.created_by)}</span>',
                 f"opened {_age(item.created_at, now)} ago"]
    if item.run_id:
        meta_bits.append(f"run {escape(item.run_id)}")
    # The parent, as a link. An epic's children each name it and none of them
    # could open it until this page existed. A corrections queue is not an
    # item, so its id would link to a 404; it links to the Corrections page.
    if item.parent_id and library_client.corrections_queue_ids({item.parent_id}):
        meta_bits.append('in <a href="/engineering/corrections">Corrections</a>')
    elif item.parent_id:
        href = escape(f"{_DETAIL_PREFIX}{quote(str(item.parent_id), safe='')}", quote=True)
        meta_bits.append(f'under <a href="{href}">{escape(item.parent_id)}</a>')
    # `found_during` can name an epic in another product, so the link is shown
    # only when that epic is in the viewer's scope (`_ref_in_scope`).
    if item.found_during and _ref_in_scope(item.found_during, scope):
        href = escape(f"{_DETAIL_PREFIX}{quote(str(item.found_during), safe='')}", quote=True)
        meta_bits.append(f'found during <a href="{href}">{escape(item.found_during)}</a>')

    # THE ACTIONS ARE THE QUEUE'S, and only for an issue the queue would show
    # them for. Offering "Won't do" on an already-resolved issue would post a
    # second resolution over the first and report success.
    body = f'''<div class="ops ops-detail">
  <header class="ops-head">
    <div class="eyebrow-line"><span class="dot"></span><a class="back" href="/engineering/issues">← issues</a></div>
    <div class="detail-badges">{"".join(badges)}</div>
    <h1>{escape(item.title)}</h1>
    <div class="meta">{" · ".join(meta_bits)}</div>
  </header>
  <div class="wrap">
    {_model_section(item, now)}
    {_spec_section(item)}
    {_reviews_section(item)}
    {_found_during_section(item, scope)}
    <div class="queue" id="queue">
      {_blocked_banner(item, scope)}
      {_terminal_banner(item, scope)}
      {_card(item, now, thread_limit=_THREAD_DETAIL_LIMIT)
       if item.state in ISSUE_ACTIVE else _frozen_card(item, now, scope)}
    </div>
    <div class="footnote">
      Signed in as <strong>__WHOAMI__</strong>. This is the URL every issue already
      carries as its own — <code>{escape(_DETAIL_PREFIX + item.id)}</code> — so an
      id quoted in a commit message, a PR body or another issue resolves to the
      thing it names.
    </div>
  </div>
  <div class="toast" id="toast"></div>
</div>
<script>{_JS}</script>
<script>{_SPEC_EDIT_JS}</script>
<script>{_MODEL_JS}</script>'''
    return ds.page_shell(
        title=item.title,
        nav_html=ds.nav_component("/engineering/issues", "full"),
        body=body,
        extra_css=_CSS + _DETAIL_CSS,
    )


def _found_during_section(item: library_client.IssueRecord,
                          scope: frozenset[str] = _ALL_PRODUCTS) -> str:
    """On an epic's page, the items whose `found_during` names it, open ones
    first. They stay in the corrections lane, so this is the only place they
    are shown against the epic. Absent on a non-epic and on an epic with none.

    A found-during item is filed against whatever product it was actually
    found in, not necessarily the epic's own — that cross-product case is the
    feature — so this is filtered to `scope` on its own rather than inheriting
    the epic's already-checked visibility; an epic in scope must not leak the
    title of a found-during item in a project the viewer cannot see."""
    if item.type != ISSUE_TYPE_EPIC:
        return ""
    found = [it for it in library_client.list_found_during(item.id)
             if _in_scope(it.product, scope)]
    if not found:
        return ""
    found.sort(key=lambda it: (it.state not in ISSUE_ACTIVE, it.created_at))
    rows = []
    for it in found:
        href = escape(f"{_DETAIL_PREFIX}{quote(it.id, safe='')}", quote=True)
        rows.append(f'<li><a href="{href}">{escape(it.title)}</a> '
                    f'<span class="fd-state">{escape(it.state)}</span></li>')
    label = f'<div class="spec-label">Found during this epic · {len(found)}</div>'
    return (f'<section class="spec-section found-during-section">{label}'
            f'<ul class="fd-list">{"".join(rows)}</ul></section>')


_GITHUB_PR_URL = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)(?:[/?#].*)?$")


def _linked_pr(item: library_client.IssueRecord) -> tuple[str, int] | None:
    """The `github:<owner>/<repo>` identity and number of the pull request this
    item links, read from `refs["url"]`. A ref carrying only a bare `repo`
    (`www`) names no owner, so it has no identity to look reviews up under."""
    url = (item.refs or {}).get("url") if isinstance(item.refs, dict) else None
    m = _GITHUB_PR_URL.match(str(url or ""))
    if m is None:
        return None
    return f"github:{m.group(1)}/{m.group(2)}", int(m.group(3))


def _finding_html(finding) -> str:
    """One finding. The Library takes any object here and no writer sends one
    yet, so each field renders as text rather than against a guessed schema."""
    if not isinstance(finding, dict):
        return f'<li>{md_to_html(str(finding))}</li>'
    lines = "\n".join(f"- **{k}:** {v}" for k, v in finding.items())
    return f'<li>{md_to_html(lines)}</li>'


def _reviews_section(item: library_client.IssueRecord) -> str:
    """The linked pull request's reviews, read live from the Library's review
    record (issue_01M38316270TMDKT5MQSAANEM3), rather than from the GitHub
    comments `github_capture.py` records as signals.

    One entry per role per reviewed commit, newest first; the route serves
    newest first, so the first record seen for a (role, commit) pair is the one
    kept. A record whose commit is not the newest record's commit is marked
    stale, and an `unverified` record is marked as not counting toward a merge
    rather than hidden. Absent for an item that links no GitHub pull request.
    """
    linked = _linked_pr(item)
    if linked is None:
        return ""
    repo_identity, pr = linked
    records = library_client.list_review_records(repo_identity, pr)
    label = f'<div class="spec-label">Reviews · PR #{pr}</div>'
    if not records:
        return (f'<section class="spec-section reviews-section">{label}'
                '<p class="rv-none">The Library holds no review record for this pull '
                'request.</p></section>')
    latest = records[0].reviewed_ref
    seen: set[tuple[str, str]] = set()
    entries = []
    for rec in records:
        if (rec.role, rec.reviewed_ref) in seen:
            continue
        seen.add((rec.role, rec.reviewed_ref))
        verdict_cls = {"APPROVE": "ok", "REQUEST_CHANGES": "bad"}.get(rec.verdict, "other")
        marks = []
        if rec.reviewed_ref != latest:
            marks.append(f'<span class="rv-mark rv-stale">stale: the newest review is of '
                         f'{escape(latest[:9])}</span>')
        if rec.auth != "run-verified":
            why = f" ({escape(rec.auth_reason)})" if rec.auth_reason else ""
            marks.append(f'<span class="rv-mark rv-unverified">{escape(rec.auth)}{why}: '
                         'does not count toward a merge</span>')
        run = f' · run {escape(rec.run_id)}' if rec.run_id else ""
        findings = (f'<ul class="rv-findings">{"".join(_finding_html(f) for f in rec.findings)}</ul>'
                    if rec.findings else "")
        entries.append(
            f'<div class="rv" data-auth="{escape(rec.auth, quote=True)}">'
            f'<div class="rv-head"><span class="rv-role">{escape(rec.role)}</span>'
            f'<span class="rv-verdict rv-{verdict_cls}">{escape(rec.verdict)}</span>'
            f'<span class="rv-meta">reviewed {escape(rec.reviewed_ref[:9])}{run}</span></div>'
            f'{"".join(marks)}'
            f'<div class="rv-body">{md_to_html(rec.prose)}</div>{findings}</div>')
    return f'<section class="spec-section reviews-section">{label}{"".join(entries)}</section>'


def _spec_section(item: library_client.IssueRecord) -> str:
    """An epic's specification, definition of done and maturity — the detail
    page only, never the queue's card (issue_01M123PF05MFZPY2Q07XGB1CS0). A
    bug never carries these, so the section is absent entirely unless at
    least one field does — no empty heading for the common case.

    Both fields render through `md_to_html`, the same real CommonMark parser
    `body` uses, rather than a hand-written filter for lines starting with
    "- ". A `definition_of_done` line that is not a bullet still becomes a
    paragraph instead of vanishing — the trap Deck's own generator hit, where
    filtering for "- " silently dropped any non-bullet line with no trace.
    Approving an epic is approving this text, so nothing here may be lost
    silently.
    """
    spec = (item.specification or "").strip()
    dod = (item.definition_of_done or "").strip()
    maturity = (item.maturity or "").strip()
    if not spec and not dod and not maturity:
        return ""
    # Shown next to the fields it qualifies, not lost among the generic issue
    # badges above — a specification with no visible maturity cannot be told
    # from an approved one, which is the gap this page exists to close.
    maturity_badge = (
        f'<span class="badge maturity maturity-{escape(maturity, quote=True)}">{escape(maturity)}</span>'
        if maturity else
        '<span class="badge maturity maturity-unset">maturity not set</span>')
    editable = (item.type == ISSUE_TYPE_EPIC and item.state in ISSUE_ACTIVE
                and put_forward(item))
    open_btn = ('<button type="button" class="spec-edit-open">Edit</button>'
                if editable else "")
    view = []
    if spec:
        view.append('<div class="spec-label">Specification</div>')
        view.append(f'<div class="spec-body">{md_to_html(item.specification)}</div>')
    if dod:
        view.append('<div class="spec-label">Definition of done</div>')
        view.append(f'<div class="dod-body">{md_to_html(item.definition_of_done)}</div>')
    parts = [f'<div class="spec-head">{open_btn}{maturity_badge}</div>',
             '<div class="spec-view">' + "".join(view) + '</div>']
    if not editable:
        return '<section class="spec-section">' + "".join(parts) + '</section>'
    # The editor carries the RAW text, escaped for a textarea, so what is edited
    # is the source the Library stores rather than the rendered HTML. Hidden
    # until opened; `_SPEC_EDIT_JS` drives it.
    parts.append(
        '<div class="spec-edit" hidden>'
        '<label class="spec-label" for="spec-edit-spec">Specification</label>'
        '<textarea id="spec-edit-spec" class="spec-edit-spec" rows="12">'
        f'{escape(item.specification or "")}</textarea>'
        '<label class="spec-label" for="spec-edit-dod">Definition of done</label>'
        '<textarea id="spec-edit-dod" class="spec-edit-dod" rows="10">'
        f'{escape(item.definition_of_done or "")}</textarea>'
        '<div class="spec-edit-actions">'
        '<button type="button" class="btn approve spec-edit-save">Save</button>'
        '<button type="button" class="btn spec-edit-cancel">Cancel</button>'
        '</div>'
        '<p class="approve-note spec-edit-note" hidden></p>'
        '</div>')
    return (f'<section class="spec-section" data-edit="1" data-id="{escape(item.id, quote=True)}"'
            ' data-can-approve="__CAN_APPROVE_EPIC__">' + "".join(parts) + '</section>')


def is_queued(item: library_client.IssueRecord) -> bool:
    """Whether nothing is working `item` yet: `open`, or claimed with the claim
    expired. This is the window the model override acts in (the operator, 2026-09-23:
    the button "changes the model on work already queued"), since the Bosun
    reads the choice when a dispatch takes the item and a run already under
    way keeps the model it started on."""
    return item.state == ISSUE_OPEN or (item.state == ISSUE_CLAIMED and item.claim_expired)


def model_choice_dict(choice: library_client.ModelChoice | None) -> dict | None:
    if choice is None:
        return None
    return {"model": choice.model, "reason": choice.reason,
            "set_by_client": choice.set_by_client, "acting_human": choice.acting_human,
            "set_at": choice.set_at.strftime("%Y-%m-%dT%H:%M:%SZ") if choice.set_at else None}


def _model_section(item: library_client.IssueRecord, now) -> str:
    """The operator's model choice, shown as a section of its own at the top of
    the page rather than as a badge, because it changes what the fleet runs this
    item on (the operator, 2026-09-23: "loudly"). A queued item also gets the control
    that sets or clears it, shown only to a role holding `item_model` through
    `__CAN_SET_ITEM_MODEL__` — cosmetic, since model_override_api and the
    Library's `item.model` are the wall.

    Absent entirely for an item with no choice that is not queued, so a
    finished item does not grow an empty heading."""
    choice = item.model_choice
    queued = is_queued(item)
    if choice is None and not queued:
        return ""
    if choice is not None:
        when = f" · {_age(choice.set_at, now)} ago" if choice.set_at else ""
        reason = (f'<p class="model-reason">{escape(choice.reason)}</p>'
                  if choice.reason else "")
        # The client is the Library's attestation; the person is what that
        # client reported, which the Library does not verify.
        shown = (f'<div class="model-now"><code class="model-name">{escape(choice.model)}</code></div>'
                 f'<div class="model-by">chosen by {escape(choice.acting_human)}'
                 f' through {escape(choice.set_by_client)}{when}</div>{reason}')
    else:
        shown = ('<div class="model-by">No model chosen. A dispatch runs this item '
                 "on its agent's roster model.</div>")
    control = ""
    if queued:
        clear = ('<button type="button" class="btn model-clear">Clear</button>'
                 if choice is not None else "")
        # Only the models agentctl dispatches, in its order, valued by the
        # resolved id (issue_01M3NWDHCDQK6PE9NEXFQ37G9K). With no list there is
        # nothing to choose from, so the page says so and offers only Clear.
        catalog = model_catalog.load()
        if catalog:
            options = "".join(
                f'<option value="{escape(m.model, quote=True)}">'
                f'{escape(f"{m.alias} — {m.model}" if m.alias else m.model)}</option>'
                for m in catalog)
            picker = ('<select class="model-select" aria-label="Model">'
                      f'<option value="">Choose a model</option>{options}</select>')
            set_btn = '<button type="button" class="btn approve model-set">Set model</button>'
        else:
            picker = ('<p class="model-unavailable">The model list is unavailable: '
                      '<code>agentctl models</code> gave no usable answer, so a model '
                      'cannot be set until it does.</p>')
            set_btn = ""
        actions = (
            '<input type="text" class="model-reason-input" maxlength="300" autocomplete="off"'
            ' placeholder="why (optional)" aria-label="Reason">'
            f'<div class="model-actions">{set_btn}{clear}</div>'
            '<p class="approve-note model-note" hidden></p>') if set_btn or clear else ""
        control = f'<div class="model-edit">{picker}{actions}</div>'
    return (f'<section class="model-section" data-id="{escape(item.id, quote=True)}"'
            ' data-can-set="__CAN_SET_ITEM_MODEL__">'
            '<div class="spec-label">Model override</div>'
            f'{shown}{control}</section>')


def _frozen_card(item: library_client.IssueRecord, now,
                 scope: frozenset[str] = _ALL_PRODUCTS) -> str:
    """A terminal issue: everything the card shows, and no buttons.

    Built rather than reusing `_card` with the actions stripped, because the
    stripping is the load-bearing part. A resolved issue still carrying a "Won't
    do" button posts a second resolution over the first and the API answers
    success, so the page would report an outcome that overwrote the real one.
    """
    body = _body_block(item)
    return f'''
    <article class="card frozen" data-prod="{escape(item.product, quote=True)}" data-id="{escape(item.id, quote=True)}" data-state="{escape(item.state, quote=True)}">
      {body}
      {_ref_chips(item.refs, scope)}
      {_thread(item, limit=_THREAD_DETAIL_LIMIT)}
    </article>'''


def detail_html(item_id: str, scope: frozenset[str] = _ALL_PRODUCTS):
    """Render one issue's page, or None when there is no such issue — or
    when `item_id` names one whose product is outside `scope`, the viewer's
    Library-resolved project scope (E19·2, issue_01M35K5AB4PRTJ71DMQW7Q4D3B).
    Both answer None identically, and server.py turns None into the same 404
    either way, so an id outside scope reads exactly like one that does not
    exist — it must not confirm the id is real.

    **A terminal issue renders**, which is most of the point. The active queue
    holds none, and the resolved strip keeps twelve — so the ids that most need
    a page are exactly the ones no list will ever show again.
    """
    now = utcnow()
    item = library_client.get_issue_item(item_id)
    if item is None or not _in_scope(item.product, scope):
        return None
    return _detail_page(item, now, scope)


def _log_swallowed(what: str, ex: BaseException) -> None:
    """Print a swallowed exception to stderr, never raising doing it — the
    same discipline `chart_house_build._log_swallowed` uses for its own
    per-course degrade.

    stderr rather than a logger, because this repo has no logging setup on
    the render path — launchd captures it into the serve log. Exception text
    can be shaped by a malformed course/issue id, so it goes through
    `logutil.clean_log_field`, the same control-character rule
    `server.py._log_field` and `chart_house_build._log_swallowed` apply."""
    try:
        print(clean_log_field(f"issue_build: {what} — {type(ex).__name__}: {ex}"),
              file=sys.stderr)
    except Exception:
        pass


def course_anchor_href(product: str) -> str:
    """Where a product's Course review lives — `/engineering/course`, scrolled
    to that product's own section (the `course-<product>` id `_course_section`
    sets). Every "awaits your sign-off"-style chip elsewhere on the site
    (`chart_house_build`'s row chip, counts line and fleet-signals line) calls
    this rather than re-deriving the anchor, so the scheme has one owner
    (issue_01M17W7DRZ3FJNRNYCAQFWHS1F) — full-tier only, matching both this
    route and every page that links to it."""
    return f"/engineering/course#course-{quote(product, safe='')}"


def _course_entry_row(entry: "library_client.CourseEntry") -> str:
    """One step in a Course's sequence, as a link to its issue. Rendered in
    the order `entries` already carries — the one thing the Course object
    has that its issues do not, so nothing here re-sorts or groups it."""
    cls = "done" if entry.state in ISSUE_TERMINAL else (entry.maturity or "unspecified")
    title = escape(entry.title or entry.issue_id)
    href = escape(f"{_DETAIL_PREFIX}{quote(entry.issue_id, safe='')}", quote=True)
    return (f'<a class="course-step step-{escape(cls, quote=True)}" href="{href}">'
            f'{title}</a>')


def _course_section(course: "library_client.CourseRecord") -> str:
    """One project's Course, its steps in sequence.

    The section id is `course-<product>`, not just a styling hook: it is the
    anchor `course_anchor_href` (this module) builds hrefs against, and that
    every "awaits your sign-off"-style chip elsewhere on the site links
    through, so a reader following one lands on this product's own proposed
    steps rather than the top of the page (issue_01M17W7DRZ3FJNRNYCAQFWHS1F)."""
    steps = "".join(_course_entry_row(e) for e in course.entries) or (
        '<div class="allclear">No entries.</div>')
    anchor = escape(f"course-{course.product}", quote=True)
    return (f'<section class="proj-group" data-prod="{escape(course.product, quote=True)}" '
            f'id="{anchor}">'
            f'<h2 class="proj-head">{escape(course.product)} '
            f'<span class="n">{len(course.entries)}</span></h2>'
            f'<div class="course-track">{steps}</div>'
            f'</section>')


def _course_page(sections_html: str) -> str:
    body = f'''<div class="ops">
  <header class="ops-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / engineering / course</div>
    <h1>Course</h1>
    <p class="lede">Each project's Course, in the order its steps run.</p>
  </header>
  <div class="wrap">
    <div class="queue">{sections_html or '<div class="allclear">No live Course for any project.</div>'}</div>
    <div class="footnote">
      Signed in as <strong>__WHOAMI__</strong>. A step links to its issue; there is no resolve action
      on this page.
    </div>
  </div>
</div>'''
    return ds.page_shell(
        title="Course",
        nav_html=ds.nav_component("/engineering/course", "full"),
        body=body,
        extra_css=_CSS + _GROUP_CSS,
    )


def live_html_course(scope: frozenset[str] = _ALL_PRODUCTS) -> str:
    """Render the Course page from the Library: the Course per project, its
    entries in sequence (the operator, 2026-08-29 — issue_01M1518ZFBGHAWC4Z46W893H2S).
    Full-tier only (server-gated).

    `scope` is the viewer's Library-resolved project scope (E19·2,
    issue_01M35K5AB4PRTJ71DMQW7Q4D3B): a project's Course is dropped before
    any section is built for it, the same "filter before grouping" rule
    `live_html` and `live_html_corrections` apply.

    Read-only — a Course entry carries no resolve action of its own; the
    linked issue does. `list_courses` raises on failure, uncaught, matching
    `library_client`'s module docstring (this page's only data section, so
    there is nothing left for a Library outage to spare — server.py's stale-
    static fallback is the right degrade). A single course's `get_course`
    detail call still degrades to its `list_courses` record on its own
    failure, the same discipline `chart_house_build._course_rows` applies,
    so one course's timeout cannot take every other row down with it."""
    courses = sorted((c for c in library_client.list_courses(status="live")
                      if _in_scope(c.product, scope)),
                     key=lambda c: c.product)
    sections = []
    for course in courses:
        try:
            detail = library_client.get_course(course.id) or course
        except Exception as ex:
            _log_swallowed(f"course {course.id} detail unavailable", ex)
            detail = course
        sections.append(_course_section(detail))
    return _course_page("".join(sections))


def main() -> None:
    """Seed static empty-state fallbacks for the three pages this module
    renders live — Epics, Corrections, Course. Served only if a live render
    throws (Library unreachable) — deliberately empty, never a stale
    snapshot, so a failure never shows misleading queue state."""
    SITE.mkdir(parents=True, exist_ok=True)
    empty_chip = '<button class="chip" data-prod="all" aria-pressed="true">All <span class="n">0</span></button>'

    epics_fallback = _epics_page("", empty_chip, 0).replace(
        '<div class="allclear">No open epics.</div>',
        '<div class="allclear">Live view unavailable — reload in a moment.</div>')
    (SITE / "issues.html").write_text(epics_fallback, encoding="utf-8")

    corrections_fallback = _corrections_page("", empty_chip, 0).replace(
        '<div class="allclear">No corrections queued.</div>',
        '<div class="allclear">Live view unavailable — reload in a moment.</div>')
    (SITE / "corrections.html").write_text(corrections_fallback, encoding="utf-8")

    course_fallback = _course_page("").replace(
        '<div class="allclear">No live Course for any project.</div>',
        '<div class="allclear">Live view unavailable — reload in a moment.</div>')
    (SITE / "course.html").write_text(course_fallback, encoding="utf-8")

    print("Wrote site/issues.html, site/corrections.html, site/course.html (static fallbacks)")


# A single issue's page. Used to follow the same rules `decisions_build` used
# for its detail view, so the two pages read as one system; that module is gone
# with the decision plane (2026-08-18), and this page's own styling now stands
# alone under /engineering.
_DETAIL_CSS = r"""
.ops-detail .back{color:var(--faint);text-decoration:none;letter-spacing:.14em;}
.ops-detail .back:hover{color:var(--accent);}
.ops-detail .detail-badges{display:flex;gap:9px;flex-wrap:wrap;margin:10px 0 0;}
.ops-detail h1{font-size:1.35rem;margin:8px 0 4px;}
.ops-detail .ops-head .meta{padding:0 0 16px;font-family:var(--mono);font-size:.72rem;color:var(--faint);}
.ops-detail .ops-head .meta a{color:var(--accent);text-decoration:none;}
.ops-detail .ops-head .meta a:hover{text-decoration:underline;}
.ops-detail .card{padding-bottom:2px;}
.ops-detail .card .body{margin:14px 16px 0;font-size:.94rem;}
.ops-detail .card .refs{padding:13px 16px 0;}
.ops-detail .card .actions{padding-top:16px;}
.ops-detail .card.frozen{padding-bottom:14px;}
.ops-detail .queue > .outcome{margin:0 0 12px;}
/* The terminal banners: the same shape as the in-queue outcome banners, in the
   resolution's own colour, so the page says at a glance how it ended. */
.ops .outcome.resolved-done .oc-label{background:var(--good-soft);color:var(--good);}
.ops .outcome.resolved-wontfix .oc-label{background:var(--bad-soft);color:var(--bad);}
.ops .outcome.resolved-duplicate .oc-label,
.ops .outcome.resolved-redirected .oc-label{background:var(--accent-soft);color:var(--accent);}
/* The blocked banner lists prerequisites rather than counting them, so it is
   taller than its siblings and the entries carry their own state. A met one
   stays visible and recedes: a reader judging how close an item is needs to see
   what has cleared, not only what has not. */
.ops .outcome.blocked .oc-label{background:var(--warn-soft);color:var(--warn);}
.ops .outcome.blocked .oc-label.ok{background:var(--good-soft);color:var(--good);}
.ops .pq-list{list-style:none;margin:8px 0 0;padding:0;display:flex;
              flex-direction:column;gap:6px;}
.ops .pq{display:flex;gap:8px;align-items:baseline;font-size:13px;line-height:1.45;}
.ops .pq-mark{flex:0 0 auto;font-size:11px;font-weight:700;letter-spacing:.02em;
              text-transform:uppercase;padding:1px 6px;border-radius:4px;
              background:var(--warn-soft);color:var(--warn);}
.ops .pq.met{color:var(--ink-3);}
.ops .pq.met .pq-mark{background:var(--good-soft);color:var(--good);}
/* An item the reader is not entitled to see: shown as a blocker, deliberately
   without a link, since there is nothing it could open. */
.ops .pq.missing code{color:var(--ink-2);}
.ops .pq-by{color:var(--ink-3);font-size:12px;}
.ops .outcome a{color:var(--accent);}
/* The specification / definition-of-done section (issue_01M123PF05MFZPY2Q07XGB1CS0):
   the same body/list typography the card uses, in its own section so it reads
   as the epic's own document rather than another comment in the thread. */
.ops-detail .spec-section{background:var(--surface);border:1px solid var(--hairline);
  border-radius:14px;box-shadow:var(--shadow);padding:16px;margin:0 0 12px;}
/* The model override (issue_01M38WVA7XHEDN1NFNPP8KDM0C): a section, not a
   badge, with the model at display size, because it changes what the fleet runs
   this item on. The control hides for a role without item_model. */
.ops-detail .model-section{background:var(--surface);border:1px solid var(--hairline);
  border-left:4px solid var(--accent);border-radius:14px;box-shadow:var(--shadow);
  padding:16px;margin:0 0 12px;}
.ops-detail .model-section .spec-label{margin-top:0;}
.ops-detail .model-name{font-family:var(--mono);font-size:1.25rem;font-weight:600;color:var(--accent);
  overflow-wrap:anywhere;}
.ops-detail .model-by{font-size:.85rem;color:var(--ink-2);margin-top:4px;}
.ops-detail .model-reason{font-size:.9rem;margin:8px 0 0;}
.ops-detail .model-section[data-can-set="false"] .model-edit{display:none;}
.ops-detail .model-edit{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;}
.ops-detail .model-edit input,.ops-detail .model-edit select{flex:1 1 200px;min-width:0;
  box-sizing:border-box;font:inherit;
  font-size:.88rem;padding:7px 10px;border:1px solid var(--hairline-strong);border-radius:8px;
  background:var(--surface-2,transparent);color:var(--ink);}
.ops-detail .model-edit .model-select{font-family:var(--mono);}
.ops-detail .model-unavailable{flex:1 1 100%;font-size:.85rem;color:var(--ink-2);margin:0;}
.ops-detail .model-actions{display:flex;gap:8px;flex-wrap:wrap;}
.ops-detail .spec-head{display:flex;justify-content:flex-end;align-items:center;gap:10px;margin-bottom:2px;}
/* The epic editor (the operator, 2026-09-22). Gated like Approve: a role without
   epic_approve sees no Edit, and epic_revise_api refuses it regardless. */
.ops-detail .spec-section[data-can-approve="false"] .spec-edit-open{display:none;}
.ops-detail .spec-edit-open{font:inherit;font-size:.8rem;background:transparent;color:var(--accent);
  border:1px solid var(--hairline-strong);border-radius:8px;padding:3px 10px;cursor:pointer;}
.ops-detail .spec-edit textarea{width:100%;box-sizing:border-box;font-family:var(--mono);
  font-size:.85rem;line-height:1.45;padding:10px;border:1px solid var(--hairline-strong);
  border-radius:8px;background:var(--surface-2,transparent);color:var(--ink);resize:vertical;}
.ops-detail .spec-edit-actions{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;}
.ops-detail .badge.maturity{background:var(--accent-soft);color:var(--accent);
  font-family:var(--mono);font-size:.68rem;letter-spacing:.05em;text-transform:uppercase;
  padding:3px 8px;border-radius:6px;font-weight:600;}
.ops-detail .badge.maturity-unset{background:var(--surface-2);color:var(--faint);}
.ops-detail .spec-label{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;
  text-transform:uppercase;color:var(--faint);margin:14px 0 6px;}
.ops-detail .spec-section .spec-label:first-of-type{margin-top:2px;}
.ops-detail .spec-body,.ops-detail .dod-body{font-size:.94rem;color:var(--muted);}
.ops-detail .spec-body p,.ops-detail .dod-body p{margin:0 0 9px;}
.ops-detail .rv{border-top:1px solid var(--hairline);padding:12px 0 4px;}
.ops-detail .rv:first-of-type{border-top:none;}
.ops-detail .rv-head{display:flex;flex-wrap:wrap;gap:10px;align-items:baseline;}
.ops-detail .rv-role{font-family:var(--mono);font-weight:600;color:var(--ink);}
.ops-detail .rv-verdict{font-family:var(--mono);font-size:.72rem;padding:2px 8px;border-radius:6px;font-weight:600;}
.ops-detail .rv-ok{background:var(--ok-wash);color:var(--ok);}
.ops-detail .rv-bad{background:var(--err-wash);color:var(--err);}
.ops-detail .rv-other{background:var(--surface-2);color:var(--ink-2);}
.ops-detail .rv-meta{font-family:var(--mono);font-size:.72rem;color:var(--faint);}
.ops-detail .rv-mark{display:block;font-size:.8rem;margin-top:6px;}
.ops-detail .rv-stale{color:var(--warn);}
.ops-detail .rv-unverified{color:var(--faint);}
.ops-detail .rv-body{font-size:.9rem;color:var(--muted);margin-top:8px;overflow-wrap:anywhere;}
.ops-detail .rv-findings{font-size:.88rem;margin:6px 0 0;padding-left:18px;}
.ops-detail .rv-none{font-size:.9rem;color:var(--ink-2);margin:0;}
.ops-detail .fd-list{font-size:.9rem;margin:0;padding-left:18px;}
.ops-detail .fd-state{font-family:var(--mono);font-size:.72rem;color:var(--faint);}
.ops-detail .spec-body p:last-child,.ops-detail .dod-body p:last-child{margin-bottom:0;}
.ops-detail .spec-body strong,.ops-detail .dod-body strong{color:var(--ink);font-weight:650;}
.ops-detail .spec-body code,.ops-detail .dod-body code{font-family:var(--mono);font-size:.85em;
  background:var(--surface-2,rgba(127,127,127,.12));padding:1px 4px;border-radius:4px;}
.ops-detail .dod-body ul,.ops-detail .dod-body ol,
.ops-detail .spec-body ul,.ops-detail .spec-body ol{margin:0 0 9px;padding-left:20px;}
.ops-detail .dod-body li,.ops-detail .spec-body li{margin:3px 0;}
"""

_CSS = r"""
[hidden]{display:none!important;}
.ops{
  --paper:var(--bg);--hairline:var(--line);--hairline-strong:var(--line);
  --muted:var(--ink-2);--faint:var(--ink-3);
  --accent:var(--brand-ink);--accent-soft:var(--brand-wash);
  --good:var(--ok);--good-soft:color-mix(in srgb,var(--ok) 14%,var(--surface));
  --bad:var(--err);--bad-soft:color-mix(in srgb,var(--err) 12%,var(--surface));
  --warn-soft:color-mix(in srgb,var(--warn) 16%,var(--surface));
}
.ops{line-height:1.5;}
.ops .wrap{max-width:760px;margin:0 auto;padding:0 2px 72px;}
.ops .ops-head{max-width:760px;margin:0 auto;padding:22px 2px 0;}
.ops .eyebrow-line{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);display:flex;align-items:center;gap:8px;}
.ops .eyebrow-line .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px var(--accent-soft);}
.ops h1{font-size:1.55rem;letter-spacing:-.02em;margin:6px 0 2px;text-wrap:balance;font-weight:640;}
.ops .lede{color:var(--muted);font-size:.92rem;margin:0 0 14px;}
.ops .count{color:var(--accent);font-variant-numeric:tabular-nums;font-weight:680;}
.ops .chips{display:flex;gap:7px;overflow-x:auto;padding:0 0 14px;scrollbar-width:none;}
.ops .chips::-webkit-scrollbar{display:none;}
.ops .chip{flex:0 0 auto;font-family:var(--mono);font-size:.74rem;padding:6px 11px;border-radius:999px;
border:1px solid var(--hairline-strong);background:var(--surface);color:var(--muted);cursor:pointer;white-space:nowrap;
transition:background .15s,color .15s,border-color .15s;}
.ops .chip[aria-pressed="true"]{background:var(--ink);color:var(--paper);border-color:var(--ink);}
.ops .chip .n{opacity:.6;margin-left:5px;}
.ops .sec{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);
margin:26px 2px 12px;display:flex;align-items:center;gap:10px;}
.ops .sec::after{content:"";flex:1;height:1px;background:var(--hairline);}
/* The truncation note sits inside .sec but must read as a sentence, not as
   another tracked-out section label — it is the one thing on the page that
   explains why the card count is short of the headline. */
.ops .sec .trunc{text-transform:none;letter-spacing:.02em;color:var(--warn,var(--faint));
font-size:.72rem;order:1;}
.ops .queue{display:flex;flex-direction:column;gap:12px;}
.ops .card{background:var(--surface);border:1px solid var(--hairline);border-radius:14px;box-shadow:var(--shadow);
overflow:hidden;transition:opacity .3s,transform .3s;}
.ops .card-top{display:flex;align-items:center;gap:9px;padding:13px 16px 0;flex-wrap:wrap;}
.ops .badge{font-family:var(--mono);font-size:.68rem;letter-spacing:.05em;text-transform:uppercase;padding:3px 8px;border-radius:6px;font-weight:600;}
.ops .badge.prod{background:var(--accent-soft);color:var(--accent);}
.ops .cross-product{margin-top:6px;}
.ops .badge.xprod{background:var(--surface-2);color:var(--muted);border:1px dashed var(--hairline-strong);}
.ops .badge.type{background:var(--surface-2);color:var(--muted);border:1px solid var(--hairline-strong);}
.ops .badge.type-feature,.ops .badge.type-task{background:var(--warn-soft);color:var(--warn);border-color:transparent;}
.ops .badge.needs{background:var(--surface-2);color:var(--muted);border:1px solid var(--hairline-strong);}
/* "needs you" reads as an accent, not an alarm: it is a routing fact, not a
   severity. A row nothing will pick up should catch the eye without competing
   with sev-high, which is a genuinely different axis and may appear alongside. */
.ops .badge.needs-operator{background:var(--accent-soft);color:var(--accent);border-color:transparent;font-weight:600;}
.ops .badge.sev{background:transparent;color:var(--faint);border:1px solid var(--hairline-strong);}
.ops .badge.sev-high,.ops .badge.sev-critical{background:var(--bad-soft);color:var(--bad);border-color:transparent;}
/* An epic's maturity, on the queue card as well as the detail page
   (issue_01M32CB6A9T04NVYPARGDT2GWQ). **APPROVED AND PROPOSED ARE GIVEN
   DIFFERENT COLOUR, NOT JUST DIFFERENT TEXT** — the requirement is that the
   two never render alike, and a reader scanning a queue of 51 epics reads
   colour before words. `proposed` takes the same accent as `needs-operator`
   because it means the same thing: this one is waiting on the operator. */
.ops .badge.maturity{background:transparent;color:var(--faint);border:1px solid var(--hairline-strong);
  font-family:var(--mono);font-size:.68rem;letter-spacing:.05em;text-transform:uppercase;}
.ops .badge.maturity-approved{background:var(--good-soft);color:var(--good);border-color:transparent;}
.ops .badge.maturity-proposed{background:var(--accent-soft);color:var(--accent);border-color:transparent;font-weight:600;}
.ops .age{margin-left:auto;font-family:var(--mono);font-size:.72rem;color:var(--faint);font-variant-numeric:tabular-nums;white-space:nowrap;}
.ops .card h2{font-size:1.06rem;letter-spacing:-.01em;margin:9px 16px 0;font-weight:600;text-wrap:balance;}
.ops .body{color:var(--muted);font-size:.9rem;margin:5px 16px 0;}
.ops details.background{margin:5px 16px 0;}
.ops details.background summary{cursor:pointer;color:var(--faint);font-size:.85rem;}
.ops details.background .body{margin:6px 0 0;}
.ops .body p{margin:0 0 7px;}
.ops .body p:last-child{margin-bottom:0;}
.ops .body strong{color:var(--ink);font-weight:650;}
.ops .body em{font-style:italic;}
.ops .body code{font-family:var(--mono);font-size:.85em;background:var(--surface-2,rgba(127,127,127,.12));padding:1px 4px;border-radius:4px;overflow-wrap:anywhere;}
.ops .body pre{font-family:var(--mono);font-size:.82em;background:var(--surface-2,rgba(127,127,127,.12));padding:8px 10px;border-radius:6px;overflow-x:auto;margin:6px 0;}
.ops .body pre code{background:none;padding:0;}
.ops .body a{color:var(--brand);text-decoration:underline;}
/* The issue thread (ARCH-373). A diagnosis is visually distinct because it is
   the handoff contract, not just the newest opinion in the thread. */
.ops .thread{margin:9px 16px 0;display:flex;flex-direction:column;gap:7px;}
.ops .cmt{border-left:2px solid var(--hairline-strong);padding:5px 0 5px 10px;}
.ops .cmt-top{display:flex;align-items:center;gap:8px;margin-bottom:2px;}
.ops .cmt-kind{font-family:var(--mono);font-size:.66rem;text-transform:uppercase;
  letter-spacing:.06em;color:var(--muted);}
.ops .cmt-who{font-family:var(--mono);font-size:.68rem;color:var(--faint);}
.ops .cmt-body{font-size:.86rem;color:var(--muted);}
.ops .cmt-body p{margin:0 0 5px;}
.ops .cmt-body p:last-child{margin-bottom:0;}
.ops .cmt-body code{font-family:var(--mono);font-size:.85em;
  background:var(--surface-2,rgba(127,127,127,.12));padding:1px 4px;border-radius:4px;}
.ops .cmt-diagnosis{border-left-color:var(--accent);}
.ops .cmt-diagnosis .cmt-kind{color:var(--accent);font-weight:650;}
.ops .cmt-decline .cmt-body{color:var(--faint);}
.ops .cmt-elided{border-left-style:dashed;color:var(--faint);font-family:var(--mono);
  font-size:.72rem;padding-top:3px;padding-bottom:3px;}
.ops .body ul,.ops .body ol{margin:0 0 7px;padding-left:20px;}
.ops .body li{margin:2px 0;}
.ops .body h1,.ops .body h2,.ops .body h3,.ops .body h4{font-size:.94rem;font-weight:650;color:var(--ink);margin:9px 0 4px;}
.ops .body blockquote{margin:6px 0;padding-left:10px;border-left:2px solid var(--hairline-strong);color:var(--faint);}
.ops .refs{display:flex;gap:7px;flex-wrap:wrap;padding:11px 16px 0;}
.ops .ref{font-family:var(--mono);font-size:.72rem;color:var(--muted);background:var(--surface-2);
border:1px solid var(--hairline);border-radius:6px;padding:2px 7px;text-decoration:none;}
.ops a.ref:hover{border-color:var(--accent);color:var(--accent);}
.ops .outcome{display:flex;align-items:center;gap:8px;margin:10px 16px 0;font-family:var(--mono);font-size:.74rem;flex-wrap:wrap;}
.ops .outcome .oc-label{font-size:.66rem;letter-spacing:.05em;text-transform:uppercase;padding:3px 8px;border-radius:6px;font-weight:600;}
.ops .outcome.pr-open .oc-label{background:var(--accent-soft);color:var(--accent);}
.ops .outcome.claimed .oc-label{background:var(--good-soft);color:var(--good);}
.ops .outcome.claim-stale .oc-label{background:var(--bad-soft);color:var(--bad);}
.ops .outcome.claim-stale{color:var(--muted);}
.ops .outcome a{color:var(--accent);text-decoration:underline;}
.ops .outcome .oc-id{color:var(--faint);}
.ops .meta{padding:10px 16px 0;font-family:var(--mono);font-size:.72rem;color:var(--faint);}
.ops .meta .who{color:var(--muted);}
.ops .actions{display:flex;gap:8px;padding:13px 16px 15px;flex-wrap:wrap;}
.ops .btn{flex:1 1 auto;font-family:var(--sans);font-size:.86rem;font-weight:560;padding:10px 12px;border-radius:9px;
border:1px solid var(--hairline-strong);background:var(--surface-2);color:var(--ink);cursor:pointer;
transition:transform .08s,background .15s,border-color .15s;}
.ops .btn:active{transform:translateY(1px);}
.ops .btn:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
.ops .btn.fixed{background:var(--good-soft);color:var(--good);border-color:transparent;}
.ops .btn.wontfix{background:transparent;color:var(--bad);border-color:var(--hairline-strong);}
/* Approve, on an epic put forward for it (issue_01M32CB6A9T04NVYPARGDT2GWQ). The
   filled, primary treatment is deliberate: on a proposal it is the act the page
   exists for, and Decline beside it keeps the outline Won't do already has. */
.ops .btn.approve{background:var(--good);color:#fff;border-color:transparent;font-weight:600;}
.ops .btn.approve:disabled{opacity:.6;cursor:default;}
/* **The role gate hides Approve and leaves Decline**, rather than hiding the row:
   a full-tier member without epic_approve can still close a proposal, which is
   what the resolve route already lets them do. Cosmetic — approve_api refuses
   a role without the capability whatever the page renders. */
.ops .actions[data-can-approve="false"] .btn.approve,
.ops .actions[data-can-approve="false"] .btn.edit{display:none;}
/* Edit is a link (it navigates from the queue), styled as the row's buttons. */
.ops a.btn.edit{background:transparent;color:var(--accent);border-color:var(--hairline-strong);
  text-decoration:none;display:inline-flex;align-items:center;}
/* A refusal is shown inside the card, where the control is, across the full
   row width so a Library message of any length reads without wrapping into
   the buttons. */
.ops .approve-note{flex-basis:100%;margin:2px 0 0;font-size:.8rem;padding:.45rem .65rem;border-radius:6px;}
.ops .approve-note.ok{background:var(--good-soft);color:var(--good);}
.ops .approve-note.err{background:var(--bad-soft);color:var(--bad);}
.ops .btn.duplicate{background:transparent;color:var(--muted);border-color:var(--hairline-strong);}
.ops .btn.redirected{background:var(--accent-soft);color:var(--accent);border-color:transparent;}
/* Only the Duplicate path asks which issue is duplicated; the other two
   resolutions have no original to point at. */
.ops .dupe-row{display:none;margin-bottom:8px;}
.ops .directive[data-resolution="duplicate"] .dupe-row{display:block;}
.ops .dupeid{width:100%;font:inherit;font-size:.86rem;padding:6px 9px;border-radius:7px;
  border:1px solid var(--hairline-strong);background:var(--surface);color:var(--ink);}
.ops .dupeid:focus{outline:none;border-color:var(--accent);}
.ops .ref.dupe{background:var(--surface-2,rgba(127,127,127,.12));}
.ops .btn[disabled]{opacity:.5;cursor:default;}
.ops .directive{display:none;padding:0 16px 15px;}
.ops .directive.open{display:block;}
.ops .directive label{font-family:var(--mono);font-size:.7rem;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);display:block;margin-bottom:6px;}
.ops .directive textarea{width:100%;min-height:66px;resize:vertical;font-family:var(--sans);font-size:.9rem;color:var(--ink);
background:var(--surface-2);border:1px solid var(--hairline-strong);border-radius:9px;padding:9px 11px;}
.ops .directive textarea:focus-visible{outline:2px solid var(--accent);outline-offset:1px;border-color:var(--accent);}
.ops .directive .send-row{display:flex;gap:8px;margin-top:9px;}
.ops .directive .send{background:var(--accent);color:var(--surface);border:none;padding:9px 18px;border-radius:9px;font-weight:560;font-size:.86rem;cursor:pointer;}
.ops .directive .cancel{background:transparent;border:1px solid var(--hairline-strong);color:var(--muted);padding:9px 14px;border-radius:9px;cursor:pointer;font-size:.86rem;}
.ops .closed{display:flex;flex-direction:column;gap:9px;}
.ops .row{display:flex;align-items:center;gap:11px;background:var(--surface);border:1px solid var(--hairline);border-radius:11px;padding:11px 14px;}
.ops .status{flex:0 0 auto;width:26px;height:26px;border-radius:8px;display:grid;place-items:center;font-size:.8rem;font-weight:700;}
.ops .status.done{background:var(--good-soft);color:var(--good);}
.ops .status.wontfix{background:var(--bad-soft);color:var(--bad);}
.ops .status.duplicate{background:var(--surface-2);color:var(--faint);}
.ops .status.redirected{background:var(--accent-soft);color:var(--accent);}
.ops .row-main{flex:1;min-width:0;}
.ops .row-title{font-size:.9rem;font-weight:540;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.ops .row-sub{font-family:var(--mono);font-size:.71rem;color:var(--faint);margin-top:2px;}
.ops .row-sub .verdict{color:var(--muted);}
.ops .row-prod{font-family:var(--mono);font-size:.66rem;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);
background:var(--accent-soft);padding:3px 7px;border-radius:6px;flex:0 0 auto;}
.ops .toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%) translateY(20px);background:var(--ink);
color:var(--paper);font-size:.85rem;padding:11px 18px;border-radius:11px;box-shadow:var(--shadow);opacity:0;
pointer-events:none;transition:opacity .25s,transform .25s;font-family:var(--mono);z-index:40;max-width:90vw;}
.ops .toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
.ops .toast.err{background:var(--bad);}
.ops .empty{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.8rem;padding:30px 0;display:none;}
.ops .allclear{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.82rem;padding:34px 0;}
.ops .footnote{margin-top:34px;padding-top:16px;border-top:1px solid var(--hairline);color:var(--faint);font-size:.8rem;}
.ops .footnote code{font-family:var(--mono);color:var(--muted);font-size:.76rem;}
.ops .capture{margin-bottom:22px;}
.ops .capture[data-can-log="false"]{display:none;}
.ops .capture-toggle{font-family:var(--mono);font-size:.78rem;letter-spacing:.04em;color:var(--accent);
background:var(--surface);border:1px dashed var(--hairline-strong);border-radius:10px;padding:9px 14px;cursor:pointer;}
.ops .capture-toggle[aria-expanded="true"]{border-style:solid;border-color:var(--accent);}
.ops .capture-form{margin-top:10px;padding:16px;display:flex;flex-direction:column;gap:12px;}
.ops .capture-form label{display:flex;flex-direction:column;gap:4px;font-family:var(--mono);font-size:.7rem;
letter-spacing:.1em;text-transform:uppercase;color:var(--faint);}
.ops .capture-form label .opt{text-transform:none;letter-spacing:0;opacity:.7;}
.ops .capture-form input[type=text],.ops .capture-form input:not([type]),.ops .capture-form textarea,
.ops .capture-form select{font-family:var(--sans);
font-size:.9rem;color:var(--ink);background:var(--surface-2);border:1px solid var(--hairline-strong);
border-radius:8px;padding:8px 11px;letter-spacing:normal;text-transform:none;}
.ops .capture-form input:focus-visible,.ops .capture-form textarea:focus-visible,
.ops .capture-form select:focus-visible{outline:2px solid var(--accent);outline-offset:1px;}
.ops .capture-form textarea{resize:vertical;line-height:1.45;}
.ops .cf-row{display:flex;gap:10px;flex-wrap:wrap;}
.ops .cf-row label{flex:1;min-width:140px;}
.ops .cf-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
.ops .cf-submit{margin-left:auto;font-family:var(--sans);font-size:.88rem;font-weight:560;background:var(--accent);
color:var(--surface);border:none;border-radius:8px;padding:9px 18px;cursor:pointer;}
.ops .cf-submit[disabled]{opacity:.55;cursor:default;}
.ops .cf-note{font-family:var(--mono);font-size:.76rem;padding:9px 12px;border-radius:8px;}
.ops .cf-note.ok{background:var(--good-soft);color:var(--good);}
.ops .cf-note.err{background:var(--warn-soft);color:var(--warn);}
@media (max-width:520px){.ops h1{font-size:1.35rem;}.ops .actions{flex-wrap:wrap;}.ops .btn{flex:1 1 30%;}}
"""

# The per-project grouping shared by Epics, Corrections and Course
# (issue_01M1518ZFBGHAWC4Z46W893H2S) — the section header + expand toggle +
# Course step track, none of which the flat single-page queue needed.
_GROUP_CSS = r"""
.ops .proj-group{margin:0 0 26px;}
.ops .proj-group:last-child{margin-bottom:0;}
.ops .proj-head{font-family:var(--mono);font-size:.78rem;letter-spacing:.08em;text-transform:uppercase;
color:var(--muted);margin:0 0 10px;display:flex;align-items:center;gap:8px;}
.ops .proj-head .n{font-family:var(--mono);font-size:.7rem;color:var(--faint);
background:var(--surface-2);border-radius:999px;padding:1px 8px;}
/* `?sort=topic`'s area headings, one level in from the project heading they
   sit under and styled to read as its subordinate rather than as a second
   project. `.by-topic` supplies the spacing `.queue` gets from `.children` in
   the flat view, which the grouped view moves a level out. */
.ops .topic-head{font-family:var(--mono);font-size:.72rem;letter-spacing:.06em;
text-transform:uppercase;color:var(--faint);margin:14px 0 6px;display:flex;
align-items:center;gap:8px;}
.ops .by-topic>.topic-head:first-child{margin-top:0;}
.ops .topic-head .n{font-family:var(--mono);font-size:.68rem;color:var(--faint);
background:var(--surface-2);border-radius:999px;padding:1px 7px;}
.ops .task-group{margin:0 0 12px;}
.ops .task-group:last-child{margin-bottom:0;}
.ops .children-toggle{margin-top:6px;}
.ops .children-toggle>summary{cursor:pointer;font-family:var(--mono);font-size:.74rem;
color:var(--accent);list-style:none;padding:4px 0;}
.ops .children-toggle>summary::-webkit-details-marker{display:none;}
.ops .children-toggle>summary::before{content:"▸ ";}
.ops .children-toggle[open]>summary::before{content:"▾ ";}
.ops .children-toggle .children{margin-top:8px;padding-left:14px;
border-left:2px solid var(--hairline-strong);}
.ops .course-track{display:flex;flex-wrap:wrap;gap:6px;}
.ops .course-step{font-family:var(--mono);font-size:.74rem;padding:6px 10px;border-radius:8px;
border:1px solid var(--hairline-strong);background:var(--surface);color:var(--muted);
text-decoration:none;}
.ops .course-step:hover{border-color:var(--accent);color:var(--accent);}
.ops .course-step.step-done{background:var(--good-soft);color:var(--good);border-color:transparent;}
.ops .course-step.step-approved{background:var(--accent-soft);color:var(--accent);border-color:transparent;}
.ops .course-step.step-proposed{background:var(--warn-soft);color:var(--warn);border-color:transparent;}
"""

_JS = r"""
(function(){
  var queue=document.getElementById('queue'),waitEl=document.getElementById('waitCount'),
      toast=document.getElementById('toast'),empty=document.getElementById('empty'),
      chips=document.getElementById('chips'),activeProd='all',toastTimer;
  function showToast(msg,isErr){toast.textContent=msg;toast.classList.toggle('err',!!isErr);
    toast.classList.add('show');clearTimeout(toastTimer);
    toastTimer=setTimeout(function(){toast.classList.remove('show');},2600);}
  // Issues the server counted but did not render (the queue is capped for page
  // weight). recount() can only see cards, so without adding this back it would
  // overwrite the server's true headline with the cap on the very first call —
  // which runs on load. Resolving a card still decrements correctly, because
  // only the rendered half changes.
  var hiddenCount=(function(){var w=document.querySelector('.ops .wrap');
    return w?(parseInt(w.dataset.hidden,10)||0):0;})();
  // **AN ISSUE'S OWN PAGE CARRIES THE CARD BUT NOT THE QUEUE'S CHROME.** It has
  // `#queue` and `#toast` and no `#chips`, `#waitCount` or `#empty`, so every
  // use of those three is guarded. Before this, the page threw at the chips
  // listener below on load, and the card's click handler — registered after it
  // — was never attached: every issue page's Won't do / Fixed / Duplicate /
  // Redirected buttons did nothing, and had since the page existed. Found
  // while wiring Approve onto that page (issue_01M32CB6A9T04NVYPARGDT2GWQ).
  function recount(){var vis=0,total=0;
    queue.querySelectorAll('.card').forEach(function(c){if(c.dataset.resolved)return;total++;
      if(activeProd==='all'||c.dataset.prod===activeProd)vis++;});
    if(waitEl)waitEl.textContent=total+hiddenCount;
    if(empty)empty.style.display=(vis===0&&total>0)?'block':'none';}
  function applyFilter(){queue.querySelectorAll('.card').forEach(function(c){
      var m=activeProd==='all'||c.dataset.prod===activeProd;
      c.style.display=(m&&!c.dataset.resolved)?'':'none';});
    // Epics/Corrections wrap their cards in a per-project <section
    // class="proj-group">; filtering to one product hides every OTHER
    // product's cards but leaves that section's own header and epic toggles
    // stranded with nothing under them unless the section itself is hidden
    // too. Blockers has no such wrapper, so this is a no-op there.
    queue.querySelectorAll('.proj-group').forEach(function(g){
      var any=false;
      g.querySelectorAll('.card').forEach(function(c){if(c.style.display!=='none')any=true;});
      g.style.display=any?'':'none';});
    recount();}
  if(chips)chips.addEventListener('click',function(e){var chip=e.target.closest('.chip');if(!chip)return;
    chips.querySelectorAll('.chip').forEach(function(c){c.setAttribute('aria-pressed','false');});
    chip.setAttribute('aria-pressed','true');activeProd=chip.dataset.prod;applyFilter();});
  function resolve(card,label){card.dataset.resolved='1';card.style.opacity='0';
    card.style.transform='translateY(-6px)';
    setTimeout(function(){card.style.display='none';recount();},300);showToast(label);}
  function doResolve(card,resolution,note,dupeOf,kind){
    var id=card.dataset.id;
    card.querySelectorAll('button').forEach(function(b){b.disabled=true;});
    var body={resolution:resolution};if(note)body.outcome_note=note;
    // The whole point of the Duplicate button: record WHICH issue this repeats.
    // Without it a duplicate is just a worse "won't do" — closed, with the
    // relationship it was closed for nowhere on the record.
    if(resolution==='duplicate'&&dupeOf)body.refs={duplicate_of:dupeOf};
    fetch('/api/issues/'+encodeURIComponent(id)+'/resolve',{
      method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)
    }).then(function(r){
      if(r.ok){var label=kind==='decline'?'✕ declined':
        resolution==='fixed'?'✓ fixed':resolution==='wontfix'?'✕ won\'t do':
        resolution==='redirected'?'↪ redirected':'⧉ duplicate';resolve(card,label);}
      else{card.querySelectorAll('button').forEach(function(b){b.disabled=false;});
        r.json().then(function(j){showToast('Not saved: '+((j&&j.error)||('HTTP '+r.status)),true);})
         .catch(function(){showToast('Not saved: HTTP '+r.status,true);});}
    }).catch(function(){card.querySelectorAll('button').forEach(function(b){b.disabled=false;});
      showToast('Not saved: network error',true);});
  }
  // **APPROVE IS THE SAME ROUTE THE CHART HOUSE CALLS** (approve_api), so there
  // is one approval path rather than two. Unlike a resolve, an approved epic
  // stays open — it is now approved work on its Course — so the card is NOT
  // removed; its buttons are replaced by a record of the act, and the next
  // render shows it as the approved epic it now is.
  //
  // A refusal renders INSIDE the card, where the control is, rather than as a
  // toast: the specification asks for the cause to be named where the member
  // pressed, and a toast vanishes before an unfamiliar Library message can be
  // read. The text is the Library's own and reaches the DOM through
  // textContent, never innerHTML.
  function doApprove(card,btn){
    var id=card.dataset.id,row=btn.closest('.actions'),note=row.querySelector('.approve-note');
    row.querySelectorAll('button').forEach(function(b){b.disabled=true;});
    note.hidden=true;
    fetch('/api/epics/'+encodeURIComponent(id)+'/approve',{method:'POST',credentials:'same-origin'})
      .then(function(r){return r.json().catch(function(){return {};})
        .then(function(j){return {ok:r.ok,body:j};});})
      .then(function(res){
        if(res.ok){
          row.querySelectorAll('button').forEach(function(b){b.remove();});
          note.className='approve-note ok';
          note.textContent=res.body.position?
            'Approved — at position '+res.body.position+' on its Course.':'Approved.';
          note.hidden=false;showToast('✓ approved');return;}
        row.querySelectorAll('button').forEach(function(b){b.disabled=false;});
        note.className='approve-note err';
        note.textContent=(res.body&&res.body.error)||'Couldn\'t approve this epic.';
        note.hidden=false;})
      .catch(function(){
        row.querySelectorAll('button').forEach(function(b){b.disabled=false;});
        note.className='approve-note err';note.textContent='Couldn\'t reach the server.';
        note.hidden=false;});}
  // Update note-label text and make textarea required when resolution is 'redirected'.
  function syncDirective(dir,resolution,kind){
    var label=dir.querySelector('.note-label');
    var ta=dir.querySelector('textarea');
    // A declined PROPOSAL is closed as wontfix, but "what fixed it" cannot be
    // true of work that was never started, so the prompt says what the note
    // is for: the crew that proposed it reads this reason.
    if(label){label.textContent=resolution==='redirected'?
      'Directive — what should happen next (required)':
      kind==='decline'?'Why it is declined — the crew that proposed it reads this (optional)':
      'Outcome note — why it won\'t be done, or what fixed it (optional)';}
    if(ta){ta.required=resolution==='redirected';}}
  queue.addEventListener('click',function(e){var btn=e.target.closest('button');if(!btn)return;
    var card=btn.closest('.card');if(!card)return;var act=btn.dataset.act;
    var dir=card.querySelector('.directive');
    if(act==='approve'){doApprove(card,btn);return;}
    if(act==='resolve'){var res=btn.dataset.resolution;dir.dataset.resolution=res;
      dir.dataset.kind=btn.dataset.kind||'';
      syncDirective(dir,res,btn.dataset.kind);dir.classList.add('open');
      var dupeEl=dir.querySelector('.dupeid');
      if(res==='duplicate'&&dupeEl)dupeEl.focus();
      else dir.querySelector('textarea').focus();}
    else if(act==='cancel')dir.classList.remove('open');
    else if(act==='send'){var t=dir.querySelector('textarea').value.trim();
      var res=dir.dataset.resolution;
      var dupeEl=dir.querySelector('.dupeid');
      var dupeOf=dupeEl?dupeEl.value.trim():'';
      if(res==='duplicate'&&!dupeOf){
        showToast('Which issue does this duplicate?',true);
        if(dupeEl)dupeEl.focus();
        return;}
      if(res==='redirected'&&!t){
        showToast('A directive is required for redirected',true);
        dir.querySelector('textarea').focus();
        return;}
      doResolve(card,res,t||null,dupeOf,dir.dataset.kind);}});
  recount();
})();

// The "Log an issue" capture panel — POSTs straight to /api/issues as the viewer's
// own CF Access identity; the __CAN_LOG_ISSUE__ substitution only hides the form
// from roles that would be 403'd anyway.
(function(){
  var toggle=document.getElementById('logToggle');
  if(!toggle)return;
  var form=document.getElementById('logForm'),note=document.getElementById('logNote');
  function setNote(cls,text){note.className='cf-note '+cls;note.textContent=text;note.hidden=false;}
  toggle.addEventListener('click',function(){
    var open=form.hidden;form.hidden=!open;
    toggle.setAttribute('aria-expanded',open?'true':'false');});
  form.addEventListener('submit',function(e){
    e.preventDefault();
    var btn=form.querySelector('.cf-submit');btn.disabled=true;btn.textContent='Logging…';
    var payload={
      product:form.elements.product.value.trim(),
      title:form.elements.title.value.trim(),
      body:form.elements.body.value,
      severity:form.elements.severity.value};
    fetch('/api/issues',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
    }).then(function(r){
      return r.json().catch(function(){return {};}).then(function(j){
        if(!r.ok)throw new Error((j&&j.error)||('HTTP '+r.status));
        return j;});
    }).then(function(j){
      setNote('ok','Logged '+(j.id||'')+(j.idempotent?' (already captured)':'')+
        ' — reload to see it in the queue.');
      form.elements.title.value='';form.elements.body.value='';
    }).catch(function(err){
      setNote('err','Log failed: '+err.message);
    }).finally(function(){btn.disabled=false;btn.textContent='Log issue';});});
})();
"""

# **THE EPIC EDITOR, on an epic's own page only** (the operator, 2026-09-22). A separate
# script rather than part of `_JS`, because `_JS` also runs on every queue page
# and none of them has a specification to edit.
#
# **`base` is the text the editor OPENED with**, captured at the first open
# rather than at page load, so it is the textarea's own value before any
# keystroke. The server compares it with the epic as read under the Library's
# lease and refuses the save if they differ, which is how a second editor's
# save is kept from silently overwriting the first. A refused save keeps the
# draft in place so nothing typed is lost.
#
# Every Library message reaches the page through textContent, never innerHTML.
_SPEC_EDIT_JS = r"""
(function(){
  var sec=document.querySelector('.spec-section');
  if(!sec||sec.dataset.edit!=='1')return;
  var form=sec.querySelector('.spec-edit'),view=sec.querySelector('.spec-view');
  var spec=form.querySelector('.spec-edit-spec'),dod=form.querySelector('.spec-edit-dod');
  var save=form.querySelector('.spec-edit-save'),note=form.querySelector('.spec-edit-note');
  var base=null;
  function setNote(cls,text){note.className='approve-note spec-edit-note '+cls;
    note.textContent=text;note.hidden=false;}
  function open(){
    if(sec.dataset.canApprove!=='true')return;
    if(base===null)base={specification:spec.value,definition_of_done:dod.value};
    view.hidden=true;form.hidden=false;note.hidden=true;spec.focus();}
  function close(){form.hidden=true;view.hidden=false;}
  sec.querySelector('.spec-edit-open').addEventListener('click',open);
  // The action row's Edit is a link to this page's #edit. Here it opens the
  // editor directly, since a hash that is already #edit fires no hashchange.
  var rowEdit=document.querySelector('.actions .edit');
  if(rowEdit)rowEdit.addEventListener('click',function(e){e.preventDefault();open();});
  form.querySelector('.spec-edit-cancel').addEventListener('click',function(){
    if(base){spec.value=base.specification;dod.value=base.definition_of_done;}
    close();});
  save.addEventListener('click',function(){
    save.disabled=true;
    fetch('/api/epics/'+encodeURIComponent(sec.dataset.id)+'/revise',{
      method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({specification:spec.value,definition_of_done:dod.value,base:base})
    }).then(function(r){return r.json().catch(function(){return {};})
        .then(function(j){return {ok:r.ok,body:j};});})
      .then(function(res){
        save.disabled=false;
        var b=res.body||{};
        if(res.ok&&b.release_error){setNote('err','Saved, but '+b.release_error);return;}
        if(res.ok&&b.saved){location.reload();return;}
        if(res.ok){setNote('ok','Nothing changed, so nothing was saved.');return;}
        setNote('err',(b.error||'Couldn\'t save this epic.')+
          (b.release_error?' Also: '+b.release_error:''));})
      .catch(function(){save.disabled=false;setNote('err','Couldn\'t reach the server.');});});
  if(location.hash==='#edit')open();
})();
"""


# The model control on the item page. A success reloads, so what the page then
# shows is the Library's record rather than what the browser sent.
_MODEL_JS = r"""
(function(){
  var sec=document.querySelector('.model-section');
  if(!sec||sec.dataset.canSet!=='true')return;
  var edit=sec.querySelector('.model-edit');
  if(!edit||!edit.querySelector('.model-note'))return;
  var pick=edit.querySelector('.model-select'),why=edit.querySelector('.model-reason-input');
  var note=edit.querySelector('.model-note');
  function setNote(cls,text){note.className='approve-note model-note '+cls;
    note.textContent=text;note.hidden=false;}
  function send(model,btn){
    btn.disabled=true;
    fetch('/api/items/'+encodeURIComponent(sec.dataset.id)+'/model',{
      method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model:model,reason:why.value.trim()||null})
    }).then(function(r){return r.json().catch(function(){return {};})
        .then(function(j){return {ok:r.ok,body:j};});})
      .then(function(res){
        btn.disabled=false;
        if(res.ok){location.reload();return;}
        setNote('err',(res.body&&res.body.error)||'Couldn\'t change the model.');})
      .catch(function(){btn.disabled=false;setNote('err','Couldn\'t reach the server.');});}
  var set=edit.querySelector('.model-set');
  if(set&&pick)set.addEventListener('click',function(){
    var m=pick.value;
    if(!m){setNote('err','Choose a model first.');pick.focus();return;}
    send(m,set);});
  var clear=edit.querySelector('.model-clear');
  if(clear)clear.addEventListener('click',function(){send(null,clear);});
})();
"""


if __name__ == "__main__":
    main()


# --- Blockers ---------------------------------------------------------------
# A lane lifted out of the issue queue. It READS the same `all_items` list
# through `_active_in_lane`, and renders with the same `_card` the Issues
# page uses, so a change to how an issue is displayed reaches both or
# neither. A second card renderer is how two pages start describing the same
# row differently.

_LANE_COPY = {
    LANE_BLOCKERS: {
        "title": "Blockers",
        "eyebrow": "blockers",
        "h1": "Blocked on you",
        "one": "thing only you can do",
        "many": "things only you can do",
        "empty": "Nothing is waiting on you.",
        "note": ("An agent cannot finish these — they need a console login, a "
                 "credential, or a decision only you can make. They are excluded "
                 "from the drain by design, so nothing is working them in the "
                 "background. If one is here because it was mis-filed, re-file it "
                 "as <code>agent</code> and the queue will pick it up."),
    },
}


# Ids `_JS` looks up but tolerates being absent: the log-form IIFE opens with
# `if(!toggle)return`, so a page without the capture form degrades instead of
# throwing. EVERY OTHER id `_JS` dereferences must exist on every page that ships
# the script — see the test of the same name. Kept as an explicit short list
# rather than inferred, because "is this dereference guarded?" is not something a
# regex can answer honestly.
_JS_OPTIONAL_IDS = frozenset({"logForm", "logNote", "logToggle"})


def _lane_page(lane: str, cards_html: str, count: int, products) -> str:
    copy = _LANE_COPY[lane]
    # The ids below are not decoration: `_JS` dereferences #chips, #queue,
    # #waitCount and #empty without a null check, so a page that ships the script
    # and omits them throws on load and EVERY action button silently stops working.
    # The first cut of this page did exactly that — the cards rendered, "Won't do"
    # did nothing, and because Epics and Corrections both exclude this lane, this
    # page is the only UI path to resolve a Blocker. The structure here mirrors
    # `_epics_page()`/`_corrections_page()` for that reason. `_JS_REQUIRED_IDS`
    # and `test_every_page_shipping_the_js_has_the_ids_it_dereferences` pin it.
    chips = _chips(products, count)
    body = f'''<div class="ops">
  <header class="ops-head">
    <div class="eyebrow-line"><span class="dot"></span><a href="/engineering/issues">Epics</a> / {escape(copy["eyebrow"])}</div>
    <h1>{escape(copy["h1"])}</h1>
    <p class="lede"><span class="count" id="waitCount">{count}</span> {escape(copy["one"] if count == 1 else copy["many"])}.</p>
    <div class="chips" id="chips">{chips}</div>
  </header>
  <div class="wrap">
    <div class="queue" id="queue">{cards_html or f'<div class="allclear">{escape(copy["empty"])}</div>'}
      <div class="empty" id="empty">Nothing in this product.</div>
    </div>
    <div class="footnote">
      {copy["note"]}
      Signed in as <strong>__WHOAMI__</strong>.
    </div>
  </div>
  <div class="toast" id="toast"></div>
</div>
<script>{_JS}</script>'''
    return ds.page_shell(
        title=copy["title"],
        nav_html=ds.nav_component(f"/engineering/{lane}", "full"),
        body=body,
        extra_css=_CSS,
    )


def _live_lane(lane: str, scope: frozenset[str] = _ALL_PRODUCTS) -> str:
    """Render one lane. Counts come from the full matching set, never from
    len(rendered rows). `scope` (E19·2, issue_01M35K5AB4PRTJ71DMQW7Q4D3B) is
    applied to `all_items` before the lane split, so an out-of-scope
    project's items are absent from the count, the chips and the cards alike."""
    now = utcnow()
    all_items = library_client.list_issue_items()
    all_items = [it for it in all_items if _in_scope(it.product, scope)]
    active_matching = _active_in_lane(all_items, lane)
    total = len(active_matching)
    products = _product_counts(active_matching)
    active = active_matching[:_QUEUE_LIMIT]
    _attach_comments(active)
    cards = "".join(_card(it, now) for it in active)
    return _lane_page(lane, cards, total, products)


def live_html_blockers(scope: frozenset[str] = _ALL_PRODUCTS) -> str:
    """/engineering/blockers — active issues only the operator can finish."""
    return _live_lane(LANE_BLOCKERS, scope)
