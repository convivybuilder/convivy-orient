#!/usr/bin/env python3.11
"""The Home "waiting on you" strip (ARCH-362 Phase 2).

The site's worst cognitive load sat at exactly the spot it exists to serve:
"what needs me right now" had no single surface, so the operator had to visit Issues and
Merge queue separately. That is the failure the waiting-on-operator digest was built to
paper over on Slack, and this strip is its durable replacement — the same
instinct as the standing "print ready-PR URLs every turn" habit, moved off a
chat post and onto the page the operator already opens.

Two tiles, each a count plus the one detail that tells them whether it can
wait, each linking into the queue it summarises. Neither Ready to merge nor
Alerts is a new data source; each already renders on its own page.

Three tiles the strip used to carry are gone. Unplanned epics counted approved
epics sitting on no live Course, which the Library called a defect and this
strip surfaced because the strip is where defects needing the operator belong. That
state can no longer occur (issue_01M32CB6A9T04NVYPARGDT2GWQ): approving an
epic now places it on its product's Course in the same transaction, so the
Library removed the route the tile read and the tile went with it. Decisions
was removed with the decision plane (2026-08-18). Corrections was removed with the
`awaiting-decision` state itself (issue_01M2F3QFTVZEFVTF0QJ5C7W7NC,
dashboards epic issue_01M2F3Q1B8J9E0WC5J1F1CMWA1): crews now work corrections
without the operator, so a tile reporting "N need you" against a state the
Library no longer sets was reporting operator work that did not exist. What
corrections are open is crew activity, not something waiting on the operator, and
`/engineering/corrections` already shows it.

TWO PROPERTIES THIS MODULE EXISTS TO HOLD
=========================================

**1. It must never slow Home down.** Home is the most-requested page on the
site. The merge queue fans out one `gh` subprocess per repo at 20s timeout each,
so reading it live would put a multi-second network call on the landing page —
the exact regression class behind the unresolved team.convivy.com slowness. So
the merge tile reads `merge_queue_build.cached_open_prs()`, which returns the
TTL cache if warm and None if cold, and NEVER fetches. A cold tile shows no
number and links through; the page it links to does the fetch.

**2. One broken source must not take Home down.** A strip that can 500 the
landing page is strictly worse than no strip. Every tile is gathered inside its
own try/except and degrades to `None` (rendered as "—"), so a schema drift or a
missing table costs one number rather than the whole page.

THE ALERTS TILE COUNTS WHAT /alerts COUNTS
===========================================
The tile used to count recent alert-*kind* signals from the signal store — a
different, unrelated number from what `/alerts` itself renders, which is
policy-threshold crossings computed by `alerts_build.gather()` (one page,
two disagreeing alert counts). `_active_alerts` now calls the same
`alerts_build.gather()` the `/alerts` page and `home_build`'s quick-link pill
already call, so the tile, the pill and the page it links to can never
disagree. `alerts_build.gather()` has no acknowledgment concept — each alert
is a live read of a metric against its own page's threshold, so it clears
itself the moment the metric recovers rather than staying until a person
marks it seen (see `alerts_build`'s own module docstring for why).
"""
from __future__ import annotations

from html import escape

import alerts_build
import merge_queue_build
from models import utcnow


def _age_short(when, now) -> str:
    """Coarse age for the tile sublabel. Deliberately less precise than the queue
    pages' `_age`: the strip answers "can this wait?", not "exactly how old"."""
    if when is None:
        return "—"
    secs = max(0, int((now - when).total_seconds()))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _active_alerts(now) -> tuple[int, str]:
    """Currently-crossed Dashboards policy thresholds — the same
    `alerts_build.gather()` call `/alerts` renders and `home_build`'s
    quick-link pill already counts (`home_build._active_alerts_count`).

    Previously this tile ran its own count of recent alert-*kind* signals from
    the signal store, a different question ("what alert-tagged rows landed
    recently") from what `/alerts` answers ("what is currently past its
    policy threshold"), so the tile and the page it links to could show two
    different numbers for the same thing. Sourcing both from one call is what
    makes that impossible now.

    `alerts_build.gather()` isolates each of its four source dashboards
    internally and never raises on a single bad snapshot; this function adds
    nothing of its own, so `gather()`'s `_vault_tile` wrapper is what turns an
    outright failure here into "unavailable" rather than a false zero.
    """
    alerts = alerts_build.gather()
    if not alerts:
        return 0, ""
    n_bad = sum(1 for a in alerts if a["severity"] == "bad")
    return len(alerts), (f"{n_bad} critical" if n_bad else "warning")


def _cleared_prs() -> tuple[int | None, str]:
    """Merge-queue PRs that have cleared, from the WARM CACHE ONLY.

    Returns (None, "") when the cache is cold — see the module docstring. None is
    rendered as "—" and is meaningfully different from 0: it means "not known
    right now", where 0 means "nothing is waiting".
    """
    rows = merge_queue_build.cached_open_prs()
    if rows is None:
        return None, "tap to load"
    cleared = sum(1 for r in rows if r.get("cleared"))
    return cleared, ("CI green, no conflicts" if cleared else "")


def gather(now=None, viewer: str | None = None) -> list[dict]:
    """The three tiles as data. Each source is isolated: a failure yields a None
    count rather than propagating, because this renders on Home.

    `viewer` is the CF-Access-verified email; kept for call-site compatibility
    with `home_build._full_body`, which passes it through. No tile currently
    reads it — the Alerts tile's old per-viewer ack read is gone along with
    `_unacked_alerts` (see the module docstring).
    """
    now = now or utcnow()
    tiles: list[dict] = []

    def _vault_tile(label: str, href: str, fn) -> dict:
        try:
            count, sub = fn(now)
            return {"label": label, "href": href, "count": count, "sub": sub}
        except Exception:
            return {"label": label, "href": href, "count": None, "sub": "unavailable"}

    try:
        count, sub = _cleared_prs()
    except Exception:
        count, sub = None, "unavailable"
    tiles.append({"label": "Ready to merge", "href": "/engineering/merge-queue",
                  "count": count, "sub": sub})
    # **THE "UNPLANNED EPICS" TILE IS GONE, AND THE STATE IT REPORTED CANNOT
    # OCCUR ANY MORE** (issue_01M32CB6A9T04NVYPARGDT2GWQ). It counted approved
    # epics sitting on no live Course, read from `GET /courses/unplanned`.
    # Approving an epic now places it on its product's Course in the same
    # Library transaction, so no epic is approved and unplanned, the Library
    # removed the route, and the tile could only ever have reported zero — or,
    # as it did between the two landings, "unavailable" forever.
    # `urgent` marks a tile that should LOOK different when it is non-zero, not
    # merely carry a bigger number. Only Alerts sets it today: an alert is the
    # fallback for a push notification the operator missed, so "something fired and you
    # have not seen it" has to read at a glance. The operator missed a standing count of
    # 14 precisely because a 14 tile and a 0 tile were styled identically.
    #
    # The flag is general so other tiles can opt in later; they are
    # deliberately left plain for now rather than restyling the whole strip.
    #
    # The href points at /alerts (epic issue_01M0EHRNW5HVJCAXZHYQCBZ3YW moved
    # the page under Dashboards; the old /engineering/alerts still 302s there).
    alerts = _vault_tile("Alerts", "/alerts", _active_alerts)
    alerts["urgent"] = True
    tiles.append(alerts)
    return tiles


def _tile_html(t: dict) -> str:
    count = t["count"]
    shown = "—" if count is None else str(count)
    # A zero tile is muted rather than hidden. Hiding it would make the strip's
    # shape change under the operator, and "Decisions: 0" is information — it says the
    # queue was checked and is empty, which an absent tile does not.
    cls = "wtile" + (" zero" if count == 0 else "") + (" unknown" if count is None else "")
    # Hot only when there is genuinely something un-actioned. A None (unknown)
    # count must NOT go hot: "we could not read the queue" is not "you have
    # alerts", and crying wolf on missing data is how the strip stops being
    # believed — the same reasoning as strip_html's all-clear rule.
    # `type(count) is int`, not isinstance: bool subclasses int in Python, so
    # isinstance(True, int) is True and a count of True would go hot and render
    # the word "True" as the number. No production path produces a bool, but a
    # guard described as load-bearing should hold for the reason it claims.
    hot = bool(t.get("urgent")) and type(count) is int and count > 0
    if hot:
        cls += " hot"
    sub = f'<span class="wsub">{escape(t["sub"])}</span>' if t["sub"] else ""
    # The state is carried in the accessible name too, not by colour alone.
    aria = (f' aria-label="{escape(t["label"], quote=True)}: {shown} '
            f'needing attention"') if hot else ""
    return (f'<a class="{cls}" href="{escape(t["href"], quote=True)}"{aria}>'
            f'<span class="wn">{shown}</span>'
            f'<span class="wlabel">{escape(t["label"])}</span>{sub}</a>')


def strip_html(now=None, viewer: str | None = None) -> str:
    """The strip, or an all-clear line when every actionable tile is zero.

    An unknown (None) count does NOT count as clear — if the merge cache is cold
    we do not know whether anything is waiting, and claiming "nothing needs you"
    on missing data is the one failure that would make the operator stop trusting it.

    SELF-SEALING BY CONTRACT. `gather()` already isolates every source, so today
    nothing below can raise. That is a property of the current body, though, not
    of the function — and `_full_body()` calls this directly with no try/except of
    its own, so anything that escapes here 500s the landing page. The outer guard
    makes the no-raise guarantee belong to `strip_html` itself, so a later edit
    that adds a line outside `gather()` cannot quietly take Home down. Raised by
    the round-1 reviewer, who noted the isolation boundary sat one level in.
    """
    try:
        tiles = gather(now, viewer)
        actionable = [t["count"] for t in tiles]
        if all(c == 0 for c in actionable):
            return ('<section class="waiting clear" aria-label="Waiting on you">'
                    '<span class="wclear">Nothing is waiting on you.</span></section>')
        # **No visible heading**, removed on the operator's report (2026-09-18) that "the
        # tiles on the front page are under a label 'waiting for you' which
        # should be removed".
        #
        # The DOM, stated rather than inferred: this `<h2>` sat at the top of
        # `section.waiting`, directly above `.wtiles`. `home_build`'s hero-stats
        # are the NEXT SIBLING of this whole section, below the tile row — never
        # directly beneath the heading. So the tiles it labelled were this
        # strip's own, and those were already links. An earlier version of this
        # comment claimed the heading sat over the hero-stats; that was a guess
        # about what the operator saw, written as though it were what they reported.
        #
        # The section keeps its `aria-label`, so the region stays named for
        # assistive technology; what went is the on-screen text.
        return ('<section class="waiting" aria-label="Waiting on you">'
                f'<div class="wtiles">{"".join(_tile_html(t) for t in tiles)}</div>'
                '</section>')
    except Exception:
        # Render nothing rather than a broken strip. Home keeps working, and the
        # absence is honest: it does NOT claim "nothing is waiting on you".
        return ""


# The strip renders INSIDE the hero, which is a gradient panel with white text —
# so these deliberately follow the `.hero-stat` idiom (translucent dark fill,
# white-alpha border) rather than the page's --card/--line variables, which are
# tuned for the light content area and would read as a foreign element here.
WAITING_CSS = """
.waiting{margin-top:26px;}
.wtiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px;}
.wtile{display:flex;flex-direction:column;gap:3px;padding:13px 16px 15px;
  border-radius:var(--radius);border:1px solid rgba(255,255,255,.22);
  background:rgba(40,12,4,.50);backdrop-filter:blur(4px);
  text-decoration:none;color:#fff;transition:border-color .15s,transform .15s;}
.wtile:hover{border-color:rgba(255,255,255,.55);transform:translateY(-1px);}
.wtile:focus-visible{outline:2px solid #fff;outline-offset:2px;}
.wn{font-size:25px;font-weight:750;line-height:1.1;font-variant-numeric:tabular-nums;}
.wlabel{font-size:12.5px;font-weight:600;color:rgba(255,255,255,.92);}
.wsub{font-size:11.5px;color:rgba(255,255,255,.72);}
/* A zero tile stays visible but recedes — it reports "checked, nothing here",
   which an absent tile would not. An unknown (—) tile reads the same way. */
.wtile.zero .wn,.wtile.unknown .wn{color:rgba(255,255,255,.55);font-weight:600;}
/* A non-zero Alerts tile is the fallback for a push that was missed, so it has
   to read at a glance against the dark hero. Brighter border, warmer fill, and
   a live dot — never colour alone (the count, the "N critical"/"warning"
   sub-label, and the aria-label all carry the same state). */
.wtile.hot{border-color:rgba(255,196,150,.85);background:rgba(150,45,10,.62);
  box-shadow:0 0 0 1px rgba(255,180,120,.35), 0 6px 18px rgba(0,0,0,.28);}
.wtile.hot:hover{border-color:#fff;}
.wtile.hot .wn{color:#ffd9bd;}
.wtile.hot .wlabel{color:#fff;}
.wtile.hot .wsub{color:rgba(255,255,255,.88);}
.wtile.hot .wlabel::before{content:"";display:inline-block;width:7px;height:7px;
  border-radius:50%;background:#ff9d5c;margin-right:6px;vertical-align:middle;
  box-shadow:0 0 0 3px rgba(255,157,92,.28);}
@media (prefers-reduced-motion:no-preference){
  .wtile.hot .wlabel::before{animation:wpulse 2.4s ease-in-out infinite;}
  @keyframes wpulse{0%,100%{box-shadow:0 0 0 3px rgba(255,157,92,.28);}
                    50%{box-shadow:0 0 0 6px rgba(255,157,92,.10);}}
}
.waiting.clear{margin-top:24px;padding:12px 16px;border-radius:var(--radius);
  border:1px dashed rgba(255,255,255,.30);background:rgba(40,12,4,.32);}
.wclear{font-size:13.5px;color:rgba(255,255,255,.88);}
@media (max-width:640px){.wtiles{grid-template-columns:repeat(2,1fr);}}
"""
