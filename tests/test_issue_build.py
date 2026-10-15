"""Render tests for the three pages the issue queue became (the operator, 2026-08-29 —
issue_01M1518ZFBGHAWC4Z46W893H2S): Epics (`live_html`), Corrections
(`live_html_corrections`) and Course (`live_html_course`).

A plain `_mk(...)` item (type="bug", no parent_id) is a CORRECTION under the
new split — `_is_correction` — so most of the card-level behaviour this file
pins (escaping, the claim banner, the thread, the resolve actions, the
outcome banners) is exercised through `live_html_corrections()` now; it used
to be exercised through the single flat `live_html()`. `_card` itself did not
change, so what these tests assert did not change — only which page renders
the card being asserted on.

Mirrors test_decisions_build.py where it still applies: the active-queue
ordering (unworked before the crew-drain outcome, oldest-first within each),
the outcome banner (pr-open) and its link, the "Won't do" human action
(worker-claim hidden), the recently-resolved strip (kept on Corrections),
escaping of untrusted titles/bodies/refs, the Log-an-issue form + its
__CAN_LOG_ISSUE__ gate, and the static empty-state fallbacks.
"""
from __future__ import annotations

import datetime as dt
import html as html_lib
import re

import pytest

import issue_build
import control_db
import library_client
import library_fake
import model_catalog
from models import utcnow as models_utcnow


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    # issue_build now reads through library_client rather than control_db
    # directly; this fake serves the Library's exact response shape from the
    # same temp DB every test already seeds (library_fake's docstring).
    library_fake.patch(monkeypatch)
    yield


@pytest.fixture(autouse=True)
def _no_courses(monkeypatch):
    """Default to "no live Course for any project" so a test that doesn't care
    about Course need not know `live_html_course` calls `list_courses` —
    `library_fake` has no course store of its own (courses are Orient-wide,
    not issue-queue-specific), same direct-monkeypatch convention
    `test_chart_house_build.py` uses for its own course reads. Course-specific
    tests below override this."""
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [])


def _mk(**kw):
    base = dict(id=control_db.new_issue_id(), product="vivy", title="t", body="",
                created_by="overnight-orchestrator")
    base.update(kw)
    return base


def test_empty_epics_page_renders_allclear(db):
    html = issue_build.live_html()
    assert "Epics" in html
    assert "no open epics" in html.lower()


def test_empty_corrections_page_renders_allclear(db):
    html = issue_build.live_html_corrections()
    assert "Corrections" in html
    assert "no corrections queued" in html.lower()


def test_the_header_count_is_not_truncated_at_the_librarys_page_clamp(db):
    """748 issue_items exist on the live database and the Library clamps a
    page to 200 rows; a builder that reads one page renders a queue that looks
    right while quietly dropping everything past row 200. This seeds MORE than
    that clamp directly into the DB the fake Library reads from and asserts
    the header count — and the project's own chip count — describe the whole
    set, not `len(one page)`. `test_library_client.py` pins the paging itself;
    this pins that the builder actually calls it rather than a single-page
    read. All 250 land in one product (`_mk`'s default), so the per-project
    cap (`_QUEUE_LIMIT`) binds and its own truncation note is checked too."""
    total = 250
    for n in range(total):
        library_fake.seed_issue(**_mk(title=f"issue {n}", state="open",
                  created_at=dt.datetime(2026, 7, 1) + dt.timedelta(seconds=n)))
    html = issue_build.live_html_corrections()
    assert f'id="waitCount">{total}<' in html
    assert f'<span class="n">{total}</span>' in html  # the project's own section count
    # the per-project queue is unrendered past _QUEUE_LIMIT cards, but the
    # count naming that gap must itself be the true total, not the cap.
    assert f"showing {issue_build._QUEUE_LIMIT} —" in html
    assert f"{total - issue_build._QUEUE_LIMIT} more not rendered" in html


def test_active_unworked_before_outcomes_oldest_first(db):
    # New drain-model ordering (ARCH-359 step 2b): the unworked group (open +
    # claimed) sorts before the crew-drain outcome group (pr-open), oldest-first
    # within each group. `claimed` is folded into the unworked group by age (no
    # more forced open-before-claimed) and the worker-claim is hidden from the
    # human view. All three items are plain bugs with no parent, so all three
    # are corrections in the same product.
    library_fake.seed_issue(**_mk(title="PROPEN-0600", state="pr-open", created_at=dt.datetime(2026, 7, 16, 6),
              refs={"pr": 5, "url": "https://github.com/convivy/vivy/pull/5"}))
    library_fake.seed_issue(**_mk(title="OPEN-0800", state="open", created_at=dt.datetime(2026, 7, 16, 8)))
    library_fake.seed_issue(**_mk(title="CLAIMED-1000", state="claimed", claimed_by="agent-x",
              created_at=dt.datetime(2026, 7, 16, 10)))
    html = issue_build.live_html_corrections()
    # unworked (oldest-first) then outcome (oldest-first)
    assert (html.index("OPEN-0800") < html.index("CLAIMED-1000")
            < html.index("PROPEN-0600"))
    assert "claimed by" not in html  # worker-claim hidden from the human view
    assert 'id="waitCount">3<' in html


def test_outcome_banner_links(db):
    library_fake.seed_issue(**_mk(title="PR-BUG", state="pr-open",
              refs={"pr": 42, "url": "https://github.com/convivy/vivy/pull/42"}))
    html = issue_build.live_html_corrections()
    assert "PR open" in html
    assert '<a href="https://github.com/convivy/vivy/pull/42"' in html and "PR #42" in html


def test_human_action_is_wontdo_not_claim(db):
    library_fake.seed_issue(**_mk(title="OPEN-BUG", state="open"))
    html = issue_build.live_html_corrections()
    assert "Won't do" in html or "Won&#x27;t do" in html
    assert 'data-act="claim"' not in html   # no Claim button on the human surface
    assert 'data-resolution="wontfix"' in html


def test_resolved_strip_shows_resolution_and_note(db):
    resolved_at = dt.datetime(2026, 7, 16, 9, 0, 0)
    library_fake.seed_issue(**_mk(title="FIXED-ITEM", state="fixed", resolution="fixed",
              resolved_by="operator@example.com", resolved_at=resolved_at,
              outcome_note="shipped as PR #920"))
    library_fake.seed_issue(**_mk(title="WONTFIX-ITEM", state="wontfix", resolution="wontfix",
              resolved_by="operator@example.com", resolved_at=resolved_at))
    html = issue_build.live_html_corrections()
    assert "Recently resolved" in html
    assert "FIXED-ITEM" in html and "shipped as PR #920" in html
    assert "WONTFIX-ITEM" in html
    assert "Won&#x27;t do" in html or "Won&#39;t do" in html


def test_redirected_renders_distinctly_from_wontfix(db):
    # A redirected issue means the concern is still live — the proposed repair
    # was wrong, not the concern. It must be visually distinct from wontfix
    # ("we decided not to") so they cannot be misread as the same verdict.
    resolved_at = dt.datetime(2026, 7, 17, 10, 0, 0)
    library_fake.seed_issue(**_mk(title="REDIRECTED-ITEM", state="redirected", resolution="redirected",
              resolved_by="overnight-orchestrator", resolved_at=resolved_at,
              outcome_note="refactored away; concern may still live in auth module"))
    library_fake.seed_issue(**_mk(title="WONTFIX-ITEM2", state="wontfix", resolution="wontfix",
              resolved_by="overnight-orchestrator", resolved_at=resolved_at))
    html = issue_build.live_html_corrections()
    assert "REDIRECTED-ITEM" in html
    assert "Redirected" in html
    # CSS class is distinct from wontfix
    assert 'status redirected' in html
    assert 'status wontfix' in html
    # The two verdict labels are distinct — redirected means the concern is still live;
    # wontfix means we decided not to act.
    assert "Redirected" in html and ("Won&#x27;t do" in html or "Won&#39;t do" in html)


def test_agent_free_text_is_escaped(db):
    library_fake.seed_issue(**_mk(title="<script>alert(1)</script>", body="<img src=x onerror=1>",
              refs={"pr": "<b>123</b>"}))
    html = issue_build.live_html_corrections()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x onerror=1>" not in html
    assert "&lt;b&gt;123&lt;/b&gt;" in html


def test_attribute_context_escaping(db):
    evil = 'vivy" onmouseover="alert(1)'
    library_fake.seed_issue(**_mk(product=evil, title="attr-ctx"))
    html = issue_build.live_html_corrections()
    assert 'onmouseover="alert(1)' not in html
    assert "&quot;" in html or "&#34;" in html


def test_url_ref_renders_a_link_but_text_ref_does_not(db):
    library_fake.seed_issue(**_mk(title="withrefs", refs={"pr": "https://github.com/convivy/vivy/pull/9",
                                      "file": "server.py"}))
    html = issue_build.live_html_corrections()
    assert 'href="https://github.com/convivy/vivy/pull/9"' in html
    assert "file: server.py" in html


def test_ref_url_with_control_character_renders_no_link(db):
    """A bare `str.startswith(("http://", "https://"))` check accepted
    `"http://\\njavascript:alert(1)"`, because `html.escape` does not encode a
    raw newline — the value would land in the href verbatim. refs is
    agent-written, so this is the prompt-injection surface PR #141 hardened
    for `overnight_build.safe_url`; `_ref_chips` shares the same allowlist now
    (`url_safety.safe_http_url`)."""
    library_fake.seed_issue(**_mk(title="withrefs", refs={"pr": "http://\njavascript:alert(1)"}))
    html = issue_build.live_html_corrections()
    assert '<a class="ref"' not in html
    assert 'href="http://' not in html


def test_ref_url_with_unbalanced_ipv6_bracket_renders_no_link():
    """`urlparse` raises `ValueError: Invalid IPv6 URL` on an unbalanced
    bracket instead of just failing the scheme/netloc check — refs is
    agent-written, so an agent writing this value took down the issue page
    instead of having its link rejected like any other bad URL.

    The value also carries an HTML-significant character so this test pins
    the escaping, not just the no-link behavior: `html.escape("http://[::1")`
    is a no-op (no `&<>"'` in it), so a plain unbalanced-bracket value passes
    identically whether `_ref_chips` renders `escape(val)` or the raw `val` —
    it would not catch a future edit that drops the escape and reopens the
    XSS bypass PR #141 closed."""
    html = issue_build._ref_chips({"pr": "http://[::1<b>"})
    assert '<a class="ref"' not in html
    assert "&lt;b&gt;" in html
    assert "<b>" not in html


def test_outcome_banner_pr_url_with_control_character_renders_no_link(db):
    """Same bypass, same fix, in `_outcome_banner`'s PR link — a second,
    independent call site over the same agent-written `refs` field."""
    library_fake.seed_issue(**_mk(title="PR-BUG", state="pr-open",
              refs={"pr": 42, "url": "http://\njavascript:alert(1)"}))
    html = issue_build.live_html_corrections()
    # **ASSERT ON THE href, NOT ON THE STRING'S ABSENCE.** `_outcome_banner`
    # emits no link at all here — it falls back to the bare `PR #42` label — but
    # the same item's `refs` also reaches `_ref_chips`, which deliberately shows
    # a refused URL as inert escaped text so a reader can see what was filed. So
    # the substring IS in the page, harmlessly, and `"javascript:" not in html`
    # fails on the display rather than on a hole. The property that matters is
    # that nothing carries it in an attribute, which is the form the two
    # pre-existing tests in this repo already assert (`href="javascript:` at the
    # markdown-render test, and the `<a class="ref"` check above).
    assert 'href="http://' not in html
    assert "javascript" not in "".join(re.findall(r'href="([^"]*)"', html))
    assert "PR open" in html
    assert "PR #42" in html
    assert '<a href=' not in html.split("PR open")[1].split("</div>")[0]


def test_product_chips_reflect_open_and_claimed(db):
    library_fake.seed_issue(**_mk(product="vivy", title="v1"))
    library_fake.seed_issue(**_mk(product="vivy", title="v2"))
    library_fake.seed_issue(**_mk(product="constance", title="c1"))
    html = issue_build.live_html_corrections()
    assert 'data-prod="vivy"' in html and 'data-prod="constance"' in html


def test_page_carries_log_issue_form_and_gate(db):
    library_fake.seed_issue(**_mk(title="open item"))
    for html in (issue_build.live_html(), issue_build.live_html_corrections()):
        assert "Log an issue" in html
        assert 'data-can-log="__CAN_LOG_ISSUE__"' in html
        assert '.capture[data-can-log="false"]' in html


def test_static_fallback_is_empty_state(tmp_path, monkeypatch):
    monkeypatch.setattr(issue_build, "SITE", tmp_path / "site")
    issue_build.main()
    for name in ("issues.html", "corrections.html", "course.html"):
        out = (tmp_path / "site" / name).read_text()
        assert "Live view unavailable" in out


def test_nav_marks_current_page(db):
    """Each page marks itself current and renders the CHART HOUSE sub-strip
    (renamed from Engineering, epic issue_01M0EHQHQ7M39K811XKX1BAG98) with
    the three-way Epics/Corrections/Course split.

    ARCH-361 moved the queue out of the Memory area into Engineering, so its
    siblings are the engineering pages; Archives (Signals' new area) is
    deliberately absent from the SUB-strip, which is what pins the move.
    (/signals still appears once, as the primary rail's own Archives link —
    that link is unconditional now that Archives is top-level, so this checks
    the sub-strip specifically rather than the whole page.) Decisions used to
    sit on this strip too (ARCH-362); the decision plane is gone (2026-08-18).
    """
    for html, current in ((issue_build.live_html(), "/engineering/issues"),
                          (issue_build.live_html_corrections(), "/engineering/corrections"),
                          (issue_build.live_html_course(), "/engineering/course")):
        assert f'href="{current}" aria-current="page"' in html
        assert 'href="/engineering/issues"' in html
        assert 'href="/engineering/corrections"' in html
        assert 'href="/engineering/course"' in html
        assert 'href="/engineering/merge-queue"' in html
        # Roster moved to Crews' sub-nav (issue_01M08YM8MTNSTAEG1NV7E2GYRV,
        # 2026-08-21) — no longer a Chart House sibling.
        assert 'href="/engineering/roster"' not in html
        assert 'aria-label="Archives navigation"' not in html


# --- the claim banner (ARCH-361 Part B) ---------------------------------------
#
# ARCH-359 deliberately hid `claimed` as a worker-internal primitive. Part B
# reverses that: a claim now says a working session holds this issue and the
# overnight drain will leave it alone, which is precisely what a human reading
# the queue needs in order to trust it.


def test_a_live_claim_shows_who_holds_it_and_when_it_frees(db):
    library_fake.seed_issue(**_mk(title="held", state=control_db.ISSUE_CLAIMED,
              claimed_by="co-vivy-0930", claimed_at=control_db.utcnow()))
    html = issue_build.live_html_corrections()
    assert "In flight" in html
    assert "co-vivy-0930" in html
    assert "frees in" in html


def test_an_expired_claim_is_called_out_as_reclaimable(db):
    """A dead session's claim must not read as live work — that would tell the operator
    something is in hand when nothing is."""
    library_fake.seed_issue(**_mk(title="stranded", state=control_db.ISSUE_CLAIMED, claimed_by="co-dead",
              claimed_at=control_db.utcnow() - library_fake._CLAIM_TTL - dt.timedelta(minutes=1)))
    html = issue_build.live_html_corrections()
    assert "Claim expired" in html
    assert "reclaimable by the next drain" in html
    assert "In flight" not in html


def test_an_open_issue_shows_no_claim_banner(db):
    library_fake.seed_issue(**_mk(title="unclaimed"))
    html = issue_build.live_html_corrections()
    assert "In flight" not in html and "Claim expired" not in html


def test_a_claim_owner_is_escaped(db):
    """claimed_by is caller-supplied now, so it is untrusted text on the page."""
    library_fake.seed_issue(**_mk(title="xss", state=control_db.ISSUE_CLAIMED,
              claimed_by="<script>alert(1)</script>", claimed_at=control_db.utcnow()))
    html = issue_build.live_html_corrections()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_a_claimed_issue_with_no_timestamp_reads_as_expired(db):
    """A corrupt row must not render an expiry computed from None."""
    library_fake.seed_issue(**_mk(title="corrupt", state=control_db.ISSUE_CLAIMED,
              claimed_by="co-a", claimed_at=None))
    html = issue_build.live_html_corrections()
    assert "Claim expired" in html


# --- issue bodies are Markdown, not plain text --------------------------------
#
# The operator: "fix the markdown that is printing to the issues". Bodies were escaped
# wholesale, so a reviewer finding pasted as GitHub-Flavored Markdown rendered
# its own syntax: `**Reviewer finding:**` printed the asterisks, and every
# `code` reference printed backticks — on the one page whose job is making a
# finding readable. Rendering now goes through the shared safe CommonMark engine
# (markdown_render), which escapes raw HTML and drops unsafe link schemes, so
# this does not relax the module's "all free text is escaped" rule.


def _seed_issue(**kw):
    library_fake.seed_issue(**_mk(**kw))


def test_issue_body_renders_bold_markdown(db):
    _seed_issue(body="**Reviewer finding:** the thing is broken")
    html = issue_build.live_html_corrections()
    assert "<strong>Reviewer finding:</strong>" in html
    assert "**Reviewer finding:**" not in html, "markdown printed literally"


def test_issue_body_renders_inline_code(db):
    _seed_issue(body="see `app/services/alert_events_service.py:40`")
    html = issue_build.live_html_corrections()
    assert "<code>app/services/alert_events_service.py:40</code>" in html


def test_issue_body_markdown_is_xss_safe(db):
    """html=False is the whole story here — an issue body is untrusted text that
    agents and GitHub capture write, so raw HTML must never survive the render."""
    _seed_issue(body='<img src=x onerror="alert(1)"> and <script>alert(2)</script>')
    html = issue_build.live_html_corrections()
    body = html.split('class="body"', 1)[1].split("</div>", 1)[0]
    # Assert the PROPERTY (no live tag), not a substring. The escaped output
    # contains the text "onerror=" inside &quot;-escaped entities, so a naive
    # `"onerror=" not in html` fails on a perfectly safe render — and would push
    # someone to "fix" working code.
    assert "<img" not in body and "<script" not in body
    assert "&lt;img" in body and "&lt;script&gt;" in body


def test_issue_body_drops_unsafe_link_schemes(db):
    _seed_issue(body="[click](javascript:alert(1))")
    html = issue_build.live_html_corrections()
    body = html.split('class="body"', 1)[1].split("</div>", 1)[0]
    # markdown-it's link validator refuses the scheme, so it never becomes an
    # anchor — the text survives verbatim, which is harmless. The property is
    # "no href carrying the scheme", not "the string is absent".
    assert 'href="javascript:' not in body
    assert "<a " not in body


def test_resolved_duplicate_shows_which_issue_it_duplicates(db):
    """A resolved-duplicate row that says only "Duplicate" tells the reader the
    one thing they already knew."""
    _seed_issue(id="issue_01ORIGINAL", title="the original")
    _seed_issue(state="duplicate", resolution="duplicate", title="a dupe",
                resolved_by="operator@example.com", resolved_at=models_utcnow(),
                refs={"duplicate_of": "issue_01ORIGINAL"})
    html = issue_build.live_html_corrections()
    assert "issue_01ORIGINAL" in html


def test_duplicate_button_asks_which_issue(db):
    """The control that makes the link possible must exist on the page."""
    _seed_issue(body="x")
    html = issue_build.live_html_corrections()
    assert 'class="dupeid"' in html
    assert "Duplicate of" in html


# --- the thread renders on the page (ARCH-373) --------------------------------


def _seed_with_thread(*comments, **kw):
    iid = library_fake.seed_issue(**_mk(**kw))
    for n, (kind, actor, body) in enumerate(comments):
        library_fake.seed_issue_comment(item_id=iid, id=f"cmt_{n}", actor=actor,
                                        kind=kind, body=body)


def test_the_diagnosis_leads_the_thread(db):
    """The diagnosis is the contract bug-fixer consumes. Burying it under three
    later notes would make the page agree with the API only by accident."""
    _seed_with_thread(
        ("note", "operator@example.com", "seen this before"),
        ("question", "bug-investigator", "which branch?"),
        ("diagnosis", "bug-investigator", "root cause: SHARED-FIXTURE-STATE"),
        body="x")
    html = issue_build.live_html_corrections()
    body = html.split('class="thread"', 1)[1]
    assert body.index("SHARED-FIXTURE-STATE") < body.index("seen this before")
    assert body.index("SHARED-FIXTURE-STATE") < body.index("which branch?")


def test_thread_bodies_render_markdown_safely(db):
    """An agent writes these, and a diagnosis is mostly code references — so they
    render as Markdown, through the same safe engine issue bodies use."""
    _seed_with_thread(("diagnosis", "bug-investigator",
                       "the **root cause** is `_per_test_db_engine` disposal"), body="x")
    html = issue_build.live_html_corrections()
    assert "<strong>root cause</strong>" in html
    assert "<code>_per_test_db_engine</code>" in html


def test_a_thread_comment_cannot_inject_html(db):
    _seed_with_thread(("note", "x", '<img src=x onerror="alert(1)">'), body="x")
    html = issue_build.live_html_corrections()
    thread = html.split('class="thread"', 1)[1].split("</div></div>", 1)[0]
    assert "<img" not in thread
    assert "&lt;img" in thread


def test_an_issue_with_no_thread_renders_nothing_extra(db):
    _seed_issue(body="plain issue")
    assert 'class="thread"' not in issue_build.live_html_corrections()


def test_a_long_thread_is_bounded_but_says_so(db):
    """The API caps a thread at 200 and a body may be ~100KB, so rendering every
    one is a multi-megabyte page. A reader who cannot tell the thread was
    truncated will assume they saw all of it, so the elision is stated."""
    many = [("note", "agent", f"note number {i}") for i in range(20)]
    _seed_with_thread(*many, body="x")
    html = issue_build.live_html_corrections()
    assert "note number 19" in html          # newest kept
    assert "note number 0" not in html       # oldest dropped
    assert "earlier comments not shown" in html


def test_every_diagnosis_survives_truncation(db):
    """The diagnosis is the contract. Truncating it to save bytes would defeat
    the point of the table."""
    comments = [("note", "a", f"filler {i}") for i in range(20)]
    comments.append(("diagnosis", "bug-investigator", "THE-ROOT-CAUSE"))
    _seed_with_thread(*comments, body="x")
    html = issue_build.live_html_corrections()
    assert "THE-ROOT-CAUSE" in html


# --- the headline count is a count, not the render cap -------------------------
#
# The queue page renders at most _QUEUE_LIMIT cards for page weight, and the
# count was being derived from that capped list in three separate places: the
# server's len(active), the per-product chip totals, and the client-side
# recount() that measures rendered cards on load. Past the cap all three read
# exactly _QUEUE_LIMIT and stop moving, so a day of draining looks like no
# progress at all. Every test below is over a queue LARGER than the cap, because
# under it the bug is invisible.


def _fill_active(n, product="vivy", state="open"):
    for i in range(n):
        library_fake.seed_issue(**_mk(title=f"ISSUE-{i:04d}", product=product, state=state,
                  created_at=dt.datetime(2026, 7, 16) + dt.timedelta(minutes=i)))


def test_headline_count_is_the_true_total_not_the_render_cap(db):
    over = issue_build._QUEUE_LIMIT + 7
    _fill_active(over)
    html = issue_build.live_html_corrections()
    assert f'id="waitCount">{over}<' in html, \
        "the headline must be the DB count; deriving it from the capped render list pins it at the cap"
    assert f'id="waitCount">{issue_build._QUEUE_LIMIT}<' not in html


def test_render_stays_capped_even_though_the_count_does_not(db):
    """The cap is deliberate — bodies are large and rendering every card makes a
    multi-megabyte page. Fixing the count must not quietly remove it. All
    items land in one product (`_fill_active`'s default), so the per-project
    cap binds exactly as the old single global cap did."""
    _fill_active(issue_build._QUEUE_LIMIT + 7)
    html = issue_build.live_html_corrections()
    assert html.count('class="card"') <= issue_build._QUEUE_LIMIT


def test_truncation_is_stated_on_the_page(db):
    over = issue_build._QUEUE_LIMIT + 7
    _fill_active(over)
    html = issue_build.live_html_corrections()
    assert f"showing {issue_build._QUEUE_LIMIT} —" in html
    assert "7 more not rendered" in html


def test_client_recount_adds_back_the_unrendered_remainder(db):
    """recount() runs on load and measures rendered cards. Without the hidden
    remainder it overwrites the server's honest headline with the cap — the fix
    would look correct in the HTML and still be wrong in the browser. `hidden`
    is now the SUM of every project's own cap (one product here, so still 7)."""
    _fill_active(issue_build._QUEUE_LIMIT + 7)
    html = issue_build.live_html_corrections()
    assert 'data-hidden="7"' in html
    assert "total+hiddenCount" in html


def test_per_product_chip_counts_are_not_short_when_the_queue_is_capped(db):
    """The chips were counted off the capped list too, so a product whose issues
    fell past the cap silently showed a smaller number with nothing saying so.
    Both counts here are under _QUEUE_LIMIT, so neither project's own queue is
    capped — the chip and the per-project section header must still agree."""
    _fill_active(80, product="vivy")
    _fill_active(40, product="sous-chef")
    html = issue_build.live_html_corrections()
    assert ">vivy <span class=\"n\">80</span>" in html
    assert ">sous-chef <span class=\"n\">40</span>" in html


def test_under_the_cap_nothing_changes(db):
    """The must-still-work direction: below _QUEUE_LIMIT the count already
    matched the render, and no truncation note should appear."""
    _fill_active(5)
    html = issue_build.live_html_corrections()
    assert 'id="waitCount">5<' in html
    assert "not rendered" not in html
    assert 'data-hidden="0"' in html


# --- comment fetching is capped once per page, not per epic/product ------------
# Round-2 review (issue_01M1518ZFBGHAWC4Z46W893H2S): the docstring's claim that
# capping each epic's children at _QUEUE_LIMIT gave "the same bound the old
# flat queue's own single cap gave its comment fan-out" was false — the old
# cap applied BEFORE any comment fetch, so it bounded the whole page at 100
# Library round trips; a per-epic (or per-product) cap multiplies by epic (or
# product) count instead. Every test below seeds MORE comment-eligible items
# than _QUEUE_LIMIT, spread across several epics/products, because under that
# total the old and new code fetch the same number of comments either way.


def test_epics_page_comment_fetch_is_capped_once_per_page_not_per_epic(db, monkeypatch):
    n_epics = 5
    children_per_epic = 30  # 5*30 + 5 = 155 comment-eligible items, over _QUEUE_LIMIT (100)
    for e in range(n_epics):
        epic_id = library_fake.seed_issue(**_mk(title=f"EPIC-{e}", type="epic", state="open"))
        for c in range(children_per_epic):
            library_fake.seed_issue(**_mk(title=f"CHILD-{e}-{c}", parent_id=epic_id, state="open"))
    calls = []
    orig = library_client.get_issue_item
    monkeypatch.setattr(library_client, "get_issue_item",
                        lambda *a, **k: (calls.append(1), orig(*a, **k))[1])
    issue_build.live_html()
    assert 0 < len(calls) <= issue_build._QUEUE_LIMIT, (
        f"expected at most {issue_build._QUEUE_LIMIT} Library round trips for "
        f"comments, got {len(calls)}")


def test_corrections_page_comment_fetch_is_capped_once_per_page_not_per_product(db, monkeypatch):
    for p in range(3):
        for i in range(40):  # 3*40 = 120 comment-eligible items, over _QUEUE_LIMIT (100),
            library_fake.seed_issue(**_mk(  # with no single product anywhere near the cap alone
                title=f"CORR-{p}-{i}", product=f"prod{p}", state="open"))
    calls = []
    orig = library_client.get_issue_item
    monkeypatch.setattr(library_client, "get_issue_item",
                        lambda *a, **k: (calls.append(1), orig(*a, **k))[1])
    issue_build.live_html_corrections()
    assert 0 < len(calls) <= issue_build._QUEUE_LIMIT, (
        f"expected at most {issue_build._QUEUE_LIMIT} Library round trips for "
        f"comments, got {len(calls)}")


def test_comment_cap_note_appears_when_the_page_level_budget_is_exhausted(db):
    """The truncation must be visible, not silent — the same contract the
    card-count cap already gives (`test_truncation_is_stated_on_the_page`)."""
    for e in range(5):
        epic_id = library_fake.seed_issue(**_mk(title=f"EPIC-{e}", type="epic", state="open"))
        for c in range(30):
            library_fake.seed_issue(**_mk(title=f"CHILD-{e}-{c}", parent_id=epic_id, state="open"))
    html = issue_build.live_html()
    assert "Comment threads are fetched for the first" in html
    assert f"more than {issue_build._QUEUE_LIMIT} Library round trips" in html


def test_comment_cap_note_absent_under_the_budget(db):
    library_fake.seed_issue(**_mk(title="EPIC-lonely", type="epic", state="open"))
    html = issue_build.live_html()
    assert "Comment threads are fetched for the first" not in html


# --- the number of epics RENDERED is capped, and the cap is visible ------------
# Round-2 review (issue 01M1518ZFBGHAWC4Z46W893H2S): the comment-fetch budget
# above bounds Library round trips, but `epics` itself was never sliced, so an
# arbitrary number of epics still built and serialized a full section each.

def test_epics_page_render_is_capped_and_the_cap_is_visible(db):
    """More epics than `_QUEUE_LIMIT`: only the cap's worth get a section, and
    the header states the true total and how many are not rendered — the same
    "showing N — M more not rendered" contract `_epic_block` already gives an
    epic's own children. Explicit ascending `created_at` (as
    `test_the_header_count_is_not_truncated_at_the_librarys_page_clamp` above
    already does), so which epics land inside the cap is deterministic rather
    than resting on `utcnow()` call order."""
    n = issue_build._QUEUE_LIMIT + 15
    for e in range(n):
        library_fake.seed_issue(**_mk(title=f"EPIC-{e:03d}", type="epic", state="open",
                  created_at=dt.datetime(2026, 7, 1) + dt.timedelta(seconds=e)))
    html = issue_build.live_html()
    assert f'id="waitCount">{n}<' in html
    assert f"showing {issue_build._QUEUE_LIMIT} — 15 more not rendered" in html
    rendered = sum(1 for e in range(n) if f"EPIC-{e:03d}" in html)
    assert rendered == issue_build._QUEUE_LIMIT, (
        f"expected exactly {issue_build._QUEUE_LIMIT} epics rendered, got {rendered}")
    # the 15 oldest-first-ordered epics past the cap are exactly the ones cut —
    # not listed here, but still reachable at their own detail page by id.
    for e in range(issue_build._QUEUE_LIMIT, n):
        assert f"EPIC-{e:03d}" not in html


def test_epics_page_render_is_uncapped_under_the_limit(db):
    """At or under the cap: every epic renders and no truncation note appears —
    the cap must not fire early."""
    n = issue_build._QUEUE_LIMIT
    for e in range(n):
        library_fake.seed_issue(**_mk(title=f"EPIC-{e:03d}", type="epic", state="open",
                  created_at=dt.datetime(2026, 7, 1) + dt.timedelta(seconds=e)))
    html = issue_build.live_html()
    assert f'id="waitCount">{n}<' in html
    assert "more not rendered" not in html
    for e in range(n):
        assert f"EPIC-{e:03d}" in html


# --- the inline JS must actually parse -----------------------------------------
# 2026-07-28..30: commit 86be5a2 introduced 82 curly quotes into `_JS`. Curly
# quotes are not valid JS string delimiters, so the whole <script> block was a
# parse error and EVERY button on the issue queue silently did nothing for two
# days — resolve, Won't do, Duplicate and Redirected alike. The operator found it by
# clicking "Redirected" twice and seeing no result.
#
# Nothing caught it: the tests asserted the buttons' MARKUP, which was correct,
# and the page rendered fine. A syntax error in an inline script is invisible to
# every check that stops at the HTML.

def _js_constants():
    import issue_build as ib
    return [(name, getattr(ib, name)) for name in dir(ib)
            if name.endswith("JS") and isinstance(getattr(ib, name), str)]


def test_every_inline_js_constant_parses():
    """Shells out to node, which is the only thing that actually answers this.
    Skips when node is absent rather than passing vacuously — a check that
    silently does nothing is the failure mode this test exists to catch."""
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to parse-check inline JS")
    constants = _js_constants()
    assert constants, "no JS constants found — the test is looking in the wrong place"
    for name, src in constants:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(src)
            path = f.name
        try:
            r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        finally:
            import os
            os.unlink(path)
        assert r.returncode == 0, f"{name} is not valid JavaScript:\n{r.stderr}"


@pytest.mark.parametrize("char,name", [("’", "right single"), ("‘", "left single"),
                                       ("“", "left double"), ("”", "right double")])
def test_no_smart_quotes_in_inline_js(char, name):
    """A cheaper, node-free guard on the same defect, and a more specific failure
    message. Smart quotes belong in prose; in a script block they are a parse
    error that no HTML assertion can see."""
    for const_name, src in _js_constants():
        assert char not in src, (
            f"{const_name} contains a {name} quote ({char!r}). Curly quotes are not "
            f"valid JS string delimiters and break the entire script block.")


# --- the product-chip filter must not strand a hidden project's header ---------
# Round-2 review (issue_01M1518ZFBGHAWC4Z46W893H2S): `applyFilter`/`recount` only
# ever touched `.card` elements. The old flat page had cards as direct children
# of #queue, so hiding non-matching cards produced a clean list. Epics and
# Corrections wrap every product's cards in a <section class="proj-group">, so
# filtering to one product hid every OTHER product's cards but left that
# section's own <h2 class="proj-head"> header and epic toggles fully visible
# with nothing under them.
#
# `test_every_inline_js_constant_parses` only shells to `node --check` — it
# proves the script is valid JavaScript, never that it does the right thing
# against a DOM, which is exactly how this defect shipped once already (the
# curly-quote defect that test exists for broke every button; this one leaves
# every button working and the page merely lying about what's on it). This
# drives the ACTUAL `_JS` — not a reimplementation of `applyFilter` — against a
# minimal DOM built from the real rendered HTML, so a regression here fails the
# way a browser would show it.

_DOM_DRIVER_JS = r"""
const fs = require('fs');
const vm = require('vm');
const [, , treePath, jsPath, targetProd] = process.argv;
const tree = JSON.parse(fs.readFileSync(treePath, 'utf8'));
const jsSrc = fs.readFileSync(jsPath, 'utf8');

function kebab(s) { return s.replace(/[A-Z]/g, function(m){ return '-' + m.toLowerCase(); }); }

function matchesSimple(el, sel) {
  if (!el || el.tag === '#root') return false;
  if (sel[0] === '.') {
    var cls = (el.attrs['class'] || '').split(/\s+/);
    return cls.indexOf(sel.slice(1)) !== -1;
  }
  if (sel[0] === '#') return el.attrs['id'] === sel.slice(1);
  return el.tag === sel;
}

function matchesCompound(el, parts) {
  var idx = parts.length - 1;
  if (!matchesSimple(el, parts[idx])) return false;
  idx--;
  var cur = el.parent;
  while (idx >= 0) {
    while (cur && !matchesSimple(cur, parts[idx])) cur = cur.parent;
    if (!cur) return false;
    idx--;
    cur = cur.parent;
  }
  return true;
}

function wrap(node, parent) {
  var attrs = node.attrs;
  var el = {
    tag: node.tag, attrs: attrs, children: [], parent: parent || null,
    _listeners: {}, style: {}, disabled: false, _text: '',
  };
  Object.defineProperty(el, 'id', { get: function(){ return attrs.id || ''; } });
  Object.defineProperty(el, 'textContent', {
    get: function(){ return this._text; },
    set: function(v){ this._text = v; },
  });
  el.dataset = new Proxy({}, {
    get: function(_, prop){ return attrs['data-' + kebab(String(prop))]; },
    set: function(_, prop, val){ attrs['data-' + kebab(String(prop))] = String(val); return true; },
  });
  el.getAttribute = function(name){ return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null; };
  el.setAttribute = function(name, val){ attrs[name] = String(val); };
  el.addEventListener = function(type, fn){ (this._listeners[type] = this._listeners[type] || []).push(fn); };
  el._dispatch = function(type, evt){ (this._listeners[type] || []).forEach(function(fn){ fn(evt); }); };
  el.closest = function(sel){
    var parts = sel.trim().split(/\s+/);
    var cur = this;
    while (cur) { if (matchesCompound(cur, parts)) return cur; cur = cur.parent; }
    return null;
  };
  el.querySelectorAll = function(sel){
    var parts = sel.trim().split(/\s+/);
    var out = [];
    (function walk(n){
      n.children.forEach(function(c){ if (matchesCompound(c, parts)) out.push(c); walk(c); });
    })(this);
    return out;
  };
  el.querySelector = function(sel){ var r = this.querySelectorAll(sel); return r.length ? r[0] : null; };
  el.focus = function(){};
  el.remove = function(){};
  el.classList = { add: function(){}, remove: function(){}, toggle: function(){}, contains: function(){ return false; } };
  node.children.forEach(function(c){ el.children.push(wrap(c, el)); });
  return el;
}

var rootEl = wrap(tree, null);
var byId = {};
(function index(n){ if (n.attrs && n.attrs.id) byId[n.attrs.id] = n; n.children.forEach(index); })(rootEl);

var documentStub = {
  getElementById: function(id){ return byId[id] || null; },
  querySelector: function(sel){ return rootEl.querySelector(sel); },
  querySelectorAll: function(sel){ return rootEl.querySelectorAll(sel); },
};
var sandbox = {
  document: documentStub,
  setTimeout: function(){ return 0; },
  clearTimeout: function(){},
  fetch: function(){ return Promise.reject(new Error('fetch unavailable in test DOM')); },
  console: console,
};
vm.createContext(sandbox);
vm.runInContext(jsSrc, sandbox);

var chipsEl = byId['chips'];
if (!chipsEl) { console.error('no #chips in rendered page'); process.exit(2); }
var chipButtons = chipsEl.querySelectorAll('.chip');
var targetChip = null;
chipButtons.forEach(function(c){ if (c.attrs['data-prod'] === targetProd) targetChip = c; });
if (!targetChip) { console.error('chip not found: ' + targetProd); process.exit(2); }

chipsEl._dispatch('click', { target: targetChip });

function isHidden(el) { return el.style.display === 'none'; }

var scope = byId['queue'] || rootEl;
var result = {
  projGroups: scope.querySelectorAll('.proj-group').map(function(g){
    return { prod: g.attrs['data-prod'], hidden: isHidden(g) };
  }),
  cards: scope.querySelectorAll('.card').map(function(c){
    return { prod: c.attrs['data-prod'], hidden: isHidden(c) };
  }),
  waitCount: byId['waitCount'] ? byId['waitCount'].textContent : null,
};
process.stdout.write(JSON.stringify(result));
"""


def _html_to_tree(html: str) -> dict:
    """Parse rendered HTML into the {tag, attrs, children} shape the Node DOM
    shim understands. `<script>`/`<style>` content is never handed to
    `handle_starttag` — `HTMLParser` treats both as CDATA — so JS containing
    `<`/`>` cannot be mistaken for markup."""
    from html.parser import HTMLParser

    void = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    class _Builder(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.root = {"tag": "#root", "attrs": {}, "children": []}
            self.stack = [self.root]

        def handle_starttag(self, tag, attrs):
            node = {"tag": tag, "attrs": dict(attrs), "children": []}
            self.stack[-1]["children"].append(node)
            if tag not in void:
                self.stack.append(node)

        def handle_startendtag(self, tag, attrs):
            self.stack[-1]["children"].append(
                {"tag": tag, "attrs": dict(attrs), "children": []})

        def handle_endtag(self, tag):
            if len(self.stack) > 1 and self.stack[-1]["tag"] == tag:
                self.stack.pop()

    builder = _Builder()
    builder.feed(html)
    return builder.root


def _click_chip(html: str, target_prod: str) -> dict:
    """Render `html`'s DOM, click the chip for `target_prod` through the
    ACTUAL `_JS`, and return which `.proj-group`s and `.card`s (scoped to
    #queue, like the real code) ended up hidden. Skips when node is
    unavailable, matching `test_every_inline_js_constant_parses`."""
    import json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to drive the inline JS against a DOM")

    tree = _html_to_tree(html)
    with tempfile.TemporaryDirectory() as tmp:
        tree_path = f"{tmp}/tree.json"
        js_path = f"{tmp}/inline.js"
        driver_path = f"{tmp}/driver.js"
        with open(tree_path, "w", encoding="utf-8") as f:
            json.dump(tree, f)
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(issue_build._JS)
        with open(driver_path, "w", encoding="utf-8") as f:
            f.write(_DOM_DRIVER_JS)
        r = subprocess.run([node, driver_path, tree_path, js_path, target_prod],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"DOM driver failed:\n{r.stdout}\n{r.stderr}"
        return json.loads(r.stdout)


def test_the_chip_filter_hides_a_project_it_stranded_on_epics(db):
    library_fake.seed_issue(**_mk(title="EPIC-vivy", type="epic", product="vivy", state="open"))
    library_fake.seed_issue(**_mk(title="EPIC-selector", type="epic", product="selector", state="open"))
    result = _click_chip(issue_build.live_html(), "selector")
    groups = {g["prod"]: g["hidden"] for g in result["projGroups"]}
    assert groups == {"selector": False, "vivy": True}, (
        "filtering to selector must hide vivy's whole section — header and "
        f"all — not just its cards: {result}")


def test_the_chip_filter_hides_a_project_it_stranded_on_corrections(db):
    library_fake.seed_issue(**_mk(title="CORR-vivy", product="vivy", state="open"))
    library_fake.seed_issue(**_mk(title="CORR-selector", product="selector", state="open"))
    result = _click_chip(issue_build.live_html_corrections(), "selector")
    groups = {g["prod"]: g["hidden"] for g in result["projGroups"]}
    assert groups == {"selector": False, "vivy": True}, (
        "filtering to selector must hide vivy's whole section — header and "
        f"all — not just its cards: {result}")


def test_the_chip_filter_leaves_the_matching_project_fully_visible(db):
    """The fix must not over-hide: a matching project's cards stay shown, and
    switching to "all" must un-hide everything again."""
    library_fake.seed_issue(**_mk(title="EPIC-vivy", type="epic", product="vivy", state="open"))
    library_fake.seed_issue(**_mk(title="EPIC-selector", type="epic", product="selector", state="open"))
    html = issue_build.live_html()
    result = _click_chip(html, "all")
    groups = {g["prod"]: g["hidden"] for g in result["projGroups"]}
    assert groups == {"selector": False, "vivy": False}


# --- the two lanes ---------------------------------------------------------------
# Issues and Blockers are two views over one table. The property that has to
# hold is that they PARTITION the active set: every active issue on exactly
# one page. A gap is silent — an issue in no lane is invisible everywhere — and
# an overlap double-counts, which is how a queue count starts lying again.
#
# Backlog (`type == feature` routing to its own lane) was retired 2026-08-28:
# a feature now falls through to Issues like any other item, which is why
# `LANE-plainfeature` below is asserted INTO the issues page rather than out
# of it — the inverse of what this sample tested before the retirement.

def _lane_sample():
    """One issue of every non-epic shape the classifier has to place,
    including the NULL columns that rows predating `needs`/`type` still
    carry. None of these carries a `parent_id`, so every one is a Corrections
    member (`_is_correction`) unless `needs == operator` routes it to
    Blockers first — Epics is exercised separately (`test_the_lanes_
    partition_the_active_set`), since a plain `_mk(...)` item is never one."""
    return [
        _mk(title="LANE-plainbug", type="bug", needs="agent", state="open"),
        _mk(title="LANE-plainnit", type="nit", needs="either", state="open"),
        _mk(title="LANE-improvement", type="improvement", needs="agent", state="claimed"),
        _mk(title="LANE-operatortask", type="task", needs="operator", state="open"),
        _mk(title="LANE-plainfeature", type="feature", needs="agent", state="open"),
        _mk(title="LANE-operatorfeature", type="feature", needs="operator", state="open"),
        _mk(title="LANE-operatorbug", type="bug", needs="operator", state="pr-open",
            refs={"pr": 1, "url": "https://github.com/convivy/vivy/pull/1"}),
        _mk(title="LANE-nullcolumns", type=None, needs=None, state="open"),
    ]


def test_the_lanes_partition_the_active_set(db):
    """Every active issue on exactly one of the three pages — Epics,
    Corrections, Blockers. This is the invariant the split exists to
    preserve — hand-written membership tests would drift, and both failure
    modes (a gap, an overlap) are silent.

    Extends the plain-item sample (every one Corrections-or-Blockers, none
    carrying a `parent_id`) with an epic and its child: both must land on
    Epics — the epic as its own section, the child nested inside it — and
    nowhere else, which is the one case the flat sample cannot exercise.

    "Reachable" here still means "present in the rendered HTML": this sample
    seeds one epic, far under `_QUEUE_LIMIT` (issue 01M1518ZFBGHAWC4Z46W893H2S's
    render cap), so the cap never triggers and does not weaken what this test
    proves. `test_epics_page_render_is_capped_and_the_cap_is_visible` covers
    the capped case, where "reachable" instead means "listed here, or reachable
    at the epic's own detail page if its id is already known"."""
    for it in _lane_sample():
        library_fake.seed_issue(**it)
    epic_id = library_fake.seed_issue(**_mk(title="LANE-epic", type="epic", needs="agent", state="open"))
    library_fake.seed_issue(**_mk(title="LANE-childwork", type="bug", needs="agent", state="open",
              parent_id=epic_id))
    pages = {
        "epics": issue_build.live_html(),
        "corrections": issue_build.live_html_corrections(),
        "blockers": issue_build.live_html_blockers(),
    }
    titles = [i["title"] for i in _lane_sample()] + ["LANE-epic", "LANE-childwork"]
    for title in titles:
        hits = [name for name, html in pages.items() if title in html]
        assert len(hits) == 1, f"{title} appears on {hits or 'NO page'}, expected exactly one"
    assert "LANE-epic" in pages["epics"]
    assert "LANE-childwork" in pages["epics"]


def test_needs_operator_beats_type_feature(db):
    """A feature only the operator can do is a blocker, not an ordinary issue. "Waiting on
    you" is actionable now; a feature sitting in either non-blocker page is not
    the same kind of thing."""
    library_fake.seed_issue(**_mk(title="LANE-operatorfeature", type="feature", needs="operator", state="open"))
    assert "LANE-operatorfeature" in issue_build.live_html_blockers()
    assert "LANE-operatorfeature" not in issue_build.live_html()
    assert "LANE-operatorfeature" not in issue_build.live_html_corrections()


def test_feature_type_items_render_in_the_corrections_lane(db):
    """Backlog is retired (the operator, 2026-08-28): `type == feature` no longer routes
    anywhere special, so a feature with no parent epic renders on Corrections
    like any other unparented work item — planted here rather than assumed,
    since a feature that no page lists is worse than the page that used to
    hold it."""
    library_fake.seed_issue(**_mk(title="LANE-plainfeature", type="feature", needs="agent", state="open"))
    html = issue_build.live_html_corrections()
    assert "LANE-plainfeature" in html
    assert "LANE-plainfeature" not in issue_build.live_html_blockers()
    assert "LANE-plainfeature" not in issue_build.live_html()


def test_null_needs_and_type_land_in_corrections(db):
    """Rows predating those columns read as agent/bug, with no parent — so they
    land in Corrections. Without COALESCE in the classifier, `NOT(needs =
    'operator')` is NULL for a NULL needs — falsy — and every such row would
    vanish from every page."""
    library_fake.seed_issue(**_mk(title="LANE-nullcolumns", type=None, needs=None, state="open"))
    assert "LANE-nullcolumns" in issue_build.live_html_corrections()


def test_issue_counts_exclude_the_other_lane(db):
    """The Corrections header count must describe the Corrections page only.
    A feature (LANE-plainfeature) counts here now — Backlog no longer exists
    to hold it — while an operator-needs item (Blockers) must not."""
    library_fake.seed_issue(**_mk(title="LANE-plainbug", type="bug", needs="agent", state="open"))
    library_fake.seed_issue(**_mk(title="LANE-operatortask", type="task", needs="operator", state="open"))
    library_fake.seed_issue(**_mk(title="LANE-plainfeature", type="feature", needs="agent", state="open"))
    html = issue_build.live_html_corrections()
    assert '<span class="count" id="waitCount">2</span>' in html
    assert "LANE-operatortask" not in html
    assert "LANE-plainfeature" in html


def test_terminal_issues_are_in_no_lane(db):
    """The lanes partition the ACTIVE set only. A resolved feature must not appear
    on Blockers."""
    library_fake.seed_issue(**_mk(title="LANE-resolvedfeature", type="feature", needs="agent", state="fixed",
              resolution="fixed"))
    assert "LANE-resolvedfeature" not in issue_build.live_html_blockers()


def test_lane_pages_carry_a_render_stamp_and_product_chips(db):
    library_fake.seed_issue(**_mk(title="LANE-operatortask", product="sous-chef", type="task",
              needs="operator", state="open"))
    html = issue_build.live_html_blockers()
    assert 'class="render-stamp"' in html
    assert "sous-chef" in html


def test_empty_lane_says_so(db):
    assert "Nothing is waiting on you" in issue_build.live_html_blockers()


def test_every_page_shipping_the_js_has_the_ids_it_dereferences(db):
    """A page that embeds `_JS` without the ids it looks up throws on load, and
    every action button silently stops working.

    That is not hypothetical: the first cut of the lane pages shipped `_JS` with
    none of the structural ids, so `getElementById('chips')` returned null,
    `chips.addEventListener` threw, and the whole script block died. The cards
    rendered perfectly and "Won't do" did nothing — and because the Issues page
    excludes this lane, this page is the only UI path to resolve a Blocker.

    It is the same failure as the curly quotes that killed this page's buttons on
    2026-07-28: markup that renders and script that never runs. Asserting the
    markup cannot see it.
    """
    import re
    required = {i for i in re.findall(r"getElementById\(['\"]([A-Za-z0-9_-]+)['\"]\)",
                                      issue_build._JS)} - issue_build._JS_OPTIONAL_IDS
    assert required, "found no ids in _JS — the regex is looking in the wrong place"
    for name, builder in (("epics", issue_build.live_html),
                          ("corrections", issue_build.live_html_corrections),
                          ("blockers", issue_build.live_html_blockers)):
        html = builder()
        if "<script>" not in html:
            continue
        missing = sorted(i for i in required if f'id="{i}"' not in html)
        assert not missing, f"{name} ships _JS but lacks {missing}"


def test_course_page_ships_no_resolve_script(db):
    """Course is read-only — no resolve actions, no `_JS` (the shell's own
    theme-toggle/url-state scripts still ship on every page, so the check is
    for `_JS` specifically, not for the absence of any `<script>` tag at
    all). If this ever starts shipping `_JS`, it needs the same id audit the
    pages above get; until then, absence is the contract."""
    html = issue_build.live_html_course()
    assert "doResolve" not in html
    assert 'data-act="resolve"' not in html


def test_lane_pages_render_the_resolve_actions(db):
    """The buttons must be present as well as functional — this page is the only
    place a Blocker item can be closed from the UI."""
    library_fake.seed_issue(**_mk(title="LANE-operatortask", type="task", needs="operator", state="open"))
    html = issue_build.live_html_blockers()
    assert 'data-act="resolve"' in html
    assert 'data-resolution="wontfix"' in html


# --- Epics: an epic per project, expanding into its work items ------------------
# The operator, 2026-08-29 (issue_01M1518ZFBGHAWC4Z46W893H2S). `_epic_and_correction_
# groups` is the one place this is decided, mirroring `lane_of`'s own
# "one place, everything else derives" discipline.


def test_an_epic_expands_into_its_work_items(db):
    epic_id = library_fake.seed_issue(**_mk(title="EPIC-parent", type="epic", state="open"))
    library_fake.seed_issue(**_mk(title="CHILD-one", parent_id=epic_id, state="open"))
    library_fake.seed_issue(**_mk(title="CHILD-two", parent_id=epic_id, state="open"))
    html = issue_build.live_html()
    assert "EPIC-parent" in html
    assert "CHILD-one" in html and "CHILD-two" in html
    # nested inside the epic's own toggle, not a second top-level card
    assert html.index("EPIC-parent") < html.index("CHILD-one")
    assert '<details class="children-toggle">' in html


def test_a_work_item_with_no_parent_is_a_correction_not_under_any_epic(db):
    """`_is_correction`: no parent epic is how a work item reads as a
    corrections-queue member, until the Library backfills a real parent id
    (ADR-0060)."""
    library_fake.seed_issue(**_mk(title="LOOSE-work", state="open"))
    assert "LOOSE-work" in issue_build.live_html_corrections()
    assert "LOOSE-work" not in issue_build.live_html()


def test_a_work_item_whose_parent_is_its_corrections_queue_is_a_correction(db):
    """The Library is moving corrections from a null parent to their product's
    queue id (library/specs/one-home-per-work-item). Both are corrections, and
    the queue id never heads an epic section.

    The order assertion is what fails without the queue lookup: the dangling-
    parent fallback would still file QUEUED-work under Corrections, but after
    every member, so the older item would sort last."""
    library_fake.seed_corrections_queue("cq_vivy")
    library_fake.seed_issue(**_mk(title="QUEUED-work", state="open", parent_id="cq_vivy",
                                  created_at=dt.datetime(2026, 7, 1)))
    library_fake.seed_issue(**_mk(title="NULL-work", state="open",
                                  created_at=dt.datetime(2026, 7, 2)))
    corrections = issue_build.live_html_corrections()
    epics = issue_build.live_html()
    assert "QUEUED-work" in corrections and "NULL-work" in corrections
    assert "QUEUED-work" not in epics and "cq_vivy" not in epics
    _, children_by_parent, queued = issue_build._epic_and_correction_groups(
        library_client.list_issue_items())
    assert children_by_parent == {}
    assert [it.title for it in queued] == ["QUEUED-work", "NULL-work"]


def test_an_item_found_during_an_epic_stays_a_correction(db):
    """`found_during` records where an item was found, not what it is part of,
    so an item carrying it with no parent stays in the corrections lane."""
    epic_id = library_fake.seed_issue(**_mk(title="EPIC-finder", type="epic", state="open"))
    library_fake.seed_issue(**_mk(title="FOUND-work", state="open", found_during=epic_id))
    assert "FOUND-work" in issue_build.live_html_corrections()
    assert "FOUND-work" not in issue_build.live_html()


def test_a_child_whose_epic_has_resolved_still_renders_under_it(db):
    """An active work item is reachable on some page even when its own epic is
    no longer active — the epic block still renders (headed by the resolved
    epic's own card) rather than the child silently vanishing because its
    parent fell out of the active set."""
    epic_id = library_fake.seed_issue(**_mk(title="EPIC-done", type="epic", state="fixed",
              resolution="fixed", resolved_by="operator@example.com"))
    library_fake.seed_issue(**_mk(title="CHILD-orphaned", parent_id=epic_id, state="open"))
    html = issue_build.live_html()
    assert "CHILD-orphaned" in html
    assert "EPIC-done" in html
    assert "CHILD-orphaned" not in issue_build.live_html_corrections()


def test_a_child_with_a_dangling_parent_falls_back_to_corrections(db):
    """A `parent_id` naming no item at all (a broken reference) has no epic
    block to render under — it falls back to Corrections rather than
    vanishing, since every live work item reachable on some page is the
    property this split exists to hold."""
    library_fake.seed_issue(**_mk(title="CHILD-dangling", parent_id="issue_does_not_exist", state="open"))
    assert "CHILD-dangling" in issue_build.live_html_corrections()
    assert "CHILD-dangling" not in issue_build.live_html()


def test_a_child_whose_parent_id_names_a_non_epic_falls_back_to_corrections(db):
    """`_epic_and_correction_groups`'s fallback loop used to trust `by_id.get
    (eid)` with no `type` check — a `parent_id` naming a non-epic item would
    have rendered that item as an epic's own section header. Not reachable
    under ADR-0060 (a work item's parent is always an epic or the corrections
    queue) but cheap to hold anyway, the same way a dangling reference falls
    back rather than vanishing."""
    not_an_epic = library_fake.seed_issue(**_mk(title="PARENT-not-an-epic", type="bug", state="open"))
    library_fake.seed_issue(**_mk(title="CHILD-of-non-epic", parent_id=not_an_epic, state="open"))
    corrections = issue_build.live_html_corrections()
    epics = issue_build.live_html()
    assert "CHILD-of-non-epic" in corrections
    assert "CHILD-of-non-epic" not in epics
    # PARENT-not-an-epic renders as an ordinary correction card of its own
    # (it IS an active, unparented work item) — the bug this test pins is it
    # ALSO rendering a second time as a fake epic section header on Epics.
    assert "PARENT-not-an-epic" not in epics


def test_epics_are_grouped_by_project(db):
    library_fake.seed_issue(**_mk(title="EPIC-vivy", type="epic", product="vivy", state="open"))
    library_fake.seed_issue(**_mk(title="EPIC-selector", type="epic", product="selector", state="open"))
    html = issue_build.live_html()
    assert '<h2 class="proj-head">vivy' in html
    assert '<h2 class="proj-head">selector' in html
    # each epic sits after its own project's header, not the other one's
    assert html.index('<h2 class="proj-head">vivy') < html.index("EPIC-vivy")
    assert html.index('<h2 class="proj-head">selector') < html.index("EPIC-selector")


def test_corrections_are_grouped_by_project(db):
    library_fake.seed_issue(**_mk(title="CORR-vivy", product="vivy", state="open"))
    library_fake.seed_issue(**_mk(title="CORR-selector", product="selector", state="open"))
    html = issue_build.live_html_corrections()
    assert '<h2 class="proj-head">vivy' in html
    assert '<h2 class="proj-head">selector' in html


def test_an_epic_with_no_active_children_says_so(db):
    library_fake.seed_issue(**_mk(title="EPIC-lonely", type="epic", state="open"))
    html = issue_build.live_html()
    assert "No open work items" in html


# --- cross-product relations (issue_01M1JSPH7DBHG0BZFPJPNNZGSC) -----------------
# An item in another product is part of an epic's work through the Library's
# relation, not a parent edge, so it renders under the epic only from that read.


def _epic_toggle(html: str) -> str:
    start = html.index('<details class="children-toggle">')
    return html[start:html.index("</details>", start)]


def test_a_cross_product_relation_renders_under_its_epic_and_is_counted(db):
    epic_id = library_fake.seed_issue(**_mk(title="EPIC-ledger", type="epic", product="vivy", state="open"))
    library_fake.seed_issue(**_mk(title="SAME-child", product="vivy", parent_id=epic_id, state="open"))
    other_id = library_fake.seed_issue(**_mk(title="XPROD-related", product="selector", state="open"))
    library_fake.seed_related_item(epic_id=epic_id, item_id=other_id)
    toggle = _epic_toggle(issue_build.live_html())
    assert "SAME-child" in toggle
    assert "XPROD-related" in toggle
    assert "2 work items" in toggle
    assert '<span class="badge xprod">from selector</span>' in toggle
    # the same-product child is not marked as from another product
    assert toggle.count("badge xprod") == 1


def test_a_same_product_relation_does_not_list_its_item_twice(db):
    epic_id = library_fake.seed_issue(**_mk(title="EPIC-ledger", type="epic", product="vivy", state="open"))
    same_id = library_fake.seed_issue(**_mk(title="SAME-child", product="vivy", parent_id=epic_id, state="open"))
    other_id = library_fake.seed_issue(**_mk(title="XPROD-related", product="selector", state="open"))
    library_fake.seed_related_item(epic_id=epic_id, item_id=same_id)
    library_fake.seed_related_item(epic_id=epic_id, item_id=other_id)
    toggle = _epic_toggle(issue_build.live_html())
    assert toggle.count("SAME-child") == 1
    assert "2 work items" in toggle


def test_a_cross_product_item_takes_its_lane_place_among_the_children(db):
    epic_id = library_fake.seed_issue(**_mk(title="EPIC-ledger", type="epic", product="vivy", state="open"))
    library_fake.seed_issue(**_mk(title="SAME-newer", product="vivy", parent_id=epic_id, state="open",
                                  created_at=dt.datetime(2026, 7, 2)))
    other_id = library_fake.seed_issue(**_mk(title="XPROD-older", product="selector", state="open",
                                             created_at=dt.datetime(2026, 6, 1)))
    library_fake.seed_related_item(epic_id=epic_id, item_id=other_id)
    toggle = _epic_toggle(issue_build.live_html())
    assert toggle.index("XPROD-older") < toggle.index("SAME-newer")


def test_a_failed_relation_read_says_the_cross_product_list_is_unavailable(db, monkeypatch):
    epic_id = library_fake.seed_issue(**_mk(title="EPIC-ledger", type="epic", product="vivy", state="open"))
    library_fake.seed_issue(**_mk(title="SAME-child", product="vivy", parent_id=epic_id, state="open"))

    def library_down(epic):
        raise OSError("library unreachable")

    monkeypatch.setattr(library_client, "list_related_item_ids", library_down)
    toggle = _epic_toggle(issue_build.live_html())
    assert "1 work item · cross-product list unavailable" in toggle
    assert "SAME-child" in toggle


def test_a_read_that_starts_inside_the_budget_runs_and_the_next_does_not_start(db, monkeypatch):
    for name in ("EPIC-a", "EPIC-b", "EPIC-c"):
        library_fake.seed_issue(**_mk(title=name, type="epic", product="vivy", state="open"))
    now = [0.0]
    reads = []

    def slow_read(epic):
        reads.append(epic)
        # the first read leaves the second starting just inside the budget;
        # the second runs its full timeout past the deadline
        now[0] += library_client.TIMEOUT - 0.1 if len(reads) == 1 else library_client.TIMEOUT
        return []

    monkeypatch.setattr(library_client, "list_related_item_ids", slow_read)
    related, failed = issue_build._read_related_items(library_client.list_issue_items(),
                                                      clock=lambda: now[0])
    assert len(reads) == 2
    assert len(related) == 2
    assert len(failed) == 1
    assert now[0] <= 2 * library_client.TIMEOUT


def test_a_stalled_library_stops_starting_relation_reads_after_one_timeout(db, monkeypatch):
    library_fake.seed_issue(**_mk(title="EPIC-a", type="epic", product="vivy", state="open"))
    library_fake.seed_issue(**_mk(title="EPIC-b", type="epic", product="vivy", state="open"))
    now = [0.0]
    reads = []

    def stalled_read(epic):
        reads.append(epic)
        now[0] += library_client.TIMEOUT
        raise TimeoutError("timed out")

    monkeypatch.setattr(library_client, "list_related_item_ids", stalled_read)
    related, failed = issue_build._read_related_items(library_client.list_issue_items(),
                                                      clock=lambda: now[0])
    assert len(reads) == 1
    assert related == {}
    assert len(failed) == 2


def test_an_item_that_names_the_epic_in_its_body_is_not_related_to_it(db):
    epic_id = library_fake.seed_issue(**_mk(title="EPIC-ledger", type="epic", product="vivy", state="open"))
    library_fake.seed_issue(**_mk(title="MENTIONER", product="selector", state="open",
                                  body=f"this belongs to {epic_id}"))
    toggle = _epic_toggle(issue_build.live_html())
    assert "No open work items" in toggle
    assert "MENTIONER" not in toggle


def test_the_relation_client_reads_related_items_off_the_epic_route(monkeypatch):
    paths = []

    def fake_request(path, params=None, **kw):
        paths.append(path)
        return {"id": "issue_E", "related_items": [{"id": "issue_X"}, {"id": "issue_Y"}]}

    monkeypatch.setattr(library_client, "_request", fake_request)
    assert library_client.list_related_item_ids("issue_E") == ["issue_X", "issue_Y"]
    assert paths == ["/items/issue_E"]


# --- Course: each project's Course, in sequence ----------------------------------
# The operator, 2026-08-29. Read-only — a step links to its issue; no resolve action.
# `library_client.list_courses`/`get_course` are faked directly per test
# (`_no_courses` above defaults to none), the same convention
# `test_chart_house_build.py` uses for its own course reads.


def _course(course_id, product, title, *, version=1, entries):
    return library_client.CourseRecord(
        id=course_id, product=product, title=title, status="live", version=version,
        entries=entries, created_by="ops-mcp", created_at=None,
        updated_by="ops-mcp", updated_at=None)


def _entry(issue_id, state, maturity=None, title=None):
    return library_client.CourseEntry(issue_id=issue_id, state=state,
                                      maturity=maturity, title=title or issue_id)


def test_course_page_renders_entries_in_sequence(db, monkeypatch):
    """`entries` order is the one thing a Course carries that its issues do
    not — rendered as given, never re-sorted or grouped."""
    course = _course("course_1", "vivy", "Vivy's course", entries=[
        _entry("issue_a", "fixed", title="FIRST-STEP"),
        _entry("issue_b", "open", maturity="approved", title="SECOND-STEP"),
        _entry("issue_c", "open", maturity="proposed", title="THIRD-STEP"),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    html = issue_build.live_html_course()
    assert html.index("FIRST-STEP") < html.index("SECOND-STEP") < html.index("THIRD-STEP")
    assert '<h2 class="proj-head">vivy' in html
    assert 'href="/engineering/issues/issue_a"' in html


def test_course_section_carries_the_anchor_id_other_pages_link_to(db, monkeypatch):
    """`course_anchor_href` is what `chart_house_build`'s "awaits your
    sign-off" chip, counts line and signals line all resolve their href
    against (issue_01M17W7DRZ3FJNRNYCAQFWHS1F) — this pins the id it depends
    on so the two modules can't drift apart."""
    course = _course("course_1", "vivy", "Vivy's course", entries=[_entry("issue_a", "open")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    html = issue_build.live_html_course()
    assert 'id="course-vivy"' in html
    assert issue_build.course_anchor_href("vivy") == "/engineering/course#course-vivy"


def test_course_step_classes_reflect_state_and_maturity(db, monkeypatch):
    course = _course("course_1", "vivy", "t", entries=[
        _entry("issue_a", "fixed", title="DONE-STEP"),
        _entry("issue_b", "open", maturity="approved", title="APPROVED-STEP"),
        _entry("issue_c", "open", maturity="proposed", title="PROPOSED-STEP"),
        _entry("issue_d", "open", maturity=None, title="UNSPEC-STEP"),
    ])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    html = issue_build.live_html_course()
    assert 'class="course-step step-done"' in html and "DONE-STEP" in html
    assert 'class="course-step step-approved"' in html and "APPROVED-STEP" in html
    assert 'class="course-step step-proposed"' in html and "PROPOSED-STEP" in html
    assert 'class="course-step step-unspecified"' in html and "UNSPEC-STEP" in html


def test_course_page_has_no_resolve_action(db, monkeypatch):
    course = _course("course_1", "vivy", "t", entries=[_entry("issue_a", "open")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: course)
    html = issue_build.live_html_course()
    assert 'data-act="resolve"' not in html


def test_course_falls_back_to_the_list_record_when_get_course_races_it_away(db, monkeypatch):
    """`get_course` degrading to the `list_courses` record on its own failure
    is the same discipline `chart_house_build._course_rows` uses — a single
    course's timeout or a delete race must not take the whole page down."""
    course = _course("course_1", "vivy", "t", entries=[_entry("issue_a", "open")])
    monkeypatch.setattr(library_client, "list_courses", lambda **k: [course])
    monkeypatch.setattr(library_client, "get_course", lambda cid: None)
    html = issue_build.live_html_course()
    assert '<h2 class="proj-head">vivy' in html


def test_course_page_empty_state(db):
    assert "no live course" in issue_build.live_html_course().lower()


# --- one issue's page ------------------------------------------------------------
# The queue cannot answer for the ids that most need a page. It holds only
# non-terminal issues and keeps twelve resolved ones, so an issue resolved a month
# ago has no row anywhere — while its id stays quoted in commit messages, PR
# bodies and other issues' text.


def test_detail_renders_an_open_issue_with_its_actions(db):
    library_fake.seed_issue(**_mk(id="issue_open1", title="An open one", body="**bold** body"))
    html = issue_build.detail_html("issue_open1")
    assert "An open one" in html
    assert "<strong>bold</strong>" in html          # markdown, like the queue
    assert 'data-act="resolve"' in html             # the queue's action set


def test_detail_is_none_for_an_unknown_id(db):
    assert issue_build.detail_html("issue_does_not_exist") is None


def test_a_resolved_issue_renders_and_cannot_be_resolved_again(db):
    """The banner is the point: the queue dropped this row and the id is still
    quoted elsewhere. And the buttons are gone, because posting a second
    resolution over the first succeeds at the API and overwrites the real one."""
    library_fake.seed_issue(**_mk(id="issue_done1", title="Finished", state="fixed",
              resolution="fixed", resolved_by="operator@example.com",
              outcome_note="shipped in #18"))
    html = issue_build.detail_html("issue_done1")
    assert "Finished" in html
    assert "shipped in #18" in html
    assert 'class="outcome resolved-done"' in html
    assert 'data-act="resolve"' not in html


def test_a_duplicate_links_to_the_issue_it_repeats(db):
    """A banner naming an id the reader cannot open is the dead end this page
    exists to close."""
    library_fake.seed_issue(**_mk(id="issue_original", title="The original"))
    library_fake.seed_issue(**_mk(id="issue_dupe1", title="Same again", state="duplicate",
              resolution="duplicate", resolved_by="operator@example.com",
              refs={"duplicate_of": "issue_original"}))
    html = issue_build.detail_html("issue_dupe1")
    assert 'href="/engineering/issues/issue_original"' in html


def test_a_child_links_to_its_parent(db):
    """An epic's children each name it and none of them could open it."""
    library_fake.seed_issue(**_mk(id="issue_epic1", title="The epic", type="epic"))
    library_fake.seed_issue(**_mk(id="issue_child1", title="Under an epic", parent_id="issue_epic1"))
    html = issue_build.detail_html("issue_child1")
    assert 'href="/engineering/issues/issue_epic1"' in html


def test_an_untyped_work_item_gets_no_type_badge_and_a_typed_one_keeps_its_own(db):
    """A work item carries no type, so it is never badged as the old default,
    `bug`. An item that still has a type, here a closed one, shows that type."""
    library_fake.seed_issue(**_mk(id="issue_untyped", title="Untyped", type=None))
    library_fake.seed_issue(**_mk(id="issue_oldtask", title="Closed task", type="task",
                                  state="fixed", resolution="fixed",
                                  resolved_by="operator@example.com"))
    untyped = issue_build.detail_html("issue_untyped")
    assert 'class="badge type' not in untyped and ">bug<" not in untyped
    assert '<span class="badge type">task</span>' in issue_build.detail_html("issue_oldtask")
    card = issue_build.live_html_corrections()
    assert "Untyped" in card and 'class="badge type' not in card


def test_a_correction_in_its_queue_links_to_corrections_not_to_the_queue_id(db):
    """A corrections queue is not an item, so `/engineering/issues/<queue id>`
    would 404 (library/specs/one-home-per-work-item)."""
    library_fake.seed_corrections_queue("cq_vivy")
    library_fake.seed_issue(**_mk(id="issue_corr1", title="In the queue", parent_id="cq_vivy"))
    html = issue_build.detail_html("issue_corr1")
    assert 'in <a href="/engineering/corrections">Corrections</a>' in html
    assert "/engineering/issues/cq_vivy" not in html


def test_an_item_found_during_an_epic_links_to_it_and_the_epic_lists_it(db):
    """Both surfaces of `found_during` (issue_01M3BFWTHTW60QGK13W5C35KB1): the
    item names the epic whose work turned it up, and the epic's page lists
    every such item, open and resolved."""
    library_fake.seed_issue(**_mk(id="issue_epicfd", title="The epic", type="epic"))
    library_fake.seed_issue(**_mk(id="issue_found1", title="Turned up open",
                                  found_during="issue_epicfd"))
    library_fake.seed_issue(**_mk(id="issue_found2", title="Turned up and fixed", state="fixed",
                                  resolution="fixed", found_during="issue_epicfd"))
    library_fake.seed_issue(**_mk(id="issue_other", title="Found elsewhere"))

    item = issue_build.detail_html("issue_found1")
    assert 'found during <a href="/engineering/issues/issue_epicfd">issue_epicfd</a>' in item
    assert "Found during this epic" not in item

    epic = issue_build.detail_html("issue_epicfd")
    assert "Found during this epic · 2" in epic
    assert 'href="/engineering/issues/issue_found1"' in epic
    assert 'href="/engineering/issues/issue_found2"' in epic
    assert "Found elsewhere" not in epic
    assert epic.index("Turned up open") < epic.index("Turned up and fixed")


def test_an_epic_with_nothing_found_during_it_shows_no_section(db):
    library_fake.seed_issue(**_mk(id="issue_epicnone", title="Quiet epic", type="epic"))
    assert "Found during this epic" not in issue_build.detail_html("issue_epicnone")


def test_the_detail_thread_is_not_elided_at_the_queue_limit(db):
    """The queue clips a thread at twelve to keep a 100-row page from becoming
    multi-megabyte. On the page whose whole job IS the thread, that limit would
    hide the thing the reader came for."""
    item_id = library_fake.seed_issue(**_mk(id="issue_chat1", title="Talkative"))
    for n in range(issue_build._THREAD_RENDER_LIMIT + 5):
        library_fake.seed_issue_comment(item_id=item_id, id=f"cmt_{n}", actor="agent",
                                        kind="note", body=f"note number {n}")
    html = issue_build.detail_html("issue_chat1")
    assert "note number 0" in html
    assert f"note number {issue_build._THREAD_RENDER_LIMIT + 4}" in html
    assert "not shown" not in html


def test_a_title_written_as_markup_is_escaped_on_the_detail_page(db):
    """Every dynamic value on this page is agent- or human-authored free text."""
    library_fake.seed_issue(**_mk(id="issue_xss1", title="<script>alert(1)</script>"))
    html = issue_build.detail_html("issue_xss1")
    assert "<script>alert(1)</script>" not in html


# --- specification / definition of done / maturity (issue_01M123PF05MFZPY2Q07XGB1CS0) -
#
# The Library has served all three on every item for a while; the detail page
# rendered none of them. Approving an epic is approving its specification, so
# a render that drops text silently is worse than no render at all — the
# reader would approve what they saw, and the stored field would hold
# something else. The load-bearing test below diffs the rendered page back
# against the stored fields rather than checking for a heading or a bullet.


_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|section|li|ul|ol|h[1-6]|blockquote|pre|br)(?:\s[^>]*)?>", re.I)
_INLINE_TAG_RE = re.compile(r"<[^>]+>")


def _rendered_text(html_fragment: str) -> str:
    """HTML back to prose for comparison against a stored field: block tags
    become line breaks, inline tags (``<strong>``, ``<code>``, ...) vanish
    with no substitute character, entities decode, and runs of whitespace
    collapse. Replacing an inline tag with a SPACE instead of nothing is the
    specific mistake that produces a false discrepancy: `<strong>identical
    </strong>, which` would compare as `identical , which`, reading as a
    dropped word where nothing was lost. `test_the_text_diff_survives_
    bold_adjacent_punctuation` pins that this does not happen."""
    text = _BLOCK_TAG_RE.sub("\n", html_fragment)
    text = _INLINE_TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    lines = [ln.strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _content_words(markdown_text: str) -> set[str]:
    """The words a rendered page must contain, stripping only Markdown's OWN
    structural punctuation (a leading `- `/`> `/`#`, and `**`/`` ` `` pairs) —
    never the field's own words. A word is `[A-Za-z0-9']+`, matching what
    `_rendered_text`'s output is tokenized the same way against, so formatting
    differences (a bullet marker consumed by the list renderer, a blockquote's
    `>` consumed by the parser) do not read as loss."""
    stripped = re.sub(r"^[\s\-*>#]+", "", markdown_text, flags=re.M)
    stripped = stripped.replace("**", "").replace("`", "")
    return set(re.findall(r"[A-Za-z0-9']+", stripped))


def test_the_text_diff_survives_bold_adjacent_punctuation():
    """The literal trap the spec calls out: naively turning every HTML tag
    into a space breaks `<strong>identical</strong>, which` into `identical ,
    which`. Plant that exact shape and confirm the real checker does not
    manufacture a discrepancy over it."""
    fragment = "<p><strong>identical</strong>, which survives</p>"
    assert "identical, which survives" in _rendered_text(fragment)


def test_specification_and_definition_of_done_render_every_character(db):
    """The whole text, diffed — not "the heading appears", not "the first
    bullet appears". Plants a `definition_of_done` line with no leading dash
    (the exact shape Deck's `- `-filtering generator silently dropped) and a
    `specification` line that is a blockquote rather than a paragraph, and
    asserts both survive to the page."""
    spec = (
        "The epic's specification is written as continuous prose, meant to "
        "be read in one sitting rather than collapsed behind a disclosure.\n\n"
        "> A line that is not a paragraph: an unindented blockquote sitting "
        "inside the specification is the shape most likely to be silently "
        "dropped by a renderer that only expects prose.\n\n"
        "It closes on a second ordinary paragraph naming SPECWORDMARKER so a "
        "test can confirm the whole field survived, not just its first line."
    )
    dod = (
        "- The first checkable item, DODBULLETONE.\n"
        "A line with no leading dash, DODPLAINLINE, which must still appear "
        "even though it is not a bullet.\n"
        "- The last checkable item, DODBULLETTWO."
    )
    library_fake.seed_issue(**_mk(id="issue_spec1", title="Epic with a spec",
              type="epic", specification=spec, definition_of_done=dod,
              maturity="approved"))
    html = issue_build.detail_html("issue_spec1")

    rendered = _rendered_text(html)
    rendered_words = set(re.findall(r"[A-Za-z0-9']+", rendered))
    missing = (_content_words(spec) | _content_words(dod)) - rendered_words
    assert not missing, f"detail page dropped words from spec/DoD: {sorted(missing)}"

    # The specific trap named in the spec, checked directly rather than only
    # through the word-set diff above: the non-bullet DoD line and the
    # blockquote spec line both made it onto the page as legible text.
    assert "DODPLAINLINE, which must still appear" in rendered
    assert "not a paragraph" in rendered
    assert "SPECWORDMARKER" in rendered
    assert 'maturity-approved">approved' in html


def test_an_item_with_none_of_the_three_omits_the_section_entirely(db):
    """A bug will never have a specification. No empty heading, no section at
    all, for the common case."""
    library_fake.seed_issue(**_mk(id="issue_plain1", title="Just a bug"))
    html = issue_build.detail_html("issue_plain1")
    assert 'class="spec-section"' not in html
    assert "Specification" not in html
    assert "Definition of done" not in html
    assert 'badge maturity' not in html


def test_maturity_alone_still_renders_the_section_with_no_text_fields(db):
    """An epic can be approved before its specification or definition of done
    is written. The section is gated on `spec or dod or maturity`, not on the
    two text fields alone — gating on only the text fields renders nothing for
    an approved epic with neither, which is indistinguishable from an epic
    that never had these fields at all."""
    library_fake.seed_issue(**_mk(id="issue_spec5", title="Approved, no text yet",
              type="epic", maturity="approved"))
    html = issue_build.detail_html("issue_spec5")
    assert 'class="spec-section"' in html
    assert 'maturity-approved">approved' in html
    assert "Specification" not in html
    assert "Definition of done" not in html


def test_an_epic_page_leads_with_its_spec_and_folds_its_body_under_a_closed_background(db):
    """The operator, 2026-09-22: the narration on an epic "takes up a ton of space", so
    the specification and definition of done lead and the body sits behind a
    closed "Background" disclosure. Closed means no `open` attribute: a
    disclosure rendered open would take the same space it exists to save."""
    library_fake.seed_issue(**_mk(id="issue_bg1", title="An epic with narration",
              type="epic", maturity="proposed", body="NARRATIONMARKER history",
              specification="SPECMARKER end state", definition_of_done="- DODMARKER"))
    html = issue_build.detail_html("issue_bg1")
    details = html.index('<details class="background">')
    assert html.index("SPECMARKER") < details
    assert html.index("DODMARKER") < details
    disclosure = html[details:html.index("</details>", details)]
    assert "<summary>Background</summary>" in disclosure
    assert "NARRATIONMARKER" in disclosure
    assert html.count("NARRATIONMARKER") == 1


def test_a_definition_of_done_alone_is_enough_to_fold_the_body(db):
    """The rule is spec OR done-when, so an epic with only a definition of done
    still leads with it."""
    library_fake.seed_issue(**_mk(id="issue_bg2", title="Done-when only",
              type="epic", body="NARRATIONMARKER", definition_of_done="- DODMARKER"))
    html = issue_build.detail_html("issue_bg2")
    assert html.index("DODMARKER") < html.index('<details class="background">')


def test_a_plain_items_body_is_not_wrapped(db):
    """For a correction or a work item the body is the content, so it stays
    open, and an empty body renders no disclosure at all."""
    library_fake.seed_issue(**_mk(id="issue_bg3", title="Just a bug", body="BUGBODYMARKER"))
    library_fake.seed_issue(**_mk(id="issue_bg4", title="An epic with no body",
              type="epic", specification="SPECMARKER", body=""))
    html = issue_build.detail_html("issue_bg3")
    assert "BUGBODYMARKER" in html
    assert "<details" not in html
    assert '<details class="background">' not in issue_build.detail_html("issue_bg4")


def test_a_resolved_epics_frozen_card_folds_its_body_too(db):
    """A terminal item renders through `_frozen_card`, a separate path from the
    active card, so it is checked on its own."""
    library_fake.seed_issue(**_mk(id="issue_bg5", title="A finished epic", type="epic",
              state="fixed", resolution="fixed", body="NARRATIONMARKER",
              specification="SPECMARKER"))
    html = issue_build.detail_html("issue_bg5")
    assert 'class="card frozen"' in html
    details = html.index('<details class="background">')
    assert "NARRATIONMARKER" in html[details:html.index("</details>", details)]


def test_the_queue_card_folds_an_epics_body_by_the_same_rule(db):
    """The Chart House list renders the same `_card`, so an epic with a
    specification shows there with its body folded, and a bug beside it does
    not."""
    library_fake.seed_issue(**_mk(id="issue_bg6", title="Listed epic", type="epic",
              body="EPICNARRATION", specification="SPECMARKER"))
    library_fake.seed_issue(**_mk(id="issue_bg7", title="Listed bug", body="BUGBODYMARKER"))
    html = issue_build.live_html() + issue_build.live_html_corrections()
    details = html.index('<details class="background">')
    assert "EPICNARRATION" in html[details:html.index("</details>", details)]
    # The bug's body sits inside no Background disclosure: any opened before it
    # has closed before it. (The page's own children-toggle disclosures are
    # other <details>, so the check names the class.)
    bug = html.index("BUGBODYMARKER")
    opened = html.rfind('<details class="background">', 0, bug)
    assert opened == -1 or html.index("</details>", opened) < bug


def test_maturity_renders_even_when_unset_so_it_cannot_be_mistaken_for_approved(db):
    """A specification with no visible maturity cannot be told from an
    approved one — the same class of gap as the missing render itself."""
    library_fake.seed_issue(**_mk(id="issue_spec2", title="Spec, no maturity",
              specification="enough text to trigger the section", maturity=None))
    html = issue_build.detail_html("issue_spec2")
    assert "maturity not set" in html
    # Scoped to the rendered BADGE, not the whole page: the queue's own
    # stylesheet now declares a `.badge.maturity-approved` rule
    # (issue_01M32CB6A9T04NVYPARGDT2GWQ), so the bare substring is present on
    # every render and the assertion would hold with the badge deleted.
    assert 'maturity maturity-approved' not in html


def test_specification_only_omits_the_definition_of_done_label(db):
    """Each field is gated independently: a specification with no DoD must
    not render an empty "Definition of done" section."""
    library_fake.seed_issue(**_mk(id="issue_spec3", title="Spec only",
              specification="just the specification, nothing else"))
    html = issue_build.detail_html("issue_spec3")
    assert "Specification" in html
    assert "Definition of done" not in html


def test_specification_markdown_is_xss_safe(db):
    """Agent- and human-written text, like `body` — raw HTML must never
    survive the render."""
    library_fake.seed_issue(**_mk(id="issue_spec4", title="Evil spec",
              specification='<img src=x onerror="alert(1)"> and <script>alert(2)</script>'))
    html = issue_build.detail_html("issue_spec4")
    section = html.split('class="spec-section"', 1)[1].split("</section>", 1)[0]
    assert "<img" not in section and "<script" not in section
    assert "&lt;img" in section and "&lt;script&gt;" in section


def test_log_swallowed_calls_the_shared_sanitiser(monkeypatch, capsys):
    # Pinned so a future edit that re-inlines the old copy fails here instead
    # of silently drifting from chart_house_build's and server.py's copies of
    # the same rule (issue_01M0VBZRCA15QSVYFMEF8MZ8N0).
    monkeypatch.setattr(issue_build, "clean_log_field", lambda v: "REPLACED")
    issue_build._log_swallowed("thing", ValueError("boom"))
    assert capsys.readouterr().err.strip() == "REPLACED"


# --- grouping the corrections queue by area (issue_01M22998K9JYPVYDAHQT0EX3G4)


def _topic_heads(html, product="vivy"):
    """The area headings rendered inside one project's section, in order."""
    m = re.search(rf'<section class="proj-group" data-prod="{product}">(.*?)</section>',
                  html, re.S)
    assert m, f"no section rendered for {product}"
    return re.findall(r'<h3 class="topic-head">(.*?) <span class="n">(\d+)</span>',
                      m.group(1))


def test_the_flat_view_is_what_a_page_with_no_sort_still_renders(db):
    """The default must not move. Someone reading the queue by lane today has
    not asked for anything and should see exactly what they saw before."""
    for n in range(3):
        library_fake.seed_issue(**_mk(title=f"i{n}", state="open", topics=["FLOW"]))
    html = issue_build.live_html_corrections()
    assert _topic_heads(html) == []
    assert '<div class="queue children">' in html      # the markup, unchanged


def test_sort_topic_groups_the_queue_by_area(db):
    """The point of the view: related work read together instead of scattered
    through one long list."""
    for n in range(3):
        library_fake.seed_issue(**_mk(title=f"flow{n}", state="open", topics=["FLOW"]))
    library_fake.seed_issue(**_mk(title="lock", state="open", topics=["LOCKS"]))
    heads = _topic_heads(issue_build.live_html_corrections({"sort": "topic"}))
    assert heads == [("FLOW", "3"), ("LOCKS", "1")]


def test_the_lane_order_survives_inside_a_group(db):
    """Grouping sits ABOVE the lane ordering rather than replacing it: unworked
    before the crew-drain outcomes, oldest-first within each. A reader still
    wants to know what is workable now, and inside an area is where they now
    ask it."""
    base = dt.datetime(2026, 7, 1)
    library_fake.seed_issue(**_mk(title="worked", state="pr-open", topics=["FLOW"],
                                  created_at=base))
    library_fake.seed_issue(**_mk(title="older-open", state="open", topics=["FLOW"],
                                  created_at=base + dt.timedelta(hours=1)))
    library_fake.seed_issue(**_mk(title="newer-open", state="open", topics=["FLOW"],
                                  created_at=base + dt.timedelta(hours=2)))
    html = issue_build.live_html_corrections({"sort": "topic"})
    order = [t for t in ("older-open", "newer-open", "worked")]
    positions = [html.index(t) for t in order]
    assert positions == sorted(positions), (
        "expected unworked before pr-open, oldest first within each")


def test_items_with_no_area_are_kept_and_sort_last(db):
    """Only one product's queue is backfilled today, so on every other project
    this group holds everything. Dropping it would hide the whole queue; leading
    with it would bury the areas that are the point of the view."""
    library_fake.seed_issue(**_mk(title="a", state="open"))
    library_fake.seed_issue(**_mk(title="b", state="open"))
    library_fake.seed_issue(**_mk(title="c", state="open"))
    library_fake.seed_issue(**_mk(title="d", state="open", topics=["FLOW"]))
    heads = _topic_heads(issue_build.live_html_corrections({"sort": "topic"}))
    assert heads == [("FLOW", "1"), (issue_build.NO_TOPIC_LABEL, "3")]


def test_grouping_renders_the_same_cards_as_the_flat_view(db):
    """A rearrangement, not a different fetch. If the two disagree on how many
    cards exist, one of them is wrong about the queue."""
    for n in range(6):
        library_fake.seed_issue(**_mk(title=f"i{n}", state="open",
                                      topics=["FLOW"] if n % 2 else None))
    flat = issue_build.live_html_corrections()
    grouped = issue_build.live_html_corrections({"sort": "topic"})
    assert flat.count('data-id=') == grouped.count('data-id=') > 0


def test_an_item_in_two_areas_appears_under_both(db):
    """`topics` is a list. No item carries two today, so this pins the rule
    rather than a case anyone has hit — a card must not be hidden from an area
    it belongs to just because another area claimed it first."""
    library_fake.seed_issue(**_mk(title="spans-two", state="open",
                                  topics=["FLOW", "LOCKS"]))
    heads = _topic_heads(issue_build.live_html_corrections({"sort": "topic"}))
    assert sorted(heads) == [("FLOW", "1"), ("LOCKS", "1")]


def test_an_unrecognised_sort_renders_the_flat_view(db):
    """A typo in the URL must not produce a third thing."""
    library_fake.seed_issue(**_mk(title="a", state="open", topics=["FLOW"]))
    assert _topic_heads(issue_build.live_html_corrections({"sort": "bogus"})) == []


# --- ordering the corrections queue by severity (issue_01M2P9385P23PMZCZFB1BZW9SN) --


def test_sort_severity_orders_worst_first(db):
    """The point of the view: `high` before `medium` before `low`."""
    library_fake.seed_issue(**_mk(title="sevtest-lo", state="open", severity="low"))
    library_fake.seed_issue(**_mk(title="sevtest-hi", state="open", severity="high"))
    library_fake.seed_issue(**_mk(title="sevtest-med", state="open", severity="medium"))
    html = issue_build.live_html_corrections({"sort": "severity"})
    positions = [html.index(t) for t in ("sevtest-hi", "sevtest-med", "sevtest-lo")]
    assert positions == sorted(positions)


def test_sort_severity_keeps_relative_order_within_a_severity(db):
    """Equal severity is not a tiebreaker case — the sort is stable, so items
    sharing a severity keep the order they already had.

    **The titles run counter to arrival order on purpose.** Seeded
    alphabetically, this test would pass against a sort that had quietly grown
    a title tiebreaker, because the two orderings would coincide and a broken
    sort and a stable one would render identically (round-1 review of PR #306).
    Charlie arrives first and alpha last, so the only ordering that satisfies
    the assertion is the arrival order this test exists to pin."""
    base = dt.datetime(2026, 7, 1)
    library_fake.seed_issue(**_mk(title="sevtest-eq-charlie", state="open", severity="high",
                                  created_at=base))
    library_fake.seed_issue(**_mk(title="sevtest-eq-bravo", state="open", severity="high",
                                  created_at=base + dt.timedelta(hours=1)))
    library_fake.seed_issue(**_mk(title="sevtest-eq-alpha", state="open", severity="high",
                                  created_at=base + dt.timedelta(hours=2)))
    html = issue_build.live_html_corrections({"sort": "severity"})
    positions = [html.index(t) for t in
                 ("sevtest-eq-charlie", "sevtest-eq-bravo", "sevtest-eq-alpha")]
    assert positions == sorted(positions)


def test_sort_severity_puts_unset_and_none_last_without_raising(db):
    """`severity` is not always one of the three ranked values: live data
    carries the literal string `"unset"`, and the field is `str | None`.
    Neither may be read as a benign `low` — both sort last, undistinguished
    from each other."""
    library_fake.seed_issue(**_mk(title="sevtest-hi", state="open", severity="high"))
    library_fake.seed_issue(**_mk(title="sevtest-unset", state="open", severity="unset"))
    library_fake.seed_issue(**_mk(title="sevtest-none", state="open", severity=None))
    html = issue_build.live_html_corrections({"sort": "severity"})
    hi_pos = html.index("sevtest-hi")
    assert hi_pos < html.index("sevtest-unset")
    assert hi_pos < html.index("sevtest-none")


def test_severity_sort_reaches_a_high_item_beyond_the_per_project_cap(db):
    """The test that fails if someone sorts after the cap: `_QUEUE_LIMIT` is
    100, so a project holding more must sort its FULL list before capping, or
    a high-severity item sitting beyond position 100 in lane order never
    renders on a page whose whole promise is worst-first
    (issue_01M2P9385P23PMZCZFB1BZW9SN's Bosun measurement: 178 corrections
    against the same cap)."""
    base = dt.datetime(2026, 7, 1)
    for n in range(issue_build._QUEUE_LIMIT + 20):
        library_fake.seed_issue(**_mk(
            title=f"low{n}", state="open", severity="low",
            created_at=base + dt.timedelta(minutes=n)))
    # Oldest-first lane order puts this item last — well beyond the cap —
    # unless the severity sort runs before the cap does.
    library_fake.seed_issue(**_mk(
        title="the-high-one", state="open", severity="high",
        created_at=base + dt.timedelta(days=1)))
    html = issue_build.live_html_corrections({"sort": "severity"})
    assert "the-high-one" in html


def test_flat_and_topic_sorts_are_unaffected_by_severity(db):
    """`?sort=topic` and the no-query default order by lane position only —
    `?sort=severity` existing beside them must not change what either shows."""
    base = dt.datetime(2026, 7, 1)
    library_fake.seed_issue(**_mk(title="older-low", state="open", severity="low",
                                  created_at=base))
    library_fake.seed_issue(**_mk(title="newer-high", state="open", severity="high",
                                  created_at=base + dt.timedelta(hours=1)))
    flat = issue_build.live_html_corrections()
    assert flat.index("older-low") < flat.index("newer-high")
    topic = issue_build.live_html_corrections({"sort": "topic"})
    assert topic.index("older-low") < topic.index("newer-high")


# --- prerequisites on the detail page (platform/decisions/0074) -----------------


def test_detail_lists_an_unmet_text_prerequisite_rather_than_counting_it(db):
    """Prerequisites replaced parking because a blocker living in a state plus a
    prose condition was only legible to whatever read that state. Rendering the
    condition is what makes it legible to a person."""
    library_fake.seed_issue(**_mk(
        id="issue_blk1", title="Blocked one", state="open",
        prerequisites=[{"text": "the Library serves status=all", "met": False,
                        "judged_by": None, "judged_at": None}]))
    html = issue_build.detail_html("issue_blk1")
    assert "Blocked" in html
    assert "the Library serves status=all" in html


def test_detail_still_shows_a_met_prerequisite(db):
    """A reader judging how close an item is needs what has cleared, not only
    what has not — so a met entry recedes rather than disappearing."""
    library_fake.seed_issue(**_mk(
        id="issue_blk2", title="Partly cleared", state="open",
        prerequisites=[{"text": "condition one", "met": True, "judged_by": "orient",
                        "judged_at": None},
                       {"text": "condition two", "met": False, "judged_by": None,
                        "judged_at": None}]))
    html = issue_build.detail_html("issue_blk2")
    assert "condition one" in html and "condition two" in html
    assert "1 of 2" in html
    assert "orient" in html          # judged_by, so a reader sees who cleared it


def test_detail_links_an_item_prerequisite_to_the_item_it_names(db):
    library_fake.seed_issue(**_mk(id="issue_OTHER", title="The thing it waits on"))
    library_fake.seed_issue(**_mk(
        id="issue_blk3", title="Waiting on work", state="open",
        prerequisites=[{"item": "issue_OTHER", "state": "open",
                        "title": "The thing it waits on"}]))
    html = issue_build.detail_html("issue_blk3")
    assert "The thing it waits on" in html
    assert "/engineering/issues/issue_OTHER" in html


def test_an_unreadable_item_prerequisite_shows_as_a_blocker_you_cannot_open(db):
    """**The case worth getting right.** An `{item:}` entry the reader is not
    entitled to see arrives with no state and no title. Hiding it would tell the
    reader the item is blocked by nothing; rendering an empty link would say the
    blocker is untitled. Both read as a defect in the page, so it is shown as a
    blocker, named by id, with no link."""
    library_fake.seed_issue(**_mk(
        id="issue_blk4", title="Waiting on something hidden", state="open",
        prerequisites=[{"item": "issue_SECRET", "missing": True}]))
    html = issue_build.detail_html("issue_blk4")
    assert "issue_SECRET" in html
    assert "not visible to you" in html
    assert '/engineering/issues/issue_SECRET"' not in html   # named, not linked


def test_an_item_with_no_prerequisites_gets_no_banner(db):
    library_fake.seed_issue(**_mk(id="issue_plain", title="Ordinary", state="open"))
    html = issue_build.detail_html("issue_plain")
    assert "outcome blocked" not in html


def test_an_item_whose_prerequisites_all_cleared_reads_as_unblocked(db):
    library_fake.seed_issue(**_mk(
        id="issue_clear", title="Cleared", state="open",
        prerequisites=[{"text": "done now", "met": True, "judged_by": "orient",
                        "judged_at": None}]))
    html = issue_build.detail_html("issue_clear")
    assert "Unblocked" in html
    assert "done now" in html


# --- every surface tells proposed from approved -----------------------------
# (issue_01M32CB6A9T04NVYPARGDT2GWQ)
#
# **The definition of done asks for this to be "established by listing every
# surface that lists epics and checking each, not by checking the Chart House
# alone."** The list, enumerated 2026-09-21 by grepping Orient for modules that
# read issues or Courses (`list_issue_items`, `get_issue_item`, `list_courses`,
# `get_course`, `IssueRecord`, `CourseEntry`):
#
#   - `/chart-house` — Course steps coloured by maturity, plus the Approve
#     section. Covered in `test_chart_house_build.py`.
#   - `/engineering/issues` — the epics queue. Covered by the test below; this
#     is the surface that DID render the two alike until this change.
#   - `/engineering/issues/{id}` — the detail page. Already carried a maturity
#     badge; covered by the sibling tests above.
#   - Home's waiting strip — carried the unplanned-epics tile, now removed.
#     Covered in `test_waiting_build.py`.
#
# Three more modules read Courses. `crews_build.py` mentions them only in
# prose and makes no Course call, so it lists no epic at all.
#
# **`/velocity` DOES name an epic, and an earlier version of this comment said
# it did not.** `waypoints._progress` takes the title of the first not-passed
# entry on each Course as `in_flight` (pinned by `test_waypoints.py`'s
# `test_mixed_course_reports_passed_in_flight_and_remaining`), and
# `velocity_build.py` renders it into that Course's row. It shows no maturity,
# so across rows a proposed in-flight epic and an approved one render alike.
#
# It is excluded BY JUDGEMENT, not because the problem is absent: the page
# lists Courses and names each one's current step as a pace readout, and
# nobody approves from it. The exclusion, and the fact that the page does name
# an epic, were put to the operator before PR #316 merged, so it merged with the gap
# stated rather than on a claim that there was none. If `/velocity` ever
# becomes somewhere an epic is acted on, this is the surface to revisit.


def test_the_queue_card_tells_a_proposed_epic_from_an_approved_one(db):
    """**The surface that used to fail this.** `_spec_section` put a maturity
    badge on the detail page and its docstring said "the detail page only,
    never the queue's card" — so on the queue, where 51 epics are scanned at
    once, an approved epic and one still awaiting the operator looked identical."""
    library_fake.seed_issue(**_mk(id="issue_ap", title="Approved epic",
                                  type="epic", maturity="approved"))
    library_fake.seed_issue(**_mk(id="issue_pr", title="Proposed epic",
                                  type="epic", maturity="proposed"))
    html = issue_build.live_html()
    assert "maturity maturity-approved" in html
    assert "maturity maturity-proposed" in html
    # Not merely different text: the two carry different classes, and the
    # stylesheet gives those classes different colour. A reader scanning a
    # queue reads colour before words.
    assert ".ops .badge.maturity-approved{" in html
    assert ".ops .badge.maturity-proposed{" in html


def test_an_epic_with_no_maturity_is_badged_rather_than_left_blank(db):
    """Unset is not approved, and a silent absence is exactly the case that
    used to read as approved."""
    library_fake.seed_issue(**_mk(id="issue_none", title="Unset epic",
                                  type="epic", maturity=None))
    html = issue_build.live_html()
    assert "maturity not set" in html


def test_a_bug_gets_no_maturity_badge(db):
    """`maturity` is an epic field. Badging every bug "maturity not set" would
    be the noise the type and needs badges are suppressed to avoid."""
    library_fake.seed_issue(**_mk(id="issue_bug", title="A bug", type="bug",
                                  maturity=None))
    html = issue_build.live_html()
    assert "maturity not set" not in html


# --- a proposed epic's action row: Decline and Approve (the operator, 2026-09-22) ------
#
# The operator: "Approve should also be down at the bottom of the page with the other
# buttons on the epic view. Also the existing buttons shouldn't be there for
# proposed epics, just Decline, Approve."
#
# The epic's own page renders the queue's `_card`, whose docstring insists the
# two stay one rendering, so the change is in `_card` and reaches both. Which
# epics qualify is `put_forward`, the rule the Chart House reads too.


def _card_actions(html: str) -> str:
    """The first card's action row, for asserting what buttons it offers."""
    start = html.index('<div class="actions"')
    return html[start:html.index("</div>", start)]


def test_a_proposed_epic_offers_decline_and_approve_and_nothing_else(db):
    """"Fixed", "Duplicate" and "Redirected" describe work that was done or
    moved, and a proposal has had neither — offering them invited an outcome
    that could not be true."""
    library_fake.seed_issue(**_mk(id="issue_prop", title="A proposal", type="epic",
                                  maturity="proposed", specification="spec"))
    row = _card_actions(issue_build.detail_html("issue_prop"))
    assert ">Decline<" in row and ">Approve<" in row
    for gone in (">Fixed<", ">Duplicate<", ">Redirected<", ">Won't do<"):
        assert gone not in row, f"a proposed epic must not offer {gone}"


def test_decline_is_the_resolve_routes_wontfix(db):
    """The Library has no separate decline route, and closing a proposal
    nobody will build is what `wontfix` records — so Decline reuses the
    resolve path, outcome note and all, rather than inventing a second one."""
    library_fake.seed_issue(**_mk(id="issue_prop", title="A proposal", type="epic",
                                  maturity="proposed", specification="spec"))
    row = _card_actions(issue_build.detail_html("issue_prop"))
    assert ('data-act="resolve" data-resolution="wontfix" data-kind="decline">Decline<'
            in row)


def test_an_approved_epic_keeps_the_four_outcomes(db):
    """Once approved it is real work on its Course, and the outcomes that
    describe finished or moved work apply to it again."""
    library_fake.seed_issue(**_mk(id="issue_appr", title="Approved", type="epic",
                                  maturity="approved", specification="spec"))
    row = _card_actions(issue_build.detail_html("issue_appr"))
    assert ">Approve<" not in row and ">Decline<" not in row
    for kept in (">Fixed<", ">Duplicate<", ">Redirected<", ">Won't do<"):
        assert kept in row


def test_a_bug_keeps_the_four_outcomes(db):
    library_fake.seed_issue(**_mk(id="issue_bug", title="A bug", type="bug"))
    row = _card_actions(issue_build.detail_html("issue_bug"))
    assert ">Approve<" not in row and ">Fixed<" in row


def test_an_unset_maturity_epic_with_a_specification_gets_approve_too(db):
    """The same `put_forward` rule the Chart House reads. A strict
    `== "proposed"` test stranded two live epics, and the two surfaces offering
    Approve must never disagree about which epics can be approved."""
    library_fake.seed_issue(**_mk(id="issue_unset", title="Unset", type="epic",
                                  maturity=None, specification="a real spec"))
    row = _card_actions(issue_build.detail_html("issue_unset"))
    assert ">Approve<" in row


def test_the_chart_house_and_the_card_read_one_rule():
    """Pinned rather than trusted: the rule moved out of `chart_house_build` so
    both surfaces call the same function, and a later copy would let them
    drift apart."""
    import chart_house_build
    assert not hasattr(chart_house_build, "_put_forward"), (
        "the Chart House must read issue_build.put_forward, not its own copy")
    assert "issue_build.put_forward(" in open(chart_house_build.__file__).read()


def test_approve_on_the_card_is_gated_on_the_viewers_role(db):
    """`__CAN_APPROVE_EPIC__` is server.py's to fill. The CSS hides Approve for
    a role without `epic_approve` and leaves Decline, which the resolve route
    already lets such a role use."""
    library_fake.seed_issue(**_mk(id="issue_prop", title="A proposal", type="epic",
                                  maturity="proposed", specification="spec"))
    html = issue_build.detail_html("issue_prop")
    assert 'data-can-approve="__CAN_APPROVE_EPIC__"' in _card_actions(html)
    assert ('.ops .actions[data-can-approve="false"] .btn.approve,\n'
            '.ops .actions[data-can-approve="false"] .btn.edit{display:none;}') in html


def _run_card_driver(html: str, scenario: str, body_js: str) -> dict:
    """Run `body_js` against the real rendered DOM with the ACTUAL `_JS` loaded,
    and return what it writes to stdout.

    Built on `_DOM_DRIVER_JS`'s own setup rather than a second DOM model, with
    two patches the card drivers need and the chip driver did not: a `remove()`
    that really detaches, since a successful approval replaces the buttons, and
    a `fetch` that records its calls and answers per `scenario` rather than
    always rejecting."""
    import json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to drive the inline JS against a DOM")
    setup = _DOM_DRIVER_JS[:_DOM_DRIVER_JS.index("var chipsEl = byId['chips'];")]
    driver = setup + r"""
const scenario = process.argv[4];
(function realRemove(n){
  n.remove = function(){ var p = this.parent; if (!p) return;
    var i = p.children.indexOf(this); if (i >= 0) p.children.splice(i, 1); };
  n.children.forEach(realRemove);
})(rootEl);
var calls = [];
sandbox.fetch = function(url, init){
  calls.push({ url: url, method: init && init.method,
               body: init && init.body ? JSON.parse(init.body) : null });
  if (scenario === 'network') return Promise.reject(new Error('down'));
  var ok = scenario === 'ok';
  var body = ok ? { approved: true, position: 5 }
                : { error: 'an epic needs a specification before it can be approved', approved: false };
  return Promise.resolve({ ok: ok, status: ok ? 200 : 409,
    json: function(){ return Promise.resolve(body); } });
};
var queue = byId['queue'];
""" + body_js
    tree = _html_to_tree(html)
    with tempfile.TemporaryDirectory() as tmp:
        paths = {name: f"{tmp}/{name}" for name in ("tree.json", "inline.js", "driver.js")}
        with open(paths["tree.json"], "w", encoding="utf-8") as f:
            json.dump(tree, f)
        with open(paths["inline.js"], "w", encoding="utf-8") as f:
            f.write(issue_build._JS)
        with open(paths["driver.js"], "w", encoding="utf-8") as f:
            f.write(driver)
        r = subprocess.run([node, paths["driver.js"], paths["tree.json"],
                            paths["inline.js"], scenario], capture_output=True, text=True)
        assert r.returncode == 0, f"card driver failed:\n{r.stdout}\n{r.stderr}"
        return json.loads(r.stdout)


def _drive_approve(html: str, scenario: str) -> dict:
    """Press Approve on the first card, with `fetch` answering per `scenario`."""
    return _run_card_driver(html, scenario, r"""
var btn = queue.querySelector('.approve'); // the harness matches one class per step
queue._dispatch('click', { target: btn });
setTimeout(function(){
  var row = queue.querySelector('.actions');
  var note = row.querySelector('.approve-note');
  process.stdout.write(JSON.stringify({
    calls: calls,
    buttons: row.querySelectorAll('button').map(function(b){
      return { text: b.children.length ? '' : (b.attrs['class'] || ''), disabled: b.disabled }; }),
    noteHidden: note.hidden, noteText: note.textContent, noteClass: note.className,
  }));
}, 20);
""")


def _drive_decline(html: str) -> dict:
    """Press Decline on the first card, read the outcome prompt it opened, then
    write a reason and press Confirm, with the resolve route answering ok."""
    return _run_card_driver(html, "ok", r"""
var card = queue.querySelector('.card');
queue._dispatch('click', { target: card.querySelector('.wontfix') });
var prompt = card.querySelector('.note-label').textContent;
card.querySelector('textarea').value = 'nobody will build this';
card.querySelector('.dupeid').value = '';  // the shim's inputs carry no value
queue._dispatch('click', { target: card.querySelector('.send') });
setTimeout(function(){
  process.stdout.write(JSON.stringify({
    calls: calls, prompt: prompt, toast: byId['toast'].textContent,
  }));
}, 20);
""")


@pytest.fixture
def proposed_page(db):
    library_fake.seed_issue(**_mk(id="issue_prop", title="A proposal", type="epic",
                                  maturity="proposed", specification="spec"))
    return issue_build.detail_html("issue_prop")


def test_pressing_approve_posts_to_the_one_approval_route(proposed_page):
    """The same route the Chart House calls, so there is one approval path. It
    sends no body: the server reads the epic and the approver for itself."""
    result = _drive_approve(proposed_page, "ok")
    assert result["calls"] == [{"url": "/api/epics/issue_prop/approve", "method": "POST",
                                "body": None}]


def test_a_successful_approval_replaces_the_buttons_with_its_record(proposed_page):
    """An approved epic stays open — it is now approved work — so the card is
    not removed the way a resolve removes it. Its buttons go and the note
    records the act, including where on the Course it landed."""
    result = _drive_approve(proposed_page, "ok")
    assert result["buttons"] == [], "Decline and Approve must both be gone after approval"
    assert result["noteHidden"] is False
    assert "position 5" in result["noteText"]
    assert "ok" in result["noteClass"]


def test_a_refusal_renders_in_the_card_and_keeps_the_controls(proposed_page):
    """The specification asks for the cause to be named WHERE THE CONTROL IS,
    and for a refused epic to stay proposed and keep its control. A toast
    would vanish before an unfamiliar Library message could be read."""
    result = _drive_approve(proposed_page, "refused")
    assert len(result["buttons"]) == 2, "both controls stay after a refusal"
    assert all(not b["disabled"] for b in result["buttons"]), "and are usable again"
    assert "specification" in result["noteText"], "the Library's own wording is shown"
    assert "err" in result["noteClass"]


def test_declining_asks_for_the_reason_the_proposing_crew_will_read(proposed_page):
    """"What fixed it" cannot be true of work never started, so Decline's
    prompt says who reads the note instead of reusing Won't do's."""
    result = _drive_decline(proposed_page)
    assert result["prompt"].startswith("Why it is declined")


def test_confirming_a_decline_closes_it_as_wontfix_and_says_declined(proposed_page):
    """Decline is the resolve route's `wontfix` with the reason attached, and
    the toast names the act that was pressed rather than "won't do"."""
    result = _drive_decline(proposed_page)
    assert result["calls"] == [{"url": "/api/issues/issue_prop/resolve", "method": "POST",
                                "body": {"resolution": "wontfix",
                                         "outcome_note": "nobody will build this"}}]
    assert result["toast"] == "✕ declined"


def test_an_unreachable_server_is_named_rather_than_left_silent(proposed_page):
    result = _drive_approve(proposed_page, "network")
    assert "reach the server" in result["noteText"]
    assert len(result["buttons"]) == 2


# --- the epic editor (the operator, 2026-09-22) ------------------------------------------


def _spec_section(html: str) -> str:
    start = html.index('<section class="spec-section"')
    return html[start:html.index("</section>", start)]


def test_a_proposed_epic_offers_edit_in_its_row_as_a_link_to_its_own_editor(proposed_page):
    """A link, because the queue's card carries no specification to edit: from
    the queue it opens the epic's page, and there `_SPEC_EDIT_JS` opens the
    editor in place."""
    row = _card_actions(proposed_page)
    assert '<a class="btn edit" href="/engineering/issues/issue_prop#edit">Edit</a>' in row


def test_the_editor_carries_the_raw_text_escaped_for_a_textarea(db):
    """What is edited is the source the Library stores, not the rendered HTML,
    and a specification holding markup cannot close the textarea early."""
    library_fake.seed_issue(**_mk(id="issue_prop", title="A proposal", type="epic",
                                  maturity="proposed",
                                  specification="**Bold** </textarea><script>x</script>",
                                  definition_of_done="- one\n- two"))
    sec = _spec_section(issue_build.detail_html("issue_prop"))
    assert 'data-edit="1"' in sec and 'data-can-approve="__CAN_APPROVE_EPIC__"' in sec
    assert ("**Bold** &lt;/textarea&gt;&lt;script&gt;x&lt;/script&gt;</textarea>") in sec
    assert ">- one\n- two</textarea>" in sec
    assert "<script>x</script>" not in sec


@pytest.mark.parametrize("kw", [
    {"maturity": "approved"},                         # its text is what was approved
    {"maturity": "proposed", "state": "resolved"},    # closed: its text is its record
])
def test_no_editor_where_the_route_would_refuse_it(db, kw):
    library_fake.seed_issue(**_mk(id="issue_x", title="X", type="epic",
                                  specification="spec", **kw))
    sec = _spec_section(issue_build.detail_html("issue_x"))
    assert 'data-edit' not in sec and "spec-edit" not in sec


def _drive_spec_edit(html: str, scenario: str, body_js: str, can_approve="true") -> dict:
    """Run the ACTUAL `_SPEC_EDIT_JS` against the real rendered page, then
    `body_js`. The shim has no `location`, so one is declared ahead of the
    script; its `reload` records the call instead of reloading."""
    import json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to drive the inline JS against a DOM")
    html = html.replace("__CAN_APPROVE_EPIC__", can_approve)
    setup = _DOM_DRIVER_JS[:_DOM_DRIVER_JS.index("var chipsEl = byId['chips'];")]
    prelude = "var location={hash:'',reloaded:0,reload:function(){this.reloaded++;}};\n"
    responses = {
        "ok": "{ok:true,status:200,body:{saved:true,fields:['specification']}}",
        "conflict": "{ok:false,status:409,body:{error:'this epic\\'s text changed since you "
                    "opened the editor.',conflict:true}}",
        "release": "{ok:true,status:200,body:{saved:true,release_error:'the epic is still "
                   "checked out as operator@example.com: reset'}}",
    }
    driver = setup.replace("vm.runInContext(jsSrc, sandbox);", "") + r"""
var calls = [];
sandbox.fetch = function(url, init){
  calls.push({ url: url, method: init.method, body: JSON.parse(init.body) });
  var r = """ + responses[scenario] + r""";
  return Promise.resolve({ ok: r.ok, status: r.status,
    json: function(){ return Promise.resolve(r.body); } });
};
vm.runInContext(jsSrc, sandbox);
var sec = rootEl.querySelector('.spec-section');
var form = sec.querySelector('.spec-edit');
var spec = form.querySelector('.spec-edit-spec'), dod = form.querySelector('.spec-edit-dod');
var note = form.querySelector('.spec-edit-note');
spec.value = 'old spec'; dod.value = '- old bullet';
function click(el){ (el._listeners.click || []).forEach(function(fn){
  fn({ target: el, preventDefault: function(){} }); }); }
""" + body_js + r"""
setTimeout(function(){
  process.stdout.write(JSON.stringify({
    calls: calls, formOpen: form.hidden === false, noteText: note.textContent,
    noteClass: note.className, spec: spec.value, reloaded: sandbox.location.reloaded,
  }));
}, 20);
"""
    tree = _html_to_tree(html)
    with tempfile.TemporaryDirectory() as tmp:
        paths = {name: f"{tmp}/{name}" for name in ("tree.json", "inline.js", "driver.js")}
        with open(paths["tree.json"], "w", encoding="utf-8") as f:
            json.dump(tree, f)
        with open(paths["inline.js"], "w", encoding="utf-8") as f:
            f.write(prelude + issue_build._SPEC_EDIT_JS)
        with open(paths["driver.js"], "w", encoding="utf-8") as f:
            f.write(driver)
        r = subprocess.run([node, paths["driver.js"], paths["tree.json"],
                            paths["inline.js"], scenario], capture_output=True, text=True)
        assert r.returncode == 0, f"spec-edit driver failed:\n{r.stdout}\n{r.stderr}"
        return json.loads(r.stdout)


@pytest.fixture
def editable_page(db):
    library_fake.seed_issue(**_mk(id="issue_prop", title="A proposal", type="epic",
                                  maturity="proposed", specification="old spec",
                                  definition_of_done="- old bullet"))
    return issue_build.detail_html("issue_prop")


_EDIT_AND_SAVE = r"""
click(rootEl.querySelector('.actions .edit'));
spec.value = 'new spec';
click(form.querySelector('.spec-edit-save'));
"""


def test_the_rows_edit_opens_the_editor_and_save_sends_the_opening_text_as_base(editable_page):
    """`base` is what the server compares with the epic under the lease, so it
    must be the text the editor OPENED with, not the draft."""
    result = _drive_spec_edit(editable_page, "ok", _EDIT_AND_SAVE)
    assert result["calls"] == [{
        "url": "/api/epics/issue_prop/revise", "method": "POST",
        "body": {"specification": "new spec", "definition_of_done": "- old bullet",
                 "base": {"specification": "old spec", "definition_of_done": "- old bullet"}}}]
    assert result["reloaded"] == 1, "a save reloads, so the page renders the saved text"


def test_a_conflict_keeps_the_draft_and_names_the_cause(editable_page):
    result = _drive_spec_edit(editable_page, "conflict", _EDIT_AND_SAVE)
    assert result["reloaded"] == 0
    assert result["spec"] == "new spec", "nothing typed is lost"
    assert "changed since you opened" in result["noteText"]
    assert "err" in result["noteClass"]


def test_a_save_whose_release_failed_says_so_instead_of_reloading(editable_page):
    result = _drive_spec_edit(editable_page, "release", _EDIT_AND_SAVE)
    assert result["reloaded"] == 0
    assert "still checked out" in result["noteText"]


def test_a_viewer_without_the_capability_cannot_open_it(editable_page):
    """Cosmetic like every such gate — the route refuses regardless — but the
    script must not open an editor the CSS hid."""
    result = _drive_spec_edit(editable_page, "ok", _EDIT_AND_SAVE, can_approve="false")
    assert result["formOpen"] is False


# --- the model override (issue_01M38WVA7XHEDN1NFNPP8KDM0C) -------------------

_CATALOG = [
    model_catalog.Model(model="claude-opus-5-5", alias="opus", provider="anthropic"),
    model_catalog.Model(model="openai/gpt-5", alias="gpt5", provider="gateway"),
    model_catalog.Model(model="claude-opus-4-8", alias=None, provider="anthropic"),
]


@pytest.fixture
def listed(monkeypatch):
    """agentctl's model list as the page reads it. Without this the page would
    run the real agentctl, which CI does not have."""
    catalog = {"value": list(_CATALOG)}
    monkeypatch.setattr(model_catalog, "load", lambda: catalog["value"])
    return catalog


def _choice(model="claude-opus-5-5", reason="needs frontier judgment"):
    return library_client.ModelChoice(
        model=model, reason=reason, set_by_client="convivy-orient",
        acting_human="operator@example.com", set_at=dt.datetime(2026, 9, 27, 19, 0))


def test_a_set_model_is_shown_with_who_chose_it(db, listed):
    """Shown as its own section, with the model, the person, the client and the
    reason, since the choice changes what the fleet runs this item on."""
    library_fake.seed_issue(**_mk(id="issue_model1", title="Queued", model_choice=_choice()))
    html = issue_build.detail_html("issue_model1")
    assert 'class="model-section"' in html
    assert '<code class="model-name">claude-opus-5-5</code>' in html
    assert "chosen by operator@example.com through convivy-orient" in html
    assert "needs frontier judgment" in html
    assert 'class="btn model-clear"' in html


def test_a_queued_item_offers_the_control_behind_the_role_token(db, listed):
    library_fake.seed_issue(**_mk(id="issue_model2", title="Queued, no choice"))
    html = issue_build.detail_html("issue_model2")
    assert "No model chosen" in html
    assert 'class="btn approve model-set"' in html
    assert 'data-can-set="__CAN_SET_ITEM_MODEL__"' in html
    assert 'class="btn model-clear"' not in html


def test_an_item_being_worked_shows_its_choice_without_the_control(db, listed):
    """A live claim means a run may be under way, and it keeps its model, so
    the page shows the choice and offers no change."""
    library_fake.seed_issue(**_mk(id="issue_model3", title="Claimed", state="claimed",
                                  claimed_by="agent-x", claimed_at=models_utcnow(),
                                  model_choice=_choice()))
    html = issue_build.detail_html("issue_model3")
    assert "claude-opus-5-5" in html
    assert 'class="btn approve model-set"' not in html


def test_a_finished_item_with_no_choice_has_no_model_section(db, listed):
    library_fake.seed_issue(**_mk(id="issue_model4", title="Done", state="fixed",
                                  resolution="fixed", resolved_by="operator@example.com"))
    assert 'class="model-section"' not in issue_build.detail_html("issue_model4")


def test_a_model_choice_is_escaped(db, listed):
    library_fake.seed_issue(**_mk(id="issue_model5", title="Hostile", model_choice=_choice(
        model="<script>x</script>", reason="<img src=x onerror=1>")))
    html = issue_build.detail_html("issue_model5")
    assert "<script>x</script>" not in html
    assert "<img src=x onerror=1>" not in html


def _drive_model(html: str, scenario: str, body_js: str, can_set="true") -> dict:
    """Run the ACTUAL `_MODEL_JS` against the real rendered page, on the same
    DOM shim `_drive_spec_edit` uses, then `body_js`."""
    import json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to drive the inline JS against a DOM")
    html = html.replace("__CAN_SET_ITEM_MODEL__", can_set)
    setup = _DOM_DRIVER_JS[:_DOM_DRIVER_JS.index("var chipsEl = byId['chips'];")]
    prelude = "var location={hash:'',reloaded:0,reload:function(){this.reloaded++;}};\n"
    responses = {
        "ok": "{ok:true,status:200,body:{model_choice:null}}",
        "refused": "{ok:false,status:409,body:{error:'another holder\\'s live lease covers the item'}}",
    }
    driver = setup.replace("vm.runInContext(jsSrc, sandbox);", "") + r"""
var calls = [];
sandbox.fetch = function(url, init){
  calls.push({ url: url, method: init.method, body: JSON.parse(init.body) });
  var r = """ + responses[scenario] + r""";
  return Promise.resolve({ ok: r.ok, status: r.status,
    json: function(){ return Promise.resolve(r.body); } });
};
vm.runInContext(jsSrc, sandbox);
var sec = rootEl.querySelector('.model-section');
var pick = sec.querySelector('.model-select'), why = sec.querySelector('.model-reason-input');
var note = sec.querySelector('.model-note');
if (pick) pick.value = ''; why.value = '';
function click(el){ (el._listeners.click || []).forEach(function(fn){
  fn({ target: el, preventDefault: function(){} }); }); }
""" + body_js + r"""
setTimeout(function(){
  process.stdout.write(JSON.stringify({
    calls: calls, noteText: note.textContent, noteClass: note.className,
    noteHidden: note.hidden, reloaded: sandbox.location.reloaded,
  }));
}, 20);
"""
    tree = _html_to_tree(html)
    with tempfile.TemporaryDirectory() as tmp:
        paths = {name: f"{tmp}/{name}" for name in ("tree.json", "inline.js", "driver.js")}
        with open(paths["tree.json"], "w", encoding="utf-8") as f:
            json.dump(tree, f)
        with open(paths["inline.js"], "w", encoding="utf-8") as f:
            f.write(prelude + issue_build._MODEL_JS)
        with open(paths["driver.js"], "w", encoding="utf-8") as f:
            f.write(driver)
        r = subprocess.run([node, paths["driver.js"], paths["tree.json"],
                            paths["inline.js"], scenario], capture_output=True, text=True)
        assert r.returncode == 0, f"model driver failed:\n{r.stdout}\n{r.stderr}"
        return json.loads(r.stdout)


@pytest.fixture
def model_page(db, listed):
    library_fake.seed_issue(**_mk(id="issue_mq", title="Queued", model_choice=_choice()))
    return issue_build.detail_html("issue_mq")


_SET = r"""
pick.value = 'claude-opus-5-5'; why.value = 'needs frontier judgment';
click(sec.querySelector('.model-set'));
"""


def test_set_sends_the_chosen_model_and_reason_and_reloads(model_page):
    result = _drive_model(model_page, "ok", _SET)
    assert result["calls"] == [{
        "url": "/api/items/issue_mq/model", "method": "POST",
        "body": {"model": "claude-opus-5-5", "reason": "needs frontier judgment"}}]
    assert result["reloaded"] == 1, "a success reloads, so the page shows the Library's record"


def test_clear_sends_a_null_model(model_page):
    result = _drive_model(model_page, "ok", "click(sec.querySelector('.model-clear'));")
    assert result["calls"] == [{"url": "/api/items/issue_mq/model", "method": "POST",
                                "body": {"model": None, "reason": None}}]


def test_a_refusal_renders_the_librarys_wording_where_the_control_is(model_page):
    result = _drive_model(model_page, "refused", _SET)
    assert result["reloaded"] == 0
    assert result["noteHidden"] is False
    assert "live lease" in result["noteText"]
    assert "err" in result["noteClass"]


def test_an_empty_model_is_not_sent(model_page):
    result = _drive_model(model_page, "ok", "click(sec.querySelector('.model-set'));")
    assert result["calls"] == []
    assert "Choose a model" in result["noteText"]


def test_a_viewer_without_the_capability_gets_no_handlers(model_page):
    """Cosmetic, like every such gate: the route refuses regardless, but the
    script must not send from a control the CSS hid."""
    result = _drive_model(model_page, "ok", _SET, can_set="false")
    assert result["calls"] == []


# --- only the models agentctl dispatches (issue_01M3NWDHCDQK6PE9NEXFQ37G9K) ---


def test_the_control_offers_agentctls_models_in_its_order(db, listed):
    """Valued by the resolved id, labelled with the alias where there is one,
    and with no free-text input left to type an id nobody dispatches."""
    library_fake.seed_issue(**_mk(id="issue_ml1", title="Queued"))
    html = issue_build.detail_html("issue_ml1")
    values = re.findall(r'<option value="([^"]*)">', html)
    assert values == ["", "claude-opus-5-5", "openai/gpt-5", "claude-opus-4-8"]
    assert '<option value="claude-opus-5-5">opus — claude-opus-5-5</option>' in html
    assert '<option value="claude-opus-4-8">claude-opus-4-8</option>' in html
    assert 'class="model-input"' not in html


@pytest.mark.parametrize("unavailable", [None, []])
def test_an_unavailable_list_says_so_and_offers_no_set(db, listed, unavailable):
    listed["value"] = unavailable
    library_fake.seed_issue(**_mk(id="issue_ml2", title="Queued"))
    html = issue_build.detail_html("issue_ml2")
    assert 'class="model-unavailable"' in html
    assert "<select" not in html
    assert 'class="btn approve model-set"' not in html
    assert 'class="model-actions"' not in html, "no reason box and no buttons with nothing to act on"


def test_an_unavailable_list_still_lets_a_choice_be_cleared(db, listed):
    listed["value"] = None
    library_fake.seed_issue(**_mk(id="issue_ml3", title="Queued", model_choice=_choice()))
    html = issue_build.detail_html("issue_ml3")
    assert 'class="model-unavailable"' in html
    assert 'class="btn model-clear"' in html
    assert 'class="btn approve model-set"' not in html
    result = _drive_model(html, "ok", "click(sec.querySelector('.model-clear'));")
    assert result["calls"] == [{"url": "/api/items/issue_ml3/model", "method": "POST",
                                "body": {"model": None, "reason": None}}]


def test_a_finished_item_does_not_run_agentctl(db, monkeypatch):
    """Only a queued item has a control to fill, so no other page pays for the
    subprocess."""
    def boom():
        raise AssertionError("model_catalog.load ran for an item that is not queued")
    monkeypatch.setattr(model_catalog, "load", boom)
    library_fake.seed_issue(**_mk(id="issue_ml4", title="Done", state="fixed",
                                  resolution="fixed", resolved_by="operator@example.com",
                                  model_choice=_choice()))
    assert "claude-opus-5-5" in issue_build.detail_html("issue_ml4")


# --- reviews, read from the Library's review record --------------------------
# issue_01M38316270TMDKT5MQSAANEM3: an item linking a pull request shows that
# PR's reviews from `GET /review-records`, not from captured GitHub comments.

_ORIENT = "github:convivy/convivy-orient"
_PR_URL = "https://github.com/convivy/convivy-orient/pull/350"


def _reviews(html):
    start = html.index('class="spec-section reviews-section"')
    return html[start:html.index("</section>", start)]


def test_an_item_linking_a_pr_shows_its_reviews_from_the_record(db):
    library_fake.seed_issue(**_mk(id="issue_rv1", refs={"pr": "350", "url": _PR_URL}))
    library_fake.seed_review_record(repo_identity=_ORIENT, pr=350, role="reviewer",
                                    verdict="APPROVE", reviewed_ref="b" * 40,
                                    prose="Looks **right**.", run_id="run-1")
    section = _reviews(issue_build.detail_html("issue_rv1"))
    assert "PR #350" in section
    assert "reviewer" in section and "APPROVE" in section
    assert "<strong>right</strong>" in section       # prose renders as markdown
    assert "stale" not in section and "does not count" not in section


def test_an_unverified_record_is_shown_and_marked_as_not_counting(db):
    library_fake.seed_issue(**_mk(id="issue_rv2", refs={"pr": "350", "url": _PR_URL}))
    library_fake.seed_review_record(repo_identity=_ORIENT, pr=350, role="security-reviewer",
                                    verdict="APPROVE", reviewed_ref="b" * 40,
                                    auth="unverified", auth_reason="backfilled comment")
    section = _reviews(issue_build.detail_html("issue_rv2"))
    assert "security-reviewer" in section
    assert "unverified (backfilled comment): does not count toward a merge" in section


def test_a_review_of_an_older_commit_is_marked_stale(db):
    library_fake.seed_issue(**_mk(id="issue_rv3", refs={"pr": "350", "url": _PR_URL}))
    library_fake.seed_review_record(repo_identity=_ORIENT, pr=350, role="security-reviewer",
                                    verdict="REQUEST_CHANGES", reviewed_ref="a" * 40)
    library_fake.seed_review_record(repo_identity=_ORIENT, pr=350, role="reviewer",
                                    verdict="APPROVE", reviewed_ref="b" * 40)
    section = _reviews(issue_build.detail_html("issue_rv3"))
    old = section[section.index("security-reviewer"):]
    assert "stale: the newest review is of bbbbbbbbb" in old
    new = section[:section.index("security-reviewer")]
    assert "stale" not in new


def test_one_entry_per_role_per_commit_keeping_the_newest(db):
    library_fake.seed_issue(**_mk(id="issue_rv4", refs={"pr": "350", "url": _PR_URL}))
    library_fake.seed_review_record(repo_identity=_ORIENT, pr=350, role="reviewer",
                                    verdict="REQUEST_CHANGES", reviewed_ref="b" * 40,
                                    prose="first post")
    library_fake.seed_review_record(repo_identity=_ORIENT, pr=350, role="reviewer",
                                    verdict="APPROVE", reviewed_ref="b" * 40,
                                    prose="re-post")
    section = _reviews(issue_build.detail_html("issue_rv4"))
    assert section.count('class="rv"') == 1
    assert "re-post" in section and "first post" not in section


def test_findings_render_each_field(db):
    library_fake.seed_issue(**_mk(id="issue_rv5", refs={"pr": "350", "url": _PR_URL}))
    library_fake.seed_review_record(repo_identity=_ORIENT, pr=350, role="reviewer",
                                    verdict="REQUEST_CHANGES", reviewed_ref="b" * 40,
                                    findings=[{"file": "x.py", "summary": "<b>off</b> by one"}])
    section = _reviews(issue_build.detail_html("issue_rv5"))
    assert "x.py" in section and "by one" in section
    assert "<b>off</b>" not in section                # writer text is escaped


def test_a_pr_with_no_record_says_so(db):
    library_fake.seed_issue(**_mk(id="issue_rv6", refs={"pr": "350", "url": _PR_URL}))
    section = _reviews(issue_build.detail_html("issue_rv6"))
    assert "holds no review record" in section


def test_records_of_another_repo_or_pr_do_not_appear(db):
    library_fake.seed_issue(**_mk(id="issue_rv7", refs={"pr": "350", "url": _PR_URL}))
    library_fake.seed_review_record(repo_identity="github:convivy/vivy", pr=350,
                                    role="reviewer", verdict="APPROVE", reviewed_ref="c" * 40)
    library_fake.seed_review_record(repo_identity=_ORIENT, pr=351,
                                    role="reviewer", verdict="APPROVE", reviewed_ref="d" * 40)
    section = _reviews(issue_build.detail_html("issue_rv7"))
    assert "holds no review record" in section


@pytest.mark.parametrize("refs", [
    {},
    {"repo": "www", "fingerprint": "x"},
    {"url": "https://example.com/convivy/convivy-orient/pull/1"},
])
def test_no_reviews_section_without_a_github_pr_url(db, refs):
    library_fake.seed_issue(**_mk(id="issue_rv8", refs=refs))
    assert "reviews-section" not in issue_build.detail_html("issue_rv8")


def test_the_client_reads_the_route_by_repo_identity_and_pr(monkeypatch):
    calls = []

    def page_all(path, key, params=None):
        calls.append((path, key, params))
        return [{"id": "rev_1", "repo_identity": _ORIENT, "pr": 350,
                 "reviewed_ref": "b" * 40, "role": "reviewer", "run_id": "run-1",
                 "identity_key": "run-1", "verdict": "APPROVE", "findings": [],
                 "prose": "ok", "paths": [], "doc_ids": [], "item_ids": [],
                 "auth": "run-verified", "auth_reason": None, "content_sha": "x",
                 "version": 1, "source_comment_id": None, "signal_id": None,
                 "created_at": "2026-09-30T15:00:00Z", "updated_at": "2026-09-30T15:00:00Z"}]

    monkeypatch.setattr(library_client, "_page_all", page_all)
    [rec] = library_client.list_review_records(_ORIENT, 350)
    assert calls == [("/review-records", "review_records",
                      {"repo_identity": _ORIENT, "pr": "350"})]
    assert rec.role == "reviewer" and rec.auth == "run-verified"
    assert rec.created_at == dt.datetime(2026, 9, 30, 15, 0)


# --- per-viewer project scope (E19·2, issue_01M35K5AB4PRTJ71DMQW7Q4D3B) ------
#
# `scope` is the viewer's Library-resolved project scope (`library_client.
# person_scope`), threaded into every Chart House builder and the single-item
# page. An administrator's scope is simply the full project set the Library
# returns for them ("*"), so these tests cover both the narrowed case and
# that wide one, never a role or admin flag of this module's own.


def _seed_two_projects():
    """vivy and bosun, each with an epic + child, a correction, a
    blocker and a live Course — so every Chart House page has one entry per
    project to tell apart."""
    library_fake.seed_issue(**_mk(id="issue_vivy_epic", product="vivy",
                                  title="VIVY-EPIC-TITLE", type="epic"))
    library_fake.seed_issue(**_mk(id="issue_vivy_child", product="vivy",
                                  title="VIVY-CHILD-TITLE", parent_id="issue_vivy_epic"))
    library_fake.seed_issue(**_mk(id="issue_vivy_corr", product="vivy",
                                  title="VIVY-CORRECTION-TITLE"))
    library_fake.seed_issue(**_mk(id="issue_vivy_block", product="vivy",
                                  title="VIVY-BLOCKER-TITLE", needs="operator"))

    library_fake.seed_issue(**_mk(id="issue_bosun_epic", product="bosun",
                                  title="BOSUN-EPIC-TITLE", type="epic"))
    library_fake.seed_issue(**_mk(id="issue_bosun_child", product="bosun",
                                  title="BOSUN-CHILD-TITLE", parent_id="issue_bosun_epic"))
    library_fake.seed_issue(**_mk(id="issue_bosun_corr", product="bosun",
                                  title="BOSUN-CORRECTION-TITLE"))
    library_fake.seed_issue(**_mk(id="issue_bosun_block", product="bosun",
                                  title="BOSUN-BLOCKER-TITLE", needs="operator"))


def _two_project_courses():
    return [
        _course("course_vivy", "vivy", "Vivy's course",
               entries=[_entry("issue_vivy_epic", "open", title="VIVY-COURSE-STEP")]),
        _course("course_bosun", "bosun", "Bosun's course",
               entries=[_entry("issue_bosun_epic", "open", title="BOSUN-COURSE-STEP")]),
    ]


def test_each_viewer_sees_only_their_own_projects_on_every_chart_house_page(db, monkeypatch):
    """Two projects, two identities: each identity's Epics, Corrections, Course
    and Blockers page carries only its own project's name, titles and item
    ids — neither page names the other project at all."""
    _seed_two_projects()
    courses = _two_project_courses()
    monkeypatch.setattr(library_client, "list_courses", lambda **k: courses)
    monkeypatch.setattr(library_client, "get_course", lambda cid: next(c for c in courses if c.id == cid))
    library_fake.seed_person_scope("vivy-viewer@example.com", {"vivy"})
    library_fake.seed_person_scope("bosun-viewer@example.com", {"bosun"})
    vivy_scope = library_client.person_scope("vivy-viewer@example.com")
    bosun_scope = library_client.person_scope("bosun-viewer@example.com")

    # Each page's own content, by project — only the needles a correctly
    # scoped AND correctly rendered page would carry for that project, so
    # this also catches a page bleeding another page's lane rather than
    # another viewer's project.
    pages = {
        "epics": (issue_build.live_html,
                 ('data-prod="vivy"', "VIVY-EPIC-TITLE", "VIVY-CHILD-TITLE", "issue_vivy_epic"),
                 ('data-prod="bosun"', "BOSUN-EPIC-TITLE", "BOSUN-CHILD-TITLE", "issue_bosun_epic")),
        "corrections": (lambda s: issue_build.live_html_corrections(None, s),
                       ('data-prod="vivy"', "VIVY-CORRECTION-TITLE", "issue_vivy_corr"),
                       ('data-prod="bosun"', "BOSUN-CORRECTION-TITLE", "issue_bosun_corr")),
        "course": (issue_build.live_html_course,
                  ('data-prod="vivy"', "VIVY-COURSE-STEP"),
                  ('data-prod="bosun"', "BOSUN-COURSE-STEP")),
        "blockers": (issue_build.live_html_blockers,
                    ('data-prod="vivy"', "VIVY-BLOCKER-TITLE", "issue_vivy_block"),
                    ('data-prod="bosun"', "BOSUN-BLOCKER-TITLE", "issue_bosun_block")),
    }
    for name, (builder, vivy_needles, bosun_needles) in pages.items():
        vivy_html = builder(vivy_scope)
        for needle in vivy_needles:
            assert needle in vivy_html, (name, needle)
        for needle in bosun_needles:
            assert needle not in vivy_html, (name, needle)

        bosun_html = builder(bosun_scope)
        for needle in bosun_needles:
            assert needle in bosun_html, (name, needle)
        for needle in vivy_needles:
            assert needle not in bosun_html, (name, needle)


def test_an_administrators_resolved_scope_still_sees_every_project(db, monkeypatch):
    """An administrator's scope is the full project set the Library returns
    for them — "*" — and this module draws no admin-role distinction of its
    own, so both projects render on every page given that same wide scope."""
    _seed_two_projects()
    courses = _two_project_courses()
    monkeypatch.setattr(library_client, "list_courses", lambda **k: courses)
    monkeypatch.setattr(library_client, "get_course", lambda cid: next(c for c in courses if c.id == cid))
    library_fake.seed_person_scope("admin@example.com", {"*"})
    admin_scope = library_client.person_scope("admin@example.com")

    epics = issue_build.live_html(admin_scope)
    assert 'data-prod="vivy"' in epics and 'data-prod="bosun"' in epics
    corrections = issue_build.live_html_corrections(None, admin_scope)
    assert 'data-prod="vivy"' in corrections and 'data-prod="bosun"' in corrections
    course_page = issue_build.live_html_course(admin_scope)
    assert 'data-prod="vivy"' in course_page and 'data-prod="bosun"' in course_page
    blockers = issue_build.live_html_blockers(admin_scope)
    assert "VIVY-BLOCKER-TITLE" in blockers and "BOSUN-BLOCKER-TITLE" in blockers


def test_an_out_of_scope_item_page_is_none_exactly_like_a_missing_one(db):
    """`detail_html` returns None for both (E19·2,
    issue_01M35K5AB4PRTJ71DMQW7Q4D3B) — server.py turns None into the same 404
    either way, so an id outside scope must not be told apart from one that
    does not exist at all."""
    library_fake.seed_issue(**_mk(id="issue_outscope", product="bosun", title="t"))
    scope = frozenset({"vivy"})
    assert issue_build.detail_html("issue_outscope", scope) is None
    assert issue_build.detail_html("issue_does_not_exist_either", scope) is None
    # The positive control: the same item, read with a scope that covers it.
    assert issue_build.detail_html("issue_outscope", frozenset({"bosun"})) is not None


def test_a_found_during_item_outside_scope_is_dropped_from_the_epic_page(db):
    """A found-during item can be filed against a different product than the
    epic it names (that cross-product case is the feature) — so it leaks that
    other project's title unless filtered on its own, separately from the
    epic's already-checked visibility."""
    library_fake.seed_issue(**_mk(id="issue_epicfd2", product="vivy",
                                  title="The epic", type="epic"))
    library_fake.seed_issue(**_mk(id="issue_found_outscope", product="bosun",
                                  title="BOSUN-FOUND-TITLE", found_during="issue_epicfd2"))
    library_fake.seed_issue(**_mk(id="issue_found_inscope", product="vivy",
                                  title="VIVY-FOUND-TITLE", found_during="issue_epicfd2"))
    scope = frozenset({"vivy"})
    html = issue_build.detail_html("issue_epicfd2", scope)
    assert "VIVY-FOUND-TITLE" in html
    assert "BOSUN-FOUND-TITLE" not in html
    assert "Found during this epic · 1" in html


def test_a_found_during_link_to_an_epic_outside_scope_is_not_shown(db):
    """The reverse direction of the test above (review round 1 on PR #372): an
    in-scope item found during an epic in another product must not show that
    epic's id, which would confirm it exists. The same page read with a scope
    that covers the epic shows the link, so the omission is the scope check
    rather than a missing link."""
    library_fake.seed_issue(**_mk(id="issue_bosun_epic_x", product="bosun",
                                  title="Bosun epic", type="epic"))
    library_fake.seed_issue(**_mk(id="issue_vivy_found_x", product="vivy",
                                  title="Vivy item", found_during="issue_bosun_epic_x"))
    scoped = issue_build.detail_html("issue_vivy_found_x", frozenset({"vivy"}))
    assert "issue_bosun_epic_x" not in scoped
    assert "found during" not in scoped
    both = issue_build.detail_html("issue_vivy_found_x", frozenset({"vivy", "bosun"}))
    assert 'found during <a href="/engineering/issues/issue_bosun_epic_x">' in both


def test_a_duplicate_of_an_item_outside_scope_names_no_id(db):
    """Review round 2 on PR #372: the resolve API accepts any duplicate_of, so a
    vivy item can be marked a duplicate of a bosun one. A viewer scoped to vivy
    must not see that bosun id, on the item's page or in the Corrections
    page's resolved strip. A viewer whose scope covers bosun sees it in both
    places, so the omission is the scope check."""
    library_fake.seed_issue(**_mk(id="issue_bosun_orig_x", product="bosun", title="Bosun original"))
    library_fake.seed_issue(**_mk(id="issue_vivy_dupe_x", product="vivy", title="Vivy dupe",
                                  state="duplicate", resolution="duplicate",
                                  resolved_by="operator@example.com", resolved_at=models_utcnow(),
                                  refs={"duplicate_of": "issue_bosun_orig_x"}))
    vivy = frozenset({"vivy"})
    both = frozenset({"vivy", "bosun"})
    assert "issue_bosun_orig_x" not in issue_build.detail_html("issue_vivy_dupe_x", vivy)
    assert "issue_bosun_orig_x" not in issue_build.live_html_corrections(None, vivy)
    assert 'href="/engineering/issues/issue_bosun_orig_x"' in issue_build.detail_html(
        "issue_vivy_dupe_x", both)
    assert "issue_bosun_orig_x" in issue_build.live_html_corrections(None, both)


def test_a_prerequisite_in_a_project_outside_scope_names_no_id_or_title(db):
    """Review round 2 on PR #372: a prerequisite can name an item in another
    product (platform/decisions/0074's example is a Deck item waiting on a
    Bosun item). A viewer scoped to vivy sees the vivy item as blocked by
    something they cannot see, with neither the bosun item's id nor its title.
    A viewer whose scope covers bosun sees the title and the link."""
    library_fake.seed_issue(**_mk(id="issue_bosun_pre_x", product="bosun",
                                  title="BOSUN-PREREQ-TITLE"))
    library_fake.seed_issue(**_mk(
        id="issue_vivy_waits_x", product="vivy", title="Vivy waits", state="open",
        prerequisites=[{"item": "issue_bosun_pre_x", "state": "open",
                        "title": "BOSUN-PREREQ-TITLE"}]))
    scoped = issue_build.detail_html("issue_vivy_waits_x", frozenset({"vivy"}))
    assert "issue_bosun_pre_x" not in scoped
    assert "BOSUN-PREREQ-TITLE" not in scoped
    assert "a project you cannot see" in scoped
    both = issue_build.detail_html("issue_vivy_waits_x", frozenset({"vivy", "bosun"}))
    assert "BOSUN-PREREQ-TITLE" in both
    assert "/engineering/issues/issue_bosun_pre_x" in both
