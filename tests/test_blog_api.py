"""Blog authoring write-API tests (ARCH-363).

The feature's whole justification is integrity: the lab's public promise that a
reader can tell who wrote a page currently rests on the writer hand-typing
`author:` honestly, and `lab-blog publish` even DEFAULTS `authorship` to "human".
So these tests are weighted toward the attestation and the gate rather than the
CRUD — a draft editor that works but lets a caller name someone else as author
would be worse than no feature, because it launders a self-assertion through a
surface that looks authoritative.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import access_identity
import api_common
import authz
import blog_api
import blog_store
import control_db
import library_client
import server as srv

AGENT_TOKEN = "agent-secret-xyz"


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
        "obs@example.com": {"role": "observer", "products": ["*"]},
        "con@example.com": {"role": "contractor", "products": ["vivy"]},
        "ctb@example.com": {"role": "contributor", "products": ["vivy"]},
    }))
    monkeypatch.setattr(authz, "ROLES", roles)
    # The project scope now comes from the Library (E19·1,
    # issue_01M35K54QHJYYPXWS28YWEHJP3), never from roles.json's `products` —
    # stubbed by re-reading the same roles.json file on every call, the way
    # the pre-E19·1 `human_identity` re-read it fresh, rather than a dict
    # snapshot that would go stale against a test that rewrites the file.
    monkeypatch.setattr(
        library_client, "person_scope",
        lambda email: frozenset(
            json.loads(authz.ROLES.read_text()).get(email, {}).get("products", [])))

    toks = tmp_path / "api-tokens.json"
    toks.write_text(json.dumps({AGENT_TOKEN: {"name": "an-agent", "products": ["vivy"]}}))
    monkeypatch.setattr(api_common, "API_TOKENS", toks)

    jwtc = tmp_path / "jwt.json"
    jwtc.write_text(json.dumps({"enforce": False}))
    monkeypatch.setattr(access_identity, "JWT_CONFIG", jwtc)

    # PUBLISH NO LONGER SHELLS OUT, and it now writes the corpus to
    # convivy/swabbys-journal. Stubbed for the whole suite: a test that published
    # must never reach the network, and the default here is the credential-absent
    # answer so a test asserting on the publish response sees the shape a host
    # with no journal push token returns.
    import journal_push
    monkeypatch.setattr(
        journal_push, "push_corpus",
        lambda: {"pushed": False, "corpus_sha": None, "reason": "stubbed"})

    with TestClient(srv.app, follow_redirects=False) as c:
        yield c


def _human(email, origin="http://testserver", **extra):
    h = {srv.EMAIL_HEADER: email, "Origin": origin}
    h.update(extra)
    return h


# --- attestation: the reason the feature exists ------------------------------


def test_the_author_is_the_verified_identity_not_the_request_body(client):
    """The defect this replaces is a self-asserted author. A body field naming
    someone else must be IGNORED, not merely overridden by a default — otherwise
    the surface launders a claim it was built to stop."""
    r = client.post("/api/blog/drafts",
                    json={"slug": "a-post", "title": "T", "author": "someone@else.example",
                          "body": "words"},
                    headers=_human("mem@example.com"))
    assert r.status_code == 201
    assert r.json()["author"] == "mem@example.com"
    stored = blog_store.get("a-post")
    assert stored.author == "mem@example.com"
    assert "someone@else.example" not in (stored.author + stored.title + stored.body)


def test_authorship_human_cannot_be_claimed_only_observed(client):
    """A writer may declare MORE assistance than the server saw; declaring LESS
    is the lie that matters. `human` is what the server attests from observing a
    verified browser session, so it is never taken from the caller."""
    r = client.post("/api/blog/drafts",
                    json={"slug": "assisted", "authorship": "llm", "body": "x"},
                    headers=_human("mem@example.com"))
    assert r.json()["authorship"] == "llm"

    # An unrecognised or absent declaration falls back to what was observed.
    r2 = client.post("/api/blog/drafts",
                     json={"slug": "plain", "authorship": "definitely-a-human", "body": "x"},
                     headers=_human("mem@example.com"))
    assert r2.json()["authorship"] == "human"


def test_a_later_save_cannot_rewrite_the_attested_author(client):
    """Otherwise the attestation holds only until the second request."""
    client.post("/api/blog/drafts", json={"slug": "p", "title": "T", "body": "one"},
                headers=_human("mem@example.com"))
    r = client.put("/api/blog/drafts/p",
                   json={"body": "two", "author": "someone@else.example",
                         "authorship": "human"},
                   headers=_human("operator@example.com"))
    assert r.status_code == 200
    stored = blog_store.get("p")
    assert stored.author == "mem@example.com", "the original author was overwritten"
    assert stored.authorship == "human"
    assert "someone@else.example" not in (stored.author + stored.body)


def test_a_title_cannot_forge_the_author(client):
    """**The attack this replaces is gone by construction, and the property is
    not.** A draft used to be a markdown file with a YAML header, and the first
    cut built it with `title: "{title}"` — so a title carrying a quote and a
    newline injected arbitrary frontmatter including `author:`, landing FIRST, so
    which key a parser honoured came down to its duplicate handling. Every other
    test passed with that hole open, because they attacked `author` directly and
    this came through a field treated as cosmetic (security review, #114).

    There is no header to inject now: title and author are separate columns and
    nothing parses either. This asserts the property the injection defeated,
    rather than the shape of the defeated attack."""
    evil = 'Innocent"\nauthor: someone@else.example\nauthorship: human\nx: "'
    r = client.post("/api/blog/drafts",
                    json={"slug": "inject", "title": evil, "body": "words"},
                    headers=_human("mem@example.com"))
    assert r.status_code == 201

    stored = blog_store.get("inject")
    assert stored.author == "mem@example.com"
    assert stored.authorship == "human"
    assert stored.title == evil[:blog_api.MAX_TITLE], "the title was not stored verbatim"

def test_a_title_cannot_forge_the_author_on_save_either(client):
    """The same property on the other write path. `blog_store.save` takes no
    author parameter at all, so this is now structural rather than defended."""
    client.post("/api/blog/drafts", json={"slug": "s", "title": "T", "body": "one"},
                headers=_human("mem@example.com"))
    client.put("/api/blog/drafts/s",
               json={"title": 'X"\nauthor: someone@else.example\ny: "', "body": "two"},
               headers=_human("mem@example.com"))

    stored = blog_store.get("s")
    assert stored.author == "mem@example.com"
    assert stored.body == "two"

def test_a_body_round_trips_exactly_including_a_horizontal_rule(client):
    """`---` on its own line is a markdown horizontal rule, which prose plausibly
    contains. It used to truncate the body, because the file format partitioned
    on that delimiter. Nothing parses a body now, so this asserts the general
    property — a body is stored exactly as written — with the rule as the case
    that historically broke it."""
    client.post("/api/blog/drafts", json={"slug": "hr", "title": "T", "body": "before"},
                headers=_human("mem@example.com"))
    prose = "Opening paragraph.\n\n---\n\nAfter the rule, which must survive."
    client.put("/api/blog/drafts/hr", json={"body": prose},
               headers=_human("mem@example.com"))

    assert blog_store.get("hr").body == prose
# --- the gate ----------------------------------------------------------------


@pytest.mark.parametrize("email", ["operator@example.com", "mem@example.com"])

def test_admin_and_member_may_author(client, email):
    r = client.post("/api/blog/drafts", json={"slug": f"by-{email[:3]}", "body": "x"},
                    headers=_human(email))
    assert r.status_code == 201


@pytest.mark.parametrize("email", ["obs@example.com", "con@example.com", "ctb@example.com"])

def test_read_tier_roles_may_not_author(client, email):
    """Deny-by-default. Reading the site is not permission to publish from it."""
    r = client.post("/api/blog/drafts", json={"slug": "nope", "body": "x"},
                    headers=_human(email))
    assert r.status_code == 403
    assert blog_store.get("nope") is None

def test_an_agent_token_may_not_author(client):
    """The surface publishes to a PUBLIC-facing site, so no automated caller
    reaches it — `blog_write` is absent from the agent and system roles, and the
    human-only check is a second lock on the same door."""
    r = client.post("/api/blog/drafts", json={"slug": "by-agent", "body": "x"},
                    headers={"Authorization": f"Bearer {AGENT_TOKEN}"})
    assert r.status_code in (401, 403)
    assert blog_store.get("by-agent") is None


def test_an_unauthenticated_caller_gets_401(client):
    assert client.post("/api/blog/drafts", json={"slug": "x", "body": "y"}).status_code == 401


def test_a_cross_origin_post_is_refused(client):
    r = client.post("/api/blog/drafts", json={"slug": "csrf", "body": "x"},
                    headers=_human("mem@example.com", origin="http://evil.example"))
    assert r.status_code == 403
    assert blog_store.get("csrf") is None


# --- listing ------------------------------------------------------------------


def test_list_returns_drafts_with_their_attested_authors(client):
    """The route had no test at all in the first cut — every other one was
    covered, so a regression here would have been invisible."""
    client.post("/api/blog/drafts", json={"slug": "one", "title": "First", "body": "x"},
                headers=_human("mem@example.com"))
    client.post("/api/blog/drafts", json={"slug": "two", "title": "Second", "body": "y"},
                headers=_human("operator@example.com"))

    r = client.get("/api/blog/drafts", headers=_human("mem@example.com"))
    assert r.status_code == 200
    by_slug = {d["slug"]: d for d in r.json()["drafts"]}
    assert by_slug["one"]["title"] == "First"
    assert by_slug["one"]["author"] == "mem@example.com"
    assert by_slug["two"]["author"] == "operator@example.com"


def test_list_is_gated_but_does_not_require_a_same_origin_header(client):
    """CSRF defends state changes. Applying it to a GET protects nothing and
    refuses legitimate cross-origin reads — every other read route here omits
    it, and the deviation was a cost with no benefit."""
    client.post("/api/blog/drafts", json={"slug": "one", "body": "x"},
                headers=_human("mem@example.com"))
    r = client.get("/api/blog/drafts", headers={srv.EMAIL_HEADER: "mem@example.com"})
    assert r.status_code == 200

    # Still gated on the capability, which is what actually protects it.
    assert client.get("/api/blog/drafts",
                      headers={srv.EMAIL_HEADER: "obs@example.com"}).status_code == 403


def test_list_is_empty_rather_than_erroring_before_any_draft_exists(client):
    r = client.get("/api/blog/drafts", headers=_human("mem@example.com"))
    assert r.status_code == 200 and r.json()["drafts"] == []


# --- listing published posts, now from Orient's own store ---------------------
#
# These read `blog_store` rather than `convivy-lab`'s git HEAD
# (issue_01M0N650ZJHPQYPF54HD22RJEH). The properties are the ones the git-backed
# version held; only the source changed. The two that did NOT survive are the
# ones about tracking a commit, because there is no commit any more.


def _publish(client, slug, *, title, author="The operator", authorship="human"):
    """Publish through the real route, so these exercise the path the Writing
    Desk actually takes rather than seeding the store behind it."""
    client.post("/api/blog/drafts",
                json={"slug": slug, "title": title, "body": f"body of {slug}"},
                headers=_human("mem@example.com"))
    r = client.post(f"/api/blog/drafts/{slug}/publish", headers=_human("mem@example.com"))
    assert r.status_code == 200, r.text
    return r


def test_list_posts_returns_published_posts_newest_first(client):
    _publish(client, "older", title="Older")
    _publish(client, "newer", title="Newer")
    r = client.get("/api/blog/posts", headers=_human("mem@example.com"))
    assert r.status_code == 200
    assert [p["slug"] for p in r.json()["posts"]] == ["newer", "older"]


@pytest.mark.parametrize("email", ["operator@example.com", "mem@example.com",
                                    "obs@example.com", "con@example.com",
                                    "ctb@example.com"])

def test_list_posts_is_readable_by_every_role_unlike_drafts(client, email):
    """Deliberately NOT gated on `blog.write`: this content is public, so a
    read-tier viewer who is refused the drafts list must still see what is
    published. Unchanged by the move to the store."""
    _publish(client, "p", title="P")
    r = client.get("/api/blog/posts", headers=_human(email))
    assert r.status_code == 200
    assert [p["slug"] for p in r.json()["posts"]] == ["p"]


def test_list_posts_never_shows_a_draft(client):
    """The two lists must not cross — a draft is not published, and this route
    exists because the drafts list was once shown in its place. The git version
    got this free by reading a different directory; the store holds both in one
    table, so it is now a real filter rather than a consequence of layout."""
    client.post("/api/blog/drafts",
                json={"slug": "unpublished", "title": "Unpublished", "body": "x"},
                headers=_human("mem@example.com"))
    _publish(client, "published", title="Published")

    slugs = [p["slug"] for p in
             client.get("/api/blog/posts", headers=_human("mem@example.com")).json()["posts"]]
    assert slugs == ["published"]


def test_list_posts_requires_authentication(client):
    """Restored: dropped in the rewrite to the store, found by both reviewers on
    PR #195. The runtime check never changed — what went was the coverage that
    would catch a future edit removing it."""
    assert client.get("/api/blog/posts").status_code == 401


def test_an_agent_token_may_not_list_published_posts(client):
    """**The load-bearing one, and the reason dropping it mattered.** A prior
    security review (PR #188) found an earlier draft of this exact route
    checking only `resolve_identity(request) is None`, which admits a
    bearer-token caller. The exposure was small — this content is public — but
    the fix matched the file's stated posture: a future role change should have
    to defeat two checks, not one. Deleting this test removed the guard against
    silently reintroducing a bug that has already happened here once."""
    _publish(client, "p", title="P")
    r = client.get("/api/blog/posts", headers={"Authorization": f"Bearer {AGENT_TOKEN}"})
    assert r.status_code == 403


def test_list_posts_is_empty_rather_than_erroring_with_no_posts(client):
    """An empty store is a legitimate state, not a failure — a Writing Desk that
    500s before anything is published is worse than one showing nothing."""
    r = client.get("/api/blog/posts", headers=_human("mem@example.com"))
    assert r.status_code == 200
    assert r.json()["posts"] == []


def test_list_posts_shows_a_post_the_moment_it_is_published(client):
    """The other half of that property: this route is not merely never-wrong by
    being always-empty. A draft is absent, and appears the instant publish
    returns."""
    client.post("/api/blog/drafts",
                json={"slug": "pending", "title": "Pending", "body": "x"},
                headers=_human("mem@example.com"))
    assert client.get("/api/blog/posts",
                      headers=_human("mem@example.com")).json()["posts"] == []

    client.post("/api/blog/drafts/pending/publish", headers=_human("mem@example.com"))
    assert [p["slug"] for p in
            client.get("/api/blog/posts",
                       headers=_human("mem@example.com")).json()["posts"]] == ["pending"]


# --- fetching one draft (the editor's "open an existing draft" path) ----------


def test_get_returns_the_full_draft_for_the_editor(client):
    """The list route deliberately omits the body (security review, #114 — n ×
    200KB for no reason); the editor needs it to populate the textarea for a
    draft opened from a PRIOR session, not just one freshly created in this one."""
    client.post("/api/blog/drafts", json={"slug": "full", "title": "Full Post", "body": "the words"},
                headers=_human("mem@example.com"))
    r = client.get("/api/blog/drafts/full", headers=_human("operator@example.com"))
    assert r.status_code == 200
    j = r.json()
    assert j["slug"] == "full"
    assert j["title"] == "Full Post"
    assert j["author"] == "mem@example.com"
    assert j["body"] == "the words"


def test_get_404s_for_a_missing_slug(client):
    r = client.get("/api/blog/drafts/nope", headers=_human("mem@example.com"))
    assert r.status_code == 404


def test_get_is_gated_like_list_but_does_not_require_a_same_origin_header(client):
    client.post("/api/blog/drafts", json={"slug": "g", "body": "x"},
                headers=_human("mem@example.com"))
    r = client.get("/api/blog/drafts/g", headers={srv.EMAIL_HEADER: "mem@example.com"})
    assert r.status_code == 200
    assert client.get("/api/blog/drafts/g",
                      headers={srv.EMAIL_HEADER: "obs@example.com"}).status_code == 403


def test_get_rejects_a_hostile_slug(client):
    r = client.get("/api/blog/drafts/..%2f..%2fetc%2fpasswd", headers=_human("mem@example.com"))
    assert r.status_code in (400, 404)


def test_get_requires_authentication(client):
    assert client.get("/api/blog/drafts/x").status_code == 401


def test_an_agent_token_cannot_get_a_draft(client):
    r = client.get("/api/blog/drafts/x", headers={"Authorization": f"Bearer {AGENT_TOKEN}"})
    assert r.status_code == 403


# --- the slug is the attack surface ------------------------------------------


@pytest.mark.parametrize("slug", [
    "../../../etc/passwd", "..%2f..%2fx", "a/b", "with space",
    "trailing-", "-leading", "", "x" * 65, ".hidden", "a--b/../c",
])

def test_a_hostile_or_malformed_slug_writes_nothing(client, slug, tmp_path):
    r = client.post("/api/blog/drafts", json={"slug": slug, "body": "x"},
                    headers=_human("mem@example.com"))
    assert r.status_code in (400, 404), f"{slug!r} was accepted"
    # **This assertion used to be vacuous and `reviewer` caught it** (PR #197):
    # it checked that nothing appeared under a temp lab directory the fixture no
    # longer creates, so it passed regardless of what the route did. There is no
    # filesystem to inspect now; the store is where a hostile slug would land.
    assert blog_store.get(slug) is None, "a hostile slug reached the store"


def test_mixed_case_is_normalized_rather_than_refused(client):
    """The caller is typing a title, not a path. Lowercasing is a courtesy the
    guard runs BEFORE, so the strict shape still governs from there."""
    r = client.post("/api/blog/drafts", json={"slug": "Mixed-Case", "body": "x"},
                    headers=_human("mem@example.com"))
    assert r.status_code == 201 and r.json()["slug"] == "mixed-case"
    assert blog_store.get("mixed-case") is not None

def test_a_duplicate_slug_is_refused_rather_than_silently_overwriting(client):
    client.post("/api/blog/drafts", json={"slug": "dupe", "body": "first"},
                headers=_human("mem@example.com"))
    r = client.post("/api/blog/drafts", json={"slug": "dupe", "body": "second"},
                    headers=_human("operator@example.com"))
    assert r.status_code == 409
    assert blog_store.get("dupe").body == "first", "the loser overwrote the winner"


# --- publishing, which now writes the store and pushes the corpus to the journal --
#
# `lab-blog publish` is gone (issue_01M0N650ZJHPQYPF54HD22RJEH). It wrote into
# `convivy-lab` and ran a leak guard; the repo is being deleted and the guard was
# retired with the dashboard pipeline it existed for. Four tests went with it —
# the guarded-tool shell, the tool-absent report, the guard-refusal surface and
# the subprocess timeout — because they asserted a control that was deliberately
# removed rather than one that broke. What replaces them is below.


def test_publish_writes_the_store_and_pushes_the_corpus_to_the_journal(client, monkeypatch):
    """**The defect this closes.** Publish used to write `convivy-lab` while the
    corpus route read `blog_store`, so a newly published post reached neither the
    store nor the site — the posts on the old site were there purely from the
    one-time import, and both halves looked like they worked.

    The publish calls `journal_push.push_corpus` with no arguments and returns
    its result, site build included, as `journal`."""
    import blog_store
    import journal_push
    calls = []

    def record():
        calls.append(True)
        return {"pushed": True, "corpus_sha": "commit2", "reason": None,
                "site_build": {"dispatched": True, "reason": None}}

    monkeypatch.setattr(journal_push, "push_corpus", record)
    client.post("/api/blog/drafts",
                json={"slug": "a-post", "title": "A Post", "body": "words"},
                headers=_human("mem@example.com"))
    r = client.post("/api/blog/drafts/a-post/publish", headers=_human("mem@example.com"))

    assert r.status_code == 200
    assert r.json()["published"] is True
    assert calls == [True]
    assert r.json()["journal"] == {"pushed": True, "corpus_sha": "commit2", "reason": None,
                                   "site_build": {"dispatched": True, "reason": None}}
    assert "rebuild" not in r.json()

    post = blog_store.get("a-post")
    assert post is not None and post.status == blog_store.PUBLISHED
    assert post.body.strip() == "words"


def test_publish_records_the_attested_publisher_on_the_revision(client):
    """A publication with nobody attached is the thing that must not be possible.
    `ingest` carries no actor because an imported post predates the store; this
    path has a verified identity and must record it."""
    import blog_store
    client.post("/api/blog/drafts",
                json={"slug": "a-post", "title": "A Post", "body": "words"},
                headers=_human("mem@example.com"))
    client.post("/api/blog/drafts/a-post/publish", headers=_human("mem@example.com"))

    rev = blog_store.revisions("a-post")[-1]
    assert rev.reason == "publish"
    assert rev.actor == "mem@example.com"


def test_publishing_an_edit_keeps_the_original_publication_date(client):
    """A blog is ordered by when a piece went out. Fixing a typo must not lift a
    two-month-old post to the top of the site."""
    import blog_store
    client.post("/api/blog/drafts",
                json={"slug": "a-post", "title": "A Post", "body": "first"},
                headers=_human("mem@example.com"))
    client.post("/api/blog/drafts/a-post/publish", headers=_human("mem@example.com"))
    first_date = blog_store.get("a-post").published_at

    client.put("/api/blog/drafts/a-post",
               json={"title": "A Post", "body": "second"},
               headers=_human("mem@example.com"))
    client.post("/api/blog/drafts/a-post/publish", headers=_human("mem@example.com"))

    post = blog_store.get("a-post")
    assert post.published_at == first_date
    assert post.body.strip() == "second"
    # publish, then the PUT save (now itself revision-bearing,
    # issue_01M0RAVDCH4DQVXDGXXVQ24J71), then publish again.
    revs = blog_store.revisions("a-post")
    assert len(revs) == 3
    assert revs[1].reason == "save" and revs[1].body.strip() == "first"
    assert revs[1].actor == "mem@example.com"


def test_a_failed_journal_push_does_not_fail_the_publish(client, monkeypatch):
    """**Best effort, and reported rather than raised.** The post is published
    once the store says so; the push only decides when the site shows it. A
    publish reported as failed because the push did not land would send the
    operator to re-publish something already in the store."""
    import journal_push
    monkeypatch.setattr(
        journal_push, "push_corpus",
        lambda: {"pushed": False, "corpus_sha": None, "reason": "github answered 500"})
    client.post("/api/blog/drafts",
                json={"slug": "a-post", "title": "A Post", "body": "words"},
                headers=_human("mem@example.com"))
    r = client.post("/api/blog/drafts/a-post/publish", headers=_human("mem@example.com"))

    assert r.status_code == 200
    assert r.json()["published"] is True
    assert r.json()["journal"]["pushed"] is False
    assert r.json()["journal"]["reason"] == "github answered 500"


def test_publishing_a_post_with_no_attested_author_is_refused(client):
    """**The reachable case changed and the guard still matters.** It used to be
    a hand-written file with no frontmatter; there are no files now, so nobody
    can hand-write a draft. What remains is the imported corpus: `greetings` came
    out of `convivy-lab` with no `author` in its frontmatter, and `ingest`
    deliberately stored it empty rather than inferring the obvious answer.

    Publishing such a post would put an unattributed piece on a public site under
    a provenance label nobody attested, so it is refused until somebody saves it
    through the Writing Desk and an identity is attached."""
    import datetime as dt
    blog_store.ingest(slug="imported", title="Imported", body="body",
                      author="", authorship="human",
                      published_at=dt.datetime(2026, 7, 1), status=blog_store.DRAFT)
    r = client.post("/api/blog/drafts/imported/publish",
                    headers=_human("mem@example.com"))
    assert r.status_code == 409

def test_publishing_an_empty_post_is_refused(client):
    """Whitespace is not a post. Reachable through the normal path now that a
    draft is a row rather than a hand-written file."""
    client.post("/api/blog/drafts", json={"slug": "empty", "title": "Empty", "body": "   "},
                headers=_human("mem@example.com"))
    r = client.post("/api/blog/drafts/empty/publish", headers=_human("mem@example.com"))
    assert r.status_code == 400
    assert blog_store.get("empty").status == blog_store.DRAFT, "it was published anyway"


# --- the listing agrees with the parse the read/write paths use ----------------


@pytest.mark.parametrize("title", [
    "React: A New Approach",     # a colon makes safe_dump single-quote the scalar
    'Title with "quotes"',       # a double quote was eaten by .strip('"')
    "Ends with a colon:",
    "It's got an apostrophe",
    "Plain title",
])

def test_a_quoted_title_lists_exactly_as_it_was_saved(client, title):
    """Reading the scalar by hand agreed with the serializer only for titles that
    needed no quoting. Both paths now go through the same parse, so the listing
    cannot drift from what a read returns (code review, #114)."""
    client.post("/api/blog/drafts", json={"slug": "quoted", "title": title, "body": "x"},
                headers=_human("mem@example.com"))
    r = client.get("/api/blog/drafts", headers=_human("mem@example.com"))
    assert r.status_code == 200
    assert r.json()["drafts"][0]["title"] == title


def test_a_published_post_can_still_be_loaded_into_the_editor(client):
    """**A regression this PR introduced, caught by `reviewer`.** The first cut
    404'd `get_draft` whenever the post was published — but `blog_store.save`
    deliberately writes to a published row, because editing a live post and
    re-publishing is the normal correction path. So the Writing Desk could save a
    post it could not open. Nothing else fills the gap: `/blog/posts` omits the
    body and `/blog/corpus` refuses a human. The file-backed version had no such
    check and this worked on `main`."""
    client.post("/api/blog/drafts",
                json={"slug": "live", "title": "Live", "body": "original"},
                headers=_human("mem@example.com"))
    client.post("/api/blog/drafts/live/publish", headers=_human("mem@example.com"))

    r = client.get("/api/blog/drafts/live", headers=_human("mem@example.com"))
    assert r.status_code == 200, "a published post cannot be reopened for editing"
    assert r.json()["body"] == "original"
    assert r.json()["status"] == "published", "the editor cannot tell which it is"

    # And the round trip still works: load, edit, re-publish.
    client.put("/api/blog/drafts/live", json={"body": "corrected"},
               headers=_human("mem@example.com"))
    client.post("/api/blog/drafts/live/publish", headers=_human("mem@example.com"))
    assert blog_store.get("live").body == "corrected"


def test_create_does_not_look_before_it_inserts(client):
    """**The previous version of this test was theatre and `reviewer` proved it**
    (PR #197). It planted a row before calling the real `create`, so ANY
    implementation refused at that point — including a `get()`-then-`add()`, which
    it was written to catch. It verified "create refuses a taken slug", which was
    never in doubt.

    The property that matters cannot be observed from outside: it is that the
    refusal comes from the INSERT rather than from a prior lookup, because a
    check-then-write lets two concurrent creates both pass the check and the
    loser's row overwrite the winner while its author receives a 201 — the draft
    gone, attributed to somebody else (code review, #114).

    So this asserts the mechanism directly: `create` must not read the row it is
    about to write. A future refactor to `get()`-then-`add()` fails here.
    """
    import sqlalchemy.orm

    real_get = sqlalchemy.orm.Session.get
    looked = []

    def _spy(self, entity, ident, *a, **kw):
        looked.append(getattr(entity, "__name__", str(entity)))
        return real_get(self, entity, ident, *a, **kw)

    sqlalchemy.orm.Session.get = _spy
    try:
        blog_store.create(slug="fresh", title="T", body="b",
                          author="mem@example.com", authorship="human")
    finally:
        sqlalchemy.orm.Session.get = real_get

    assert "BlogPost" not in looked, (
        "create looked the slug up before inserting; the 409 must be the "
        "primary key's decision, not a prior check's")


def test_a_duplicate_create_is_still_refused_with_the_winner_intact(client):
    """The behaviour the mechanism buys: the second create loses, and the first
    author's post is untouched."""
    r1 = client.post("/api/blog/drafts",
                     json={"slug": "raced", "title": "First", "body": "first"},
                     headers=_human("mem@example.com"))
    r2 = client.post("/api/blog/drafts",
                     json={"slug": "raced", "title": "Second", "body": "second"},
                     headers=_human("operator@example.com"))

    assert r1.status_code == 201 and r2.status_code == 409
    survivor = blog_store.get("raced")
    assert survivor.body == "first"
    assert survivor.author == "mem@example.com", "attributed to the wrong writer"


# --- the access log (issue_01M1Z882KE85C88GBSB2CS1XJ5) -----------------------


def _log_lines(path_fragment):
    import server as srv
    return [ln.split("\t") for ln in srv.ACCESS_LOG.read_text(encoding="utf-8").splitlines()
            if path_fragment in ln]


def test_a_forbidden_api_call_is_logged_with_who_was_refused(client):
    """The exact case the issue names: `403 blog.write required` from
    `blog_api._authorized` reached the browser and left no trace at all, so
    "The operator's role does not carry the cap" and "the browser never sent the request"
    produced identical evidence while Save was being diagnosed.

    The identity matters as much as the line: a log recording every refusal as
    anonymous would not tell a reader who was refused."""
    r = client.post("/api/blog/drafts", json={"slug": "p", "body": "x"},
                    headers=_human("con@example.com"))
    assert r.status_code == 403
    fields = _log_lines("/api/blog/drafts")
    assert len(fields) == 1
    assert fields[0][1] == "403"
    assert fields[0][2] == "con@example.com"
    assert fields[0][3] == "human"            # the KIND, not the role
    assert fields[0][4] == "auth=cf-access"


def test_the_role_never_reaches_the_log(client):
    """`authz`'s module docstring says roles.json's CONTENTS are access-control
    data that must never be logged, and `human_identity` reads `role` straight
    out of that file. A role column would have made access.log a second,
    append-only copy of the email-to-role mapping — readable by anything with
    log access rather than by roles.json's intended readers.

    `con@example.com` is a contractor and `operator@example.com` an admin in this
    fixture's roles.json, so either word appearing anywhere in the file is the
    leak (security review, PR #275)."""
    client.post("/api/blog/drafts", json={"slug": "p", "body": "x"},
                headers=_human("con@example.com"))
    client.post("/api/blog/drafts", json={"slug": "q", "body": "x"},
                headers=_human("operator@example.com"))
    import server as srv
    log = srv.ACCESS_LOG.read_text(encoding="utf-8")
    assert "/api/blog/drafts" in log          # the lines are there to be checked
    assert "contractor" not in log
    assert "admin" not in log


