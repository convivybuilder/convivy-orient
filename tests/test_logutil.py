"""`clean_log_field` contract — the one control-character rule shared by every
writer of a delimited log line built from caller-influenced text
(`issue_01M0VBZRCA15QSVYFMEF8MZ8N0`)."""
from __future__ import annotations

from logutil import clean_log_field


def test_newline_is_replaced():
    assert clean_log_field("before\nafter") == "before after"


def test_tab_is_replaced():
    assert clean_log_field("before\tafter") == "before after"


def test_del_is_replaced():
    assert clean_log_field("before\x7fafter") == "before after"


def test_c1_control_is_replaced():
    # 0x85 (NEL) is a C1 control a terminal can act on; C0 alone misses it.
    assert clean_log_field("before\x85after") == "before after"


def test_top_of_c1_range_is_replaced():
    # 0x9F is the top of the C1 range (0x80-0x9F); kb_commit's old, divergent
    # sanitizer only special-cased 0x85 and let the rest of C1 through
    # (issue_01M1KJ4VCR127XJC9SMKPRVGRG).
    assert clean_log_field("before\x9fafter") == "before after"


def test_unicode_line_separator_is_replaced():
    # str.splitlines() treats U+2028 as a line boundary, same as "\n" — a
    # value carrying it could forge an extra line in a line-oriented log
    # (issue_01M1KJ4VCR127XJC9SMKPRVGRG).
    assert clean_log_field("before after") == "before after"


def test_unicode_paragraph_separator_is_replaced():
    assert clean_log_field("before after") == "before after"


def test_bidi_override_is_replaced():
    # U+202E (RIGHT-TO-LEFT OVERRIDE) is Cf, not printable content; letting it
    # through flips the rendered direction of everything after it for a human
    # reading the log — the Trojan Source technique (CVE-2021-42574) pointed
    # at a log reader instead of a code reviewer (issue_01M1KJ4VCR127XJC9SMKPRVGRG).
    assert clean_log_field("before‮after") == "before after"


def test_bidi_isolate_is_replaced():
    # U+2066 (LEFT-TO-RIGHT ISOLATE) is one of the isolate controls
    # (U+2066-U+2069) — a second bidi-control family beyond the override pair,
    # also Cf and also unreplaced before this widening.
    assert clean_log_field("before⁦after") == "before after"


def test_plain_text_passes_through_unchanged():
    value = "GET /issues/issue_01ABC?tier=full — héllo, world! 42%"
    assert clean_log_field(value) == value
