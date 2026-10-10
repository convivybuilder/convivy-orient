#!/usr/bin/env python3.11
"""The one log-field sanitiser, shared by every writer of a delimited log
line built from caller-influenced text — exception text and URL path
segments among them, since a malformed id can shape a Library error message.
An embedded newline in such a field forges a new log line; an embedded tab
shifts a tab-delimited line's column boundaries (security review, PR #180).

This started as two character-for-character copies — `server.py._log_field`
and `chart_house_build.py`'s `_log_swallowed._clean` — kept separate because
`server.py` imports `chart_house_build`, so importing the other direction
would be circular. A leaf module removes the cycle and gives the rule one
place to drift from instead of two (`issue_01M0VBZRCA15QSVYFMEF8MZ8N0`).
"""
from __future__ import annotations


_BIDI_CONTROLS = frozenset(
    {0x200E, 0x200F, 0x061C} | set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
)


def clean_log_field(value: str) -> str:
    """`value` with every control character replaced by a space, so a
    tab-delimited log line stays on one line and keeps its columns, and so a
    human reading the line sees it rendered in its real direction.

    Replaced rather than dropped, so the field stays readable and a caller
    can't erase evidence of what it sent by embedding the delimiter.

    Covers C0 (0x00-0x1F), DEL (0x7F), C1 (0x80-0x9F), the Unicode LINE
    SEPARATOR / PARAGRAPH SEPARATOR (U+2028, U+2029), and the bidirectional
    embedding/override/isolate controls (U+202A-U+202E, U+2066-U+2069,
    U+200E, U+200F, U+061C). The first group is either a control range a
    terminal reading the log can act on, or a character `str.splitlines()`
    and other line-oriented consumers treat as a line boundary the way they
    treat `\\n` — either kind can forge a new log line through a value this
    function was not covering. The bidi group is a second, distinct harm:
    each is Unicode category Cf (a formatting control, not printable
    content), and a Right-to-Left Override or isolate can flip the rendered
    direction of everything after it in the line, the Trojan Source technique
    (CVE-2021-42574) pointed at whoever reads the log instead of at a code
    reviewer. Reachability is verified, not assumed: `server.py`'s
    `_log_render_failure` builds `page` from a caller-supplied URL path
    segment (e.g. `f"reference/doc/{doc_id}"`) and hands it to `_log_field`,
    so a percent-encoded U+202E in a path decodes and, before this widening,
    passed straight through into the RENDER-FAILED line unchanged
    (`issue_01M1KJ4VCR127XJC9SMKPRVGRG`). Ordinary printable text, including
    non-ASCII, passes through unchanged — this rejects control characters,
    not content.
    """
    return "".join(
        " "
        if ord(c) < 0x20
        or 0x7F <= ord(c) <= 0x9F
        or ord(c) in (0x2028, 0x2029)
        or ord(c) in _BIDI_CONTROLS
        else c
        for c in value
    )
