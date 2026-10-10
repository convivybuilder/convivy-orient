"""Render tests for the /archives browse + search page (S1-b, ARCH-349).

The load-bearing checks are the escaping ones: this page renders verbatim
GitHub content (bodies, actors, streams) straight from the control DB, so an
unescaped sink is a stored-XSS hole fed by anyone who can comment on a PR.
"""
from __future__ import annotations

import datetime as dt
import re

import pytest

import control_db
import library_fake
import signals_build


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    # signals_build now reads through library_client rather than control_db
    # directly; this fake serves the Library's exact response shape from the
    # same temp DB every test already seeds (library_fake's docstring).
    library_fake.patch(monkeypatch)
    yield


def _mk(**kw):
    base = dict(id=control_db.new_signal_id(), source="github_review",
                kind="pr_comment", actor="reviewer", ingested_by="github-capture",
                product="vivy", body="a finding")
    base.update(kw)
    return base


def test_empty_db_renders_empty_state(db):
    html = signals_build.live_html()
    assert "<h1>Archives</h1>" in html
    assert "Signal memory" not in html
    assert "No signals captured yet" in html
    assert "— embedded" in html   # total=0 → the division-guard branch, not 0%


def test_breadcrumb_and_title_read_archives_not_signals(db):
    # Round 2 of the /signals→/archives rename (issue_01M1A678RA8CVDNNMZC6777NNA):
    # the nav entry, subnav and route all agreed on "Archives" already, but the
    # page body's own breadcrumb and <title> still said "signals"/"Signals" —
    # the same nav/copy contradiction the rename PR exists to remove, just
    # moved one layer down (reviewer, convivy-orient#259 round 2). Nothing
    # asserted either string before this test.
    html = signals_build.live_html()
    assert "team.convivy.com / archives" in html
    assert "<title>Archives · Orient · Convivy Builder</title>" in html
    assert "team.convivy.com / signals" not in html
    assert "<title>Signals" not in html


def test_detail_breadcrumb_reads_archives(db):
    sig = _mk()
    library_fake.seed_signal(**sig)
    html = signals_build.detail_html(sig["id"], viewer_tier="full")
    assert 'href="/archives">← archives</a>' in html
    assert "signals</a>" not in html


def test_browse_renders_recent_with_counts(db):
    library_fake.seed_signal(**_mk(body="FIRST-BODY", stream="vivy#12",
                                   embedded_at=dt.datetime(2026, 7, 16)))
    library_fake.seed_signal(**_mk(body="SECOND-BODY", product="constance", source="note"))
    html = signals_build.live_html()
    assert "FIRST-BODY" in html and "SECOND-BODY" in html
    assert "vivy#12" in html
    assert '<span class="count">2</span>' in html
    # per-source tallies in the lede + product chips
    assert "github_review 1" in html and "note 1" in html
    assert 'data-prod="vivy"' in html and 'data-prod="constance"' in html
    # embed coverage: 1 of 2 → 50%
    assert "50% embedded" in html


def test_untrusted_github_content_is_escaped(db):
    library_fake.seed_signal(**_mk(body="<script>alert(1)</script>", actor="<img src=x onerror=1>",
                                   stream="<svg onload=1>"))
    html = signals_build.live_html()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x onerror=1>" not in html
    assert "<svg onload=1>" not in html


def test_search_box_is_live_not_disabled(db):
    """Search moved off the disabled placeholder (issue_01M08YKNV185DFJ6832GV8ZY22's
    gap closes here): the input and button carry no `disabled` attribute, and
    the old "Orient needs its own per-viewer wall" copy is gone."""
    html = signals_build.live_html()
    assert "offline while it moves to the Library" not in html
    assert "Search is disabled" not in html
    assert '<input type="search" id="q" placeholder="Search the Archives' in html
    q_tag = re.search(r'<input type="search" id="q"[^>]*>', html).group(0)
    assert "disabled" not in q_tag
    search_button = re.search(r'<button type="submit">Search</button>', html)
    assert search_button is not None


def test_no_awaiting_state_panel_remains(db):
    """The placeholder this page carried while search was disabled is gone —
    search is live, so there is nothing left to await. `class="awaiting-state`
    is the rendered component's own attribute (distinct from the shared
    `.awaiting-state{...}` CSS rule every page ships, used or not)."""
    html = signals_build.live_html()
    assert 'class="awaiting-state' not in html
    assert "WAITING ON ORIENT" not in html


def test_js_wires_search_and_browse_to_the_live_api(db):
    assert "offline while it moves to the Library" not in signals_build._JS
    assert "Search is disabled" not in signals_build._JS
    assert "/api/signals/search?q=" in signals_build._JS
    assert "/api/signals/browse?kind=" in signals_build._JS


def test_footnote_no_longer_claims_search_is_disabled(db):
    html = signals_build.live_html()
    assert "Search is disabled" not in html
    # the footnote still explains what each affordance does, honestly
    assert "search finds the closest matches to a phrase, not a" in html
    assert "full scan" in html


# --- browse by shape: the complete, exact path ------------------------------


def test_shape_browse_section_renders_known_kinds_and_a_free_text_field(db):
    html = signals_build.live_html()
    assert 'id="shapeKindSelect"' in html
    for kind in signals_build._KNOWN_KINDS:
        assert f'<option value="{kind}">{kind}</option>' in html
    assert 'id="shapeKindFree"' in html
    assert 'id="shapeForm"' in html


def test_shape_browse_section_is_visibly_distinct_from_search(db):
    """The spec's hard rule: the two affordances must never blend into one
    control. Confirmed here by markup shape — browse-by-shape carries the
    kind picker, search carries no kind control at all (checked below)."""
    html = signals_build.live_html()
    shape_idx = html.index('id="shapeForm"')
    search_idx = html.index('id="searchForm"')
    assert shape_idx != search_idx
    # the two headings read differently — exact vs. approximate
    assert "Browse by shape" in html
    assert "complete — every row of the kind you pick" in html
    assert "approximate — the closest matches, not a count of what exists" in html


def test_search_is_the_primary_control_ahead_of_both_browse_sections(db):
    """The operator's model: a few known shapes get a browsable section, everything
    else is reached by search — so search is the primary control and must be
    the first of the three a reader meets, not the last (issue_01M15C5CDS28TKXJCA8KAMSBWK).
    That includes the product chips: a bare "search precedes the first card"
    assertion passed on the commit where search sat below the chips as the
    page's second row, so this checks search precedes the chips directly."""
    html = signals_build.live_html()
    chips_idx = html.index('id="chips"')
    search_idx = html.index('id="searchForm"')
    shape_idx = html.index('id="shapeForm"')
    browse_idx = html.index('id="browseSec"')
    assert search_idx < chips_idx < shape_idx < browse_idx
    # moving it up must not merge the two mechanisms into one control
    assert 'class="search-panel card"' in html
    assert 'class="shape card"' in html


# --- search: no kind control, honest wording in the client-side copy -------


def test_search_panel_offers_no_kind_control(db):
    """A kind filter on the search path is a 400 from the Library
    (platform/contracts/library-protocol §3.2) — offering one here would be a
    control the reader cannot act on. Scoped to the search panel's own
    markup, not the whole page, since browse-by-shape legitimately has one."""
    html = signals_build.live_html()
    panel_start = html.index('<div class="search-panel')
    panel_end = html.index('</div>', html.index('id="searchResults"'))
    panel_html = html[panel_start:panel_end]
    assert "shapeKindSelect" not in panel_html
    assert "<select" not in panel_html
    assert 'name="kind"' not in panel_html


def test_search_js_never_renders_a_bare_zero_as_the_store_being_empty(db):
    """Honesty requirement: a zero-result search must read as 'nothing among
    the closest matches,' with a pointer back to browse-by-shape — never as
    'there are none,' which is a claim about the whole store this route
    cannot make (platform/contracts/library-protocol §3.2)."""
    js = signals_build._JS
    assert "Nothing among the closest matches" in js
    assert "not the same as" in js
    assert "Browse by shape" in js
    assert "ranked by similarity — not a count of what exists" in js


def test_search_js_sends_no_kind_or_source_or_product_params(db):
    """The fetch URL is built from `q` alone."""
    js = signals_build._JS
    fetch_line = js[js.index("/api/signals/search"):]
    fetch_call = fetch_line[:fetch_line.index(")")]
    assert "kind=" not in fetch_call
    assert "source=" not in fetch_call
    assert "product=" not in fetch_call


def test_attribute_context_escaping(db):
    # A product with a quote must not break out of data-prod="..." (quote=True).
    evil = 'vivy" onmouseover="alert(1)'
    library_fake.seed_signal(**_mk(product=evil))
    html = signals_build.live_html()
    assert 'onmouseover="alert(1)' not in html
    assert "&quot;" in html or "&#34;" in html


def test_url_ref_links_but_non_http_does_not(db):
    library_fake.seed_signal(**_mk(body="linked", refs={"pr": 9, "url": "https://github.com/convivy/vivy/pull/9"}))
    # a javascript: url must never become an href
    library_fake.seed_signal(**_mk(body="unlinked", refs={"url": "javascript:alert(1)"}))
    html = signals_build.live_html()
    assert 'href="https://github.com/convivy/vivy/pull/9"' in html
    assert 'href="javascript:' not in html


def test_long_body_is_clipped(db):
    library_fake.seed_signal(**_mk(body="x" * 2000))
    html = signals_build.live_html()
    assert "x" * 601 not in html   # clipped to the excerpt length
    assert "…" in html


_REVIEW_STAMP = (
    "<!-- agentctl-review\n"
    "v: 1\n"
    "pr: 320\n"
    "repo: knowledge\n"
    "reviewed-commit: 7842af3bf82cb74fa1522af\n"
    "verdict: APPROVE\n"
    "agent: reviewer\n"
    "run-id: run_9f8e7d\n"
    "-->\n"
)


def test_review_stamp_is_stripped_and_the_prose_after_it_renders(db):
    # A body shaped like a real captured pr_comment review: the stamp
    # followed by the reviewer's actual prose. The excerpt must show the
    # prose, not the machine-readable header (issue_01M15C5CDS28TKXJCA8KAMSBWK).
    # Planted against the OLD behaviour first would fail: the un-stripped
    # excerpt is mostly stamp and includes these header keys verbatim.
    body = _REVIEW_STAMP + "LGTM, one small nit inline on the retry logic."
    library_fake.seed_signal(**_mk(body=body))
    html = signals_build.live_html()
    assert "LGTM, one small nit inline on the retry logic." in html
    assert "agentctl-review" not in html
    assert "reviewed-commit" not in html
    assert "run-id" not in html


def test_stamp_only_body_shows_a_reason_not_a_blank_paragraph(db):
    sig = _mk(body=_REVIEW_STAMP, product="vivy", kind="pr_comment", stream="vivy#320")
    library_fake.seed_signal(**sig)
    html = signals_build.live_html()
    assert "agentctl-review" not in html
    assert '<p class="body"></p>' not in html
    # the card is still a real signal — product, kind, stream, permalink render
    assert 'data-prod="vivy"' in html
    assert "vivy#320" in html
    assert f'/archives/{sig["id"]}' in html
    # and it says WHY the body is empty, rather than leaving it blank
    assert 'class="body body-empty"' in html


def test_html_comment_later_in_a_body_is_preserved(db):
    # The strip is anchored to a LEADING agentctl-review block, not a blind
    # "remove every HTML comment" — a body with no such leading block, but a
    # comment further in, must keep it (escaped like the rest of the body).
    body = "some real prose\n\n<!-- a genuine inline note -->\nmore prose after it"
    library_fake.seed_signal(**_mk(body=body))
    html = signals_build.live_html()
    assert "a genuine inline note" in html


def test_detail_review_stamp_is_stripped_and_the_prose_after_it_renders(db):
    # The detail page (/signals/<id>) renders the raw body through md_to_html
    # with no clip, so it needs the same leading-stamp strip the browse card's
    # excerpt uses — a reader who opens the page directly, rather than via a
    # clipped card's "read the full signal" link, must not see the raw
    # <!-- agentctl-review ... --> block (issue_01M183011ZYEZF11VRPBT6Z1J6).
    body = _REVIEW_STAMP + "LGTM, one small nit inline on the retry logic."
    sig = _mk(body=body)
    library_fake.seed_signal(**sig)
    html = signals_build.detail_html(sig["id"], viewer_tier="full")
    assert "LGTM, one small nit inline on the retry logic." in html
    assert "agentctl-review" not in html
    assert "reviewed-commit" not in html
    assert "run-id" not in html


def test_detail_stamp_only_body_renders_the_no_body_placeholder(db):
    # A stamp-only body strips to empty, so the detail page falls into its
    # existing no-body branch rather than showing raw comment text.
    sig = _mk(body=_REVIEW_STAMP)
    library_fake.seed_signal(**sig)
    html = signals_build.detail_html(sig["id"], viewer_tier="full")
    assert "agentctl-review" not in html
    assert "This signal has no body." in html


def test_detail_body_with_no_stamp_renders_unchanged(db):
    sig = _mk(body="plain prose, no stamp at all")
    library_fake.seed_signal(**sig)
    html = signals_build.detail_html(sig["id"], viewer_tier="full")
    assert "plain prose, no stamp at all" in html


def test_detail_html_comment_later_in_a_body_is_preserved(db):
    # Same rule as the browse card: the strip is anchored to a LEADING
    # agentctl-review block, not a blind "remove every HTML comment" — a body
    # with no such leading block, but a genuine comment further in, keeps it.
    body = "some real prose\n\n<!-- a genuine inline note -->\nmore prose after it"
    sig = _mk(body=body)
    library_fake.seed_signal(**sig)
    html = signals_build.detail_html(sig["id"], viewer_tier="full")
    assert "a genuine inline note" in html


def test_customer_rows_never_render_server_side(db):
    # PR #53 round-1 security finding: the full tier (tiers.json) is BROADER than
    # the signal_customer capability (roles.json) — a full-tier viewer with no
    # roles.json entry gets 403 from the API yet sees this page. So customer
    # rows must never render server-side (neither content nor counts); they are
    # reachable only via search, which the API gates per-identity.
    library_fake.seed_signal(**_mk(body="pilot feedback", privacy_class="customer", source="pilot_note",
                                   product="constance-pilots"))
    library_fake.seed_signal(**_mk(body="internal finding"))
    html = signals_build.live_html()
    assert "pilot feedback" not in html
    assert "internal finding" in html
    assert '<span class="count">1</span>' in html      # total excludes customer rows
    assert "pilot_note" not in html                     # source tallies exclude them
    assert "constance-pilots" not in html               # no chip reveals the product


def test_a_credential_holding_signal_customer_would_leak_the_page(db, monkeypatch):
    """Proves the test ABOVE is testing the wall rather than an accident.

    `signals_build` no longer filters `privacy_class` itself — ADR-0022 gives the
    privacy wall to the Library, so a duplicate Orient-side filter would work
    against that design. The page is therefore safe only while Orient's token
    lacks `signal.customer`, and that fact lives in one JSON key in a gitignored,
    un-code-reviewed `api-tokens.json`: adding `"kind": "system"` to Orient's entry
    grants the capability and starts rendering customer rows to a full-tier viewer.

    This test flips exactly that key in the fake and asserts the page DOES leak, so
    `test_customer_rows_never_render_server_side` above cannot pass for the wrong
    reason — and so the blast radius of a token misprovision is written down in a
    test rather than in a reviewer's head (security review, PR #170)."""
    library_fake.seed_signal(**_mk(body="pilot feedback", privacy_class="customer", source="pilot_note",
                                   product="constance-pilots"))
    library_fake.seed_signal(**_mk(body="internal finding"))
    monkeypatch.setattr(library_fake, "FAKE_CAPABILITIES",
                        {"signal.read", "signal.customer"})
    html = signals_build.live_html()
    assert "pilot feedback" in html, (
        "with signal.customer granted the customer row renders — if this assertion "
        "fails, something else is filtering and the wall test above proves nothing")


def test_chip_counts_match_the_rendered_view(db):
    # PR #53 round-1 blocker: chip counts were whole-table while browse renders
    # only the newest _BROWSE_LIMIT — a chip could say 100, then filtering to it
    # showed zero cards and a false empty message. Counts must derive from the
    # SAME limited list the browse renders (the decisions_build pattern).
    old = dt.datetime(2026, 7, 1)
    new = dt.datetime(2026, 7, 16)
    for i in range(signals_build._BROWSE_LIMIT + 1):   # one more than fits
        library_fake.seed_signal(**_mk(body=f"old-{i}", ingested_at=old))
    library_fake.seed_signal(**_mk(body="newest", product="constance", ingested_at=new))
    html = signals_build.live_html()
    # the view = 1 constance + (limit-1) vivy; each chip counts what it filters to
    assert f'data-prod="vivy" aria-pressed="false">vivy <span class="n">{signals_build._BROWSE_LIMIT - 1}</span>' in html
    assert 'data-prod="constance" aria-pressed="false">constance <span class="n">1</span>' in html


# --- filter state across the shell's refresh control (issue_01M1517CYMAV0GAEB2FYQQKF4N) --


def test_chip_filter_is_stashed_before_the_shells_refresh_and_restored_after():
    # design_system.NAV_CLUSTER_JS fires this event only when its own button
    # triggers the reload — never a bare browser reload — so the page's own
    # filter state survives THAT refresh deliberately, the same
    # stash-then-restore shape room_build.py uses for its own reload.
    assert "convivy:beforerefresh" in signals_build._JS
    assert "sessionStorage" in signals_build._JS
    assert "signals-active-prod" in signals_build._JS


def test_chip_filter_restore_clears_the_stash_so_a_later_visit_is_unaffected():
    js = signals_build._JS
    assert "sessionStorage.removeItem(FILTER_KEY)" in js


def test_search_and_shape_panels_are_not_restored_across_refresh():
    """Deliberate: each is a point-in-time query result, not a standing view
    of the browse list — see the docstring above _JS."""
    m = re.search(r"addEventListener\('convivy:beforerefresh',function\(\)\{(.*?)\}\);",
                  signals_build._JS, re.DOTALL)
    assert m, "beforerefresh handler not found in signals_build._JS"
    handler_body = m.group(1)
    assert "shapeResults" not in handler_body
    assert "searchResults" not in handler_body


def test_capture_form_renders_with_role_gate_tokens(db):
    # S2: the log-a-signal form ships in the page with the server-substituted
    # gates intact — __CAN_CAPTURE__ on the panel (CSS-hidden for non-capture
    # roles; the POST authz gate is the wall) and __WHOAMI__ prefilling actor.
    html = signals_build.live_html()
    assert 'data-can-capture="__CAN_CAPTURE__"' in html
    assert '.capture[data-can-capture="false"]{display:none;}' in html
    # PR #57 round-1 blocker: author display (.capture-form's flex) outranks the
    # UA [hidden]{display:none}, so the form rendered open with a dead toggle.
    # The page-wide !important override must stay.
    assert "[hidden]{display:none!important;}" in html
    assert '<form class="capture-form card" id="captureForm" hidden>' in html
    assert 'name="actor" required maxlength="200" value="__WHOAMI__"' in html
    # both privacy classes are offered — contributor holds signal_customer
    assert 'value="internal" checked' in html and 'value="customer"' in html
    # the upload path is client-side plain text, no multipart
    assert 'id="captureFile" accept=".txt,.md' in html
    # the client clip mirrors the stored-body cap
    assert str(signals_build._UPLOAD_CLIP) in html


def test_capture_datalist_lists_internal_products_only(db):
    # The product datalist covers the WHOLE internal store (not the recent-60
    # window), but a product whose only signals are customer-class must not
    # leak into it — same wall as the chips and tallies.
    old = dt.datetime(2026, 7, 1)
    library_fake.seed_signal(**_mk(body="recent", product="vivy"))
    library_fake.seed_signal(**_mk(body="quiet old product", product="hollis", ingested_at=old))
    library_fake.seed_signal(**_mk(body="pilot", product="constance-pilots", privacy_class="customer"))
    html = signals_build.live_html()
    assert '<option value="vivy">' in html
    assert '<option value="hollis">' in html          # beyond any recency window
    assert "constance-pilots" not in html             # customer-only product hidden


def test_capture_datalist_escapes_product_values(db):
    # products are free text from the API; attribute context must escape quotes
    library_fake.seed_signal(**_mk(product='x"onmouseover="alert(1)'))
    html = signals_build.live_html()
    assert '<option value="x&quot;onmouseover=&quot;alert(1)">' in html
    assert '<option value="x"onmouseover=' not in html


def test_static_fallback_is_empty_state(tmp_path, monkeypatch):
    # main() writes the fallback WITHOUT touching the DB.
    monkeypatch.setattr(signals_build, "SITE", tmp_path / "site")
    signals_build.main()
    out = (tmp_path / "site" / "signals.html").read_text()
    assert "Live view unavailable" in out


def test_nav_marks_current_page(db):
    # the ops pages cross-link each other via the shared shell's sub-strip
    # (ARCH-355; the crosslinks row it replaces was the operator 2026-07-16). Signals
    # cross-links via Archives (epic issue_01M0EHQHQ7M39K811XKX1BAG98), which
    # used to also carry Notes & wrap-ups, the ARCH log and Market — all three
    # trimmed off the strip (the operator, 2026-08-26; see test_ops_reports_build.py and
    # test_design_system.py), leaving Signals and Briefings.
    html = signals_build.live_html()
    assert 'href="/archives" aria-current="page"' in html
    assert 'href="/reference/briefings"' in html
    assert 'href="/reference/arch-log"' not in html
    assert 'href="/market"' not in html
    # ARCH-362: Decisions is on the Engineering strip, not this one.
    assert 'href="/decisions"' not in html


# --- the per-signal permalink page -------------------------------------------
# The load-bearing test in this block is the customer-class one. It pins a
# privacy boundary that a later "why does this 404 on a real id?" edit could
# widen in one line, and the widened version would look correct in every other
# test here.

def test_detail_renders_the_full_body_not_the_excerpt(db):
    long_body = "para one.\n\n" + ("x" * (signals_build._BODY_CLIP + 500))
    sig = _mk(body=long_body)
    library_fake.seed_signal(**sig)
    sid = sig["id"]
    html = signals_build.detail_html(sid, viewer_tier="full")
    assert html is not None
    assert "x" * (signals_build._BODY_CLIP + 500) in html   # nothing clipped
    assert " …" not in html                                  # no excerpt ellipsis
    assert sid in html                                       # the id is displayed


def test_detail_returns_none_for_unknown_id(db):
    assert signals_build.detail_html("sig_does_not_exist", viewer_tier="full") is None


def test_detail_returns_none_for_customer_class(db):
    """A customer-class signal must not render server-side, and must be
    indistinguishable from a missing one — the full tier is broader than the
    signal_customer capability, so a viewer the API would 403 must not read it
    here, and a distinguishable answer would still confirm the id exists."""
    sig = _mk(body="a customer call", privacy_class="customer")
    library_fake.seed_signal(**sig)
    sid = sig["id"]
    assert signals_build.detail_html(sid, viewer_tier="full") is None
    assert signals_build.detail_html("sig_does_not_exist", viewer_tier="full") is None  # same answer


def test_detail_escapes_untrusted_fields(db):
    sig = _mk(actor='<img src=x onerror="alert(1)">', stream='s"onmouseover="x',
              body="plain")
    library_fake.seed_signal(**sig)
    sid = sig["id"]
    html = signals_build.detail_html(sid, viewer_tier="full")
    assert "<img src=x onerror=" not in html
    assert '&lt;img src=x' in html


def test_detail_body_renders_markdown_not_raw_symbols(db):
    sig = _mk(body="a **bold** finding")
    library_fake.seed_signal(**sig)
    sid = sig["id"]
    html = signals_build.detail_html(sid, viewer_tier="full")
    assert "<strong>bold</strong>" in html
    assert "**bold**" not in html


def test_detail_body_drops_raw_html_from_an_untrusted_body(db):
    # bodies are verbatim GitHub content; md_to_html must not pass markup through
    sig = _mk(body="before\n\n<script>alert(1)</script>\n\nafter")
    library_fake.seed_signal(**sig)
    sid = sig["id"]
    html = signals_build.detail_html(sid, viewer_tier="full")
    assert "<script>alert(1)</script>" not in html
    # and the surrounding text SURVIVES — without this, a renderer that erased
    # the whole body would pass the assertion above while destroying the record
    assert "before" in html and "after" in html


def test_browse_card_links_to_the_permalink(db):
    sig = _mk(body="short")
    library_fake.seed_signal(**sig)
    sid = sig["id"]
    html = signals_build.live_html()
    assert f'href="/archives/{sid}"' in html
    assert sid in html   # the id is visible and copyable off the browse page


def test_browse_card_offers_the_full_read_only_when_clipped(db):
    short, long = _mk(body="short one"), _mk(body="y" * (signals_build._BODY_CLIP + 10))
    library_fake.seed_signal(**short)
    library_fake.seed_signal(**long)
    html = signals_build.live_html()
    # one card was clipped, so exactly one "read the full signal" offer
    assert html.count("read the full signal") == 1


def test_detail_body_has_no_prewrap_from_the_browse_rule(db):
    """`.ops .body` is pre-wrap for the escaped browse excerpts. The detail body
    is real markup, where pre-wrap renders every source newline as a blank line
    on top of the tags' own spacing."""
    sig = _mk(body="one\n\ntwo")
    library_fake.seed_signal(**sig)
    sid = sig["id"]
    html = signals_build.detail_html(sid, viewer_tier="full")
    # tolerant of whitespace/formatting in the CSS string — the invariant is that
    # a rule scoped to .ops-detail sets the body back to normal, not how it is typed
    assert re.search(r"\.ops-detail\s+\.body\s*\{[^}]*white-space\s*:\s*normal", html)
    # and the browse rule is untouched, so cards still render their escaped text
    assert re.search(r"\.ops\s+\.body\s*\{[^}]*white-space\s*:\s*pre-wrap", html)


def test_detail_refuses_a_non_full_tier_caller(db):
    """The tier gate lives in the route, so a later caller that forgot it would
    serve every internal signal in the store to any authenticated viewer. The
    precondition is checked here so it cannot be forgotten, rather than stated in
    a docstring the caller may never read."""
    sig = _mk(body="internal only")
    library_fake.seed_signal(**sig)
    sid = sig["id"]
    for tier in ("contractor", "", None):
        with pytest.raises(ValueError, match="full-tier"):
            signals_build.detail_html(sid, viewer_tier=tier)


def test_detail_requires_the_tier_to_be_passed_at_all(db):
    """Keyword-only and required: a caller cannot omit it and get a default."""
    with pytest.raises(TypeError):
        signals_build.detail_html("sig_whatever")


@pytest.mark.parametrize("path", ["detail", "facets", "acks"])
def test_every_signal_path_moves_with_the_capability_not_just_the_browse_page(db, monkeypatch, path):
    """The other three reads, each flipped and observed rather than inferred.

    Round 2 routed all four fake paths through one `_may_see` helper, but only the
    browse page had a test that flips `signal.customer` and watches the wall move.
    The other three were correct BY INFERENCE from sharing that helper — which is
    the same "asserts the outcome instead of performing the check" gap the round
    set out to close, one level up: a future edit that inlines the old comparison
    back into one of these fakes would ship with every test still green (security
    review, PR #170 round 3).

    So each path is exercised in both capability states. Detail and acks must
    collapse a customer row to absent; facets must not count it in `total`."""
    library_fake.seed_signal(**_mk(id="sig_cust", body="pilot feedback", privacy_class="customer",
                                   source="pilot_note", product="constance-pilots"))
    library_fake.seed_signal(**_mk(id="sig_int", body="internal finding"))
    # The ack row matters: `fake_signal_acks` answers which of the ids given are
    # ACKED, so without one the customer id is absent for a reason that has
    # nothing to do with the wall — and the test would pass while proving
    # nothing. Seeding it makes the capability the only variable.
    library_fake.seed_ack(signal_id="sig_cust", identity="operator@example.com")

    def observe():
        if path == "detail":
            return library_fake.fake_get_signal("sig_cust") is not None
        if path == "facets":
            return library_fake.fake_signal_facets().total == 2
        return "sig_cust" in library_fake.fake_signal_acks(
            ["sig_cust"], acting_human="operator@example.com")

    assert observe() is False, f"{path} exposed a customer row without signal.customer"
    monkeypatch.setattr(library_fake, "FAKE_CAPABILITIES",
                        {"signal.read", "signal.customer"})
    assert observe() is True, (
        f"{path} still hid the customer row WITH signal.customer granted — it is not "
        f"consulting the capability, so its wall proves nothing")
