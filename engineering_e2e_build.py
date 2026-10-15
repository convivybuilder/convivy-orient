#!/usr/bin/env python3.11
"""The /engineering/e2e page — CI test results on the team site (ARCH-353 Phase A).

The first Slack-retirement re-homing: the vivy e2e stage-smoke result used to
post to #vivy-e2e; now a poller on the mini (bin/ci_result_fetch.py, in the
Library repo) fetches CI conclusions with `gh api` and writes them to the
Library's POST /signals under the ci-fetch-vivy identity, on a 15-minute timer
(ADR-0045). This page reads those signals through library_client and renders
the latest run per (product, suite) as a pass/fail tile, with recent history
below.

Rendered LIVE from the Library per request, via library_client, with no copy
in Orient's own control DB (server registers live_html in _full_only_routes,
full-tier only). Test results are internal-class signals, so
they render server-side like the /signals browse — no customer content here. All
free text (from the poller, ultimately GitHub Actions) is escaped on render.

The signal contract this page REQUIRES. This is what the renderers below read,
not a description of what any writer currently produces:
  source=ci  kind=test-result  product=<product>  stream=<suite>  (e.g. e2e-stage-smoke)
  meta.status ∈ {pass, fail}   meta.run_url   meta.run_number   meta.jobs={job:result}
  body = short human summary    produced_at = the run time

The poller does NOT satisfy it today, so every run it has written renders
"unknown" and grey. It puts the conclusion and the run URL under `refs`
(refs.conclusion, refs.run_url) rather than `meta`, and writes no jobs field at
all — measured 2026-09-01 across all 46 CI signals: the 14 poller-written ones
carry refs.conclusion and no meta.status, the 32 push-written ones the reverse.
Filed against the Library as issue_01M1F431KC6DPYY2D185GD036J. Do not "fix" this
by reading refs here: the contract belongs with the writer, and a consumer
deriving status from fields the writer does not send is the reaching-around
that retiring /ingest/signal existed to prevent.
Anything missing degrades gracefully (a status other than pass/fail renders
"unknown", grey) — a malformed post must never blank the page.

Retirement note: this page used to describe /ingest/signal, Orient's own write
path for this data, as live. That route is gone as of 2026-08-19
(issue_01M08YKNV185DFJ6832GV8ZY22, ADR-0045) and answers 404 permanently — see
server.py's handle_post for the route-level note. The replacement is the
poller above, not a repointed webhook; do not restore the route or redirect
this page's copy back to describing it.
"""
from __future__ import annotations

import datetime as dt
from html import escape

import design_system as ds
import library_client
from models import utcnow
from url_safety import safe_http_url
from paths import STATE_ROOT

SITE = STATE_ROOT / "site"

CI_SOURCE = "ci"
_HISTORY_LIMIT = 40    # recent runs listed below the tiles
_SCAN_LIMIT = 400      # rows scanned to derive latest-per-suite + history

# CI posts on every push to main, which happens most business days but not
# reliably over a weekend — a plain 24h cadence would fire the stalled banner
# falsely every Saturday morning. 84 hours (3.5 days) covers a normal
# Friday-afternoon-to-Monday-afternoon gap without widening enough to hide a
# feed that has actually been dead for the better part of a week.
_STALLED_AFTER_HOURS = 84
_EXPECTED_CADENCE = "daily"


def _age(when, now) -> str:
    if when is None:
        return "—"
    secs = max(0, int((now - when).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h"
    return f"{hrs // 24}d"


def _status_of(sig: library_client.SignalRecord) -> str:
    """pass | fail | unknown from meta.status, defensively."""
    st = (sig.meta or {}).get("status") if isinstance(sig.meta, dict) else None
    st = str(st).strip().lower() if isinstance(st, str) else ""
    return st if st in ("pass", "fail") else "unknown"


def _run_link(sig: library_client.SignalRecord) -> str:
    url = (sig.meta or {}).get("run_url") if isinstance(sig.meta, dict) else None
    num = (sig.meta or {}).get("run_number") if isinstance(sig.meta, dict) else None
    label = f"Run #{escape(str(num))}" if num not in (None, "") else "run ↗"
    safe_url = safe_http_url(url)
    if safe_url is not None:
        return (f'<a class="run" href="{escape(safe_url, quote=True)}" target="_blank" '
                f'rel="noopener">{label}</a>')
    return f'<span class="run">{label}</span>' if num not in (None, "") else ""


def _jobs_chips(sig: library_client.SignalRecord) -> str:
    """Per-job result chips from meta.jobs {name: result}."""
    jobs = (sig.meta or {}).get("jobs") if isinstance(sig.meta, dict) else None
    if not isinstance(jobs, dict) or not jobs:
        return ""
    chips = []
    for name, result in list(jobs.items())[:12]:
        ok = str(result).strip().lower() == "success"
        cls = "ok" if ok else "bad"
        chips.append(f'<span class="job {cls}">{escape(str(name))}</span>')
    return '<div class="jobs">' + "".join(chips) + "</div>"


def _tile(sig: library_client.SignalRecord, now) -> str:
    """The latest run for one (product, suite): a status-colored tile."""
    status = _status_of(sig)
    suite = sig.stream or sig.kind
    when = sig.produced_at or sig.ingested_at
    body = (sig.body or "").strip()
    body_html = f'<p class="summary">{escape(body[:280])}</p>' if body else ""
    return f'''
    <article class="tile {status}">
      <div class="tile-top">
        <span class="dot"></span>
        <span class="prod">{escape(sig.product)}</span>
        <span class="suite">{escape(suite)}</span>
        <span class="age">{escape(_age(when, now))}</span>
      </div>
      <div class="verdict">{status.upper()}</div>
      {body_html}
      {_jobs_chips(sig)}
      <div class="tile-meta">{_run_link(sig)}</div>
    </article>'''


def _history_row(sig: library_client.SignalRecord, now) -> str:
    status = _status_of(sig)
    suite = sig.stream or sig.kind
    when = sig.produced_at or sig.ingested_at
    return f'''
    <div class="hrow {status}">
      <span class="dot"></span>
      <div class="hmain">
        <div class="htitle"><b>{escape(sig.product)}</b> · {escape(suite)}</div>
        <div class="hsub">{status} · {escape(_age(when, now))} ago</div>
      </div>
      {_run_link(sig)}
    </div>'''


def freshness(recent: list, now) -> ds.Freshness:
    """This dashboard's own freshness verdict — read verbatim by this page's
    own banner and by /overview's E2E card, so the two can't disagree
    (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment 902). `recent` is the same
    source=ci scan the caller already fetched; this derives no new request."""
    newest = None
    for sig in recent:
        when = sig.produced_at or sig.ingested_at
        if when is not None and (newest is None or when > newest):
            newest = when
    if newest is None:
        return ds.Freshness(text="no freshness reported", stalled=None)
    text = f"{newest.date().isoformat()}, {_age(newest, now)} ago"
    hours_stale = (now - newest).total_seconds() / 3600
    return ds.Freshness(
        text=text, stalled=hours_stale > _STALLED_AFTER_HOURS,
        cadence=_EXPECTED_CADENCE,
        reason=("The mini's CI poller (bin/ci_result_fetch.py in the Library "
                "repo) writes results here every 15 minutes; a stall this "
                "long means the poller is down or CI hasn't produced a new "
                "run, not that the write path is missing."),
    )


def _stalled_banner(recent: list, now) -> str:
    """A banner above the tiles when the newest CI signal in hand is older
    than `_STALLED_AFTER_HOURS` — the feed that stopped, rendered honestly
    rather than as a quiet green tile."""
    return ds.stalled_banner_html("CI test results", freshness(recent, now))


def _page(tiles_html: str, history_html: str, empty: bool, stalled_banner_html: str = "") -> str:
    tiles_block = tiles_html or (
        '<div class="allclear">No CI results captured yet. The mini\'s CI '
        'poller writes results to the Library every 15 minutes; they show '
        'here once captured.</div>')
    history_block = (
        '<div class="sec">Recent runs</div><div class="history">' + history_html + '</div>'
    ) if history_html else ""
    body = f'''<div class="ops">
  <header class="ops-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / engineering / e2e</div>
    <h1>CI &amp; test results</h1>
    <p class="lede">The latest run per product and suite, live from the signal store. Green passed, red failed — the surface that replaced the Slack e2e post.</p>
    <p class="scope-note">Only vivy runs below: it is the only end-to-end suite on the fleet. Every other repo runs per-PR CI instead, reported by the merge gate and <code>gh pr checks</code>, not by this page.</p>
  </header>
  <div class="wrap">
    {stalled_banner_html}
    <div class="sec">Latest per suite</div>
    <div class="tiles">{tiles_block}</div>
    {history_block}
    <div class="footnote">
      Each tile is the most recent <code>test-result</code> signal for a
      (product, suite). A poller on the mini fetches CI conclusions and
      writes them to the Library on a 15-minute timer; nothing here is
      customer-class. Signed in as <strong>__WHOAMI__</strong>.
    </div>
  </div>
</div>'''
    return ds.page_shell(
        title="CI & Test Results",
        nav_html=ds.nav_component("/engineering/e2e", "full"),
        body=body,
        extra_css=_CSS,
    )


def _when(sig: library_client.SignalRecord) -> dt.datetime:
    """The clock a row is ordered and dated by, floored so it is always
    comparable — see the note in `live_html`. Mirrors the `produced_at or
    ingested_at` fallback the row renderers use, and the Library's own
    `produced_at DESC NULLS LAST, ingested_at DESC` ordering clause."""
    return sig.produced_at or sig.ingested_at or dt.datetime.min


def live_html(scope: frozenset[str] = frozenset(), whole_fleet: bool = True) -> str:
    """Render from the Library (full-tier, server-gated). Internal-class CI
    signals only — the same coarse tier boundary every full-tier page uses.

    Restricted to `scope` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): an
    out-of-scope signal is dropped from `recent` before any tile, history
    row or stalled-banner verdict is computed from it — applied to the
    already-fetched list (rather than a per-product fan-out) so the
    latest-per-suite max and the ordering this function's own docstring
    establishes stay exactly as they were.

    `source=ci` is a Library filter now, not a Python one: unfiltered, this scan
    is 26 rows out of 18,455 live signals, 93 requests to find
    (issue_01KZW183XVJCKFGQT8HMXFJFTY)."""
    now = utcnow()
    # order="produced_at" asks the Library for `produced_at DESC NULLS LAST,
    # ingested_at DESC, id` (SIGNAL_ORDER_CLAUSES) — the same fallback
    # `_tile`, `_history_row` and `freshness` use to date each row (via
    # `_when`), so the sort clock and the display clock are the same clock by
    # construction. Named rather than cited by line: an earlier draft of this
    # comment pinned three line numbers that PR #239 had already shifted, so
    # it pointed a reader at unrelated code. Sorting client-side instead
    # would be silently wrong past `_SCAN_LIMIT`: it would fetch the top 400
    # by ingested_at (storage time) and reorder only those, so once CI volume
    # passes the cap the page would drop runs that belong on it rather than
    # the ones that don't (issue_01M1F4KN3XW0670TTWMM7PP8C1).
    recent = library_client.list_signals(
        source=[CI_SOURCE], cap=_SCAN_LIMIT, order="produced_at")
    if not whole_fleet:
        recent = [s for s in recent if s.product in scope]
    # Latest per (product, stream) by an explicit max over the same clock the
    # fetch was ordered by, rather than first-occurrence in `recent`. The two
    # agree today, but a max is correct even if the Library's ordering ever
    # changes underneath this call — first-occurrence would silently pin
    # whatever row now sorts first, with no error and no visual difference
    # from a correct tile.
    #
    # `_when` floors a missing timestamp at dt.datetime.min rather than
    # comparing None. SignalRecord annotates `ingested_at` as a plain
    # datetime, but nothing enforces that at runtime: library_client._iso
    # returns None for an empty or null value, so a malformed row would yield
    # None here and `None > None` is a TypeError.
    #
    # That shape is schema-impossible today, and this was checked rather than
    # left open: the Library declares `ingested_at DATETIME NOT NULL` in its
    # baseline migration and server-stamps the value on write, never taking it
    # from the caller. Kept anyway as cheap insurance against the annotation's
    # promise not holding at runtime — the cost is one `or`, and the failure it
    # guards drops the whole page to its static fallback. First-occurrence never
    # compared, so this max is the line that would newly raise — and a raise
    # in live_html() blanks the page to its static fallback, which this
    # module's own docstring forbids ("a malformed post must never blank the
    # page"). A floored row simply never wins the max, which is the right
    # answer for a row that cannot say when it happened.
    latest: dict[tuple[str, str], library_client.SignalRecord] = {}
    latest_when: dict[tuple[str, str], dt.datetime] = {}
    for sig in recent:
        key = (sig.product, sig.stream or sig.kind)
        when = _when(sig)
        if key not in latest or when > latest_when[key]:
            latest[key] = sig
            latest_when[key] = when
    tiles = sorted(latest.values(), key=lambda x: (x.product, x.stream or x.kind))
    tiles_html = "".join(_tile(sig, now) for sig in tiles)
    history_html = "".join(_history_row(sig, now) for sig in recent[:_HISTORY_LIMIT])
    banner_html = _stalled_banner(recent, now)
    return _page(tiles_html, history_html, empty=not recent, stalled_banner_html=banner_html)


def build_static() -> None:
    """Seed the static fallback at site/engineering-e2e.html, served only if a
    live render throws — an empty state, never a stale snapshot."""
    SITE.mkdir(parents=True, exist_ok=True)
    fallback = _page("", "", empty=True).replace(
        'No CI results captured yet.', 'Live view unavailable — reload in a moment.')
    (SITE / "engineering-e2e.html").write_text(fallback, encoding="utf-8")
    print("Wrote site/engineering-e2e.html (static fallback)")


_CSS = r"""
[hidden]{display:none!important;}
.ops{
  --paper:var(--bg);--hairline:var(--line);--hairline-strong:var(--line);
  --muted:var(--ink-2);--faint:var(--ink-3);
  --accent:var(--brand-ink);--accent-soft:var(--brand-wash);
  --good:var(--ok);--good-soft:color-mix(in srgb,var(--ok) 14%,var(--surface));
  --bad:var(--err);--bad-soft:color-mix(in srgb,var(--err) 12%,var(--surface));
}
.ops{line-height:1.5;}
.ops .wrap{max-width:900px;margin:0 auto;padding:0 2px 72px;}
.ops .ops-head{max-width:900px;margin:0 auto;padding:22px 2px 0;}
.ops .eyebrow-line{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);display:flex;align-items:center;gap:8px;}
.ops .eyebrow-line .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px var(--accent-soft);}
.ops h1{font-size:1.55rem;letter-spacing:-.02em;margin:6px 0 2px;text-wrap:balance;font-weight:640;}
.ops .lede{color:var(--muted);font-size:.92rem;margin:0 0 4px;}
.ops .scope-note{color:var(--faint);font-size:.8rem;margin:0 0 4px;}
.ops .scope-note code{font-family:var(--mono);color:var(--muted);font-size:.76rem;}
.ops .sec{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);
margin:26px 2px 12px;display:flex;align-items:center;gap:10px;}
.ops .sec::after{content:"";flex:1;height:1px;background:var(--hairline);}
.ops .tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;}
.ops .tile{background:var(--surface);border:1px solid var(--hairline);border-radius:14px;box-shadow:var(--shadow);
padding:14px 16px;border-left:4px solid var(--faint);}
.ops .tile.pass{border-left-color:var(--good);}
.ops .tile.fail{border-left-color:var(--bad);}
.ops .tile-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.ops .tile .dot,.ops .hrow .dot{width:9px;height:9px;border-radius:50%;background:var(--faint);flex:0 0 auto;}
.ops .tile.pass .dot,.ops .hrow.pass .dot{background:var(--good);}
.ops .tile.fail .dot,.ops .hrow.fail .dot{background:var(--bad);}
.ops .prod{font-family:var(--mono);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);
background:var(--accent-soft);padding:2px 7px;border-radius:6px;}
.ops .suite{font-family:var(--mono);font-size:.74rem;color:var(--muted);}
.ops .age{margin-left:auto;font-family:var(--mono);font-size:.72rem;color:var(--faint);white-space:nowrap;}
.ops .verdict{font-family:var(--mono);font-weight:700;font-size:1.05rem;letter-spacing:.04em;margin:9px 0 0;color:var(--faint);}
.ops .tile.pass .verdict{color:var(--good);}
.ops .tile.fail .verdict{color:var(--bad);}
.ops .summary{color:var(--muted);font-size:.85rem;margin:6px 0 0;white-space:pre-wrap;overflow-wrap:anywhere;}
.ops .jobs{display:flex;flex-wrap:wrap;gap:5px;margin-top:9px;}
.ops .job{font-family:var(--mono);font-size:.66rem;padding:2px 7px;border-radius:5px;border:1px solid var(--hairline-strong);color:var(--muted);}
.ops .job.ok{background:var(--good-soft);color:var(--good);border-color:transparent;}
.ops .job.bad{background:var(--bad-soft);color:var(--bad);border-color:transparent;}
.ops .tile-meta{margin-top:10px;}
.ops .run{font-family:var(--mono);font-size:.74rem;color:var(--accent);text-decoration:none;}
.ops a.run:hover{text-decoration:underline;}
.ops .history{display:flex;flex-direction:column;gap:8px;}
.ops .hrow{display:flex;align-items:center;gap:10px;background:var(--surface);border:1px solid var(--hairline);
border-radius:11px;padding:10px 13px;}
.ops .hmain{flex:1;min-width:0;}
.ops .htitle{font-size:.9rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.ops .hsub{font-family:var(--mono);font-size:.71rem;color:var(--faint);margin-top:2px;}
.ops .allclear{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.82rem;padding:34px 0;grid-column:1/-1;}
.ops .footnote{margin-top:34px;padding-top:16px;border-top:1px solid var(--hairline);color:var(--faint);font-size:.8rem;}
.ops .footnote code{font-family:var(--mono);color:var(--muted);font-size:.76rem;}
@media (max-width:520px){.ops h1{font-size:1.35rem;}}
"""


if __name__ == "__main__":
    build_static()
