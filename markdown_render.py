#!/usr/bin/env python3.11
"""One safe Markdown renderer, shared by every builder that shows authored text.

The changelog and release-notes builders each carried a byte-identical private
`_md_to_html`, and the issues page was about to become a third copy. Three
copies of a SECURITY-relevant renderer is how one of them quietly drifts: the
`html=False` flag and the `target="_blank"` rewrite are the whole XSS story, and
a copy that loses either is indistinguishable from the others at a glance.

Safety, stated once:
  * `html=False` escapes raw HTML in the source. Every body rendered through
    here is untrusted — a PR description, a GitHub issue body, an agent's review
    finding — so raw HTML must never pass through.
  * markdown-it's default link validator drops `javascript:`/`vbscript:`/`data:`
    schemes, but it is a blocklist, and a blocklist only ever catches what it
    already knows to name — an http(s) URL with an empty netloc
    (`href="http://"`) and a protocol-relative destination (`//evil.example`)
    both slipped through it in earlier rounds of this fix, and a third leading
    slash (`///evil.example`) slipped through the first attempt at closing
    the second. `_is_safe_destination` below replaces "reject what's
    recognized as bad, allow everything else" with an allowlist: a
    destination is emitted as a live link only if it affirmatively matches
    one of a small enumerated set of safe classes —
      - an http(s) URL that passes `url_safety.safe_http_url`, the same check
        every other renderer on the site runs before it emits a URL it did
        not itself produce
      - `mailto:` with a recipient
      - `data:image/{gif,png,jpeg,webp};...` — markdown-it's own default
        validator's exact carve-out (it excludes `svg+xml`, since an SVG
        payload can carry a `<script>`), matched here rather than left to
        fall out of `_default_validate_link` running first, so a pasted
        screenshot keeps rendering under this allowlist too
      - a same-origin reference with at most one leading slash (`/path`,
        `relative.md`, `#anchor`, `?query`) — never two or more, because a
        browser's URL Standard parser treats any run of 2+ leading slashes on
        an http(s) page as introducing a new authority, resolving
        `///evil.example` to `https://evil.example/` exactly like
        `//evil.example` does. Python's `urlparse`/`urljoin` (RFC 3986) both
        disagree with that, confirmed empirically: they put the extra
        slashes in `.path`, not `.netloc`, and resolve `///evil.example`
        against a same-origin base as a same-origin path. So the slash count
        is checked on the raw string, never through either.
    everything unenumerated — an unrecognized scheme, a bare `//host` or
    `///host` or deeper — is rejected, whether or not markdown-it's own
    blocklist already knew to reject it.
  * `linkify=False` keeps bare URLs as text; only explicit Markdown links become
    anchors, which is what stops a pasted tracking URL from silently becoming
    clickable.

A destination that fails `validateLink` is not rewritten into a plain link the
way the other call sites of `safe_http_url` do it (an escaped label instead of
an anchor): markdown-it has no hook for "render this as text instead," so a
rejected inline link falls back to markdown-it's own existing behavior for any
other invalid destination — the `[label](url)` source renders as literal
escaped text.

A rejected reference-style definition (`[label]: <bad-url>`) is not that
fallback: markdown-it's block `reference` rule calls `validateLink` and bails
out with `return False` the moment it fails, before the line is ever recorded
anywhere — it is not "recovered as a plain paragraph," it simply is not
consumed as a reference, so the line falls through to ordinary paragraph
parsing and inherits CommonMark's lazy continuation. A blank line after it
leaves it standing alone and it reads the same either way; with no blank line,
it silently swallows the following line — including a *safe*, otherwise-valid
reference definition, which then never registers and orphans whatever
elsewhere links to it. `_reference_with_recovery` below closes that: it runs
the real `reference` rule with `validateLink` temporarily forced to accept, so
the line is consumed as its own block, then moves what it just wrote out of
`env["references"]` (which would otherwise make the rejected URL resolvable to
`[text][label]` links elsewhere in the document) into `env["unsafe_refs"]`,
where `_recovered_reference_definitions` picks it up as its own isolated
paragraph, immune to the merge, the same way an unrelated duplicate label
already is.
"""
from __future__ import annotations

import re
import threading
from html import escape
from urllib.parse import urlparse

from markdown_it import MarkdownIt
from markdown_it.rules_block.reference import reference as _original_reference_rule

from url_safety import safe_http_url

_MD = MarkdownIt("commonmark", {"html": False, "linkify": False})

# markdown-it's own default validator's exact data-URI carve-out
# (markdown_it/common/normalize_url.py:GOOD_DATA_RE) — matched here rather than
# relied on implicitly, since `_is_safe_destination` is the boundary this
# module claims sole ownership of. `svg+xml` is deliberately absent: it is
# script-bearing and neither markdown-it nor this allowlist accepts it.
_GOOD_DATA_RE = re.compile(r"^data:image/(gif|png|jpeg|webp);")

_default_validate_link = _MD.validateLink

# Set only for the duration of `_reference_with_recovery`'s call into the real
# `reference` rule below, and only on the thread making that call — `_MD` is a
# module-level singleton and FastAPI can run sync request handlers on a thread
# pool, so a shared (non-thread-local) flag would let one request's rejected
# destination flip validation for another request's concurrent render.
_recovery = threading.local()


def _is_safe_destination(url: str) -> bool:
    """True only for a destination affirmatively recognized as one of the
    classes the site must keep rendering as a live link. Everything not
    named here is rejected, including a scheme neither this function nor
    markdown-it's own blocklist has a name for.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        # urlparse raises on an unbalanced IPv6-literal bracket ("http://[::1")
        # instead of failing the scheme check below. Not reachable through
        # md_to_html today — markdown-it's normalizeLink percent-encodes the
        # brackets first — but this function is called directly by tests and
        # is the boundary this module claims sole ownership of, so it must not
        # rely on that upstream behavior to avoid the crash (url_safety.py's
        # identical guard, PR #269).
        return False
    if parsed.scheme in {"http", "https"}:
        return safe_http_url(url) is not None
    if parsed.scheme == "mailto":
        return bool(parsed.path)
    if parsed.scheme == "data":
        return bool(_GOOD_DATA_RE.search(url))
    if parsed.scheme:
        return False
    # No scheme: `/path`, `relative.md`, `#anchor`, `?query` are all safe,
    # same-origin references. 2+ leading slashes is not — it's a network-path
    # reference to an attacker-chosen origin — checked on the raw string
    # because urlparse puts a third-or-later slash in `.path` rather than
    # `.netloc`; see the module docstring.
    return not url.startswith("//")


def _validate_link(url: str) -> bool:
    if _default_validate_link(url) and _is_safe_destination(url):
        return True
    pending = getattr(_recovery, "pending_rejects", None)
    if pending is not None:
        pending.append(url)
        return True  # let the reference rule consume the line; see below
    return False


_MD.validateLink = _validate_link


def _reference_with_recovery(state, startLine, endLine, silent):
    """Wraps markdown-it's block `reference` rule so a definition whose
    destination fails `_validate_link` is still consumed as its own block
    (rather than falling through to paragraph lazy continuation) and is
    recovered as an isolated paragraph rather than made resolvable.

    Delegates untouched for `silent` probes (used by other rules to check
    whether a following line would terminate them without consuming it) since
    those never write to `env` and forcing validation open there would only
    risk misclassifying an unrelated line's terminator check.
    """
    if silent:
        return _original_reference_rule(state, startLine, endLine, silent)

    refs_before = dict(state.env.get("references", {}))
    rejected: list[str] = []
    _recovery.pending_rejects = rejected
    try:
        consumed = _original_reference_rule(state, startLine, endLine, silent)
    finally:
        _recovery.pending_rejects = None

    if consumed and rejected:
        refs = state.env.get("references", {})
        new_label = next(
            (label for label, ref in refs.items() if refs_before.get(label) is not ref),
            None,
        )
        if new_label is not None:
            state.env.setdefault("unsafe_refs", []).append(refs.pop(new_label))
        # else: a duplicate label — already recovered via `duplicate_refs`.

    return consumed


_MD.block.ruler.at("reference", _reference_with_recovery)


def md_to_html(text: str) -> str:
    """Render Markdown to safe HTML. Links open in a new tab, like every other
    outbound link on the site."""
    text = text or ""
    env: dict = {}
    html = _MD.renderer.render(_MD.parse(text, env), _MD.options, env)
    html += _recovered_reference_definitions(text, env)
    return html.replace("<a ", '<a target="_blank" rel="noopener" ')


def _recovered_reference_definitions(text: str, env: dict) -> str:
    """CommonMark parses a standalone `[label]: url` line as a link reference
    definition and drops it from the output, even when nothing ever links to
    it — a document can lose a line with no trace on the page. markdown-it
    records every consumed definition (including a duplicate label's second
    definition, dropped the same way, and — via `_reference_with_recovery`
    above — one whose destination failed `_validate_link`) in `env`, keyed by
    the source line range it consumed. Re-emit that exact source text,
    escaped, as a plain paragraph so the line always survives into the
    rendered output."""
    consumed = (
        list(env.get("references", {}).values())
        + env.get("duplicate_refs", [])
        + env.get("unsafe_refs", [])
    )
    if not consumed:
        return ""
    lines = text.split("\n")
    consumed.sort(key=lambda ref: ref["map"][0])
    parts = []
    for ref in consumed:
        start, end = ref["map"]
        raw = "\n".join(lines[start:end])
        parts.append(f"<p>{escape(raw)}</p>\n")
    return "".join(parts)
