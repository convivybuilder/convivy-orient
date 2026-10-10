"""The Field Notes store: the corpus lives in Orient, with its history
(`issue_01M0N650ZJHPQYPF54HD22RJEH`).

These pin the properties that make "Orient is the store of truth" true rather
than asserted — history survives an edit, an imported post's dates are its own,
and nothing is attributed to a human the server never verified.
"""
from __future__ import annotations

import datetime as dt

import pytest

import blog_store
import control_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh control.db on a temp path, schema at head via the same init_db()
    the app runs at startup — matching `test_control_db`'s fixture."""
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    yield


JULY = dt.datetime(2026, 7, 7, 22, 43, 48)
AUGUST = dt.datetime(2026, 8, 1, 9, 0, 0)


def _ingest(**kw):
    base = dict(slug="a-post", title="A Post", body="hello",
                author="The operator", authorship="human", published_at=JULY)
    base.update(kw)
    return blog_store.ingest(**base)


def test_an_ingested_post_is_published_and_readable(db):
    _ingest()
    posts = blog_store.published()
    assert [p.slug for p in posts] == ["a-post"]
    assert posts[0].body == "hello"


def test_every_write_appends_a_revision_so_an_edit_never_erases_what_was_public(db):
    """**The reason this table exists.** Git held the history of what a post said
    until the corpus moved out of `convivy-lab`, and it stops holding it when
    that repo is deleted. A published post is a public artifact: an edit changes
    what people read, and without this nothing records what it replaced.

    Each revision records what the post held BEFORE that write, matching
    `save`'s convention. The first ingest has no prior state to record, so it
    snapshots its own (only) content; every ingest after that records what it's
    about to replace, which is why "first" appears twice below."""
    _ingest(body="first")
    _ingest(body="second")
    _ingest(body="third")

    revs = blog_store.revisions("a-post")
    assert [r.body for r in revs] == ["first", "first", "second"]
    assert blog_store.get("a-post").body == "third"


def test_reingesting_a_corrected_export_appends_a_revision_with_the_pre_correction_body(db):
    """**Regression (`issue_01M1KESDGVFJTBVSD3GRKCCH9X`).** `ingest`'s own
    docstring calls a re-ingest after a corrected export "a normal operation" —
    but the revision was appended after the post's fields were overwritten, so
    it snapshotted the new, corrected body rather than the one it replaced. A
    re-ingest silently lost the pre-correction text the revision exists to
    preserve, the same failure `save` guards against for a live edit."""
    _ingest(body="original, has a typo")
    _ingest(body="corrected, typo fixed")

    revs = blog_store.revisions("a-post")
    assert revs[-1].reason == "ingest"
    assert revs[-1].body == "original, has a typo", (
        "a revision recording the corrected body is the bug wearing a passing test")
    assert blog_store.get("a-post").body == "corrected, typo fixed"


def test_an_imported_post_keeps_its_own_updated_date_across_a_second_ingest(db):
    """**Regression: every imported post was dated to the moment of migration.**

    `BlogPost.updated_at` carries `onupdate=utcnow`. Assigning it the value it
    already holds is not a change SQLAlchemy tracks, so the column stayed out of
    the UPDATE's SET clause and `onupdate` stamped NOW instead — but only on a
    post that took a SECOND write, which is why a single-ingest fixture would
    miss it entirely. It was found by running the real import: exactly the two
    posts with two revisions showed the wrong date, in a field the public site
    displays.
    """
    _ingest(body="first", updated_at=AUGUST)
    _ingest(body="second", updated_at=AUGUST)

    post = blog_store.get("a-post")
    assert post.updated_at == AUGUST, "onupdate overwrote the source's own date"
    assert post.published_at == JULY, "publication date must not move on an edit"


def test_updated_falls_back_to_published_when_the_source_has_no_updated_field(db):
    """Three of the five migrated posts carry no `updated` in frontmatter. The
    honest reading is that they have never been revised, so `published_at`
    stands in — not now, which would claim an edit that never happened."""
    _ingest(updated_at=None)
    assert blog_store.get("a-post").updated_at == JULY


def test_an_ingested_revision_names_no_actor(db):
    """**A post imported from `convivy-lab` predates this store and has no
    attested actor.** Recording an inferred one — the operator is the only person who has
    ever written here — is exactly the fabrication `blog_api`'s attestation rule
    refuses, and the store must not do at import what the API prevents at
    runtime."""
    _ingest()
    rev = blog_store.revisions("a-post")[0]
    assert rev.reason == "ingest"
    assert rev.actor is None


def test_the_revision_carries_the_authorship_it_was_written_under(db):
    """The label is a claim about a post AS READ. A post drafted with assistance
    and later hand-rewritten is two honest labels, not one retroactive one, so
    authorship is stored per revision rather than inherited from current state.

    Each revision holds the authorship that preceded it (matching the
    before-mutation snapshot), so the "human" hand-rewrite shows up one call
    later, on the revision recorded for the ingest after it."""
    _ingest(authorship="llm", body="drafted")
    _ingest(authorship="human", body="rewritten")
    _ingest(authorship="human", body="polished")

    assert [r.authorship for r in blog_store.revisions("a-post")] == ["llm", "llm", "human"]
    assert blog_store.get("a-post").authorship == "human"


def test_an_unknown_authorship_value_is_refused(db):
    """Fail closed on a value this code was not written for, rather than storing
    a label the site would render as though it meant something."""
    with pytest.raises(ValueError):
        _ingest(authorship="definitely-a-person")


def test_published_orders_by_publication_not_revision(db):
    """A blog is ordered by when a piece went out. Fixing a typo in an old post
    must not move it to the top of the site."""
    _ingest(slug="old", published_at=JULY, updated_at=AUGUST)
    _ingest(slug="new", published_at=dt.datetime(2026, 7, 28), updated_at=None)

    assert [p.slug for p in blog_store.published()] == ["new", "old"]


@pytest.mark.parametrize("bad", ["../escape", "/etc/passwd", "has space",
                                  "Upper", "-leading", "trailing-", "", "a" * 65])
def test_the_store_refuses_a_slug_that_is_not_url_and_path_safe(db, bad):
    """**Found by `security-reviewer` on PR #196.** The slug rule lived only in
    `blog_api`, so the route validated and the store accepted anything, while the
    column is an unconstrained `String(64)`.

    Nothing could reach the store with a bad slug — the API checks and the
    one-time import derives slugs from `Path.stem` — but a consumer joining a
    stored slug into a filesystem path would write outside its output directory
    if one ever did: `Path("/a") / "/etc/x"` is `Path("/etc/x")`, prefix silently
    discarded, which turns a site generator into an arbitrary file write.

    A store that cannot hold a malformed slug removes that for every consumer
    rather than asking each to re-derive the guard."""
    with pytest.raises(ValueError, match="slug"):
        _ingest(slug=bad)


def test_publish_refuses_a_bad_slug_too(db):
    """Both write paths, not just the import one."""
    with pytest.raises(ValueError, match="slug"):
        blog_store.publish(slug="../escape", title="T", body="b", author="The operator",
                           authorship="human", actor="operator@example.com")


def test_saving_a_published_post_appends_a_revision_with_the_pre_edit_body(db):
    """**The bug this module's docstring warns about.** `save` writes straight
    into the published row, so `corpus()` serves the new body the instant this
    call returns. If no revision records what the row held a moment ago, the
    old text is unrecoverable and nothing shows it was ever there."""
    blog_store.publish(slug="a-post", title="T", body="original", author="The operator",
                       authorship="human", actor="operator@example.com")

    blog_store.save(slug="a-post", title=None, body="edited", actor="operator@example.com")

    revs = blog_store.revisions("a-post")
    assert revs[-1].reason == "save"
    assert revs[-1].body == "original", (
        "a revision recording the new body is the bug wearing a passing test")
    assert revs[-1].actor == "operator@example.com"
    assert blog_store.get("a-post").body == "edited"


def test_saving_a_draft_also_appends_a_revision(db):
    """**Every save appends, drafts included — the simpler rule, matching the
    module docstring's "every write appends a revision" literally.** A draft's
    prior text was never public, so this revision is cheap insurance rather than
    the point, but a status check here would make `save`'s behaviour depend on a
    field the function is deliberately not told (`status` is absent from its
    signature), and history that only starts once a post goes live is history
    with a gap at the beginning."""
    blog_store.create(slug="a-draft", title="T", body="first draft",
                      author="The operator", authorship="human")

    blog_store.save(slug="a-draft", title=None, body="second draft")

    revs = blog_store.revisions("a-draft")
    assert revs[-1].reason == "save"
    assert revs[-1].body == "first draft"
    assert revs[-1].actor is None
    assert blog_store.get("a-draft").body == "second draft"


def test_save_on_a_missing_slug_returns_none_and_appends_nothing(db):
    assert blog_store.save(slug="no-such-post", title=None, body="x") is None
    assert blog_store.revisions("no-such-post") == []


# --- corpus() must never emit an email address as author --------------------
# `author` stores the attested identity, a verified email for a human post
# (`blog_api.py`'s `author=ident.actor`). `corpus()` is what both the public
# the public Swabby's Journal page and the `corpus.json` its repository holds
# serve that field to, so an email must not ride along to either
# (`security-reviewer`, PR #289).


def test_corpus_turns_an_email_author_into_a_capitalised_local_part(db):
    _ingest(slug="a-post", author="operator@example.com")
    [post] = blog_store.corpus()["posts"]
    assert post["author"] == "Operator"


def test_corpus_leaves_a_non_email_author_unchanged(db):
    _ingest(slug="a-post", author="The operator")
    [post] = blog_store.corpus()["posts"]
    assert post["author"] == "The operator"


def test_corpus_leaves_an_empty_author_unchanged(db):
    _ingest(slug="a-post", author="")
    [post] = blog_store.corpus()["posts"]
    assert post["author"] == ""


def test_corpus_never_emits_an_author_containing_an_at_sign(db):
    """The stored row keeps the real email — only what `corpus()` emits is
    scrubbed. Planted the pass-through (`"author": p.author` with no
    `_display_author` call) and confirmed this fails against
    `operator@example.com`."""
    _ingest(slug="a-post", author="operator@example.com")
    _ingest(slug="b-post", author="")
    _ingest(slug="c-post", author="The operator")

    assert blog_store.get("a-post").author == "operator@example.com", (
        "the stored row is the attested identity and must not be scrubbed")
    assert all("@" not in p["author"] for p in blog_store.corpus()["posts"])


# --- corpus() is the one function a public rendering surface may trust ------
# These properties used to be pinned by `GET /api/blog/corpus`'s own tests in
# `test_blog_api.py`; that route is retired now that the site no longer pulls
# from Orient, but `journal_push.push_corpus` still builds its payload from this
# function, so what it may and may not emit still matters.


def test_corpus_never_includes_a_draft(db):
    """**The refusal that matters most.** `corpus()` feeds a public renderer
    (via `journal_push.push_corpus`). A draft has never been public, and
    serving one through the one function that feeds publication would have
    published it — the human publish gate in the Writing Desk bypassed without
    anyone acting."""
    _ingest(slug="published-one")
    blog_store.create(slug="a-draft", title="A Draft", body="not yet",
                      author="The operator", authorship="human")

    slugs = [p["slug"] for p in blog_store.corpus()["posts"]]
    assert slugs == ["published-one"]


def test_corpus_carries_no_revision_history(db):
    """Revisions exist so Orient can say what a post USED to say. A public
    rendering surface renders what it says now, and shipping the history would
    put superseded text — including anything an edit was made to remove — on a
    public site."""
    _ingest(body="first")
    blog_store.save(slug="a-post", title=None, body="second")

    [post] = blog_store.corpus()["posts"]
    assert post["body"] == "second"
    assert "revisions" not in post and "history" not in post


def test_corpus_carries_the_attested_authorship(db):
    """The operator wants this label rendered on every piece of prose the public site
    serves. Its worth is that `human` is server-attested and cannot be
    claimed, so it has to reach the renderer rather than being dropped as
    internal."""
    _ingest(authorship="collab")
    [post] = blog_store.corpus()["posts"]
    assert post["authorship"] == "collab"
