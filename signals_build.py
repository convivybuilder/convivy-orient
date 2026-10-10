#!/usr/bin/env python3.11
"""The /archives browse page (S1-b, ARCH-349). Renamed from /signals
(issue_01M1A678RA8CVDNNMZC6777NNA, 2026-09-03) — the nav entry has read
"Archives" all along; /signals still resolves, via server.py's _MOVED.

The human-facing surface of the signal-memory plane: the store the GitHub
capture poller and manual capture fill, browsable newest-first. Browse is
server-rendered live from the Library per request
(full-tier only, like every full-tier page), through library_client — this
module only READS, through that client; capture goes through the authz-gated
signal_capture_api proxy.

**Search is live** (issue_01M08YKNV185DFJ6832GV8ZY22's gap closes here) **and
browse-by-shape joins it** — the Archives tail: every signal kind this page
has no dedicated section for, reachable by a kind filter or a text search
rather than only by the recent-60 window below. Both call `GET
/api/signals/browse` and `GET /api/signals/search` (`signal_search_api.py`),
which proxy `library_client.list_signals`/`search_signals` under Orient's
plain AGENT credential — the same one this page's own browse already reads
under, verified at process start to lack `signal.customer`
(`library_client.FORBIDDEN_AGENT_CAPABILITY`). That is what makes this safe
to enable where the OLD disabled placeholder was not: the earlier text here
worried about the APPROVED-CLIENT credential, which does hold
`signal.customer` (ADR-0038) and has no per-viewer wall over it. Nothing here
reaches for that credential, so there is no such wall to build.

**Browse-by-shape is exact; search is not, and the two must never look like
one control** — `signal_search_api.py`'s module docstring and
`platform/contracts/library-protocol` §3.2 give the measured reason: `GET
/signals/search` filters an oversampled retrieval window rather than the
table, so a real, common row can come back as zero matches. Browse-by-shape
calls `GET /signals` (via `list_signals`), which filters the whole table and
reports an exact `signal_facets` total; search calls `GET /signals/search`
and reports only how many of the CLOSEST matches came back, never a total.
The page renders them as two visibly distinct sections for exactly this
reason.

Visibility model: browse shows INTERNAL-class signals only, to any full-tier
viewer — the same coarse boundary every full-tier page uses. Customer-class
rows never render, because the Library's own agent-role credential Orient
holds lacks the `signal.customer` capability (ADR-0022 gives the privacy wall
to the Library) — the same structural exclusion that makes a duplicate
Orient-side filter unnecessary here.

All free text (bodies, actors, streams — much of it fetched verbatim from
GitHub) is escaped on render.

Manual capture (S2, ARCH-349): the page carries a "Log a signal" form — the
human path into the store (a meeting transcript, a customer call, a note).
It POSTs to /api/signals as the viewer's own CF Access identity, so the API's
authz gate (signal_write; signal_customer for customer-class) is the wall;
the __CAN_CAPTURE__ substitution only hides the form from roles that would be
403'd anyway — cosmetic, not the wall. The upload button reads
a plain-text file (transcript exports: .txt/.md/.vtt/.srt) client-side via
FileReader into the body field — no multipart, no new server surface.
"""
from __future__ import annotations

from html import escape

import design_system as ds
import library_client
from control_db import PRIVACY_INTERNAL
from markdown_render import md_to_html
from models import utcnow
from url_safety import safe_http_url
from paths import STATE_ROOT

SITE = STATE_ROOT / "site"

_BROWSE_LIMIT = 60
_BODY_CLIP = 600      # rendered body excerpt length (chars)
_UPLOAD_CLIP = 200_000  # client-side cap, mirrors the Library's own MAX_TEXT
                        # (the stored body cap) so an upload is clipped visibly
                        # here, not silently at the server

# Suggested kinds for the capture form's datalist — free text, these are hints.
_CAPTURE_KINDS = ("meeting", "transcript", "call", "note", "email", "idea")

# A FALLBACK for the browse-by-shape kind picker — `GET /signals/facets`
# serves no `by_kind`, so nothing today can list which kinds the store holds
# (issue_01M11WV4TD327Z01XV03ZJHYEX). Measured live against the running
# Library 2026-08-27 (re-measure rather than trusting these; they move):
# pr_comment 14,929 · changelog 5,138 · alert 111 · finding 51 ·
# test-result 32 · release_note 11 · digest 11 · deploy_failure 11.
# **The free-text kind field next to this dropdown is what makes a kind NOT
# in this tuple reachable** — swapping this constant for a live `by_kind` call
# later should need no change to how a kind actually gets browsed, only to
# where the dropdown's options come from.
_KNOWN_KINDS = ("pr_comment", "changelog", "alert", "finding", "test-result",
               "release_note", "digest", "deploy_failure")


def _age(when, now) -> str:
    """A compact human age from a naive-UTC timestamp to now."""
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


def _excerpt(body: str) -> str:
    """`body` arrives from `library_client` with any leading `agentctl-review`
    stamp already stripped (`SignalRecord.body`'s own docstring) — this only
    clips length."""
    body = (body or "").strip()
    if len(body) <= _BODY_CLIP:
        return body
    return body[:_BODY_CLIP].rstrip() + " …"


def _card(sig: library_client.SignalRecord, now) -> str:
    """One signal card. Every dynamic value is escaped — the bodies are verbatim
    GitHub content and must never reach the page unescaped."""
    badges = [f'<span class="badge prod">{escape(sig.product)}</span>',
              f'<span class="badge kind">{escape(sig.kind)}</span>']
    if sig.privacy_class != "internal":
        badges.append(f'<span class="badge priv">{escape(sig.privacy_class)}</span>')
    stream = f'<span class="stream">{escape(sig.stream)}</span>' if sig.stream else ""
    url = (sig.refs or {}).get("url") if isinstance(sig.refs, dict) else None
    safe_url = safe_http_url(url)
    link = (f'<a class="src" href="{escape(safe_url, quote=True)}" target="_blank" '
            f'rel="noopener">source ↗</a>'
            if safe_url is not None else "")
    when = sig.produced_at or sig.ingested_at
    raw_body = (sig.body or "").strip()
    body = _excerpt(raw_body)
    clipped = len(raw_body) > _BODY_CLIP
    if body:
        body_html = f'<p class="body">{escape(body)}</p>'
    elif sig.stamp_only:
        # The body was captured, but it was only agentctl-review's stamp —
        # library_client already stripped it, so sig.body is empty and this
        # reads that fact off SignalRecord.stamp_only rather than the (now
        # gone) raw text. The card is still a real signal (badges, stream,
        # permalink below), so it gets a stated reason rather than a blank
        # <p class="body">.
        body_html = '<p class="body body-empty">No notes beyond the review stamp.</p>'
    else:
        body_html = ""
    # The permalink is what makes a signal id resolvable. Until it existed, an
    # id in a document or a message was a dead end: this card shows a 600-char
    # excerpt and nothing linked anywhere, so a long body had no reachable form
    # on the site at all. `open ↗` is loud when the body IS clipped, because
    # that is exactly when the card is not the whole record.
    href = escape(f"/archives/{sig.id}", quote=True)
    more = (f'<a class="more" href="{href}">read the full signal →</a>'
            if clipped else "")
    return f'''
    <article class="card" data-prod="{escape(sig.product, quote=True)}">
      <div class="card-top">
        {"".join(badges)}
        {stream}
        <span class="age">{escape(_age(when, now))}</span>
      </div>
      {body_html}
      {more}
      <div class="meta"><span class="who">{escape(sig.actor)}</span> · {escape(sig.source)}{" · " + link if link else ""} · <a class="perma" href="{href}"><code>{escape(sig.id)}</code></a></div>
    </article>'''


def _chips(products, total: int) -> str:
    out = [f'<button class="chip" data-prod="all" aria-pressed="true">All '
           f'<span class="n">{total}</span></button>']
    for prod, n in products:
        out.append(f'<button class="chip" data-prod="{escape(prod, quote=True)}" '
                   f'aria-pressed="false">{escape(prod)} <span class="n">{n}</span></button>')
    return "".join(out)


def _capture_form(known_products: list[str]) -> str:
    """The "Log a signal" panel. Visibility gates on data-can-capture (the
    server substitutes __CAN_CAPTURE__ from the viewer's role) — cosmetic; the
    POST /api/signals authz gate is the wall. The actor field prefills with the
    server-substituted __WHOAMI__ label (html.escape(quote=True), so it is safe
    inside a double-quoted attribute). Known products feed a datalist — the
    fields stay free text."""
    prods = "".join(f'<option value="{escape(p, quote=True)}">' for p in known_products)
    kinds = "".join(f'<option value="{k}">' for k in _CAPTURE_KINDS)
    return f'''
  <div class="capture" data-can-capture="__CAN_CAPTURE__">
    <button class="capture-toggle" id="captureToggle" type="button" aria-expanded="false">＋ Log a signal</button>
    <form class="capture-form card" id="captureForm" hidden>
      <div class="cf-row">
        <label>Kind
          <input name="kind" list="kindList" required maxlength="64" placeholder="meeting">
          <datalist id="kindList">{kinds}</datalist>
        </label>
        <label>Product
          <input name="product" list="prodList" required maxlength="64" placeholder="constance">
          <datalist id="prodList">{prods}</datalist>
        </label>
        <label>Stream <span class="opt">optional</span>
          <input name="stream" maxlength="200" placeholder="value-prop">
        </label>
      </div>
      <div class="cf-row">
        <label class="grow">Who produced it
          <input name="actor" required maxlength="200" value="__WHOAMI__">
        </label>
        <fieldset class="cf-priv">
          <legend>Privacy</legend>
          <label><input type="radio" name="privacy_class" value="internal" checked> internal</label>
          <label><input type="radio" name="privacy_class" value="customer"> customer</label>
        </fieldset>
      </div>
      <label class="cf-body">Content
        <textarea name="body" required rows="7" placeholder="Paste a transcript, notes from a call, an idea — or upload a file below."></textarea>
      </label>
      <div class="cf-actions">
        <input type="file" id="captureFile" accept=".txt,.md,.markdown,.vtt,.srt,.log,text/plain" hidden>
        <button type="button" class="cf-upload" id="captureUpload">Upload from device</button>
        <span class="cf-file" id="captureFileName"></span>
        <button type="submit" class="cf-submit">Log signal</button>
      </div>
      <div class="cf-note" id="captureNote" hidden></div>
    </form>
  </div>'''


def _page(cards_html: str, chips_html: str, total: int, embedded: int,
          by_source: list, known_products: list[str] | None = None) -> str:
    """Assemble the page through the shared Atelier shell (ARCH-355). __WHOAMI__
    is substituted by the server with the viewer's identity label, as on every
    other full-tier page."""
    src_bits = " · ".join(f"{escape(src)} {n:,}" for src, n in by_source) or "empty"
    coverage = f"{embedded / total:.0%}" if total else "—"
    browse_block = cards_html or (
        '<div class="allclear">No signals captured yet. The GitHub poller will fill this in.</div>')
    kind_options = "".join(f'<option value="{escape(k, quote=True)}">{escape(k)}</option>'
                           for k in _KNOWN_KINDS)
    body = f'''<div class="ops">
  <header class="ops-head">
    <div class="eyebrow-line"><span class="dot"></span>team.convivy.com / archives</div>
    <h1>Archives</h1>
    <p class="lede">Everything captured that isn't canonical knowledge —
      <span class="count">{total:,}</span> so far · {src_bits} · {coverage} embedded</p>
    <div class="sec">Search
      <span class="tail-hint">approximate — the closest matches, not a count of what exists</span></div>
    <div class="search-panel card">
      <form class="search" id="searchForm">
        <input type="search" id="q" placeholder="Search the Archives — text, ranked by closeness"
               autocomplete="off" maxlength="2000">
        <button type="submit">Search</button>
      </form>
      <div class="search-note" id="searchNote"></div>
      <div class="queue" id="searchResults"></div>
    </div>
    <div class="chips" id="chips">{chips_html}</div>
  </header>
  <div class="wrap">
    {_capture_form(known_products or [])}

    <div class="sec">Browse by shape
      <span class="tail-hint">complete — every row of the kind you pick</span></div>
    <div class="shape card">
      <form class="shape-form" id="shapeForm">
        <select id="shapeKindSelect" aria-label="Known kind">
          <option value="">Choose a known kind…</option>
          {kind_options}
        </select>
        <span class="shape-or">or</span>
        <input type="text" id="shapeKindFree" placeholder="type any kind" maxlength="64"
               autocomplete="off" aria-label="Any kind, free text">
        <button type="submit">Browse</button>
      </form>
      <div class="shape-meta" id="shapeMeta" hidden></div>
      <div class="queue" id="shapeResults"></div>
    </div>

    <div class="sec" id="browseSec">Latest captured</div>
    <div class="queue" id="browse">{browse_block}
      <div class="empty" id="empty">Nothing in this product yet.</div>
    </div>
    <div class="footnote">
      Signed in as <strong>__WHOAMI__</strong>. Browse shows the
      {_BROWSE_LIMIT} most recent captured; browse by shape shows every row of
      one kind, exactly; search finds the closest matches to a phrase, not a
      full scan (issue_01M08YKNV185DFJ6832GV8ZY22). Capture: GitHub reviews
      poll in every 15 minutes; agents and integrations POST to
      <code>/api/signals</code>; the "Log a signal" form above captures one
      manually.
    </div>
  </div>
</div>
<script>{_JS.replace("__BODY_CLIP__", str(_BODY_CLIP)).replace("__UPLOAD_CLIP__", str(_UPLOAD_CLIP))}</script>'''
    return ds.page_shell(
        title="Archives",
        nav_html=ds.nav_component("/archives", "full"),
        body=body,
        extra_css=_CSS,
    )


def live_html() -> str:
    """Render the page from the Library (registered in server._full_only_routes;
    a render throw falls back to the static build).

    Everything here — the browse cards, the totals, the chips — is scoped to
    internal-class rows: the Library's own wall excludes customer-class content
    from a credential with no `signal.customer` capability, the same way the old
    `internal`-filtered ORM query did (see the module docstring for why customer
    rows must not render server-side). Chip counts derive from the SAME limited
    list the browse renders (the decisions_build pattern), so a chip's number
    always matches what filtering to it shows — a whole-table count against a
    60-item view reads as items the filter then can't find.

    `total`/`embedded`/`by_source`/the product datalist come from
    `signal_facets`, an aggregate over the WHOLE matching set — paging 18,455
    signals to compute these by hand would cost 93 requests
    (issue_01KZW183XVJCKFGQT8HMXFJFTY)."""
    now = utcnow()
    recent = library_client.list_signals(cap=_BROWSE_LIMIT)
    facets = library_client.signal_facets()
    counts: dict[str, int] = {}
    for sig in recent:
        counts[sig.product] = counts.get(sig.product, 0) + 1
    by_product = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    cards = "".join(_card(sig, now) for sig in recent)
    chips = _chips(by_product, len(recent))
    return _page(cards, chips, facets.total, facets.embedded, facets.by_source,
                 facets.products)


def _detail_page(sig: library_client.SignalRecord, now) -> str:
    """One signal's page, through the shared Atelier shell.

    The body renders as Markdown through the same safe renderer `/decisions`
    uses. Most signal bodies are verbatim GitHub content — PR descriptions,
    review comments — which is authored as Markdown, so escaping it into a flat
    block would show a reader raw `**` and backticks on the text the record
    exists to convey. md_to_html escapes raw HTML and drops javascript:/data:
    schemes, so a body fetched from a stranger's PR comment stays untrusted
    input. Every other field is escape()d.

    `sig.body` arrives from `library_client` with any leading
    `agentctl-review` stamp already stripped, so a stamp-only or
    stamp-prefixed pr_comment body doesn't show the raw
    `<!-- agentctl-review ... -->` block to a reader who opens this page
    directly rather than clicking through a clipped card
    (issue_01M183011ZYEZF11VRPBT6Z1J6; centralized to the read boundary in
    convivy-orient#254 round 2 after the same unstripped body turned up on
    three more render sites).
    """
    badges = [f'<span class="badge prod">{escape(sig.product)}</span>',
              f'<span class="badge kind">{escape(sig.kind)}</span>']
    if sig.privacy_class != PRIVACY_INTERNAL:
        badges.append(f'<span class="badge priv">{escape(sig.privacy_class)}</span>')
    when = sig.produced_at or sig.ingested_at
    meta_bits = [f'<span class="who">{escape(sig.actor)}</span>',
                 f"captured {_age(when, now)} ago",
                 f"via {escape(sig.source)}"]
    if sig.stream:
        meta_bits.append(f'stream {escape(sig.stream)}')
    url = (sig.refs or {}).get("url") if isinstance(sig.refs, dict) else None
    safe_url = safe_http_url(url)
    if safe_url is not None:
        meta_bits.append(f'<a class="src" href="{escape(safe_url, quote=True)}" '
                         f'target="_blank" rel="noopener">source ↗</a>')
    body = (sig.body or "").strip()
    body_html = (f'<div class="body">{md_to_html(body)}</div>' if body
                 else '<div class="body"><p class="none">This signal has no body.</p></div>')

    page = f'''<div class="ops ops-detail">
  <header class="ops-head">
    <div class="eyebrow-line"><span class="dot"></span><a class="back" href="/archives">← archives</a></div>
    <div class="detail-badges">{"".join(badges)}</div>
    <h1>{escape(sig.kind.replace("_", " ").title())} · {escape(sig.product)}</h1>
    <div class="meta">{" · ".join(meta_bits)}</div>
    <div class="sigid"><code>{escape(sig.id)}</code></div>
  </header>
  <div class="wrap">
    <article class="card detail">
      {body_html}
    </article>
  </div>
</div>'''
    return ds.page_shell(
        title=f"{sig.kind} · {sig.product}",
        nav_html=ds.nav_component("/archives", "full"),
        body=page,
        extra_css=_CSS,
    )


def detail_html(signal_id: str, *, viewer_tier: str):
    """Render one signal's page, or None when it must not be served here.

    **`viewer_tier` is a required precondition, not a hint, and this function
    raises on anything but "full".** The tier gate lives in the route, and a
    later caller that forgot it would serve every internal signal in the store —
    16k of them — to any authenticated viewer. Stating that dependency in prose
    is the failure mode `platform/decisions/0016` names: a control the caller
    never encounters at the moment it decides. Taking the tier as a required
    keyword makes it unforgettable, and checking it here makes it unbypassable
    (security-reviewer, convivy-orient#152).

    **Internal-class only, deliberately, and a customer-class row returns None
    exactly as a missing one does.** The browse page's module docstring gives
    the reason and it governs here too: Orient's own agent credential lacks the
    Library's `signal.customer` capability, so a customer-class row 404s at the
    Library before it ever reaches this function — the privacy wall is the
    credential, not a check here.

    Returning None rather than a "you may not see this" page is also deliberate:
    a distinguishable response would confirm an id exists to a viewer who may
    not see it, which hands an existence oracle to exactly the population this
    gate excludes. `library_client.get_signal` already collapses "missing" and
    "customer-class, not `signal.customer`" into the same None, so no separate
    privacy_class check is needed here — widening it would mean reading the
    class off a response the Library has already decided not to allow.

    Do NOT "fix" this by rendering whatever `get_signal` returns unconditionally.
    If a scoped non-admin full role is ever added to roles.json, revisit this and
    the browse page together.
    """
    if viewer_tier != "full":
        raise ValueError(
            f"detail_html serves full-tier viewers only; got {viewer_tier!r}. "
            "The caller must gate on tier BEFORE rendering.")
    now = utcnow()
    sig = library_client.get_signal(signal_id)
    if sig is None:
        return None
    return _detail_page(sig, now)


def main() -> None:
    """Seed a static empty-state fallback at site/signals.html, served only if a
    live render throws — deliberately empty, not a stale snapshot."""
    SITE.mkdir(parents=True, exist_ok=True)
    fallback = _page(
        '<div class="allclear">Live view unavailable — reload in a moment.</div>',
        '<button class="chip" data-prod="all" aria-pressed="true">All <span class="n">0</span></button>',
        0, 0, [])
    (SITE / "signals.html").write_text(fallback, encoding="utf-8")
    print("Wrote site/signals.html (static fallback)")


_CSS = r"""
/* An author-origin display (e.g. .capture-form's flex) outranks the UA
   stylesheet's [hidden]{display:none} in the cascade, silently defeating the
   hidden attribute the page JS toggles — !important restores it globally
   (PR #57 round-1 blocker; #resultsSec/#results had the same latent defect). */
[hidden]{display:none!important;}
/* Site-token aliases: these components were built on the ops palette
   (--accent/--hairline/…); mapping those names onto the Atelier tokens keeps
   the markup + rules intact while the page follows the site theme, light and
   dark, through the shared machinery. */
.ops{
  --paper:var(--bg);--hairline:var(--line);--hairline-strong:var(--line);
  --muted:var(--ink-2);--faint:var(--ink-3);
  --accent:var(--brand-ink);--accent-soft:var(--brand-wash);
  --good:var(--ok);--good-soft:color-mix(in srgb,var(--ok) 14%,var(--surface));
  --warn-soft:color-mix(in srgb,var(--warn) 16%,var(--surface));
}
.ops{line-height:1.5;}
.ops .wrap{max-width:760px;margin:0 auto;padding:0 2px 72px;}
.ops .ops-head{max-width:760px;margin:0 auto;padding:22px 2px 0;}
.ops .eyebrow-line{font-family:var(--mono);font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);display:flex;align-items:center;gap:8px;}
.ops .eyebrow-line .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px var(--accent-soft);}
.ops h1{font-size:1.55rem;letter-spacing:-.02em;margin:6px 0 2px;text-wrap:balance;font-weight:640;}
.ops .lede{color:var(--muted);font-size:.92rem;margin:0 0 12px;font-variant-numeric:tabular-nums;}
.ops .count{color:var(--accent);font-weight:680;}
.ops .search{display:flex;gap:8px;margin:0 0 12px;}
.ops .search input{flex:1;font-family:var(--sans);font-size:.92rem;color:var(--ink);background:var(--surface);
border:1px solid var(--hairline-strong);border-radius:10px;padding:10px 13px;}
.ops .search input:focus-visible{outline:2px solid var(--accent);outline-offset:1px;border-color:var(--accent);}
.ops .search button{font-family:var(--sans);font-size:.88rem;font-weight:560;background:var(--accent);color:var(--surface);
border:none;border-radius:10px;padding:10px 18px;cursor:pointer;}
.ops .search button[disabled]{opacity:.55;cursor:default;}
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
.ops .sec .clear{font-family:var(--mono);font-size:.68rem;letter-spacing:.05em;text-transform:uppercase;color:var(--accent);
background:none;border:none;cursor:pointer;padding:0;}
.ops .sec .tail-hint{font-family:var(--sans);font-size:.72rem;letter-spacing:0;text-transform:none;color:var(--faint);}
/* Browse-by-shape and search share the `.card` panel look but are two
   visibly distinct affordances, deliberately never merged into one control —
   one filters the whole table exactly, the other ranks an approximate
   window, and a reader must be able to tell which is which at a glance. */
.ops .shape,.ops .search-panel{padding:16px;margin-bottom:14px;}
.ops .shape-form{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
.ops .shape-form select,.ops .shape-form input[type=text]{font-family:var(--sans);font-size:.88rem;color:var(--ink);
background:var(--surface-2);border:1px solid var(--hairline-strong);border-radius:8px;padding:8px 11px;}
.ops .shape-form .shape-or{font-family:var(--mono);font-size:.72rem;color:var(--faint);}
.ops .shape-form button{font-family:var(--sans);font-size:.88rem;font-weight:560;background:var(--accent);color:var(--surface);
border:none;border-radius:8px;padding:9px 16px;cursor:pointer;}
.ops .shape-meta{margin-top:11px;font-family:var(--mono);font-size:.76rem;color:var(--muted);}
.ops .search-panel .search{margin:0;}
.ops .search-note{margin-top:11px;font-family:var(--mono);font-size:.76rem;color:var(--muted);}
.ops .shape .queue,.ops .search-panel .queue{margin-top:14px;}
.ops .queue{display:flex;flex-direction:column;gap:12px;}
.ops .card{background:var(--surface);border:1px solid var(--hairline);border-radius:14px;box-shadow:var(--shadow);overflow:hidden;padding-bottom:13px;}
.ops .card-top{display:flex;align-items:center;gap:9px;padding:13px 16px 0;flex-wrap:wrap;}
.ops .badge{font-family:var(--mono);font-size:.68rem;letter-spacing:.05em;text-transform:uppercase;padding:3px 8px;border-radius:6px;font-weight:600;}
.ops .badge.prod{background:var(--accent-soft);color:var(--accent);}
.ops .badge.kind{background:transparent;color:var(--faint);border:1px solid var(--hairline-strong);}
.ops .badge.priv{background:var(--warn-soft);color:var(--warn);}
.ops .badge.dist{background:var(--good-soft);color:var(--good);}
.ops .stream{font-family:var(--mono);font-size:.72rem;color:var(--muted);}
.ops .age{margin-left:auto;font-family:var(--mono);font-size:.72rem;color:var(--faint);font-variant-numeric:tabular-nums;white-space:nowrap;}
.ops .body{color:var(--muted);font-size:.88rem;margin:8px 16px 0;white-space:pre-wrap;overflow-wrap:anywhere;}
.ops .body.body-empty{font-style:italic;color:var(--faint);}
.ops .meta{padding:9px 16px 0;font-family:var(--mono);font-size:.72rem;color:var(--faint);}
.ops .meta .who{color:var(--muted);}
.ops .meta a.src{color:var(--accent);text-decoration:none;}
.ops .meta a.perma{color:var(--faint);text-decoration:none;}
.ops .meta a.perma:hover{color:var(--accent);}
/* Shown only on a card whose body was clipped, which is the case where the card
   is not the whole record and the permalink is the point. */
.ops a.more{display:inline-block;margin:9px 16px 0;font-size:.8rem;color:var(--accent);text-decoration:none;}
.ops a.more:hover{text-decoration:underline;}

/* The detail page. `.ops .body` carries white-space:pre-wrap for the browse
   cards, whose bodies are escaped plain text; here the body is real markup from
   md_to_html, and pre-wrap would render every source newline as a visible blank
   line on top of the block spacing the tags already provide. */
.ops-detail .ops-head{padding-bottom:6px;}
.ops-detail .detail-badges{display:flex;gap:6px;margin:10px 0 4px;}
.ops-detail .sigid{margin-top:8px;font-family:var(--mono);font-size:.72rem;color:var(--faint);user-select:all;}
.ops-detail .card.detail{padding:6px 0 20px;}
.ops-detail .body{white-space:normal;color:var(--ink);font-size:.92rem;line-height:1.62;}
.ops-detail .body p{margin:0 0 .85em;}
.ops-detail .body ul,.ops-detail .body ol{margin:0 0 .85em 1.3em;}
.ops-detail .body h1,.ops-detail .body h2,.ops-detail .body h3{margin:1.3em 0 .5em;line-height:1.3;}
.ops-detail .body code{font-family:var(--mono);font-size:.86em;}
.ops-detail .body pre{overflow-x:auto;padding:10px 12px;border:1px solid var(--hairline);border-radius:8px;}
.ops-detail .body .none{color:var(--faint);}
.ops .empty{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.8rem;padding:30px 0;display:none;}
.ops .allclear{text-align:center;color:var(--faint);font-family:var(--mono);font-size:.82rem;padding:34px 0;}
.ops .footnote{margin-top:34px;padding-top:16px;border-top:1px solid var(--hairline);color:var(--faint);font-size:.8rem;}
.ops .footnote code{font-family:var(--mono);color:var(--muted);font-size:.76rem;}
.ops .capture{margin-top:18px;}
.ops .capture[data-can-capture="false"]{display:none;}
.ops .capture-toggle{font-family:var(--mono);font-size:.78rem;letter-spacing:.04em;color:var(--accent);
background:var(--surface);border:1px dashed var(--hairline-strong);border-radius:10px;padding:9px 14px;cursor:pointer;}
.ops .capture-toggle[aria-expanded="true"]{border-style:solid;border-color:var(--accent);}
.ops .capture-form{margin-top:10px;padding:16px;display:flex;flex-direction:column;gap:12px;}
.ops .capture-form label{display:flex;flex-direction:column;gap:4px;font-family:var(--mono);font-size:.7rem;
letter-spacing:.1em;text-transform:uppercase;color:var(--faint);}
.ops .capture-form label .opt{text-transform:none;letter-spacing:0;opacity:.7;}
.ops .capture-form input[type=text],.ops .capture-form input:not([type]),.ops .capture-form textarea{font-family:var(--sans);
font-size:.9rem;color:var(--ink);background:var(--surface-2);border:1px solid var(--hairline-strong);
border-radius:8px;padding:8px 11px;letter-spacing:normal;text-transform:none;}
.ops .capture-form input:focus-visible,.ops .capture-form textarea:focus-visible{outline:2px solid var(--accent);outline-offset:1px;}
.ops .capture-form textarea{resize:vertical;line-height:1.45;}
.ops .cf-row{display:flex;gap:10px;flex-wrap:wrap;}
.ops .cf-row label{flex:1;min-width:140px;}
.ops .cf-row label.grow{flex:2;}
.ops .cf-priv{border:1px solid var(--hairline);border-radius:8px;margin:0;padding:6px 12px 8px;display:flex;gap:14px;align-items:center;}
.ops .cf-priv legend{font-family:var(--mono);font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);padding:0 4px;}
.ops .cf-priv label{flex-direction:row;align-items:center;gap:6px;text-transform:none;letter-spacing:0;
font-size:.84rem;color:var(--muted);font-family:var(--sans);}
.ops .cf-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
.ops .cf-upload{font-family:var(--sans);font-size:.85rem;color:var(--accent);background:var(--accent-soft);
border:none;border-radius:8px;padding:9px 14px;cursor:pointer;}
.ops .cf-file{font-family:var(--mono);font-size:.72rem;color:var(--muted);overflow-wrap:anywhere;}
.ops .cf-submit{margin-left:auto;font-family:var(--sans);font-size:.88rem;font-weight:560;background:var(--accent);
color:var(--surface);border:none;border-radius:8px;padding:9px 18px;cursor:pointer;}
.ops .cf-submit[disabled]{opacity:.55;cursor:default;}
.ops .cf-note{font-family:var(--mono);font-size:.76rem;padding:9px 12px;border-radius:8px;}
.ops .cf-note.ok{background:var(--good-soft);color:var(--good);}
.ops .cf-note.err{background:var(--warn-soft);color:var(--warn);}
.ops .cf-note.info{background:var(--accent-soft);color:var(--accent);}
@media (max-width:520px){.ops h1{font-size:1.35rem;}.ops .search{flex-wrap:wrap;}.ops .search button{flex:1;}}
"""

# The recent-60 browse's product-chip filter — unchanged by the Archives-tail
# work below, since the spec keeps this section as is.
#
# Stashes the selected chip across the shell's refresh control
# (issue_01M1517CYMAV0GAEB2FYQQKF4N): a refresh asks this page for newer
# signals, not a fresh start, so it should not silently drop which product
# the reader had filtered to. Reuses the Reading Room's stash-then-restore
# shape (room_build.py) — write to sessionStorage right before the reload,
# read and clear it on the next load — rather than a persistent key, so an
# ordinary navigation away and back later does not resurrect a stale filter.
# The `convivy:beforerefresh` event is design_system.py's NAV_CLUSTER_JS,
# fired only when ITS button triggers the reload, never a bare browser
# reload. The browse-by-shape and search panels below are NOT restored the
# same way: each shows a point-in-time query result rather than a standing
# view of the browse list, so replaying an old query on every refresh would
# not obviously read as "preserved" the way the chip does.
_JS = r"""
(function(){
  var chips=document.getElementById('chips'),browse=document.getElementById('browse'),
      empty=document.getElementById('empty'),activeProd='all';
  var FILTER_KEY='signals-active-prod';
  function applyFilter(){var vis=0,total=0;
    browse.querySelectorAll('.card').forEach(function(c){total++;
      var m=activeProd==='all'||c.dataset.prod===activeProd;
      c.style.display=m?'':'none';if(m)vis++;});
    empty.style.display=(vis===0&&total>0)?'block':'none';}
  function selectChip(prod){
    var all=chips.querySelectorAll('.chip'), target=null;
    all.forEach(function(c){if(c.dataset.prod===prod)target=c;});
    // A stashed product that dropped out of this view (the recent-60 window
    // moved on) is left alone rather than blanking every chip's pressed
    // state — the default "All" stays visibly selected, matching what
    // activeProd already is.
    if(!target)return;
    all.forEach(function(c){c.setAttribute('aria-pressed','false');});
    target.setAttribute('aria-pressed','true');
    activeProd=prod;applyFilter();}
  chips.addEventListener('click',function(e){var chip=e.target.closest('.chip');if(!chip)return;
    selectChip(chip.dataset.prod);});
  document.addEventListener('convivy:beforerefresh',function(){
    try{sessionStorage.setItem(FILTER_KEY,activeProd);}catch(e){}});
  try{
    var stashed=sessionStorage.getItem(FILTER_KEY);
    if(stashed!==null){
      sessionStorage.removeItem(FILTER_KEY);
      if(stashed!=='all')selectChip(stashed);
    }
  }catch(e){}
})();

// Browse by shape + text search — the Archives tail. Both build results with
// document.createElement + textContent only, matching the recent-60 cards
// above: signal bodies are verbatim GitHub content and must never reach the
// page through an innerHTML sink.
(function(){
  function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;
    if(text!=null)e.textContent=text;return e;}
  function card(item,opts){
    opts=opts||{};
    var c=el('article','card'),top=el('div','card-top');
    top.appendChild(el('span','badge prod',item.product));
    top.appendChild(el('span','badge kind',item.kind));
    if(item.privacy_class&&item.privacy_class!=='internal')
      top.appendChild(el('span','badge priv',item.privacy_class));
    if(opts.showDistance&&typeof item.distance==='number')
      top.appendChild(el('span','badge dist','d '+item.distance.toFixed(3)));
    if(item.stream)top.appendChild(el('span','stream',item.stream));
    var when=item.produced_at||item.ingested_at;
    top.appendChild(el('span','age',when?when.slice(0,10):''));
    c.appendChild(top);
    var body=(item.body||'').trim();
    if(body){if(body.length>__BODY_CLIP__)body=body.slice(0,__BODY_CLIP__)+' …';c.appendChild(el('p','body',body));}
    var meta=el('div','meta');meta.appendChild(el('span','who',item.actor));
    meta.appendChild(document.createTextNode(' · '+item.source+' · '));
    var perma=el('a','perma');perma.href='/archives/'+encodeURIComponent(item.id);perma.textContent=item.id;
    meta.appendChild(perma);
    var url=item.refs&&item.refs.url;
    if(typeof url==='string'&&(url.indexOf('https://')===0||url.indexOf('http://')===0)){
      meta.appendChild(document.createTextNode(' · '));
      var a=el('a','src','source ↗');a.href=url;a.target='_blank';a.rel='noopener';
      meta.appendChild(a);}
    c.appendChild(meta);return c;}

  // --- Browse by shape: GET /api/signals/browse, filters the whole table ----
  var shapeForm=document.getElementById('shapeForm'),
      shapeSelect=document.getElementById('shapeKindSelect'),
      shapeFree=document.getElementById('shapeKindFree'),
      shapeMeta=document.getElementById('shapeMeta'),
      shapeResults=document.getElementById('shapeResults');
  shapeForm.addEventListener('submit',function(e){
    e.preventDefault();
    var kind=(shapeFree.value.trim()||shapeSelect.value).trim();
    shapeResults.replaceChildren();
    shapeMeta.hidden=false;
    if(!kind){shapeMeta.textContent='Pick or type a kind first.';return;}
    shapeMeta.textContent='Loading…';
    fetch('/api/signals/browse?kind='+encodeURIComponent(kind),{credentials:'same-origin'})
      .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
      .then(function(res){
        if(!res.ok){shapeMeta.textContent='Could not browse "'+kind+'": '+(res.j.error||'error');return;}
        var j=res.j;
        shapeMeta.textContent='This archive holds '+j.total.toLocaleString()+
          ' signals of kind "'+j.kind+'". Showing the '+j.signals.length+
          ' most recent of them.';
        if(!j.signals.length){shapeResults.appendChild(el('div','allclear','No signals of this kind.'));return;}
        j.signals.forEach(function(item){shapeResults.appendChild(card(item,{}));});
      })
      .catch(function(){shapeMeta.textContent='Could not reach the Archives — reload in a moment.';});
  });

  // --- Search: GET /api/signals/search, ranks a retrieved window ------------
  var searchForm=document.getElementById('searchForm'),
      searchQ=document.getElementById('q'),
      searchNote=document.getElementById('searchNote'),
      searchResults=document.getElementById('searchResults');
  searchForm.addEventListener('submit',function(e){
    e.preventDefault();
    var q=searchQ.value.trim();
    searchResults.replaceChildren();
    if(!q){searchNote.textContent='Type something to search.';return;}
    searchNote.textContent='Searching…';
    fetch('/api/signals/search?q='+encodeURIComponent(q),{credentials:'same-origin'})
      .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
      .then(function(res){
        if(!res.ok){searchNote.textContent='Search failed: '+(res.j.error||'error');return;}
        var j=res.j;
        if(!j.signals.length){
          searchNote.textContent='Nothing among the closest matches for "'+q+
            '." That is not the same as "none exist" — try Browse by shape above for the complete list.';
          return;}
        searchNote.textContent='The '+j.signals.length+' closest match'+(j.signals.length===1?'':'es')+
          ' for "'+q+'," ranked by similarity — not a count of what exists.';
        j.signals.forEach(function(item){searchResults.appendChild(card(item,{showDistance:true}));});
      })
      .catch(function(){searchNote.textContent='Could not reach the Archives — reload in a moment.';});
  });
})();

// The "Log a signal" capture panel. The file is read CLIENT-side (FileReader)
// into the body field — the POST is the same JSON /api/signals every other
// capture path uses, no multipart. The dedup_key is minted per attempt and kept
// across retries of the same content, so a double-submit lands idempotent
// (the API returns the existing row) instead of duplicating.
(function(){
  var toggle=document.getElementById('captureToggle');
  if(!toggle)return;
  var form=document.getElementById('captureForm'),note=document.getElementById('captureNote'),
      fileInput=document.getElementById('captureFile'),uploadBtn=document.getElementById('captureUpload'),
      fileName=document.getElementById('captureFileName'),uploadedName=null,dedupKey=null;
  function mintKey(){
    dedupKey='manual-'+(window.crypto&&crypto.randomUUID?crypto.randomUUID():
      Date.now().toString(36)+'-'+Math.random().toString(36).slice(2));}
  function setNote(cls,text){note.className='cf-note '+cls;note.textContent=text;note.hidden=false;}
  toggle.addEventListener('click',function(){
    var open=form.hidden;form.hidden=!open;
    toggle.setAttribute('aria-expanded',open?'true':'false');
    if(open&&!dedupKey)mintKey();});
  // Any edit re-mints the key: edited content is NEW content. Without this, a
  // submit whose response was lost (but landed server-side) followed by an edit
  // and a retry would hit the idempotency path and silently discard the edit.
  // A retry of UNCHANGED content fires no input event and keeps its key.
  form.addEventListener('input',mintKey);
  uploadBtn.addEventListener('click',function(){
    if(form.elements.body.value.trim()&&
       !window.confirm('Replace the current content with the file?'))return;
    fileInput.click();});
  fileInput.addEventListener('change',function(){
    var f=fileInput.files&&fileInput.files[0];
    if(!f)return;
    var reader=new FileReader();
    reader.onload=function(){
      var text=String(reader.result||'');
      if(text.length>__UPLOAD_CLIP__){
        text=text.slice(0,__UPLOAD_CLIP__);
        setNote('info','File clipped to the '+(__UPLOAD_CLIP__).toLocaleString()+'-character store limit.');
      }else{note.hidden=true;}
      form.elements.body.value=text;
      uploadedName=f.name;fileName.textContent=f.name;
      mintKey(); // programmatic value set fires no input event — re-mint here
    };
    reader.onerror=function(){setNote('err','Could not read that file.');};
    reader.readAsText(f);
    fileInput.value='';});
  form.addEventListener('submit',function(e){
    e.preventDefault();
    var btn=form.querySelector('.cf-submit');btn.disabled=true;btn.textContent='Logging…';
    var payload={
      source:'manual',
      kind:form.elements.kind.value.trim(),
      product:form.elements.product.value.trim(),
      actor:form.elements.actor.value.trim(),
      privacy_class:form.elements.privacy_class.value,
      body:form.elements.body.value,
      dedup_key:dedupKey};
    var stream=form.elements.stream.value.trim();
    if(stream)payload.stream=stream;
    if(uploadedName)payload.meta={filename:uploadedName};
    fetch('/api/signals',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)
    }).then(function(r){
      return r.json().catch(function(){return {};}).then(function(j){
        if(!r.ok)throw new Error((j&&j.error)||('HTTP '+r.status));
        return j;});
    }).then(function(j){
      setNote('ok','Logged '+(j.id||'')+(j.idempotent?' (already captured)':'')+
        ' — reload to see it under Latest captured.');
      form.elements.body.value='';form.elements.stream.value='';
      uploadedName=null;fileName.textContent='';
      mintKey(); // the logged content is spent; next submit is a new signal
    }).catch(function(err){
      setNote('err','Capture failed: '+err.message);
    }).finally(function(){btn.disabled=false;btn.textContent='Log signal';});});
})();
"""


if __name__ == "__main__":
    main()
