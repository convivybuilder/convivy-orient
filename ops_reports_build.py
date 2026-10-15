#!/usr/bin/env python3.11
"""/reference/briefings — see its own docstring below.

The Reports/Notes pages this module used to render are retired:

  * /engineering/reports — retired into Notes (the operator 2026-07-19).
  * /engineering/notes   — decision explanations, off the *-explanations
                           streams, merged with the overnight wrap-ups. Split
                           out of Reports by ARCH-362/R4; the page itself is
                           retired too (the operator, 2026-08-26).

The categorisation machinery that used to serve them (CHANNEL_CATEGORY,
_category, _render, _merged_recent, _slack_mrkdwn_to_html and the rest) had no
live caller once /reference/briefings — the last page reading it — moved onto the
KB index (see briefings_live_html's own docstring), and was deleted along with
its unit tests (2026-08-26): both callers this module ever had (Notes, then
Briefings) had been independently retired, so nothing remained to exercise it
beyond its own tests. Alerts left earlier the same way (raw alert-tagged
signals, ARCH-362 Phase 2-4a), redefined and moved to /alerts under Dashboards
(epic issue_01M0EHRNW5HVJCAXZHYQCBZ3YW) — see alerts_build.py for what replaced
it.

`_page`/`_CSS`/`_JS` below survive as `build_static`'s empty-state fallback
shell, not as part of any live rendering path.
"""
from __future__ import annotations

import json
import re
from html import escape

import build
import design_system as ds
import library_client
from paths import STATE_ROOT

SITE = STATE_ROOT / "site"
# Named so who this page serves is greppable, matching project_build._AUDIENCE,
# home_build._AUDIENCE and build._AUDIENCE.
_AUDIENCE = "team"


def _page(*, title: str, area_nav: str, eyebrow: str, heading: str, lede: str,
          cards_html: str, chips_html: str, empty_msg: str, footnote: str) -> str:
    allclear = (f'<div class="allclear" id="allclear"'
                f'{"" if not cards_html else " hidden"}>{escape(empty_msg)}</div>')
    body = f'''<div class="ops">
  <header class="ops-head">
    <div class="eyebrow-line"><span class="dot"></span>{escape(eyebrow)}</div>
    <h1>{escape(heading)}</h1>
    <p class="lede">{escape(lede)}</p>
    <div class="chips" id="chips">{chips_html}</div>
  </header>
  <div class="wrap">
    <div class="queue" id="feed">{cards_html}
      <div class="empty" id="empty">Nothing in this stream.</div>
    </div>
    {allclear}
    <div class="footnote">{footnote} Signed in as <strong>__WHOAMI__</strong>.</div>
  </div>
</div>
<script>{_JS}</script>'''
    return ds.page_shell(title=title, nav_html=ds.nav_component(area_nav, "full"),
                         body=body, extra_css=_CSS)


# /reference/briefings' second half: the four KB-index doc kinds
# engineering_build.VISIBLE_KINDS drops from the canonical browse as
# "operational noise for a human" (the operator 2026-08-26). Under the Archives-vs-
# Documents split, that noise is exactly what Archives holds — a record of
# something that happened, not a claim anyone maintains as current — so they
# surface here instead of nowhere. `legal` and `marketing` are dropped by the
# same VISIBLE_KINDS exclusion but were considered and rejected for this page
# (the operator: they read as canonical, not memory) — deliberately absent below.
_MEMORY_KIND_LABEL: dict[str, str] = {
    "meeting-notes": "Meeting notes",
    "proposal": "Proposal",
    "observation": "Observation",
    "feature-request": "Feature request",
}


def _entitled_metas() -> list:
    """The entitled document metadata for one render, every status.

    **A render does this ONCE and both memory groups filter the result.** This
    page is served live per request (`server.py`'s `_full_only_routes`), not
    built in a batch, which is the distinction the epic behind this migration
    sequenced its children around: `build.load_docs` was cleared because "a
    batch job absorbs the round trips a request path could not," and `/memory`'s
    own listing was redesigned rather than ported as-is for the same reason. The
    first version of this module made the caller's two groups pay for a separate
    corpus pass each; measured against the live Library, one pass is 0.027s and
    two were 0.054s of the render.
    """
    return library_client.list_reference_documents(audience=_AUDIENCE, status="all")


def _load_kb_docs(kinds, metas=None) -> list[dict]:
    """Every team-audience KB doc of the given kind(s), any status, read through
    the Library rather than a local index file (issue_01M1MWRXAZ5NRH10NP6Z9MBH0C).

    `metas` is the shared listing from `_entitled_metas()`; a caller loading
    more than one group passes it so the corpus is listed once per render.
    Omitted, this fetches its own — which is what a single-group caller and the
    tests want.

    **`status="all"` is the load-bearing argument here.** No status filter,
    unlike engineering_build.load_docs()'s current/draft-only canonical browse:
    a memory is a record of what happened, not a claim someone still maintains,
    so an archived proposal is as much a memory as a current one (the operator
    2026-08-26). The listing route's DEFAULT is current+draft — 760 of 778 when
    measured — so a port that simply dropped the old SQL's absent WHERE clause
    would silently lose exactly the documents this page exists to show, and
    would pass any test seeded with current documents. That failure is why the
    sentinel was added (convivy/library#103); this is the caller it was added
    for.

    Gated on the same `team` audience marker as build.load_docs() — a doc not
    opted into `team` was written for an agent role or a specific person, not
    the whole company, and this page is a full-tier human surface like every
    other KB reader here (substrate ARCH-089). The gate lives in
    `list_reference_documents`, so it is not reimplemented here.

    `kind` is filtered client-side rather than asked of the route. The route
    does take a `kind`, but one value only — a list silently yields the last —
    and the kinds are Orient's editorial choice of what counts as a memory
    rather than a Library vocabulary, so a kind appearing upstream should NOT
    join this page until someone decides it does. The listing omits bodies, so
    reading the corpus and filtering locally is four requests.
    """
    wanted = set(kinds)
    docs = []
    for meta in (_entitled_metas() if metas is None else metas):
        if meta.kind not in wanted:
            continue
        full = library_client.get_reference_document(meta.id, audience=_AUDIENCE)
        if full is None or full.body is None:
            # Entitlement or existence changed between the list and the fetch.
            # Drop the row rather than raise, as build.load_docs does.
            continue
        docs.append({
            "id": full.id, "project": full.project, "kind": full.kind,
            "title": full.title, "date": full.last_reviewed or "",
            "body": build._clean_body(full.body),
        })
    # Newest first; ties (or a missing date) broken by title for stability.
    docs.sort(key=lambda d: (d["date"], d["title"]), reverse=True)
    return docs


def _memory_entry(doc: dict, kind_label: str = "") -> dict:
    anchor = re.sub(r"[^a-z0-9]+", "-", doc["id"].lower()).strip("-")
    return {"anchor": anchor, "title": doc["title"], "project": doc["project"],
            "date": doc["date"], "kindLabel": kind_label, "body": doc["body"]}


_MEMORY_JS = r"""
const DATA = JSON.parse(document.getElementById('data').textContent);
marked.setOptions({ gfm:true, breaks:false });
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

let activeProject = '__all';
const pillsEl = document.getElementById('memPills');
const briefsEl = document.getElementById('briefsList');
const otherEl = document.getElementById('otherList');

function cardHtml(e, kindTag){
  const dateTag = e.date ? `<span class="tag date">${esc(e.date)}</span>` : '';
  return `<article class="mem-card" id="${esc(e.anchor)}" data-project="${esc(e.project)}">
    <div class="mem-top">
      <span class="mem-title">${esc(e.title)}</span>
      <span class="tags">${dateTag}<span class="tag proj">${esc(e.project)}</span>${kindTag}</span>
    </div>
    <div class="content">${DOMPurify.sanitize(marked.parse(e.body||''))}</div>
  </article>`;
}

function renderPills(){
  let h = `<button type="button" class="mem-pill" data-p="__all" aria-pressed="${activeProject==='__all'}">All projects</button>`;
  for (const p of DATA.projects) h += `<button type="button" class="mem-pill" data-p="${esc(p)}" aria-pressed="${activeProject===p}">${esc(p)}</button>`;
  pillsEl.innerHTML = h;
  pillsEl.querySelectorAll('.mem-pill').forEach(el => {
    el.onclick = () => { activeProject = el.dataset.p; render(); };
  });
}

function renderList(el, entries, emptyHtml, kindFromEntry){
  const shown = activeProject === '__all' ? entries : entries.filter(e => e.project === activeProject);
  el.innerHTML = shown.length
    ? shown.map(e => cardHtml(e, kindFromEntry(e))).join('')
    : emptyHtml;
}

function render(){
  renderPills();
  renderList(briefsEl, DATA.briefs, DATA.briefsEmptyHtml, () => '');
  renderList(otherEl, DATA.other, DATA.otherEmptyHtml,
    e => e.kindLabel ? `<span class="tag kind">${esc(e.kindLabel)}</span>` : '');
}
render();
"""

_MEMORY_CSS = r"""
.mem-wrap{max-width:820px;margin:0 auto;padding:22px 12px 80px;}
.mem-head .eyebrow-line{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-3);display:flex;align-items:center;gap:8px;}
.mem-head .eyebrow-line .dot{width:6px;height:6px;border-radius:50%;background:var(--brand-ink);box-shadow:0 0 0 4px var(--brand-wash);}
.mem-head h1{font-size:1.55rem;letter-spacing:-.02em;margin:6px 0 8px;font-weight:640;}
.mem-head .lede{color:var(--ink-2);font-size:.92rem;max-width:680px;margin:0 0 14px;}
.mem-pills{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:22px;}
.mem-pill{font-family:var(--mono);font-size:.74rem;padding:6px 11px;border-radius:999px;border:1px solid var(--line);background:var(--surface);color:var(--ink-2);cursor:pointer;}
.mem-pill[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink);}
.mem-area{font-size:1.05rem;font-weight:700;margin:26px 0 4px;color:var(--ink);border-bottom:1px solid var(--line);padding-bottom:6px;}
.mem-wrap .note{color:var(--ink-3);font-size:13px;margin:4px 0 14px;}
.mem-card{border:1px solid var(--line);border-radius:var(--radius);background:var(--surface);box-shadow:var(--shadow);padding:16px 18px;margin:12px 0;scroll-margin-top:16px;}
.mem-top{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:8px;}
.mem-title{font-weight:700;color:var(--ink);}
.mem-card .tags{display:flex;gap:6px;flex-wrap:wrap;}
.mem-card .tag{font-size:11px;font-weight:600;padding:2px 9px;border-radius:999px;background:var(--surface-2);color:var(--ink-2);}
.mem-card .tag.date{background:var(--brand-wash);color:var(--brand-ink);font-family:var(--mono);}
.mem-card .content p{margin:10px 0;}
.mem-card .content pre{background:var(--surface-2);border:1px solid var(--line);padding:12px;border-radius:var(--radius);overflow:auto;font-size:13px;font-family:var(--mono);}
.mem-card .content code{background:var(--brand-wash);color:var(--brand-ink);padding:1px 5px;border-radius:4px;font-size:12.5px;font-family:var(--mono);}
.mem-card .content pre code{background:none;padding:0;color:inherit;}
"""


def briefings_live_html(scope: frozenset[str] = frozenset(), whole_fleet: bool = True) -> str:
    """/reference/briefings — the daily AI briefings, plus every KB memory kind
    engineering_build.VISIBLE_KINDS drops from the canonical browse.

    Restricted to `scope` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) the same
    way `engineering_build.live_html`'s own doc list is: a brief or memory
    whose project is outside `scope` is dropped before the `projects` pill
    list or either payload is built from it.

    Split out of Reports by ARCH-362/R2. These sit under **Memory**, not
    Engineering: they are outside-world industry reading rather than our
    fleet's own output, and Memory is where the site keeps context the
    company draws on.

    Reads the KB index directly (like engineering_build.load_docs()), NOT the
    signal store: the producer (ai-briefing, launchd 03:00/04:00 daily) moved
    from posting signals to publishing KB documents (kind=brief) via the write
    cage on 2026-08-26, and this page had kept reading the old store, so its
    newest item sat 39 days behind what the KB actually held.

    It stays in Archives, not Documents, despite reading the same index
    Documents reads — under the operator's 2026-08-26 model the split is memory vs.
    canonical knowledge, not which store a page happens to read: a brief is a
    dated snapshot of what was read that week, not a claim anyone maintains as
    current, and the same is true of the meeting notes, proposals,
    observations and feature requests below it on this page.
    """
    metas = _entitled_metas()  # one listing pass, filtered into both groups
    briefs = _load_kb_docs(("brief",), metas)
    other = _load_kb_docs(tuple(_MEMORY_KIND_LABEL), metas)
    if not whole_fleet:
        briefs = [d for d in briefs if d["project"] in scope]
        other = [d for d in other if d["project"] in scope]
    projects = sorted({d["project"] for d in (*briefs, *other)})

    briefs_payload = [_memory_entry(d) for d in briefs]
    other_payload = [_memory_entry(d, _MEMORY_KIND_LABEL[d["kind"]]) for d in other]

    payload = json.dumps({
        "briefs": briefs_payload, "other": other_payload, "projects": projects,
        # Rendered server-side through the shared empty-state component
        # (ds.empty_state) rather than hand-rolled in _MEMORY_JS, so a
        # per-project-empty list matches every other page's empty state instead
        # of carrying its own bespoke markup and CSS class.
        "briefsEmptyHtml": ds.empty_state("No briefings for this project."),
        "otherEmptyHtml": ds.empty_state("Nothing captured for this project."),
    }, ensure_ascii=False).replace("</", "<\\/")

    marked_src = build.MARKED_JS.read_text(encoding="utf-8")
    dompurify_src = build.DOMPURIFY_JS.read_text(encoding="utf-8")

    body = (
        f'<script>{marked_src}</script>\n'
        f'<script>{dompurify_src}</script>\n'
        f'<div class="mem-wrap">\n'
        f'  <header class="mem-head">\n'
        f'    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / archives / briefings</div>\n'
        f'    <h1>AI briefings &amp; other memories</h1>\n'
        f'    <p class="lede">Outside-world reading the company draws on — the daily AI engineering and '
        f'transformation briefings — plus the meeting notes, proposals, observations and feature requests '
        f'that never became a maintained doc. Every entry here is a record of something that happened, not '
        f'a claim anyone is keeping current; see Documents for that.</p>\n'
        f'    <div class="mem-pills" id="memPills"></div>\n'
        f'  </header>\n'
        f'  <section>\n'
        f'    <h2 class="mem-area">AI briefings</h2>\n'
        f'    <div id="briefsList"></div>\n'
        f'  </section>\n'
        f'  <section>\n'
        f'    <h2 class="mem-area">Other memories</h2>\n'
        f'    <p class="note">Meeting notes, proposals, observations and feature requests — excluded from '
        f'the canonical KB browse (engineering_build.VISIBLE_KINDS) because nobody maintains them as '
        f'current, kept here because they still happened.</p>\n'
        f'    <div id="otherList"></div>\n'
        f'  </section>\n'
        f'</div>\n'
        f'<script id="data" type="application/json">{payload}</script>\n'
        f'<script>\n{_MEMORY_JS}\n</script>'
    )
    return ds.page_shell(
        title="AI Briefings & Memories",
        nav_html=ds.nav_component("/reference/briefings", "full"),
        body=body,
        extra_css=_MEMORY_CSS,
    )


def build_static() -> None:
    """Seed empty-state fallbacks, deliberately empty rather than calling the
    live renderer (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) — this used to
    call `briefings_live_html()` with no scope argument, the same
    zero-argument leak 26bb079 found and fixed in
    `archives_build.build_static()`."""
    SITE.mkdir(parents=True, exist_ok=True)
    # engineering-reports.html and engineering-notes.html are gone with their
    # pages (the latter retired 2026-08-26, the operator): a fallback nothing serves is
    # a file that only ever misleads whoever finds it in site/.
    html = _page(title="AI Briefings & Memories", area_nav="/engineering/reports", eyebrow="", heading="",
                lede="", cards_html="", chips_html="",
                empty_msg="Live view unavailable — reload in a moment.", footnote="")
    (SITE / "memory-briefings.html").write_text(html, encoding="utf-8")
    print("Wrote site/memory-briefings.html (static fallback)")


_CSS = r"""
[hidden]{display:none!important;}
.ops{
  --paper:var(--bg);--hairline:var(--line);--hairline-strong:var(--line);
  --muted:var(--ink-2);--faint:var(--ink-3);
  --accent:var(--brand-ink);--accent-soft:var(--brand-wash);
}
.ops{line-height:1.5;}
.ops .wrap{max-width:760px;margin:0 auto;padding:0 2px 72px;}
.ops .ops-head{max-width:760px;margin:0 auto;padding:22px 2px 0;}
.ops .eyebrow-line{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);display:flex;align-items:center;gap:8px;}
.ops .eyebrow-line .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px var(--accent-soft);}
.ops h1{font-size:1.55rem;letter-spacing:-.02em;margin:6px 0 2px;text-wrap:balance;font-weight:640;}
.ops .lede{color:var(--muted);font-size:.92rem;margin:0 0 12px;}
.ops .chips{display:flex;gap:7px;overflow-x:auto;padding:0 0 14px;scrollbar-width:none;}
.ops .chips::-webkit-scrollbar{display:none;}
.ops .chip{flex:0 0 auto;font-family:var(--mono);font-size:.74rem;padding:6px 11px;border-radius:999px;
border:1px solid var(--hairline-strong);background:var(--surface);color:var(--muted);cursor:pointer;white-space:nowrap;}
.ops .chip[aria-pressed="true"]{background:var(--ink);color:var(--paper);border-color:var(--ink);}
.ops .chip .n{opacity:.6;margin-left:5px;}
.ops .queue{display:flex;flex-direction:column;gap:12px;}
.ops .card{background:var(--surface);border:1px solid var(--hairline);border-radius:14px;box-shadow:var(--shadow);overflow:hidden;padding-bottom:13px;}
.ops .card-top{display:flex;align-items:center;gap:9px;padding:13px 16px 0;flex-wrap:wrap;}
.ops .stream{font-family:var(--mono);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);background:var(--accent-soft);padding:2px 7px;border-radius:6px;}
.ops .prod{font-family:var(--mono);font-size:.72rem;color:var(--muted);}
.ops .age{margin-left:auto;font-family:var(--mono);font-size:.72rem;color:var(--faint);white-space:nowrap;}
.ops .body{color:var(--muted);font-size:.88rem;margin:8px 16px 0;white-space:pre-wrap;overflow-wrap:anywhere;}
.ops .body strong{color:var(--ink);font-weight:650;}
.ops .body em{font-style:italic;}
.ops .body code{font-family:var(--mono);font-size:.82em;background:var(--surface-2,rgba(127,127,127,.12));padding:1px 4px;border-radius:4px;}
.ops .body pre{font-family:var(--mono);font-size:.82em;background:var(--surface-2,rgba(127,127,127,.12));padding:8px 10px;border-radius:6px;overflow-x:auto;margin:6px 0;}
.ops .body pre code{background:none;padding:0;}
.ops .body a{color:var(--brand);text-decoration:underline;}
.ops .empty{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.8rem;padding:30px 0;display:none;}
.ops .allclear{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.82rem;padding:34px 0;}
.ops .footnote{margin-top:34px;padding-top:16px;border-top:1px solid var(--hairline);color:var(--faint);font-size:.8rem;}
@media (max-width:520px){.ops h1{font-size:1.35rem;}}
"""

_JS = r"""
(function(){
  var chips=document.getElementById('chips'),feed=document.getElementById('feed'),
      empty=document.getElementById('empty'),active='all',
      allclear=document.getElementById('allclear');
  function filter(root,emptyEl){var vis=0,total=0;
    if(!root)return 0;
    root.querySelectorAll('.card').forEach(function(c){total++;
      var m=active==='all'||c.dataset.stream===active;
      c.style.display=m?'':'none';if(m)vis++;});
    if(emptyEl)emptyEl.style.display=(vis===0&&total>0)?'block':'none';
    return vis;}
  function apply(){
    var live=filter(feed,empty);
    if(allclear)allclear.hidden=!(live===0&&active==='all');}
  chips.addEventListener('click',function(e){var chip=e.target.closest('.chip');if(!chip)return;
    chips.querySelectorAll('.chip').forEach(function(c){c.setAttribute('aria-pressed','false');});
    chip.setAttribute('aria-pressed','true');active=chip.dataset.stream;apply();});
})();
"""


if __name__ == "__main__":
    build_static()
