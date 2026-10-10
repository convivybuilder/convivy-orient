"""Render tests for archives_build.py — /archives/prs and /archives/changelogs.

Load-bearing: PR comments group by (product, PR number) with the merge-time
description attached, a comment with no resolvable PR reference is not
dropped, both pages are product-filterable, and a kind with no dedicated
section (e.g. `alert`) stays reachable through `library_client.list_signals`'s
own `kind` filter — the guarantee that makes "the rest is searchable" true.
"""
from __future__ import annotations

import pytest

import archives_build as a
import control_db
import library_client
import library_fake


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    library_fake.patch(monkeypatch)
    yield


def _comment(**kw):
    base = dict(id=control_db.new_signal_id(), source="github_review", kind="pr_comment",
                actor="alice", ingested_by="agent", product="vivy",
                body="a comment", refs={"pr": 42, "url": "https://github.com/convivy/vivy/pull/42#c1"},
                privacy_class="internal", produced_at=None)
    base.update(kw)
    return base


def _description(**kw):
    base = dict(id=control_db.new_signal_id(), source="github", kind="changelog",
                actor="bob", ingested_by="agent", product="vivy",
                body="Add feature X\n\nDescription of X.",
                refs={"pr": 42, "url": "https://github.com/convivy/vivy/pull/42",
                      "merged_at": "2026-08-01T00:00:00Z"},
                privacy_class="internal", produced_at=None)
    base.update(kw)
    return base


def _seed(*sigs):
    for sig in sigs:
        library_fake.seed_signal(**sig)


# --- PRs: grouping -------------------------------------------------------


def test_multiple_comments_on_one_pr_render_as_one_group(db):
    _seed(_comment(actor="alice", body="FIRST-COMMENT"),
          _comment(actor="bob", body="SECOND-COMMENT"))
    html = a.prs_live_html()
    assert html.count('class="pr-group"') == 1
    assert "FIRST-COMMENT" in html and "SECOND-COMMENT" in html
    assert "PR #42" in html


def test_different_prs_render_as_separate_groups(db):
    _seed(_comment(refs={"pr": 42, "url": "https://github.com/convivy/vivy/pull/42#c1"}),
          _comment(refs={"pr": 43, "url": "https://github.com/convivy/vivy/pull/43#c1"}))
    html = a.prs_live_html()
    assert html.count("pr-group") >= 2
    assert "PR #42" in html and "PR #43" in html


def test_description_attaches_to_its_pr_group(db):
    _seed(_comment(body="A-COMMENT"),
          _description(body="THE-DESCRIPTION-TEXT"))
    html = a.prs_live_html()
    assert "THE-DESCRIPTION-TEXT" in html
    assert "A-COMMENT" in html
    # one group, not two — the description joined the comment's group rather
    # than rendering as its own unrelated card.
    assert html.count('class="pr-group"') == 1


def test_pr_with_no_comments_yet_still_shows_its_description(db):
    """An unmerged PR with comments but no description is the common case;
    the reverse — a merge record with no captured comments — must still
    render its own group rather than being dropped for lack of comments."""
    _seed(_description(body="DESCRIPTION-ONLY-NO-COMMENTS"))
    html = a.prs_live_html()
    assert "DESCRIPTION-ONLY-NO-COMMENTS" in html
    assert "No captured comments" in html


def test_unmerged_pr_shows_comments_with_no_description_block(db):
    _seed(_comment(body="OPEN-PR-COMMENT"))
    html = a.prs_live_html()
    assert "OPEN-PR-COMMENT" in html
    assert 'class="pr-desc"' not in html


# --- PRs: the row shape that makes grouping possible at all ---------------


def test_comment_with_no_pr_reference_is_not_dropped(db):
    """A `pr_comment` row whose `refs` carries no usable PR number must still
    appear on the page, in its own clearly-labeled section — silently
    dropping it would be worse than the flat feed this page replaces."""
    _seed(_comment(body="GROUPED-COMMENT"),
          _comment(id=control_db.new_signal_id(), body="UNLINKED-COMMENT", refs={}))
    html = a.prs_live_html()
    assert "GROUPED-COMMENT" in html
    assert "UNLINKED-COMMENT" in html
    assert "no resolvable PR reference" in html


def test_comment_with_non_integer_pr_ref_is_unlinked_not_dropped(db):
    """A malformed `refs.pr` (string, bool, missing) must fail closed into the
    unlinked bucket, not crash the render or silently vanish."""
    _seed(_comment(id=control_db.new_signal_id(), body="STRING-PR-COMMENT",
                   refs={"pr": "not-a-number", "url": "https://x/y"}),
          _comment(id=control_db.new_signal_id(), body="BOOL-PR-COMMENT",
                   refs={"pr": True, "url": "https://x/y"}))
    html = a.prs_live_html()  # must not raise
    assert "STRING-PR-COMMENT" in html
    assert "BOOL-PR-COMMENT" in html
    # one shared "unlinked" section (the heading), not a section per row —
    # the footnote also mentions the phrase once, hence 2 total, not 1.
    assert html.count("no resolvable PR reference") == 2
    assert html.count('class="sec"') == 1


def test_unlinked_comments_sit_inside_the_filterable_groups_container(db):
    """The product-chip JS queries `#groups .pr-group` — an unlinked comment
    rendered outside that container would stay visible regardless of which
    product chip is selected, silently exempting it from "every section
    filters by product". `#empty` is the last element inside `#groups` in the
    template, so a row appearing before it is inside the container; pin the
    nesting, not just the row's presence on the page."""
    _seed(_comment(id=control_db.new_signal_id(), body="UNLINKED-ROW", refs={}))
    html = a.prs_live_html()
    groups_start = html.index('id="groups"')
    groups_inner_end = html.index('id="empty"')
    assert groups_start < html.index("UNLINKED-ROW") < groups_inner_end


# --- product filter --------------------------------------------------------


def test_pr_groups_carry_their_product_for_client_side_filtering(db):
    _seed(_comment(product="vivy", refs={"pr": 1, "url": "https://x/1"}),
          _comment(product="constance", refs={"pr": 2, "url": "https://x/2"}))
    html = a.prs_live_html()
    assert 'data-prod="vivy"' in html
    assert 'data-prod="constance"' in html


def test_pr_chip_counts_match_visible_groups_not_a_wider_total(db):
    """The chip's own count must match what selecting it shows — the
    decisions_build/signals_build rule: derive chip counts from the same set
    being rendered, not a separate broader query."""
    _seed(_comment(product="vivy", refs={"pr": 1, "url": "https://x/1"}),
          _comment(product="vivy", refs={"pr": 2, "url": "https://x/2"}),
          _comment(product="constance", refs={"pr": 3, "url": "https://x/3"}))
    html = a.prs_live_html()
    assert '<span class="n">2</span></button>' in html  # vivy: 2 PR groups
    assert 'data-prod="vivy"' in html and 'data-prod="constance"' in html


def test_changelogs_chip_filterable_by_product(db):
    _seed(_description(product="vivy", body="VIVY-CHANGELOG-ENTRY"),
          _description(id=control_db.new_signal_id(), product="constance",
                       body="CONSTANCE-CHANGELOG-ENTRY"))
    html = a.changelogs_live_html()
    assert 'data-prod="vivy"' in html and 'data-prod="constance"' in html
    assert "VIVY-CHANGELOG-ENTRY" in html and "CONSTANCE-CHANGELOG-ENTRY" in html


def test_changelogs_only_changelog_kind_renders(db):
    _seed(_description(body="KEEP-THIS-CHANGELOG"),
          dict(id=control_db.new_signal_id(), source="slack", kind="notification",
              actor="platform", ingested_by="agent", product="vivy",
              body="NOT-A-CHANGELOG", privacy_class="internal"))
    html = a.changelogs_live_html()
    assert "KEEP-THIS-CHANGELOG" in html
    assert "NOT-A-CHANGELOG" not in html


# --- fan-out fairness across products --------------------------------------


def test_a_busy_product_does_not_crowd_out_a_quiet_ones_pr(db, monkeypatch):
    """The whole reason `_fan_out` asks the Library per-product rather than
    for one flat top-N: a busy product's rows must not push a quiet product's
    single PR out of a shared cap. `quiet` is seeded FIRST (oldest by the
    fake's own default sort, ingested_at) and `busy` gets 5 rows after it,
    with the cap forced to 2 — the exact shape that would crowd `quiet` out
    entirely under a single flat `list_signals(kind=..., cap=2)` call.
    Verified against that flat-fetch simulation before trusting this test:
    it fails (quiet vanishes) under the old approach and passes under
    `_fan_out`'s per-product fetch."""
    monkeypatch.setattr(a, "_PR_CAP_PER_PRODUCT", 2)
    _seed(_comment(id=control_db.new_signal_id(), product="quiet",
                   refs={"pr": 1, "url": "https://x/quiet"}))
    for n in range(5):
        _seed(_comment(id=control_db.new_signal_id(), product="busy",
                       refs={"pr": n, "url": f"https://x/{n}"}))
    html = a.prs_live_html()
    assert 'data-prod="quiet"' in html
    assert 'data-prod="busy"' in html


# --- kind with no section stays reachable -----------------------------------


def test_alert_kind_has_no_dedicated_section_but_stays_filterable(db):
    """`alert` and `finding` got no Archives section (110/49 rows vs.
    thousands for PRs/Changelogs) — this pins that the tail is still reachable
    through `list_signals`'s own `kind` filter, which is the guarantee that
    makes "the rest is searchable" true rather than aspirational."""
    _seed(dict(id=control_db.new_signal_id(), source="ci", kind="alert",
              actor="ci-bot", ingested_by="ci-bot", product="vivy",
              body="ALERT-BODY", privacy_class="internal"),
          _comment(body="A-PR-COMMENT"))
    rows = library_client.list_signals(kind=["alert"])
    assert len(rows) == 1
    assert rows[0].body == "ALERT-BODY"
    # archives_build's own pages never render this kind — confirms the
    # absence of a section, not merely the presence of the filter above.
    assert "ALERT-BODY" not in a.prs_live_html()
    assert "ALERT-BODY" not in a.changelogs_live_html()


# --- XSS -------------------------------------------------------------------


def test_pr_comment_body_is_xss_safe(db):
    _seed(_comment(body="<script>alert(1)</script> and <img src=x onerror=alert(2)>"))
    html = a.prs_live_html()
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x" not in html


def test_changelog_body_is_xss_safe(db):
    _seed(_description(body="<script>alert(1)</script>"))
    html = a.changelogs_live_html()
    assert "<script>alert(1)</script>" not in html


# --- the agentctl-review stamp: closed here too (convivy-orient#254 round 2) -
#
# `signals_build`'s detail page stripped a leading `agentctl-review` stamp
# from a `pr_comment` body; these three sites read the identical `pr_comment`/
# `changelog` bodies through the same `library_client` and rendered them
# through the same safe Markdown path with no strip of their own, so a
# stamp-led body showed the raw `<!-- agentctl-review ... -->` block here,
# live, before this fix. All three close via `library_client.SignalRecord`
# stripping the stamp once for every reader (`library_client.py`'s
# `_signal_record`), so none of these builders calls a stripper directly.

_REVIEW_STAMP = (
    "<!-- agentctl-review\n"
    "v: 1\n"
    "pr: 42\n"
    "repo: vivy\n"
    "reviewed-commit: 7842af3bf82cb74fa1522af\n"
    "verdict: APPROVE\n"
    "agent: reviewer\n"
    "run-id: run_9f8e7d\n"
    "-->\n"
)


def test_pr_comment_review_stamp_is_stripped(db):
    # _comment_html (archives_build.py), feeding /signals/prs.
    _seed(_comment(body=_REVIEW_STAMP + "LGTM, one small nit inline."))
    html = a.prs_live_html()
    assert "LGTM, one small nit inline." in html
    assert "agentctl-review" not in html
    assert "reviewed-commit" not in html


def test_pr_description_review_stamp_is_stripped(db):
    # _pr_group_html's PR-description render (archives_build.py), same page.
    _seed(_comment(body="a comment"),
          _description(body=_REVIEW_STAMP + "Ships the retry logic."))
    html = a.prs_live_html()
    assert "Ships the retry logic." in html
    assert "agentctl-review" not in html


def test_changelog_card_review_stamp_is_stripped(db):
    # _changelog_card (archives_build.py), feeding /signals/changelogs.
    _seed(_description(body=_REVIEW_STAMP + "Add feature X."))
    html = a.changelogs_live_html()
    assert "Add feature X." in html
    assert "agentctl-review" not in html


# --- routing + nav -----------------------------------------------------------


def test_routes_registered_full_tier():
    import server
    routes = server._full_only_routes()
    assert routes["/archives/prs"][1] is a.prs_live_html
    assert routes["/archives/changelogs"][1] is a.changelogs_live_html


def test_nav_places_prs_and_changelogs_in_archives():
    import design_system as ds
    html = ds.nav_component("/archives/prs", "full")
    assert 'href="/archives/prs" aria-current="page"' in html
    assert 'aria-label="Archives navigation"' in html
    assert 'href="/archives/changelogs"' in html
    assert 'href="/archives"' in html
    assert 'href="/reference/briefings"' in html


# --- empty states ------------------------------------------------------------


def test_prs_empty_state(db):
    assert "No PRs captured yet" in a.prs_live_html()


def test_changelogs_empty_state(db):
    assert "Nothing captured yet" in a.changelogs_live_html()
