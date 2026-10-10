"""Engineering KB area (engineering_build.py, ARCH-194).

Invariants: the browse loads only human-browsable kinds from the index, marks the
`team`-audience docs as human + carries an edit ref for each doc; the rendered page
embeds the docs safely (no </script> break-out); Edit opens the shared editor in
the drawer, where the document is being read, rather than navigating to its own
page (issue_01M17HYDBQ4GPWHPFS56M6QQPH), withheld for a doc the write API would
refuse; closing the drawer checks the document back in; the
ARCH-log render surfaces the substrate decision log and degrades to a clear message
when the file is absent.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import engineering_build as E
import kb_editor
import library_client
import library_fake


def _seed_kb(monkeypatch, rows):
    """Put `rows` behind the Library's reference readers.

    Replaces the `index.db` this suite built in `tmp_path` and monkeypatched
    onto `engineering_build.DB` — that seam went away with the module's direct
    read (issue_01M1MFKD2KX9CPBVCQKJP1R003). `audience` is a real list here
    rather than the JSON string the index column held; a test exercising the
    non-list guard passes a bare string on purpose."""
    library_fake.patch(monkeypatch)
    for row in rows:
        aud = row.pop("audience", None)
        if isinstance(aud, str):
            try:
                aud = json.loads(aud)
            except Exception:
                pass          # a deliberately malformed value stays as it is
        library_fake.seed_reference_doc(audience=aud, **row)


def _row(**kw):
    d = {"id": "x/y", "project": "shared", "kind": "decision", "title": "T",
         "status": "current", "last_reviewed": "2026-06-01",
         "source_ref": "projects/shared/decisions/y.md", "audience": "[]",
         "body": "Body line.\n"}
    d.update(kw)
    return d


# --- load_docs ---------------------------------------------------------------

def test_load_docs_filters_to_visible_kinds(tmp_path, monkeypatch):
    _seed_kb(monkeypatch, [
        _row(id="a", kind="decision"),
        _row(id="b", kind="meeting-notes"),   # hidden noise kind
        _row(id="c", kind="legal"),            # hidden
        _row(id="d", kind="runbook"),
    ])
    kinds = {d["kind"] for d in E.load_docs()}
    assert kinds == {"decision", "runbook"}


def test_brief_left_the_canonical_browse(tmp_path, monkeypatch):
    # 2026-08-26: `brief` moved out of VISIBLE_KINDS to /reference/briefings
    # (Archives) — a doc of this kind must not reach the canonical KB browse
    # any more, even though it still indexes fine (regression guard for the
    # exact "brief" cycle described in VISIBLE_KINDS' own comment).
    _seed_kb(monkeypatch, [
        _row(id="a", kind="brief", title="AI Engineering Briefing"),
        _row(id="b", kind="decision"),
    ])
    kinds = {d["kind"] for d in E.load_docs()}
    assert kinds == {"decision"}
    assert "brief" not in E.VISIBLE_KINDS
    assert "brief" not in E.KIND_LABEL and "brief" not in E.KIND_TOOLTIP


def test_load_docs_excludes_non_current_status(tmp_path, monkeypatch):
    _seed_kb(monkeypatch, [
        _row(id="a", status="current"),
        _row(id="b", status="draft"),
        _row(id="c", status="archived"),       # excluded
        _row(id="e", status="superseded"),     # excluded
    ])
    assert {d["id"] for d in E.load_docs()} == {"a", "b"}


def test_load_docs_marks_human_audience_and_carries_ref(tmp_path, monkeypatch):
    _seed_kb(monkeypatch, [
        _row(id="h", audience='["team","llm"]', source_ref="projects/p/d.md"),
        _row(id="l", audience='["llm"]'),
    ])
    docs = {d["id"]: d for d in E.load_docs()}
    assert docs["h"]["human"] is True and docs["l"]["human"] is False
    assert docs["h"]["ref"] == "projects/p/d.md"


def test_load_docs_human_false_for_non_list_audience_substring_match(tmp_path, monkeypatch):
    """A bare-string `audience` containing "team" as a substring must not
    pass `"team" in aud` as a substring test — the same non-list-audience bug
    `library_client._entitled` guards against (PR #205 review); reproduced a
    third time here for `load_docs()` (PR #235 review)."""
    _seed_kb(monkeypatch, [_row(id="s", audience='"steam-team"')])
    docs = {d["id"]: d for d in E.load_docs()}
    assert docs["s"]["human"] is False


def test_load_docs_human_false_on_malformed_json_and_does_not_raise(tmp_path, monkeypatch):
    _seed_kb(monkeypatch, [_row(id="m", audience="not json")])
    docs = {d["id"]: d for d in E.load_docs()}
    assert docs["m"]["human"] is False


def test_load_docs_human_false_for_null_audience(tmp_path, monkeypatch):
    _seed_kb(monkeypatch, [_row(id="n", audience=None)])
    docs = {d["id"]: d for d in E.load_docs()}
    assert docs["n"]["human"] is False


def test_blurb_strips_markdown_and_skips_headings():
    body = "# Heading\n\n**Bold** intro with [a link](http://x) and `code`.\n"
    b = E._blurb(body)
    assert "Heading" not in b
    assert "Bold intro with a link and code." in b


def test_blurb_strips_html_comments():
    # A doc whose body opens with an HTML comment (e.g. an unsupported Notion
    # block marker) must not surface the comment as the excerpt.
    body = "<!-- unsupported notion block: file -->\nReal prose here.\n"
    b = E._blurb(body)
    assert "unsupported notion block" not in b
    assert "Real prose here." in b


def test_blurb_strips_multiline_html_comment():
    body = "<!--\nunsupported notion block: file\n-->\nActual content.\n"
    b = E._blurb(body)
    assert "unsupported" not in b
    assert "Actual content." in b


def test_blurb_empty_when_only_html_comments():
    # If all prose is inside HTML comments, the blurb returns empty string.
    body = "<!-- nothing here -->\n<!-- more nothing -->\n"
    assert E._blurb(body) == ""


# --- render (the browse page) ------------------------------------------------

def _docs():
    return [
        {"id": "shared/decisions/0001-foo", "project": "shared", "kind": "decision",
         "title": "Foo decision", "reflabel": "ADR-0001",
         "status": "current", "reviewed": "2026-06-01",
         "ref": "projects/shared/decisions/0001-foo.md", "human": True,
         "blurb": "A human-readable explainer.", "len": 120, "body": "## Foo\nshort\n"},
        {"id": "vivy/architecture/big", "project": "vivy", "kind": "architecture",
         "title": "Big arch", "reflabel": "big", "status": "draft", "reviewed": "", "ref": "",
         "human": False, "blurb": "no ref here", "len": 9000, "body": "long body\n"},
    ]


def test_render_has_browse_scaffold_and_edit_base():
    html = E.render(_docs())
    # Title is now "Engineering" (ARCH-219 rename; old "Convivy · Knowledge Base" gone)
    for marker in ('<title>Knowledge Base · Orient · Convivy Builder</title>', 'Human-readable docs', 'Browse by type',
                   'class="drawer"', 'ARCH platform decision log'):
        assert marker in html, f"missing {marker!r}"
    # Edit links to the document's own page (issue_01M17HYDBQ4GPWHPFS56M6QQPH)
    # rather than opening Typora over the SMB mount.
    assert "'/reference/doc/' + encodeURIComponent(d.id)" in html
    assert "if(!d.human) return null;" in html
    assert "OPEN_BASE" not in html
    assert "localhost:8765" not in html
    # the reference badge renders on rows and cards (ARCH-196)
    assert "esc(d.reflabel)" in html
    # the 2-level nav rail is present (Documents area highlighted, Dashboards via /overview)
    assert 'href="/reference" aria-current="page"' in html and 'href="/overview"' in html
    # fields interpolated into innerHTML are escaped (titles can carry <, &, quotes)
    assert "function esc(" in html
    assert "esc(d.title)" in html and "esc(d.blurb)" in html


def test_doc_href_targets_the_document_actually_read():
    """DoD (issue_01M17HYDBQ4GPWHPFS56M6QQPH): the permalink must land on the
    document a member was actually reading, not a neighbour — a single-document
    fixture can't catch an off-by-one, so this plants two team-audience
    documents and asserts each resolves to its own id.

    The Edit control no longer uses this: it opens the editor in the drawer, and
    `test_the_row_edit_button_edits_the_document_it_belongs_to` is the same
    off-by-one check against that path."""
    data = {
        "docs": [
            {"id": "shared/decisions/0001-foo", "project": "shared", "kind": "decision",
             "title": "Foo decision", "reflabel": "ADR-0001", "status": "current",
             "reviewed": "2026-06-01", "ref": "projects/shared/decisions/0001-foo.md",
             "human": True, "blurb": "b", "len": 10},
            {"id": "shared/decisions/0002-bar", "project": "shared", "kind": "decision",
             "title": "Bar decision", "reflabel": "ADR-0002", "status": "current",
             "reviewed": "2026-06-01", "ref": "projects/shared/decisions/0002-bar.md",
             "human": True, "blurb": "b", "len": 10},
        ],
        "projectLabel": {"shared": "Shared"}, "projectOrder": ["shared"],
        "kindOrder": ["decision"], "kindLabel": {"decision": "Decisions"},
        "kindTooltip": {}, "drawerThreshold": 3000,
    }
    result = _run_browse_js(data, r"""
console.log(JSON.stringify({ first: docHref(docs[0]), second: docHref(docs[1]) }));
""")
    assert result["first"] == "/reference/doc/shared%2Fdecisions%2F0001-foo"
    assert result["second"] == "/reference/doc/shared%2Fdecisions%2F0002-bar"
    assert result["first"] != result["second"], (
        "the second document's permalink must not resolve to the first's id")


def test_edit_and_open_withheld_for_non_team_audience_and_arch_docs():
    """A doc `/reference/doc/{id}` would 404 for must get no Open link — a
    link nothing can serve is worse than no link — and no Edit control either,
    since the write API refuses the same set for the same reason. That covers a non-team KB
    doc (skill/agent kinds are typically LLM-facing, not `team` audience) and
    an ARCH-log entry (kind 'arch'), which is parsed from a flat file at
    render time and was never a separate Library document — both are
    fixtured with human:false the way load_docs()/arch_docs() actually set
    it, rather than asserting on kind alone."""
    data = {
        "docs": [
            {"id": "vivy/skill/x", "project": "vivy", "kind": "skill",
             "title": "Skill doc", "reflabel": "skill-x", "status": "current",
             "reviewed": "", "ref": "", "human": False, "blurb": "b", "len": 10},
            {"id": "substrate/arch/arch-1", "project": "substrate", "kind": "arch",
             "title": "Arch entry", "reflabel": "ARCH-1", "status": "current",
             "reviewed": "", "ref": "substrate/decisions.md", "human": False,
             "blurb": "b", "len": 10},
            {"id": "vivy/decisions/0001-ok", "project": "vivy", "kind": "skill",
             "title": "Team doc", "reflabel": "ADR-0001", "status": "current",
             "reviewed": "", "ref": "", "human": True, "blurb": "b", "len": 10},
        ],
        "projectLabel": {"vivy": "Vivy", "substrate": "Substrate"},
        "projectOrder": ["vivy", "substrate"],
        "kindOrder": ["skill", "arch"],
        "kindLabel": {"skill": "Skills", "arch": "ARCH"},
        "kindTooltip": {}, "drawerThreshold": 3000,
        # WITHOUT THIS THE TEST WOULD PASS ON THE WRONG REASON. `canEdit` ANDs
        # the viewer's role with the document's audience, so a fixture missing
        # the role makes every document uneditable and the audience half of the
        # rule could be deleted with this test still green. The role is granted
        # here and `teamEdit` below is the positive control that proves it took.
        "canKbWrite": "true",
    }
    result = _run_browse_js(data, r"""
console.log(JSON.stringify({
  skillHref: docHref(docs[0]), archHref: docHref(docs[1]),
  skillEdit: canEdit(docs[0]), archEdit: canEdit(docs[1]),
  teamEdit: canEdit(docs[2]),
}));
""")
    assert result["skillHref"] is None
    assert result["archHref"] is None
    assert result["skillEdit"] is False, (
        "a non-team document must get no Edit control: the write API refuses it")
    assert result["archEdit"] is False, (
        "an ARCH-log entry is not a Library document, so nothing could check it out")
    assert result["teamEdit"] is True, (
        "positive control: with the same role granted, a team-audience document "
        "IS editable — without this the two assertions above would hold just as "
        "well if canEdit always returned false")


def test_render_browse_has_atelier_nav():
    """Browse page is on the Atelier design system: nav, tokens, no old palette."""
    html = E.render(_docs())
    # Atelier nav class present (ds.nav_component output)
    assert 'atelier-nav' in html
    # Engineering area is active in the nav
    assert 'aria-current="page"' in html
    # A ds token is present (from TOKENS block in page_shell)
    assert '--brand' in html
    # Dark-mode toggle is wired (data-theme attribute from THEME_TOGGLE_JS)
    assert 'data-theme' in html
    # Old palette tokens are gone
    assert '#fffcf8' not in html
    assert '--accent' not in html
    # Old 4-stop pink/purple gradient is gone
    assert '#ec4899' not in html
    assert '#a855f7' not in html
    # Old topnav class is gone
    assert 'class="topnav"' not in html


def test_render_escapes_script_in_doc_blurb():
    """`body` no longer rides in the payload (issue_01M0Y7GZED2THYETHCT8NPHHDS),
    but `blurb` still does — the payload-wide `</` escape must still guard it."""
    docs = _docs()
    docs[0]["blurb"] = "before </script><script>alert(1)</script> after"
    html = E.render(docs)
    # the raw closing tag must be escaped inside the JSON payload so it can't
    # break out of the <script id="data"> block.
    assert "</script><script>alert(1)" not in html
    assert "<\\/script>" in html


def test_render_drops_body_from_payload():
    """The cut this issue is about: `body` was 94% of a 10.5 MB page across
    1158 rows, and no longer rides in the payload at all — the drawer and
    inline expansion fetch it on demand from `/api/memory/documents/{id}/body`
    instead. A body containing content found nowhere else on the page must not
    appear in the rendered HTML."""
    docs = _docs()
    docs[0]["body"] = "UNIQUE_BODY_MARKER_NOT_ELSEWHERE_ON_PAGE"
    html = E.render(docs)
    assert "UNIQUE_BODY_MARKER_NOT_ELSEWHERE_ON_PAGE" not in html
    assert '"body":' not in html
    # `blurb` and `len` stay — the row excerpt and the drawer threshold
    # (`clickRow` reads `d.len`, not the body) both still need them.
    assert '"blurb"' in html
    assert '"len"' in html


def test_browse_sanitizes_doc_body_before_innerhtml():
    """ARCH-244: every marked.parse() output is DOMPurify-sanitized, and the
    DOMPurify library is inlined, so a <script> in a fetched doc body can't
    execute on the full-tier browse/drawer render.

    Bodies are fetched on demand (issue_01M0Y7GZED2THYETHCT8NPHHDS) rather than
    read off `d.body`, which no longer exists on a row — the inline row inset
    parses `bodyCache.get(d.id)` (the fetch cache) and the drawer parses `body`
    (the fetch response), and both call sites are checked here rather than the
    old single `d.body` one."""
    html = E.render(_docs())
    assert "Cure53" in html, "DOMPurify library not inlined into the browse bundle"
    assert "DOMPurify.sanitize(marked.parse(bodyCache.get(d.id)))" in html  # inline row inset
    assert "DOMPurify.sanitize(marked.parse(body))" in html                # side drawer
    # no call site may read the (now-absent) `d.body` directly
    assert "marked.parse(d.body)" not in html


def test_body_search_failure_is_not_silently_treated_as_empty(monkeypatch):
    """Round-fix on PR #209 (reviewer, blocking): the server's 503 on a
    Library outage exists so an outage reads as "search failed" rather than
    "no KB matches" (memory_api.py's `search` route). The old client fetch
    chain (`r.ok ? r.json() : { ids: [] }`, `.catch(() => {})`) threw that
    distinction away — a 503 or network error resolved to the same empty
    match set as a genuinely empty result. A non-2xx response must now be
    treated as a rejection, not silently coerced into a fabricated `{ids: []}`."""
    html = E.render(_docs())
    assert "r.ok ? r.json() : { ids: [] }" not in html
    assert "if (!r.ok) throw new Error(" in html
    # the failure is tracked as its own state, distinct from a real (possibly
    # empty) match set — not folded into bodyMatchIds/bodyMatchQuery.
    assert "bodySearchError" in html
    assert "bodySearchError = s;" in html


def test_body_search_failure_and_pending_states_are_visible_in_listing():
    """A failed or in-flight body search must render a visible note, not
    render identically to "genuinely no body match" — the same silent-loss
    failure mode this issue exists to close, moved onto the outage path."""
    html = E.render(_docs())
    assert "Couldn't check document text" in html
    assert "Checking document text" in html
    assert "function bodySearchStatus(" in html


def test_body_search_has_a_minimum_query_length():
    """Search is now keystroke-driven behind a 250ms debounce; a 1-character
    query fired a full server round trip (Library BM25 + an in-process ARCH
    scan) on every keystroke. A short query is skipped rather than searched."""
    html = E.render(_docs())
    assert "MIN_BODY_SEARCH_LEN" in html
    assert "s.length < MIN_BODY_SEARCH_LEN" in html


# --- landing search (issue_01M17443PJXY9TACWDGMMMTA9Z) -----------------------
# `kind === null` is the view the reader lands on. The search box used to only
# filter once a kind was selected: typing on arrival ran a real body search
# (runBodySearch) whose result (`bodyMatchIds`) nothing on the landing path
# ever rendered. These tests run the actual client-side JS (E._BROWSE_JS) in
# node against a DOM/fetch stub rather than grepping for expected strings —
# a fix that restores only the instant field match (and leaves body search
# unrendered) reads as complete under a string-only check.

def _kb_landing_search_data():
    return {
        "docs": [
            {"id": "shared/decisions/0001-foo", "project": "shared", "kind": "decision",
             "title": "Foo decision", "reflabel": "ADR-0001", "status": "current",
             "reviewed": "2026-06-01", "ref": "projects/shared/decisions/0001-foo.md",
             "human": True, "blurb": "A human-readable explainer.", "len": 120},
            {"id": "vivy/runbooks/deploy", "project": "vivy", "kind": "runbook",
             "title": "Deploy runbook", "reflabel": "deploy", "status": "current",
             "reviewed": "2026-06-01", "ref": "projects/vivy/runbooks/deploy.md",
             "human": False, "blurb": "how to deploy", "len": 300},
            # Matches NEITHER title, reflabel, id nor blurb — only a
            # server-side body search (runBodySearch -> /api/memory/search)
            # can find it.
            {"id": "vivy/architecture/zzzneedle", "project": "vivy", "kind": "architecture",
             "title": "Some Arch Doc", "reflabel": "arch-1", "status": "current",
             "reviewed": "2026-06-01", "ref": "",
             "human": False, "blurb": "nothing matching here", "len": 500},
        ],
        "projectLabel": {"shared": "Shared", "vivy": "Vivy"},
        "projectOrder": ["shared", "vivy"],
        "kindOrder": ["decision", "runbook", "architecture"],
        "kindLabel": {"decision": "Decisions", "runbook": "Runbooks", "architecture": "Architecture"},
        "kindTooltip": {},
        "drawerThreshold": 3000,
    }


def _run_browse_js(data, driver_js):
    """Execute E._BROWSE_JS for real in node against a minimal DOM/fetch
    stub, run `driver_js` against it, and return the JSON object it prints.
    Skips (not fails) when node is absent, matching the node-dependent check
    in test_every_inline_js_constant_parses (test_issue_build.py)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to exercise the browse JS")
    dom_stub = f"""
function makeEl(id) {{
  return {{
    id, _html: '', value: '', dataset: {{}},
    classList: {{ add(){{}}, remove(){{}}, toggle(){{ return false; }}, contains(){{ return false; }} }},
    get innerHTML(){{ return this._html; }},
    set innerHTML(v){{ this._html = v; }},
    get textContent(){{ return this._text !== undefined ? this._text : this._html; }},
    set textContent(v){{ this._text = v; }},
    querySelectorAll(){{ return []; }},
    querySelector(){{ return null; }},
    addEventListener(){{}},
    setAttribute(){{}},
    focus(){{}},
    scrollIntoView(){{}},
  }};
}}
const elements = {{}};
function el(id) {{ if (!elements[id]) elements[id] = makeEl(id); return elements[id]; }}
el('data').textContent = JSON.stringify({json.dumps(data)});

global.document = {{
  getElementById(id) {{ if (id === 'engSidebarToggle') return null; return el(id); }},
  addEventListener(){{}},
  readyState: 'complete',
  querySelectorAll(){{ return []; }},
  querySelector(){{ return null; }},
}};
global.window = {{ addEventListener(){{}} }};
global.location = {{ hash: '' }};
global.marked = {{ setOptions(){{}}, parse(s){{ return s; }} }};
global.DOMPurify = {{ sanitize(s){{ return s; }} }};
"""
    full = f"'use strict';\n{dom_stub}\n{E._BROWSE_JS}\n{driver_js}\n"
    r = subprocess.run([node, "-e", full], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"browse JS harness failed:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_landing_search_shows_matches_not_unfiltered_landing():
    """A query at/above MIN_BODY_SEARCH_LEN on the landing page (kind===null)
    must show matching documents, not the unfiltered landing boilerplate a
    listing()-only fix would still show there."""
    result = _run_browse_js(_kb_landing_search_data(), r"""
global.fetch = () => new Promise((resolve) => setTimeout(() =>
  resolve({ ok: true, json: async () => ({ ids: [] }) }), 1));
const initial = el('view').innerHTML;
el('q').value = 'deploy';
el('q').oninput();
setTimeout(() => {
  console.log(JSON.stringify({
    initialHasLandingBoilerplate: initial.includes('Browse the full KB'),
    hasMatchedDoc: el('view').innerHTML.includes('Deploy runbook'),
    hasUnrelatedDoc: el('view').innerHTML.includes('Foo decision'),
    hasLandingBoilerplate: el('view').innerHTML.includes('Browse the full KB'),
  }));
}, 400);
""")
    assert result["initialHasLandingBoilerplate"], "sanity: landing must still render on arrival with no query"
    assert result["hasMatchedDoc"], "a query matching a doc's title must surface it from the landing view"
    assert not result["hasUnrelatedDoc"], "an unrelated doc must not still show once a query is typed"
    assert not result["hasLandingBoilerplate"], (
        "search results must replace the unfiltered landing content, not sit alongside it")


def test_landing_search_shows_matches_for_a_query_below_the_body_search_threshold():
    """A query BELOW MIN_BODY_SEARCH_LEN (a single keystroke) must still run
    the instant field match on the landing view, same as listing() applies
    matchQ unconditionally for any non-empty query — MIN_BODY_SEARCH_LEN
    exists to gate the body-search round trip, not the render, and this is
    the same defect this PR exists to fix, at a smaller size."""
    fetch_calls = "let fetchCalls = 0;\n"
    result = _run_browse_js(_kb_landing_search_data(), fetch_calls + r"""
global.fetch = () => { fetchCalls++; return new Promise((resolve) => setTimeout(() =>
  resolve({ ok: true, json: async () => ({ ids: [] }) }), 1)); };
el('q').value = 'k';   // one char — matches "Deploy runbook" via title, below MIN_BODY_SEARCH_LEN
el('q').oninput();
setTimeout(() => {
  console.log(JSON.stringify({
    hasMatchedDoc: el('view').innerHTML.includes('Deploy runbook'),
    hasUnrelatedDoc: el('view').innerHTML.includes('Foo decision'),
    hasLandingBoilerplate: el('view').innerHTML.includes('Browse the full KB'),
    fetchCalls,
  }));
}, 400);
""")
    assert result["hasMatchedDoc"], "a one-character query must surface a matching document on the landing view"
    assert not result["hasUnrelatedDoc"], "an unrelated doc must not still show once a query is typed"
    assert not result["hasLandingBoilerplate"], (
        "the unfiltered landing content must not still show once a query is typed")
    assert result["fetchCalls"] == 0, (
        "a query below MIN_BODY_SEARCH_LEN must not fire the body-search round trip")


def test_landing_search_surfaces_a_body_only_match():
    """The half most likely left behind by a partial fix: runBodySearch does
    the real search and stores the result in `bodyMatchIds`, but nothing on
    the landing path used to consult it. A doc matching by body text ALONE —
    not title, id, reflabel or blurb — must surface once that search
    resolves for the current query."""
    result = _run_browse_js(_kb_landing_search_data(), r"""
global.fetch = (url) => {
  const m = /[?&]q=([^&]+)/.exec(url);
  const q = m ? decodeURIComponent(m[1]) : '';
  return new Promise((resolve) => setTimeout(() => {
    if (q === 'bodyneedle') resolve({ ok: true, json: async () => ({ ids: ['vivy/architecture/zzzneedle'] }) });
    else resolve({ ok: true, json: async () => ({ ids: [] }) });
  }, 1));
};
el('q').value = 'bodyneedle';
el('q').oninput();
const beforeFetch = el('view').innerHTML;
setTimeout(() => {
  console.log(JSON.stringify({
    beforeFetchHasDoc: beforeFetch.includes('Some Arch Doc'),
    afterFetchHasDoc: el('view').innerHTML.includes('Some Arch Doc'),
  }));
}, 400);
""")
    assert not result["beforeFetchHasDoc"], "sanity: nothing matches this doc before the body search resolves"
    assert result["afterFetchHasDoc"], (
        "a body-only match must surface on the landing page once runBodySearch resolves")


def test_kind_pill_click_preserves_the_typed_query():
    """Clearing qEl.value on a kind switch made sense when the search box did
    nothing on the landing page — now that landing search works, clearing it
    would discard the reader's query at the exact moment they narrow it with
    a type pill."""
    html = E.render(_docs())
    assert "kind = (k==='__home')?null:k; openRow=null; render();" in html
    assert "qEl.value=''" not in html


def test_arch_log_sanitizes_entry_body_before_innerhtml(tmp_path, monkeypatch):
    """ARCH-244: the ARCH-log bundle sanitizes entry bodies too."""
    log = tmp_path / "decisions.md"
    log.write_text("## ARCH-001: first\n\nbody\n")
    monkeypatch.setattr(E, "ARCH_LOG", log)
    html = E.arch_log_html()
    assert "Cure53" in html, "DOMPurify library not inlined into the ARCH-log bundle"
    assert "DOMPurify.sanitize(marked.parse(e.body))" in html
    assert html.count("marked.parse(e.body)") == html.count("DOMPurify.sanitize(marked.parse(e.body))")


def test_render_drawer_threshold_in_payload():
    html = E.render(_docs())
    assert str(E.DRAWER_THRESHOLD) in html


# --- arch log ----------------------------------------------------------------

def test_parse_arch_log_numbers_titles_anchors_and_order():
    text = ("---\nid: x\n---\nPreamble line.\n"
            "## ARCH-002 — second\n\nbody two\n"
            "## ARCH-001: first\n\nbody one\n"
            "## ARCH-139 — onethirtynine\n\nbody 139\n")
    ents = E.parse_arch_log(text)
    # both `:` and `—` separators parse; leading zeros dropped; sorted ascending
    assert [e["num"] for e in ents] == [1, 2, 139]
    one = ents[0]
    assert one["ref"] == "ARCH-1" and one["anchor"] == "arch-1" and one["title"] == "first"
    assert one["body"] == "body one"
    # the preamble / frontmatter before the first heading is not an entry
    assert all("Preamble" not in e["body"] for e in ents)
    assert "id: x" not in "".join(e["body"] for e in ents)


# --- post-ARCH-370 title/date fallback (ARCH-371 moved the heading format;
# `_arch_title_and_date` recovers a title from the body's opening bold line
# for entries the bare "## ARCH-441" heading leaves titleless) -------------


def test_parse_arch_log_title_is_not_its_own_ref_on_new_format(tmp_path):
    """The post-ARCH-371 heading format ("## ARCH-441" then a bold title line
    with the date in parentheses) must parse to a real title, not fall back to
    the bare ref — that silent fallback is exactly how the bug shipped: a
    parse failure and a titleless entry rendered identically."""
    text = (
        "## ARCH-440\n\n"
        "**write-better and convivy-lab take their own dispatch profiles** "
        "(2026-08-03)\n\nbody 440\n"
        "## ARCH-441\n\n"
        "**The log is frozen here: new decisions become project ADRs** "
        "(2026-08-03)\n\nbody 441\n"
    )
    ents = E.parse_arch_log(text)
    newest = ents[-1]
    assert newest["ref"] == "ARCH-441"
    assert newest["title"] != newest["ref"], (
        "a real title must not fall back to the bare ARCH ref — that fallback "
        "is indistinguishable from a broken parse."
    )
    assert newest["title"] == "The log is frozen here: new decisions become project ADRs"
    assert newest["date"] == "2026-08-03"


def test_parse_arch_log_skips_leading_superseded_blockquote(tmp_path):
    """A '> **[SUPERSEDED]** ...' pointer sometimes precedes an entry's own
    title (ARCH-425 in the real log); it must not be taken for the title."""
    text = (
        "## ARCH-425\n\n"
        "> **[SUPERSEDED]** by ARCH-429. Read that entry first.\n\n"
        "**The Library and the Keel: names for the two halves of the platform** "
        "(2026-07-31, the operator)\n\nbody\n"
    )
    ents = E.parse_arch_log(text)
    assert ents[0]["title"] == (
        "The Library and the Keel: names for the two halves of the platform"
    )


def test_arch_title_paren_does_not_span_unrelated_bold_runs():
    """A non-greedy, unanchored search for "**...** (...)" used to match from
    the entry's FIRST bold marker to whatever LATER bold run happened to
    precede an unrelated parenthetical, stitching together a garbled title
    that spanned two sentences. The fix anchors the search to the very start
    of the first non-blockquote paragraph: if that paragraph doesn't open
    with "**title** (date)", there's no title to recover, and the entry must
    fall back to its ref rather than surface a plausible-looking wrong one."""
    body = (
        "**Some heading with no immediate parens.** This sentence continues "
        "and mentions **a related decision** (2026-08-03) closing it.\n\n"
        "Second paragraph."
    )
    title, date = E._arch_title_and_date("ARCH-999", "", body)
    assert title == "ARCH-999"


def test_arch_docs_have_no_empty_title_for_either_format(tmp_path, monkeypatch):
    """Every entry has a title" is the invariant `arch_docs()` promises its
    callers (the /engineering browse, search, and arch-log page, plus Home's
    recent-activity panel) — pin it directly rather than trusting that each
    caller happens to compensate for a blank one."""
    log = tmp_path / "decisions.md"
    log.write_text(
        "## ARCH-1: team-site redesign Phase 1\n\n"
        "**Date:** 2026-05-16\n**Status:** Adopted\n\nbody\n"
        "## ARCH-2\n\n**A bare-heading entry** (2026-08-03)\n\nbody 2\n"
    )
    monkeypatch.setattr(E, "ARCH_LOG", log)
    docs = E.arch_docs()
    assert len(docs) == 2
    assert all(d["title"].strip() for d in docs)


def test_parse_arch_log_no_fallback_titles_on_live_log():
    """Regression pin for the anchoring fix above, run against this machine's
    actual ARCH log rather than a fixture: no real entry's title should
    silently regress to its bare ref. Skipped where the file isn't present
    (CI checks out this repo alone, not the substrate checkout)."""
    log = E.ARCH_LOG
    if not log.exists():
        pytest.skip(f"no substrate checkout at {log}")
    entries = E.parse_arch_log(log.read_text(encoding="utf-8"))
    fell_back = [e["ref"] for e in entries if e["title"] == e["ref"]]
    assert not fell_back, (
        f"{len(fell_back)} of {len(entries)} live entries fell back to their "
        f"ref: {fell_back[:10]}"
    )


def test_arch_log_html_embeds_entries_and_anchors(tmp_path, monkeypatch):
    log = tmp_path / "decisions.md"
    log.write_text("## ARCH-001: first\n\nbody\n## ARCH-139 — big\n\nmore\n")
    monkeypatch.setattr(E, "ARCH_LOG", log)
    html = E.arch_log_html()
    # the citation form + the jump anchor for ARCH-139 are in the embedded payload
    assert '"ref": "ARCH-139"' in html and '"anchor": "arch-139"' in html
    assert 'id="jump"' in html                     # the jump-by-number box
    # deep-link scroll wiring is present
    assert "location.hash" in html
    # padded citations (ARCH-072) must still match — the jump normalizes leading
    # zeros (regression guard for the reviewer's r1 finding)
    assert "normNum" in html and "replace(/^0+(?=" in html
    # No edit affordance on this page (issue_01M17HYDBQ4GPWHPFS56M6QQPH): an
    # ARCH entry is not its own Library document, and the log is closed besides.
    assert 'id="edit"' not in html
    assert "OPEN_BASE" not in html
    assert "localhost:8765" not in html


def test_arch_log_missing_file_degrades(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "ARCH_LOG", tmp_path / "nope.md")
    html = E.arch_log_html()
    assert '"entries": []' in html                 # no entries embedded


# --- project ordering + reference search (ARCH-195) --------------------------

def test_every_ordered_project_has_a_label():
    """PROJECT_ORDER and PROJECT_LABEL drift apart silently. The browse UI falls
    back to `L[p]||p`, so a project in the order with no label renders its raw
    slug as a group header and a filter pill — user-visible, and no test caught
    it when the re-file replaced the scope names (reviewer, PR #148)."""
    unlabelled = [p for p in E.PROJECT_ORDER if p not in E.PROJECT_LABEL]
    assert not unlabelled, (
        f"in PROJECT_ORDER but missing from PROJECT_LABEL, so /reference browse "
        f"shows the bare slug: {unlabelled}")


def test_render_project_order_puts_company_first():
    html = E.render(_docs())
    order = json.loads(re.search(r'"projectOrder":\s*(\[[^\]]*\])', html).group(1))
    # The company-altitude scope leads every category page. It was `shared` until
    # Phase 0 step 5 emptied that scope and `convivy` became `company`; the intent
    # is unchanged and the name it travels under is not.
    assert order[0] == "company"


def test_render_links_project_group_header_to_its_project_page():
    """Epic issue_01M0AK8CM3ADKDTG8PM6GRCP8H, item 5: the per-project page's
    entry point is wired from the "All projects" grouped-by-project header on
    this browse page, since that's where a project name already renders as a
    label — no new nav item was added for it."""
    html = E.render(_docs())
    assert 'class="grp-link" href="/projects/${encodeURIComponent(p)}"' in html


def test_render_makes_doc_id_searchable():
    html = E.render(_docs())
    assert "d.id.toLowerCase().includes(s)" in html


# --- references + ARCH-in-KB (ARCH-196) --------------------------------------

def test_ref_label_decisions_get_adr_others_get_slug():
    assert E._ref_label("decision", "vivy/decisions/0021-ledger-engine") == "ADR-0021"
    assert E._ref_label("decision", "constance/decisions/0001-foo") == "ADR-0001"
    # a decision id without a numeric prefix falls back to the slug
    assert E._ref_label("decision", "shared/decisions/charter") == "charter"
    # non-decision kinds show their slug name
    assert E._ref_label("reference", "vivy/reference/overnight-queue") == "overnight-queue"
    assert E._ref_label("runbook", "vivy/runbooks/release") == "release"


def test_load_docs_carries_reflabel(tmp_path, monkeypatch):
    _seed_kb(monkeypatch, [
        _row(id="vivy/decisions/0007-foo", kind="decision"),
        _row(id="vivy/runbooks/deploy", kind="runbook"),
    ])
    by = {d["id"]: d for d in E.load_docs()}
    assert by["vivy/decisions/0007-foo"]["reflabel"] == "ADR-0007"
    assert by["vivy/runbooks/deploy"]["reflabel"] == "deploy"


def test_arch_docs_surfaces_log_entries_as_arch_kind(tmp_path, monkeypatch):
    log = tmp_path / "decisions.md"
    log.write_text("## ARCH-001: first\n\nbody one\n## ARCH-139 — onethirtynine\n\nbody 139\n")
    monkeypatch.setattr(E, "ARCH_LOG", log)
    docs = E.arch_docs()
    assert {d["reflabel"] for d in docs} == {"ARCH-1", "ARCH-139"}
    d = next(x for x in docs if x["reflabel"] == "ARCH-139")
    assert d["kind"] == "arch"
    # `substrate`, agreeing with the id and the ref. It was `engineering` until
    # PR G — a scope Phase 0 step 5 retired, which sorted every ARCH entry last
    # and rendered a bare slug where a project label belongs.
    assert d["project"] == "substrate"
    assert d["ref"] == "substrate/decisions.md"   # the flat file this entry was parsed from
    assert d["title"] == "onethirtynine"


def test_arch_docs_empty_when_log_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "ARCH_LOG", tmp_path / "nope.md")
    assert E.arch_docs() == []


# --- get_arch_body / search_arch_bodies (issue_01M0Y7GZED2THYETHCT8NPHHDS) ---
# The half of the on-demand body fetch + search the Library cannot serve: ARCH
# entries live only in substrate/decisions.md, parsed in-process.

def test_get_arch_body_returns_the_entrys_body(tmp_path, monkeypatch):
    log = tmp_path / "decisions.md"
    log.write_text("## ARCH-001: first\n\nbody one\n## ARCH-139 — big\n\nbody 139\n")
    monkeypatch.setattr(E, "ARCH_LOG", log)
    assert E.get_arch_body("substrate/arch/arch-139") == "body 139"


def test_get_arch_body_none_for_unknown_id(tmp_path, monkeypatch):
    log = tmp_path / "decisions.md"
    log.write_text("## ARCH-001: first\n\nbody one\n")
    monkeypatch.setattr(E, "ARCH_LOG", log)
    assert E.get_arch_body("substrate/arch/arch-999") is None


def test_search_arch_bodies_finds_a_phrase_only_in_the_body(tmp_path, monkeypatch):
    """The regression this issue exists to prevent: dropping `body` from the
    payload must not silently degrade search for an ARCH entry, the half of
    the corpus the Library never served in the first place. The phrase below
    appears in neither the title nor anywhere else — this only passes if
    `search_arch_bodies` actually reads the entry body."""
    log = tmp_path / "decisions.md"
    log.write_text(
        "## ARCH-001: first entry\n\nSome unrelated prose in the third paragraph.\n"
        "## ARCH-002: second entry\n\nAn opening line, then a phrase found nowhere "
        "else on the page: zephyrquartz-signature.\n")
    monkeypatch.setattr(E, "ARCH_LOG", log)
    ids = E.search_arch_bodies("zephyrquartz-signature")
    assert ids == ["substrate/arch/arch-2"]
    # sanity: breaking the search (e.g. searching title instead of body) must
    # fail this test — a query matching neither title finds nothing spurious
    assert E.search_arch_bodies("no such phrase anywhere") == []


def test_arch_docs_caches_the_parse_until_the_file_changes(tmp_path, monkeypatch):
    """Round-fix on PR #209 (mine): `arch_docs()` re-read and re-parsed the
    ~1.5 MB log on every call, and search is now keystroke-driven with no
    minimum query length — measured at ~10-15ms uncached against the live
    log. `search_arch_bodies`/`get_arch_body` both go through `arch_docs()`,
    so caching here covers both. A second call with the file unchanged must
    not re-parse."""
    E._ARCH_DOCS_CACHE.clear()
    log = tmp_path / "decisions.md"
    log.write_text("## ARCH-001: first\n\nbody one\n")
    monkeypatch.setattr(E, "ARCH_LOG", log)
    calls = []
    real_parse = E.parse_arch_log
    monkeypatch.setattr(E, "parse_arch_log",
                         lambda text: (calls.append(1), real_parse(text))[1])
    first = E.arch_docs()
    second = E.arch_docs()
    assert len(calls) == 1, "second call re-parsed instead of hitting the cache"
    assert first == second


def test_arch_docs_cache_invalidates_when_the_log_changes(tmp_path, monkeypatch):
    """The steward keeps appending to the live log — a cache that never
    invalidates would serve a stale entry list forever."""
    E._ARCH_DOCS_CACHE.clear()
    log = tmp_path / "decisions.md"
    log.write_text("## ARCH-001: first\n\nbody one\n")
    monkeypatch.setattr(E, "ARCH_LOG", log)
    assert {d["reflabel"] for d in E.arch_docs()} == {"ARCH-1"}
    log.write_text("## ARCH-001: first\n\nbody one\n## ARCH-002: second\n\nbody two\n")
    import os
    st = log.stat()
    os.utime(log, (st.st_atime, st.st_mtime + 5))   # force a distinct mtime
    assert {d["reflabel"] for d in E.arch_docs()} == {"ARCH-1", "ARCH-2"}


def test_render_carries_arch_kind_and_descriptive_labels():
    html = E.render(_docs())
    assert '"arch"' in html                          # arch is in kindOrder (VISIBLE_KINDS)
    # descriptive labels (ARCH-200) — not terse DEC/ARCH
    assert '"Platform Decisions (ARCH)"' in html
    assert '"Decisions (ADR)"' in html
    assert "kindLabel" in html


def test_render_carries_kind_tooltips():
    html = E.render(_docs())
    # each category has a hover tooltip; the ARCH-vs-architecture disambiguation is explicit
    assert "kindTooltip" in html and "function kindTip(" in html
    assert "the steward's engineering decision log" in html   # arch tooltip
    assert "not a point-in-time decision" in html             # architecture tooltip
    # tooltips wired into the sidebar rows + stat chips
    assert 'title="${esc(kindTip(k))}"' in html


def test_tag_draft_uses_tokens_not_hardcoded_hex():
    # .tag.draft must use design-system tokens (brand-wash/brand-ink), not the
    # hardcoded #fff0d6/#b8860b hex values that fail WCAG contrast (2.90).
    import engineering_build as E
    assert "#b8860b" not in E._BROWSE_CSS
    assert "#fff0d6" not in E._BROWSE_CSS
    assert "var(--brand-wash)" in E._BROWSE_CSS
    assert "var(--brand-ink)" in E._BROWSE_CSS


def test_browse_row_has_role_button():
    html = E.render(_docs())
    assert 'role="button"' in html


def test_browse_card_has_role_button_and_tabindex():
    html = E.render(_docs())
    # Cards in the feature landing grid need keyboard operability.
    assert 'role="button" tabindex="0"' in html or 'tabindex="0" role="button"' in html or (
        'class="card" role="button" tabindex="0"' in html or
        'role="button"' in html and 'tabindex="0"' in html
    )


def test_eng_stat_has_role_button():
    html = E.render(_docs())
    # Kind-count stat chips on the landing need role=button for keyboard activation.
    assert 'class="eng-stat" role="button"' in html


def test_arch_log_has_mobile_toggle():
    import engineering_build as E
    html = E.arch_log_html()
    assert 'id="archSidebarToggle"' in html
    assert 'aria-controls="archSidebar"' in html


def test_live_html_includes_arch_docs(monkeypatch):
    # live_html = KB docs + arch docs; stub both so the test is hermetic
    monkeypatch.setattr(E, "load_docs", lambda: _docs())
    monkeypatch.setattr(E, "arch_docs", lambda: [
        # `substrate`, matching what arch_docs() actually returns since PR G. The
        # assertion below checks the id reaches the HTML rather than the project
        # grouping, so the old value passed — a fixture misrepresenting its
        # subject, which is the shape PR F found three of.
        {"id": "substrate/arch/arch-189", "project": "substrate", "kind": "arch",
         "title": "review integrity", "reflabel": "ARCH-189", "status": "current",
         "reviewed": "", "ref": "substrate/decisions.md", "human": False,
         "blurb": "b", "len": 50, "body": "body"}])
    html = E.live_html()
    assert '"ARCH-189"' in html and '"substrate/arch/arch-189"' in html


def test_doc_drawer_content_contains_overflow_guards():
    """Wide doc content (multi-column tables, long inline-code file paths) must
    scroll within its own box, not spill the drawer edge (the operator 2026-07-17)."""
    import engineering_build
    html = engineering_build.render([])
    # the table becomes its own horizontal-scroll container
    assert ".eng-doc-content table { display: block; overflow-x: auto;" in html
    # long unbroken strings wrap instead of forcing a wide canvas
    assert ".eng-doc-content { max-width: 100%; overflow-wrap: anywhere; }" in html
    # code blocks + images stay within the drawer
    assert "overflow-x: auto;   /* a wide code block scrolls within itself" in html
    assert ".eng-doc-content img { max-width: 100%; height: auto; }" in html


# --- kind coverage: a KB kind orient does not know about is invisible ---------
#
# ARCH-366. `field-notes` was added to the KB schema (ARCH-289) and to
# agent-platform's kind list, but never to VISIBLE_KINDS here — and
# engineering_build skips any doc whose kind is not in that list. So the first
# doc to use it validated cleanly, indexed cleanly, and silently never appeared
# in browse. Nothing failed; the doc was simply unreachable.
#
# This is the same shape as the orphanings ARCH-357 and ARCH-366 cleaned up:
# the artifact exists, so everything reads as done, and no check asks whether a
# human can actually get to it.

# Kinds the KB allows that orient deliberately does NOT browse. Stated
# explicitly so "not browsable" is a decision on the record rather than the
# accident of omission that hid field-notes. Most of these are operational
# noise for a human reader (engineering_build.VISIBLE_KINDS' own comment); the
# KB and MCP still serve them to agents.
#
# `brief` is the exception: it left VISIBLE_KINDS on 2026-08-26, not because
# it's noise, but because a human DOES read it — under the operator's Archives-vs-
# Documents split, a brief is a memory (a dated snapshot of what was read that
# week), not canonical knowledge someone maintains, so it surfaces at
# /reference/briefings (Archives) instead of this canonical browse.
# `meeting-notes`, `observation`, `proposal` and `feature-request` are memories
# by that same rule and now surface there too (the operator 2026-08-26) — they stay in
# this set because the set means "not in the canonical browse," not "invisible
# everywhere." `legal` and `marketing` were considered for the same move and
# rejected (the operator: they read canonical, not memory).
DELIBERATELY_NOT_BROWSABLE = {
    "meeting-notes", "legal", "observation", "proposal", "feature-request",
    "marketing", "playbook", "brief",
}


def test_every_visible_kind_is_fully_wired():
    """A kind in VISIBLE_KINDS needs a label and a tooltip too. field-notes was
    half-wired in exactly this way in the KB (allowed, but not renderable), and
    a kind listed as visible with no label renders a blank left-nav entry."""
    for kind in E.VISIBLE_KINDS:
        assert kind in E.KIND_LABEL, f"{kind} is browsable but has no KIND_LABEL"
        assert kind in E.KIND_TOOLTIP, f"{kind} is browsable but has no KIND_TOOLTIP"


@pytest.mark.needs_kb
def test_every_kb_kind_is_either_browsable_or_explicitly_hidden():
    """Every kind the KB schema allows must be a decision here: browsable, or
    named in DELIBERATELY_NOT_BROWSABLE. A kind in neither set is invisible by
    accident, which is how field-notes was lost.

    Reads the KB's own schema rather than a copy, so it cannot drift, and keeps
    the skip rather than vendoring the kind list — a vendored copy is another
    second source of truth, the defect this whole change is undoing.

    `needs_kb` carries that skip now, which is what makes the guard REACHABLE.
    Skipping in CI while running nowhere else meant a green main and a failing
    assertion on every machine with the KB; `bin/check-kb-guards` runs the marked
    set daily on the deploy host, which is where a KB kind change actually lands.
    """
    import ast

    from conftest import knowledge_repo

    validate = knowledge_repo() / "scripts" / "validate.py"
    assert validate.exists(), (
        f"the knowledge checkout has no scripts/validate.py at {validate} — the "
        f"marker admitted this test, so the checkout is there but malformed")

    tree = ast.parse(validate.read_text(encoding="utf-8"))
    kb_kinds = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "ALLOWED_KINDS" for t in node.targets):
            kb_kinds = {c.value for c in ast.walk(node.value)
                        if isinstance(c, ast.Constant) and isinstance(c.value, str)}
            break
    assert kb_kinds, "could not read ALLOWED_KINDS from the knowledge repo's validate.py"

    unaccounted = kb_kinds - set(E.VISIBLE_KINDS) - DELIBERATELY_NOT_BROWSABLE
    assert not unaccounted, (
        f"KB kinds neither browsable nor explicitly hidden: {sorted(unaccounted)} — "
        f"a doc of this kind indexes fine and then never appears in /reference browse. "
        f"Add it to VISIBLE_KINDS (with a label and tooltip) or to "
        f"DELIBERATELY_NOT_BROWSABLE.")


# --- the ARCH log page is history, and has to say so ------------------------
#
# The log closed at ARCH-441 on 2026-08-03 (platform/decisions/0018). This page
# renders it exactly as it did while the log was open, so a reader has no way to
# tell a frozen decision log from a live one unless the page tells them.

def test_the_arch_log_page_says_the_log_is_closed():
    html = E.arch_log_html()
    assert "This log is closed" in html
    assert "ARCH-441" in html


def test_the_closed_banner_links_to_the_entry_that_explains_it():
    """A bare "closed" is a dead end. The reader's next question is why, and the
    answer is one anchor away on the page they are already on."""
    assert '#arch-441' in E.arch_log_html()


def test_the_page_still_renders_the_entries_it_always_did():
    """Closed is not gone. 9,328 ARCH citations across five repos still point
    here, so the page's job is unchanged — only its framing moved."""
    html = E.arch_log_html()
    assert 'id="entries"' in html
    assert 'id="toc"' in html          # the jump index a citation depends on
    assert len(html) > 100_000         # the log itself, not just the shell


# --- the Greater KB browse reads the WHOLE corpus (issue_01M1MFKD2KX9CPBVCQKJP1R003) ---


def test_load_docs_lists_a_non_team_document_and_marks_it_unopenable(monkeypatch):
    """**This page is the Greater KB: it lists every current/draft document of a
    browsable kind, not only the ones the reader may open.**

    Measured on 2026-09-11: 644 rows, 300 team-audience and 344 not. Reaching
    for `list_reference_documents(audience="team")` — the obvious function —
    returns exactly those 300 and would have dropped 344 documents while looking
    like a clean migration. `human` is the per-row gate: false here means the
    title is visible and the document is not linked."""
    _seed_kb(monkeypatch, [
        _row(id="open/one", kind="decision", audience=["team"]),
        _row(id="shut/two", kind="decision", audience=["refactorer"]),
    ])
    by = {d["id"]: d for d in E.load_docs()}
    assert set(by) == {"open/one", "shut/two"}, "a non-team document was dropped"
    assert by["open/one"]["human"] is True
    assert by["shut/two"]["human"] is False


def test_load_docs_takes_blurb_and_length_from_the_listing_not_a_body(monkeypatch):
    """The capability the slice waited for (convivy/library#107). This page
    renders live per request; fetching 644 bodies at 3.7ms each would add 2.4s
    to every render, so both values arrive on the listing row."""
    _seed_kb(monkeypatch, [
        _row(id="x/y", kind="decision", body="First real prose line.\n\nMore.\n"),
    ])

    def _boom(*a, **k):
        raise AssertionError("load_docs must not fetch a body")
    monkeypatch.setattr(library_client, "get_reference_document", _boom)

    d = E.load_docs()[0]
    assert d["blurb"] == "First real prose line."
    assert d["len"] == len("First real prose line.\n\nMore.\n")


def test_load_docs_orders_by_project_kind_id(monkeypatch):
    """The old SQL carried `ORDER BY project, kind, id`; the Library's listing
    order is not guaranteed, so the sort is restated rather than inherited —
    the same correction `build.load_docs` needed when it moved."""
    _seed_kb(monkeypatch, [
        _row(id="z", project="zeta", kind="decision"),
        _row(id="a", project="alpha", kind="spec"),
        _row(id="m", project="alpha", kind="decision"),
    ])
    assert [d["id"] for d in E.load_docs()] == ["m", "a", "z"]


# --- editing where the document is read (issue_01M17HYDBQ4GPWHPFS56M6QQPH) ---
# The done-when bullet is that a member reading a document on THIS page reaches
# the editor without the page navigating. Edit used to be a link to
# `/reference/doc/{id}`, so the reader lost their place in a 1158-row listing to
# fix a typo.
#
# These run the browse JS and the shared editor controller together in node, for
# the reason `test_kb_editor.py` gives at more length: the thing being checked is
# who holds a four-hour Library checkout and when it goes back, and a released
# lock and a stranded one look identical in the source.

_EDITOR_DOM_STUB = r"""
function camel(name) {
  return name.replace(/^data-/, '').replace(/-([a-z])/g, (m, c) => c.toUpperCase());
}
function makeEl(id) {
  const el = {
    id: id, _html: '', _children: {}, _listeners: {}, value: '', hidden: false,
    disabled: false, className: '', dataset: {}, onclick: null, onkeydown: null,
    _classes: new Set(),
    classList: {
      add(c) { el._classes.add(c); }, remove(c) { el._classes.delete(c); },
      contains(c) { return el._classes.has(c); },
      toggle(c) { if (el._classes.has(c)) { el._classes.delete(c); return false; }
                  el._classes.add(c); return true; },
    },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = v; this._children = {}; },
    get textContent() { return this._text === undefined ? this._html : this._text; },
    set textContent(v) { this._text = v; },
    // Finds every occurrence of the selector's attribute in the markup that was
    // assigned, one stub each, with `dataset` filled from the attribute value.
    // That is what lets a test click the SECOND row's Edit button and see which
    // document it acts on — the off-by-one a single-element stub cannot catch.
    querySelectorAll(sel) {
      const attr = sel.replace(/^\[/, '').replace(/\]$/, '');
      const valued = attr.includes('=');
      const name = valued ? attr.slice(0, attr.indexOf('=')) : attr;
      const re = valued
        ? new RegExp(attr.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g')
        : new RegExp(name + '(?:="([^"]*)")?', 'g');
      const out = [];
      let m, n = 0;
      while ((m = re.exec(this._html)) !== null) {
        const key = sel + '#' + (n++);
        if (!this._children[key]) this._children[key] = makeEl();
        const child = this._children[key];
        if (!valued) child.dataset[camel(name)] = m[1] === undefined ? '' : m[1];
        out.push(child);
      }
      return out;
    },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    fire(type, ev) { (this._listeners[type] || []).forEach(fn => fn(ev || {})); },
    setAttribute() {}, removeAttribute() {}, focus() {}, scrollIntoView() {},
  };
  return el;
}

const elements = {};
function el(id) { if (!elements[id]) elements[id] = makeEl(id); return elements[id]; }
const drawerBody = makeEl('drawer-body');

global.document = {
  getElementById(id) { if (id === 'engSidebarToggle') return null; return el(id); },
  addEventListener() {},
  readyState: 'complete',
  querySelectorAll() { return []; },
  querySelector(sel) {
    if (sel === '#doc-drawer .drawer-body') return drawerBody;
    if (sel === '#doc-drawer [data-kb-mount]') return drawerBody.querySelector('[data-kb-mount]');
    return null;
  },
};
// The drawer's open/close, as design_system wires them: `_dsOpen` is what
// openDoc calls, and `ds:drawer-close` is what the X, the backdrop and Escape
// all end up dispatching.
el('doc-drawer')._dsOpen = function () { el('doc-drawer').classList.add('open'); };
function closeDrawer() {
  el('doc-drawer').classList.remove('open');
  el('doc-drawer').fire('ds:drawer-close', {});
}

const calls = [];
let responses = {};
const bodyDelays = {};
let confirmAnswer = true;
let confirmCount = 0;
global.fetch = function (url, init) {
  const action = String(url).split('/').pop();
  calls.push({ action: action, url: String(url) });
  if (action === 'body') {
    // `bodyDelays` keys a per-document delay, so a test can make document A's
    // body land after the reader has already moved the drawer to B.
    const id = decodeURIComponent(String(url).split('/documents/')[1].replace('/body', ''));
    const answer = { ok: true, json: () => Promise.resolve({ body: 'body of ' + id }) };
    const wait = bodyDelays[id] || 0;
    if (!wait) return Promise.resolve(answer);
    return new Promise(resolve => setTimeout(() => resolve(answer), wait));
  }
  if (action.startsWith('search?') || String(url).includes('/api/memory/search')) {
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ids: [] }) });
  }
  const r = responses[action] || { ok: true, status: 200, body: {} };
  return Promise.resolve({ ok: r.ok, status: r.status, json: () => Promise.resolve(r.body) });
};

// md-serial's mountEditor, faked as in test_kb_editor.py: the body's text lives
// on the root element as `_src`, and `typeBody` is a member typing into it.
// The glue is the real editor_js/kb-doc.js, imported from disk.
function fakeMountEditor(root, src, opts) {
  root._src = src;
  root._rev = (opts && opts.revision) || 0;
  return {
    save() { return root._src; },
    saveChecked() { return { source: root._src, roundTrip: { ok: true, failures: [] } }; },
    revision() { return root._rev; },
  };
}
function typeBody(mount, text) {
  const body = mount.querySelector('[data-kb="body"]');
  body._src = text; body._rev++;
}
global.window = {
  addEventListener() {},
  confirm() { confirmCount++; return confirmAnswer; },
  KbEditorLoadLibrary() {
    return import(KB_DOC_URL).then(kb => ({ kb: kb, mountEditor: fakeMountEditor }));
  },
};
global.location = { hash: '' };
global.marked = { setOptions() {}, parse(s) { return s; } };
global.DOMPurify = { sanitize(s) { return s; } };
const setIntervalCalls = [];
global.setInterval = function (fn) { setIntervalCalls.push(fn); return setIntervalCalls.length; };
global.clearInterval = function () {};
function kbActions() { return calls.filter(c => c.action !== 'body').map(c => c.action); }
responses.checkout = { ok: true, status: 200,
  body: { content: 'stored file', content_sha: 'sha-1' } };
"""


def _editor_docs():
    return {
        "docs": [
            {"id": "shared/decisions/0001-foo", "project": "shared", "kind": "decision",
             "title": "Foo decision", "reflabel": "ADR-0001", "status": "current",
             "reviewed": "2026-06-01", "ref": "projects/shared/decisions/0001-foo.md",
             "human": True, "blurb": "b", "len": 10},
            {"id": "shared/decisions/0002-bar", "project": "shared", "kind": "decision",
             "title": "Bar decision", "reflabel": "ADR-0002", "status": "current",
             "reviewed": "2026-06-01", "ref": "projects/shared/decisions/0002-bar.md",
             "human": True, "blurb": "b", "len": 10},
        ],
        "projectLabel": {"shared": "Shared"}, "projectOrder": ["shared"],
        "kindOrder": ["decision"], "kindLabel": {"decision": "Decisions"},
        "kindTooltip": {}, "drawerThreshold": 3000,
        "canKbWrite": "true",
    }


def _run_editor_js(driver_js, data=None):
    """Run the shared editor controller and the browse JS together in node.
    Skips when node is absent, like the other node-driven checks here."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to exercise the browse JS")
    data_js = f"el('data').textContent = JSON.stringify({json.dumps(data or _editor_docs())});\n"
    kb_doc_url = (Path(kb_editor.__file__).resolve().parent / "editor_js" / "kb-doc.js").as_uri()
    src = ("'use strict';\n" + f"const KB_DOC_URL = {json.dumps(kb_doc_url)};\n"
           + _EDITOR_DOM_STUB + data_js
           + kb_editor.EDITOR_JS + "\n" + E._BROWSE_JS + "\n" + driver_js + "\n")
    r = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"browse/editor harness failed:\n{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_row_edit_button_edits_the_document_it_belongs_to():
    """DoD: the Edit control must act on the document the member was reading,
    not a neighbour. A single-document fixture cannot catch an off-by-one, so
    this plants two and clicks the SECOND row's button."""
    result = _run_editor_js(r"""
kind = 'decision';
render();
const buttons = el('view').querySelectorAll('[data-edit]');
buttons[1].onclick({ stopPropagation() {} });
setTimeout(() => console.log(JSON.stringify({
  buttonCount: buttons.length,
  checkoutUrl: (calls.find(c => c.action === 'checkout') || {}).url,
  editingDocId: window.KbEditor.docId(),
  drawerOpen: el('doc-drawer').classList.contains('open'),
})), 30);
""")
    assert result["buttonCount"] == 2, "both team-audience rows must offer Edit"
    assert result["editingDocId"] == "shared/decisions/0002-bar"
    assert result["checkoutUrl"] == "/api/kb/documents/shared%2Fdecisions%2F0002-bar/checkout"
    assert result["drawerOpen"] is True, (
        "the editor opens in the drawer, on this page — the whole point of the change")


def test_editing_a_second_document_from_the_listing_releases_the_first():
    """**The stranded-lock case the browse page introduces and the document's
    own page never had.** That page ends when you navigate away; this listing
    stays put while a reader clicks between documents, so without an explicit
    release, ten Edit clicks would mean ten four-hour locks, each blocking every
    other writer including the Library's own kb_update and kb_propose."""
    result = _run_editor_js(r"""
kind = 'decision';
render();
const buttons = el('view').querySelectorAll('[data-edit]');
buttons[0].onclick({ stopPropagation() {} });
setTimeout(() => {
  el('view').querySelectorAll('[data-edit]')[1].onclick({ stopPropagation() {} });
  setTimeout(() => console.log(JSON.stringify({
    actions: kbActions(),
    checkinUrl: (calls.find(c => c.action === 'checkin') || {}).url,
    editingDocId: window.KbEditor.docId(),
  })), 30);
}, 30);
""")
    assert result["actions"] == ["checkout", "checkin", "checkout"]
    assert result["checkinUrl"] == "/api/kb/documents/shared%2Fdecisions%2F0001-foo/checkin", (
        "the first document's lock is what goes back")
    assert result["editingDocId"] == "shared/decisions/0002-bar"


def test_closing_the_drawer_checks_the_document_back_in():
    """Escape, the X and the backdrop all end in design_system's `close()`,
    which is why it dispatches `ds:drawer-close`. Without this wiring a reader
    who pressed Escape over an open editor would leave a four-hour lock held
    with nothing on screen to release it."""
    result = _run_editor_js(r"""
kind = 'decision';
render();
el('view').querySelectorAll('[data-edit]')[0].onclick({ stopPropagation() {} });
setTimeout(() => {
  closeDrawer();
  setTimeout(() => console.log(JSON.stringify({
    actions: kbActions(),
    checkinUrl: (calls.find(c => c.action === 'checkin') || {}).url,
    editingDocId: window.KbEditor.docId(),
  })), 30);
}, 30);
""")
    assert result["actions"] == ["checkout", "checkin"]
    assert result["checkinUrl"] == "/api/kb/documents/shared%2Fdecisions%2F0001-foo/checkin"
    assert result["editingDocId"] is None


def test_a_declined_discard_reopens_the_drawer_and_keeps_the_lock():
    """The drawer has already shut by the time `ds:drawer-close` fires, so a
    reader who declines to discard unsaved work gets it back rather than
    prevented. Releasing the lock anyway would leave them looking at an editor
    that can no longer save."""
    result = _run_editor_js(r"""
confirmAnswer = false;
kind = 'decision';
render();
el('view').querySelectorAll('[data-edit]')[0].onclick({ stopPropagation() {} });
setTimeout(() => {
  const mount = document.querySelector('#doc-drawer [data-kb-mount]');
  typeBody(mount, 'unsaved work');
  closeDrawer();
  setTimeout(() => console.log(JSON.stringify({
    actions: kbActions(),
    confirmCount: confirmCount,
    editingDocId: window.KbEditor.docId(),
    drawerReopened: el('doc-drawer').classList.contains('open'),
  })), 30);
}, 30);
""")
    assert result["confirmCount"] == 1
    assert result["actions"] == ["checkout"], "nothing may be released on a declined discard"
    assert result["editingDocId"] == "shared/decisions/0001-foo"
    assert result["drawerReopened"] is True


def test_typing_in_the_search_box_does_not_disturb_an_open_editor():
    """The listing is rebuilt from state on every keystroke, which is why the
    editor mounts in the drawer rather than in a row. A stray character must
    not destroy the editor, lose the draft or strand the checkout."""
    result = _run_editor_js(r"""
kind = 'decision';
render();
el('view').querySelectorAll('[data-edit]')[0].onclick({ stopPropagation() {} });
setTimeout(() => {
  const mount = document.querySelector('#doc-drawer [data-kb-mount]');
  typeBody(mount, 'half-written sentence');
  el('q').value = 'zzzz';   // matches neither document
  el('q').oninput();
  setTimeout(() => {
    const after = document.querySelector('#doc-drawer [data-kb-mount]');
    console.log(JSON.stringify({
      actions: kbActions(),
      editingDocId: window.KbEditor.docId(),
      draft: after.querySelector('[data-kb="body"]')._src,
      listingEmptied: el('view').innerHTML.includes('Nothing matches'),
    }));
  }, 30);
}, 30);
""")
    assert result["listingEmptied"] is True, (
        "sanity: the query really did filter both documents out of the listing")
    assert result["actions"] == ["checkout"], (
        "a keystroke must not release or re-take the checkout")
    assert result["editingDocId"] == "shared/decisions/0001-foo"
    assert result["draft"] == "half-written sentence"


def test_reading_another_document_in_the_drawer_releases_the_editor_first():
    """**The path a mutation check caught with nothing watching it.** Switching
    documents via Edit is safe on its own, because the controller releases the
    old lock as part of opening the new one. Merely READING another long
    document goes through `openDoc` without touching the controller, so the
    drawer's markup — the open editor with it — is replaced while the checkout
    stays held. Without the release in `openDoc`, a reader who edits one
    document and then clicks a second to read it strands the first one's lock
    for the full four hours."""
    data = _editor_docs()
    data["docs"][1]["len"] = 9999   # over drawerThreshold, so a row click opens the drawer
    result = _run_editor_js(r"""
kind = 'decision';
render();
el('view').querySelectorAll('[data-edit]')[0].onclick({ stopPropagation() {} });
setTimeout(() => {
  clickRow(1);   // just reading the second document, not editing it
  setTimeout(() => console.log(JSON.stringify({
    actions: kbActions(),
    checkinUrl: (calls.find(c => c.action === 'checkin') || {}).url,
    editingDocId: window.KbEditor.docId(),
  })), 30);
}, 30);
""", data=data)
    assert result["actions"] == ["checkout", "checkin"], (
        "reading another document must return the lock the editor was holding")
    assert result["checkinUrl"] == "/api/kb/documents/shared%2Fdecisions%2F0001-foo/checkin"
    assert result["editingDocId"] is None


def test_the_browse_payload_carries_the_kb_write_placeholder():
    """server.py fills `__CAN_KB_WRITE__` from the viewer's role; this page's
    job is to emit it, and to emit it INSIDE the JSON payload the script reads
    rather than on an attribute, since the rows it gates are built client-side
    from `DATA`. A literal reaching the browser would be read by `canEdit` as a
    role string."""
    html = E.render(_docs())
    assert '"canKbWrite": "__CAN_KB_WRITE__"' in html
    payload = re.search(r'<script id="data" type="application/json">(.*?)</script>',
                        html, re.S).group(1)
    assert json.loads(payload)["canKbWrite"] == "__CAN_KB_WRITE__", (
        "the placeholder must sit in the payload the script parses, not merely "
        "somewhere on the page")


def test_a_slow_body_for_the_previous_document_does_not_repaint_over_the_editor():
    """`fetchBody` is a round trip, so document A's body can land after the
    reader has moved the drawer to B. Repainting then overwrites B's markup with
    A's and leaves B's live editor painted into a node no longer in the page:
    the draft invisible and the checkout still held. Raised in review of PR
    #315; the drawer's own close hook would eventually recover the lock, but not
    the draft."""
    data = _editor_docs()
    result = _run_editor_js(r"""
bodyDelays['shared/decisions/0001-foo'] = 60;
kind = 'decision';
render();
openDoc(0);                 // slow body, still in flight
el('view').querySelectorAll('[data-edit]')[1].onclick({ stopPropagation() {} });
setTimeout(() => {
  const mount = document.querySelector('#doc-drawer [data-kb-mount]');
  typeBody(mount, 'typing into the second document');
  setTimeout(() => {        // by now the FIRST document's body has landed
    const after = document.querySelector('#doc-drawer [data-kb-mount]');
    console.log(JSON.stringify({
      drawerShows: document.querySelector('#doc-drawer .drawer-body').innerHTML.includes('0002-bar')
        ? 'second' : 'first',
      editingDocId: window.KbEditor.docId(),
      draft: after ? after.querySelector('[data-kb="body"]')._src : null,
    }));
  }, 80);
}, 20);
""", data=data)
    assert result["drawerShows"] == "second", (
        "the first document's late body must not repaint over the second")
    assert result["editingDocId"] == "shared/decisions/0002-bar"
    assert result["draft"] == "typing into the second document", (
        "the draft must survive a repaint the reader never asked for")


def test_editing_one_document_while_reading_another_opens_the_right_one():
    """**The failure this whole feature exists to avoid, one level in.** The
    drawer's mount element survives every repaint, so a reader who opens
    document A in the drawer and then clicks Edit on document B's row could get
    B's editor painted under A's text. Found by the slow-body test above while
    fixing a finding from review of PR #315."""
    result = _run_editor_js(r"""
kind = 'decision';
render();
openDoc(0);                 // reading the first document in the drawer
setTimeout(() => {
  el('view').querySelectorAll('[data-edit]')[1].onclick({ stopPropagation() {} });
  setTimeout(() => {
    const drawer = document.querySelector('#doc-drawer .drawer-body').innerHTML;
    console.log(JSON.stringify({
      editingDocId: window.KbEditor.docId(),
      drawerShowsSecond: drawer.includes('0002-bar'),
      drawerShowsFirst: drawer.includes('0001-foo'),
    }));
  }, 40);
}, 40);
""")
    assert result["editingDocId"] == "shared/decisions/0002-bar"
    assert result["drawerShowsSecond"] is True, (
        "the drawer must move to the document being edited")
    assert result["drawerShowsFirst"] is False, (
        "the editor must never sit under a different document's text")


# --- the Edit control precedes the document body -----------------------------
# (issue_01M17HYDBQ4GPWHPFS56M6QQPH, done-when bullet 1)
#
# The bullet reads: "A full-tier member reading a document on `/memory` reaches
# the editor without scrolling and without the page navigating. Asserted by a
# test on the rendered markup that the Edit control precedes the document body,
# not by reviewing where the control was placed."
#
# **The behaviour shipped in PR #315; the assertion the bullet names did not.**
# A 2026-09-21 status count recorded the bullet as met on the strength of the
# behaviour alone, and a recount against the bullet's own wording on 2026-09-22
# found no test anywhere asserting the ordering. These are that test, on both
# surfaces a member reads a document on.
#
# Ordering is the property because "without scrolling" is: a control placed
# after the body sits below the fold on any document long enough to need the
# drawer, which is every document that opens there.


def test_the_drawer_edit_control_precedes_the_document_body():
    """The drawer is where the editor opens, so it is the surface that matters
    most. Asserted on the painted drawer AFTER the body has landed, since the
    drawer is repainted then and a control that preceded the loading
    placeholder but followed the real content would pass a check taken too
    early."""
    result = _run_editor_js(r"""
kind = 'decision';
render();
openDoc(0);
setTimeout(() => {
  const body = document.querySelector('#doc-drawer .drawer-body').innerHTML;
  console.log(JSON.stringify({
    editAt: body.indexOf('data-drawer-edit'),
    contentAt: body.indexOf('class="eng-doc-content"'),
    loadedBody: body.includes('body of shared/decisions/0001-foo'),
  }));
}, 40);
""")
    assert result["loadedBody"], "sanity: the check ran after the real body landed"
    assert result["editAt"] >= 0, "the drawer must carry an Edit control"
    assert result["contentAt"] >= 0, "the drawer must carry the rendered body"
    assert result["editAt"] < result["contentAt"], (
        "the Edit control must come before the document body, or a member "
        "scrolls past the whole document to reach it")


def test_the_row_edit_control_precedes_the_inline_body():
    """A short document expands inline rather than in the drawer, so its row is
    the other place a member reads. The row's Edit control sits in the title
    line; the expanded body sits in the inset below it."""
    result = _run_editor_js(r"""
kind = 'decision';
render();
clickRow(0);
setTimeout(() => {
  const view = el('view').innerHTML;
  const row = view.slice(view.indexOf('data-i="0"'));
  console.log(JSON.stringify({
    editAt: row.indexOf('data-edit="0"'),
    insetAt: row.indexOf('class="inset"'),
  }));
}, 40);
""")
    assert result["editAt"] >= 0, "the row must carry an Edit control"
    assert result["insetAt"] >= 0, "sanity: the row expanded to show its body"
    assert result["editAt"] < result["insetAt"], (
        "the row's Edit control must come before its expanded body")
