#!/usr/bin/env python3.11
"""Release notes — /products/release-notes (ARCH-360 follow-up, item 5b).

The first content of the Products area: curated, user-facing release notes, one
per shipped version per product. Unlike the changelog (every merged PR, derived),
release notes are AUTHORED — a human writes a polished note through the "Write a
release note" form, so this page is both the reader and the authoring surface.

A release note is a memory signal (memory-first, ARCH-359): kind=release_note,
source=release-notes, product=<product>, stream=<version>, body=<Markdown>. The
form POSTs to /api/signals as the viewer's own CF Access identity; the API's authz
gate is the wall (the __CAN_CAPTURE__ substitution only hides the form from a role
that would be 403'd). Rendered LIVE per request (ARCH-182), newest first.

Full-tier only (server.py's _full_only_routes) — this is the internal authoring +
preview surface; the notes are published to end users elsewhere. Nav placement
(the Products area) is provisional pending the Fable IA audit.
"""
from __future__ import annotations

from html import escape

from markdown_render import md_to_html

import design_system as ds
import library_client
import orient_time
from paths import STATE_ROOT

SITE = STATE_ROOT / "site"

_LIMIT = 200
_KIND = "release_note"  # the JS form posts source=release-notes, kind=release_note

# A release note body is authored Markdown — rendered through the shared safe
# CommonMark engine (markdown_render).
_md_to_html = md_to_html


def _fmt_date(sig: library_client.SignalRecord) -> str:
    # assume_utc because the control DB stores naive UTC (models.utcnow strips
    # tzinfo — SQLite has no native timezone). Without it a naive value reads as
    # MST already and shifts seven hours the other way.
    local = orient_time.to_mst(sig.produced_at or sig.ingested_at, assume_utc=True)
    return local.strftime("%b %-d, %Y") if local else "—"


def _card(sig: library_client.SignalRecord) -> str:
    """One release note. The version (signal stream) heads the card; the body is
    authored Markdown rendered to safe HTML."""
    version = escape(sig.stream) if sig.stream else "release"
    body_html = f'<div class="body">{_md_to_html(sig.body)}</div>' if (sig.body or "").strip() else ""
    return f'''
    <article class="note" data-prod="{escape(sig.product, quote=True)}">
      <div class="note-top">
        <span class="prod">{escape(sig.product)}</span>
        <span class="ver">{version}</span>
        <span class="date">{escape(_fmt_date(sig))}</span>
      </div>
      {body_html}
      <div class="by">{escape(sig.actor)}</div>
    </article>'''


def _chips(products: list[tuple[str, int]], total: int) -> str:
    out = [f'<button class="chip" data-prod="all" aria-pressed="true">All <span class="n">{total}</span></button>']
    for prod, n in products:
        out.append(f'<button class="chip" data-prod="{escape(prod, quote=True)}" '
                   f'aria-pressed="false">{escape(prod)} <span class="n">{n}</span></button>')
    return "".join(out)


def _write_form(known_products: list[str]) -> str:
    """The "Write a release note" panel (the /archives capture-form pattern). The
    __CAN_CAPTURE__ substitution hides it from a role lacking signal_write —
    cosmetic; the POST /api/signals authz gate is the wall. __WHOAMI__ prefills
    the author."""
    prods = "".join(f'<option value="{escape(p, quote=True)}">' for p in known_products)
    return f'''
  <div class="capture" data-can-capture="__CAN_CAPTURE__">
    <button class="capture-toggle" id="rnToggle" type="button" aria-expanded="false">＋ Write a release note</button>
    <form class="capture-form card" id="rnForm" hidden>
      <div class="cf-row">
        <label>Product
          <input name="product" list="rnProds" required maxlength="64" placeholder="constance">
          <datalist id="rnProds">{prods}</datalist>
        </label>
        <label>Version
          <input name="version" required maxlength="200" placeholder="v1.4.0">
        </label>
      </div>
      <label class="cf-body">Notes <span class="opt">Markdown</span>
        <textarea name="body" required rows="8" placeholder="## What's new&#10;- A crisp, user-facing line per change&#10;- Curated — not every merge"></textarea>
      </label>
      <div class="cf-actions">
        <button type="submit" class="cf-submit">Publish note</button>
      </div>
      <div class="cf-note" id="rnNote" hidden></div>
    </form>
  </div>'''


def _page(cards_html: str, chips_html: str, count: int,
          known_products: list[str] | None = None) -> str:
    block = cards_html or '<div class="allclear">No release notes yet. Write the first one above.</div>'
    body = f'''<div class="rn">
  <header class="rn-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / products / release notes</div>
    <h1>Release notes</h1>
    <p class="lede">Curated, user-facing notes — one per shipped version, authored not auto-derived.</p>
    <div class="chips" id="chips">{chips_html}</div>
  </header>
  <div class="wrap">
    {_write_form(known_products or [])}
    <div class="feed" id="feed">{block}
      <div class="empty" id="empty">Nothing for this product yet.</div>
    </div>
    <div class="footnote">Each note is a memory signal (<code>kind=release_note</code>, ARCH-359/360),
    authored through the form above. Signed in as <strong>__WHOAMI__</strong>.</div>
  </div>
<script>{_JS}</script>
</div>'''
    return ds.page_shell(title="Release notes",
                         nav_html=ds.nav_component("/products/release-notes", "full"),
                         body=body, extra_css=_CSS)


def live_html() -> str:
    """/products/release-notes — authored release notes, newest first.

    The product datalist (`known`) comes from `signal_facets` over the WHOLE
    store, unfiltered — the same whole-store distinct-product scan the browse
    page needs, served in one request rather than a page-through
    (issue_01KZW183XVJCKFGQT8HMXFJFTY)."""
    rows = library_client.list_signals(kind=[_KIND], order="produced_at", cap=_LIMIT)
    counts: dict[str, int] = {}
    for sig in rows:
        counts[sig.product] = counts.get(sig.product, 0) + 1
    products = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    known = library_client.signal_facets().products
    cards = "".join(_card(sig) for sig in rows)
    return _page(cards, _chips(products, len(rows)), len(rows), known)


def build_static() -> None:
    """Seed the empty-state fallback (served only if a live render throws)."""
    SITE.mkdir(parents=True, exist_ok=True)
    try:
        html = live_html()
    except Exception:
        html = _page("", "", 0)
    (SITE / "products-release-notes.html").write_text(html, encoding="utf-8")
    print("Wrote site/products-release-notes.html")


_CSS = r"""
[hidden]{display:none!important;}
.rn{--muted:var(--ink-2);--faint:var(--ink-3);--accent:var(--brand-ink);--accent-soft:var(--brand-wash);line-height:1.5;}
.rn .wrap{max-width:760px;margin:0 auto;padding:0 2px 72px;}
.rn .rn-head{max-width:760px;margin:0 auto;padding:22px 2px 0;}
.rn .eyebrow-line{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);display:flex;align-items:center;gap:8px;}
.rn .eyebrow-line .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px var(--accent-soft);}
.rn h1{font-size:1.55rem;letter-spacing:-.02em;margin:6px 0 2px;font-weight:640;}
.rn .lede{color:var(--muted);font-size:.92rem;margin:0 0 12px;}
.rn .chips{display:flex;gap:7px;overflow-x:auto;padding:0 0 14px;scrollbar-width:none;}
.rn .chips::-webkit-scrollbar{display:none;}
.rn .chip{flex:0 0 auto;font-family:var(--mono);font-size:.74rem;padding:6px 11px;border-radius:999px;
border:1px solid var(--line);background:var(--surface);color:var(--muted);cursor:pointer;white-space:nowrap;}
.rn .chip[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink);}
.rn .chip .n{opacity:.6;margin-left:5px;}
.rn .feed{display:flex;flex-direction:column;gap:12px;}
.rn .note{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:14px 16px 12px;}
.rn .note-top{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;}
.rn .prod{font-family:var(--mono);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);background:var(--accent-soft);padding:2px 7px;border-radius:6px;}
.rn .ver{font-weight:680;font-size:1rem;letter-spacing:-.01em;font-variant-numeric:tabular-nums;}
.rn .date{margin-left:auto;font-family:var(--mono);font-size:.72rem;color:var(--faint);}
.rn .body{color:var(--muted);font-size:.9rem;margin:8px 0 0;overflow-wrap:anywhere;}
.rn .body>*:first-child{margin-top:0;}
.rn .body>*:last-child{margin-bottom:0;}
.rn .body h1,.rn .body h2,.rn .body h3,.rn .body h4{color:var(--ink);font-weight:650;font-size:.92rem;margin:12px 0 5px;}
.rn .body p{margin:6px 0;}
.rn .body ul,.rn .body ol{margin:6px 0;padding-left:1.35em;}
.rn .body li{margin:2px 0;}
.rn .body code{font-family:var(--mono);font-size:.82em;background:var(--surface-2,rgba(127,127,127,.12));padding:1px 4px;border-radius:4px;}
.rn .body pre{font-family:var(--mono);font-size:.82em;background:var(--surface-2,rgba(127,127,127,.12));padding:8px 10px;border-radius:6px;overflow-x:auto;margin:6px 0;}
.rn .body pre code{background:none;padding:0;}
.rn .body a{color:var(--brand);text-decoration:underline;}
.rn .body strong{color:var(--ink);}
.rn .by{font-family:var(--mono);font-size:.7rem;color:var(--faint);margin-top:10px;}
.rn .empty{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.8rem;padding:26px 0;display:none;}
.rn .allclear{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.82rem;padding:34px 0;}
.rn .footnote{margin-top:32px;padding-top:16px;border-top:1px solid var(--line);color:var(--faint);font-size:.8rem;}
.rn .footnote code{font-family:var(--mono);color:var(--muted);font-size:.76rem;}
.rn .capture{margin:4px 0 20px;}
.rn .capture[data-can-capture="false"]{display:none;}
.rn .capture-toggle{font-family:var(--mono);font-size:.78rem;letter-spacing:.04em;color:var(--accent);background:var(--surface);border:1px dashed var(--line);border-radius:10px;padding:9px 14px;cursor:pointer;}
.rn .capture-toggle[aria-expanded="true"]{border-style:solid;border-color:var(--accent);}
.rn .capture-form{margin-top:10px;padding:16px;display:flex;flex-direction:column;gap:12px;}
.rn .capture-form label{display:flex;flex-direction:column;gap:4px;font-family:var(--mono);font-size:.7rem;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);}
.rn .capture-form label .opt{text-transform:none;letter-spacing:0;opacity:.7;}
.rn .capture-form input,.rn .capture-form textarea{font-family:var(--sans);font-size:.9rem;color:var(--ink);background:var(--surface-2);border:1px solid var(--line);border-radius:8px;padding:8px 11px;letter-spacing:normal;text-transform:none;}
.rn .capture-form input:focus-visible,.rn .capture-form textarea:focus-visible{outline:2px solid var(--accent);outline-offset:1px;}
.rn .capture-form textarea{resize:vertical;line-height:1.5;}
.rn .cf-row{display:flex;gap:10px;flex-wrap:wrap;}
.rn .cf-row label{flex:1;min-width:140px;}
.rn .cf-actions{display:flex;align-items:center;}
.rn .cf-submit{margin-left:auto;font-family:var(--sans);font-size:.88rem;font-weight:560;background:var(--accent);color:var(--surface);border:none;border-radius:8px;padding:9px 18px;cursor:pointer;}
.rn .cf-submit[disabled]{opacity:.55;cursor:default;}
.rn .cf-note{font-family:var(--mono);font-size:.76rem;padding:9px 12px;border-radius:8px;}
.rn .cf-note.ok{background:color-mix(in srgb,var(--ok) 14%,var(--surface));color:var(--ok);}
.rn .cf-note.err{background:color-mix(in srgb,var(--warn) 16%,var(--surface));color:var(--warn);}
@media (max-width:520px){.rn h1{font-size:1.35rem;}}
"""

_JS = r"""
(function(){
  var chips=document.getElementById('chips'),feed=document.getElementById('feed'),
      empty=document.getElementById('empty'),active='all';
  function apply(){var vis=0,total=0;
    feed.querySelectorAll('.note').forEach(function(c){total++;
      var m=active==='all'||c.dataset.prod===active;c.style.display=m?'':'none';if(m)vis++;});
    empty.style.display=(vis===0&&total>0)?'block':'none';}
  chips.addEventListener('click',function(e){var chip=e.target.closest('.chip');if(!chip)return;
    chips.querySelectorAll('.chip').forEach(function(c){c.setAttribute('aria-pressed','false');});
    chip.setAttribute('aria-pressed','true');active=chip.dataset.prod;apply();});
})();

// "Write a release note" → POST /api/signals as the viewer's own CF Access
// identity (kind=release_note, source=release-notes, stream=version, body=Markdown).
(function(){
  var toggle=document.getElementById('rnToggle');
  if(!toggle)return;
  var form=document.getElementById('rnForm'),note=document.getElementById('rnNote');
  function setNote(cls,text){note.className='cf-note '+cls;note.textContent=text;note.hidden=false;}
  toggle.addEventListener('click',function(){
    var open=form.hidden;form.hidden=!open;
    toggle.setAttribute('aria-expanded',open?'true':'false');});
  form.addEventListener('submit',function(e){
    e.preventDefault();
    var btn=form.querySelector('.cf-submit');btn.disabled=true;btn.textContent='Publishing…';
    // __WHOAMI__ substitutes to "<email> · <tier> view"; the author is just the
    // email, so strip the UI-chrome suffix before storing it as the signal actor.
    var who=('__WHOAMI__'.split(' · ')[0]||'__WHOAMI__').trim();
    var product=form.elements.product.value.trim(),
        version=form.elements.version.value.trim();
    var payload={source:'release-notes',kind:'release_note',actor:who,
      product:product,stream:version,body:form.elements.body.value,
      dedup_key:'release_note:'+product+':'+version};
    fetch('/api/signals',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
    }).then(function(r){
      return r.json().catch(function(){return {};}).then(function(j){
        if(!r.ok)throw new Error((j&&j.error)||('HTTP '+r.status));
        return j;});
    }).then(function(){
      setNote('ok','Published — reload to see it.');
      form.elements.version.value='';form.elements.body.value='';
    }).catch(function(err){setNote('err','Failed: '+err.message);
    }).finally(function(){btn.disabled=false;btn.textContent='Publish note';});});
})();
"""


if __name__ == "__main__":
    build_static()
