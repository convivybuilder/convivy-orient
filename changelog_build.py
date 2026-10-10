#!/usr/bin/env python3.11
"""The in-box changelog (ARCH-359, ARCH-411) — /engineering/changelog.

A view over our own memory: a changelog entry is a `changelog`-kind signal in
the Library, read through library_client, and this page renders the most
recent ones. The changelog reads memory, never GitHub.

TWO KINDS OF ROW SHARE THAT KIND, AND ONLY ONE OF THEM IS AN ENTRY.
`github_capture.signal_from_merge` writes one per merge whose body is the PR
title plus the PR description — raw material, and the thing this page used to
show. `post-changelog` writes the authored entry (source=co) that a co or the
explainer composes on merge. This page renders only the authored ones; see
CHANGELOG_AUTHORED_SOURCES for why the git-derived rows stay in the store rather
than being retired.

Same internal-only, full-tier-only posture as /signals and /engineering/reports: a
contractor gets a plain 404 (server.py's full-only route table), and the query
never selects a customer-class row. Rendered live per request (ARCH-182) — never
staler than the last capture poll.
"""
from __future__ import annotations

import datetime as dt
from html import escape

from markdown_render import md_to_html

import design_system as ds
import library_client
from url_safety import safe_http_url
import orient_time
from paths import STATE_ROOT

SITE = STATE_ROOT / "site"

_LIMIT = 120
_SUMMARY_CLIP = 1400

# Which sources count as a CHANGELOG ENTRY, as opposed to the raw merge record.
#
# `github_capture.signal_from_merge` writes a `changelog` signal per merge whose
# body is the PR title plus the PR description. A PR body is written for a
# reviewer — it opens with `## Summary`, carries code blocks, and explains
# implementation — so read back as a changelog it is unusable. That was the whole
# finding behind post-changelog (2026-07-23): the co-authored entry is the one
# worth keeping. The tool shipped and nothing ever called it, so every entry on
# this page stayed git-derived (measured 2026-07-26: 3045 changelog signals,
# every one source=github, none source=co).
#
# The github rows STAY in the store and keep their `changelog` kind, because they
# are how a merge is detected at all — changelog-drain reads them to find merges
# that still need an entry written. They are the raw material; they are not the
# changelog. Filtering here, rather than retiring the capture, is what lets one
# feed serve both purposes without a migration (ARCH-411).
CHANGELOG_AUTHORED_SOURCES = frozenset({"co"})

# A PR description is GitHub-Flavored Markdown, not Slack mrkdwn — render it with
# the shared CommonMark engine (markdown_render), which is where the html=False
# and unsafe-scheme rules live for every builder that shows authored text.
_md_to_html = md_to_html


def _merged_date(when: dt.datetime | None) -> str:
    # assume_utc because the control DB stores naive UTC (models.utcnow strips
    # tzinfo — SQLite has no native timezone). Without it a naive value reads as
    # MST already and shifts seven hours the other way.
    local = orient_time.to_mst(when, assume_utc=True)
    return local.strftime("%b %-d, %Y") if local else "—"


def _pr_link(sig: library_client.SignalRecord) -> str:
    refs = sig.refs or {}
    url = refs.get("url") or ""
    pr = refs.get("pr")
    label = f"PR #{pr}" if pr is not None else "PR"
    safe = safe_http_url(url)
    if not safe:
        return escape(label)
    return (f'<a href="{escape(safe, quote=True)}" target="_blank" rel="noopener">'
            f'{escape(label)}</a>')


def _card(sig: library_client.SignalRecord) -> str:
    """One changelog entry.

    The whole body renders as Markdown rather than being split into an escaped
    title plus a summary. That split existed because the body WAS a PR — title on
    the first line, description after the blank line — and an authored entry has
    no such shape: it is a couple of sentences saying what shipped and why it
    matters. Imposing the old split on it would bold whichever clause happened to
    come first and, worse, escape a one-paragraph entry into literal `**` markers
    (ARCH-411).

    md_to_html carries the safety, as everywhere else: raw HTML is escaped and
    unsafe link schemes are dropped, so an agent-authored entry stays untrusted
    input.
    """
    body = (sig.body or "").strip()
    if len(body) > _SUMMARY_CLIP:
        body = body[:_SUMMARY_CLIP].rstrip() + " …"
    entry_html = (f'<div class="summary">{_md_to_html(body)}</div>' if body
                  else f'<p class="title">{escape(sig.stream or sig.product)}</p>')
    return f'''
    <article class="entry" data-stream="{escape(sig.stream or "", quote=True)}">
      <div class="entry-top">
        <span class="prod">{escape(sig.product)}</span>
        <span class="date">{escape(_merged_date(sig.produced_at))}</span>
        <span class="link">{_pr_link(sig)}</span>
      </div>
      {entry_html}
    </article>'''


def _chips(streams: list[tuple[str, int]], total: int) -> str:
    out = [f'<button class="chip" data-stream="all" aria-pressed="true">All '
           f'<span class="n">{total}</span></button>']
    for stream, n in streams:
        out.append(f'<button class="chip" data-stream="{escape(stream, quote=True)}" '
                   f'aria-pressed="false">{escape(stream)} '
                   f'<span class="n">{n}</span></button>')
    return "".join(out)


def _page(*, cards_html: str, chips_html: str, empty_msg: str) -> str:
    block = cards_html or f'<div class="allclear">{escape(empty_msg)}</div>'
    body = f'''<div class="chglog">
  <header class="chglog-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / engineering / changelog</div>
    <h1>Changelog</h1>
    <p class="lede">The {_LIMIT} most recent entries across the org, newest first. Each is written on merge and kept as a memory object in our store.</p>
    <div class="chips" id="chips">{chips_html}</div>
  </header>
  <div class="wrap">
    <div class="feed" id="feed">{block}
      <div class="empty" id="empty">Nothing merged in this product yet.</div>
    </div>
    <div class="footnote">Each entry is written when the PR merges and kept as a memory
    signal in our store (ARCH-359, ARCH-411). Signed in as <strong>__WHOAMI__</strong>.</div>
  </div>
</div>
<script>{_JS}</script>'''
    return ds.page_shell(title="Changelog",
                         nav_html=ds.nav_component("/engineering/changelog", "full"),
                         body=body, extra_css=_CSS)


def changelog_live_html() -> str:
    """/engineering/changelog — the most recent merges, read from memory signals.

    `source IN ("co")` is a Library filter now, not a Python one: unfiltered,
    `kind == "changelog"` alone is 4,590 rows on the live database, 23 pages to
    find the ~handful of authored entries within it
    (issue_01KZW183XVJCKFGQT8HMXFJFTY)."""
    rows = library_client.list_signals(
        source=sorted(CHANGELOG_AUTHORED_SOURCES), kind=["changelog"],
        order="produced_at", cap=_LIMIT)
    counts: dict[str, int] = {}
    for sig in rows:
        counts[sig.stream or sig.product] = counts.get(sig.stream or sig.product, 0) + 1
    streams = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    cards = "".join(_card(sig) for sig in rows)
    chips = _chips(streams, len(rows))
    return _page(cards_html=cards, chips_html=chips,
                 empty_msg="No entries written yet. changelog-drain fills this in "
                           "as merges land.")


def build_static() -> None:
    """Seed the empty-state fallback (served only if a live render throws)."""
    SITE.mkdir(parents=True, exist_ok=True)
    try:
        html = changelog_live_html()
    except Exception:
        html = _page(cards_html="", chips_html="",
                     empty_msg="Live view unavailable — reload in a moment.")
    (SITE / "engineering-changelog.html").write_text(html, encoding="utf-8")
    print("Wrote site/engineering-changelog.html")


_CSS = r"""
[hidden]{display:none!important;}
.chglog{
  --paper:var(--bg);--hairline:var(--line);--hairline-strong:var(--line);
  --muted:var(--ink-2);--faint:var(--ink-3);
  --accent:var(--brand-ink);--accent-soft:var(--brand-wash);
}
.chglog{line-height:1.5;}
.chglog .wrap{max-width:760px;margin:0 auto;padding:0 2px 72px;}
.chglog .chglog-head{max-width:760px;margin:0 auto;padding:22px 2px 0;}
.chglog .eyebrow-line{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);display:flex;align-items:center;gap:8px;}
.chglog .eyebrow-line .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px var(--accent-soft);}
.chglog h1{font-size:1.55rem;letter-spacing:-.02em;margin:6px 0 2px;text-wrap:balance;font-weight:640;}
.chglog .lede{color:var(--muted);font-size:.92rem;margin:0 0 12px;}
.chglog .chips{display:flex;gap:7px;overflow-x:auto;padding:0 0 14px;scrollbar-width:none;}
.chglog .chips::-webkit-scrollbar{display:none;}
.chglog .chip{flex:0 0 auto;font-family:var(--mono);font-size:.74rem;padding:6px 11px;border-radius:999px;
border:1px solid var(--hairline-strong);background:var(--surface);color:var(--muted);cursor:pointer;white-space:nowrap;}
.chglog .chip[aria-pressed="true"]{background:var(--ink);color:var(--paper);border-color:var(--ink);}
.chglog .chip .n{opacity:.6;margin-left:5px;}
.chglog .feed{display:flex;flex-direction:column;gap:12px;}
.chglog .entry{background:var(--surface);border:1px solid var(--hairline);border-radius:14px;box-shadow:var(--shadow);overflow:hidden;padding:13px 16px 15px;}
.chglog .entry-top{display:flex;align-items:center;gap:9px;flex-wrap:wrap;}
.chglog .prod{font-family:var(--mono);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);background:var(--accent-soft);padding:2px 7px;border-radius:6px;}
.chglog .date{font-family:var(--mono);font-size:.72rem;color:var(--faint);}
.chglog .link{margin-left:auto;font-family:var(--mono);font-size:.72rem;white-space:nowrap;}
.chglog .link a{color:var(--brand);text-decoration:underline;}
.chglog .title{color:var(--ink);font-weight:650;font-size:.95rem;margin:9px 0 0;}
.chglog .summary{color:var(--muted);font-size:.88rem;margin:6px 0 0;overflow-wrap:anywhere;}
.chglog .summary>*:first-child{margin-top:0;}
.chglog .summary>*:last-child{margin-bottom:0;}
.chglog .summary p{margin:6px 0;}
.chglog .summary h1,.chglog .summary h2,.chglog .summary h3,.chglog .summary h4{
color:var(--ink);font-weight:650;font-size:.9rem;letter-spacing:-.01em;margin:12px 0 5px;}
.chglog .summary ul,.chglog .summary ol{margin:6px 0;padding-left:1.35em;}
.chglog .summary li{margin:2px 0;}
.chglog .summary li>p{margin:0;}
.chglog .summary blockquote{margin:6px 0;padding-left:11px;border-left:2px solid var(--hairline-strong);color:var(--faint);}
.chglog .summary hr{border:none;border-top:1px solid var(--hairline);margin:10px 0;}
.chglog .summary strong{color:var(--ink);font-weight:650;}
.chglog .summary em{font-style:italic;}
.chglog .summary code{font-family:var(--mono);font-size:.82em;background:var(--surface-2,rgba(127,127,127,.12));padding:1px 4px;border-radius:4px;}
.chglog .summary pre{font-family:var(--mono);font-size:.82em;background:var(--surface-2,rgba(127,127,127,.12));padding:8px 10px;border-radius:6px;overflow-x:auto;margin:6px 0;}
.chglog .summary pre code{background:none;padding:0;}
.chglog .summary a{color:var(--brand);text-decoration:underline;}
.chglog .empty{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.8rem;padding:30px 0;display:none;}
.chglog .allclear{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.82rem;padding:34px 0;}
.chglog .footnote{margin-top:34px;padding-top:16px;border-top:1px solid var(--hairline);color:var(--faint);font-size:.8rem;}
@media (max-width:520px){.chglog h1{font-size:1.35rem;}}
"""

_JS = r"""
(function(){
  var chips=document.getElementById('chips'),feed=document.getElementById('feed'),
      empty=document.getElementById('empty'),active='all';
  function apply(){var vis=0,total=0;
    feed.querySelectorAll('.entry').forEach(function(c){total++;
      var m=active==='all'||c.dataset.stream===active;
      c.style.display=m?'':'none';if(m)vis++;});
    empty.style.display=(vis===0&&total>0)?'block':'none';}
  chips.addEventListener('click',function(e){var chip=e.target.closest('.chip');if(!chip)return;
    chips.querySelectorAll('.chip').forEach(function(c){c.setAttribute('aria-pressed','false');});
    chip.setAttribute('aria-pressed','true');active=chip.dataset.stream;apply();});
})();
"""


if __name__ == "__main__":
    build_static()
