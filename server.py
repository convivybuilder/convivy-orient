#!/usr/bin/env python3.11
"""Identity-aware origin for convivy-orient (loopback:8099).

Serves a per-tier bundle chosen from the Cloudflare Access identity. The
contractor bundle physically omits Company/legal/strategy/platform docs, so
enforcement is real, not in-browser. Loopback-only: the sole legitimate path
here is the cloudflared tunnel, which carries only Access-authenticated
requests.

A FastAPI app on uvicorn (WS0.4, ARCH-348) — the shell the control plane grows
in. This PR is a pure lift of the prior stdlib server: same routes, same tier
gating, same live-render-with-static-fallback, same access.log lines, same
error bodies. No new routes, no behaviour change. The identity/tier logic lives
in access_identity (WS0.3); this module consumes it.

Identity comes from one of two places, controlled by jwt.json["enforce"]:

- **enforce = false (monitor):** trust the `Cf-Access-Authenticated-User-Email`
  header (the original ARCH-056 model — fine because only Access traffic reaches
  this loopback origin). If a `Cf-Access-Jwt-Assertion` is present we ALSO verify
  it and log the outcome (+ its aud) to access.log, so we can confirm real
  traffic verifies cleanly and capture the app's AUD tag before enforcing.
- **enforce = true (hardened, ARCH-056 follow-up):** trust ONLY a cryptographically
  verified `Cf-Access-Jwt-Assertion` — RS256 signature checked against the team
  JWKS, plus iss / exp / aud. The plaintext email header is no longer trusted;
  a request without a valid signed JWT is refused (403).

Tier rule: verified email in tiers.json["full"] → full bundle; every other
authenticated email → contractor bundle (least privilege by default). tiers.json
and jwt.json are re-read per request, so adding someone to "full" or flipping
enforce takes effect without a restart.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os
import sys
import time
from html import escape as _html_escape
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from access_identity import (
    EMAIL_HEADER,
    JWT_HEADER,
    jwt_config,
    tier_for,
    verify_access_jwt,
)
import activity_build
import alerts_build
import alerts_policy_api
import archives_build
import authz
import issue_build
import changelog_build
import chart_house_build
import code_health_build
import crews_build
import dashboard_build
import engineering_build
import engineering_e2e_build
import home_build
import kb_build
import library_client
from logutil import clean_log_field
import memory_api
import merge_queue_build
import overview_build
import ops_reports_build
import approve_api
import model_override_api
import issue_write_api
import epic_revise_api
import ledger_caps_api
import policy_api
import policy_build
import project_build
import reference_doc_build
import release_notes_build
import roster_build
import push_api
import blog_api
import kb_write_api
import room_api
import room_build
import security_build
import signal_ack_api
import signal_capture_api
import signal_search_api
import signals_build
import warrant_build
import velocity_build
import writing_desk_build
import blog_posts_build
from paths import STATE_ROOT

DIR = Path(__file__).resolve().parent
SITE = STATE_ROOT / "site"
ASSETS = DIR / "assets"

# Brand assets served publicly at /assets/<file> (icons + manifest). Only these
# names are reachable — an explicit allow-list, not a directory walk, so the
# route can never serve source-mark.png, generate_icons.py, or anything else
# that happens to live in assets/.
PUBLIC_ASSETS = {
    "apple-touch-icon.png": "image/png",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "favicon-32.png": "image/png",
    "site.webmanifest": "application/manifest+json",
    # Served at /sw.js (see the root-scope note in handle_get), not /assets/sw.js.
    "sw.js": "application/javascript",
}
ACCESS_LOG = STATE_ROOT / "access.log"

# The knowledge-base editor's ES modules, served at /editor/js/<name> to the full
# tier only, because editing is full-tier and a contractor must not learn the
# editor's route. md-serial's three files are vendored copies (vendor/md-serial/
# SOURCE.md); kb-doc.js is Orient's glue. An explicit allow-list like
# PUBLIC_ASSETS, so the route can serve nothing else from either directory.
# editor.js imports './md-serial.js' and './render.js', which resolve to
# siblings under the same prefix.
_EDITOR_JS_PREFIX = "/editor/js/"
EDITOR_JS_FILES = {
    "kb-doc.js": DIR / "editor_js" / "kb-doc.js",
    "editor.js": DIR / "vendor" / "md-serial" / "editor.js",
    "render.js": DIR / "vendor" / "md-serial" / "render.js",
    "md-serial.js": DIR / "vendor" / "md-serial" / "md-serial.js",
}


# --- response helpers (preserve the prior server's exact headers/bodies) ------


def _html(status: int, body: bytes, request: Request, *,
          extra_headers: dict | None = None) -> Response:
    """text/html; charset=utf-8, Cache-Control: no-store. HEAD sends the same
    Content-Length with no body, matching the prior handler.

    `extra_headers` is how a caller serving a stale static fallback marks the
    response as one — see `_log_render_failure`'s docstring for why that
    marker exists."""
    headers = {"Cache-Control": "no-store"}
    if extra_headers:
        headers.update(extra_headers)
    if request.method == "HEAD":
        headers["Content-Length"] = str(len(body))
        return Response(content=b"", status_code=status, media_type="text/html", headers=headers)
    return Response(content=body, status_code=status, media_type="text/html", headers=headers)


def _json(status: int, obj: dict) -> Response:
    return JSONResponse(obj, status_code=status, headers={"Cache-Control": "no-store"})


def _redirect(location: str) -> Response:
    return Response(content=b"", status_code=302,
                    headers={"Location": location, "Cache-Control": "no-store",
                             "Content-Length": "0"})


def _serve_asset(name: str, request: Request) -> Response:
    """Serve a public brand asset (icon/manifest) by exact allow-listed name.

    Public and pre-auth: iOS fetches the apple-touch-icon on "Add to Home
    Screen" without reliably carrying the Access cookie, and an icon is not
    sensitive. The allow-list is the whole security boundary — the name is
    matched exactly, so no '..' or subpath can escape assets/."""
    ctype = PUBLIC_ASSETS.get(name)
    if not ctype:
        return _html(404, b"<h1>404</h1>", request)
    try:
        data = (ASSETS / name).read_bytes()
    except OSError:
        return _html(404, b"<h1>404</h1>", request)
    headers = {"Cache-Control": "public, max-age=86400"}
    if request.method == "HEAD":
        headers["Content-Length"] = str(len(data))
        return Response(content=b"", status_code=200, media_type=ctype, headers=headers)
    return Response(content=data, status_code=200, media_type=ctype, headers=headers)


def access_log(request: Request, code: int, email: str, tier: str, jwt_status: str) -> None:
    """Append one line per request to access.log — the origin-side audit surface
    (who reached the site, what sort of caller, and how they authenticated).
    Never let a logging failure break the response.

    THE LAST TWO COLUMNS CARRY TWO SHAPES, one per surface. A page route logs
    the tier its bundle was chosen by (`full` / `contractor`) and the JWT
    outcome (`jwt=ok aud=...`). A router route logs the identity kind
    (`human` / `agent` / `system`) and how it authenticated (`auth=cf-access`,
    `auth=bearer`, `auth=none`) — see `api_common._note_identity`, including why
    the kind is logged where the role is not. The `jwt=` and `auth=` prefixes
    are what tell a reader which shape a line is.

    Marking the request logged is what keeps the middleware backstop from
    writing a second line for every page route that calls this itself. The flag
    lives on the request rather than on a list of paths the middleware would
    have to keep in step with the routing table.

    THE LAST COLUMN IS THE SERVER'S TIME, `ms=<n>`, from the middleware's
    stamp to this call (issue_01M388E3Q5GWBJTF2BKTHKD5WT). Every page took
    about 8 seconds on 2026-07-18 and the cause was never found, because
    nothing here could separate a slow render from a slow transfer. A page
    route logs before its response is sent, so the figure is the render; the
    backstop logs after `call_next`, so for a router it is the whole handler.
    `ms=-` means no stamp, which only a request that bypassed the middleware
    can produce."""
    request.state.access_logged = True
    started = getattr(request.state, "started", None)
    elapsed = f"ms={round((time.perf_counter() - started) * 1000)}" if started is not None else "ms=-"
    ts = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    # The two caller-influenced fields, cleaned the way PR #180 already decided
    # every other caller-influenced field in this file is (see `_log_field`).
    # `email` is a verified JWT claim under enforce and the raw
    # Cf-Access-Authenticated-User-Email header under monitor, and `actor` on
    # the middleware's call is an agent name out of api-tokens.json. Nothing
    # reaches this today that carries a tab — h11 refuses one in a header value,
    # and `urlsplit` strips it out of a path — but that is a property of the
    # HTTP parser rather than of this code, and this is the one field in the
    # line that the file's own convention had missed.
    line = (f"{ts}\t{code}\t{_log_field(email) or '(no-email)'}\t{tier}\t"
            f"{jwt_status}\t{request.method} {_log_field(target)}\t{elapsed}\n")
    try:
        with ACCESS_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# The header a stale static fallback carries, so a reader (a human checking
# response headers, or a future automated check) can tell a live render from
# a snapshot without diffing page content — the two look identical otherwise.
STALE_FALLBACK_HEADER = "X-Orient-Render"


def _log_field(value: str) -> str:
    """Strip a value for a tab-delimited access.log line. `page` on two call
    sites (issue/signal detail) is built from a caller-supplied URL path
    segment, and the exception text on every call site is caller-influenced
    too (a malformed id can shape a Library error message) — either could
    carry an embedded tab or newline that forges a new log line or shifts a
    parser's column boundaries (security review, PR #180). `logutil` is the
    shared rule; see it for what's covered and why."""
    return clean_log_field(value)


def _log_render_failure(page: str, exc: Exception) -> None:
    """A `live_html()` (or equivalent) call threw. Logged to access.log — the
    same file `access_log` writes, so a render failure is greppable in the one
    place operators already look — rather than silently swallowed.

    Found on 2026-08-17: every full-tier page's render throw was caught,
    logged nowhere, and answered with a normal 200 serving whatever static
    build happened to be on disk (site/<page>.html, written at the last
    `serve.sh` run). A page that had been failing to render since the last
    restart was indistinguishable from a current one — worse since the read
    rewire, because a builder's data now arrives over HTTP and
    `library_client`'s own docstring leaves a Library outage to raise on the
    assumption somebody notices. Nobody did; the static files were a day
    stale before anyone found out. This does not change the fallback
    itself — serving something is still better than a blank page — only
    that it stops being invisible.

    Never let a logging failure break the response, matching `access_log`."""
    ts = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    line = (f"{ts}\tRENDER-FAILED\t{_log_field(page)}\t"
            f"{type(exc).__name__}: {_log_field(str(exc))}\n")
    try:
        with ACCESS_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _identity(request: Request):
    """The auth gate (the prior do_GET's identity block). Returns
    (email, tier, label, jwt_status) on success, or (None, None, None, response)
    when access is refused — the caller returns that response verbatim."""
    cfg = jwt_config()
    header_email = (request.headers.get(EMAIL_HEADER) or "").strip().lower()
    assertion = request.headers.get(JWT_HEADER) or ""

    # Verify the JWT whenever one is present (both phases — in monitor it's
    # observe-only). Defensive: a verifier error must never break serving.
    jwt_ok, jwt_email, jwt_reason = False, "", "absent"
    jwt_aud = ""
    if assertion:
        try:
            jwt_ok, claims, jwt_reason = verify_access_jwt(
                assertion, cfg["team_domain"], cfg["aud"])
            jwt_email = (claims.get("email") or "").strip().lower()
            a = claims.get("aud", "")
            jwt_aud = a[0] if isinstance(a, list) and a else (a if isinstance(a, str) else "")
        except Exception as ex:  # pragma: no cover
            jwt_reason = f"verify-error:{type(ex).__name__}"
    jwt_status = f"jwt={jwt_reason}" + (f" aud={jwt_aud}" if jwt_aud else "")

    if cfg["enforce"]:
        if not (jwt_ok and jwt_email):
            access_log(request, 403, header_email or jwt_email, "-", jwt_status)
            return None, None, None, _html(
                403, b"<h1>403</h1><p>Access verification failed.</p>", request)
        email = jwt_email
    else:
        email = header_email
        if not email:
            access_log(request, 403, "", "-", jwt_status)
            return None, None, None, _html(
                403,
                b"<h1>403</h1><p>Access required \xe2\x80\x94 reach this via team.convivy.com.</p>",
                request)

    tier = tier_for(email)
    label = f"{email} · {'full team' if tier == 'full' else 'contractor'} view"
    return email, tier, label, jwt_status


def _substitute_viewer_tokens(html: str, email: str, esc_label: str,
                              scope: frozenset[str]) -> str:
    """Fill the per-viewer tokens a full-tier page carries.

    `__WHOAMI__` is the identity label every Engineering page shows in its
    header (the dashboards carry none, so it is a no-op for them; ARCH-194).
    `__CAN_CAPTURE__` gates the /archives log-a-signal form on roles holding
    signal_write, and `__CAN_LOG_ISSUE__` the "Log an issue" form on roles
    holding create — both cosmetic, since the POST route is the actual wall.
    `__CAN_ALERTS_WRITE__` gates the alert-threshold write form on /health,
    /spending and /kb (issue_01M17W6XGXVHNDWCFRTQ38Y2RK) on roles holding
    alerts_policy_write — same cosmetic role, alerts_policy_api's own gate is
    the wall. `__CAN_KB_WRITE__` gates the KB document editor on roles holding
    kb_write — kb_write_api's own gate is that one's wall.
    `__CAN_APPROVE_EPIC__` gates the Chart House's Approve control on roles
    holding epic_approve — approve_api's own gate, and the Library's own
    `epic.approve` capability behind it, are that one's wall.
    `__CAN_SET_ITEM_MODEL__` gates an item page's model control on roles
    holding item_model — model_override_api and the Library's `item.model`
    are that one's wall. `__CAN_SET_LEDGER_CAPS__` gates the Spending page's
    per-project ledger-caps form on roles holding ledger_caps_write —
    ledger_caps_api and the Library's own `ledger.grant` are that one's wall.
    All of those pages
    render through `_full_only_routes()`, so this one substitution covers every
    one of them with nothing route-specific to remember.

    **`__CAN_KB_WRITE__` is here rather than on the one route that used to fill
    it** because `/reference` now carries the editor too, and its token rides
    inside the page's JSON payload, where a literal left unfilled would be read
    by the script rather than merely look wrong. The document's own page at
    `/reference/doc/{id}` is handled outside this table (it takes an id) and
    calls this function for exactly the reason the paragraph below gives.

    EXTRACTED SO A ROUTE HANDLED OUTSIDE `_full_only_routes()` CANNOT SHIP A
    PAGE WITH THE TOKENS UNFILLED. A route lifted out of that table to read a
    query parameter keeps every other property of the page, so it must keep
    this one too, and a hand-copied substitution is what drifts. The
    Corrections page emits `__CAN_LOG_ISSUE__`, so lifting it out for
    `?sort=topic` would otherwise have left the literal token in the form's
    `data-can-log` attribute — the form reads it, so it would have broken
    rather than merely looked wrong.

    `scope` is the viewer's Library-resolved project scope, from `_identity`'s
    caller (once per request, never cached) — threaded through to
    `authz.human_identity` rather than re-resolved here."""
    viewer_role = authz.human_identity(email, scope).role
    html = html.replace("__CAN_CAPTURE__",
                        "true" if viewer_role in authz.CAPTURE_ROLES else "false")
    html = html.replace("__CAN_LOG_ISSUE__",
                        "true" if viewer_role in authz.ISSUE_LOGGER_ROLES else "false")
    html = html.replace("__CAN_ALERTS_WRITE__",
                        "true" if viewer_role in authz.ALERTS_POLICY_WRITER_ROLES else "false")
    html = html.replace("__CAN_KB_WRITE__",
                        "true" if viewer_role in authz.KB_WRITER_ROLES else "false")
    html = html.replace("__CAN_APPROVE_EPIC__",
                        "true" if viewer_role in authz.EPIC_APPROVER_ROLES else "false")
    html = html.replace("__CAN_SET_ITEM_MODEL__",
                        "true" if viewer_role in authz.ITEM_MODEL_SETTER_ROLES else "false")
    html = html.replace("__CAN_SET_LEDGER_CAPS__",
                        "true" if viewer_role in authz.LEDGER_CAPS_WRITER_ROLES else "false")
    return html.replace("__WHOAMI__", esc_label)


def _full_only_routes() -> dict:
    """The full-tier-only internal pages, resolved fresh per request so a
    live-render swap (or a test monkeypatch) on any builder is picked up — the
    prior stdlib server rebuilt this map inside do_GET for exactly this reason.

    Spend (/spending) + agent cost-performance (/cost-performance) split from the
    old combined /dashboard (ARCH-191); code health (/health); KB retrieval
    (/kb); the Engineering + Reference-Docs pages. All sensitive: a contractor
    gets a plain 404, the routes aren't revealed to anyone not entitled to them.
    Each renders LIVE per request (ARCH-182): live_html() re-reads its current
    data so the page is never staler than the data; a render throw falls back to
    the last static build so a bug never blanks a page."""
    return {
        # The signal-memory browse + semantic search (S1-b, ARCH-349): the store
        # the GitHub capture poller fills, rendered live from the control DB.
        # issue_01M1A678RA8CVDNNMZC6777NNA: renamed from /signals, which named
        # what fed the page rather than what it is — the nav entry has said
        # "Archives" all along (design_system.py). /signals still resolves,
        # via _MOVED below.
        "/archives": ("signals.html", signals_build.live_html),
        # Two more Archives sections (archives_build.py): PRs (grouped by PR +
        # product, comments and merge-time description together) and
        # Changelogs (every captured merge, every source — the raw memory
        # /engineering/changelog's authored-only feed is drawn from).
        "/archives/prs": ("signals-prs.html", archives_build.prs_live_html),
        "/archives/changelogs": ("signals-changelogs.html", archives_build.changelogs_live_html),
        # The Issues page became three (the operator, 2026-08-29 —
        # issue_01M1518ZFBGHAWC4Z46W893H2S): Epics (an epic per project,
        # expanding into its work items) at this same route — renamed +
        # moved under Engineering originally at ARCH-361, route path kept
        # for link stability including the old /bugs 302 via _MOVED —
        # Corrections (the corrections queue per project, adjacent) and
        # Course (each project's Course, in sequence) alongside it. Full-tier
        # only; each renders live from the Library.
        "/engineering/issues": ("issues.html", issue_build.live_html),
        # Corrections is NOT here: it reads `?sort=topic` off the query string,
        # and this dispatcher only ever calls its renderer with no arguments.
        # Handled above, alongside /crews/runs and /engineering/policy.
        "/engineering/course": ("course.html", issue_build.live_html_course),
        "/overview": ("overview.html", overview_build.live_html),
        "/spending": ("spending.html", dashboard_build.spending_live_html),
        "/cost-performance": ("cost-performance.html", dashboard_build.cost_performance_live_html),
        "/activity": ("activity.html", activity_build.live_html),
        "/velocity": ("velocity.html", velocity_build.live_html),
        "/health": ("health.html", code_health_build.live_html),
        "/security": ("security.html", security_build.live_html),
        "/warrant": ("warrant.html", warrant_build.live_html),
        "/kb": ("kb.html", kb_build.live_html),
        # Threshold-driven alerts (epic issue_01M0EHRNW5HVJCAXZHYQCBZ3YW): fires
        # only when a Dashboards metric above crosses its own page's policy
        # threshold. Moved out from under Engineering, which is why it renders
        # through the plain route table like every other Dashboards page rather
        # than the per-viewer early-return branch the old alerts page needed —
        # this page carries no ack state, so it needs no viewer identity.
        "/alerts": ("alerts.html", alerts_build.live_html),
        # The Documents area (ARCH-355): the KB browse + briefs + ARCH log moved
        # here from /engineering (which 302s below). Static fallback filenames
        # keep their historical engineering-* names — build.py and serve.sh
        # reference them; the route table is the mapping.
        # issue_01M1A678RA8CVDNNMZC6777NNA: renamed from /memory, which named
        # what fed the page rather than what it is — the nav entry has said
        # "Documents" all along (design_system.py). /memory still resolves,
        # via _MOVED below.
        "/reference": ("engineering.html", engineering_build.live_html),
        "/reference/arch-log": ("engineering-arch-log.html", engineering_build.arch_log_html),
        # Reports moved out of Documents (ARCH-362/R1), then retired into Notes
        # (the operator 2026-07-19). Notes itself is retired now too (the operator, 2026-08-26):
        # /memory/reports, /reference/reports and /engineering/reports plainly
        # 404 (see _MOVED). The streams that used to feed the Notes page are
        # dashboard inputs (agent-cost-report, warrant-scorecard
        # write their own JSON separately, which /spending and /warrant
        # read) — their jobs keep running; only their signal-store
        # posting to the `note`/`report` categories now renders on no page.
        # AI Briefings (ARCH-362/R2): the other half of the Reports split. Under
        # Documents rather than Engineering — these are outside-world industry
        # reading, not our fleet's output. Grouped under the Archives subnav
        # (design_system.py._ARCHIVES_SUBNAV), not Documents — see that file's
        # notes on the "what it IS, not what it reads" rule.
        "/reference/briefings": ("memory-briefings.html", ops_reports_build.briefings_live_html),
        "/engineering/roster": ("roster.html", roster_build.live_html),
        # CI/test results (ARCH-353 Phase A) — the vivy e2e post's team-site home.
        "/engineering/e2e": ("engineering-e2e.html", engineering_e2e_build.live_html),
        # /engineering/alerts retired (moved to /alerts under Dashboards) —
        # 302s there via _MOVED below.
        # The in-box changelog (ARCH-359): every merged PR, derived from the
        # GitHub capture poller's `changelog`-kind signals. Full-tier only.
        "/engineering/changelog": ("engineering-changelog.html", changelog_build.changelog_live_html),
        # The merge-queue index (ARCH-359 step 3, read-side): every open PR across
        # the org with its live CI + review state, cleared ones first. Full-tier only.
        "/engineering/merge-queue": ("engineering-merge-queue.html", merge_queue_build.live_html),
        # Products area (ARCH-360 follow-up): release notes — authored, user-facing,
        # kind=release_note signals. Full-tier only (internal authoring + preview).
        "/products/release-notes": ("products-release-notes.html", release_notes_build.live_html),
        # Chart House's base page (issue_01M0JFSVSGZW7A4RD77YNG395B, epic
        # issue_01M0EHQHQ7M39K811XKX1BAG98): every project's open work + a 30-day
        # cost rollup. Reads only cached/local data (never shells to `gh` itself),
        # so it's cheap to render live on every request like its siblings here.
        "/chart-house": ("chart-house.html", chart_house_build.live_html),
        # Crews (epic issue_01M08YM8MTNSTAEG1NV7E2GYRV): roster, active work,
        # held leases, and cost — one row per dispatchable crew. Reads the
        # Library live (issue items, leases) plus local roster/cost/run data;
        # no `gh` calls, cheap to render every request. Each row's own recent
        # sessions and runs expand inline; the custom-window click-through
        # to the same list lives at /crews/runs, handled explicitly above
        # (it needs query-string args this zero-arg dispatch can't pass).
        "/crews": ("crews.html", crews_build.live_html),
    }


# docs_url/openapi_url disabled: no unauthenticated schema surface (the prior
# stdlib server exposed no such endpoint).
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# --- merge-queue cache warmer (ARCH-362 follow-up) ---------------------------
#
# The Home "waiting on you" strip reads merge_queue_build's cache and NEVER
# fetches, because the fetch fans out one `gh` subprocess per repo at 20s timeout
# each and Home is the most-requested page. The consequence the operator hit: the tile read
# "— / tap to load" until they visited the merge-queue page, so the count they wanted
# at a glance was the one number Home could not show them — and on a phone they are
# least likely to tap through.
#
# Warming has to happen IN THIS PROCESS. The cache is a module-level tuple, so a
# separate launchd job would populate its own memory and exit, leaving the
# server's cache exactly as cold as before.
#
# Two properties this loop must hold:
#   1. It runs the fetch via asyncio.to_thread. The fetch is blocking subprocess
#      work; awaiting it on the event loop would stall EVERY request for the
#      duration — the precise slowness this whole design avoids.
#   2. It never propagates. A warmer that dies takes nothing with it: the tile
#      degrades to the cold "tap to load" it shows today, which is the current
#      behaviour rather than a regression.
_WARM_EVERY = 150  # just inside merge_queue_build._CACHE_TTL (180s)
_WARM_MAX_BACKOFF = 1800  # 30 min; a rate-limit window is an hour


async def _warm_merge_queue_cache() -> None:
    # Back off on repeated failure rather than retrying on the fixed interval.
    # The fetch is `gh search prs` + one `gh pr list` per repo, all GraphQL, and
    # that budget is exhaustible: on 2026-07-20 a heavy review day burned the
    # full 5000/hour GraphQL allowance. A warmer that keeps hammering through a
    # rate-limit window spends nothing useful, delays the reset, and prints a
    # line every 150s. Backing off yields the budget to the interactive work
    # that actually needs it, and the tile degrades to the cold "tap to load"
    # it already shows.
    delay = _WARM_EVERY
    while True:
        try:
            await asyncio.to_thread(merge_queue_build._fetch_open_prs, True)
            delay = _WARM_EVERY
        except asyncio.CancelledError:
            raise
        except Exception as e:                                   # noqa: BLE001
            # Never crash the server over a warm miss; the tile just stays cold.
            delay = min(delay * 2, _WARM_MAX_BACKOFF)
            print(f"merge-queue warm failed ({e!r}); next attempt in {delay:.0f}s",
                  file=sys.stderr)
        await asyncio.sleep(delay)


# Serving-only, and OFF unless __main__ turns it on. A lifespan hook alone is not
# enough: `TestClient(app)` runs the lifespan too, so every TestClient test would
# start fanning out real `gh` subprocesses — which hung this repo's suite at 34%
# when the warmer was first written without this gate.
_WARM_ENV = "ORIENT_WARM_MERGE_QUEUE"

@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    tasks = []
    if os.environ.get(_WARM_ENV) == "1":
        tasks.append(asyncio.create_task(_warm_merge_queue_cache()))
    try:
        yield
    finally:
        # Cancelling a task does not cancel work already running inside
        # to_thread — threads are not interruptible — so a worker may outlive
        # this. Harmless: the warmer writes to a module cache nothing will read
        # again, the sweeper's write is idempotent, and the process is going
        # down anyway.
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


app.router.lifespan_context = _lifespan


@app.middleware("http")
async def _server_header(request: Request, call_next):
    """Advertise the prior server's identifier, not the framework (uvicorn's own
    Server header is disabled in __main__ with server_header=False, so this
    stands), and log any request the route did not log itself.

    THE LOGGING BACKSTOP EXISTS BECAUSE THE ROUTERS NEVER HAD ONE. `access_log`
    was called only from the catch-all page handler and the shared auth gate, so
    every route on the nine mounted `/api` routers returned unlogged — a `403
    blog.write required` reached the browser and left no trace, and access.log
    held 13 `/api/blog` lines in its whole history against 107 Writing Desk page
    loads. On 2026-09-07 that made "the browser never sent a request" and "the
    server refused it" produce identical evidence while Save and Publish were
    being diagnosed, which is this shop's own definition of a check that is not a
    check. Ruling out the second hypothesis took reproducing the store layer
    against a scratch DB rather than reading a log
    (issue_01M1Z882KE85C88GBSB2CS1XJ5; the cause turned out to be client-side,
    convivy/convivy-orient#274).

    Here rather than in the routers because the middleware already sees every
    request regardless of which router handled it, so no future route can be
    added unlogged. The identity is the one the route resolved and stashed —
    see `api_common._note_identity` for why it cannot be resolved here.

    An unauthenticated request logs `-` and `auth=none`, which is the point: a
    refused API call and one that was never sent now leave different evidence.

    The stamp `access_log` measures duration from is taken here, first."""
    request.state.started = time.perf_counter()
    response = await call_next(request)
    response.headers["Server"] = "convivy-orient/1.1"
    if not getattr(request.state, "access_logged", False):
        access_log(request, response.status_code,
                   getattr(request.state, "log_actor", ""),
                   getattr(request.state, "log_kind", "-"),
                   getattr(request.state, "log_auth", "auth=none"))
    return response


# The Reading Room admin/provisioning API (ARCH-357). Included BEFORE the
# catch-all below so /api/* routes match first — Starlette dispatches in
# registration order, and the catch-all would otherwise swallow them. Same
# /api prefix (/api/rooms*), own auth (admin-only, human-only, CSRF).
app.include_router(room_api.router)
app.include_router(blog_api.router)
app.include_router(kb_write_api.router)

# /reference's on-demand body fetch + full-text search (issue_01M0Y7GZED2THYETHCT8NPHHDS).
# Kept its /api/memory prefix (a separate, internal API namespace from the
# page route it serves — see issue_01M1A678RA8CVDNNMZC6777NNA's PR body).
# Same ordering as the routers above — registered before the catch-all, own
# full-tier + human gate.
app.include_router(memory_api.router)

# Web Push subscriptions (ARCH-362 Phase 4b). Same /api prefix + ordering as the
# router above: registered before the catch-all, with its own human-only gate.
app.include_router(push_api.router)

# Signal acknowledgment, proxied to the Library under the approved-client
# credential (ARCH-362 Phase 4a; issue_01M08YKNV185DFJ6832GV8ZY22). Same
# ordering, same human-only + CSRF gate.
app.include_router(signal_ack_api.router)

# Manual signal capture ("Log a signal"), proxied to the Library under the
# agent credential (S2, ARCH-349; issue_01M08YKNV185DFJ6832GV8ZY22). Same
# ordering; open to agents as well as humans, so its own CSRF gate is
# conditional on ident.kind == "human" rather than unconditional.
app.include_router(signal_capture_api.router)

# The Archives tail: browse-by-shape and text search, the two affordances
# /archives adds beyond its own recent-60 and the PRs/Changelogs sections.
# Same ordering, same human + full-tier gate as /api/memory.
app.include_router(signal_search_api.router)

# The agent tool deny-list write route (ADR-0048, epic
# issue_01M0C91CGDR0YT8T5ERX2HD2HS, issue_01M1F1ENN8P6RD4W3SJJ04HR45). Same
# ordering, own human-only + CSRF + policy_write gate — see policy_api.py.
app.include_router(policy_api.router)

# The dashboard alert-threshold write route (issue_01M17W6XGXVHNDWCFRTQ38Y2RK).
# Same ordering, own human-only + CSRF + alerts_policy_write gate — see
# alerts_policy_api.py.
app.include_router(alerts_policy_api.router)

# The operator's approve button (issue_01M32CB6A9T04NVYPARGDT2GWQ). Same
# ordering, own human-only + CSRF + epic_approve gate — see approve_api.py. It
# is the ONLY path by which Orient sets an epic's maturity, and the Library's
# `epic.approve` capability is the wall behind it.
app.include_router(approve_api.router)

# Editing a proposed epic's specification and definition of done (the operator,
# 2026-09-22). Same ordering and the same human-only + CSRF gate as approving,
# on `epic.revise`, which is the `epic_approve` capability — see
# epic_revise_api.py.
app.include_router(epic_revise_api.router)

# Choosing the model a queued work item runs on (issue_01M38WVA7XHEDN1NFNPP8KDM0C).
# Same ordering and the same human-only + CSRF gate as approving, on
# `item.model`; the Library's own `item.model` is the wall behind it — see
# model_override_api.py.
app.include_router(model_override_api.router)

# The issue queue's resolve buttons and "Log an issue" panel
# (issue_01M3J6D09C38T4AT0SGZ20XHDW). Same ordering and the same human-only +
# CSRF gate as approving, on `issue.resolve` / `issue.create`; the Library's
# `item.resolve` / `item.create` are the wall behind them — see
# issue_write_api.py.
app.include_router(issue_write_api.router)

# The Spending page's per-project ledger-caps form (issue_01M4BH8KSSMC8XBKB2C2CX9QTF).
# Same ordering and the same human-only + CSRF gate as approving, on
# `ledger.caps_write`; the Library's own `ledger.grant` is the wall behind it
# — see ledger_caps_api.py.
app.include_router(ledger_caps_api.router)


# Retired paths that 302 to their new homes (link stability), tier-gated like the
# pages they point at. Module-level so the table is inspectable by tests — it was
# rebuilt inside handle_get on every request, which also meant the one invariant
# that matters here could not be asserted directly.
#
# THE INVARIANT: no key may also appear as a value. A path on both sides is an
# infinite redirect, which surfaces as a browser error rather than a test failure.
# ARCH-362/R5 reversed the briefs pair, which is exactly the edit that can
# introduce one. tests/test_server.py pins both the table and the walked behavior.
# /memory/reports and /engineering/reports used to redirect here too (ARCH-362/R1:
# Reports moved from Memory to Engineering, then retired into Notes, the operator
# 2026-07-19). Both entries are gone now that Notes itself is retired (the operator,
# 2026-08-26) — a redirect chain ending in a 404 misleads a reader into thinking
# something moved rather than telling them plainly that it's gone; the ingest
# routes below (`handle_post`) answer a bare 404 for exactly this reason.
#
# issue_01M1A678RA8CVDNNMZC6777NNA (2026-09-03): /memory and /signals renamed
# to /reference and /archives — both named what fed the page, not what it is.
# /memory's own redirect target flips from /memory to /reference along with it.
_MOVED = {"/engineering": "/reference",
          "/engineering/arch-log": "/reference/arch-log",
          "/memory": "/reference",
          "/memory/arch-log": "/reference/arch-log",
          "/memory/briefings": "/reference/briefings",
          "/signals": "/archives",
          "/signals/prs": "/archives/prs",
          "/signals/changelogs": "/archives/changelogs",
          # ARCH-361: /bugs renamed to /issues and moved under Engineering.
          "/bugs": "/engineering/issues",
          # epic issue_01M0EHRNW5HVJCAXZHYQCBZ3YW: Alerts moved out from under
          # Engineering into Dashboards, redefined as a policy-threshold view.
          "/engineering/alerts": "/alerts"}

_ISSUE_PREFIX = "/engineering/issues/"

# The per-signal page's prefix (ARCH-410). Dynamic, so it cannot live in
# _full_only_routes, which keys on the exact path. A signal id appears in
# documents, ADRs and messages, and until this route existed it resolved to
# nothing — /archives renders a 600-char excerpt with no link, so a long body
# (a meeting transcript, a captured call) had no reachable form on the site at
# all. Renamed from /signals/ alongside its parent
# (issue_01M1A678RA8CVDNNMZC6777NNA); _OLD_SIGNAL_PREFIX below keeps every old
# /signals/<id> link resolving.
_SIGNAL_PREFIX = "/archives/"

# The old per-signal prefix (pre-rename). Not a `_MOVED` entry: the id is
# dynamic, so no fixed key can carry it. Checked as a fallback, after
# `_SIGNAL_PREFIX` below has had first claim on `/archives/...` — this only
# ever matches a request still using the retired `/signals/...` form.
_OLD_SIGNAL_PREFIX = "/signals/"

# The per-project page's prefix (epic issue_01M0AK8CM3ADKDTG8PM6GRCP8H, item
# 5). Dynamic like the signal/issue prefixes above, for the same reason: a
# project name is not one of a fixed set of paths `_full_only_routes` can key
# on.
_PROJECT_PREFIX = "/projects/"

# The per-document page's prefix (issue_01M08YBCDTEFJ6N9S798ERF89P step 3;
# issue_01M0TKX6DW2E1TXGN9QT6K61J5 §3). Dynamic like the prefixes above, for
# the same reason: a document id is not one of a fixed set of paths
# `_full_only_routes` can key on, and it carries slashes of its own
# (`convivy-orient/agents/orient-coder`), so the slice below keeps them
# rather than treating the remainder as one opaque segment.
_REFERENCE_DOC_PREFIX = "/reference/doc/"

# Every `?sort` value `issue_build.live_html_corrections` actually groups or
# reorders — one membership test, so a third sort added later cannot forget to
# join it and fall through to the stale-flat-fallback trap below. An
# unrecognised value (a typo, or no `sort` at all) is deliberately NOT in this
# set: it renders the flat default and keeps the static fallback.
_CORRECTIONS_SORTS_WITHOUT_FALLBACK = frozenset({"topic", "severity"})


@app.api_route("/{full_path:path}", methods=["GET", "HEAD"])
def handle_get(request: Request):
    # Public brand assets (icons + manifest) resolve before the auth gate, so
    # "Add to Home Screen" and the favicon work regardless of session.
    path = request.url.path
    if path.startswith("/assets/"):
        return _serve_asset(path[len("/assets/"):], request)
    # The service worker MUST be served from the origin root: a worker's default
    # scope is its own directory, so one registered from /assets/ could not
    # control the site. Public and pre-auth like the other static assets — it
    # contains no data, only push-handling code, and the browser fetches it
    # during registration where carrying the Access cookie is not guaranteed.
    if path == "/sw.js":
        return _serve_asset("sw.js", request)

    email, tier, label, extra = _identity(request)
    if email is None:
        return extra  # the refusal Response
    jwt_status = extra

    # The viewer's project scope, resolved beside the identity gate (E19·1,
    # issue_01M35K54QHJYYPXWS28YWEHJP3): every later builder that needs it
    # reads `scope` from here rather than re-deriving it, and an
    # administrator's scope is simply the full set the Library returns for
    # them — nothing here branches on an admin role or flag. Resolved fresh
    # on every request, never cached: a Library outage refuses the request
    # rather than falling back to the global view, roles.json, or any
    # default scope.
    try:
        scope = library_client.person_scope(email)
    except Exception as exc:
        _log_render_failure("person-scope", exc)
        access_log(request, 503, email, tier, jwt_status)
        return _html(
            503, b"<h1>503</h1><p>project access could not be resolved.</p>", request)
    esc_label = _html_escape(label)

    if path.startswith(_EDITOR_JS_PREFIX):
        source = EDITOR_JS_FILES.get(path[len(_EDITOR_JS_PREFIX):])
        if tier != "full" or source is None:
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        data = source.read_bytes()
        access_log(request, 200, email, tier, jwt_status)
        # no-cache rather than a max-age: a deploy replaces these files, and a
        # browser holding last deploy's editor.js against this deploy's kb-doc.js
        # would run a pairing nothing tested.
        headers = {"Cache-Control": "no-cache"}
        if request.method == "HEAD":
            headers["Content-Length"] = str(len(data))
            data = b""
        return Response(content=data, status_code=200,
                        media_type="text/javascript; charset=utf-8", headers=headers)

    # ARCH-216 Phase 2: adaptive Home for both tiers. Live render with static
    # fallback (site/home.full.html or site/home.contractor.html).
    if path in ("/", "", "/index.html"):
        try:
            # `email` (not `label`) drives the waiting-on-you strip's alert count:
            # acknowledgment is per-identity, and the label is a cosmetic display
            # name while the email is what authz and the ack rows resolve against.
            html = home_build.live_html(tier, label, email=email)
            stale = False
        except Exception as exc:
            _log_render_failure(f"home.{tier}", exc)
            fallback = SITE / f"home.{tier}.html"
            try:
                html = fallback.read_text(encoding="utf-8")
                stale = True
            except FileNotFoundError:
                access_log(request, 500, email, tier, jwt_status)
                return _html(500, b"<h1>500</h1><p>home bundle missing - run home_build.py</p>", request)
        access_log(request, 200, email, tier, jwt_status)
        render_headers = {STALE_FALLBACK_HEADER: "stale-fallback"} if stale else None
        return _html(200, html.replace("__WHOAMI__", esc_label).encode("utf-8"), request,
                    extra_headers=render_headers)

    # The Reading Room (ARCH-357): one personalized reading surface per member,
    # reachable by BOTH tiers (un-tier-gated, as the retired Orientation area was).
    # Rendered live per member with NO static fallback — a per-member fallback file
    # would be a cross-member leak if fallback selection ever picked wrong — so a
    # render failure is a plain 500, never another member's room.

    # Blockers: a lane lifted out of the issue queue. Same tier gate as
    # /engineering/issues, since it reads the same table.
    if path == "/engineering/blockers":
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        lane = "blockers"
        try:
            html = issue_build.live_html_blockers()
        except Exception as exc:
            _log_render_failure(lane, exc)
            access_log(request, 500, email, tier, jwt_status)
            return _html(500, f"<h1>500</h1><p>{lane} unavailable</p>".encode(), request)
        access_log(request, 200, email, tier, jwt_status)
        # Blockers renders `issue_build._card`, which carries the Approve
        # control and so `__CAN_APPROVE_EPIC__` (issue_01M32CB6A9T04NVYPARGDT2GWQ).
        # Filled through the shared helper rather than a hand-written
        # `__WHOAMI__` replace: this route sits outside `_full_only_routes()`,
        # which is exactly the case that helper was extracted for — a
        # route lifted out of the table shipped the card's tokens unfilled.
        html = _substitute_viewer_tokens(html, email, esc_label, scope)
        return _html(200, html.encode("utf-8"), request)

    # /engineering/corrections (issue_01M22998K9JYPVYDAHQT0EX3G4): the
    # corrections queue, which takes `?sort=topic` to group each project's
    # items by area or `?sort=severity` (issue_01M2P9385P23PMZCZFB1BZW9SN) to
    # order each project's items worst-first. Handled here rather than in
    # `_full_only_routes()` for the same reason the two routes below are —
    # that dispatcher calls its renderer with no arguments.
    #
    # THE STATIC FALLBACK IS KEPT FOR THE FLAT VIEW AND WITHHELD FROM EVERY
    # NON-DEFAULT SORT. The fallback file is whatever the last build wrote,
    # which is always the flat lane order; serving it for a `?sort=topic` or
    # `?sort=severity` request would answer 200 with the wrong ordering, and
    # "that sort is not implemented" and "the render threw" would look
    # identical to whoever followed the link. That is the exact failure this
    # issue was filed for, one layer over, so a render that throws for a
    # recognised non-default sort is a visible 500 instead.
    if path == "/engineering/corrections":
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        params = dict(request.query_params)
        stale = False
        try:
            html = issue_build.live_html_corrections(params)
        except Exception as exc:
            _log_render_failure("engineering-corrections", exc)
            if params.get("sort") in _CORRECTIONS_SORTS_WITHOUT_FALLBACK:
                access_log(request, 500, email, tier, jwt_status)
                return _html(500, b"<h1>500</h1><p>corrections unavailable</p>", request)
            try:
                html = (SITE / "corrections.html").read_text(encoding="utf-8")
                stale = True
            except FileNotFoundError:
                access_log(request, 500, email, tier, jwt_status)
                return _html(500, b"<h1>500</h1><p>corrections.html missing - run its build</p>", request)
        access_log(request, 200, email, tier, jwt_status)
        html = _substitute_viewer_tokens(html, email, esc_label, scope)
        render_headers = {STALE_FALLBACK_HEADER: "stale-fallback"} if stale else None
        return _html(200, html.encode("utf-8"), request, extra_headers=render_headers)

    # /activity/nodes (issue_01M388E7WD8C83T5430YS88EJG): one session's
    # dispatch tree, which /activity fetches when a row is expanded instead of
    # embedding every session's tree in the page. Full tier only, as /activity
    # is; a contractor and an unknown id both get 404.
    if path == "/activity/nodes":
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        nodes = activity_build.session_nodes(request.query_params.get("id", ""))
        if nodes is None:
            access_log(request, 404, email, tier, jwt_status)
            return _json(404, {"error": "no such session"})
        access_log(request, 200, email, tier, jwt_status)
        return _json(200, {"nodes": nodes})

    # /crews/runs (issue_01M15C5W1GFGC81CW7YNCB0CP9): the click-through from a
    # crew panel on /crews to the same run list over a caller-chosen time
    # window. A literal path handled outside `_full_only_routes()` because
    # that dispatcher calls its renderer with no arguments — this one needs
    # `from`/`to`/`product` off the query string. Same tier gate and no
    # static fallback as the other query-driven pages above; crews_build
    # itself clamps the window, so this can't be made to scan an unbounded
    # range of ~/Convivy/agent-runs.
    if path == "/crews/runs":
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        try:
            html = crews_build.runs_html(dict(request.query_params))
        except Exception as exc:
            _log_render_failure("crews-runs", exc)
            access_log(request, 500, email, tier, jwt_status)
            return _html(500, b"<h1>500</h1><p>crew runs unavailable</p>", request)
        access_log(request, 200, email, tier, jwt_status)
        return _html(200, html.replace("__WHOAMI__", esc_label).encode("utf-8"), request)

    # /engineering/policy (issue_01M1F1E59D38JMZ8WJVKH6W1AY, epic
    # issue_01M0C91CGDR0YT8T5ERX2HD2HS): the deny-list in force for one
    # agent/crew/model triple, off the query string. Handled outside
    # `_full_only_routes()` for the same reason /crews/runs is above — this
    # dispatcher only ever calls its renderer with no arguments — plus one
    # more: that dispatcher's failure path serves the last static build,
    # which for a page reading a deny-list at request time would be a stale
    # copy of exactly the thing this page exists not to cache. No static
    # fallback; a render failure is a plain 500, which is also what makes it
    # visibly different from a page that rendered and found nothing denied.
    if path == "/engineering/policy":
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        try:
            html = policy_build.live_html(dict(request.query_params))
        except Exception as exc:
            _log_render_failure("engineering-policy", exc)
            access_log(request, 500, email, tier, jwt_status)
            return _html(500, b"<h1>500</h1><p>policy unavailable</p>", request)
        access_log(request, 200, email, tier, jwt_status)
        # __CAN_POLICY_WRITE__ works the same way as __CAN_KB_WRITE__ for the
        # write-form affordance (issue_01M1F1ENN8P6RD4W3SJJ04HR45) — shown to
        # roles holding policy_write (admin only; see authz.py). Cosmetic:
        # policy_api's own authz gate is the wall.
        viewer_role = authz.human_identity(email, scope).role
        html = html.replace("__CAN_POLICY_WRITE__",
                            "true" if viewer_role in authz.POLICY_WRITER_ROLES else "false")
        return _html(200, html.replace("__WHOAMI__", esc_label).encode("utf-8"), request)

    # One issue, at /engineering/issues/<id> — the URL every issue has always
    # carried as its own `url` and which has never resolved to anything.
    #
    # **This is the fix for a link the API hands out on every single row.**
    # `_issue_dict` sets `"url": f"/engineering/issues/{item.id}"`, so every issue
    # ever returned by the API, quoted in a commit message, or named in another
    # issue's body pointed here and 404'd. Decisions and signals each got this
    # page; issues did not, and nothing said so.
    #
    # Full-tier only and identity-substituted, exactly like the queue it is
    # reached from. No static fallback exists for a per-id page, so a render
    # throw is a 500 rather than a stale snapshot.
    if path.startswith(_ISSUE_PREFIX):
        item_id = path[len(_ISSUE_PREFIX):]
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        if not item_id:  # a bare trailing slash on the queue URL
            access_log(request, 302, email, tier, jwt_status)
            return _redirect("/engineering/issues")
        try:
            html = issue_build.detail_html(item_id)
        except Exception as exc:
            _log_render_failure(f"engineering-issues/{item_id}", exc)
            access_log(request, 500, email, tier, jwt_status)
            return _html(500, b"<h1>500</h1><p>issue unavailable</p>", request)
        if html is None:  # no such issue
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        access_log(request, 200, email, tier, jwt_status)
        # An issue's own page renders `issue_build._card`, which carries the Approve
        # control and so `__CAN_APPROVE_EPIC__` (issue_01M32CB6A9T04NVYPARGDT2GWQ).
        # Filled through the shared helper rather than a hand-written
        # `__WHOAMI__` replace: this route sits outside `_full_only_routes()`,
        # which is exactly the case that helper was extracted for — a
        # route lifted out of the table shipped the card's tokens unfilled.
        html = _substitute_viewer_tokens(html, email, esc_label, scope)
        return _html(200, html.encode("utf-8"), request)

    if path in ("/room", "/room/"):
        try:
            html = room_build.live_html(email, scope)
        except Exception as exc:
            _log_render_failure("room", exc)
            access_log(request, 500, email, tier, jwt_status)
            return _html(500, b"<h1>500</h1><p>reading room unavailable</p>", request)
        access_log(request, 200, email, tier, jwt_status)
        # __CAN_BLOG_WRITE__ works the same way as __CAN_CAPTURE__ / __CAN_LOG_ISSUE__
        # for the Room's blog-drafts panel (roles holding blog_write) — cosmetic;
        # blog_api's own authz gate on every route is the wall.
        viewer_role = authz.human_identity(email, scope).role
        html = html.replace("__CAN_BLOG_WRITE__",
                            "true" if viewer_role in authz.BLOG_WRITER_ROLES else "false")
        return _html(200, html.replace("__WHOAMI__", esc_label).encode("utf-8"), request)

    # The Writing Desk (epic issue_01M0EHQHQ7M39K811XKX1BAG98): the blog-drafts
    # panel's own top-level page, split out of the Reading Room. Reachable by
    # BOTH tiers like /room — the panel gates on __CAN_BLOG_WRITE__ (authz
    # role), not on site tier. No static fallback, matching /room: a render
    # failure is a plain 500 rather than a stale snapshot.
    if path in ("/writing-desk", "/writing-desk/"):
        try:
            html = writing_desk_build.live_html(email)
        except Exception as exc:
            _log_render_failure("writing-desk", exc)
            access_log(request, 500, email, tier, jwt_status)
            return _html(500, b"<h1>500</h1><p>writing desk unavailable</p>", request)
        access_log(request, 200, email, tier, jwt_status)
        viewer_role = authz.human_identity(email, scope).role
        html = html.replace("__CAN_BLOG_WRITE__",
                            "true" if viewer_role in authz.BLOG_WRITER_ROLES else "false")
        return _html(200, html.replace("__WHOAMI__", esc_label).encode("utf-8"), request)

    # Orientation retired (ARCH-357) → the Reading Room. /knowledge was the older
    # alias. Un-tier-gated: both tiers reach /room.
    if path in ("/knowledge", "/knowledge/", "/orientation", "/orientation/"):
        access_log(request, 302, email, tier, jwt_status)
        return _redirect("/room")

    if path == "/dashboard":  # ARCH-191: old combined route → Spending (tier-gated)
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        access_log(request, 302, email, tier, jwt_status)
        return _redirect("/spending")
    # Retired reading surfaces (ARCH-357) → the Reading Room, un-tier-gated (both
    # tiers reach /room): Reference Docs (root + every subpage + the old
    # /engineering/system-design alias). The KB browse (/reference) survives,
    # reconceived as the KB vault view.
    #
    # Briefs REJOINS this block (ARCH-372), and this time the Room actually
    # renders them. ARCH-357 sent briefs here as a 302 while /room rendered
    # nothing, so they were orphaned and the operator reported them lost; the two
    # restorations that followed each fixed the ROUTE, which was never what was
    # broken. The real cause was that every brief is written `audience: [operator]`
    # and kb_index.team_docs() skips anything without `team` — so briefs were
    # absent from the entitled doc set entirely, and no amount of routing could
    # surface them. Redirecting here is only correct because room_build now has a
    # standing Briefs section AND the briefs are team-audience.
    if (path in ("/engineering/briefs", "/memory/briefs")
            or path == "/reference-docs" or path.startswith("/reference-docs/")
            or path in ("/engineering/system-design", "/engineering/design")):
        access_log(request, 302, email, tier, jwt_status)
        return _redirect("/room")
    if path in _MOVED:
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        access_log(request, 302, email, tier, jwt_status)
        return _redirect(_MOVED[path])

    # Blog Posts (the operator, 2026-09-24): full tier like the rest of Documents, and
    # handled here rather than in `_full_only_routes()` because whether the
    # page carries drafts depends on the viewer's role, which that table's
    # no-argument renderers and static fallback cannot express. No static
    # fallback: a render failure is a plain 500, as on /room.
    if path in (blog_posts_build.ROUTE, blog_posts_build.ROUTE + "/"):
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        viewer_role = authz.human_identity(email, scope).role
        try:
            html = blog_posts_build.live_html(
                email, can_see_drafts=viewer_role in authz.BLOG_WRITER_ROLES)
        except Exception as exc:
            _log_render_failure("blog-posts", exc)
            access_log(request, 500, email, tier, jwt_status)
            return _html(500, b"<h1>500</h1><p>blog posts unavailable</p>", request)
        access_log(request, 200, email, tier, jwt_status)
        return _html(200, _substitute_viewer_tokens(html, email, esc_label, scope).encode("utf-8"),
                     request)

    full_only = _full_only_routes()
    if path in full_only:
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        page, renderer = full_only[path]
        try:
            html = renderer()
            stale = False
        except Exception as exc:  # live render failed — serve the last static build
            _log_render_failure(page, exc)
            try:
                html = (SITE / page).read_text(encoding="utf-8")
                stale = True
            except FileNotFoundError:
                access_log(request, 500, email, tier, jwt_status)
                return _html(500, f"<h1>500</h1><p>{page} missing — run its build</p>".encode(), request)
        access_log(request, 200, email, tier, jwt_status)
        html = _substitute_viewer_tokens(html, email, esc_label, scope)
        render_headers = {STALE_FALLBACK_HEADER: "stale-fallback"} if stale else None
        return _html(200, html.encode("utf-8"), request, extra_headers=render_headers)

    # One signal, at /archives/<id> — the URL a signal id has never resolved to.
    # Full-tier only and identity-substituted, like the browse page it is reached
    # from. No static fallback exists for a per-id page, so a render throw is a
    # 500 rather than a stale snapshot.
    #
    # detail_html returns None both for "no such signal" and for a customer-class
    # one, and BOTH answer 404 here. That is the privacy wall, not an oversight:
    # the full tier is broader than the signal_customer capability, so a viewer
    # who would be 403'd by the API must not read customer content off a
    # server-rendered page, and a distinguishable response would still confirm
    # the id exists. signals_build.detail_html carries the full reasoning.
    if path.startswith(_SIGNAL_PREFIX):
        signal_id = path[len(_SIGNAL_PREFIX):]
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        if not signal_id:  # a bare trailing slash on the browse URL
            access_log(request, 302, email, tier, jwt_status)
            return _redirect("/archives")
        try:
            html = signals_build.detail_html(signal_id, viewer_tier=tier)
        except Exception as exc:
            _log_render_failure(f"archives/{signal_id}", exc)
            access_log(request, 500, email, tier, jwt_status)
            return _html(500, b"<h1>500</h1><p>signal unavailable</p>", request)
        if html is None:  # no such signal, or one that must not render here
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        access_log(request, 200, email, tier, jwt_status)
        return _html(200, html.replace("__WHOAMI__", esc_label).encode("utf-8"), request)

    # The old per-signal URL, /signals/<id> (issue_01M1A678RA8CVDNNMZC6777NNA):
    # not a `_MOVED` entry because the id is dynamic, so no fixed key can carry
    # it. Checked after `_SIGNAL_PREFIX` above has had first claim on
    # `/archives/...`, so this only matches a request still using the retired
    # `/signals/...` form — including a bare `/signals/` trailing slash, which
    # redirects straight to the new landing page rather than `/archives/`.
    if path.startswith(_OLD_SIGNAL_PREFIX):
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        tail = path[len(_OLD_SIGNAL_PREFIX):]
        access_log(request, 302, email, tier, jwt_status)
        return _redirect(f"/archives/{tail}" if tail else "/archives")

    # One document, at /reference/doc/<id> (issue_01M08YBCDTEFJ6N9S798ERF89P
    # step 3; issue_01M0TKX6DW2E1TXGN9QT6K61J5 §3) — the canonical URL a
    # document has never had; Home's recent-docs panel now links here instead
    # of loading /reference's whole bundle to open a drawer. Full-tier only, like
    # /reference. No static fallback exists for a per-id page; unlike the other
    # dynamic pages here, a Library failure does not reach this except-block
    # at all — reference_doc_build.detail_html catches it itself and renders
    # ds.error_state inside the page shell, so this try/except is defense
    # against a bug in the builder rather than the expected Library-down path.
    if path.startswith(_REFERENCE_DOC_PREFIX):
        doc_id = path[len(_REFERENCE_DOC_PREFIX):]
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        if not doc_id:  # a bare trailing slash
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        try:
            html = reference_doc_build.detail_html(doc_id)
        except Exception as exc:
            _log_render_failure(f"reference/doc/{doc_id}", exc)
            access_log(request, 500, email, tier, jwt_status)
            return _html(500, b"<h1>500</h1><p>document unavailable</p>", request)
        if html is None:  # no such document, or one not team-audience
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        access_log(request, 200, email, tier, jwt_status)
        # __CAN_KB_WRITE__ gates the edit affordance
        # (issue_01M0TKX6DW2E1TXGN9QT6K61J5 §4) on roles holding kb_write, and
        # is filled by the shared substitution rather than here: /reference now
        # carries the same editor, so one definition serves both and cannot
        # drift. Cosmetic either way — kb_write_api's own authz gate, plus this
        # route's full-tier check above, are the wall.
        html = _substitute_viewer_tokens(html, email, esc_label, scope)
        return _html(200, html.encode("utf-8"), request)

    # One project, at /projects/<name> (epic issue_01M0AK8CM3ADKDTG8PM6GRCP8H,
    # item 5) — open PRs, recently merged PRs and recent commits, all read live
    # from the GitHub engine, same posture as /engineering/merge-queue.
    # Full-tier only: this shells out to `gh`. No static fallback exists for a
    # per-project page, so a render throw is a 500 rather than a stale snapshot.
    if path.startswith(_PROJECT_PREFIX):
        name = path[len(_PROJECT_PREFIX):]
        if tier != "full":
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        if not name:  # a bare trailing slash
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        try:
            html = project_build.detail_html(name)
        except Exception as exc:
            _log_render_failure(f"projects/{name}", exc)
            access_log(request, 500, email, tier, jwt_status)
            return _html(500, b"<h1>500</h1><p>project unavailable</p>", request)
        if html is None:  # not a known project name
            access_log(request, 404, email, tier, jwt_status)
            return _html(404, b"<h1>404</h1>", request)
        access_log(request, 200, email, tier, jwt_status)
        return _html(200, html.replace("__WHOAMI__", esc_label).encode("utf-8"), request)

    access_log(request, 404, email, "-", jwt_status)
    return _html(404, b"<h1>404</h1>", request)


@app.post("/{full_path:path}")
async def handle_post(request: Request):
    """Ingest write paths, both retired.

    /ingest/llm-spend (ARCH-152, per-call spend events → the spend ledger) is
    gone (2026-08-18): it took no real traffic in two months, and Orient
    should not hold a write path over app spend at all — that belongs to the
    Library's Chart House, once it exists (issue_01M0B8Q15RAFEWRVT0MFRKNGG1).

    /ingest/signal (external CI signals, ARCH-353 Phase A) is gone
    (2026-08-19, issue_01M08YKNV185DFJ6832GV8ZY22): Orient should not hold a
    write path over vault data, CI results included. GitHub Actions still
    posts here today — that traffic 404s. Per ADR-0045 the replacement is a
    poller on the mini fetching CI conclusions, not a repointed workflow (the
    Library binds loopback-only, so a GitHub runner has no credential it
    could present there), so this route answers 404 permanently rather than
    until some workflow file is repointed (issue_01M0B92VVV7NEH3NK068ZJ33Y2).
    Both gaps were accepted deliberately (the operator, 2026-08-18/19) rather than
    waited on.
    """
    access_log(request, 404, "", "-", "ingest")
    return _json(404, {"error": "not found"})


if __name__ == "__main__":
    # server_header=False: the app's middleware sets a custom Server header, so
    # uvicorn must not add its own (which would advertise the framework).
    # Warm the merge-queue cache only when actually serving (see _WARM_ENV).
    os.environ.setdefault(_WARM_ENV, "1")
    uvicorn.run(app, host="127.0.0.1", port=8099, log_level="warning", server_header=False)
