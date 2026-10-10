#!/usr/bin/env python3.11
"""The merge-queue index — /engineering/merge-queue (ARCH-359 step 3, read-side).

Every open PR across the org, in the box, so the operator's merge queue lives here instead
of scattered across github.com — the durable form of the "print ready-PR URLs
every turn" habit (the queue IS the list, and it survives a session close). Rows
sort by most-recent activity (updated_at desc, the operator 2026-07-17); the CI-green +
conflict-free ones carry a ✓ badge and the "Ready" filter chip.

Source (the operator's 2026-07-17 fork): the list is derived LIVE from the GitHub engine —
`gh search prs` for the repos that have open PRs, then `gh pr list` per repo for
each PR's review decision + CI rollup — TTL-cached so a page reload doesn't
re-shell. This is the read-side step: the box reads the engine's state. The
memory-first `pr_ready` signal (a co writes it at cycle-clear) lands with the
step-5 authorship cutover; until then the engine's own reviewDecision +
statusCheckRollup are the accurate source.

Full-tier only (server.py's _full_only_routes), same posture as /engineering/changelog:
a contractor gets a plain 404. This module shells out to `gh` (the mini's login) and
renders; it never writes. No merge/close actions here — those are step 4, human-gated.
"""
from __future__ import annotations

import json
import subprocess
import time
from html import escape

import design_system as ds
import orient_time
from url_safety import safe_http_url
from paths import STATE_ROOT

SITE = STATE_ROOT / "site"

ORG = "convivy"
_GH_TIMEOUT = 20
_CACHE_TTL = 180  # seconds; a merge queue a few minutes stale is fine
_PR_LIST_FIELDS = ("number,title,url,isDraft,createdAt,updatedAt,author,reviewDecision,"
                   "mergeable,statusCheckRollup,headRefOid")

# module-level TTL cache: (fetched_at, rows) — time.time() is fine in app code
# (control_db.new_ulid makes the same call); this is not a resumable workflow.
_cache: tuple[float, list[dict]] | None = None


# --- fetch (live from the GitHub engine) --------------------------------------


def _gh(args: list[str]) -> list | dict | None:
    """Run a `gh` command returning JSON. None on any failure — a single repo's
    fetch failing must not blank the whole queue."""
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=_GH_TIMEOUT)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    text = out.stdout.strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _repos_with_open_prs() -> list[str]:
    """One org-wide search for the repos that currently have an open PR, so the
    per-repo enrichment only hits repos that need it."""
    data = _gh(["search", "prs", "--owner", ORG, "--state", "open", "--limit", "200",
                "--json", "repository"])
    if not isinstance(data, list):
        return []
    return sorted({p.get("repository", {}).get("name", "") for p in data} - {""})


def _open_prs_for_repo(repo: str) -> list[dict]:
    data = _gh(["pr", "list", "--repo", f"{ORG}/{repo}", "--state", "open",
                "--limit", "100", "--json", _PR_LIST_FIELDS])
    if not isinstance(data, list):
        return []
    for pr in data:
        pr["repo"] = repo
    return data


def cached_open_prs() -> list[dict] | None:
    """The cached PR list if it is warm, else None. NEVER fetches.

    This exists for the Home "waiting on you" strip (ARCH-362 Phase 2). Home is
    the most-requested page on the site, and `_fetch_open_prs` fans out one `gh`
    call per repo with a 20s timeout each — so calling it from Home on a cold
    cache would block the landing page for seconds, which is the exact regression
    class behind the unresolved team.convivy.com slowness.

    So the strip reads the cache opportunistically: warm, it shows a real count;
    cold, it shows no number and links through to the page, which does the fetch.
    A missing count on one tile is a far better failure than a slow Home.
    """
    if _cache is not None and (time.time() - _cache[0]) < _CACHE_TTL:
        return _cache[1]
    return None


def _fetch_open_prs(force: bool = False) -> list[dict]:
    """The assembled, classified open-PR list, TTL-cached."""
    global _cache
    now = time.time()
    if not force and _cache is not None and (now - _cache[0]) < _CACHE_TTL:
        return _cache[1]
    rows: list[dict] = []
    for repo in _repos_with_open_prs():
        for pr in _open_prs_for_repo(repo):
            rows.append(_classify(pr))
    # Most recent activity first (the operator 2026-07-17). ISO-8601 timestamps sort
    # lexicographically in chronological order, so a plain reverse string sort on
    # updated_at is newest-first; created_at breaks a tie. The cleared/blocked
    # status stays a per-card badge + the "Ready" chip, not the sort key.
    rows.sort(key=lambda r: (r["updated_at"] or r["created_at"], r["created_at"]), reverse=True)
    _cache = (now, rows)
    return rows


# --- classification (pure) ----------------------------------------------------


def _ci_status(rollup) -> str:
    """Roll a statusCheckRollup list up to passing | failing | pending | none, by
    PRIORITY not array order: any failure → failing; else any pending → pending;
    else any success → passing. Scan the whole list before deciding — a failing
    check that sorts after a pending one must still make the PR read failing."""
    if not isinstance(rollup, list) or not rollup:
        return "none"
    any_ok = any_pending = False
    for c in rollup:
        # CheckRun carries status/conclusion; StatusContext carries state.
        state = (c.get("state") or "").upper()
        conclusion = (c.get("conclusion") or "").upper()
        status = (c.get("status") or "").upper()
        if state in ("FAILURE", "ERROR") or conclusion in (
                "FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"):
            return "failing"  # a single failure fails the rollup, regardless of the rest
        if state in ("PENDING", "EXPECTED") or (status and status != "COMPLETED"):
            any_pending = True
        elif state == "SUCCESS" or conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            any_ok = True
    if any_pending:
        return "pending"
    return "passing" if any_ok else "pending"


def _classify(pr: dict) -> dict:
    # `reviewDecision` is informational only: our review cycle posts verdicts as
    # PR comments, not GitHub review approvals, so an in-box-cleared PR still reads
    # REVIEW_REQUIRED here. The structured "our cycle cleared" signal is `pr_ready`,
    # deferred to the step-5 authorship cutover (the operator 2026-07-17). So step-3 readiness
    # is MECHANICAL — CI green, no conflict, not a draft — and the human applies the
    # in-box review judgment on top.
    decision = (pr.get("reviewDecision") or "").upper()
    ci = _ci_status(pr.get("statusCheckRollup"))
    mergeable = (pr.get("mergeable") or "").upper()
    draft = bool(pr.get("isDraft"))
    conflicting = mergeable == "CONFLICTING"
    changes = decision == "CHANGES_REQUESTED"
    cleared = ci == "passing" and not draft and not conflicting
    if cleared:
        group = "cleared"
    elif draft:
        group = "draft"
    elif conflicting or ci == "failing":
        group = "blocked"
    else:
        group = "in-cycle"
    return {
        "repo": pr.get("repo", ""),
        "number": pr.get("number"),
        "title": pr.get("title") or "",
        "url": pr.get("url") or "",
        "author": (pr.get("author") or {}).get("login") or "unknown",
        "created_at": pr.get("createdAt") or "",
        "updated_at": pr.get("updatedAt") or "",
        "decision": decision, "ci": ci, "mergeable": mergeable,
        "draft": draft, "conflicting": conflicting, "changes": changes,
        "cleared": cleared, "group": group,
    }


# --- render -------------------------------------------------------------------

_CI_LABEL = {"passing": "CI green", "failing": "CI failing", "pending": "CI running", "none": "no CI"}
_DECISION_LABEL = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested",
                   "REVIEW_REQUIRED": "review pending", "": "review pending"}


def _reason(r: dict) -> str:
    """The one-line 'why it is / isn't ready' shown under the title. Mechanical
    readiness (CI + conflict + draft); review clearance is the human's call until
    the pr_ready signal lands (step 5), so an APPROVED decision is a bonus note,
    never the gate."""
    if r["cleared"]:
        return "CI green · no conflicts — approved" if r["decision"] == "APPROVED" else "CI green · no conflicts"
    bits = [_CI_LABEL.get(r["ci"], r["ci"])]
    if r["conflicting"]:
        bits.append("merge conflict")
    if r["changes"]:
        bits.append("changes requested")
    if r["draft"]:
        bits.append("draft")
    return " · ".join(bits)


def _short_date(iso: str) -> str:
    """An ISO-8601 timestamp → a short 'Jul 18' date in MST; the raw string if
    unparseable.

    GitHub sends UTC. Formatting that directly put every PR opened or merged
    after 17:00 MST on the FOLLOWING day, which is the window most of our merges
    land in — so the page was reliably wrong for exactly the rows the operator looks at.
    """
    if not iso:
        return "—"
    when = orient_time.to_mst(iso)
    return when.strftime("%b %-d") if when else iso[:10]


def _row(r: dict) -> str:
    icon = {"cleared": "✓", "blocked": "✕", "draft": "◦"}.get(r["group"], "·")
    label = escape(f"{r['repo']} #{r['number']}")
    safe_url = safe_http_url(r["url"])
    title_link = (f'<a href="{escape(safe_url, quote=True)}" target="_blank" rel="noopener">{label}</a>'
                  if safe_url is not None else label)
    dates = f'opened {escape(_short_date(r["created_at"]))} · updated {escape(_short_date(r["updated_at"]))}'
    return f'''
    <article class="pr" data-repo="{escape(r['repo'], quote=True)}" data-group="{escape(r['group'], quote=True)}">
      <div class="status {escape(r['group'], quote=True)}">{icon}</div>
      <div class="pr-main">
        <div class="pr-top"><span class="ref">{title_link}</span>
          <span class="who">{escape(r['author'])}</span></div>
        <div class="pr-title">{escape(r['title'])}</div>
        <div class="pr-reason">{escape(_reason(r))}</div>
        <div class="pr-dates">{dates}</div>
      </div>
    </article>'''


def _chips(repos: list[tuple[str, int]], total: int, ready: int) -> str:
    out = [f'<button class="chip" data-repo="all" aria-pressed="true">All <span class="n">{total}</span></button>']
    if ready:
        out.append(f'<button class="chip ready" data-repo="__ready__" aria-pressed="false">'
                   f'Ready <span class="n">{ready}</span></button>')
    for repo, n in repos:
        out.append(f'<button class="chip" data-repo="{escape(repo, quote=True)}" '
                   f'aria-pressed="false">{escape(repo)} <span class="n">{n}</span></button>')
    return "".join(out)


def _page(rows: list[dict]) -> str:
    ready = sum(1 for r in rows if r["cleared"])
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["repo"]] = counts.get(r["repo"], 0) + 1
    repos = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    cards = "".join(_row(r) for r in rows)
    block = cards or '<div class="allclear">No open pull requests. The queue is clear.</div>'
    lede = (f'<span class="count">{ready}</span> CI-green &amp; conflict-free, '
            f'{len(rows)} open in all.' if rows else "No open pull requests.")
    body = f'''<div class="mq">
  <header class="mq-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / engineering / merge queue</div>
    <h1>Merge queue</h1>
    <p class="lede">{lede} Most-recent activity first; the ✓ badge + Ready filter mark the CI-green, conflict-free ones.</p>
    <div class="chips" id="chips">{_chips(repos, len(rows), ready)}</div>
  </header>
  <div class="wrap">
    <div class="feed" id="feed">{block}
      <div class="empty" id="empty">Nothing matching in this filter.</div>
    </div>
    <div class="footnote">Open PRs read live from the GitHub engine (review decision + CI status),
    most-recent activity first. Merge stays on github.com until the in-box merge
    action ships (ARCH-359 step 4). Signed in as <strong>__WHOAMI__</strong>.</div>
  </div>
<script>{_JS}</script>
</div>'''
    return ds.page_shell(title="Merge queue",
                         nav_html=ds.nav_component("/engineering/merge-queue", "full"),
                         body=body, extra_css=_CSS)


def live_html() -> str:
    """/engineering/merge-queue — the org's open PRs, most-recent activity first."""
    try:
        rows = _fetch_open_prs()
    except Exception:
        rows = []
    return _page(rows)


def build_static() -> None:
    """Seed the empty-state fallback at site/engineering-merge-queue.html (served
    only if a live render throws). Deliberately does NOT call `gh` — serve.sh runs
    this at startup, and shelling out to the network there would stall the boot;
    the fallback is intentionally empty, never a stale queue snapshot."""
    SITE.mkdir(parents=True, exist_ok=True)
    html = _page([]).replace(
        "No open pull requests. The queue is clear.",
        "Live view unavailable — reload in a moment.")
    (SITE / "engineering-merge-queue.html").write_text(html, encoding="utf-8")
    print("Wrote site/engineering-merge-queue.html (static fallback)")


_CSS = r"""
[hidden]{display:none!important;}
.mq{--muted:var(--ink-2);--faint:var(--ink-3);--accent:var(--brand-ink);--accent-soft:var(--brand-wash);
--good:var(--ok);--good-soft:color-mix(in srgb,var(--ok) 14%,var(--surface));
--bad:var(--err);--bad-soft:color-mix(in srgb,var(--err) 12%,var(--surface));
--warn-soft:color-mix(in srgb,var(--warn) 16%,var(--surface));line-height:1.5;}
.mq .wrap{max-width:760px;margin:0 auto;padding:0 2px 72px;}
.mq .mq-head{max-width:760px;margin:0 auto;padding:22px 2px 0;}
.mq .eyebrow-line{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);display:flex;align-items:center;gap:8px;}
.mq .eyebrow-line .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px var(--accent-soft);}
.mq h1{font-size:1.55rem;letter-spacing:-.02em;margin:6px 0 2px;font-weight:640;}
.mq .lede{color:var(--muted);font-size:.92rem;margin:0 0 12px;}
.mq .count{color:var(--good);font-variant-numeric:tabular-nums;font-weight:680;}
.mq .chips{display:flex;gap:7px;overflow-x:auto;padding:0 0 14px;scrollbar-width:none;}
.mq .chips::-webkit-scrollbar{display:none;}
.mq .chip{flex:0 0 auto;font-family:var(--mono);font-size:.74rem;padding:6px 11px;border-radius:999px;
border:1px solid var(--line);background:var(--surface);color:var(--muted);cursor:pointer;white-space:nowrap;}
.mq .chip[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink);}
.mq .chip.ready{color:var(--good);border-color:color-mix(in srgb,var(--ok) 40%,var(--line));}
.mq .chip.ready[aria-pressed="true"]{background:var(--good);color:var(--surface);border-color:var(--good);}
.mq .chip .n{opacity:.6;margin-left:5px;}
.mq .feed{display:flex;flex-direction:column;gap:9px;}
.mq .pr{display:flex;align-items:flex-start;gap:11px;background:var(--surface);border:1px solid var(--line);
border-radius:12px;padding:12px 14px;}
.mq .pr[data-group="cleared"]{border-color:color-mix(in srgb,var(--ok) 34%,var(--line));
background:color-mix(in srgb,var(--ok) 5%,var(--surface));}
.mq .status{flex:0 0 auto;width:24px;height:24px;border-radius:7px;display:grid;place-items:center;font-size:.78rem;font-weight:700;margin-top:1px;}
.mq .status.cleared{background:var(--good-soft);color:var(--good);}
.mq .status.blocked{background:var(--bad-soft);color:var(--bad);}
.mq .status.in-cycle{background:var(--warn-soft);color:var(--warn);}
.mq .status.draft{background:var(--surface-2);color:var(--faint);}
.mq .pr-main{flex:1;min-width:0;}
.mq .pr-top{display:flex;align-items:baseline;gap:9px;font-family:var(--mono);font-size:.74rem;}
.mq .pr-top .ref a{color:var(--accent);text-decoration:none;font-weight:600;}
.mq .pr-top .ref a:hover{text-decoration:underline;}
.mq .pr-top .who{color:var(--faint);margin-left:auto;white-space:nowrap;}
.mq .pr-title{font-size:.94rem;font-weight:540;margin:3px 0 0;overflow-wrap:anywhere;}
.mq .pr-reason{font-family:var(--mono);font-size:.72rem;color:var(--faint);margin-top:3px;}
.mq .pr[data-group="cleared"] .pr-reason{color:var(--good);}
.mq .pr-dates{font-family:var(--mono);font-size:.68rem;color:var(--faint);margin-top:3px;opacity:.85;}
.mq .empty{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.8rem;padding:26px 0;display:none;}
.mq .allclear{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.82rem;padding:34px 0;}
.mq .footnote{margin-top:32px;padding-top:16px;border-top:1px solid var(--line);color:var(--faint);font-size:.8rem;}
@media (max-width:520px){.mq h1{font-size:1.35rem;}}
"""

_JS = r"""
(function(){
  var chips=document.getElementById('chips'),feed=document.getElementById('feed'),
      empty=document.getElementById('empty'),active='all';
  function apply(){var vis=0,total=0;
    feed.querySelectorAll('.pr').forEach(function(c){total++;
      var m=active==='all'||(active==='__ready__'?c.dataset.group==='cleared':c.dataset.repo===active);
      c.style.display=m?'':'none';if(m)vis++;});
    empty.style.display=(vis===0&&total>0)?'block':'none';}
  chips.addEventListener('click',function(e){var chip=e.target.closest('.chip');if(!chip)return;
    chips.querySelectorAll('.chip').forEach(function(c){c.setAttribute('aria-pressed','false');});
    chip.setAttribute('aria-pressed','true');active=chip.dataset.repo;apply();});
})();
"""


if __name__ == "__main__":
    build_static()
