#!/usr/bin/env python3.11
"""Two more Archives sections: /archives/prs and /archives/changelogs.

The operator named four memory shapes worth their own browsable section: Briefings
(already its own page, ops_reports_build.py), PRs, Proposed Epics (a separate
change — see the `_ARCHIVES_SUBNAV` comment in design_system.py for the
current state of the `maturity`-field blocker and why it's re-measured rather
than quoted) and Changelogs. This module builds the last two.

**Why a fair per-product sample, not one flat capped fetch.** `pr_comment` is
19,008 rows and `changelog` is 1000+ on the live database — comparable in
scale to what made `GET /signals/search` unsuitable for this
(issue_01M0ZXSVZG8K9DGG2CSX0JYE5K post-filters a retrieved page, so 5
product-scoped rows requested can come back as 1). A single "most recent N
across every product" fetch has the same shape of problem one level up: if one
repo merges or comments heavily in a short window, its rows fill the whole
capped window and every other product's PRs/changelog entries silently vanish
from the page — the product filter would then show a blank page for a product
that has real data, just none recent enough to survive the cap. `fan_out`
avoids this by asking the Library for each known product's own top-N
independently (`list_signals(product=...)`, the pre-filter
`library_client.list_signals` gained for exactly this) and merging the
results, rather than asking for one flat top-N across every product and
hoping every product is represented in it. There is no further slice to a
global cap afterward — every product keeps its own up-to-N rows.

**PRs are grouped by (product, PR number), not rendered as a flat comment
feed** (the operator: "pull out PR comments and description, and organize them by PR &
Product"). `github_capture.signal_from_issue_comment` sets `kind="pr_comment"`
with `refs={"pr": <int>, "url": ...}` and `product=<repo>` on every row it
writes — the grouping key is reliably present in every row the current
capture code produces, so grouping is possible; nothing here should raise if
a legacy or foreign row lacks it, so `_pr_number` returns `None` for that row
rather than crashing — `_group_prs` routes a `None` into its `unlinked`
return value instead of dropping the row, and `prs_live_html` renders that
list under its own "no resolvable PR reference" section rather than silently
losing it.

**"description" is the merge-time record, not another comment.**
`github_capture.signal_from_merge` writes one `kind="changelog",
source="github"` signal per merged PR, body = title + description, keyed by
the same `(product, pr)` pair (`dedup_key=f"gh:{repo}:pr{number}:merged"`, so
at most one per PR). That is the PR's own description; joining it in by
`(product, pr)` is what puts it under the same group as its comments. Only
merged PRs have one — an open PR's group shows only its comments, which is
correct: nothing has generated its description signal yet.
"""
from __future__ import annotations

import datetime as dt
from html import escape

import design_system as ds
import library_client
import orient_time
from markdown_render import md_to_html
from models import utcnow
from url_safety import safe_http_url
from paths import STATE_ROOT

SITE = STATE_ROOT / "site"

# The GitHub org every `product` (repo) signal comes from
# (github_capture.ORG) — duplicated as a plain constant rather than importing
# the capture script into a live-serving builder, which pulls in gh/subprocess
# plumbing this module has no use for.
_GH_ORG = "convivy"

# Per-product cap for the fan-out fetch (`fan_out`). Not the page's total
# render cap — the total is `cap_per_product * len(known products)`, which
# self-limits to the real number of active repos rather than a fixed ceiling
# that would need raising as the org grows.
_PR_CAP_PER_PRODUCT = 150
_DESC_CAP_PER_PRODUCT = 150
_CHANGELOG_CAP_PER_PRODUCT = 150

_BODY_CLIP = 1200


def fan_out(kind: str, *, cap_per_product: int, source=None,
            products: list[str] | None = None) -> list[library_client.SignalRecord]:
    """Every product in `products`' own top-`cap_per_product` rows of `kind`
    (optionally further scoped by `source`), merged. Exact per product, unlike
    a single flat `cap` across every product — see the module docstring.

    `products=None` fans out over every product `signal_facets` reports — the
    whole-fleet default. A scoped, non-whole-fleet caller (E19·4,
    issue_01M35K5JQ7Z7MHYZGWRSB5F85M) passes its own viewer scope instead, so
    an out-of-scope product is never even fetched, let alone rendered."""
    if products is None:
        facets = library_client.signal_facets(kind=[kind])
        products = facets.products
    rows: list[library_client.SignalRecord] = []
    for product in products:
        rows.extend(library_client.list_signals(
            kind=[kind], product=product, source=source, cap=cap_per_product))
    return rows


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
    days = hrs // 24
    if days < 60:
        return f"{days}d"
    return f"{days // 30}mo"


def _sig_time(sig: library_client.SignalRecord) -> dt.datetime:
    return sig.produced_at or sig.ingested_at


# ---------------------------------------------------------------------------
# PRs (/archives/prs)
# ---------------------------------------------------------------------------


def _pr_number(sig: library_client.SignalRecord) -> int | None:
    """The PR number a `pr_comment`/`changelog` signal's `refs` names, or None
    if it isn't there or isn't the shape `github_capture` writes. `bool` is
    excluded explicitly — it is a subclass of `int` in Python, and True/False
    are not PR numbers even though `isinstance(True, int)` is true."""
    refs = sig.refs if isinstance(sig.refs, dict) else {}
    pr = refs.get("pr")
    if isinstance(pr, int) and not isinstance(pr, bool):
        return pr
    return None


def _pr_url(product: str, pr: int) -> str:
    return f"https://github.com/{_GH_ORG}/{product}/pull/{pr}"


def _group_prs(comments, descriptions):
    """Group `pr_comment` rows by (product, PR number), attach each PR's
    merge-time description (if one exists yet), and return
    `(groups, unlinked)` — `unlinked` holds every comment whose `refs` carried
    no usable PR number, so a row that doesn't fit the grouping key is still
    in the page rather than dropped."""
    groups: dict[tuple[str, int], dict] = {}
    unlinked = []
    for sig in comments:
        pr = _pr_number(sig)
        if pr is None:
            unlinked.append(sig)
            continue
        key = (sig.product, pr)
        g = groups.setdefault(key, {"product": sig.product, "pr": pr,
                                    "description": None, "comments": []})
        g["comments"].append(sig)
    for sig in descriptions:
        pr = _pr_number(sig)
        if pr is None:
            continue
        key = (sig.product, pr)
        g = groups.setdefault(key, {"product": sig.product, "pr": pr,
                                    "description": None, "comments": []})
        # dedup_key gh:{repo}:pr{number}:merged is one signal per PR, so this
        # assigns at most once per group; a later row (there shouldn't be one)
        # would just overwrite with an equally-valid description of the same PR.
        g["description"] = sig
    return groups, unlinked


def _clip(body: str, limit: int) -> str:
    """`body` is a `SignalRecord.body` (or the same, pre-clipped) — already
    stripped of any leading `agentctl-review` stamp by `library_client`
    (`SignalRecord.body`'s own docstring), so this only clips length. Neither
    this function nor any caller below needs to strip the stamp itself."""
    body = (body or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + " …"


def _comment_html(sig: library_client.SignalRecord, now) -> str:
    body = _clip(sig.body, _BODY_CLIP)
    body_html = f'<div class="c-body">{md_to_html(body)}</div>' if body else ""
    url = (sig.refs or {}).get("url") if isinstance(sig.refs, dict) else None
    safe_url = safe_http_url(url)
    link = (f'<a class="c-src" href="{escape(safe_url, quote=True)}" target="_blank" '
            f'rel="noopener">↗</a>' if safe_url is not None else "")
    return f'''
      <div class="comment">
        <div class="c-top"><span class="c-who">{escape(sig.actor)}</span>
          <span class="c-age">{escape(_age(_sig_time(sig), now))}</span>{link}</div>
        {body_html}
      </div>'''


def _pr_group_html(g: dict, now) -> str:
    product, pr = g["product"], g["pr"]
    comments = sorted(g["comments"], key=_sig_time)  # chronological within a PR
    activity = max([_sig_time(c) for c in comments]
                   + ([_sig_time(g["description"])] if g["description"] else []),
                   default=None)
    desc = g["description"]
    desc_html = ""
    if desc is not None:
        desc_html = (f'<div class="pr-desc">{md_to_html(_clip(desc.body, _BODY_CLIP))}</div>')
    comments_html = "".join(_comment_html(c, now) for c in comments)
    return f'''
    <article class="pr-group" data-prod="{escape(product, quote=True)}">
      <div class="pr-top">
        <span class="prod">{escape(product)}</span>
        <a class="prnum" href="{escape(_pr_url(product, pr), quote=True)}"
           target="_blank" rel="noopener">PR #{pr}</a>
        <span class="pr-count">{len(comments)} comment{"" if len(comments) == 1 else "s"}</span>
        <span class="age">{escape(_age(activity, now))}</span>
      </div>
      {desc_html}
      <div class="pr-comments">{comments_html or '<p class="none">No captured comments.</p>'}</div>
    </article>'''


def _unlinked_html(unlinked: list, now) -> str:
    """Rendered INSIDE the same `#groups` container the product-chip filter
    queries (`.pr-group` elements) — an unlinked comment must narrow with the
    same product filter every other section uses, not sit outside it and stay
    visible regardless of the selected product."""
    if not unlinked:
        return ""
    rows = "".join(f'''
    <article class="pr-group unlinked" data-prod="{escape(sig.product, quote=True)}">
      <div class="pr-top"><span class="prod">{escape(sig.product)}</span>
        <span class="pr-count">no PR reference</span>
        <span class="age">{escape(_age(_sig_time(sig), now))}</span></div>
      <div class="pr-comments">{_comment_html(sig, now)}</div>
    </article>''' for sig in sorted(unlinked, key=_sig_time, reverse=True))
    return f'<div class="sec">Comments with no resolvable PR reference</div>{rows}'


def _chips(products: list[tuple[str, int]], total: int) -> str:
    out = [f'<button class="chip" data-prod="all" aria-pressed="true">All '
           f'<span class="n">{total}</span></button>']
    for prod, n in products:
        out.append(f'<button class="chip" data-prod="{escape(prod, quote=True)}" '
                   f'aria-pressed="false">{escape(prod)} <span class="n">{n}</span></button>')
    return "".join(out)


def prs_live_html(scope: frozenset[str], whole_fleet: bool) -> str:
    """/archives/prs — every captured PR comment, grouped by PR and product,
    with each PR's merge-time description attached where one exists.
    Full-tier only, rendered live per request (ARCH-182).

    `scope`/`whole_fleet` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): a scoped
    viewer's fan-out is restricted to their own projects, so an out-of-scope
    product's PRs are never fetched."""
    now = utcnow()
    products = None if whole_fleet else sorted(scope)
    comments = fan_out("pr_comment", cap_per_product=_PR_CAP_PER_PRODUCT, products=products)
    descriptions = fan_out("changelog", cap_per_product=_DESC_CAP_PER_PRODUCT,
                            source=["github"], products=products)
    groups, unlinked = _group_prs(comments, descriptions)

    def _group_activity(g):
        times = [_sig_time(c) for c in g["comments"]]
        if g["description"] is not None:
            times.append(_sig_time(g["description"]))
        return max(times) if times else dt.datetime.min

    ordered = sorted(groups.values(), key=_group_activity, reverse=True)
    counts: dict[str, int] = {}
    for g in groups.values():
        counts[g["product"]] = counts.get(g["product"], 0) + 1
    for sig in unlinked:
        counts[sig.product] = counts.get(sig.product, 0) + 1
    by_product = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    cards = "".join(_pr_group_html(g, now) for g in ordered)
    unlinked_html = _unlinked_html(unlinked, now)
    total = len(ordered) + len(unlinked)
    block = (cards + unlinked_html) or '<div class="allclear">No PRs captured yet.</div>'
    body = f'''<div class="ops">
  <header class="ops-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / archives / prs</div>
    <h1>PRs</h1>
    <p class="lede">Every captured pull request, grouped by PR and product — its
    description (once merged) and every comment underneath it.</p>
    <div class="chips" id="chips">{_chips(by_product, total)}</div>
  </header>
  <div class="wrap">
    <div class="queue" id="groups">{block}
      <div class="empty" id="empty">Nothing in this product yet.</div>
    </div>
    <div class="footnote">{len(groups)} PRs across {len(by_product)} products
    · {len(unlinked)} comment{"" if len(unlinked) == 1 else "s"} with no resolvable PR reference.
    Signed in as <strong>__WHOAMI__</strong>.</div>
  </div>
</div>
<script>{_PR_JS}</script>'''
    return ds.page_shell(
        title="PRs",
        nav_html=ds.nav_component("/archives", "full"),
        body=body,
        extra_css=_PR_CSS,
    )


# ---------------------------------------------------------------------------
# Changelogs (/archives/changelogs)
# ---------------------------------------------------------------------------


def _changelog_date(when: dt.datetime | None) -> str:
    # assume_utc because the control DB stores naive UTC (models.utcnow strips
    # tzinfo) — same conversion changelog_build._merged_date applies to the
    # same produced_at field.
    local = orient_time.to_mst(when, assume_utc=True)
    return local.strftime("%b %-d, %Y") if local else "—"


def _changelog_link(sig: library_client.SignalRecord) -> str:
    refs = sig.refs if isinstance(sig.refs, dict) else {}
    url = refs.get("url") or ""
    pr = refs.get("pr")
    label = f"PR #{pr}" if isinstance(pr, int) and not isinstance(pr, bool) else "source"
    safe = safe_http_url(url)
    if not safe:
        return ""
    return (f'<a href="{escape(safe, quote=True)}" target="_blank" rel="noopener">'
            f'{escape(label)}</a>')


def _changelog_card(sig: library_client.SignalRecord) -> str:
    body = _clip(sig.body, _BODY_CLIP)
    body_html = (f'<div class="summary">{md_to_html(body)}</div>' if body
                 else f'<p class="title">{escape(sig.stream or sig.product)}</p>')
    link = _changelog_link(sig)
    return f'''
    <article class="entry" data-prod="{escape(sig.product, quote=True)}">
      <div class="entry-top">
        <span class="prod">{escape(sig.product)}</span>
        <span class="src">{escape(sig.source)}</span>
        <span class="date">{escape(_changelog_date(sig.produced_at))}</span>
        {f'<span class="link">{link}</span>' if link else ""}
      </div>
      {body_html}
    </article>'''


def changelogs_live_html(scope: frozenset[str], whole_fleet: bool) -> str:
    """/archives/changelogs — every captured `changelog` signal (every source,
    unlike /engineering/changelog under Chart House, which shows only the
    co-authored entries) newest-first, filterable by product. This is the raw
    memory of what merged, not the curated announcement feed; the two pages
    read the same kind on purpose and differ only in which rows they keep.
    Full-tier only, rendered live per request (ARCH-182).

    `scope`/`whole_fleet` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): same
    restricted fan-out as `prs_live_html`."""
    products = None if whole_fleet else sorted(scope)
    rows = fan_out("changelog", cap_per_product=_CHANGELOG_CAP_PER_PRODUCT, products=products)
    rows.sort(key=_sig_time, reverse=True)
    counts: dict[str, int] = {}
    for sig in rows:
        counts[sig.product] = counts.get(sig.product, 0) + 1
    by_product = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    cards = "".join(_changelog_card(sig) for sig in rows)
    block = cards or '<div class="allclear">Nothing captured yet.</div>'
    body = f'''<div class="chglog">
  <header class="chglog-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / archives / changelogs</div>
    <h1>Changelogs</h1>
    <p class="lede">Every merge captured as a memory, every source, newest first —
    the raw material behind the in-box changelog, not the curated announcement.</p>
    <div class="chips" id="chips">{_chips(by_product, len(rows))}</div>
  </header>
  <div class="wrap">
    <div class="feed" id="feed">{block}
      <div class="empty" id="empty">Nothing in this product yet.</div>
    </div>
    <div class="footnote">{len(rows)} entries across {len(by_product)} products.
    See <a href="/engineering/changelog">the in-box changelog</a> for the
    authored announcement feed. Signed in as <strong>__WHOAMI__</strong>.</div>
  </div>
</div>
<script>{_CHANGELOG_JS}</script>'''
    return ds.page_shell(
        title="Changelogs",
        nav_html=ds.nav_component("/archives", "full"),
        body=body,
        extra_css=_CHANGELOG_CSS,
    )


# ---------------------------------------------------------------------------
# Shared token aliases + product-chip filtering (matches signals_build.py's
# `.ops` idiom — the "existing filter idiom on /archives" the spec asks every
# section to reuse).
# ---------------------------------------------------------------------------

_TOKEN_ALIASES = r"""
[hidden]{display:none!important;}
.ops,.chglog{
  --paper:var(--bg);--hairline:var(--line);--hairline-strong:var(--line);
  --muted:var(--ink-2);--faint:var(--ink-3);
  --accent:var(--brand-ink);--accent-soft:var(--brand-wash);
}
.ops,.chglog{line-height:1.5;}
.ops .wrap,.chglog .wrap{max-width:760px;margin:0 auto;padding:0 2px 72px;}
.ops .ops-head,.chglog .chglog-head{max-width:760px;margin:0 auto;padding:22px 2px 0;}
.ops .eyebrow-line,.chglog .eyebrow-line{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);display:flex;align-items:center;gap:8px;}
.ops .eyebrow-line .dot,.chglog .eyebrow-line .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px var(--accent-soft);}
.ops h1,.chglog h1{font-size:1.55rem;letter-spacing:-.02em;margin:6px 0 2px;text-wrap:balance;font-weight:640;}
.ops .lede,.chglog .lede{color:var(--muted);font-size:.92rem;margin:0 0 12px;}
.ops .chips,.chglog .chips{display:flex;gap:7px;overflow-x:auto;padding:0 0 14px;scrollbar-width:none;}
.ops .chips::-webkit-scrollbar,.chglog .chips::-webkit-scrollbar{display:none;}
.ops .chip,.chglog .chip{flex:0 0 auto;font-family:var(--mono);font-size:.74rem;padding:6px 11px;border-radius:999px;
border:1px solid var(--hairline-strong);background:var(--surface);color:var(--muted);cursor:pointer;white-space:nowrap;}
.ops .chip[aria-pressed="true"],.chglog .chip[aria-pressed="true"]{background:var(--ink);color:var(--paper);border-color:var(--ink);}
.ops .chip .n,.chglog .chip .n{opacity:.6;margin-left:5px;}
.ops .sec{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);
margin:26px 2px 12px;display:flex;align-items:center;gap:10px;}
.ops .sec::after{content:"";flex:1;height:1px;background:var(--hairline);}
.ops .empty,.chglog .empty{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.8rem;padding:30px 0;display:none;}
.ops .allclear,.chglog .allclear{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.82rem;padding:34px 0;}
.ops .footnote,.chglog .footnote{margin-top:34px;padding-top:16px;border-top:1px solid var(--hairline);color:var(--faint);font-size:.8rem;}
.ops .footnote a,.chglog .footnote a{color:var(--brand);text-decoration:underline;}
@media (max-width:520px){.ops h1,.chglog h1{font-size:1.35rem;}}
"""

_PR_CSS = _TOKEN_ALIASES + r"""
.ops .queue{display:flex;flex-direction:column;gap:12px;}
.ops .pr-group{background:var(--surface);border:1px solid var(--hairline);border-radius:14px;box-shadow:var(--shadow);overflow:hidden;padding:13px 16px 15px;}
.ops .pr-top{display:flex;align-items:center;gap:9px;flex-wrap:wrap;}
.ops .prod{font-family:var(--mono);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);background:var(--accent-soft);padding:2px 7px;border-radius:6px;}
.ops .prnum{font-family:var(--mono);font-size:.82rem;font-weight:650;color:var(--ink);text-decoration:none;}
.ops .prnum:hover{text-decoration:underline;}
.ops .pr-count{font-family:var(--mono);font-size:.72rem;color:var(--faint);}
.ops .age{margin-left:auto;font-family:var(--mono);font-size:.72rem;color:var(--faint);white-space:nowrap;}
.ops .pr-desc{margin:10px 0 4px;padding:10px 12px;background:var(--surface-2,rgba(127,127,127,.06));border-radius:8px;font-size:.88rem;color:var(--ink);}
.ops .pr-comments{margin-top:8px;display:flex;flex-direction:column;gap:8px;}
.ops .comment{padding:8px 0 0;border-top:1px solid var(--hairline);}
.ops .comment:first-child{border-top:none;padding-top:0;}
.ops .c-top{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:.72rem;color:var(--faint);}
.ops .c-who{color:var(--muted);}
.ops .c-src{margin-left:auto;color:var(--accent);text-decoration:none;}
.ops .c-body{font-size:.86rem;color:var(--muted);margin-top:3px;overflow-wrap:anywhere;}
.ops .c-body p{margin:4px 0;}
.ops .pr-group.unlinked{opacity:.9;}
.ops .none{color:var(--faint);font-size:.85rem;}
"""

_CHANGELOG_CSS = _TOKEN_ALIASES + r"""
.chglog .feed{display:flex;flex-direction:column;gap:12px;}
.chglog .entry{background:var(--surface);border:1px solid var(--hairline);border-radius:14px;box-shadow:var(--shadow);overflow:hidden;padding:13px 16px 15px;}
.chglog .entry-top{display:flex;align-items:center;gap:9px;flex-wrap:wrap;}
.chglog .prod{font-family:var(--mono);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);background:var(--accent-soft);padding:2px 7px;border-radius:6px;}
.chglog .src{font-family:var(--mono);font-size:.7rem;color:var(--faint);}
.chglog .date{font-family:var(--mono);font-size:.72rem;color:var(--faint);}
.chglog .link{margin-left:auto;font-family:var(--mono);font-size:.72rem;white-space:nowrap;}
.chglog .link a{color:var(--brand);text-decoration:underline;}
.chglog .title{color:var(--ink);font-weight:650;font-size:.95rem;margin:9px 0 0;}
.chglog .summary{color:var(--muted);font-size:.88rem;margin:6px 0 0;overflow-wrap:anywhere;}
.chglog .summary>*:first-child{margin-top:0;}
.chglog .summary>*:last-child{margin-bottom:0;}
.chglog .summary p{margin:6px 0;}
"""

_PR_JS = r"""
(function(){
  var chips=document.getElementById('chips'),groups=document.getElementById('groups'),
      empty=document.getElementById('empty'),active='all';
  function apply(){var vis=0,total=0;
    groups.querySelectorAll('.pr-group').forEach(function(c){total++;
      var m=active==='all'||c.dataset.prod===active;
      c.style.display=m?'':'none';if(m)vis++;});
    empty.style.display=(vis===0&&total>0)?'block':'none';}
  chips.addEventListener('click',function(e){var chip=e.target.closest('.chip');if(!chip)return;
    chips.querySelectorAll('.chip').forEach(function(c){c.setAttribute('aria-pressed','false');});
    chip.setAttribute('aria-pressed','true');active=chip.dataset.prod;apply();});
})();
"""

_CHANGELOG_JS = r"""
(function(){
  var chips=document.getElementById('chips'),feed=document.getElementById('feed'),
      empty=document.getElementById('empty'),active='all';
  function apply(){var vis=0,total=0;
    feed.querySelectorAll('.entry').forEach(function(c){total++;
      var m=active==='all'||c.dataset.prod===active;
      c.style.display=m?'':'none';if(m)vis++;});
    empty.style.display=(vis===0&&total>0)?'block':'none';}
  chips.addEventListener('click',function(e){var chip=e.target.closest('.chip');if(!chip)return;
    chips.querySelectorAll('.chip').forEach(function(c){c.setAttribute('aria-pressed','false');});
    chip.setAttribute('aria-pressed','true');active=chip.dataset.prod;apply();});
})();
"""


def _fallback_page(title: str, current: str) -> str:
    """A deliberately-empty shell — served only when a live render throws
    (server.py's full-only route table falls back to the last static build)."""
    body = ('<div class="ops"><div class="wrap">'
            '<div class="allclear">Live view unavailable — reload in a moment.</div>'
            '</div></div>')
    return ds.page_shell(title=title, nav_html=ds.nav_component(current, "full"),
                         body=body, extra_css=_TOKEN_ALIASES)


def build_static() -> None:
    """Seed empty-state fallbacks for both pages (served only if a live render
    throws).

    **Always the empty shell, never `prs_live_html()`/`changelogs_live_html()`
    of some scope** (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): this file is
    served to whichever viewer's live render failed, scoped or not, so a
    snapshot naming every project's PRs or changelog entries would leak to a
    scoped viewer — the same reasoning `chart_house_build.build_static` and
    `issue_build.main` already apply to their own fallbacks."""
    SITE.mkdir(parents=True, exist_ok=True)
    for name, title in (
        ("signals-prs.html", "PRs"),
        ("signals-changelogs.html", "Changelogs"),
    ):
        (SITE / name).write_text(_fallback_page(title, "/archives"), encoding="utf-8")
        print(f"Wrote site/{name}")


if __name__ == "__main__":
    build_static()
