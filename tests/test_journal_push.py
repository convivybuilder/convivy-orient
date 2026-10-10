"""The corpus push to `convivy/swabbys-journal` (`issue_01M0N650ZJHPQYPF54HD22RJEH`,
step 2). Orient writes `corpus.json` to `main`, then starts Shipwright's site
build through `workflow_dispatch`.

Every test here is about the properties that make the push safe: a publish
that succeeded must never be reported as failed because a network step failed,
nothing else is written to the journal repository, and the only request outside
it is one dispatch of the site workflow, sent with its own token after a new
commit.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

import blog_store
import journal_push

JOURNAL_API = "https://api.github.com/repos/convivy/swabbys-journal/"
SITE_DISPATCH = "https://api.github.com/repos/convivy/shipwright/actions/workflows/site.yml/dispatches"


@pytest.fixture
def token(tmp_path, monkeypatch):
    f = tmp_path / "journal-push-token"
    f.write_text("a-token")
    monkeypatch.setattr(journal_push, "TOKEN_FILE", f)
    monkeypatch.delenv("ORIENT_JOURNAL_PUSH_TOKEN", raising=False)
    d = tmp_path / "journal-dispatch-token"
    d.write_text("a-dispatch-token")
    monkeypatch.setattr(journal_push, "DISPATCH_TOKEN_FILE", d)
    monkeypatch.delenv("ORIENT_JOURNAL_DISPATCH_TOKEN", raising=False)
    yield


@pytest.fixture
def fixed_corpus(monkeypatch):
    """`push_corpus` calls `blog_store.corpus()` — stubbed here so these tests
    are about the GitHub mechanics, not the store. `test_blog_api.py` pins that
    the route and this call produce the same dict."""
    data = {"posts": [{"slug": "a-post", "title": "A Post",
                       "date": "2026-07-07T22:43:48Z", "updated": "2026-07-07T22:43:48Z",
                       "author": "The operator", "authorship": "human", "body": "hello"}],
            "count": 1}
    monkeypatch.setattr(blog_store, "corpus", lambda: data)
    return data


class _R:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _json_response(obj, status=200):
    return _R(status, json.dumps(obj).encode("utf-8"))


class _Seen(list):
    def __init__(self):
        super().__init__()
        self.auth = []


def _install(monkeypatch, handler):
    """A fake `urlopen` dispatching on (method, url, body) to `handler(...) -> _R`,
    raising `urllib.error.HTTPError` itself where a handler wants to simulate one.
    Returns the list of calls seen, in order. `auth` collects each call's
    Authorization header, in the same order."""
    seen = _Seen()

    def fake(req, timeout=None):
        method = req.get_method()
        body = json.loads(req.data) if req.data else None
        # Every handler below matches on a URL suffix, so without this a push
        # to the wrong repository would pass every test (reviewer, PR #355).
        # The site dispatch is the one request allowed outside it, by exact URL.
        assert req.full_url.startswith(JOURNAL_API) or req.full_url == SITE_DISPATCH, \
            f"request outside convivy/swabbys-journal: {req.full_url}"
        seen.append((method, req.full_url, body))
        seen.auth.append(req.get_header("Authorization"))
        return handler(method, req.full_url, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return seen


def _unexpected(method, url, body):
    raise AssertionError(f"unexpected call: {method} {url}")


def _head_at(commit_sha, tree_sha, entries):
    """Handler branches for a head commit `commit_sha` whose tree `tree_sha` lists
    `entries` (a list of `{"path", "sha"}`)."""
    def branches(method, url, body):
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return _json_response({"object": {"sha": commit_sha}})
        if method == "GET" and url.endswith(f"/git/commits/{commit_sha}"):
            return _json_response({"tree": {"sha": tree_sha}})
        if method == "GET" and url.endswith(f"/git/trees/{tree_sha}"):
            return _json_response({"tree": entries})
        return None
    return branches


# --- a publish: one fast-forward commit on main, force: false ----------------


def test_a_publish_makes_one_fast_forward_commit_on_main(token, fixed_corpus, monkeypatch):
    head = _head_at("commit1", "treeA", [{"path": "corpus.json", "sha": "blob-old"}])

    def handler(method, url, body):
        if method == "POST" and url.endswith("/git/blobs"):
            assert body["encoding"] == "utf-8"
            return _json_response({"sha": "blob-new"})
        if method == "POST" and url.endswith("/git/trees"):
            assert body == {"tree": [{"path": "corpus.json", "mode": "100644",
                                      "type": "blob", "sha": "blob-new"}]}
            return _json_response({"sha": "tree2"})
        if method == "POST" and url.endswith("/git/commits"):
            assert body["tree"] == "tree2"
            assert body["parents"] == ["commit1"]
            return _json_response({"sha": "commit2"})
        if method == "PATCH" and url.endswith("/git/refs/heads/main"):
            assert body == {"sha": "commit2", "force": False}
            return _json_response({})
        if method == "POST" and url == SITE_DISPATCH:
            return _R(204)
        return head(method, url, body) or _unexpected(method, url, body)

    seen = _install(monkeypatch, handler)
    out = journal_push.push_corpus()

    assert out == {"pushed": True, "corpus_sha": "commit2", "reason": None,
                   "site_build": {"dispatched": True, "reason": None}}
    assert not any(m == "POST" and u.endswith("/git/refs") for m, u, _ in seen), \
        "main exists; it is updated, never created"


def test_the_first_publish_replaces_the_initial_commits_tree_with_corpus_json_alone(
        token, fixed_corpus, monkeypatch):
    """The repository is created with an initial commit holding files such as a
    README. The first publish's commit has that commit as its parent and a tree
    holding only `corpus.json`, so the initial files are gone and the commit is
    still a fast-forward."""
    head = _head_at("initial", "treeI", [{"path": "README.md", "sha": "readme"}])
    trees = []

    def handler(method, url, body):
        if method == "POST" and url.endswith("/git/blobs"):
            return _json_response({"sha": "blob-new"})
        if method == "POST" and url.endswith("/git/trees"):
            trees.append(body)
            return _json_response({"sha": "tree2"})
        if method == "POST" and url.endswith("/git/commits"):
            assert body["parents"] == ["initial"]
            return _json_response({"sha": "commit2"})
        if method == "PATCH" and url.endswith("/git/refs/heads/main"):
            return _json_response({})
        if method == "POST" and url == SITE_DISPATCH:
            return _R(204)
        return head(method, url, body) or _unexpected(method, url, body)

    _install(monkeypatch, handler)
    out = journal_push.push_corpus()

    assert out["pushed"] is True
    assert trees == [{"tree": [{"path": "corpus.json", "mode": "100644",
                                "type": "blob", "sha": "blob-new"}]}]


def test_the_publish_tree_holds_only_corpus_json(token, fixed_corpus, monkeypatch):
    """**Plant the old behaviour: a tree built with `base_tree`.** A tree that
    carries a `base_tree` inherits every file already on `main`, so a publish
    would keep the repository's other files. The requirement is a tree with ONLY
    `corpus.json`."""
    seen_tree_bodies = []
    head = _head_at("commit1", "treeA", [{"path": "corpus.json", "sha": "blob-old"}])

    def handler(method, url, body):
        if method == "POST" and url.endswith("/git/blobs"):
            return _json_response({"sha": "blob-new"})
        if method == "POST" and url.endswith("/git/trees"):
            seen_tree_bodies.append(body)
            return _json_response({"sha": "tree2"})
        if method == "POST" and url.endswith("/git/commits"):
            return _json_response({"sha": "commit2"})
        if method == "PATCH" and url.endswith("/git/refs/heads/main"):
            return _json_response({})
        if method == "POST" and url == SITE_DISPATCH:
            return _R(204)
        return head(method, url, body) or _unexpected(method, url, body)

    _install(monkeypatch, handler)
    journal_push.push_corpus()

    assert len(seen_tree_bodies) == 1
    assert "base_tree" not in seen_tree_bodies[0]
    assert len(seen_tree_bodies[0]["tree"]) == 1


# --- a missing branch is an error, never a create ----------------------------


def test_a_missing_main_ref_is_reported_and_nothing_is_created(token, fixed_corpus, monkeypatch):
    """The repository is created with its initial commit, so `main` always exists.
    A 404 on the ref means the repository or its configuration is wrong; the
    push reports it rather than creating a branch."""
    def handler(method, url, body):
        if method == "POST" and url.endswith("/git/blobs"):
            return _json_response({"sha": "blob-new"})
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _unexpected(method, url, body)

    seen = _install(monkeypatch, handler)
    out = journal_push.push_corpus()

    assert out == {"pushed": False, "corpus_sha": None, "reason": "github answered 404",
                   "site_build": None}
    assert not any(u.endswith("/git/refs") and m == "POST" for m, u, _ in seen)


# --- an unchanged corpus: no commit, no ref update ---------------------------


def test_an_unchanged_corpus_makes_no_commit(token, fixed_corpus, monkeypatch):
    head = _head_at("commit1", "treeA", [{"path": "corpus.json", "sha": "blob1"}])

    def handler(method, url, body):
        if method == "POST" and url.endswith("/git/blobs"):
            return _json_response({"sha": "blob1"})  # same content as last time
        return head(method, url, body) or _unexpected(method, url, body)

    seen = _install(monkeypatch, handler)
    out = journal_push.push_corpus()

    assert out == {"pushed": False, "corpus_sha": "commit1", "reason": "unchanged",
                   "site_build": None}
    assert not any(u.endswith("/git/trees") for _, u, _ in seen), "no new tree"
    assert not any(u.endswith("/git/commits") and m == "POST" for m, u, _ in seen), "no new commit"
    assert not any(m == "PATCH" for m, _, _ in seen), "no ref update"
    assert not any(u == SITE_DISPATCH for _, u, _ in seen), "no site build for an unchanged corpus"


# --- a push failure: a structured result, never an exception ----------------


def test_a_push_failure_reports_and_stops_rather_than_raising(token, fixed_corpus, monkeypatch):
    def handler(method, url, body):
        if method == "POST" and url.endswith("/git/blobs"):
            raise urllib.error.HTTPError(url, 500, "server error", {}, None)
        return _unexpected(method, url, body)

    seen = _install(monkeypatch, handler)
    out = journal_push.push_corpus()

    assert out == {"pushed": False, "corpus_sha": None, "reason": "github answered 500",
                   "site_build": None}
    assert len(seen) == 1, "nothing after the failed blob write, and no site build"


def test_a_missing_credential_makes_no_network_call(tmp_path, fixed_corpus, monkeypatch):
    monkeypatch.setattr(journal_push, "TOKEN_FILE", tmp_path / "absent")
    monkeypatch.delenv("ORIENT_JOURNAL_PUSH_TOKEN", raising=False)

    def fake(req, timeout=None):
        raise AssertionError("no network call should be made with no credential")

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    out = journal_push.push_corpus()

    assert out["pushed"] is False
    assert out["corpus_sha"] is None
    assert "no journal push credential" in out["reason"]


def test_a_network_failure_during_the_push_does_not_raise(token, fixed_corpus, monkeypatch):
    def handler(method, url, body):
        if method == "POST" and url.endswith("/git/blobs"):
            raise OSError("down")
        return _unexpected(method, url, body)

    _install(monkeypatch, handler)
    out = journal_push.push_corpus()
    assert out["pushed"] is False
    assert out["reason"] == "OSError"


# --- the site build: one dispatch after a new commit, its own token ---------


def _publishing_handler(dispatch):
    """A handler for a publish that makes a new commit, answering the site
    dispatch with `dispatch(url)`."""
    head = _head_at("commit1", "treeA", [{"path": "corpus.json", "sha": "blob-old"}])

    def handler(method, url, body):
        if method == "POST" and url.endswith("/git/blobs"):
            return _json_response({"sha": "blob-new"})
        if method == "POST" and url.endswith("/git/trees"):
            return _json_response({"sha": "tree2"})
        if method == "POST" and url.endswith("/git/commits"):
            return _json_response({"sha": "commit2"})
        if method == "PATCH" and url.endswith("/git/refs/heads/main"):
            return _json_response({})
        if method == "POST" and url == SITE_DISPATCH:
            return dispatch(url)
        return head(method, url, body) or _unexpected(method, url, body)
    return handler


def test_a_new_commit_starts_the_site_build_on_main_with_the_dispatch_token(
        token, fixed_corpus, monkeypatch):
    """**Plant the old behaviour: no request after the ref update.** The site no
    longer polls hourly, so a commit that sends no dispatch waits for the next scheduled
    run. The dispatch must also carry the dispatch token: the push token holds
    no Actions permission on `convivy/shipwright`, and the dispatch token holds
    nothing on the journal."""
    seen = _install(monkeypatch, _publishing_handler(lambda url: _R(204)))
    out = journal_push.push_corpus()

    assert out["pushed"] is True
    assert out["site_build"] == {"dispatched": True, "reason": None}
    assert [(m, u, b) for m, u, b in seen if u == SITE_DISPATCH] == \
        [("POST", SITE_DISPATCH, {"ref": "main"})]
    assert seen[-1][1] == SITE_DISPATCH, "the dispatch follows the ref update"
    assert seen.auth[-1] == "Bearer a-dispatch-token"
    assert all(a == "Bearer a-token" for a in seen.auth[:-1])


def test_a_refused_dispatch_is_reported_once_and_the_push_still_counts(
        token, fixed_corpus, monkeypatch):
    def refuse(url):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    seen = _install(monkeypatch, _publishing_handler(refuse))
    out = journal_push.push_corpus()

    assert out["pushed"] is True
    assert out["corpus_sha"] == "commit2"
    assert out["reason"] is None
    assert out["site_build"] == {"dispatched": False, "reason": "github answered 403"}
    assert sum(1 for _, u, _ in seen if u == SITE_DISPATCH) == 1, "no retry"


def test_a_refused_ref_update_sends_no_dispatch(token, fixed_corpus, monkeypatch):
    """**Plant a dispatch sent before the ref update.** A commit object that
    `main` never points at is not on the site's branch, so starting the build
    for it would deploy the old corpus. The dispatch must follow a ref update
    that succeeded."""
    head = _head_at("commit1", "treeA", [{"path": "corpus.json", "sha": "blob-old"}])

    def handler(method, url, body):
        if method == "POST" and url.endswith("/git/blobs"):
            return _json_response({"sha": "blob-new"})
        if method == "POST" and url.endswith("/git/trees"):
            return _json_response({"sha": "tree2"})
        if method == "POST" and url.endswith("/git/commits"):
            return _json_response({"sha": "commit2"})
        if method == "PATCH" and url.endswith("/git/refs/heads/main"):
            raise urllib.error.HTTPError(url, 422, "not a fast forward", {}, None)
        return head(method, url, body) or _unexpected(method, url, body)

    seen = _install(monkeypatch, handler)
    out = journal_push.push_corpus()

    assert out == {"pushed": False, "corpus_sha": None, "reason": "github answered 422",
                   "site_build": None}
    assert not any(u == SITE_DISPATCH for _, u, _ in seen)


def test_a_network_failure_on_the_dispatch_does_not_raise(token, fixed_corpus, monkeypatch):
    def down(url):
        raise OSError("down")

    _install(monkeypatch, _publishing_handler(down))
    out = journal_push.push_corpus()

    assert out["pushed"] is True
    assert out["site_build"] == {"dispatched": False, "reason": "OSError"}


def test_a_missing_dispatch_credential_skips_the_dispatch_and_says_so(
        token, fixed_corpus, monkeypatch, tmp_path):
    monkeypatch.setattr(journal_push, "DISPATCH_TOKEN_FILE", tmp_path / "absent")

    seen = _install(monkeypatch, _publishing_handler(
        lambda url: _unexpected("POST", url, None)))
    out = journal_push.push_corpus()

    assert out["pushed"] is True
    assert out["site_build"]["dispatched"] is False
    assert "no site build credential" in out["site_build"]["reason"]
    assert not any(u == SITE_DISPATCH for _, u, _ in seen)


def test_the_dispatch_token_can_come_from_the_environment(token, fixed_corpus, monkeypatch, tmp_path):
    monkeypatch.setattr(journal_push, "DISPATCH_TOKEN_FILE", tmp_path / "absent")
    monkeypatch.setenv("ORIENT_JOURNAL_DISPATCH_TOKEN", "env-dispatch-token")

    seen = _install(monkeypatch, _publishing_handler(lambda url: _R(204)))
    out = journal_push.push_corpus()

    assert out["site_build"] == {"dispatched": True, "reason": None}
    assert seen.auth[-1] == "Bearer env-dispatch-token"
