"""Reading Room tests (ARCH-357).

The load-bearing property is that a render is always grant ∩ curation: a curated
entry pointing at a doc outside the member's live entitlement must never place that
doc's body in the response, even when the entry was seeded while the doc WAS in
grant and the grant later narrowed. That render-side wall is tested head-on, plus
the entitlement rule (full sees every team doc; a contractor sees only
contractor-audience docs in their product scope), the admin-only/human-only/CSRF
provisioning API with write-time entitlement validation, and the retirement
redirects (Orientation / Reference Docs / Briefs → /room; /reference survives).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import access_identity
import api_common
import authz
import control_db
import kb_index
import library_fake
import orient_time
import server as srv
from control_db import new_room_id, session
from models import ReadingRoom, RoomEntry

AGENT_TOKEN = "agent-secret-xyz"

# Distinct body markers so a leak is unambiguous in the rendered HTML.
BODY_A = "AAA_vivy_contractor_visible_AAA"
BODY_B = "BBB_vivy_team_only_BBB"
BODY_C = "CCC_constance_contractor_CCC"
BODY_BRIEF_OLD = "brief-body-july-fourth"
BODY_BRIEF_NEW = "brief-body-july-eleventh"
BODY_BRIEF_HIDDEN = "brief-body-operator-audience-only"
BODY_BRIEF_ANCIENT = "brief-body-ancient-outage-postmortem"
BODY_BRIEF_UNDATED = "brief-body-welcome-aboard-no-date"
BODY_BRIEF_CONTRACTOR = "brief-body-vivy-contractor-outage-briefing"

# The room_build window test fixture freezes "now" here (client fixture below):
# 8 days after the OLD brief and 1 day after the NEW one, so both stay inside
# the 14-day window and every pre-existing brief assertion is unaffected by
# adding the window. The ancient brief (2026-05-01) is ~72 days before this and
# exists to test the boundary that DOES drop something.
_FROZEN_NOW = datetime(2026, 7, 12, 12, 0, tzinfo=orient_time.MST)


def _seed_docs():
    """The same corpus the old `_seed_index` built directly in sqlite, now
    seeded through `library_fake` (issue_01M1MFM06D7EK15RV5QB57PCVK) in the
    shape `library_client.list_reference_documents`/`get_reference_document`
    serve — three team-audience docs:
      A — vivy, team+contractor  → a vivy contractor may see it
      B — vivy, team only        → NOT contractor-visible
      C — constance, team+contractor → out of a vivy contractor's product scope
    plus the Briefs fixtures (ARCH-372): two dates so newest-first is
    assertable, one audience: [operator] brief to pin the historical failure, and
    one aged well outside the 14-day window to prove it actually drops
    something.
    """
    library_fake.seed_reference_doc(
        id="vivy/a", project="vivy", kind="runbook", title="Doc A",
        status="current", body=BODY_A, audience=["team", "contractor"])
    library_fake.seed_reference_doc(
        id="vivy/b", project="vivy", kind="runbook", title="Doc B",
        status="current", body=BODY_B, audience=["team"])
    library_fake.seed_reference_doc(
        id="constance/c", project="constance", kind="runbook", title="Doc C",
        status="current", body=BODY_C, audience=["team", "contractor"])
    library_fake.seed_reference_doc(
        id="platform/briefs/ai-engineering-briefing-2026-07-04", project="platform",
        kind="reference", title="Eng brief Jul 4", status="current",
        body=BODY_BRIEF_OLD, audience=["team"])
    library_fake.seed_reference_doc(
        id="company/briefs/ai-transformation-briefing-2026-07-11", project="company",
        kind="reference", title="Transformation brief Jul 11", status="current",
        body=BODY_BRIEF_NEW, audience=["team"])
    library_fake.seed_reference_doc(
        id="platform/briefs/legacy-operator-only-2026-06-01", project="platform",
        kind="reference", title="Legacy operator-only brief", status="current",
        body=BODY_BRIEF_HIDDEN, audience=["operator"])
    # A brief well outside the 14-day window (frozen "now" is 2026-07-12).
    library_fake.seed_reference_doc(
        id="platform/briefs/ancient-outage-postmortem-2026-05-01", project="platform",
        kind="reference", title="Ancient outage postmortem", status="current",
        body=BODY_BRIEF_ANCIENT, audience=["team"])
    # No trailing -YYYY-MM-DD — proves an undated id can't be shown to be
    # old, so it stays visible rather than raising or dropping out.
    library_fake.seed_reference_doc(
        id="platform/briefs/welcome-aboard", project="platform",
        kind="reference", title="Welcome aboard", status="current",
        body=BODY_BRIEF_UNDATED, audience=["team"])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()

    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "mem@example.com": {"role": "member", "products": ["vivy"]},
        "con@example.com": {"role": "contractor", "products": ["vivy"]},
    }))
    monkeypatch.setattr(authz, "ROLES", roles)

    # tiers: only operator is full; mem + con are contractor tier (tier is distinct
    # from role — mem is a "member" by role but not full-tier here).
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps({"full": ["operator@example.com"]}))
    monkeypatch.setattr(access_identity, "TIERS", tiers)

    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({AGENT_TOKEN: {"name": "an-agent", "products": ["*"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)

    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)

    library_fake.patch(monkeypatch)
    # The project scope now comes from the Library (E19·1,
    # issue_01M35K54QHJYYPXWS28YWEHJP3), never from roles.json's `products` —
    # seeded here to match, for every email this file acts as or provisions
    # a room for.
    library_fake.seed_person_scope("operator@example.com", {"*"})
    library_fake.seed_person_scope("mem@example.com", {"vivy"})
    library_fake.seed_person_scope("con@example.com", {"vivy"})
    _seed_docs()

    # Freeze the Briefs window's notion of "now" so the whole suite is
    # deterministic regardless of the wall-clock date it runs on.
    monkeypatch.setattr(orient_time, "now", lambda: _FROZEN_NOW)

    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


def _human(email, origin="http://testserver", **extra):
    h = {srv.EMAIL_HEADER: email, "Origin": origin}
    h.update(extra)
    return h


# --- entitlement --------------------------------------------------------------


def test_full_tier_sees_every_team_doc(client):
    grant = kb_index.room_entitlement("operator@example.com", frozenset({"*"}))
    # The team-audience briefs are all in the grant, ancient and undated ones
    # included — entitlement is about audience, not age; the audience: [operator]
    # one is NOT — which is the whole reason briefs were invisible before
    # ARCH-372. The window that keeps only recent ones out of the standing
    # Briefs section is a room_build render-time rule, not a grant narrowing.
    assert grant == {"vivy/a", "vivy/b", "constance/c",
                     "platform/briefs/ai-engineering-briefing-2026-07-04",
                     "company/briefs/ai-transformation-briefing-2026-07-11",
                     "platform/briefs/ancient-outage-postmortem-2026-05-01",
                     "platform/briefs/welcome-aboard"}


def test_contractor_scoped_to_contractor_audience_and_product(client):
    # A vivy contractor: A only. B is team-only; C is out of the vivy scope.
    grant = kb_index.room_entitlement("con@example.com", frozenset({"vivy"}))
    assert grant == {"vivy/a"}


def test_room_entitlement_never_fetches_a_body(client, monkeypatch):
    """`room_entitlement` reduces its result to a set of ids, so it must route
    through the metadata-only path (`team_doc_meta`), never `team_docs`'s
    per-doc body fetch — the waste `room_api.py`'s create-room and add-entries
    routes were paying on every call before this fix (PR #264 round 2
    review). Failing `get_reference_document` outright proves the id-only
    path never calls it, rather than merely observing the right ids come
    back for the wrong reason."""
    import library_client

    def must_not_be_called(doc_id, **kw):
        raise AssertionError(f"room_entitlement fetched a body for {doc_id}")

    monkeypatch.setattr(library_client, "get_reference_document", must_not_be_called)
    grant = kb_index.room_entitlement("operator@example.com", frozenset({"*"}))
    assert grant == {"vivy/a", "vivy/b", "constance/c",
                     "platform/briefs/ai-engineering-briefing-2026-07-04",
                     "company/briefs/ai-transformation-briefing-2026-07-11",
                     "platform/briefs/ancient-outage-postmortem-2026-05-01",
                     "platform/briefs/welcome-aboard"}


def test_team_docs_filters_status_itself_not_just_via_the_fake(client, monkeypatch):
    """The two tests above pass even with NO status filter in `team_docs`,
    because `library_fake` drops non-current/draft rows itself
    (`library_fake.py:400`) the way the Library's route does — so they pin
    the fake, not this module. This one hands `team_docs` a retired row
    directly, bypassing that filter, so it fails if `_STATUSES` is ever
    dropped from the comprehension (`project_build`'s identical test, PR #263
    review — a widened server default must not silently widen a room's
    grant, with no error and no failing test)."""
    import library_client

    retired = library_client.ReferenceDocument(
        id="vivy/retired", source_ref="projects/vivy/runbook/retired.md",
        kind="runbook", project="vivy", title="Retired runbook", status="archived",
        topics=[], audience=["team"], last_reviewed=None, updated_at=None,
        body="retired body")
    monkeypatch.setattr(library_client, "list_reference_documents",
                        lambda **kw: [retired])
    # `team_docs` must never reach the per-doc fetch for a status it excludes
    # itself — if it did, this doc (which exists and IS entitled by audience)
    # would come back and the assertion below would pass for the wrong
    # reason, the same false-positive shape `project_build`'s equivalent test
    # warns against.
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **kw: retired if doc_id == "vivy/retired" else None)
    assert kb_index.team_docs() == []


def test_team_docs_validates_status_on_the_fetched_copy_not_the_listing(client, monkeypatch):
    """The listing and the per-doc fetch are two separate network calls, so a
    document can change status in the window between them — the old sqlite
    read this module replaced was one atomic row read, so status and body
    could never disagree; the two-step split opened this window. `meta`
    (the listing snapshot) says "current", passing the cheap pre-fetch skip;
    `full` (the copy actually served) says "archived". The row served must
    come from `full`, never from the stale `meta` snapshot, or a document
    that moved out of `_STATUSES` gets served anyway carrying its excluded
    status (PR #264 round 2 review)."""
    import library_client

    meta = library_client.ReferenceDocument(
        id="vivy/moved", source_ref="projects/vivy/runbook/moved.md",
        kind="runbook", project="vivy", title="Moved runbook", status="current",
        topics=[], audience=["team"], last_reviewed=None, updated_at=None)
    full = library_client.ReferenceDocument(
        id="vivy/moved", source_ref="projects/vivy/runbook/moved.md",
        kind="runbook", project="vivy", title="Moved runbook", status="archived",
        topics=[], audience=["team"], last_reviewed=None, updated_at=None,
        body="moved body")
    monkeypatch.setattr(library_client, "list_reference_documents",
                        lambda **kw: [meta])
    monkeypatch.setattr(library_client, "get_reference_document",
                        lambda doc_id, **kw: full if doc_id == "vivy/moved" else None)
    assert kb_index.team_docs() == []


def test_team_docs_drops_a_doc_that_vanishes_between_the_list_and_the_fetch(client, monkeypatch):
    """Adapted from `build.load_docs()`'s identical branch
    (`tests/test_build.py`, `test_load_docs_drops_a_doc_that_vanishes_between_the_list_and_the_fetch`,
    added after a reviewer caught the same gap on PR #262) — `team_docs()`
    has the same two-step list/fetch split and the same drop, unpinned here
    until now (PR #264 round 2 review). A doc can be deleted or re-tagged in
    the window between the listing and its own per-doc fetch; that row is
    dropped and the rest of the grant still builds."""
    import library_client

    listed = library_client.get_reference_document

    def _gone_for_one(doc_id, **kw):
        return None if doc_id == "vivy/b" else listed(doc_id, **kw)

    monkeypatch.setattr(library_client, "get_reference_document", _gone_for_one)
    ids = {d["id"] for d in kb_index.team_docs()}
    assert "vivy/b" not in ids
    assert "vivy/a" in ids and "constance/c" in ids


def test_team_docs_raises_on_a_library_failure(client, monkeypatch):
    """The old sqlite read returned `[]` for a missing/locked index; the
    Library read must not collapse the same way — an unreachable Library and
    an empty grant are different facts (module docstring), so this must
    raise rather than degrade to an empty list."""
    import library_client

    def boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_reference_documents", boom)
    with pytest.raises(RuntimeError):
        kb_index.team_docs()
    with pytest.raises(RuntimeError):
        kb_index.entitled_docs("operator@example.com", frozenset({"*"}))
    with pytest.raises(RuntimeError):
        kb_index.room_entitlement("operator@example.com", frozenset({"*"}))


def test_room_render_500s_on_a_library_failure_not_an_empty_room(client, monkeypatch):
    """`/room` has deliberately no static fallback (room_build's module
    docstring) — a render failure must surface as a plain 500 (server.py's
    generic catch), never as an empty or stub room, which would read to a
    member as "you have nothing" rather than "we couldn't check"."""
    import library_client

    _make_room("operator@example.com", ["vivy/a"])

    def boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_reference_documents", boom)
    r = client.get("/room", headers=_human("operator@example.com"))
    assert r.status_code == 500


def test_room_write_500s_on_a_library_failure_not_a_partial_write(client, monkeypatch):
    """`room_api.py`'s write routes call `room_entitlement`/`entitled_docs` to
    validate every entry before writing. A Library failure there must not be
    read as "this member is entitled to nothing" — which `_clean_entries`
    would then reject every entry against, indistinguishable from a real
    empty grant — so it must raise past `_clean_entries` entirely and let
    `api_common.api_guard` turn it into a terse 500, with no room created and
    no row written."""
    import library_client

    def boom(**kw):
        raise RuntimeError("Library unreachable")

    monkeypatch.setattr(library_client, "list_reference_documents", boom)
    r = _create_room(client, _human("operator@example.com"))
    assert r.status_code == 500
    with session() as s:
        from sqlalchemy import select as _select
        assert s.scalar(_select(ReadingRoom).where(
            ReadingRoom.email == "con@example.com")) is None


# --- the render wall: grant ∩ curation ---------------------------------------


def _make_room(email, doc_ids):
    with session() as s:
        room = ReadingRoom(id=new_room_id(), email=email, title="Room",
                           landing_note="", created_by="operator@example.com")
        for i, d in enumerate(doc_ids):
            room.entries.append(RoomEntry(doc_id=d, section="Docs", position=i,
                                          source="provision", added_by="operator@example.com"))
        s.add(room)


def test_render_drops_out_of_grant_entries(client):
    # Seed a contractor's room with entries for A, B AND C directly (as if the
    # grant was wider at seed time, then narrowed). Only A is currently entitled.
    _make_room("con@example.com", ["vivy/a", "vivy/b", "constance/c"])
    r = client.get("/room", headers=_human("con@example.com"))
    assert r.status_code == 200
    html = r.text
    assert BODY_A in html                 # entitled → body embedded
    assert BODY_B not in html             # team-only → never reaches the page
    assert BODY_C not in html             # out-of-scope product → never reaches the page


def test_sections_render_in_curation_order_not_alphabetical(client):
    # Insert "Zeta" before "Alpha": alphabetical order would flip them, curation
    # (insertion) order keeps Zeta first — so "Start here" can lead a real room.
    import json
    import re as _re
    with session() as s:
        room = ReadingRoom(id=new_room_id(), email="operator@example.com", title="R",
                           landing_note="", created_by="operator@example.com")
        room.entries.append(RoomEntry(doc_id="vivy/a", section="Zeta", position=0,
                                      source="provision", added_by="operator@example.com"))
        room.entries.append(RoomEntry(doc_id="vivy/b", section="Alpha", position=0,
                                      source="provision", added_by="operator@example.com"))
        s.add(room)
    r = client.get("/room", headers=_human("operator@example.com"))
    assert r.status_code == 200
    m = _re.search(r"const ROOM = (\{.*?\});\nfunction md", r.text, _re.S)
    payload = json.loads(m.group(1))
    # Curated sections keep insertion order; the standing Briefs section is
    # appended after them, so curation still leads (a "Start here" stays first).
    assert [sec["name"] for sec in payload["sections"]] == ["Zeta", "Alpha", "Briefs"]


def test_full_member_room_renders_entitled_body(client):
    _make_room("operator@example.com", ["vivy/b"])
    r = client.get("/room", headers=_human("operator@example.com"))
    assert r.status_code == 200
    assert BODY_B in r.text               # full tier is entitled to a team-only doc


def test_room_offers_no_edit_affordance(client):
    """The Reading Room stays a reading surface even for a full-tier member
    with a provisioned room (issue_01M17HYDBQ4GPWHPFS56M6QQPH decided editing
    lives on /reference/doc/{id} only) — asserted so a later change can't add
    one silently. Covers reference_doc_build._editor_section's own markers
    (kb-editor / __CAN_KB_WRITE__ / the kb_write_api endpoints) and the
    retired Typora bridge alike."""
    _make_room("operator@example.com", ["vivy/b"])
    r = client.get("/room", headers=_human("operator@example.com"))
    assert r.status_code == 200
    html = r.text
    for marker in ("kb-editor", "__CAN_KB_WRITE__", "/api/kb/documents/",
                    "kbEditBtn", "OPEN_BASE", "localhost:8765", "Typora"):
        assert marker not in html, marker


# --- the ask-form reply must survive the reload (issue_01M1745W7M2MWTCX5G6RNWCE30)
# On an applied change the server re-renders grant ∩ curation, so a reload is
# correct and stays — but `data.reply` is the only place the applied change is
# stated in prose, and it used to be destroyed 700ms after it appeared. This
# runs the page's actual ask-form JS in node (extracted from the real rendered
# room, not retyped) to prove the reply is still readable after the reload,
# not merely present in the instant before it fires.

def _extract_ask_form_js(html):
    start_marker = 'const form = document.getElementById("roomAsk")'
    idx = html.index(start_marker)
    iife_start = html.rindex("(function() {", 0, idx)
    end = html.index("})();", idx) + len("})();")
    return html[iife_start:end]


def _run_ask_form_js(js_src, driver_js, *, fail_get=False, fail_set=False):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to exercise the ask-form JS")
    full = f"""
'use strict';
const IIFE_SRC = {json.dumps(js_src)};

class Storage {{
  constructor() {{ this.map = new Map(); this.failGet = {json.dumps(fail_get)}; this.failSet = {json.dumps(fail_set)}; }}
  getItem(k) {{ if (this.failGet) throw new Error('blocked'); return this.map.has(k) ? this.map.get(k) : null; }}
  setItem(k, v) {{ if (this.failSet) throw new Error('blocked'); this.map.set(k, v); }}
  removeItem(k) {{ this.map.delete(k); }}
}}
global.sessionStorage = new Storage();
global.reloaded = false;
global.location = {{ reload() {{ global.reloaded = true; }} }};
global.setTimeout = (fn) => fn();   // run the 700ms reload delay synchronously

function makeReply() {{ return {{ hidden: true, textContent: '' }}; }}
function makeInput() {{ return {{ value: 'add the deployment runbook', disabled: false }}; }}
function makeBtn() {{ return {{ disabled: false }}; }}
function makeForm() {{
  return {{ _submitHandler: null, addEventListener(t, fn) {{ if (t === 'submit') this._submitHandler = fn; }} }};
}}
function newPage() {{
  const els = {{
    roomAsk: makeForm(), roomAskInput: makeInput(), roomAskBtn: makeBtn(), roomAskReply: makeReply(),
  }};
  global.document = {{ getElementById: (id) => els[id] }};
  eval(IIFE_SRC);
  return els;
}}

{driver_js}
"""
    r = subprocess.run([node, "-e", full], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"ask-form JS harness failed:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_room_ask_reply_survives_reload(client):
    _make_room("operator@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("operator@example.com"))
    js_src = _extract_ask_form_js(r.text)
    result = _run_ask_form_js(js_src, r"""
global.fetch = () => Promise.resolve({
  ok: true,
  json: async () => ({ reply: 'Added the deployment runbook.', status: 'applied' }),
});
const page1 = newPage();
(async () => {
  await page1.roomAsk._submitHandler({ preventDefault(){} });
  const page2 = newPage();   // a fresh page load, as the reload produces
  console.log(JSON.stringify({
    reloaded: global.reloaded,
    replyAfterReloadOnFreshPage: page2.roomAskReply.textContent,
    replyHiddenOnFreshPage: page2.roomAskReply.hidden,
  }));
})();
""")
    assert result["reloaded"], "sanity: the applied path must still reload"
    # The regression this issue exists to catch: asserting textContent right
    # after submit (before the reload runs) passes on the broken code too —
    # the reply has to be checked on the page the reload PRODUCES.
    assert result["replyAfterReloadOnFreshPage"] == "Added the deployment runbook.", (
        "the reply must be readable on the page the reload lands on, not just "
        "in the instant before it fires")
    assert result["replyHiddenOnFreshPage"] is False


def test_room_ask_reply_survives_reload_even_if_sessionstorage_throws(client):
    """A private window can throw on sessionStorage access — the stash must
    not crash the handler, and the reload (the correct, load-bearing part of
    this path) must still happen even though the reply itself is lost."""
    _make_room("operator@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("operator@example.com"))
    js_src = _extract_ask_form_js(r.text)
    result = _run_ask_form_js(js_src, r"""
global.fetch = () => Promise.resolve({
  ok: true,
  json: async () => ({ reply: 'Added the deployment runbook.', status: 'applied' }),
});
const page1 = newPage();
(async () => {
  await page1.roomAsk._submitHandler({ preventDefault(){} });
  console.log(JSON.stringify({
    reloaded: global.reloaded,
    replyShownBeforeReload: page1.roomAskReply.textContent,
  }));
})();
""", fail_set=True)
    assert result["reloaded"], "a storage failure must not block the reload"
    assert result["replyShownBeforeReload"] == "Added the deployment runbook."


def test_room_ask_reply_restore_survives_sessionstorage_throw_on_read(client):
    """The restore guard (room_build.py) is the try/catch around
    sessionStorage.getItem/removeItem added so a private window can't lose
    the reply on the way BACK from the reload — the mirror of the stash-side
    throw covered above. Plants the throw on getItem instead of setItem, so
    the stash succeeds but reading it back on the reloaded page fails."""
    _make_room("operator@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("operator@example.com"))
    js_src = _extract_ask_form_js(r.text)
    result = _run_ask_form_js(js_src, r"""
global.fetch = () => Promise.resolve({
  ok: true,
  json: async () => ({ reply: 'Added the deployment runbook.', status: 'applied' }),
});
const page1 = newPage();
(async () => {
  await page1.roomAsk._submitHandler({ preventDefault(){} });
  const page2 = newPage();   // a fresh page load, as the reload produces
  console.log(JSON.stringify({
    reloaded: global.reloaded,
    replyAfterReloadOnFreshPage: page2.roomAskReply.textContent,
    replyHiddenOnFreshPage: page2.roomAskReply.hidden,
  }));
})();
""", fail_get=True)
    assert result["reloaded"], "a read-side storage failure must not block the reload"
    # No exception escaped the restore guard (the harness would have failed
    # with a nonzero exit otherwise) — the reply is simply absent, not the
    # page broken.
    assert result["replyAfterReloadOnFreshPage"] == ""
    assert result["replyHiddenOnFreshPage"] is True


def test_room_ask_reply_not_stashed_when_the_request_was_not_applied(client):
    """The asymmetry this issue calls out: a non-applied reply already
    persists (no reload happens on that path), so the stash exists only to
    close the gap on the applied path. Confirms it isn't written otherwise,
    and that the reply is still shown directly (no reload, no stash needed)."""
    _make_room("operator@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("operator@example.com"))
    js_src = _extract_ask_form_js(r.text)
    result = _run_ask_form_js(js_src, r"""
global.fetch = () => Promise.resolve({
  ok: true,
  json: async () => ({ reply: 'That request is out of scope.', status: 'rejected' }),
});
const page1 = newPage();
(async () => {
  await page1.roomAsk._submitHandler({ preventDefault(){} });
  console.log(JSON.stringify({
    reloaded: global.reloaded,
    reply: page1.roomAskReply.textContent,
    stashed: sessionStorage.map.get('room-ask-reply') || null,
  }));
})();
""")
    assert result["reloaded"] is False
    assert result["reply"] == "That request is out of scope."
    assert result["stashed"] is None


def test_no_room_shows_stub(client):
    r = client.get("/room", headers=_human("mem@example.com"))
    assert r.status_code == 200
    assert "don't have a Reading Room yet" in r.text
    assert "Mem's Reading Room" in r.text            # heading personalized to the viewer
    assert "Ask the operator to set one up" in r.text     # the admin who provisions
    assert "the steward" not in r.text               # not the agentic role


# --- the blog-drafts panel moved to the Writing Desk ---------------------------
# (epic issue_01M0EHQHQ7M39K811XKX1BAG98). See tests/test_writing_desk_build.py
# for the __CAN_BLOG_WRITE__ gate tests that used to live here, retargeted at
# /writing-desk.


def test_room_no_longer_renders_the_blog_panel(client):
    # Checked on both a provisioned room (operator) and the no-room stub (mem) — the
    # panel used to render on both before it moved.
    for email in ("operator@example.com", "mem@example.com"):
        r = client.get("/room", headers=_human(email))
        assert r.status_code == 200
        assert "room-blog" not in r.text, email
        assert "__CAN_BLOG_WRITE__" not in r.text, email


# --- provisioning API: admin-only, human-only, CSRF, entitlement-validated ----


def _create_room(client, headers, email="con@example.com", entries=None):
    body = {"email": email, "title": "Con's Room",
            "entries": entries if entries is not None else [{"doc_id": "vivy/a"}]}
    return client.post("/api/rooms", json=body, headers=headers)


def test_admin_creates_room(client):
    r = _create_room(client, _human("operator@example.com"))
    assert r.status_code == 201
    assert r.json()["email"] == "con@example.com"
    assert [e["doc_id"] for e in r.json()["entries"]] == ["vivy/a"]


def test_create_rejects_doc_outside_target_entitlement(client):
    # Seeding con with a team-only doc (B) they cannot see → 400, not a silent add.
    r = _create_room(client, _human("operator@example.com"), entries=[{"doc_id": "vivy/b"}])
    assert r.status_code == 400
    assert "entitlement" in r.json()["error"]


def test_create_rejects_boolean_position(client):
    # bool is an int subclass; a JSON `true` position must not slip through as 1.
    r = _create_room(client, _human("operator@example.com"),
                     entries=[{"doc_id": "vivy/a", "position": True}])
    assert r.status_code == 400


def test_non_admin_cannot_provision(client):
    r = _create_room(client, _human("mem@example.com"))
    assert r.status_code == 403


def test_agent_token_cannot_provision(client):
    r = _create_room(client, {"Authorization": f"Bearer {AGENT_TOKEN}"})
    assert r.status_code == 403


def test_csrf_blocks_cross_origin_create(client):
    r = _create_room(client, _human("operator@example.com", origin="http://evil.example"))
    assert r.status_code == 403


def test_duplicate_room_conflicts(client):
    assert _create_room(client, _human("operator@example.com")).status_code == 201
    assert _create_room(client, _human("operator@example.com")).status_code == 409


def test_add_entries_and_archive(client):
    room_id = _create_room(client, _human("operator@example.com")).json()["id"]
    # add is idempotent-ish: re-adding vivy/a updates it; a doc out of grant is 400.
    bad = client.post(f"/api/rooms/{room_id}/entries",
                      json={"entries": [{"doc_id": "constance/c"}]},
                      headers=_human("operator@example.com"))
    assert bad.status_code == 400
    arch = client.post(f"/api/rooms/{room_id}/archive", headers=_human("operator@example.com"))
    assert arch.status_code == 200 and arch.json()["status"] == "archived"


# --- retirement redirects -----------------------------------------------------


@pytest.mark.parametrize("path", [
    "/orientation", "/knowledge", "/reference-docs",
    "/reference-docs/system-design", "/reference-docs/taste-map",
    "/reference-docs/design", "/engineering/system-design",
])
def test_retired_surfaces_redirect_to_room(client, path):
    r = client.get(path, headers=_human("operator@example.com"))
    assert r.status_code == 302
    assert r.headers["location"] == "/room"


def test_briefs_redirect_to_room_now_that_the_room_renders_them(client):
    """ARCH-372 reverses this test, and the reason matters.

    The OLD version pinned "briefs must NOT go to /room" — correct at the time,
    because ARCH-357 had 302'd briefs into a Room that rendered nothing and the operator
    reported them lost. That happened three times, each fix aiming at the ROUTE.

    The route was never the bug. Every brief is written `audience: [operator]`, and
    kb_index.team_docs() skips anything without `team`, so briefs were absent
    from the entitled doc set entirely and no routing could surface them.

    Redirecting here is only correct because room_build now has a standing
    Briefs section AND briefs are team-audience. If either half is reverted,
    revert this too — a 302 into a Room that renders nothing is how briefs got
    lost three times.
    """
    for path in ("/engineering/briefs", "/memory/briefs"):
        r = client.get(path, headers=_human("operator@example.com"))
        assert r.status_code == 302, f"{path} -> {r.status_code}"
        assert r.headers["location"] == "/room", f"{path} -> {r.headers['location']}"


def test_retired_surface_redirects_for_contractor_too(client):
    # /room is both-tier, so a contractor's old Orientation link lands there, not a 404.
    r = client.get("/orientation", headers=_human("con@example.com"))
    assert r.status_code == 302
    assert r.headers["location"] == "/room"


# --- the standing Briefs section (ARCH-372) -----------------------------------
#
# The operator: briefs are narrative prose and belong in a reading room, undivided —
# engineering brief, AI Transformation brief, and whatever comes next. Three
# earlier attempts to put them here failed, and each aimed at the ROUTE. The
# cause was entitlement: every brief was written audience: [operator], which
# team_docs() skips, so briefs were never in the grant at all.
#
# These tests exist so the fourth attempt cannot regress into the third.


def _payload(text: str) -> dict:
    import json as _json
    import re as _re
    m = _re.search(r"const ROOM = (\{.*?\});\nfunction md", text, _re.S)
    assert m, "room payload not found"
    return _json.loads(m.group(1))


def test_briefs_render_without_any_curation_entry(client):
    """The whole point: a brief appears because it is entitled, not because
    someone remembered to curate this week's."""
    _make_room("operator@example.com", ["vivy/a"])          # curation mentions no brief
    r = client.get("/room", headers=_human("operator@example.com"))
    assert r.status_code == 200
    assert BODY_BRIEF_NEW in r.text and BODY_BRIEF_OLD in r.text
    names = [s["name"] for s in _payload(r.text)["sections"]]
    assert "Briefs" in names


def test_briefs_section_is_newest_first(client):
    _make_room("operator@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("operator@example.com"))
    briefs = next(s for s in _payload(r.text)["sections"] if s["name"] == "Briefs")
    ids = [e["docId"] for e in briefs["entries"]]
    # The undated brief sorts last (no date to rank it), and the ancient one
    # is outside the window so it never reaches this section at all.
    assert ids == ["company/briefs/ai-transformation-briefing-2026-07-11",
                   "platform/briefs/ai-engineering-briefing-2026-07-04",
                   "platform/briefs/welcome-aboard"]


def test_briefs_section_spans_kb_projects(client):
    """A brief's KB project tracks its SUBJECT (engineering practice vs the
    business); the Room is where both are read. The section must not be
    project-scoped or the transformation brief goes missing again."""
    _make_room("operator@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("operator@example.com"))
    briefs = next(s for s in _payload(r.text)["sections"] if s["name"] == "Briefs")
    projects = {e["docId"].split("/")[0] for e in briefs["entries"]}
    assert projects == {"platform", "company"}


def test_an_operator_audience_brief_stays_invisible(client):
    """The historical failure, pinned. audience: [operator] is skipped by team_docs(),
    so such a brief is outside the grant and the Briefs rule must not smuggle it
    in — the rule widens CURATION, never the grant."""
    _make_room("operator@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("operator@example.com"))
    assert BODY_BRIEF_HIDDEN not in r.text


def test_briefs_rule_respects_the_grant_for_a_contractor(client):
    """Both seeded briefs are team-only, so a contractor sees NO Briefs section.
    The rule draws from entitled_docs(email), so it cannot become a way in.
    With no entitled brief at all, there's nothing to point at either — no
    section and no Archives note."""
    _make_room("con@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("con@example.com"))
    assert r.status_code == 200
    assert BODY_BRIEF_NEW not in r.text and BODY_BRIEF_OLD not in r.text
    assert "Briefs" not in [s["name"] for s in _payload(r.text)["sections"]]
    assert not any(s.get("standing") for s in _payload(r.text)["sections"])
    assert "/reference/briefings" not in r.text


def test_a_curated_brief_is_not_shown_twice(client):
    """If a curator pins a brief into their own section, it belongs there and
    must not also appear under the standing Briefs heading."""
    _make_room("operator@example.com",
               ["company/briefs/ai-transformation-briefing-2026-07-11"])
    r = client.get("/room", headers=_human("operator@example.com"))
    payload = _payload(r.text)
    briefs = [s for s in payload["sections"] if s["name"] == "Briefs"]
    assert briefs, "the other brief should still form the section"
    ids = [e["docId"] for e in briefs[0]["entries"]]
    assert "company/briefs/ai-transformation-briefing-2026-07-11" not in ids
    assert ids == ["platform/briefs/ai-engineering-briefing-2026-07-04",
                   "platform/briefs/welcome-aboard"]


# --- windowing the standing Briefs section (issue_01M17W67YVZZC79NAYVSTRBKPB) --
#
# The Room's Briefs section used to show every entitled brief forever. It now
# shows roughly the last two weeks; the rest stay reachable at
# /reference/briefings. This must apply only to the STANDING rule, never to a
# curator's explicit placement, and must never 500 on a brief id with no date.


def test_brief_inside_the_window_renders(client):
    """Sanity: the NEW brief (1 day before the frozen 'now') is well inside
    the 14-day window and still renders — the window doesn't touch it."""
    _make_room("operator@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("operator@example.com"))
    assert r.status_code == 200
    assert BODY_BRIEF_NEW in r.text


def test_brief_outside_the_window_does_not_render(client):
    """The ancient brief (2026-05-01, ~72 days before the frozen 'now') is
    entitled — it's in the grant (test_full_tier_sees_every_team_doc) — but
    aged out of the standing Briefs section, so its body never reaches the
    page and it's absent from the payload."""
    _make_room("operator@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("operator@example.com"))
    assert r.status_code == 200
    assert BODY_BRIEF_ANCIENT not in r.text
    briefs = next(s for s in _payload(r.text)["sections"] if s["name"] == "Briefs")
    ids = [e["docId"] for e in briefs["entries"]]
    assert "platform/briefs/ancient-outage-postmortem-2026-05-01" not in ids


def test_curated_old_brief_still_renders_despite_the_window(client):
    """Curator intent outranks the window: a curator who explicitly placed the
    ancient brief in a section must still see it, and it renders in THAT
    section rather than under the standing Briefs heading (which, per the
    prior test, would have dropped it for age)."""
    _make_room("operator@example.com",
               ["platform/briefs/ancient-outage-postmortem-2026-05-01"])
    r = client.get("/room", headers=_human("operator@example.com"))
    assert r.status_code == 200
    assert BODY_BRIEF_ANCIENT in r.text
    payload = _payload(r.text)
    curated = next(s for s in payload["sections"] if s["name"] == "Docs")
    assert [e["docId"] for e in curated["entries"]] == \
        ["platform/briefs/ancient-outage-postmortem-2026-05-01"]
    briefs = next(s for s in payload["sections"] if s["name"] == "Briefs")
    assert "platform/briefs/ancient-outage-postmortem-2026-05-01" not in \
        [e["docId"] for e in briefs["entries"]]


def test_undated_brief_id_does_not_raise_and_stays_visible(client):
    """A brief id with no trailing -YYYY-MM-DD can't be shown to be old, and
    this render has no fallback (a 500 shows the member nothing), so it stays
    in the section rather than raising or being silently dropped."""
    _make_room("operator@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("operator@example.com"))
    assert r.status_code == 200
    assert BODY_BRIEF_UNDATED in r.text
    briefs = next(s for s in _payload(r.text)["sections"] if s["name"] == "Briefs")
    assert "platform/briefs/welcome-aboard" in [e["docId"] for e in briefs["entries"]]


def test_archives_line_present_when_briefs_section_renders(client):
    """The boundary must not be silent: whenever the Briefs section renders
    for a full-tier member, a line points a reader at the full archive,
    whether or not anything was actually hidden this time. operator is full tier,
    so this also covers "a full-tier member still gets the link"."""
    _make_room("operator@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("operator@example.com"))
    assert r.status_code == 200
    assert "/reference/briefings" in r.text
    assert "Older briefings live in" in r.text
    briefs = next(s for s in _payload(r.text)["sections"] if s.get("standing"))
    assert briefs["noteHtml"] == (
        'Older briefings live in <a href="/reference/briefings">Archives</a>.')


def test_contractor_with_a_contractor_brief_gets_the_section_not_the_archives_link(
        client, monkeypatch):
    """A vivy contractor entitled to a contractor-audience brief gets the
    standing Briefs section — the rule draws from entitled_docs(email), which
    includes it — but must not get the Archives note, since /reference/briefings
    is full-tier-only and the link would 404 for them. This is the case the
    fixture never exercised before: none of the seeded briefs were
    contractor-visible, so the finding was latent rather than live."""
    real_team_docs = kb_index.team_docs

    def patched():
        docs = real_team_docs()
        docs.append({
            "id": "vivy/briefs/vivy-outage-briefing-2026-07-10", "project": "vivy",
            "kind": "reference", "title": "Vivy outage briefing", "status": "current",
            "body": BODY_BRIEF_CONTRACTOR, "contractor": True,
        })
        return docs

    monkeypatch.setattr(kb_index, "team_docs", patched)
    _make_room("con@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("con@example.com"))
    assert r.status_code == 200
    assert BODY_BRIEF_CONTRACTOR in r.text
    briefs = next(s for s in _payload(r.text)["sections"] if s.get("standing"))
    assert briefs["entries"], "the contractor-visible brief should be in the section"
    assert "noteHtml" not in briefs
    assert "/reference/briefings" not in r.text


def test_contractor_whose_only_brief_is_aged_out_gets_no_section(client, monkeypatch):
    """Round-2 reviewer catch: a contractor entitled to a contractor-audience
    brief that's aged out of the window must get no Briefs section at all —
    not an empty one. Full tier can render an empty section because it always
    carries the Archives note explaining the gap; a contractor never gets that
    note (it points at a full-tier-only route), so an empty section for them
    would be a bare heading over nothing, with no explanation."""
    real_team_docs = kb_index.team_docs

    def patched():
        docs = real_team_docs()
        docs.append({
            "id": "vivy/briefs/vivy-outage-briefing-2026-05-01", "project": "vivy",
            "kind": "reference", "title": "Vivy outage briefing", "status": "current",
            "body": BODY_BRIEF_CONTRACTOR, "contractor": True,
        })
        return docs

    monkeypatch.setattr(kb_index, "team_docs", patched)
    _make_room("con@example.com", ["vivy/a"])
    r = client.get("/room", headers=_human("con@example.com"))
    assert r.status_code == 200
    assert BODY_BRIEF_CONTRACTOR not in r.text
    assert not any(s.get("standing") for s in _payload(r.text)["sections"])
    assert "Briefs" not in [s["name"] for s in _payload(r.text)["sections"]]
    assert "/reference/briefings" not in r.text


def test_full_tier_gets_the_note_even_when_every_brief_aged_out(client):
    """The note is nested inside the standing section, and used to vanish
    entirely once every entitled brief aged out of the window — exactly the
    moment a full-tier reader most needs to be told where they went. Curate
    every brief away so the only remaining standing-rule candidate is the
    ancient one, which the window excludes — leaving zero entries but a
    candidate still exists, so the section (and its note) still renders."""
    _make_room("operator@example.com", [
        "vivy/a",
        "platform/briefs/ai-engineering-briefing-2026-07-04",
        "company/briefs/ai-transformation-briefing-2026-07-11",
        "platform/briefs/welcome-aboard",
    ])
    r = client.get("/room", headers=_human("operator@example.com"))
    assert r.status_code == 200
    briefs = next(s for s in _payload(r.text)["sections"] if s.get("standing"))
    assert briefs["entries"] == []
    assert briefs["noteHtml"] == (
        'Older briefings live in <a href="/reference/briefings">Archives</a>.')
    assert "/reference/briefings" in r.text


def test_curated_section_named_briefs_does_not_pick_up_the_note(client):
    """Section names are curator-controlled free text (room_api.py) — a
    curator naming their own section "Briefs" must not pick up the standing
    rule's note. The client keys off the `standing` marker, not `sec.name`."""
    with session() as s:
        room = ReadingRoom(id=new_room_id(), email="operator@example.com", title="R",
                           landing_note="", created_by="operator@example.com")
        room.entries.append(RoomEntry(doc_id="vivy/a", section="Briefs", position=0,
                                      source="provision", added_by="operator@example.com"))
        s.add(room)
    r = client.get("/room", headers=_human("operator@example.com"))
    assert r.status_code == 200
    payload = _payload(r.text)
    named_briefs = [s for s in payload["sections"] if s["name"] == "Briefs"]
    # Two sections both named "Briefs": the curator's own and the standing rule's.
    assert len(named_briefs) == 2
    curated = next(s for s in named_briefs if not s.get("standing"))
    standing = next(s for s in named_briefs if s.get("standing"))
    assert "noteHtml" not in curated
    assert "noteHtml" in standing
