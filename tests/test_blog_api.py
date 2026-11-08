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
import control_db
import library_client
import library_fake
import server as srv

AGENT_TOKEN = "agent-secret-xyz"


def _archive_revisions(slug):
    """Every Archives entry for `slug`, oldest first — the same read
    `blog_api._slug_entries` does, so a test can check what a route actually
    wrote through the Library, the only store a post has (CQ·168, CQ·170)."""
    posts = library_client.list_signals(product=blog_api.PRODUCT, kind=[blog_api.KIND_POST],
                                        stream=slug)
    drafts = library_client.list_signals(product=blog_api.PRODUCT, kind=[blog_api.KIND_DRAFT],
                                         stream=slug, human_reserved=True)
    sigs = sorted([*posts, *drafts], key=lambda s: s.produced_at or s.ingested_at)
    return sigs


def _archive_latest(slug):
    sigs = _archive_revisions(slug)
    return sigs[-1] if sigs else None


def _archive_body(slug):
    sig = _archive_latest(slug)
    if sig is None:
        return None
    return library_client.get_signal_blob(sig.id, human_reserved=(sig.kind == blog_api.KIND_DRAFT))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(control_db, "CONTROL_DB", tmp_path / "control.db")
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    control_db.init_db()
    library_fake.patch(monkeypatch)

    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({
        "operator@example.com": {"role": "admin", "products": ["*"]},
        "mem@example.com": {"role": "member", "products": ["alpha"]},
        "obs@example.com": {"role": "observer", "products": ["*"]},
        "con@example.com": {"role": "contractor", "products": ["alpha"]},
        "ctb@example.com": {"role": "contributor", "products": ["alpha"]},
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
    toks.write_text(json.dumps({AGENT_TOKEN: {"name": "an-agent", "products": ["alpha"]}}))
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
    sig = _archive_latest("a-post")
    assert sig.actor == "mem@example.com"
    title = sig.meta.get("title", "")
    assert "someone@else.example" not in (sig.actor + title + _archive_body("a-post"))


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


def test_a_save_attests_its_own_writer_and_never_an_earlier_entrys(client):
    """Rule 1 still holds — the author comes from the verified identity, never
    from the request body — but with the Archives as the store, each revision
    carries its OWN attested author rather than one frozen at creation: E26's
    spec calls it "its author, taken from the identity Orient verified for the
    person writing" for EACH entry, and `operator@example.com` genuinely wrote
    this one. Saving still never rewrites an EARLIER entry's own attestation,
    which stays `mem@example.com` below — append-only, not "immutable
    forever"."""
    client.post("/api/blog/drafts", json={"slug": "p", "title": "T", "body": "one"},
                headers=_human("mem@example.com"))
    r = client.put("/api/blog/drafts/p",
                   json={"body": "two", "author": "someone@else.example",
                         "authorship": "human"},
                   headers=_human("operator@example.com"))
    assert r.status_code == 200
    sig = _archive_latest("p")
    assert sig.actor == "operator@example.com", "the real writer was not attested"
    assert sig.meta["authorship"] == "human"
    assert "someone@else.example" not in (sig.actor + _archive_body("p"))

    first = _archive_revisions("p")[0]
    assert first.actor == "mem@example.com", "an earlier entry's attestation was rewritten"


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

    sig = _archive_latest("inject")
    assert sig.actor == "mem@example.com"
    assert sig.meta["authorship"] == "human"
    assert sig.meta["title"] == evil[:blog_api.MAX_TITLE], "the title was not stored verbatim"

def test_a_title_cannot_forge_the_author_on_save_either(client):
    """The same property on the other write path. `save_draft` takes no
    author parameter at all, so this is now structural rather than defended."""
    client.post("/api/blog/drafts", json={"slug": "s", "title": "T", "body": "one"},
                headers=_human("mem@example.com"))
    client.put("/api/blog/drafts/s",
               json={"title": 'X"\nauthor: someone@else.example\ny: "', "body": "two"},
               headers=_human("mem@example.com"))

    sig = _archive_latest("s")
    assert sig.actor == "mem@example.com"
    assert _archive_body("s") == "two"

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

    assert _archive_body("hr") == prose
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
    assert _archive_latest("nope") is None

def test_an_agent_token_may_not_author(client):
    """The surface publishes to a PUBLIC-facing site, so no automated caller
    reaches it — `blog_write` is absent from the agent and system roles, and the
    human-only check is a second lock on the same door."""
    r = client.post("/api/blog/drafts", json={"slug": "by-agent", "body": "x"},
                    headers={"Authorization": f"Bearer {AGENT_TOKEN}"})
    assert r.status_code in (401, 403)
    assert _archive_latest("by-agent") is None


def test_an_unauthenticated_caller_gets_401(client):
    assert client.post("/api/blog/drafts", json={"slug": "x", "body": "y"}).status_code == 401


def test_a_cross_origin_post_is_refused(client):
    r = client.post("/api/blog/drafts", json={"slug": "csrf", "body": "x"},
                    headers=_human("mem@example.com", origin="http://evil.example"))
    assert r.status_code == 403
    assert _archive_latest("csrf") is None


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


# --- listing published posts, now from the Library's Archives ----------------
#
# These read the Archives rather than `convivy-lab`'s git HEAD
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


def test_list_posts_never_asks_the_library_for_a_draft_row(client, monkeypatch):
    """DoD: a draft must not appear to a viewer without Writing Desk write
    access on any Orient page OR ANY LIBRARY READ ORIENT MAKES ON THEIR
    BEHALF. Proven by making a `human_reserved` call raise outright — if
    `list_posts` ever asked for a `kind=post_draft` row, this errors rather
    than merely showing an empty field that could pass for the right reason
    by accident."""
    client.post("/api/blog/drafts", json={"slug": "secret", "title": "Secret", "body": "x"},
                headers=_human("mem@example.com"))

    real_list_signals = library_client.list_signals

    def _guarded(*, human_reserved=False, **kw):
        if human_reserved:
            raise AssertionError("list_posts asked the Library for a post_draft row")
        return real_list_signals(human_reserved=human_reserved, **kw)

    monkeypatch.setattr(library_client, "list_signals", _guarded)
    r = client.get("/api/blog/posts", headers=_human("obs@example.com"))
    assert r.status_code == 200
    assert r.json()["posts"] == []


def test_a_migrated_posts_current_entry_is_latest_until_a_new_save(client):
    """Mirrors `bin/copy_posts_to_archives.py`'s own shapes: a real revision
    `1` plus one `reason="current"` mirror carrying no number, dated after it.
    The mirror is latest — and reachable with no `?revision=` at all — until a
    post-deploy save gives the slug an entry dated later still, continuing the
    numbering from the real revision rather than from the mirror."""
    import datetime as dt
    library_fake.seed_signal(
        id="sig_rev1", source="blog_migration", kind=blog_api.KIND_POST,
        actor="writer@example.com", ingested_by="migration", product=blog_api.PRODUCT,
        stream="old-post", body="Old Post",
        meta={"title": "Old Post", "revision": 1, "authorship": "human", "reason": "publish"},
        content="original text", produced_at=dt.datetime(2026, 1, 1))
    library_fake.seed_signal(
        id="sig_current", source="blog_migration", kind=blog_api.KIND_POST,
        actor="writer@example.com", ingested_by="migration", product=blog_api.PRODUCT,
        stream="old-post", body="Old Post",
        meta={"title": "Old Post", "revision": "current", "authorship": "human",
              "reason": "current"},
        content="current text", produced_at=dt.datetime(2026, 2, 1))

    r = client.get("/api/blog/drafts/old-post", headers=_human("mem@example.com"))
    assert r.status_code == 200
    assert r.json()["body"] == "current text"
    assert r.json()["revision"] is None
    assert r.json()["revisions"] == [1]

    client.put("/api/blog/drafts/old-post", json={"body": "post-deploy edit"},
               headers=_human("mem@example.com"))
    r2 = client.get("/api/blog/drafts/old-post", headers=_human("mem@example.com"))
    assert r2.json()["body"] == "post-deploy edit"
    assert r2.json()["revision"] == 2
    assert r2.json()["revisions"] == [1, 2]

    r3 = client.get("/api/blog/drafts/old-post?revision=1", headers=_human("mem@example.com"))
    assert r3.json()["body"] == "original text"


def test_a_malformed_or_unknown_revision_is_refused(client):
    created = client.post("/api/blog/drafts", json={"slug": "p", "title": "P", "body": "text"},
                          headers=_human("mem@example.com"))
    assert created.status_code == 201
    r = client.get("/api/blog/drafts/p?revision=abc", headers=_human("mem@example.com"))
    assert r.status_code == 400
    r2 = client.get("/api/blog/drafts/p?revision=99", headers=_human("mem@example.com"))
    assert r2.status_code == 404


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
    # filesystem to inspect now; the Archives are where a hostile slug would land.
    assert _archive_latest(slug) is None, "a hostile slug reached the Archives"


def test_mixed_case_is_normalized_rather_than_refused(client):
    """The caller is typing a title, not a path. Lowercasing is a courtesy the
    guard runs BEFORE, so the strict shape still governs from there."""
    r = client.post("/api/blog/drafts", json={"slug": "Mixed-Case", "body": "x"},
                    headers=_human("mem@example.com"))
    assert r.status_code == 201 and r.json()["slug"] == "mixed-case"
    assert _archive_latest("mixed-case") is not None

def test_a_duplicate_slug_is_refused_rather_than_silently_overwriting(client):
    client.post("/api/blog/drafts", json={"slug": "dupe", "body": "first"},
                headers=_human("mem@example.com"))
    r = client.post("/api/blog/drafts", json={"slug": "dupe", "body": "second"},
                    headers=_human("operator@example.com"))
    assert r.status_code == 409
    assert _archive_body("dupe") == "first", "the loser overwrote the winner"


# --- publishing, which now writes the store and pushes the corpus to the journal --
#
# `lab-blog publish` is gone (issue_01M0N650ZJHPQYPF54HD22RJEH). It wrote into
# `convivy-lab` and ran a leak guard; the repo is being deleted and the guard was
# retired with the dashboard pipeline it existed for. Four tests went with it —
# the guarded-tool shell, the tool-absent report, the guard-refusal surface and
# the subprocess timeout — because they asserted a control that was deliberately
# removed rather than one that broke. What replaces them is below.


def test_publish_writes_the_archives_and_pushes_the_corpus_to_the_journal(client, monkeypatch):
    """**The defect this closes.** Publish used to write `convivy-lab` while the
    corpus route read Orient's own store, so a newly published post reached
    neither the store nor the site — the posts on the old site were there
    purely from the one-time import, and both halves looked like they worked.

    The publish calls `journal_push.push_corpus` with no arguments and returns
    its result, site build included, as `journal`."""
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

    latest = _archive_latest("a-post")
    assert latest is not None and latest.kind == blog_api.KIND_POST
    assert _archive_body("a-post").strip() == "words"


def test_publish_records_the_attested_publisher_on_the_revision(client):
    """A publication with nobody attached is the thing that must not be possible.
    `ingest` carries no actor because an imported post predates the store; this
    path has a verified identity and must record it."""
    client.post("/api/blog/drafts",
                json={"slug": "a-post", "title": "A Post", "body": "words"},
                headers=_human("mem@example.com"))
    client.post("/api/blog/drafts/a-post/publish", headers=_human("mem@example.com"))

    rev = _archive_revisions("a-post")[-1]
    assert rev.meta["reason"] == "publish"
    assert rev.actor == "mem@example.com"


def test_publishing_an_edit_keeps_the_original_publication_date(client):
    """A blog is ordered by when a piece went out. Fixing a typo must not lift a
    two-month-old post to the top of the site."""
    client.post("/api/blog/drafts",
                json={"slug": "a-post", "title": "A Post", "body": "first"},
                headers=_human("mem@example.com"))
    r1 = client.post("/api/blog/drafts/a-post/publish", headers=_human("mem@example.com"))
    first_date = r1.json()["published_at"]

    client.put("/api/blog/drafts/a-post",
               json={"title": "A Post", "body": "second"},
               headers=_human("mem@example.com"))
    r2 = client.post("/api/blog/drafts/a-post/publish", headers=_human("mem@example.com"))

    assert r2.json()["published_at"] == first_date
    assert _archive_body("a-post").strip() == "second"
    # create, the first publish, the PUT save, then the second publish — four
    # Archives entries, oldest first.
    revs = _archive_revisions("a-post")
    assert [r.meta.get("reason") for r in revs] == ["create", "publish", "save", "publish"]
    assert library_client.get_signal_blob(
        revs[1].id, human_reserved=(revs[1].kind == blog_api.KIND_DRAFT)).strip() == "first"
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
    can hand-write a draft. What remains is a row the Archives migration itself
    cannot produce any more (its own `--authorless-actor` fills every empty
    author in before the write), but a signal with an empty `actor` is still a
    shape this module cannot rule out by construction, so the guard stays and
    this seeds one straight into the Archives the way the migration no longer
    can.

    Publishing such a post would put an unattributed piece on a public site under
    a provenance label nobody attested, so it is refused until somebody saves it
    through the Writing Desk and an identity is attached."""
    library_fake.seed_signal(
        id="sig_imported", source="blog_migration", kind=blog_api.KIND_DRAFT,
        actor="", ingested_by="migration", product=blog_api.PRODUCT, stream="imported",
        body="Imported", meta={"title": "Imported", "revision": 1,
                               "authorship": "human", "reason": "ingest"},
        content="body")
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
    assert _archive_latest("empty").kind == blog_api.KIND_DRAFT, "it was published anyway"


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
    404'd `get_draft` whenever the post was published — but `save_draft`
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
    assert _archive_body("live") == "corrected"


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
    assert _archive_body("raced") == "first"
    assert _archive_latest("raced").actor == "mem@example.com", "attributed to the wrong writer"


def test_create_refuses_a_slug_the_migration_left_with_no_numbered_revision(client):
    """A migrated draft that was created but never saved copies with only a
    `reason="current"` mirror and no `post:<slug>:1` key — the one shape the
    `dedup_key` collision cannot catch on its own, so the route's own
    pre-check is what refuses a second create here."""
    import datetime as dt
    library_fake.seed_signal(
        id="sig_only_current", source="blog_migration", kind=blog_api.KIND_DRAFT,
        actor="writer@example.com", ingested_by="migration", product=blog_api.PRODUCT,
        stream="never-saved", body="Never Saved",
        meta={"title": "Never Saved", "revision": "current", "authorship": "human",
              "reason": "current"},
        content="original", produced_at=dt.datetime(2026, 3, 1))
    r = client.post("/api/blog/drafts", json={"slug": "never-saved", "body": "new text"},
                    headers=_human("mem@example.com"))
    assert r.status_code == 409


def test_create_refuses_via_the_librarys_own_dedup_even_when_the_pre_check_misses_it(client):
    """The route's own pre-check cannot catch two truly concurrent creates —
    both would read "no entry yet" before either had written. `create_signal`'s
    `dedup_key="post:<slug>:1"` is what resolves that race to one winner;
    this forces the collision directly, the way two requests racing through
    the pre-check at once would, rather than relying on request timing."""
    r1 = client.post("/api/blog/drafts", json={"slug": "race2", "body": "first"},
                     headers=_human("mem@example.com"))
    assert r1.status_code == 201

    _, idempotent, existing = library_client.create_signal(
        source=blog_api.SOURCE, kind=blog_api.KIND_DRAFT, actor="operator@example.com",
        product=blog_api.PRODUCT, stream="race2", body="second", content="second",
        meta={"title": "race2", "revision": 1, "authorship": "human", "reason": "create"},
        dedup_key="post:race2:1", human_reserved=True)
    assert idempotent is True
    assert existing.actor == "mem@example.com"


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




# --- a save or publish that loses a race is told so ---------------------------

def _race(monkeypatch, slug):
    """Another writer lands revision 2 between this route's read and its
    write: the route still computes 2 from its stale read."""
    import library_client
    library_client.create_signal(
        source=blog_api.SOURCE, kind=blog_api.KIND_DRAFT, actor="other@example.com",
        product=blog_api.PRODUCT, stream=slug, body="THEIRS", content="THEIRS",
        meta={"title": "T", "revision": 2, "authorship": "human", "reason": "save"},
        dedup_key=f"post:{slug}:2", human_reserved=True)
    monkeypatch.setattr(blog_api, "_next_revision_number", lambda revs: 2)


def test_a_save_that_loses_a_race_is_refused_and_keeps_the_winner(client, monkeypatch):
    client.post("/api/blog/drafts", json={"slug": "r", "title": "T", "body": "one"},
                headers=_human("mem@example.com"))
    _race(monkeypatch, "r")
    r = client.put("/api/blog/drafts/r", json={"body": "MINE"},
                   headers=_human("operator@example.com"))
    assert r.status_code == 409
    assert _archive_body("r") == "THEIRS"


def test_a_publish_that_loses_a_race_publishes_nothing(client, monkeypatch):
    import journal_push
    pushed = []
    monkeypatch.setattr(journal_push, "push_corpus", lambda: pushed.append(True) or {})
    client.post("/api/blog/drafts", json={"slug": "q", "title": "T", "body": "one"},
                headers=_human("mem@example.com"))
    _race(monkeypatch, "q")
    r = client.post("/api/blog/drafts/q/publish", headers=_human("mem@example.com"))
    assert r.status_code == 409
    assert pushed == []
    assert _archive_latest("q").kind == blog_api.KIND_DRAFT, "it was published anyway"


# --- the `first-published` dating marker is never read as a revision -------
#
# CQ·170 part A's one-time script (issue_01M5GBJ3H5BFX0CR83019T737D)
# wrote a one-time `kind=post` entry carrying a migrated post's true
# original `published_at`, with no revision position. Every reader of a
# slug's revisions here must filter it out before it can be read as "latest".


def _seed_marker(slug, *, produced_at, title="Live", author="writer@example.com",
                 authorship="human"):
    library_client.create_signal(
        source="blog_migration", kind=blog_api.KIND_POST, actor=author,
        product=blog_api.PRODUCT, stream=slug, body="",
        meta={"title": title, "revision": None, "authorship": authorship,
              "reason": "first-published"},
        dedup_key=f"post:{slug}:first-published", produced_at=produced_at)


def _seed_current(slug, *, produced_at, title="Live", author="writer@example.com",
                  authorship="human"):
    library_client.create_signal(
        source="writing_desk", kind=blog_api.KIND_POST, actor=author,
        product=blog_api.PRODUCT, stream=slug, body="live text", content="live text",
        meta={"title": title, "revision": "current", "authorship": authorship,
              "reason": "current"},
        dedup_key=f"post:{slug}:current:{produced_at}", produced_at=produced_at)


@pytest.mark.parametrize("marker_first", [True, False])
def test_a_marker_tied_with_a_current_entry_never_wins_latest(client, marker_first):
    """Named case: a migrated post's `current` entry can share the marker's
    exact `produced_at` (the live `you-will-almost-certainly…` post does,
    both at `2026-07-28T21:59:53Z`). Tested in both insertion orders, since a
    sort that broke ties on insertion order would let the marker win half
    the time."""
    same_time = "2026-07-28T21:59:53Z"
    if marker_first:
        _seed_marker("live", produced_at=same_time)
        _seed_current("live", produced_at=same_time)
    else:
        _seed_current("live", produced_at=same_time)
        _seed_marker("live", produced_at=same_time)

    revs = blog_api._slug_entries("live")
    assert len(revs) == 1, "the marker leaked into the revision list"
    latest = blog_api._latest(revs)
    assert latest.reason == "current"


def test_a_marker_does_not_appear_in_list_posts(client):
    """Same tie as above, through the route: if the marker won `latest` it
    would supply the title `list_posts` serves, since both entries share
    the one `produced_at`."""
    same_time = "2026-07-28T21:59:53Z"
    _seed_marker("live", produced_at=same_time, title="Marker Title")
    _seed_current("live", produced_at=same_time, title="Live")

    r = client.get("/api/blog/posts", headers=_human("mem@example.com"))
    [post] = r.json()["posts"]
    assert post["title"] == "Live"


# --- every dated reader takes the marker's date -------------------------------
#
# A migrated post's `current` entry is dated to the Archives copy, months
# after the post first went out; the marker carries the real date. Found by
# `reviewer` on PR #412: publish's response and the Blog Posts page read the
# first `kind=post` entry only.


def test_list_posts_dates_a_migrated_post_by_its_marker_and_sorts_by_it(client):
    _seed_marker("old", produced_at="2026-07-06T20:11:29Z", title="Old")
    _seed_current("old", produced_at="2026-10-04T04:37:00Z", title="Old")
    _seed_marker("mid", produced_at="2026-07-18T16:04:58Z", title="Mid")
    _seed_current("mid", produced_at="2026-08-01T00:00:00Z", title="Mid")

    r = client.get("/api/blog/posts", headers=_human("mem@example.com"))
    posts = r.json()["posts"]
    assert [(p["slug"], p["date"]) for p in posts] == [
        ("mid", "2026-07-18T16:04:58Z"), ("old", "2026-07-06T20:11:29Z")]
    assert posts[1]["updated"] == "2026-10-04T04:37:00Z"


def test_published_views_dates_a_migrated_post_by_its_marker(client):
    _seed_marker("old", produced_at="2026-07-06T20:11:29Z")
    _seed_current("old", produced_at="2026-10-04T04:37:00Z")
    [view] = blog_api.published_views()
    assert blog_api._iso(view.published_at) == "2026-07-06T20:11:29Z"


def test_republishing_a_migrated_post_reports_its_marker_date(client):
    _seed_marker("old", produced_at="2026-07-06T20:11:29Z")
    _seed_current("old", produced_at="2026-10-04T04:37:00Z")
    client.put("/api/blog/drafts/old", json={"body": "edited"},
               headers=_human("mem@example.com"))
    r = client.post("/api/blog/drafts/old/publish", headers=_human("mem@example.com"))
    assert r.status_code == 200, r.text
    assert r.json()["published_at"] == "2026-07-06T20:11:29Z"


def test_a_post_with_no_marker_is_dated_by_its_first_publish(client):
    _publish(client, "fresh", title="Fresh")
    first = client.get("/api/blog/posts", headers=_human("mem@example.com")).json()["posts"][0]
    client.put("/api/blog/drafts/fresh", json={"body": "edited"},
               headers=_human("mem@example.com"))
    r = client.post("/api/blog/drafts/fresh/publish", headers=_human("mem@example.com"))
    assert r.json()["published_at"] == first["date"]
