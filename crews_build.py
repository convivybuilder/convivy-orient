#!/usr/bin/env python3.11
"""The Crews page — /crews (epic issue_01M08YM8MTNSTAEG1NV7E2GYRV, "One page
answers what every crew is doing right now, and where they are in it").

The operator's full spec (2026-08-20 broadening, epic comment): per project, roster,
utilization, cost, recent activity with dispatch detail, Course progress, and
live Bosun state, combined with the Chart House's own ledgers. "This page
overlaps Chart House, Dashboards and the knowledge base on purpose — a
different lens on the same information, not a second store of it."

Four quarters shipped here, all buildable without a Library change:

- **Roster**: `roster_build.load_roster()`, unchanged — reused, not
  reimplemented, so this page can never disagree with /engineering/roster
  about who's dispatchable where.
- **Where they are in the work**: `library_client.list_issue_items()`,
  grouped by product — active count (open + claimed) and how many of those
  carry a linked PR (`refs.pr_url`).
- **Held leases**: `library_client.list_leases()` (new — see library_client.py),
  joined to issue items by root_id for product attribution, since a lease
  itself carries no product column (the Library scopes it through the root
  item, deliberately — see `LeaseRecord`'s docstring). Three states, not two
  (issue_01M1F1F8M4TZ0FN917DGB1Q62Q): dead is rendered as its own state, not
  filtered out — "nothing holds this" and "something abandoned this" are
  different answers, per the epic's own wording — and a live lease running
  low on time is split further into "near expiry" versus comfortably "alive"
  (`_lease_state`), so a crew about to lose its hold is visible before it
  does, not just after — judged against the served `LeasePolicy`
  (`list_leases()`'s own envelope, alongside the rows) rather than a
  hardcoded grace/max-lease guess, since those are Library tunables that
  move.
- **Cost**: reuses `chart_house_build`'s rollup and alias table directly
  (`_cost_rollup`/`_cost_for`/`_COST_ALIASES`) rather than recomputing —
  one source of truth for "what a project's runs cost", read through two
  lenses.
- **Refusals**: `library_client.list_lease_refusals()` (new), the fourth
  item of the epic's original spec and the one it said had to be asked of
  the Library first. It was: `GET /lease-refusals` shipped 2026-08-22 and
  this page's footer went on saying no route existed. Rendered as a count
  per crew AND as the collisions themselves, because the epic's reason for
  wanting them is "who collided with whom" — a count names nobody, and two
  crews repeatedly blocking each other is invisible in a number and obvious
  in a list.

Crew identity is `roster_build.discover_projects()`'s own self-updating list
— the same set that already answers "who has a dispatchable crew" for
/engineering/roster — rather than `project_build.KNOWN_PROJECTS` (a
narrower, hand-maintained area list Chart House uses for "which repos read
as a project"). The two lists disagree at the edges (roster's discovery adds
`CFO`, `library`, `selector-backend`, the `*-commercial` sandboxes;
KNOWN_PROJECTS has `keel`, `platform`, `company`, none of which have their
own `.claude/agents` crew) — deliberate, not a bug: this page is about who
can be DISPATCHED, which is roster's question, not Chart House's.

**Live Bosun state** (the operator reading /crews at their screen, 2026-09-02) is now
built: `library_client.list_live_runs()` reads `GET /runs`, which the
Library serves live — `{"runs": [...], "available": bool, "reason":
str | None, "generated_at": str | None, "stale_after_seconds": float,
"count": int}`. `generated_at` is null whenever the register did not supply
one — on every degraded answer (`reason` names which failure it was) and
also on a successful answer whose payload omits the key — so this page
null-checks it regardless of what `available` says, and renders a degraded
answer as degraded, naming `reason`, never as "no runs". A per-crew "in
flight" count on each row and the full list under "Live Bosun state" both
read from this one call. **This is dispatched-run state, not co-session
state** — see the footnote on the page itself and item 1c of
issue_01M08YM8MTNSTAEG1NV7E2GYRV's 2026-09-02 comment: ADR-0050 keeps
Orient off the Bosun's control channel, so a working CO session (as opposed
to a run it dispatched) is not visible here today.

**Course progress is NOT rendered here, and that is settled rather than
pending** (the operator, 2026-09-03: "Isn't course progress already covered in the
Chart house page? I don't think we need it here"). It is — `/chart-house`
renders one row per live Course with its steps classified done / approved /
proposed / unspecified, a counts line, and a state chip separating "awaits
your sign-off" from "underway" from "running uncharted". Checked before
removing the panel: six products carry a live Course (`bosun`,
`convivy-deck`, `convivy-orient`, `library`, `selector`, `vivy`) and the
Chart House renders all six, so nothing would be added here that is not
already answered there.

**A second reason, which is the one that would have bitten.** A Course's
`product` key is not this page's crew slug. The Bosun's Course is keyed
`bosun` while `/crews` keys the crew `agent-platform` — the same divergence
`chart_house_build._BEAD_PROJECT_ALIASES` exists for. Joining Courses here
would silently drop the Bosun until someone copied that alias table, and a
second copy of an alias table is how one goes stale.

This page carried an `awaiting_state` panel claiming Orient needed "a client
for GET /courses" until 2026-09-03. **The claim was false the whole time** —
`library_client.list_courses` and `get_course` exist and both
`chart_house_build` and `issue_build` consume them. Its probe read
`GET /courses -> 200`, which confirms the LIBRARY serves the route and says
nothing about whether Orient has a client, so it returned 200 either way and
made a false claim look verified. **A probe of a Library route cannot check
an `owner="orient"` claim**; the shape that works is `contradicted_by`,
holding the value that would falsify the claim at render time, as
`security_build` does.

**Sessions and runs, in flight and past** (issue_01M15C5W1GFGC81CW7YNCB0CP9,
The operator 2026-08-29): now built, and it turned out to need neither a Library nor
an agent-platform client — the two gaps above were about `GET /runs` (the
Bosun's live in-memory state) and `GET /courses`; this is a different
question, "what actually ran," which was already sitting in
`~/Convivy/agent-runs/<date>/{run-*,co-*}/{meta,cost,outcome}.json` the whole
time (measured against the live corpus 2026-08-25..2026-08-29, issue thread
comment 1354 — 1007 session records, 960 run records, every field this page
uses present on every record it reads). Each crew row that dispatched a run
in the last `RUNS_DEFAULT_WINDOW_DAYS` days gets an expander: session
context (role, start, model — or "not recorded" when a field genuinely
isn't captured), then every run underneath it with the epic's six fields —
agent, what it was dispatched for, duration, model, cost, and what it did.
Capped per crew (`RUNS_PANEL_LIMIT`) with a link to `/crews/runs`, the
click-through to the same list over a caller-chosen window
(`from`/`to`, one crew at a time, clamped to `RUNS_MAX_WINDOW_DAYS` so a
hand-typed range can't force an unbounded directory scan).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict
from html import escape
from pathlib import Path
from urllib.parse import quote

import chart_house_build as ch
import crew_registry
import design_system as ds
import library_client
import orient_time
import project_build
import roster_build
from paths import STATE_ROOT

OUT = STATE_ROOT / "site"

_logger = logging.getLogger(__name__)

# --- sessions and runs, read directly from local run directories ------------
#
# issue_01M15C5W1GFGC81CW7YNCB0CP9: "the Crews page is supposed to show
# sessions, subagent runs including tasks and costs, both currently in flight
# and past." Measured against the live corpus 2026-08-25..2026-08-29
# (issue thread, comment 1354) before writing any of this:
#
# - Every co session writes `~/Convivy/agent-runs/<date>/co-<id>/meta.json`
#   (`kind: "co-session"`), nine fields, all present on all 1007 records
#   sampled. `ended_at` does not exist on ANY of them — a session's duration
#   is `heartbeat_at - started_at`, a LOWER BOUND, never rendered as a
#   finished duration. `model` is empty on 980/1007.
# - Every run writes `~/Convivy/agent-runs/<date>/run-*/{meta,cost,outcome}.json`.
#   `cost.json` and `outcome.json` are ABSENT (not empty) while a run is still
#   in flight — `ended_at: null` on `meta.json` is the in-flight signal, and
#   the other two files simply don't exist yet.
# - A run's own `meta.repo` is the crew attribution (e.g. "selector",
#   "convivy-orient") — authoritative, unlike a session's `product` field,
#   which is only the basename of `cwd` (a worktree slug for a dispatched
#   agent, not a clean product name).
# - The join from a run to its dispatching session is `meta.co_session`,
#   present on every run. A session that started before the read window
#   simply won't resolve — rendered as "session not recorded", never as a
#   dropped run.
#
# Local file reads only, no `library_client` addition: this data isn't the
# Library's, and dashboard_build/chart_house_build already read this same
# tree the same way for cost.
#
# **Bounded, on purpose.** ~200 sessions and ~190 runs land per day, so
# reading the whole corpus on every render doesn't scale. `_date_dirs` only
# ever opens the date directories inside the requested window, never walks
# `AGENT_RUNS_ROOT` itself.
AGENT_RUNS_ROOT = Path.home() / "Convivy" / "agent-runs"
WORKTREES_ROOT = Path.home() / "Convivy" / "worktrees"
CONVIVY_ROOT = Path.home() / "Convivy"

# The recent slice shown inline on /crews, and this page's own default when
# no explicit range is given.
RUNS_DEFAULT_WINDOW_DAYS = 3
# A hand-typed custom range is clamped here — the read stays bounded no
# matter what a URL asks for.
RUNS_MAX_WINDOW_DAYS = 30
# Runs shown per crew panel on /crews itself before "N more — view all".
RUNS_PANEL_LIMIT = 12
# Runs shown on /crews/runs (one crew, a chosen window) before truncating.
RUNS_PAGE_LIMIT = 300

# This page is about CREWS, not repos, and the two names diverge (the operator reading
# /crews, 2026-09-02: "I see... the Bosun CO is working, but... the
# 'agent-platform' (which we need to change to Bosun) line"). ADR-0042's own
# crew table (`platform/decisions/0042-crews-not-projects-and-the-keel-splits`)
# is the source: crew `bosun` = repo `agent-platform`, `library` = Library,
# `convivy-orient` = Orient, `substrate` = Steward. `roster_build.load_roster()`'s
# `label` is the REPO's display name — right for /engineering/roster, which is
# a repo view — so this overrides it here rather than renaming
# `roster_build.PROJECTS` itself, which would relabel the repo view too.
_CREW_NAME_OVERRIDES = {
    "agent-platform": "Bosun",
    "library": "Library",
    "convivy-orient": "Orient",
    "substrate": "Steward",
}


def _read_json(path: Path) -> dict | None:
    """None on anything that means "not there yet" — a run mid-flight has no
    cost.json or outcome.json at all, which is a fact to render, not an
    error to raise.

    Also None on a syntactically valid JSON value of the wrong shape (a
    list, `null`, a bare string): these files are written by many
    independent dispatch processes and validated against no schema before
    this page reads them, so a wrong-shaped record is as unusable as a
    missing one, and treating it that way keeps one bad file from taking
    down every crew's panel via AttributeError on the next `.get()`.

    A missing file is the ordinary in-flight case (no cost.json/outcome.json
    yet) and isn't logged. An existing-but-corrupt or wrong-shaped file is a
    write nothing should ever produce, so it's logged server-side — never on
    the page itself — to keep a future writer bug observable rather than
    silently indistinguishable from "nothing there"."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        _logger.warning("crews_build: %s is not readable JSON (%s) — treated as absent", path, exc)
        return None
    if not isinstance(value, dict):
        _logger.warning("crews_build: %s parsed to %s, not a dict — treated as absent",
                        path, type(value).__name__)
        return None
    return value


def _date_dirs(since: dt.date, until: dt.date) -> list[Path]:
    """Every existing date directory in [since, until], oldest first. This is
    the bound: only the days actually asked for are opened, never a walk of
    AGENT_RUNS_ROOT.

    Breaks before incrementing past `until` when `until` is `date.max` —
    `d += timedelta(days=1)` would otherwise raise OverflowError on the final
    iteration, reachable by any full-tier caller via `?to=9999-12-31`."""
    out = []
    d = since
    while d <= until:
        p = AGENT_RUNS_ROOT / d.isoformat()
        if p.is_dir():
            out.append(p)
        if d == dt.date.max:
            break
        d += dt.timedelta(days=1)
    return out


def _parse_date(value: str | None) -> dt.date | None:
    """`fromisoformat` only ever produces a year in `[MINYEAR, MAXYEAR]` —
    exactly `date.min`/`date.max`'s range — so it raises `ValueError` for
    every malformed or out-of-range string, never `OverflowError`. The real
    protection against boundary dates is `_sub_days_clamped` and the
    pre-increment break in `_date_dirs`, both covered by direct tests."""
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _sub_days_clamped(d: dt.date, days: int) -> dt.date:
    """`d - timedelta(days=days)`, clamped to `date.min` instead of raising
    OverflowError when `d` is near the boundary — reachable via `?to=` from
    any full-tier caller (e.g. `?to=0001-01-01`)."""
    try:
        return d - dt.timedelta(days=days)
    except OverflowError:
        return dt.date.min


def resolve_window(query: dict | None) -> tuple[dt.date, dt.date, bool]:
    """(since, until, is_custom) from `from`/`to` query params, or the
    default recent slice when neither is given. Clamped to
    RUNS_MAX_WINDOW_DAYS regardless of what a hand-edited URL asks for, so
    the read this drives stays bounded."""
    query = query or {}
    today = dt.datetime.now(dt.timezone.utc).date()
    since = _parse_date(query.get("from"))
    until = _parse_date(query.get("to"))
    if since is None and until is None:
        return _sub_days_clamped(today, RUNS_DEFAULT_WINDOW_DAYS - 1), today, False
    until = until or today
    since = since or _sub_days_clamped(until, RUNS_DEFAULT_WINDOW_DAYS - 1)
    if since > until:
        since, until = until, since
    if (until - since).days > RUNS_MAX_WINDOW_DAYS:
        since = _sub_days_clamped(until, RUNS_MAX_WINDOW_DAYS)
    return since, until, True


def _str_field(d: dict, key: str) -> str | None:
    """`d[key]` if it's actually a string, else None — a wrong-typed field is
    as unusable as a missing one, and treating it that way is what keeps a
    future writer bug from reaching a `.get()`, hash, or `len()` call
    downstream that assumes the type `_read_json`'s dict check doesn't reach
    (an `AttributeError`/`TypeError` one level below the file's own shape).
    Logged server-side only when the field was actually present with the
    wrong type — never for the ordinary case of a field simply absent."""
    v = d.get(key)
    if v is None or isinstance(v, str):
        return v
    _logger.warning("crews_build: field %r expected str, got %s — treated as absent",
                    key, type(v).__name__)
    return None


def _list_field(d: dict, key: str) -> list:
    v = d.get(key)
    if v is None:
        return []
    if isinstance(v, list):
        return v
    _logger.warning("crews_build: field %r expected list, got %s — treated as empty",
                    key, type(v).__name__)
    return []


def _normalize_run_meta(meta: dict) -> dict:
    return {
        **meta,
        "run_id": _str_field(meta, "run_id"),
        "repo": _str_field(meta, "repo"),
        "agent": _str_field(meta, "agent"),
        "label": _str_field(meta, "label"),
        "task": _str_field(meta, "task"),
        "model_resolved": _str_field(meta, "model_resolved"),
        "co_session": _str_field(meta, "co_session"),
        "started_at": _str_field(meta, "started_at"),
        "ended_at": _str_field(meta, "ended_at"),
    }


def _normalize_session_meta(meta: dict) -> dict:
    return {
        **meta,
        "kind": _str_field(meta, "kind"),
        "session_id": _str_field(meta, "session_id"),
        "cwd": _str_field(meta, "cwd"),
        "started_at": _str_field(meta, "started_at"),
        "heartbeat_at": _str_field(meta, "heartbeat_at"),
        "model": _str_field(meta, "model"),
    }


def _normalize_outcome(outcome: dict) -> dict:
    return {
        **outcome,
        "exit_status": _str_field(outcome, "exit_status"),
        "prs_opened": _list_field(outcome, "prs_opened"),
        "files_modified": _list_field(outcome, "files_modified"),
    }


def _load_session_records(date_dirs: list[Path]) -> dict[str, dict]:
    """{session_id: meta} for every co-session directory in the window. A run
    whose dispatching session started before the window just won't resolve
    here — the caller renders that as "session not recorded"."""
    out: dict[str, dict] = {}
    for dd in date_dirs:
        for d in sorted(dd.glob("co-*")):
            meta = _read_json(d / "meta.json")
            if meta is None:
                continue
            meta = _normalize_session_meta(meta)
            if meta.get("kind") == "co-session" and meta.get("session_id"):
                out[meta["session_id"]] = meta
    return out


def _load_run_records(date_dirs: list[Path]) -> list[dict]:
    """One dict per run directory in the window: `meta.json` plus whatever
    `cost.json`/`outcome.json` exist (None, not {}, when they don't — an
    in-flight run's absence of them is meaningful and must survive to the
    render)."""
    out = []
    for dd in date_dirs:
        for d in sorted(dd.glob("run-*")):
            meta = _read_json(d / "meta.json")
            if meta is None:
                continue
            meta = _normalize_run_meta(meta)
            if not meta.get("run_id"):
                continue
            outcome = _read_json(d / "outcome.json")
            out.append({
                "meta": meta,
                "cost": _read_json(d / "cost.json"),
                "outcome": _normalize_outcome(outcome) if outcome is not None else None,
            })
    return out


def _elapsed_seconds(start, end) -> float | None:
    s = orient_time.to_mst(start)
    e = orient_time.to_mst(end)
    if s is None or e is None:
        return None
    return max((e - s).total_seconds(), 0.0)


def _clip(text: str | None, n: int = 72) -> str | None:
    """A task is a paragraph; a label is a word. Clip either to one line
    rather than letting it set the row height (issue thread, comment 1354,
    "clip it rather than letting it set the row height")."""
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _clippable(text: str | None, n: int = 72) -> str:
    """`text`, clipped to `n` chars via `_clip`, as a native `<details>` that
    expands to the full text in place on click when it was actually clipped —
    plain escaped text otherwise. Nothing here throws the rest of the string
    away the way a bare `_clip()` call used to (the operator, reading /crews,
    2026-09-02: "there needs to be a way to click the ellipsis and expand the
    full text"). Both states are escaped; a task is free-form text that can
    contain anything a command line can.

    A `<details>` growing taller than one line inside a CSS grid cell does not
    break the grid's column tracks — track widths are fixed independently of
    a cell's content height, so an open `.cr-clip` only grows that row, never
    a neighboring column (checked against `.cr-run`'s and `.cr-row`'s actual
    grid rules, both fixed-track; see `_CSS`'s `align-items:start` on those
    rows, chosen over `baseline` for exactly this reason)."""
    if not text:
        return "—"
    clipped = _clip(text, n)
    if clipped == text:
        return escape(text)
    return (f'<details class="cr-clip"><summary>{escape(clipped)}</summary>'
            f'<div class="cr-clip-full">{escape(text)}</div></details>')


def _fmt_ago(seconds: float | None) -> str:
    """A duration since some past moment, at day/hour/minute granularity —
    "4m", "2h13m", "3d" — never finer, since a reader scanning "last
    activity" across a table of crews wants the shape of the gap, not its
    exact second. Em dash for "nothing known" (never "0s" — a crew this page
    has no timestamp for at all is a different fact from one active a moment
    ago, and `_last_activity_by_crew` already keeps that distinction; this
    only formats what it's given)."""
    if seconds is None:
        return "—"
    seconds = max(int(round(seconds)), 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _rem = divmod(rem, 60)
    if days:
        return f"{days}d"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return "just now"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _session_crew_and_role(cwd: str | None) -> tuple[str | None, str]:
    """(crew_slug, role) from a session's `cwd`. `cwd` is what separates a
    dispatched agent from the human co session that dispatched it, exactly
    (issue thread, comment 1354): a dispatched agent sits under
    `~/Convivy/worktrees/<slug>.<rest>`, a human session sits directly under
    `~/Convivy/<slug>`. Neither shape -> (None, "other")."""
    if not cwd:
        return None, "other"
    wt_prefix = str(WORKTREES_ROOT) + "/"
    if cwd.startswith(wt_prefix):
        rest = cwd[len(wt_prefix):]
        slug = rest.split("/", 1)[0].split(".", 1)[0]
        return (slug or None), "dispatched"
    home_prefix = str(CONVIVY_ROOT) + "/"
    if cwd.startswith(home_prefix):
        rest = cwd[len(home_prefix):]
        slug = rest.split("/", 1)[0]
        return (slug or None), "human"
    return None, "other"


_EXIT_LABELS = {
    # healthy
    "completed": "completed",
    "committed_not_pushed": "committed, not pushed",
    "committed_local_only": "committed, no remote",
    "superseded": "superseded",
    # attention — neither healthy nor failed; see activity_build.py's
    # ATTENTION_EXIT_LABELS for why primary_mutated isn't classed as failed
    "primary_mutated": "primary checkout changed",
    # failed
    "errored": "errored",
    "killed": "killed",
    "budget_refused": "budget refused",
    "capacity_refused": "capacity refused",
}


def _outcome_summary(outcome: dict | None, *, in_flight: bool) -> str:
    """What a run did, in one line. `outcome.json` doesn't exist yet for an
    in-flight run — that's "running", not "unknown"."""
    if in_flight:
        return "running"
    if outcome is None:
        return "no outcome recorded"
    status = outcome.get("exit_status")
    bits = [_EXIT_LABELS.get(status, status or "unknown")]
    prs = outcome.get("prs_opened") or []
    if prs:
        bits.append(f"{len(prs)} PR{'s' if len(prs) != 1 else ''}")
    files = outcome.get("files_modified") or []
    if files:
        bits.append(f"{len(files)} file{'s' if len(files) != 1 else ''}")
    return " · ".join(bits)


def _run_view(run: dict, sessions: dict[str, dict]) -> dict:
    """One run's meta+cost+outcome, normalized to exactly the six fields the
    epic asked for plus what's needed to join it to a session.

    `task` and `label` are carried RAW here, unclipped — clipping and the
    task-over-label display preference are `_dispatched_for_html`'s job
    (the operator, reading /crews, 2026-09-02: a bare dispatcher slug like "rev-170"
    was winning over the readable task sentence on every run that had both;
    counted against the live corpus, 2026-09-01..2026-09-02: label is never
    the ONLY source when both exist — 182 of 272 runs carry both, 90 carry
    task only, 0 carry label only — so preferring label was throwing away
    the readable sentence for no gain)."""
    meta = run["meta"]
    cost = run["cost"]
    outcome = run["outcome"]
    started_at = meta.get("started_at")
    ended_at = meta.get("ended_at")
    in_flight = not ended_at
    duration_s = None
    if started_at and not in_flight:
        duration_s = _elapsed_seconds(started_at, ended_at)
    elif started_at and in_flight:
        duration_s = _elapsed_seconds(started_at, orient_time.now())
    session_id = meta.get("co_session")
    return {
        "run_id": meta.get("run_id"),
        "agent": meta.get("agent") or "—",
        "task": meta.get("task"),
        "label": meta.get("label"),
        "started_at": started_at,
        "ended_at": ended_at,
        "in_flight": in_flight,
        "duration_s": duration_s,
        "model": meta.get("model_resolved") or "—",
        "cost_usd": cost.get("usd") if cost is not None else None,
        "cost_known": cost is not None,
        "outcome_text": _outcome_summary(outcome, in_flight=in_flight),
        "session_id": session_id,
        "session": sessions.get(session_id) if session_id else None,
    }


def _dispatched_for_html(rv: dict) -> str:
    """The "Dispatched for" cell: the task — a readable sentence — is
    primary, clipped with click-to-expand (`_clippable`); the label — the
    terse slug `agentctl` and the dashboard call this work unit by — rides
    along as a small secondary handle rather than vanishing. A record with
    no task at all falls back to showing the label as primary, since that's
    genuinely all there is to show."""
    task = rv["task"]
    label = rv["label"]
    if task:
        secondary = f'<span class="cr-run-secondary">{escape(label)}</span>' if label else ""
        return f'<span class="cr-run-primary">{_clippable(task)}</span>{secondary}'
    if label:
        return f'<span class="cr-run-primary">{escape(label)}</span>'
    return '<span class="cr-run-primary">—</span>'


def _group_runs_by_crew(runs: list[dict], sessions: dict[str, dict]) -> dict[str, list[dict]]:
    """{repo: [run_view, ...]}, newest first. Attribution is a run's OWN
    `meta.repo` — authoritative — never a session's `cwd`, since one session
    can in principle dispatch runs against more than one repo."""
    out: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        repo = run["meta"].get("repo")
        if not repo:
            continue
        out[repo].append(_run_view(run, sessions))
    for views in out.values():
        views.sort(key=lambda v: v["started_at"] or "", reverse=True)
    return dict(out)


def _live_runs_by_crew(runs: list) -> dict[str, list]:
    """{repo: [LiveRunRecord, ...]} from the Bosun's live register
    (`library_client.list_live_runs()`) — the "runs on foot right now"
    column and panel, a different question from `_group_runs_by_crew`'s
    local dispatch history."""
    out: dict[str, list] = defaultdict(list)
    for run in runs:
        if run.repo:
            out[run.repo].append(run)
    return dict(out)


def _sessions_by_crew(sessions: dict[str, dict]) -> dict[str, list[dict]]:
    """{crew_slug: [session_meta, ...]} via `_session_crew_and_role`'s own
    `cwd`-based split — the same join `_session_line` uses per-run, reused
    here so "last activity" can look at every session's heartbeat, not just
    the ones a run happens to reference."""
    out: dict[str, list[dict]] = defaultdict(list)
    for meta in sessions.values():
        slug, _role = _session_crew_and_role(meta.get("cwd"))
        if slug:
            out[slug].append(meta)
    return dict(out)


def _last_activity_by_crew(slugs, *, live_runs_by_crew: dict[str, list],
                           runs_by_crew: dict[str, list[dict]],
                           sessions_by_crew: dict[str, list[dict]],
                           now: dt.datetime) -> dict[str, dt.datetime | None]:
    """{slug: most-recent-activity-datetime-or-None}, the max of three
    signals (the operator, reading /crews, 2026-09-02): a live in-flight run (activity
    is NOW — `_run_view`/`_group_runs_by_crew`'s window bounds the other two,
    so this is the one signal that can't just be read off a timestamp), the
    newest local run record's `ended_at`/`started_at` in that same window,
    and the newest session heartbeat in the window.

    **`heartbeat_at` is the wrong signal for "is a crew live right now"**
    (issue thread: it freezes on the `Stop` hook, so it calls every working
    session stale) **and the right one for "when did something last happen"**,
    which is what this column asks — keep that distinction if this ever gets
    reused for a liveness check, which it must not become one of."""
    out: dict[str, dt.datetime | None] = {}
    for slug in slugs:
        candidates: list[dt.datetime] = []
        if live_runs_by_crew.get(slug):
            candidates.append(now)
        for rv in runs_by_crew.get(slug, []):
            ts = orient_time.to_mst(rv.get("ended_at") or rv.get("started_at"))
            if ts is not None:
                candidates.append(ts)
        for meta in sessions_by_crew.get(slug, []):
            ts = orient_time.to_mst(meta.get("heartbeat_at"))
            if ts is not None:
                candidates.append(ts)
        out[slug] = max(candidates) if candidates else None
    return out


# --- data ---------------------------------------------------------------------


def _work_by_crew(items: list) -> dict[str, dict]:
    """{product: {"active": n, "in_review": n}} — active is open + claimed;
    in_review is the subset of those carrying a linked PR (`refs.pr_url`),
    the cheapest available proxy for "where in the work" without a second
    state-machine reimplementation. `items` is unfiltered by parent type, so
    full detail is one click away at /engineering/issues (Epics) and
    /engineering/corrections (Corrections), both filtered, since the
    Epics/Corrections split (PR #232) moved corrections off the former page."""
    out: dict[str, dict] = defaultdict(lambda: {"active": 0, "in_review": 0})
    for item in items:
        if item.state not in ("open", "claimed"):
            continue
        bucket = out[item.product]
        bucket["active"] += 1
        if (item.refs or {}).get("pr_url"):
            bucket["in_review"] += 1
    return dict(out)


# A lease with this little time left is marked "near expiry" instead of
# merely counted as alive (issue_01M1F1F8M4TZ0FN917DGB1Q62Q). There is no
# fleet history to calibrate this from — every lease on this machine measured
# while writing this was already dead, none near-expiry — so the number is a
# starting guess: long enough that a reader checking /crews every few minutes
# sees the warning before the lease actually dies, short enough that it
# doesn't fire hours in advance for every held lease. Revisit once a real
# near-expiry lease has actually been observed against it. This is the
# RUNWAY CUTOFF — "how soon do we warn" — a different question from
# LEASE_HEARTBEAT_STALE_MINUTES below ("how long since a check-in counts as
# quiet"); see `_lease_state` for why the two are not interchangeable.
LEASE_NEAR_EXPIRY_MINUTES = 15

# How long since a heartbeat before it stops excusing a short runway on the
# GRACE arm (see `_grace_arm_binds`). Kept as its own constant, not reused
# from LEASE_NEAR_EXPIRY_MINUTES above, even though both start at 15: a lease
# with `remaining <= LEASE_NEAR_EXPIRY_MINUTES` under the grace arm implies
# `heartbeat_age >= grace - LEASE_NEAR_EXPIRY_MINUTES`, which is only ever
# `< LEASE_NEAR_EXPIRY_MINUTES` itself when grace is tuned below double that
# cutoff — at the shipped 4-hour default the two constants sharing one name
# would silently never both matter on the same lease.
LEASE_HEARTBEAT_STALE_MINUTES = 15


def _grace_arm_binds(lease, policy: library_client.LeasePolicy | None) -> bool:
    """Whether the GRACE arm of the Library's `expires_at = min(heartbeat_at
    + grace, taken_at + max_lease)` is what actually set this lease's expiry
    — the only arm a fresh heartbeat can excuse, since the max-lease arm is a
    hard cap from checkout that the holder's activity cannot move.

    Computed independently from `policy` and the lease's own timestamps
    rather than trusted from `expires_at` alone, because `expires_at` on the
    wire doesn't say which arm produced it.

    Returns `False` — meaning "do not suppress, warn" — whenever the policy
    or either timestamp needed to tell the arms apart is missing. That is a
    deliberate asymmetry: a wrongly suppressed warning is the exact failure
    this page exists to catch (a lease dying quietly while marked healthy), a
    wrongly shown one is merely a false alarm, so an unresolvable case must
    fail toward the warning.
    """
    if policy is None or lease.taken_at is None or lease.heartbeat_at is None:
        return False
    grace_end = lease.heartbeat_at + dt.timedelta(seconds=policy.grace_seconds)
    max_end = lease.taken_at + dt.timedelta(seconds=policy.max_lease_seconds)
    return grace_end <= max_end


def _lease_state(lease, now: dt.datetime,
                  policy: library_client.LeasePolicy | None = None) -> tuple[str, float | None]:
    """(state, remaining_seconds) for one lease at `now` — one of "dead",
    "near_expiry" or "alive". `remaining_seconds` is only set for
    "near_expiry" (clamped to >=0, for display); the other two states carry
    `None`.

    `now` and both lease timestamps are naive UTC, matching the Library's own
    `lease.is_dead()` convention and what `library_client._iso` already
    produces — so this never touches a timezone.

    A dead lease is dead regardless of its clock. `expires_at` of `None` on a
    non-dead lease is unexpected — the Library's own `is_dead()` already
    treats a lease whose `taken_at`/`heartbeat_at` can't be parsed as dead
    server-side, so a genuinely live row should always carry a derivable
    expiry — but it must not crash and must not read as urgent for a TTL this
    page cannot see, so it renders as ordinary "alive" (logged, since it's a
    row shape nothing should produce).

    **Why `heartbeat_at` alone is not enough.** `expires_at` on the wire is
    already `min(heartbeat_at + grace, taken_at + max_lease)` — so a short
    runway alone doesn't say WHY: it reads identically whether the holder has
    gone quiet on the GRACE arm, or is pinned against the immovable MAX_LEASE
    cap while being actively heartbeated. Only the first is the false alarm
    this guard exists to prevent (`_grace_arm_binds`, checked with `policy`
    against the served tunables); the second is unambiguous no matter how
    fresh the heartbeat is, because the cap does not move for it. A previous
    version of this function suppressed on heartbeat freshness alone and, at
    the shipped defaults (grace=4h, max_lease=24h), that suppressed every
    near-expiry max-lease case and none of the grace-arm ones it was written
    for — exactly backwards.
    """
    if lease.dead:
        return "dead", None
    if lease.expires_at is None:
        _logger.warning("crews_build: live lease %s has no expires_at", lease.id)
        return "alive", None
    remaining = (lease.expires_at - now).total_seconds()
    if remaining > LEASE_NEAR_EXPIRY_MINUTES * 60:
        return "alive", None
    if _grace_arm_binds(lease, policy) and lease.heartbeat_at is not None:
        heartbeat_age = (now - lease.heartbeat_at).total_seconds()
        if heartbeat_age < LEASE_HEARTBEAT_STALE_MINUTES * 60:
            return "alive", None
    return "near_expiry", max(remaining, 0.0)


def _leases_by_crew(leases: list, items: list, *,
                     policy: library_client.LeasePolicy | None = None,
                     now: dt.datetime | None = None) -> dict[str, dict]:
    """{product: {"alive": n, "near_expiry": [remaining_seconds, ...], "dead": n}},
    joined via each lease's root_id — a lease carries no product of its own
    (see LeaseRecord's docstring). Three states, not two: dead already read as
    distinct from absent; near_expiry is now split out of what used to be a
    single undifferentiated "live" count, per `_lease_state`'s reasoning.

    `policy` is threaded straight through to `_lease_state` — the served
    `LeasePolicy` from the same `list_leases()` call `leases` came from, or
    `None` when the Library's response didn't carry one, which `_lease_state`
    treats as "warn rather than suppress."

    `now` is a parameter (defaulting to the real current time) so a caller —
    this module's own tests included — can pin the clock a near-expiry lease
    is judged against, rather than the render racing a wall-clock deadline."""
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if policy is None:
        _logger.warning("crews_build: no lease policy served — every short-runway "
                         "lease this render warns as near_expiry, since grace-arm "
                         "suppression can't be computed without it. Check the "
                         "Library's GET /leases response for a `policy` object "
                         "carrying integer grace_seconds and max_lease_seconds")
    product_of = {item.id: item.product for item in items}
    out: dict[str, dict] = defaultdict(lambda: {"alive": 0, "near_expiry": [], "dead": 0})
    for lease in leases:
        product = product_of.get(lease.root_id)
        if product is None:
            continue
        state, remaining = _lease_state(lease, now, policy)
        if state == "near_expiry":
            out[product]["near_expiry"].append(remaining)
        else:
            out[product][state] += 1
    return dict(out)


def _refusals_by_crew(refusals: list) -> dict[str, int]:
    """{product: n}. A refusal names its own product, so unlike `_leases_by_crew`
    this needs no join — see `LeaseRefusalRecord`'s docstring. A refusal whose
    product is null is counted nowhere rather than under a placeholder crew: the
    collision list below still shows it, so it is visible without inventing an
    attribution the row does not carry."""
    out: dict[str, int] = defaultdict(int)
    for refusal in refusals:
        if refusal.product:
            out[refusal.product] += 1
    return dict(out)


def _visible_repos(scope: frozenset[str], whole_fleet: bool) -> frozenset[str] | None:
    """The repo names (roster_build's own project-slug identity, e.g.
    "agent-platform" rather than the registry's "bosun") the viewer's scope
    reaches — `None` means every repo, for a `whole_fleet` viewer (E19·4,
    issue_01M35K5JQ7Z7MHYZGWRSB5F85M).

    This page's "crew" is a repo, not a registry project slug — the same gap
    `crew_registry.repos_in_scope`'s own docstring names (`agent-platform`
    belongs to project `bosun`; `convivy-app-kit`/`convivy-services` to `svc`)
    — so that function, which already resolves a project slug the Library
    returns to the repo names a project unit lists, is reused rather than a
    second alias table grown here."""
    if whole_fleet:
        return None
    return crew_registry.repos_in_scope(scope)


def _roster_counts(scope: frozenset[str], whole_fleet: bool) -> dict[str, dict]:
    """{slug: {"label": str, "agents": n}} from roster_build's own payload —
    reused, not reimplemented, so this page can't disagree with the roster
    page about who's dispatchable.

    A roster project whose slug is not a repo the viewer's scope reaches
    (`_visible_repos`) is dropped before any row is built for it (E19·4) —
    the same "filter before grouping" rule `issue_build.live_html` applies."""
    roster = roster_build.load_roster()
    visible = _visible_repos(scope, whole_fleet)
    out = {}
    for proj in roster["projects"]:
        if visible is not None and proj["slug"] not in visible:
            continue
        agent_count = sum(len(lane["agents"]) for lane in proj["lanes"])
        label = _CREW_NAME_OVERRIDES.get(proj["slug"], proj["label"])
        out[proj["slug"]] = {"label": label, "agents": agent_count}
    return out


# --- render ---------------------------------------------------------------------


def _cell(value, empty="—") -> str:
    return escape(str(value)) if value else empty


# The crew table's columns, in render order — one place both the header row
# (`_crew_table_head_html`) and each data row (`_row`) draw from, so a column
# can't be added to one and forgotten in the other. `key` is what a header
# button's `data-col` and the click-to-sort JS resolve by index rather than
# by name (simpler than a name-keyed lookup client-side, and the index is
# fixed by this same list at render time either way).
_CREW_COLUMNS = ("name", "last_activity", "in_flight", "roster", "work",
                 "leases", "refused", "cost")


def _row(slug: str, label: str, agents: int, work: dict | None,
         leases: dict | None, cost: dict | None, refusals: int = 0, *,
         in_flight: int = 0, last_activity: dt.datetime | None = None,
         now: dt.datetime | None = None) -> str:
    active = work["active"] if work else 0
    in_review = work["in_review"] if work else 0
    work_cell = f"{active} active" + (f" · {in_review} in review" if in_review else "") \
        if active else "—"
    lease_cell = "—"
    lease_total = 0
    if leases:
        lease_total = leases["alive"] + len(leases["near_expiry"]) + leases["dead"]
        parts = []
        if leases["near_expiry"]:
            times = ", ".join(_fmt_duration(s) for s in sorted(leases["near_expiry"]))
            n = len(leases["near_expiry"])
            parts.append(f'<span class="cr-lease-near">{n} near expiry '
                        f'({escape(times)})</span>')
        if leases["alive"]:
            parts.append(f'<span class="cr-lease-alive">{leases["alive"]} alive</span>')
        if leases["dead"]:
            parts.append(f'<span class="cr-lease-dead">{leases["dead"]} dead</span>')
        if parts:
            lease_cell = " · ".join(parts)
    cost_usd = cost["cost_usd"] if cost else 0.0
    cost_cell = f"${cost['cost_usd']:,.2f} · {cost['runs']} run{'s' if cost['runs'] != 1 else ''}" \
        if cost else "—"
    href = f"/projects/{slug}" if slug in project_build.KNOWN_PROJECTS else None
    name_html = (f'<a href="{escape(href, quote=True)}">{escape(label)}</a>'
                if href else escape(label))
    now = now or orient_time.now()
    # Empty (never "0") is what makes a never-active crew sort last regardless
    # of direction — the click-to-sort JS treats an empty `data-sort` as
    # "unknown, always after every known value" for every column, not just
    # this one (the operator: "a crew with no activity sorts last, never first").
    activity_sort = f"{last_activity.timestamp():.0f}" if last_activity else ""
    activity_cell = _fmt_ago((now - last_activity).total_seconds()) if last_activity else "—"
    in_flight_cell = f"{in_flight} running" if in_flight else "—"
    return (f'<div class="cr-row" role="row">'
            f'<span class="cr-row-name" data-sort="{escape(label.lower(), quote=True)}">'
            f'{name_html}</span>'
            f'<span class="cr-row-cell" data-sort="{escape(activity_sort, quote=True)}">'
            f'{escape(activity_cell)}</span>'
            f'<span class="cr-row-cell" data-sort="{in_flight}">{escape(in_flight_cell)}</span>'
            f'<span class="cr-row-cell" data-sort="{agents}">{_cell(agents)}</span>'
            f'<span class="cr-row-cell" data-sort="{active}">{_cell(work_cell)}</span>'
            f'<span class="cr-row-cell" data-sort="{lease_total}">{lease_cell}</span>'
            f'<span class="cr-row-cell" data-sort="{refusals}">{_cell(refusals)}</span>'
            f'<span class="cr-row-cell" data-sort="{cost_usd}">{escape(cost_cell)}</span>'
            f'</div>')


def _crew_table_head_html() -> str:
    """The crew table's header — one `<button>` per column (native focus and
    Enter/Space activation, no `role`/`tabindex` needed), each carrying the
    `data-col` index the click-to-sort JS reorders `.cr-group`s by
    (`_CREW_SORT_JS`).
    "Last activity" is marked as the page's default sort so the arrow it
    shows on load matches the order `live_html` actually rendered rows in —
    JS reads that marker to initialize its own state without re-sorting
    already-correctly-ordered rows on page load."""
    labels = {"name": "Crew", "last_activity": "Last activity", "in_flight": "In flight",
              "roster": "Roster", "work": "Work", "leases": "Leases", "refused": "Refused",
              "cost": f"{ch.WINDOW_DAYS}d cost"}
    cells = []
    for i, key in enumerate(_CREW_COLUMNS):
        is_default = key == "last_activity"
        default_attr = ' data-default-dir="desc"' if is_default else ""
        arrow = "▾" if is_default else ""
        cls = "cr-sort-btn cr-sort-active" if is_default else "cr-sort-btn"
        cells.append(f'<button type="button" class="{cls}" data-col="{i}"{default_attr}>'
                    f'{escape(labels[key])}<span class="cr-sort-arrow">{arrow}</span></button>')
    return f'<div class="cr-row cr-row-head" role="row">{"".join(cells)}</div>'


def _session_line(run_view: dict) -> str:
    """The session context for a group of runs: role, when it started, and
    its model — or an honest "not recorded" when the join doesn't resolve,
    never a silently dropped run (issue thread, comment 1354)."""
    session = run_view["session"]
    if session is None:
        return "session not recorded" if run_view["session_id"] else "no session recorded"
    _, role = _session_crew_and_role(session.get("cwd"))
    role_label = {"dispatched": "dispatched agent", "human": "human session"}.get(role, "session")
    started = orient_time.fmt(session.get("started_at")) or "—"
    model = session.get("model") or "model not recorded"
    return f"{role_label} · started {started} · {model}"


def _run_row_html(rv: dict) -> str:
    started = orient_time.fmt(rv["started_at"]) if rv["started_at"] else "—"
    if rv["in_flight"]:
        dur = f"running ({_fmt_duration(rv['duration_s'])} so far)"
    else:
        dur = _fmt_duration(rv["duration_s"])
    if rv["in_flight"] and not rv["cost_known"]:
        cost = "in progress"
    elif not rv["cost_known"]:
        cost = "cost not recorded"
    elif isinstance(rv["cost_usd"], (int, float)):
        cost = f"${rv['cost_usd']:,.2f}"
    else:
        cost = "cost not recorded"
    return (f'<div class="cr-run">'
            f'<span class="cr-run-agent">{escape(rv["agent"])}</span>'
            f'<span class="cr-run-label">{_dispatched_for_html(rv)}</span>'
            f'<span class="cr-run-when">{escape(started)}</span>'
            f'<span class="cr-run-dur">{escape(dur)}</span>'
            f'<span class="cr-run-model">{escape(rv["model"])}</span>'
            f'<span class="cr-run-cost">{escape(cost)}</span>'
            f'<span class="cr-run-outcome">{escape(rv["outcome_text"])}</span>'
            f'</div>')


def _run_columns_head_html() -> str:
    """The run table's column headers — shared by the per-crew panel
    (`_runs_panel_html`) and the custom-window page (`runs_html`) so the two
    can never drift apart (the operator, reading /crews, 2026-09-02: the expanded
    panel had none at all)."""
    return ('<div class="cr-run cr-run-head">'
            '<span class="cr-run-agent">Agent</span>'
            '<span class="cr-run-label">Dispatched for</span>'
            '<span class="cr-run-when">Started</span>'
            '<span class="cr-run-dur">Duration</span>'
            '<span class="cr-run-model">Model</span>'
            '<span class="cr-run-cost">Cost</span>'
            '<span class="cr-run-outcome">Outcome</span>'
            '</div>')


def _runs_panel_html(slug: str, runs: list[dict], since: dt.date, until: dt.date) -> str:
    """The crew panel's expand-into-run-detail (issue_01M15C5W1GFGC81CW7YNCB0CP9):
    a native <details> — no JS needed — grouped by dispatching session so a
    session's cost and its runs read together, capped at RUNS_PANEL_LIMIT with
    a link to the unbounded custom-window page for the rest."""
    if not runs:
        return ""
    shown = runs[:RUNS_PANEL_LIMIT]
    order: list[str | None] = []
    grouped: dict[str | None, list[dict]] = {}
    for rv in shown:
        key = rv["session_id"]
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(rv)
    groups_html = "".join(
        f'<div class="cr-run-session">'
        f'<div class="cr-run-session-line">{escape(_session_line(grouped[key][0]))}</div>'
        + "".join(_run_row_html(rv) for rv in grouped[key])
        + '</div>'
        for key in order)
    more = len(runs) - len(shown)
    href = f"/crews/runs?product={escape(quote(slug), quote=True)}"
    more_html = (f'<div class="cr-run-more">{more} more in this window — '
                f'<a href="{href}">view all</a></div>') if more > 0 else ""
    window_label = f"{since.isoformat()} to {until.isoformat()}"
    return (f'<details class="cr-runs">'
            f'<summary>{len(runs)} run{"s" if len(runs) != 1 else ""} '
            f'({escape(window_label)})</summary>'
            f'<div class="cr-runs-body">{_run_columns_head_html()}{groups_html}{more_html}'
            f'<div class="cr-run-custom"><a href="{href}">custom time window &rarr;</a></div>'
            f'</div></details>')


def _collision(refusal) -> str:
    """One collision, as the sentence the epic wants: who was refused, by whom,
    and on what. The blocking ROOT is named rather than the requested item —
    that root is the subtree the fence covers, so it is what the two crews
    actually contended for, and it is the thing either of them can act on.

    **THE WITHHELD CASE IS SAID, NOT BLANKED** (reviewer, PR #200). When the
    blocking subtree is outside the reading credential's scope the Library omits
    the holder, the lease and the root, and sends `blocked: true` instead so the
    row still reads as "something held this". Rendering that through the normal
    template produces "blocked by — on —", which looks like a parsing fault
    rather than the privacy boundary it is. Orient's credential is `*`-scoped
    today so this shape does not arrive, and that is a fact about one token
    rather than a promise of the contract.

    **THE CAPTION SAYS WHERE THE FENCE SAT AND STOPS THERE.** Two earlier
    versions each claimed more than the row supports. The first read `blocks_by`
    as "an epic fenced a sibling", which is wrong because `live_conflicts` tags
    a lease on the requested item itself `"ancestor"` too. The second compared
    the blocking root against the requested item and called equality a race for
    the same work, which is wrong for the case it most needed to be right about:
    a leaf leases its PARENT, so two crews racing for the identical leaf never
    compare equal (reviewer, PR #200, both rounds).

    Whether the other crew wanted the same item is not on this row and is not
    inferred here — `issue_01M0RCF408K7TBE0N9VP224MGA` asks the Library for it.
    A reader who wants it today has the root and the requester on the line and
    can look. That is less than the page could say and all that is true.
    """
    when = orient_time.fmt(refusal.at, assume_utc=True) if refusal.at else "—"
    if refusal.blocked:
        return (f'<div class="cr-col">'
                f'<span class="cr-col-who">{escape(refusal.requester)}</span>'
                f'<span class="cr-col-arrow">blocked</span>'
                f'<span class="cr-col-on">by a crew outside this view, on '
                f'<code>{escape(refusal.requested_item_id)}</code></span>'
                f'<span class="cr-col-when">{escape(when)}</span>'
                f'</div>')
    root = refusal.blocking_root_id or "—"
    if refusal.fence_was_the_requested_item:
        by = " (the item itself)"
    elif refusal.blocks_by == "ancestor":
        by = " (which fences what it asked for)"
    elif refusal.blocks_by == "descendant":
        by = " (which sits under what it asked for)"
    else:
        by = ""
    return (f'<div class="cr-col">'
            f'<span class="cr-col-who">{escape(refusal.requester)}</span>'
            f'<span class="cr-col-arrow">blocked by</span>'
            f'<span class="cr-col-who">{escape(refusal.blocking_holder or "—")}</span>'
            f'<span class="cr-col-on">on <code>{escape(root)}</code>{by}</span>'
            f'<span class="cr-col-when">{escape(when)}</span>'
            f'</div>')


def _live_run_line_html(run) -> str:
    started = orient_time.fmt(run.started_at) if run.started_at else "—"
    return (f'<div class="cr-live-run">'
            f'<span class="cr-live-run-repo">{escape(run.repo or "—")}</span>'
            f'<span class="cr-live-run-agent">{escape(run.agent or "—")}</span>'
            f'<span class="cr-live-run-task">{_clippable(run.task)}</span>'
            f'<span class="cr-live-run-when">{escape(started)}</span>'
            f'</div>')


def _visible_refusals(refusals: list, visible: frozenset[str] | None) -> list:
    """Drop a refusal whose own `product` the viewer's scope does not reach
    (E19·4). `visible is None` is the whole-fleet viewer, who sees every
    refusal including the unattributed ones (`product` is nullable on the
    row — `_refusals_by_crew`'s own docstring); a scoped viewer sees neither
    an out-of-scope product's collision nor an unattributed one, the same
    default-deny rule the Library's own disclosure bound rests on for this
    page (`test_crews_is_full_tier_only_because_the_disclosure_bound_depends_on_it`)."""
    if visible is None:
        return list(refusals)
    return [r for r in refusals if r.product in visible]


def _visible_live_runs(runs: list, visible: frozenset[str] | None) -> list:
    """Drop a live Bosun run whose own `repo` the viewer's scope does not
    reach (E19·4) — the raw "Live Bosun state" list is not keyed by roster
    slug the way the in-flight column is, so it needs its own filter rather
    than inheriting one from `_roster_counts`."""
    if visible is None:
        return list(runs)
    return [r for r in runs if r.repo in visible]


def _live_runs_section_html(result, visible: frozenset[str] | None) -> str:
    """The real "Live Bosun state" panel (the operator, reading /crews, 2026-09-02:
    replacing the `awaiting_state` placeholder now that
    `library_client.list_live_runs()` exists). `available: False` is the
    Library successfully reporting its OWN upstream (the Bosun) didn't
    answer — rendered by naming `reason`, never blanked and never read as
    "no runs", which would be indistinguishable from the healthy empty case
    to a reader (this module's own docstring, and the reasoning
    `library_client`'s module docstring already applies to a hard request
    failure — this is the same rule one layer up, for a soft one the wire
    itself reports)."""
    if not result.available:
        reason = result.reason or "no reason given"
        return ds.empty_state(f"Live Bosun state is unavailable right now — {reason}.")
    runs = _visible_live_runs(result.runs, visible)
    if not runs:
        return ds.empty_state("No runs on foot right now.")
    as_of = ""
    if result.generated_at:
        as_of = (f'<p class="cr-sub">As of '
                f'{escape(orient_time.fmt(result.generated_at, assume_utc=True))}.</p>')
    rows = "".join(_live_run_line_html(r) for r in runs)
    return as_of + f'<div class="cr-live-runs">{rows}</div>'


def live_html(scope: frozenset[str], whole_fleet: bool) -> str:
    """Render /crews at request time, restricted to the viewer's project
    scope (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): a crew whose repo is not
    reachable from `scope` carries no row, no runs panel, no in-flight count
    and no cost figure; the lease-refusal "Collisions" list and the raw
    "Live Bosun state" list — the two sections not keyed by roster slug, so
    filtering the roster alone would not reach them — are filtered against
    the same visible-repo set. `whole_fleet`
    (`crew_registry.covers_every_project(scope)`, computed by the caller)
    gates nothing further here beyond lifting that filter entirely, since
    every figure on this page already carries its own project."""
    visible = _visible_repos(scope, whole_fleet)
    roster = _roster_counts(scope, whole_fleet)
    items = library_client.list_issue_items()
    lease_result = library_client.list_leases(state="all")
    refusals = _visible_refusals(library_client.list_lease_refusals(), visible)
    live_runs_result = library_client.list_live_runs()
    work = _work_by_crew(items)
    lease_counts = _leases_by_crew(lease_result.leases, items, policy=lease_result.policy)
    refusal_counts = _refusals_by_crew(refusals)
    live_runs_by_crew = _live_runs_by_crew(live_runs_result.runs if live_runs_result.available else [])
    cost = ch._cost_rollup()

    runs_since, runs_until, _ = resolve_window(None)
    runs_date_dirs = _date_dirs(runs_since, runs_until)
    session_records = _load_session_records(runs_date_dirs)
    runs_by_crew = _group_runs_by_crew(_load_run_records(runs_date_dirs), session_records)
    sessions_by_crew = _sessions_by_crew(session_records)

    now = orient_time.now()
    last_activity = _last_activity_by_crew(
        roster, live_runs_by_crew=live_runs_by_crew, runs_by_crew=runs_by_crew,
        sessions_by_crew=sessions_by_crew, now=now)

    # Default order is most-recent-activity-first, not alphabetical (the operator,
    # reading /crews, 2026-09-02) — a crew this page has no activity signal
    # for at all sorts last, never first, matching the click-to-sort JS'
    # own rule for an unknown `data-sort` value on any column.
    def _default_sort_key(slug: str):
        la = last_activity.get(slug)
        return (0, -la.timestamp()) if la is not None else (1, 0)

    rows = "".join(
        '<div class="cr-group">'
        + _row(slug, info["label"], info["agents"], work.get(slug),
              lease_counts.get(slug), ch._cost_for(slug, cost),
              refusal_counts.get(slug, 0),
              in_flight=len(live_runs_by_crew.get(slug, [])),
              last_activity=last_activity.get(slug), now=now)
        + _runs_panel_html(slug, runs_by_crew.get(slug, []), runs_since, runs_until)
        + '</div>'
        for slug, info in sorted(roster.items(), key=lambda kv: _default_sort_key(kv[0])))

    # Newest first, and every one of them: a refusal exists only when two crews
    # reached for one fenced subtree, so the whole history is short enough to
    # read and a truncated list would hide exactly the repeat collision this is
    # for.
    collisions = "".join(_collision(r) for r in refusals)

    body = f'''<div class="cr">
  <header class="cr-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / crews</div>
    <h1>Crews</h1>
    <p class="lede">Every crew at once: roster size, active work, held leases —
    alive, near expiry (with time left) or dead, a dead lease being a different
    answer from no lease — and a {ch.WINDOW_DAYS}-day
    cost rollup, and the collisions the lease fence refused. A crew with dispatched
    sessions in the last {RUNS_DEFAULT_WINDOW_DAYS} days carries a "runs" expander
    underneath — what ran, for what, how long, on which model, at what cost, and
    what it did. Sorted by last activity by default; click a column heading to
    sort by it, click again to reverse.</p>
  </header>
  <div class="wrap">
    <div class="cr-table" id="cr-table">
      {_crew_table_head_html()}
      {rows or ds.empty_state("No crews discovered.")}
    </div>
    <h2 class="cr-h2">Collisions <span class="cr-h2-note">all time</span></h2>
    <p class="cr-sub">Every time the lease fence refused one crew because
    another held the subtree. The same two crews meeting repeatedly on one root
    is the thing worth looking at — whether they wanted the same item or one
    epic kept fencing the other's siblings, the row cannot say, and the answer
    changes what to do about it. A count alone names nobody. Unwindowed, unlike
    the cost column above: a repeat collision across a long stretch is the
    finding, and a rolling window would hide it.</p>
    <div class="cr-cols">
      {collisions or ds.empty_state("No crew has been refused a lease.")}
    </div>
    <h2 class="cr-h2">Live Bosun state</h2>
    <p class="cr-sub">Runs the Bosun reports as on foot right now, across every
    crew, read live from <code>GET /runs</code> — dispatched work only; a
    working CO session that has not yet dispatched a run does not appear
    here, since Orient reads the Bosun's run register, never its control
    channel (ADR-0050).</p>
    {_live_runs_section_html(live_runs_result, visible)}
    <div class="footnote">Roster from /engineering/roster's own data, crew names
    per ADR-0042 where they differ from a repo's own label. Work and leases read
    live from the Library, joined by issue id; refusals carry their own product
    and need no join. Live Bosun state reads <code>GET /runs</code> directly.
    Cost, sessions and runs from local files under <code>~/Convivy/agent-runs</code>
    — Orient holds no copy of them; a run's own "repo" field is what attributes
    it to a crew. A crew name links to its project page when one exists.</div>
  </div>
</div>
<script>
{_CREW_SORT_JS}
</script>'''
    return ds.page_shell(title="Crews",
                         nav_html=ds.nav_component("/crews", "full"),
                         body=body, extra_css=_CSS)


def _crew_controls_html(roster: dict, selected: str, since: dt.date, until: dt.date) -> str:
    options = "".join(
        f'<option value="{escape(slug, quote=True)}"'
        f'{" selected" if slug == selected else ""}>{escape(info["label"])}</option>'
        for slug, info in sorted(roster.items(), key=lambda kv: kv[1]["label"]))
    return (f'<form method="get" action="/crews/runs" class="cr-form">'
            f'<label>Crew<br><select name="product">'
            f'<option value="">Choose a crew…</option>{options}</select></label>'
            f'<label>From<br><input type="date" name="from" '
            f'value="{escape(since.isoformat(), quote=True)}"></label>'
            f'<label>To<br><input type="date" name="to" '
            f'value="{escape(until.isoformat(), quote=True)}"></label>'
            f'<button type="submit">View</button></form>')


def runs_html(query: dict | None, scope: frozenset[str], whole_fleet: bool) -> str:
    """The click-through the epic asks for: the same per-crew run list /crews
    shows a recent slice of, over a time window the reader picks — one crew
    at a time, so the read stays bounded (RUNS_PAGE_LIMIT) instead of an
    unbounded fleet-wide scan.

    `scope`/`whole_fleet` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): `roster`
    is already filtered to the viewer's visible repos, so a `?product=`
    naming one outside it simply isn't `in roster` and falls to the same
    "pick a crew" empty state an unknown product name already got — no
    separate scope check needed here."""
    query = query or {}
    since, until, _ = resolve_window(query)
    roster = _roster_counts(scope, whole_fleet)
    product = (query.get("product") or "").strip()

    if product and product in roster:
        date_dirs = _date_dirs(since, until)
        sessions = _load_session_records(date_dirs)
        run_records = _load_run_records(date_dirs)
        runs = sorted(
            (_run_view(r, sessions) for r in run_records if r["meta"].get("repo") == product),
            key=lambda v: v["started_at"] or "", reverse=True)
        shown = runs[:RUNS_PAGE_LIMIT]
        rows_html = "".join(_run_row_html(rv) for rv in shown) or ds.empty_state(
            f"No runs for {escape(roster[product]['label'])} in this window.")
        more = len(runs) - len(shown)
        more_note = f" (showing the most recent {RUNS_PAGE_LIMIT})" if more > 0 else ""
        count_line = (f'{len(runs)} run{"s" if len(runs) != 1 else ""} for '
                      f'{escape(roster[product]["label"])}, {since.isoformat()} '
                      f'to {until.isoformat()}{more_note}')
    else:
        rows_html = ds.empty_state("Pick a crew above to see its runs.")
        count_line = ""

    body = f'''<div class="cr">
  <header class="cr-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / crews / runs</div>
    <h1>Crew runs</h1>
    <p class="lede">One crew's dispatched runs over a time window you choose — the
    same list each crew's panel on <a href="/crews">Crews</a> shows a recent slice
    of. Bounded to the crew and range picked below; nothing here scans the whole
    fleet at once.</p>
  </header>
  <div class="wrap">
    {_crew_controls_html(roster, product, since, until)}
    <p class="cr-sub">{count_line}</p>
    <div class="cr-runs-page">
      {_run_columns_head_html()}
      {rows_html}
    </div>
  </div>
</div>'''
    return ds.page_shell(title="Crew runs",
                         nav_html=ds.nav_component("/crews", "full"),
                         body=body, extra_css=_CSS)


# The click-to-sort table's client-side reordering. `.cr-group` (name +
# runs panel, kept together by `display:contents` in `_CSS` — see there for
# why) is the unit this moves; the header row itself is never one, so it can
# never end up sorted into the body.
_CREW_SORT_JS = r"""
(function () {
  var table = document.getElementById('cr-table');
  if (!table) return;
  var groups = Array.prototype.slice.call(table.querySelectorAll('.cr-group'));
  var buttons = Array.prototype.slice.call(table.querySelectorAll('.cr-row-head .cr-sort-btn'));
  var state = null;
  buttons.forEach(function (btn) {
    if (btn.hasAttribute('data-default-dir')) {
      state = {col: Number(btn.dataset.col), dir: btn.dataset.defaultDir === 'asc' ? 1 : -1};
    }
  });

  function cellAt(group, col) {
    var row = group.querySelector('.cr-row');
    return row ? row.children[col] : null;
  }

  // An empty `data-sort` (no value known) always sorts last, in EITHER
  // direction — "a crew with no activity sorts last, never first" (the operator,
  // reading /crews, 2026-09-02), generalized to every column rather than
  // special-cased to just "last activity".
  function sortValue(cell) {
    if (!cell) return null;
    var raw = cell.getAttribute('data-sort');
    return raw === null || raw === '' ? null : raw;
  }

  function sortGroups(col, dir) {
    groups.sort(function (a, b) {
      var av = sortValue(cellAt(a, col));
      var bv = sortValue(cellAt(b, col));
      if (av === null && bv === null) return 0;
      if (av === null) return 1;
      if (bv === null) return -1;
      var an = parseFloat(av), bn = parseFloat(bv);
      var cmp = (!isNaN(an) && !isNaN(bn)) ? an - bn : String(av).localeCompare(String(bv));
      return cmp * dir;
    });
    groups.forEach(function (g) { table.appendChild(g); });
  }

  function paint() {
    buttons.forEach(function (btn) {
      var arrow = btn.querySelector('.cr-sort-arrow');
      var active = !!state && Number(btn.dataset.col) === state.col;
      btn.classList.toggle('cr-sort-active', active);
      if (arrow) arrow.textContent = active ? (state.dir === 1 ? '▴' : '▾') : '';
    });
  }

  buttons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var col = Number(btn.dataset.col);
      var dir = (state && state.col === col && state.dir === -1) ? 1 : -1;
      state = {col: col, dir: dir};
      sortGroups(col, dir);
      paint();
    });
  });

  // The server already rendered rows in the default order (most-recent-
  // activity-first) — this only paints the arrow to match, it does not
  // re-sort, so it can't disagree with the SSR order over a float rounding
  // difference.
  paint();
})();
"""

_CSS = r"""
.cr .cr-head{margin-bottom:24px;}
.cr .lede{color:var(--ink-2);}
.cr .cr-table{display:flex;flex-direction:column;gap:6px;}
.cr .cr-group{display:contents;}
.cr .cr-row{display:grid;
grid-template-columns:1.1fr 115px 90px 70px 160px 130px 90px 150px;
align-items:center;gap:9px;background:var(--surface);border:1px solid var(--line);
border-radius:10px;padding:9px 13px;font-size:.86rem;}
.cr .cr-row-head{background:transparent;border-color:transparent;font-weight:640;
font-size:.78rem;color:var(--ink-3);padding:0 13px;}
.cr .cr-row-name{font-weight:560;}
.cr .cr-row-name a{color:inherit;text-decoration:none;}
.cr .cr-row-name a:hover{text-decoration:underline;}
.cr .cr-row-cell{font-family:var(--mono);font-size:.8rem;color:var(--ink-2);white-space:nowrap;}
.cr .cr-sort-btn{all:unset;box-sizing:border-box;display:flex;align-items:center;
gap:4px;cursor:pointer;font:inherit;font-weight:640;font-size:.78rem;color:var(--ink-3);
white-space:nowrap;}
.cr .cr-sort-btn:hover,.cr .cr-sort-btn.cr-sort-active{color:var(--ink);}
.cr .cr-sort-btn:focus-visible{outline:2px solid var(--brand);outline-offset:2px;
border-radius:3px;}
.cr .cr-sort-arrow{font-size:.7em;}
.cr .cr-lease-near{color:var(--warn);font-weight:610;}
.cr .cr-lease-alive{color:var(--ok);}
.cr .cr-lease-dead{color:var(--err);}
.cr .cr-h2{margin:30px 0 4px;font-size:1rem;}
.cr .cr-h2-note{font-weight:400;font-size:.76rem;color:var(--ink-3);
text-transform:uppercase;letter-spacing:.03em;margin-left:.4rem;}
.cr .cr-sub{color:var(--ink-3);font-size:.84rem;margin:0 0 12px;max-width:62ch;}
.cr .cr-cols{display:flex;flex-direction:column;gap:6px;}
.cr .cr-col{display:flex;flex-wrap:wrap;align-items:baseline;gap:7px;
background:var(--surface);border:1px solid var(--line);border-radius:10px;
padding:9px 13px;font-size:.84rem;}
.cr .cr-col-who{font-family:var(--mono);font-size:.8rem;font-weight:560;}
.cr .cr-col-arrow{color:var(--ink-3);font-size:.78rem;}
.cr .cr-col-on{color:var(--ink-2);}
.cr .cr-col-on code{font-family:var(--mono);font-size:.76rem;}
.cr .cr-col-when{margin-left:auto;color:var(--ink-3);font-size:.78rem;white-space:nowrap;}
.cr .cr-runs{margin:-2px 0 2px;background:var(--surface);border:1px solid var(--line);
border-top:none;border-radius:0 0 10px 10px;font-size:.82rem;}
.cr .cr-runs summary{cursor:pointer;padding:6px 13px;color:var(--ink-3);
font-size:.78rem;list-style:none;}
.cr .cr-runs summary::-webkit-details-marker{display:none;}
.cr .cr-runs summary::before{content:"▸ ";}
.cr .cr-runs[open] summary::before{content:"▾ ";}
.cr .cr-runs-body{padding:0 13px 10px;}
.cr .cr-run-session{margin-top:8px;}
.cr .cr-run-session-line{color:var(--ink-3);font-size:.74rem;text-transform:uppercase;
letter-spacing:.03em;margin:0 0 4px;}
/* Every fixed track sized to its widest real content plus headroom, measured
   against the live corpus 2026-08-31..2026-09-02 (417 records): agent max 22
   chars/169px, "started" max 20/154px, an in-flight duration max 23/177px,
   model max 15/115px, cost-cell max 17/131px — four of five were previously
   undersized, which is how the "MST" timezone label overtyped the duration
   column (the operator, reading /crews, 2026-09-02). Header and body rows are two
   separate grid containers sharing this one literal template — the only
   thing holding their columns in line — so re-measure and update both call
   sites (`_run_columns_head_html`, `_run_row_html`) if this ever changes. */
.cr .cr-run{display:grid;
grid-template-columns:190px 1.6fr 178px 200px 130px 148px 1fr;
gap:9px;align-items:start;padding:4px 0;font-size:.8rem;border-top:1px solid var(--line);}
.cr .cr-run:first-child{border-top:none;}
.cr .cr-run-agent{font-family:var(--mono);font-weight:560;}
.cr .cr-run-label{color:var(--ink-2);}
.cr .cr-run-primary{display:block;}
.cr .cr-run-secondary{display:block;color:var(--ink-3);font-size:.74rem;
font-family:var(--mono);margin-top:2px;}
.cr .cr-run-when,.cr .cr-run-dur,.cr .cr-run-cost{font-family:var(--mono);
color:var(--ink-2);white-space:nowrap;}
.cr .cr-run-model{font-family:var(--mono);color:var(--ink-3);white-space:nowrap;}
.cr .cr-run-outcome{color:var(--ink-3);}
.cr .cr-run-more,.cr .cr-run-custom{margin-top:8px;font-size:.78rem;}
.cr .cr-run-custom a,.cr .cr-run-more a{color:var(--brand);}
.cr .cr-runs-page .cr-run{border-top:1px solid var(--line);}
.cr .cr-run-head{font-weight:640;font-size:.76rem;color:var(--ink-3);border-top:none;}
/* A <details> opening inside a fixed-track grid cell only grows that row's
   height — track WIDTHS are independent of a cell's content, so this cannot
   misalign a neighboring column the way an unbounded nowrap cell can (that
   was item 3's bug, above). `align-items:start` (rather than the previous
   `baseline`) keeps the other cells pinned to the top of the row once one
   cell's content goes multi-line, instead of baseline-aligning to a moving
   target. */
.cr .cr-clip summary{cursor:pointer;list-style:none;display:inline;}
.cr .cr-clip summary::-webkit-details-marker{display:none;}
.cr .cr-clip[open] summary{color:var(--ink-3);}
.cr .cr-clip-full{margin-top:4px;white-space:normal;word-break:break-word;}
.cr .cr-live-runs{display:flex;flex-direction:column;gap:6px;}
.cr .cr-live-run{display:grid;grid-template-columns:150px 170px 1fr 175px;
gap:9px;align-items:start;background:var(--surface);border:1px solid var(--line);
border-radius:10px;padding:9px 13px;font-size:.84rem;}
.cr .cr-live-run-repo{font-weight:560;}
.cr .cr-live-run-agent{font-family:var(--mono);color:var(--ink-2);white-space:nowrap;}
.cr .cr-live-run-task{color:var(--ink-2);}
.cr .cr-live-run-when{font-family:var(--mono);color:var(--ink-3);white-space:nowrap;}
.cr .cr-form{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;
margin:0 0 16px;font-size:.84rem;}
.cr .cr-form label{color:var(--ink-2);}
.cr .cr-form select,.cr .cr-form input{margin-top:3px;padding:5px 8px;
border:1px solid var(--line);border-radius:6px;background:var(--surface);
color:inherit;font-size:.84rem;}
.cr .cr-form button{padding:6px 14px;border-radius:6px;border:1px solid var(--line);
background:var(--brand-wash);cursor:pointer;font-size:.84rem;}
"""


def build_static() -> None:
    """Seed the static fallback (issue_01M2KVHTVFF4MAQF94M2SP6TZ0): /crews had
    no builder at all, so a live-render throw during a Library stall had
    nothing to fall back to and server.py returned 500 naming this file
    rather than a stale page.

    **Deliberately empty, not `live_html()` of some scope** (E19·4,
    issue_01M35K5JQ7Z7MHYZGWRSB5F85M): this file is served to whichever
    viewer's live render failed, scoped or not, so a snapshot naming every
    crew's roster, work and cost would leak to a scoped viewer — the same
    reasoning `chart_house_build.build_static` and `issue_build.main` already
    apply to their own fallbacks."""
    OUT.mkdir(parents=True, exist_ok=True)
    fallback = ds.page_shell(
        title="Crews",
        nav_html=ds.nav_component("/crews", "full"),
        body=ds.empty_state("Live view unavailable — reload in a moment."),
        extra_css=_CSS)
    (OUT / "crews.html").write_text(fallback, encoding="utf-8")
    print("Wrote site/crews.html")


if __name__ == "__main__":
    build_static()
