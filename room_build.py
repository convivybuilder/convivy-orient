"""The Reading Room renderer (ARCH-357) — one member's personalized reading surface.

live_html(email) renders the room for the verified caller. The load-bearing
property is that the render is bounded by the GRANT, computed server-side. It is
**grant ∩ (curation ∪ the standing Briefs rule)**:

  - the GRANT is kb_index.room_entitlement(email, products) / entitled_docs(email,
    products), derived live from tiers.json + the caller's Library-resolved
    project scope + audience frontmatter;
  - the CURATION is the member's ReadingRoom entries (which docs, sections, order,
    notes) from the control DB;
  - the BRIEFS RULE (ARCH-372) renders every entitled doc under `*/briefs/` from
    roughly the last two weeks (`_BRIEF_WINDOW_DAYS`) in a standing "Briefs"
    section, with no curation entry required. Older ones stay reachable at
    /memory/briefings rather than accumulating here forever
    (issue_01M17W67YVZZC79NAYVSTRBKPB); a curator who explicitly places an old
    brief in a section is unaffected — the window narrows only the standing
    rule, never curation. The pointer to /memory/briefings is embedded in the
    payload only for full tier, since that route is a `_full_only_routes`
    entry (server.py) and a contractor following it would get a plain 404.

The Briefs rule is a widening of the CURATION half, never of the grant: it draws
only from entitled_docs(email), so it can surface at most what the member could
already be curated into. Briefs are a recurring stream — a new one lands every
week — and hand-curating each week's into each member's room is friction that
guarantees drift, which is how the last three attempts to put briefs in the Room
ended with an empty Room and a redirect pointing at it.

Only docs in BOTH get their body embedded in the page payload. A curated entry
pointing at a doc outside the member's current grant silently drops out — its body
is never placed in the response — so narrowing a grant (a change to the member's
project scope in the Library, or an audience edit) takes effect on the next load with no room edit, and a buggy or hostile entry can
render at most nothing. Doc bodies render client-side through the vendored
marked + DOMPurify pipeline (ARCH-244); the landing note and per-entry notes are
member/agent-authored text, so they go through DOMPurify too.

AN `item` OR `signal` ENTRY (CQ·147/148, E22) is resolved live on every render,
the same way a `doc` entry is: `library_client.get_issue_item`/`get_signal` is
called fresh each time, so a work item's state is always what the Library
reports at THIS render, never a value cached from curation time. Either kind
renders only when the viewer is full tier AND the record's product is in
`products` (the viewer's own Library-resolved scope, `room_api._in_scope` —
the same check `room_api._clean_entries` already applies at write time, not a
second implementation of it). An entry outside that grant contributes nothing
to the payload at all: not its title, not its id, not its body — the same
wall `docs_by_id` already is for a `doc` entry. A contractor's grant has no
item/signal half (room_api.py), so this loop never calls the Library for one
at all. A work item renders its title, state, specification and latest
activity (its most recent comment, if any); an Archives entry renders its
body, source and date. Every piece of free text — a specification, a
comment body, a signal body — goes through the same marked + DOMPurify path
the notes do; a title/state/source/date is plain text, rendered the same way
a doc title already is (`textContent`, never `innerHTML`).

There is deliberately NO static fallback (contrast the ARCH-182 full-tier pages):
a per-member static file would be a cross-member leak if fallback selection ever
picked the wrong one, so a render failure returns a plain 500 (server.py), never
another member's room. `get_issue_item`/`get_signal` raise on any Library
failure but a 404 (library_client.py), so a Library outage mid-render surfaces
the same plain 500 rather than a room silently missing one member's work item.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from html import escape
from types import SimpleNamespace

from sqlalchemy import select

import design_system as ds
import library_client
import orient_time
from access_identity import tier_for
from build import DOMPURIFY_JS, MARKED_JS
from control_db import session
from kb_index import entitled_docs
from models import ReadingRoom
from room_api import _in_scope

_PAGE_CSS = """
.room-wrap { display:grid; grid-template-columns:300px 1fr; gap:2rem; align-items:start; }
.room-side { position:sticky; top:5rem; max-height:calc(100vh - 7rem); overflow:auto; }
.room-landing { background:var(--brand-wash); border:1px solid var(--line); border-radius:12px;
                padding:1rem 1.25rem; margin-bottom:1.5rem; }
.room-landing:empty { display:none; }
.room-ask { display:flex; gap:.5rem; flex-wrap:wrap; align-items:center; margin-bottom:1.5rem; }
.room-ask input { flex:1 1 20rem; padding:.55rem .75rem; border:1px solid var(--line);
                  border-radius:8px; font:inherit; background:var(--surface); color:var(--ink); }
.room-ask button { padding:.55rem 1rem; border:0; border-radius:8px; background:var(--brand);
                   color:#fff; font:inherit; font-weight:600; cursor:pointer; }
.room-ask button:disabled { opacity:.6; cursor:default; }
.room-ask-reply { flex-basis:100%; font-size:.9rem; color:var(--ink-2); }
.room-section { margin-bottom:1.25rem; }
.room-section h4 { font-size:.72rem; text-transform:uppercase; letter-spacing:.08em;
                   color:var(--ink-3); margin:0 0 .5rem; }
.room-section-note { font-size:.82rem; color:var(--ink-3); margin:0 0 .75rem; }
.room-section-note a { color:var(--brand-ink); }
.room-entry { display:block; width:100%; text-align:left; background:none; border:0;
              border-radius:8px; padding:.5rem .6rem; cursor:pointer; color:var(--ink);
              font:inherit; }
.room-entry:hover { background:var(--brand-wash); }
.room-entry.on { background:var(--brand-wash); font-weight:600; }
.room-entry .re-note { display:block; font-size:.78rem; color:var(--ink-3); font-weight:400;
                       margin-top:.15rem; }
.room-entry .re-pin { color:var(--brand); }
.room-main { min-width:0; }
.room-empty { color:var(--ink-3); }

/* Reading-pane rhythm for rendered doc bodies (ported from the orientation-docs
   legibility pass 71deda2, extended with table/code/blockquote treatment). */
.room-main .doc-body { max-width:72ch; font-size:15px; line-height:1.65; color:var(--ink); }
.doc-body > :first-child { margin-top:0; }
.doc-body h1 { font-size:24px; font-weight:700; margin:0 0 16px; line-height:1.25; }
.doc-body h2 { font-size:19px; font-weight:650; margin:32px 0 10px; line-height:1.35; }
.doc-body h3 { font-size:16px; font-weight:650; margin:26px 0 8px; line-height:1.4; }
.doc-body h4 { font-size:13px; font-weight:650; margin:22px 0 6px; text-transform:uppercase;
               letter-spacing:.04em; color:var(--ink-2); }
.doc-body p { margin:14px 0; }
.doc-body ul, .doc-body ol { margin:14px 0; padding-left:26px; }
.doc-body li { margin:7px 0; }
.doc-body li > ul, .doc-body li > ol { margin:7px 0 5px; }
.doc-body li > p { margin:6px 0; }
.doc-body li::marker { color:var(--ink-3); }
.doc-body strong { font-weight:650; }
.doc-body a { color:var(--brand-ink); text-decoration:underline; text-underline-offset:2px; }
.doc-body code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.86em;
                 background:var(--surface-2); padding:1px 5px; border-radius:4px; }
.doc-body pre { background:var(--surface-2); padding:12px 14px; border-radius:8px;
                overflow-x:auto; margin:16px 0; }
.doc-body pre code { background:none; padding:0; }
.doc-body blockquote { border-left:3px solid var(--line); padding-left:14px; margin:16px 0;
                       color:var(--ink-2); }
.doc-body hr { border:0; border-top:1px solid var(--line); margin:28px 0; }
.doc-body img { max-width:100%; height:auto; }
/* Tables size to their content and scroll horizontally if wider than the pane
   (the #67 wide-content pattern), rather than crushing every column to a word. */
.doc-body table { display:block; width:max-content; max-width:100%; overflow-x:auto;
                  border-collapse:collapse; margin:18px 0; font-size:14px; }
.doc-body thead th { text-align:left; font-weight:650; border-bottom:2px solid var(--line);
                     white-space:nowrap; }
.doc-body th, .doc-body td { padding:8px 14px; border-bottom:1px solid var(--line);
                             vertical-align:top; }
.doc-body tbody tr:last-child td { border-bottom:0; }
.item-state { color:var(--ink-2); font-weight:600; margin:0 0 14px; }
.item-activity { margin-top:24px; padding-top:16px; border-top:1px solid var(--line); }
.item-activity-who { font-size:.82rem; color:var(--ink-3); margin:0 0 .4rem; }
.signal-meta { color:var(--ink-3); font-size:.85rem; margin-top:16px; }
@media (max-width:820px){ .room-wrap { grid-template-columns:1fr; } .room-side { position:static; max-height:none; } }
"""


def _load_room(email: str) -> tuple[SimpleNamespace | None, list[dict]]:
    """The member's active room (scalar fields as a plain carrier) + its entries as
    dicts, both detached from the session. None when the member has no active room."""
    key = (email or "").strip().lower()
    with session() as s:
        room = s.scalar(
            select(ReadingRoom).where(
                ReadingRoom.email == key, ReadingRoom.status == "active"))
        if room is None:
            return None, None
        entries = [{
            "kind": e.kind, "doc_id": e.doc_id, "section": e.section or "",
            "position": e.position, "note": e.note or "", "pinned": bool(e.pinned),
        } for e in room.entries]
        # Copy the scalar fields out before the session closes — a plain carrier,
        # not a live ORM row.
        detached = SimpleNamespace(
            id=room.id, email=room.email, title=room.title,
            landing_note=room.landing_note, status=room.status)
        return detached, entries


def _first_name(email: str) -> str:
    """A display first name from an email local part (dana@… → 'Dana'); '' if none."""
    token = re.split(r"[._+]", (email or "").split("@")[0])[0]
    return token.title() if token else ""


def _stub(tier: str, email: str) -> str:
    """Shown when a verified member has no room yet (possible during rollout).
    Personalizes the heading to the viewer and names the human who provisions rooms
    (the operator, the admin, not "the steward" — that's the agentic role)."""
    name = _first_name(email)
    heading = f"{escape(name)}'s Reading Room" if name else "Your Reading Room"
    body = (
        '<header class="hero"><div class="hero-inner">'
        f'<h1>{heading}</h1>'
        '<p class="hero-sub">You don\'t have a Reading Room yet. '
        'Ask the operator to set one up for you.</p>'
        '</div></header>')
    return ds.page_shell(title="Reading Room", nav_html=ds.nav_component("/room", tier), body=body,
                         extra_css=_PAGE_CSS)


# A brief is any entitled doc under `<project>/briefs/`. Matching on the id keeps
# this independent of which KB project a brief lives in — engineering practice
# sits in `engineering`, the business brief in `convivy` — because the project
# tracks the SUBJECT while the Room is where every brief is READ (ARCH-372).
_BRIEF_ID_RE = re.compile(r"^[^/:]+/briefs/")
_BRIEF_DATE_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})$")

# The standing Briefs section shows roughly the last two weeks (issue
# issue_01M17W67YVZZC79NAYVSTRBKPB): a new brief lands weekly, so two cycles
# of slack covers a reader back from a short trip without the section
# becoming the archive it is meant to defer to. Every brief, in or out of the
# window, stays reachable at /memory/briefings.
_BRIEF_WINDOW_DAYS = 14


def _brief_sort_key(doc: dict) -> str:
    """Sort key for the Briefs section: the trailing -YYYY-MM-DD, else "" so an
    id without a date sorts last under reverse=True instead of raising."""
    m = _BRIEF_DATE_RE.search(doc.get("id") or "")
    return m.group(1) if m else ""


def _in_brief_window(doc: dict, cutoff: str) -> bool:
    """True if `doc` belongs in the standing Briefs section's recent window.

    An id without a parseable date can't be shown to be old, and the render
    has no fallback (a 500 shows the member nothing), so an undated brief is
    kept rather than silently dropped — the same safer-default the sort key
    already applies. The boundary is inclusive: a brief dated exactly on the
    cutoff day is kept, not dropped."""
    date = _brief_sort_key(doc)
    return date == "" or date >= cutoff


def _latest_activity(item: library_client.IssueRecord) -> dict | None:
    """The work item's most recent comment, for the room payload, or None when
    it has none. `get_issue_item` fills `item.comments` in chronological order
    (oldest first, the same order `issue_build._thread` relies on to keep "the
    most recent of the rest" with a plain `[-limit:]` slice), so the last
    element is the latest one."""
    comments = list(getattr(item, "comments", None) or [])
    if not comments:
        return None
    c = comments[-1]
    return {"actor": c.actor, "body": c.body, "at": c.at.isoformat() if c.at else None}


def _signal_label(sig: library_client.SignalRecord) -> str:
    """A short nav-list label for an Archives entry — it carries no title of
    its own (unlike a doc or a work item), so this names it by what it is
    rather than leaving the button blank."""
    return f"{sig.kind} ({sig.source})"


def live_html(email: str, products: frozenset[str]) -> str:
    """Render the caller's Reading Room. `products` is the caller's own
    Library-resolved project scope (server.py resolves it once per request,
    beside the identity gate). Raises on a hard failure (server.py turns
    that into a 500 — never a fallback to another member's room)."""
    tier = tier_for(email)
    room, entries = _load_room(email)
    if room is None:
        return _stub(tier, email)

    # The grant, as the id→doc map (body + title live here). Only entitled docs
    # are ever loaded, so an entry outside the grant has no body to embed.
    docs_by_id = {d["id"]: d for d in entitled_docs(email, products)}

    # Resolve every entry by kind, live, on every render — never cached from
    # curation time, so a work item's state and an Archives entry's body are
    # always what the Library answers for THIS render. A `doc` entry is
    # intersected with the grant above, as always. An `item`/`signal` entry is
    # read from the Library and kept only when the viewer is full tier AND the
    # record's own product is in `products` (the viewer's Library-resolved
    # scope) — `room_api._in_scope`, the same per-kind check room_api.py
    # already applies at write time, reused rather than re-derived. The tier
    # check is checked first so a contractor's render never calls the Library
    # for either kind at all — their grant has no item/signal half. An entry
    # that fails either check is simply dropped: it contributes nothing to
    # `resolved`, so none of its title, id or body ever reaches the payload.
    full = tier == "full"
    resolved: list[tuple[dict, str, object]] = []
    for e in entries:
        kind = e["kind"]
        if kind == "doc":
            doc = docs_by_id.get(e["doc_id"])
            if doc is not None:
                resolved.append((e, "doc", doc))
        elif kind == "item" and full:
            item = library_client.get_issue_item(e["doc_id"])
            if item is not None and _in_scope(item.product, products):
                resolved.append((e, "item", item))
        elif kind == "signal" and full:
            sig = library_client.get_signal(e["doc_id"])
            if sig is not None and _in_scope(sig.product, products):
                resolved.append((e, "signal", sig))

    # Only `doc` entries feed the Briefs rule below — an item/signal entry
    # cannot be a brief and must not suppress one.
    curated_ids = {e["doc_id"] for e, kind, _ in resolved if kind == "doc"}

    # Sections render in CURATION order — the order they first appear in the room's
    # entries (which arrive id-ordered, i.e. the seed/edit order) — NOT alphabetically,
    # so "Start here" leads and the curator controls the sequence. Within a section,
    # order by position (then doc_id) so a curator can reorder without touching others.
    # A section mixes kinds freely: that's the DoD's "one section, each in its
    # own form".
    section_order: list[str] = []
    by_section: dict[str, list] = {}
    for e, kind, record in resolved:
        if e["section"] not in by_section:
            by_section[e["section"]] = []
            section_order.append(e["section"])
        by_section[e["section"]].append((e, kind, record))

    sections: list[dict] = []
    docs_payload: dict[str, dict] = {}
    items_payload: dict[str, dict] = {}
    signals_payload: dict[str, dict] = {}
    for name in section_order:
        out_entries = []
        for e, kind, record in sorted(by_section[name], key=lambda t: (t[0]["position"], t[0]["doc_id"])):
            if kind == "doc":
                out_entries.append({
                    "docId": e["doc_id"], "title": record["title"], "kind": record["kind"],
                    "entryKind": "doc", "note": e["note"], "pinned": e["pinned"],
                })
                docs_payload[e["doc_id"]] = {"title": record["title"], "body": record["body"]}
            elif kind == "item":
                out_entries.append({
                    "docId": e["doc_id"], "title": record.title, "entryKind": "item",
                    "note": e["note"], "pinned": e["pinned"],
                })
                items_payload[e["doc_id"]] = {
                    "title": record.title, "state": record.state,
                    "specification": record.specification or "",
                    "latestActivity": _latest_activity(record),
                }
            else:  # signal
                out_entries.append({
                    "docId": e["doc_id"], "title": _signal_label(record), "entryKind": "signal",
                    "note": e["note"], "pinned": e["pinned"],
                })
                when = record.produced_at or record.ingested_at
                signals_payload[e["doc_id"]] = {
                    "body": record.body, "source": record.source,
                    "date": when.isoformat() if when else None,
                }
        sections.append({"name": name, "entries": out_entries})

    # The standing Briefs section (ARCH-372). Drawn from the grant, so it cannot
    # surface anything curation could not; a brief already curated into another
    # section is skipped rather than shown twice, and renders there regardless
    # of age — curator intent outranks the window below.
    #
    # Windowed to the last _BRIEF_WINDOW_DAYS days so the section stays "worth
    # reading now" rather than accumulating every brief ever published; the
    # rest remain reachable at /memory/briefings (issue_01M17W67YVZZC79NAYVSTRBKPB).
    # Full tier renders the section even when the window empties it, because it
    # gets the Archives note explaining where the rest went — that's exactly the
    # moment a reader most needs the pointer, not the moment to hide it. Contractor
    # tier has no such note (see below), so the same empty state would be a bare
    # heading over nothing; contractor tier skips the section instead.
    #
    # `standing: True` marks this as the rule's own section, not a name string
    # — a curator can title their own section "Briefs" too (room_api.py sets no
    # reserved words), and the client keys the note off this marker rather than
    # off `sec.name` so a curated "Briefs" section never picks it up.
    cutoff = (orient_time.now() - timedelta(days=_BRIEF_WINDOW_DAYS)).strftime("%Y-%m-%d")
    brief_candidates = [d for d in docs_by_id.values()
                        if _BRIEF_ID_RE.match(d["id"]) and d["id"] not in curated_ids]
    if brief_candidates:
        brief_docs = [d for d in brief_candidates if _in_brief_window(d, cutoff)]
        brief_docs.sort(key=_brief_sort_key, reverse=True)
        out_entries = []
        for doc in brief_docs:
            out_entries.append({
                "docId": doc["id"], "title": doc["title"], "kind": doc["kind"],
                "entryKind": "doc", "note": "", "pinned": False,
            })
            docs_payload[doc["id"]] = {"title": doc["title"], "body": doc["body"]}
        briefs_section = {"name": "Briefs", "entries": out_entries, "standing": True}
        # /reference/briefings is a `_full_only_routes` entry (server.py): a
        # contractor who followed the link would get a plain 404, so the note
        # (and the link itself) is embedded in the payload only for full tier
        # — never sent client-side and gated by a tier check there, so a
        # contractor's response contains no trace of the link at all.
        if tier == "full":
            briefs_section["noteHtml"] = (
                'Older briefings live in <a href="/reference/briefings">Archives</a>.')
        # Only append when the section has something to show or something to say.
        # Full tier can still be empty-but-rendered here because it always has the
        # note above to explain the gap; contractor tier never gets a note, so an
        # empty contractor section would be a heading with nothing under it and no
        # explanation — worse than not showing the section at all.
        if out_entries or briefs_section.get("noteHtml"):
            sections.append(briefs_section)

    payload = json.dumps({
        "landingNote": room.landing_note or "",
        "sections": sections,
        "docs": docs_payload,
        "items": items_payload,
        "signals": signals_payload,
    }, ensure_ascii=False).replace("</", "<\\/")

    title = escape(room.title or "Reading Room")
    body = f"""<header class="hero"><div class="hero-inner">
  <h1>{title}</h1>
  <p class="hero-sub">Your reading, curated for you.</p>
</div></header>
<section class="home-section">
  <div class="room-landing" id="roomLanding"></div>
  <form class="room-ask" id="roomAsk" autocomplete="off">
    <input id="roomAskInput" type="text" name="request"
           placeholder="Ask to change your room — “add the deployment runbook”, “explain the signal store”…">
    <button type="submit" id="roomAskBtn">Ask</button>
    <div class="room-ask-reply" id="roomAskReply" hidden></div>
  </form>
  <div class="room-wrap">
    <nav class="room-side" id="roomSide" aria-label="Your docs"></nav>
    <div class="room-main" id="roomMain">
      <div class="room-empty">This room has no docs yet. Ask the steward to add some.</div>
    </div>
  </div>
</section>
<script>{MARKED_JS.read_text(encoding="utf-8")}</script>
<script>{DOMPURIFY_JS.read_text(encoding="utf-8")}</script>
<script>
const ROOM = {payload};
function md(src) {{ return DOMPurify.sanitize(marked.parse(src || "")); }}
(function() {{
  const landing = document.getElementById("roomLanding");
  if (ROOM.landingNote) landing.innerHTML = md(ROOM.landingNote);
  const side = document.getElementById("roomSide");
  const main = document.getElementById("roomMain");
  let firstBtn = null;
  // Each kind renders in its own form (DoD): a doc shows its body; a work
  // item its title, state, specification and latest activity; an Archives
  // entry its body, source and date. Built with createElement/textContent
  // rather than string interpolation, so a title or state cannot smuggle
  // markup in — the same discipline the nav-list buttons above already use.
  function renderEntry(en) {{
    main.innerHTML = "";
    const article = document.createElement("article");
    article.className = "doc-body";
    if (en.entryKind === "item") {{
      const rec = ROOM.items[en.docId] || {{}};
      const h = document.createElement("h2"); h.textContent = rec.title || ""; article.appendChild(h);
      const state = document.createElement("p"); state.className = "item-state";
      state.textContent = "State: " + (rec.state || "");
      article.appendChild(state);
      const spec = document.createElement("div");
      spec.innerHTML = md(rec.specification || "");
      article.appendChild(spec);
      if (rec.latestActivity) {{
        const act = document.createElement("div"); act.className = "item-activity";
        const who = document.createElement("p"); who.className = "item-activity-who";
        who.textContent = "Latest activity — " + rec.latestActivity.actor;
        act.appendChild(who);
        const activityBody = document.createElement("div");
        activityBody.innerHTML = md(rec.latestActivity.body || "");
        act.appendChild(activityBody);
        article.appendChild(act);
      }}
    }} else if (en.entryKind === "signal") {{
      const rec = ROOM.signals[en.docId] || {{}};
      const body = document.createElement("div");
      body.innerHTML = md(rec.body || "");
      article.appendChild(body);
      const meta = document.createElement("p"); meta.className = "signal-meta";
      meta.textContent = (rec.source || "") + (rec.date ? " — " + rec.date : "");
      article.appendChild(meta);
    }} else {{
      const doc = ROOM.docs[en.docId] || {{}};
      article.innerHTML = md(doc.body);
    }}
    main.appendChild(article);
  }}
  ROOM.sections.forEach(function(sec) {{
    const wrap = document.createElement("div");
    wrap.className = "room-section";
    if (sec.name) {{ const h = document.createElement("h4"); h.textContent = sec.name; wrap.appendChild(h); }}
    if (sec.standing && sec.noteHtml) {{
      const note = document.createElement("p");
      note.className = "room-section-note";
      note.innerHTML = sec.noteHtml;
      wrap.appendChild(note);
    }}
    sec.entries.forEach(function(en) {{
      const btn = document.createElement("button");
      btn.className = "room-entry"; btn.type = "button";
      const pin = en.pinned ? '<span class="re-pin" aria-hidden="true">\\u2605 </span>' : "";
      const noteHtml = en.note ? '<span class="re-note"></span>' : "";
      btn.innerHTML = pin + "<span></span>" + noteHtml;
      btn.querySelector("span:not(.re-pin)").textContent = en.title;
      if (en.note) btn.querySelector(".re-note").textContent = en.note;
      btn.addEventListener("click", function() {{
        document.querySelectorAll(".room-entry.on").forEach(function(b){{ b.classList.remove("on"); }});
        btn.classList.add("on");
        renderEntry(en);
        main.scrollIntoView({{block: "start", behavior: "smooth"}});
      }});
      wrap.appendChild(btn);
      if (!firstBtn) firstBtn = btn;
    }});
    side.appendChild(wrap);
  }});
  if (firstBtn) firstBtn.click();
}})();
</script>
<script>
(function() {{
  const form = document.getElementById("roomAsk");
  const input = document.getElementById("roomAskInput");
  const btn = document.getElementById("roomAskBtn");
  const reply = document.getElementById("roomAskReply");
  const REPLY_KEY = "room-ask-reply";

  // Restore a reply stashed just before the reload below — a reload is the
  // only way the page can show the applied change, but the prose in `data.reply`
  // is the one place that change is stated, so it has to survive the trip.
  // sessionStorage can throw in a private window, and a reply lost to that
  // exception is the same defect this restores from.
  try {{
    const stashed = sessionStorage.getItem(REPLY_KEY);
    if (stashed) {{
      sessionStorage.removeItem(REPLY_KEY);
      reply.hidden = false;
      reply.textContent = stashed;
    }}
  }} catch (err) {{ /* private window or storage disabled — nothing to restore */ }}

  form.addEventListener("submit", async function(e) {{
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    btn.disabled = true; input.disabled = true;
    reply.hidden = false; reply.textContent = "Working…";
    try {{
      const r = await fetch("/api/room/requests", {{
        method: "POST", credentials: "same-origin",
        headers: {{"content-type": "application/json"}},
        body: JSON.stringify({{request: text}}),
      }});
      const data = await r.json().catch(function() {{ return {{}}; }});
      const replyText = data.reply || (r.ok ? "Done." : "Sorry — that didn't work.");
      reply.textContent = replyText;
      if (r.ok && data.status === "applied") {{
        // Reload so the room reflects the change (server re-renders grant ∩
        // curation). Stash the reply first so it survives the reload instead
        // of being destroyed 700ms after it appears.
        try {{ sessionStorage.setItem(REPLY_KEY, replyText); }} catch (err) {{ /* ignore */ }}
        setTimeout(function() {{ location.reload(); }}, 700);
        return;
      }}
    }} catch (err) {{
      reply.textContent = "Sorry — that didn't work. Please try again.";
    }}
    btn.disabled = false; input.disabled = false; input.value = "";
  }});
}})();
</script>"""

    return ds.page_shell(
        title=room.title or "Reading Room",
        nav_html=ds.nav_component("/room", tier),
        body=body,
        extra_css=_PAGE_CSS,
    )
