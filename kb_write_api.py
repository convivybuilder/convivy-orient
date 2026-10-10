#!/usr/bin/env python3.11
"""Coordination for the in-window KB document editor — option C
(issue_01M0TKX6DW2E1TXGN9QT6K61J5 §4). Heartbeat and checkin proxy straight
to the Library's own coordination routes. Checkout takes the Library's lock and
then reads the stored file through `agentctl` (`kb_commit.read_document`), so
it answers 502 when that read fails and hands the lock back. Save runs
admission and then commits through `kb_commit.commit_document`.

**This is the first surface in this repo where Orient writes anything toward
the knowledge repository.** Every other Library call `library_client.py`
makes is a read; the three write calls here (`checkout_reference_document`,
`heartbeat_reference_checkout`, `checkin_reference_document`) take and release
a coordination LOCK — a row in the Library's own database — and never touch a
document's text. `library#45`'s own module docstring is explicit: "The
Library does not commit or push." Treat this module as new ground rather than
an extension of `blog_api.py`'s pattern, even though the shape below borrows
from it deliberately.

**Full tier AND `kb.write`, both checked here rather than assumed from the
page.** `/reference/doc/{id}` already 404s a contractor-tier viewer in
`server.py` before this module is ever reached, but that is a PAGE gate — a
contractor who somehow reached these routes directly (a saved link, a replayed
request) would not be stopped by it. `_authorized` below is the wall these
routes actually stand behind, matching `blog_api._authorized`'s shape plus the
tier check that surface does not need (the Writing Desk is reachable by both
tiers; a KB document is not).

**The commit step writes for real** — `save_document` runs admission, a real
check against the Library, and then calls `kb_commit.commit_document`, which
shells to `agentctl kb apply` (see that module's docstring for the Library-
arbitrates/Bosun-executes split). Each way `agentctl` can refuse is its own
`kb_commit` exception, mapped below to a status code that tells the human
what to do next: reopen and retry (stale), wait or coordinate (lease
conflict), fix the document (rejected), or nothing they can act on (failed).
The checkout is left held on every failure branch — nothing was written, so
there is nothing to check back in, and a human who was mid-edit does not
lose their lock along with a failed save."""
from __future__ import annotations

import asyncio
import datetime as dt
import sys

import yaml
from fastapi import APIRouter, Request

import kb_commit
import library_client
from access_identity import JWT_HEADER, jwt_config, tier_for
from api_common import (_exception_line, _json, api_guard as _api, read_json_body,
                        resolve_identity, same_origin)
from authz import authorize
from logutil import clean_log_field

router = APIRouter(prefix="/api")

MAX_BODY = 400_000  # generous relative to the corpus: the largest team-audience
# document measured while this was written was well under this.


async def _authorized(request: Request, *, writes: bool = True):
    """The common gate: a verified human, full tier, `kb.write`, and (for a
    state change) same-origin. Returns `(identity, None)` or `(None,
    error-response)`.

    Ordered CSRF-after-role like `blog_api._authorized`: the origin check
    only matters once we know we would otherwise act, so a read-only caller
    (there is none below yet, `writes` exists for the one that will want it)
    is not refused on it."""
    ident = resolve_identity(request)
    if ident is None:
        return None, _json(401, {"error": "unauthorized"})
    if ident.kind != "human":
        return None, _json(403, {"error": "only a human may edit a KB document"})
    if tier_for(ident.actor) != "full":
        # 404 rather than 403, matching every full-tier page and per-id route
        # in server.py: a contractor-tier identity is not told this surface
        # exists at all.
        return None, _json(404, {"error": "not found"})
    if writes and not same_origin(request):
        return None, _json(403, {"error": "csrf: origin check failed"})
    if not authorize(ident, "kb.write", None):
        return None, _json(403, {"error": "kb.write required"})
    return ident, None


def _checkout_dict(c: "library_client.ReferenceCheckout") -> dict:
    return {"holder": c.holder,
            "expires_at": c.expires_at.isoformat() if c.expires_at else None}


# The audience this site serves, and the ONLY documents any route below may
# touch. `library_client` is the one place the entitlement rule lives (PR #205);
# this constant names who is asking, it does not re-implement the check.
_AUDIENCE = "team"


async def _visible_or_404(doc_id: str):
    """`(doc, None)` if this document may be touched, else `(None, response)`.

    Returns the document rather than discarding it: `save_document` needs the
    same row a few lines later to build the admission frontmatter, and fetching
    it twice is a second round trip for an answer already in hand (PR #207
    review).

    **Every route here takes `doc_id` straight from the request path**, so
    without this a viewer holding `kb.write` could take, refresh or release
    the Library's real edit-lock on ANY document in the corpus by id —
    including the roughly half of it that is not team-audience (PR #207
    security review). The site lists those documents on `/reference` to a
    full-tier reader by id, title and a summary drawn from the body, a
    deliberate exception the operator kept on 2026-09-12. It never serves their body,
    never links them for editing, and `/reference/doc/{id}` 404s for one. Holding a
    write capability is not the same as being entitled to a document, and the
    lock routes never read a body, so nothing else on their path was checking.

    Reuses `get_reference_document`'s gate rather than adding a second
    visibility path: a cheaper bespoke check is a second thing to get wrong,
    and the per-document route measures at ~0.0s. A document that is missing
    and one that is not ours collapse to the same 404 upstream, which is the
    property that stops a guessed id confirming a document exists."""
    try:
        doc = library_client.get_reference_document(doc_id, audience=_AUDIENCE)
    except Exception:
        # The Library is unreachable. Fail CLOSED: a lock we cannot justify is
        # a lock we do not take.
        return None, _json(503, {"error": "the Library could not be reached; try again shortly"})
    if doc is None:
        return None, _json(404, {"error": "no such document"})
    return doc, None


@router.post("/kb/documents/{doc_id:path}/checkout")
@_api
async def checkout_document(doc_id: str, request: Request):
    """Take the coordination lock ahead of an edit, and return the stored file
    the editor opens on. 409 names the current holder rather than a bare
    refusal, so the editor can open read-only and say who has it — never a
    silent fall-through to an editable box.

    **`content` is the document's file as origin/main holds it, frontmatter
    and body together**, with the `content_sha` it was read at. The editor
    posts that sha back with its save, and `save_document` requires it: it is
    the stale-editor guard. The editor used to be seeded from
    `ReferenceDocument.body`, which the Library serves with the frontmatter
    stripped, so a save submitted a body `save_document` could only refuse (no
    `---` block, 422) and no document could ever be saved. This route is the
    one place that hands the editor the file it must round-trip."""
    ident, err = await _authorized(request)
    if err:
        return err
    doc, gate = await _visible_or_404(doc_id)
    if gate is not None:
        return gate
    adopted = False
    try:
        checkout = library_client.checkout_reference_document(doc_id, holder=ident.actor)
    except library_client.ReferenceCheckoutConflict as e:
        if e.checkout.holder != ident.actor:
            return _json(409, {"error": "already checked out",
                               "checkout": _checkout_dict(e.checkout)})
        # **The member's own lock**, held by an earlier session or by another
        # tab of theirs. A save that failed keeps it held on purpose (see
        # `save_document`), and a tab still editing keeps it alive with
        # heartbeats. The Library's `take` refuses any live checkout without
        # comparing holders, so without this the member is shown "Read only,
        # checked out by <their own email>" until it lapses.
        #
        # **Adopting it is a trust decision, and it is the one `save_document`,
        # `heartbeat_document` and `checkin_document` already make.** The Library
        # stores `holder` as whatever string the caller declared, so equality
        # with `ident.actor` proves a caller declared this email, not that the
        # human did. Orient always declares the verified Cloudflare Access
        # identity. Another client could declare the same string and would then
        # share this lock. Adopting it grants nothing beyond what that holder
        # string can already do here: `save_document` accepts any checkout whose
        # holder equals `ident.actor`, and every route above passes
        # `holder=ident.actor` to the Library. The commit step (`kb_commit`) is
        # untouched. The save's base sha is the one the editor opened at, posted
        # back with the save, so a second tab that adopts this lock and saves
        # against a version the first tab already replaced is refused with a
        # 409 (issue_01M2VCK379AFZAD0EXJMN4HJ82). Exact string equality, as in
        # `save_document`: a holder that differs only in case stays read-only.
        #
        # **The cost is that one member's concurrent editors now share one
        # lock.** Before this, a second tab got a read-only 409 and the first
        # kept its lock. Now tab B adopts tab A's live lock, and either tab's
        # check-in (closing the editor, or a save that lands) releases it for
        # both, so the other's next heartbeat or save answers 409 and shows
        # "lock lost".
        try:
            checkout = library_client.heartbeat_reference_checkout(
                doc_id, holder=ident.actor)
        except library_client.ReferenceWritesNotEnabled:
            return _json(503, {"error": "document locking is not turned on for the Library yet"})
        adopted = True
        if checkout is None:
            # The heartbeat's 404 covers no live checkout, someone else's, and
            # dead past grace, and the Library does not say which. Nothing was
            # refreshed and no editor may open on the strength of a lock we
            # cannot show is held, so this is a refusal and never a 200. The
            # lock is left as found: it is either already gone or not ours to
            # release. The editor renders a 409 with no `checkout` as its error
            # note and hides the Edit button, hence the instruction to reload.
            return _json(409, {"error": "you already held this document's checkout, but it "
                                        "could not be refreshed; it may have just lapsed or "
                                        "been reclaimed. Reload the page and try Edit again"})
    except library_client.ReferenceWritesNotEnabled:
        return _json(503, {"error": "document locking is not turned on for the Library yet"})

    # The file is read under the Library lock rather than before it.
    #
    # **If the file cannot be read, the whole checkout fails and a lock just
    # taken is handed back.** The alternative, a 200 carrying a lock and no
    # content, opens an editor on nothing: the member types into a blank box and
    # learns it cannot be saved only when the save is refused. A lock the editor
    # cannot use is also one every other writer waits out for nothing.
    #
    # **An adopted lock is left held.** It was not taken here, and a successful
    # heartbeat proves it is live, so it may belong to another tab that is
    # editing right now; checking it in would make that tab's next heartbeat or
    # save answer 409. Adopting it again is idempotent, so keeping it after a
    # failed read costs this member nothing.
    #
    # **OFF THE EVENT LOOP.** `read_document` shells out to `agentctl kb
    # checkout`, which runs `git fetch origin main` on every call, under a 60s
    # timeout. This server runs a single uvicorn worker, so awaiting that here
    # would stall EVERY request to Orient for the duration, not just this
    # member's. `to_thread` propagates the exception to this frame, so the
    # mapping below is unchanged. Same rule as `server._warm_merge_queue_cache`
    # and `blog_api.publish_draft`; found by `security-reviewer` on PR #310.
    try:
        stored = await asyncio.to_thread(kb_commit.read_document, doc_id,
                                         actor=ident.actor)
    except Exception as e:
        # Every path that does not return `stored` gives back a lock taken
        # here: the two refusals below and anything unforeseen alike.
        if not adopted:
            _return_lock(doc_id, ident.actor)
        if isinstance(e, kb_commit.CommitLeaseConflict):
            return _json(409, {"error": "another session is writing this document right "
                                        "now; try again shortly", "detail": str(e)})
        if isinstance(e, kb_commit.CommitFailed):
            return _json(502, {"error": "could not read this document from the knowledge "
                                        "repository", "detail": str(e)})
        raise
    return _json(200, {"checkout": _checkout_dict(checkout),
                       "content": stored.content, "content_sha": stored.content_sha})


def _log_save_failure(request: Request, exc: kb_commit.CommitFailed) -> None:
    """Write why a save came back 502 to stderr, which `serve.sh` sends to
    serve.log, with the method and path of the request.

    The cause reached only the response body, which the editor shows as
    "Couldn't save" and nothing else reads, so a save that failed on a
    refused overwrite (2026-09-18) left serve.log with no new line.
    `api_common._log_unhandled` fixes the same gap for an exception; a
    `CommitFailed` is handled, so it never reaches that path.

    The line is `_exception_line`'s: the type and one bounded, control-
    character-free message, which for a `CommitFailed` is agentctl's stderr
    after `kb_commit._sanitize`. The path is caller-supplied, so it passes
    through `clean_log_field`. Logging must not change the response, so a
    failure here is swallowed."""
    try:
        print(f"kb_write_api: save failed in {clean_log_field(request.method)} "
              f"{clean_log_field(request.url.path)}\n{_exception_line(exc)}",
              file=sys.stderr, flush=True)
    except Exception:
        pass


def _return_lock(doc_id: str, actor: str) -> None:
    """Best-effort check-in of a Library lock this request just took. A failure
    here must not replace the outcome the caller is already returning, and the
    lock lapses on its own TTL if it is never sent."""
    try:
        library_client.checkin_reference_document(doc_id, holder=actor)
    except Exception:
        pass


@router.post("/kb/documents/{doc_id:path}/heartbeat")
@_api
async def heartbeat_document(doc_id: str, request: Request):
    """Keep the lock alive while the editor is open. A dropped heartbeat must
    be visible in the UI rather than silent (the Library's own docstring:
    a GUI editor that stops calling looks identical to one that died), so a
    404 here — the checkout lapsed, or was never this holder's — is reported
    plainly rather than retried silently."""
    ident, err = await _authorized(request)
    if err:
        return err
    doc, gate = await _visible_or_404(doc_id)
    if gate is not None:
        return gate
    try:
        checkout = library_client.heartbeat_reference_checkout(doc_id, holder=ident.actor)
    except library_client.ReferenceWritesNotEnabled:
        return _json(503, {"error": "document locking is not turned on for the Library yet"})
    if checkout is None:
        return _json(409, {"error": "the checkout lapsed or was reclaimed; reopen the document "
                                    "to edit it again"})
    return _json(200, {"checkout": _checkout_dict(checkout)})


@router.post("/kb/documents/{doc_id:path}/checkin")
@_api
async def checkin_document(doc_id: str, request: Request):
    """Hand the lock back — Close, or navigating away with no changes to
    save. Best-effort from the UI's side (a closed tab may never send this),
    which is exactly why the heartbeat above exists as the backstop."""
    ident, err = await _authorized(request)
    if err:
        return err
    doc, gate = await _visible_or_404(doc_id)
    if gate is not None:
        return gate
    try:
        library_client.checkin_reference_document(doc_id, holder=ident.actor)
    except library_client.ReferenceWritesNotEnabled:
        return _json(503, {"error": "document locking is not turned on for the Library yet"})
    return _json(200, {"released": True})


class FrontmatterAliasError(Exception):
    """The submitted frontmatter uses a YAML alias (`*name`), which includes
    every merge key that draws on an anchor (`<<: *base`). Raised by
    `_NoAliasLoader`, caught in `save_document` and turned into a 422 that
    says so, so a member who writes one learns why the save was refused rather
    than being told their frontmatter block is missing.

    It is not a `yaml.YAMLError`, so `_parse_frontmatter`'s handler for
    unparseable YAML cannot swallow it into the `None` outcome."""


class _NoAliasLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` that refuses an alias when the composer meets it.

    **This is what bounds the parse and the payload built from it.** An alias
    makes the composed tree share nodes, and every later consumer walks or
    serializes the shared node once per reference. Two amplifications reached
    the single uvicorn worker's event loop, both measured:

    - PyYAML's `flatten_mapping` copies a merged mapping's pairs into its
      parent, so `a_k: {<<: [*a_{k-1}, *a_{k-1}]}` doubles the pair list at
      every level, inside `yaml.safe_load` itself. 517 bytes took 0.6 s, 633
      bytes took 10 s and ~200 MB, and each two more levels multiplied both by
      four.
    - `topics` and `audience` accept any list of strings, and
      `library_client.check_reference_admission` JSON-serializes the payload,
      which writes a shared string once per reference. A 200 KB string with
      28,000 `- *s` entries is about 5.7 GB of JSON, inside a 400 KB request.

    With no alias there is no sharing, so merge-key doubling, fan-out, shared
    DAGs and self-referential anchors are all impossible, and the type checks
    in `_admission_frontmatter` walk exactly what the text spelled out.

    **The corpus holds no aliases.** All 822 `.md` files under
    `~/Convivy/knowledge/projects` were scanned on 2026-09-18 for `&anchor`,
    `*alias` and `<<:` inside their frontmatter block, and none has one, so
    refusing them breaks no real document. An anchor with no alias is inert
    and still parses. A merge key over an inline mapping (`<<: {a: 1}`) shares
    nothing and still parses too."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            raise FrontmatterAliasError(
                "frontmatter may not use YAML aliases (`*name`) or merge keys "
                "over an anchor (`<<: *name`); write each value out")
        return super().compose_node(parent, index)


def _parse_frontmatter(raw: str) -> dict | None:
    """The YAML frontmatter block a submitted body actually opens with,
    parsed from the text about to be committed — the same split the
    one-time `blog_ingest` import used, before that module was retired.

    Line endings are normalized to `\\n` before the delimiter check runs.
    The delimiters below are matched literally (three dashes and a newline
    to open, a newline, three dashes and a newline to close), so a body
    whose frontmatter uses CRLF line endings
    (nothing about the JSON body of this route requires `\\n` — only a
    browser `<textarea>` happens to normalize to it) would fail the check
    outright and be treated as having no frontmatter at all. That reopened
    the exact hole `_admission_frontmatter` was fixed to close earlier this
    round, by a different path: an unparsed body validates nothing about
    what it actually says (PR #210 round 3 review).

    Returns `None` — not `{}` — when no mapping could be found: no `---`
    opener, no closing delimiter, unparseable YAML, or YAML that parses to
    something other than a mapping. `None` is a real, distinct outcome from
    `{}`: a block that parses to an explicitly empty mapping (`---\\n{}\\n
    ---\\n`) is well-formed and returns `{}`, and the caller sends that `{}`
    to admission same as any other frontmatter. `None` means no mapping was
    found at all, and `save_document` fails closed on it (422) rather than
    passing an empty payload to the Library's admission route, which only
    checks a field when the payload contains it and so cannot itself
    distinguish a deliberately-empty submission from a body this repo
    failed to parse (PR #210 round 3 review).

    Raises `FrontmatterAliasError` when the block uses an alias. That is a
    third outcome and not `None`, so the caller can say why; see
    `_NoAliasLoader` for what it bounds."""
    raw = raw.replace("\r\n", "\n")
    if not raw.startswith("---\n"):
        return None
    end = raw.find("\n---\n", 4)
    if end == -1:
        return None
    try:
        parsed = yaml.load(raw[4:end], Loader=_NoAliasLoader)
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


class AdmissionShapeError(Exception):
    """A present field in the submitted frontmatter is not the shape admission
    expects. Raised by `_admission_frontmatter`, caught in `save_document` and
    turned into a 422 naming the field and the expected shape.

    `library_client.check_reference_admission` just JSON-serializes whatever
    `_admission_frontmatter` returns and POSTs it — no type checking happens
    anywhere on this path otherwise (security review, PR #210 round 4). A
    body with `audience: team` (a bare string, not a list) parsed straight
    through and would have been forwarded exactly as written; same for `id:
    12345` as an int. If the Library's admission check does list membership
    on `audience` (`"restricted" in declared_audience`), a string there
    silently swaps that for Python substring semantics — a type-confusion
    class, not a hypothetical one. This gets the same fail-closed treatment
    already given an unparseable body: refuse locally rather than forward a
    shape the remote validator's logic may not have been written to expect."""
    def __init__(self, field: str, expected: str):
        self.field = field
        self.expected = expected
        super().__init__(f"'{field}' must be {expected}")


# The two shapes admission's string-typed fields take. `last_reviewed` is the
# one forwarded field with a third rule: YAML reads an unquoted `2026-08-22` as
# a `datetime.date`, which `json.dumps` refuses, so it is checked on its own.
_ADMISSION_STRING_FIELDS = ("id", "kind", "project", "title", "status")
_ADMISSION_LIST_OF_STR_FIELDS = ("topics", "audience")


def _admission_frontmatter(fm: dict) -> dict:
    """The frontmatter object `POST /reference/admission` checks, built from
    `fm` — the frontmatter ACTUALLY parsed out of the body about to be
    committed (`_parse_frontmatter(new_body)`, already confirmed non-`None`
    by the caller), never from the pre-edit document.

    **This used to be built from the stale, pre-edit `ReferenceDocument`
    instead, and that was a live privilege-escalation path** (security
    review, PR #210): a stored document's own frontmatter always already
    validates, so checking it again told admission nothing about what a
    human was about to write. A save whose submitted body changed `audience`,
    `project` or `id` sailed through, because nothing here ever looked at the
    submitted body's frontmatter at all. Admission must see what is about to
    land, not what already did.

    **Still a PARTIAL frontmatter.** `visibility`, `applies_to`, `depends_on`,
    `authors` and `tools` are not covered by the fields the editor round-trips
    through this object, and admission's own validator only checks a field
    when the payload contains it — a partial object validates correctly for
    what is missing, which is the Library admission contract, not a gap this
    function can close. The gap that made the STALE-SOURCE version of this
    function safe to leave alone — `kb_commit.py` unconditionally raising
    `CommitNotConfigured` — no longer holds; this function is now checked
    against the real write on every save.

    **Only present keys are forwarded, and that filtering is unchanged** — a
    key the submission omits stays omitted, matching the Library's contract
    that a missing field validates as missing rather than as some default.
    What's new is a type check on a key that IS present: `raises
    AdmissionShapeError` rather than coercing, since guessing a caller meant
    `["team"]` when they sent `"team"` is exactly the kind of guess this
    function should not make on a field that decides who may read a
    document. The one coercion is `last_reviewed`'s date to an ISO string, in
    the returned payload only.

    **`last_reviewed` is a `date` or a `str` and nothing else.** The corpus
    holds no other shape (822 frontmatter blocks, 2026-09-18), so a container
    is refused rather than admitted. A datetime is a `date`, and the ISO string
    keeps its time. The committed bytes are `new_body` exactly as submitted,
    and a document opened and saved unchanged must come back byte-identical,
    so nothing here is written back into `fm`.

    **`_NoAliasLoader` bounds `fm`; this function does not.** `topics` and
    `audience` accept a list of any length, and checking each element as a
    string is linear in the list without limiting what it serializes to. The
    payload stays proportional to the request because the parser refuses
    aliases: every list element and string in `fm` is text the submission
    spelled out, and `read_json_body` caps the submission."""
    out = {k: fm[k] for k in
           ("id", "kind", "project", "title", "status", "topics",
            "audience", "last_reviewed") if k in fm}
    for field in _ADMISSION_STRING_FIELDS:
        if field in out and not isinstance(out[field], str):
            raise AdmissionShapeError(field, "a string")
    for field in _ADMISSION_LIST_OF_STR_FIELDS:
        if field in out:
            value = out[field]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise AdmissionShapeError(field, "a list of strings")
    if "last_reviewed" in out:
        value = out["last_reviewed"]
        if isinstance(value, dt.date):
            out["last_reviewed"] = value.isoformat()
        elif not isinstance(value, str):
            raise AdmissionShapeError("last_reviewed", "a date or a string")
    return out


def _access_enforced() -> bool:
    """Whether Access runs in enforce mode, read per call the way `jwt_config`
    is everywhere else. A separate function so the save tests, which run in
    monitor mode, can exercise the save path while the refusal keeps tests of
    its own."""
    return jwt_config()["enforce"]


@router.post("/kb/documents/{doc_id:path}/save")
@_api
async def save_document(doc_id: str, request: Request):
    """Admission, then commit — see `kb_commit.py` for what the commit step
    does. The checkout is left held on every failure branch below: nothing
    was written, so there is nothing to release, and a human mid-edit keeps
    their lock rather than losing it to a failed save."""
    ident, err = await _authorized(request)
    if err:
        return err
    # **NO PERSON COMMIT UNLESS ACCESS IS ENFORCED.** A save commits under
    # `person` provenance, which agentctl attributes to the email in the
    # Access assertion it verifies. In monitor mode (`jwt.json` enforce=false)
    # this route authorized the plaintext email header instead, and the
    # assertion may be absent or belong to someone else, so the lease holder
    # and the commit's person could name two different people with nothing to
    # report it. Refused before any lock is taken (person-provenance contract,
    # point 6).
    if not _access_enforced():
        return _json(503, {"error": "saving is off while Cloudflare Access verification "
                                    "is in monitor mode; nothing was changed"})
    doc, gate = await _visible_or_404(doc_id)
    if gate is not None:
        return gate
    body, err = await read_json_body(request, MAX_BODY + 8192)
    if err:
        return err
    new_body = str(body.get("body", ""))
    if not new_body.strip():
        return _json(400, {"error": "refusing to save an empty document"})
    # The revision the editor opened at, posted back from the checkout
    # response. It becomes `kb apply --base-sha`, the stale-editor guard, and
    # it is CLIENT-SUPPLIED and reaches a subprocess argv, so anything but a
    # plain 64-digit hex digest is refused here and never passed on. Required:
    # a save with no open-time sha would fall back to a sha read at save time,
    # which cannot see a commit that landed while the member was typing.
    base_sha = body.get("content_sha")
    if not kb_commit.is_content_sha(base_sha):
        return _json(400, {"error": "this save does not say which version of the document "
                                    "you opened; reopen the document and try again"})

    held = library_client.get_reference_checkout(doc_id)
    if held is None or held.holder != ident.actor:
        return _json(409, {"error": "you do not hold this document's checkout; "
                                    "reopen it to edit"})

    # `doc` (from `_visible_or_404` above) is used only for the visibility
    # gate now — the admission payload comes from `new_body`, the content
    # actually about to be committed, not from this pre-edit row (PR #210
    # security review: checking the stale row's already-valid frontmatter
    # validated nothing about what the human submitted).
    try:
        fm = _parse_frontmatter(new_body)
    except FrontmatterAliasError as e:
        return _json(422, {"error": str(e)})
    if fm is None:
        # No mapping could be found in the submitted body at all — fail
        # closed here rather than send an empty admission payload and trust
        # a remote validator (whose contract is "check a field only when
        # given one") to reject what this repo could not itself confirm was
        # safe (PR #210 round 3 review).
        return _json(422, {"error": "could not find a frontmatter block to validate; "
                                    "the document must open with a `---`-delimited "
                                    "YAML block"})
    try:
        admission_fm = _admission_frontmatter(fm)
    except AdmissionShapeError as e:
        return _json(422, {"error": f"frontmatter field {e.field!r} must be {e.expected}"})
    # **THE SUBMITTED `id` MUST BE THE DOCUMENT BEING SAVED.** The Library's
    # checkout clause looks the checkout up by `frontmatter["id"]`, while the
    # commit below writes `doc_id`. The frontmatter is free text in the editor,
    # so without this a member holding checkouts on two documents could type
    # the second's id into the first: the clause would verify the second and
    # the content would land at the first (PR #326 review). An ABSENT id is
    # left to the Library, which lists `id` among its required fields and
    # refuses it there; an earlier review settled that an empty mapping is the
    # Library's to judge (`test_save_forwards_an_explicitly_empty_frontmatter_…`).
    if "id" in admission_fm and admission_fm["id"] != doc_id:
        return _json(422, {"error": "the frontmatter `id` must be this document's id, "
                                    f"{doc_id!r}; a document's id is not changed by "
                                    "editing it"})
    try:
        # `owner` is the label the checkout was taken under, which the
        # `held.holder != ident.actor` check above has just confirmed.
        admission = library_client.check_reference_admission(
            admission_fm, owner=ident.actor)
    except library_client.ReferenceWritesNotEnabled:
        # Admission is a pure validation route with no `connect_rw` behind it
        # (`reference_admission.py` never opens a writable handle), so this
        # should not be reachable in practice — kept for the same reason
        # every other Library call here handles it: a caller must not crash
        # on an outcome the module already has a name for.
        return _json(503, {"error": "document locking is not turned on for the Library yet"})
    if not admission.may_land:
        return _json(422, {"error": "frontmatter does not validate", "errors": admission.errors})

    # **OFF THE EVENT LOOP**, for the reason given in `checkout_document`:
    # `commit_document` runs three sequential agentctl calls (checkout, apply,
    # release), each under a 60s timeout, on a single uvicorn worker.
    try:
        # The raw assertion, read off the request itself rather than carried on
        # `authz.Identity`, which nine routers share and none of the others
        # need. agentctl verifies it again and records the email it verified.
        await asyncio.to_thread(kb_commit.commit_document, doc_id, body=new_body,
                                actor=ident.actor, base_sha=base_sha,
                                assertion=request.headers.get(JWT_HEADER) or "")
    except kb_commit.CommitStale as e:
        return _json(409, {"error": "the document changed on origin/main since you opened "
                                    "it; reopen it to read the current version, then reapply "
                                    "your edit and save again",
                           "detail": str(e)})
    except kb_commit.CommitLeaseConflict as e:
        return _json(409, {"error": "another session is writing this document right now",
                           "detail": str(e)})
    except kb_commit.CommitRejected as e:
        return _json(422, {"error": "the write was rejected", "detail": str(e)})
    except kb_commit.CommitAuthExpired as e:
        return _json(401, {"error": "your Access session expired, reload "
                                    "team.convivy.com and save again",
                           "detail": str(e)})
    except kb_commit.CommitFailed as e:
        _log_save_failure(request, e)
        return _json(502, {"error": "saving to the knowledge repository failed",
                           "detail": str(e)})

    # This call had no `except ReferenceWritesNotEnabled` in an earlier
    # version, unlike every other write call in this file. A 503 there would
    # have fallen through to api_guard's catch-all as a generic 500, and the
    # editor's `else` branch says "Couldn't save" — telling a human their edit
    # was LOST at the exact moment it had in fact been committed. That is the
    # inverse of what this branch exists to prevent, and it was invisible
    # because nothing exercised the post-commit path while commit always
    # raised (PR #207 review).
    #
    # **The commit has already landed by this point. Nothing below may report
    # failure to save.**
    try:
        released = library_client.checkin_reference_document(doc_id, holder=ident.actor)
    except library_client.ReferenceWritesNotEnabled:
        released = None
    except Exception:
        released = None
    if released is None:
        # The commit landed and the checkin did not — the one partial failure
        # the spec calls out by name as the one that strands a document. The
        # save DID succeed; say so, and say the document is still locked.
        return _json(200, {"saved": True, "checked_in": False,
                           "warning": "saved, but the checkout could not be released — "
                                      "the document is still locked under your session"})
    return _json(200, {"saved": True, "checked_in": True})
