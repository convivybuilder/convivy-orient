"""Tests for markdown_render.py — the shared safe Markdown renderer.

Invariants:
- A standalone link-reference-definition line survives into the output as
  visible, escaped text (issue_01M138FKHVZCYSS0FW490VERY2): CommonMark treats
  it as a definition and drops it from the rendered HTML entirely, with no
  trace of the line remaining.
- A reference that IS used elsewhere (`[text][Doc]`) still renders as a
  working link.
- A document with no reference definitions renders byte-identical to the
  pre-fix output.
- HTML-special characters in a recovered definition line come out escaped,
  never as raw HTML.
- An inline or reference-style link destination is routed through
  `url_safety.safe_http_url` the same as every other renderer on the site
  (issue_01M1KFM498C1PKQD2NV0W5KRDM): an http(s) URL with an empty netloc
  (`http://`) must not become a live anchor.
- A protocol-relative destination (`//evil.example`) is rejected the same way,
  as an inline link, an image, and a reference definition: it has no scheme
  for the `http`/`https` check to catch, but it is a fully qualified
  cross-origin reference, not a same-site relative link.
- The same rejection holds at three and four leading slashes
  (`///evil.example`, `////evil.example`): a browser's URL Standard parser
  treats any run of 2+ leading slashes on http(s) as introducing a new
  authority, but `urlparse` puts the third-and-later slash in `.path`, not
  `.netloc`, so a netloc-based check alone misses these.
- A rejected reference-style definition is routed through the same recovery
  path regardless of which check rejected it — `_is_safe_destination`'s
  allowlist or markdown-it's own scheme blocklist (`javascript:`, etc.) —
  so neither one can swallow a following safe reference via lazy
  continuation.
- A rejected reference definition is consumed as its own block, so it cannot
  swallow the following line via CommonMark lazy continuation — a safe
  reference on the very next line (no blank line between them) must still
  register and resolve.
- An uppercase scheme, an angle-bracket destination, and a bare autolink all
  route through the same `validateLink` hook as the plain `[x](url)` shape.
- A relative link, `#anchor`, and `mailto:` destination — none of which
  `safe_http_url` has an opinion on — still render as live anchors.
- A raw backslash in a link destination never reaches `href` unescaped:
  CommonMark's own backslash-escaping plus markdown-it's `normalizeLink`
  percent-encode it to `%5C` before `_validate_link` ever runs. This is a
  property of the dependency, pinned here rather than guarded in this
  module's code.
- The reference-rule wrapper's thread-local `pending_rejects` is cleared by
  a `finally`, so an exception raised inside the wrapped rule still leaves
  the thread usable for the next render.
- A label defined twice, both times with an unsafe destination, resolves to
  neither definition and is recovered exactly once per line — no double
  emission.
- Each of the four data: image types `_GOOD_DATA_RE` allows (gif, png, jpeg,
  webp) renders as a live `<img src>` (issue_01M1KFM498C1PKQD2NV0W5KRDM,
  regression coverage for the "four image types silently stopped rendering"
  bug fixed in this file).
- `data:image/svg+xml` stays inert — it is script-bearing, and it is the
  reason the allowed set is four named types rather than `data:image/*`.
- `data:text/html` and `data:text/plain` stay inert.
- A subtype that merely starts with an allowed name — `image/pngX`,
  `image/pngjpeg` — stays inert: `_GOOD_DATA_RE` anchors on `;` after the
  type, not just a prefix match.
- `_is_safe_destination` itself rejects svg+xml and the prefix-match
  subtypes, checked directly rather than only through `md_to_html`:
  markdown-it's own default validator carries an identical
  `(gif|png|jpeg|webp)` carve-out and independently rejects these same
  destinations before `_is_safe_destination` is ever reached, so a
  regression in `_GOOD_DATA_RE` alone would not surface through the full
  render pipeline.
"""
from unittest import mock

import markdown_render as mr


def test_standalone_reference_definition_survives_into_output():
    out = mr.md_to_html(
        "Checklist:\n\n[Doc]: https://docs.example.com/spec\n\nMore text after."
    )
    assert "[Doc]: https://docs.example.com/spec" in out
    assert "<p>Checklist:</p>" in out
    assert "<p>More text after.</p>" in out


def test_used_reference_link_still_renders_as_a_link():
    out = mr.md_to_html(
        "See the [spec][Doc] for details.\n\n[Doc]: https://docs.example.com/spec"
    )
    assert (
        '<a target="_blank" rel="noopener" href="https://docs.example.com/spec">spec</a>'
        in out
    )


def test_document_with_no_reference_definitions_is_unchanged():
    text = "# Title\n\nA plain paragraph with a [real link](https://example.com/x)."
    out = mr.md_to_html(text)
    assert out == (
        '<h1>Title</h1>\n<p>A plain paragraph with a '
        '<a target="_blank" rel="noopener" href="https://example.com/x">real link</a>.</p>\n'
    )


def test_recovered_definition_line_is_html_escaped():
    out = mr.md_to_html(
        'Before.\n\n[<script>]: https://evil.example.com/"x\n\nAfter.'
    )
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_empty_netloc_http_link_does_not_become_a_live_anchor():
    out = mr.md_to_html("[x](http://)")
    assert "<a " not in out
    assert "href=" not in out
    assert "[x](http://)" in out


def test_empty_netloc_reference_style_link_does_not_become_a_live_anchor():
    out = mr.md_to_html("[x][1]\n\n[1]: http://")
    assert "<a " not in out
    assert "href=" not in out


def test_normal_https_link_still_renders_as_a_live_anchor():
    out = mr.md_to_html("[x](https://example.com)")
    assert (
        '<a target="_blank" rel="noopener" href="https://example.com">x</a>' in out
    )


def test_relative_anchor_and_mailto_links_still_render():
    out = mr.md_to_html(
        "[rel](/path) [frag](#section) [mail](mailto:a@b.example)"
    )
    assert '<a target="_blank" rel="noopener" href="/path">rel</a>' in out
    assert '<a target="_blank" rel="noopener" href="#section">frag</a>' in out
    assert (
        '<a target="_blank" rel="noopener" href="mailto:a@b.example">mail</a>' in out
    )


def test_protocol_relative_inline_link_does_not_become_a_live_anchor():
    out = mr.md_to_html("[x](//evil.example)")
    assert "<a " not in out
    assert "href=" not in out
    assert "[x](//evil.example)" in out


def test_protocol_relative_image_does_not_become_a_live_source():
    out = mr.md_to_html("![x](//evil.example)")
    assert "<img" not in out
    assert "src=" not in out
    assert "![x](//evil.example)" in out


def test_protocol_relative_reference_definition_does_not_become_a_live_anchor():
    out = mr.md_to_html("[x][r]\n\n[r]: //evil.example")
    assert "<a " not in out
    assert "href=" not in out
    assert "[r]: //evil.example" in out


def test_rejected_reference_does_not_swallow_the_next_line_via_lazy_continuation():
    out = mr.md_to_html(
        "[1]: http://\n[2]: http://good.example\n\nSee [here][2] for more."
    )
    assert (
        '<a target="_blank" rel="noopener" href="http://good.example">here</a>'
        in out
    )
    assert "[1]: http://" in out
    assert "[2]: http://good.example" in out


def test_uppercase_scheme_link_is_still_validated():
    assert "<a " not in mr.md_to_html("[x](HTTP://)")
    out = mr.md_to_html("[x](HTTP://ok.example)")
    assert '<a target="_blank" rel="noopener" href="HTTP://ok.example">x</a>' in out


def test_angle_bracket_destination_is_still_validated():
    assert "<a " not in mr.md_to_html("[x](<http://>)")
    out = mr.md_to_html("[x](<http://ok.example>)")
    assert (
        '<a target="_blank" rel="noopener" href="http://ok.example">x</a>' in out
    )


def test_bare_autolink_is_still_validated():
    out = mr.md_to_html("<http://ok.example>")
    assert (
        '<a target="_blank" rel="noopener" href="http://ok.example">http://ok.example</a>'
        in out
    )
    assert "<a " not in mr.md_to_html("See <http://> here")


def test_triple_slash_inline_link_does_not_become_a_live_anchor():
    out = mr.md_to_html("[x](///evil.example)")
    assert "<a " not in out
    assert "href=" not in out
    assert "[x](///evil.example)" in out


def test_quadruple_slash_inline_link_does_not_become_a_live_anchor():
    out = mr.md_to_html("[x](////evil.example)")
    assert "<a " not in out
    assert "href=" not in out
    assert "[x](////evil.example)" in out


def test_triple_slash_image_does_not_become_a_live_source():
    out = mr.md_to_html("![x](///evil.example/p.png)")
    assert "<img" not in out
    assert "src=" not in out
    assert "![x](///evil.example/p.png)" in out


def test_triple_slash_reference_definition_does_not_become_a_live_anchor():
    out = mr.md_to_html("[x][r]\n\n[r]: ///evil.example")
    assert "<a " not in out
    assert "href=" not in out
    assert "[r]: ///evil.example" in out


def test_backslash_destination_never_reaches_href_unescaped():
    # A property of markdown-it's normalizeLink + CommonMark's own backslash
    # escaping, not of this module's code — see the module docstring for why
    # no guard for this lives in `_is_safe_destination`.
    out = mr.md_to_html("[x](\\evil.example)")
    assert "\\evil.example" not in out
    assert 'href="%5Cevil.example"' in out


def test_reference_rejected_by_markdown_its_own_blocklist_does_not_swallow_the_next_line():
    out = mr.md_to_html(
        "[ref]: javascript:alert(1)\n[ref2]: /safe\n\n[a][ref2]"
    )
    assert (
        '<a target="_blank" rel="noopener" href="/safe">a</a>' in out
    )
    assert "[ref]: javascript:alert(1)" in out
    assert "[ref2]: /safe" in out


def test_duplicate_label_where_both_definitions_are_unsafe_does_not_swallow_the_next_line():
    # A substring count can't tell two separate recovered `<p>` tags apart
    # from one merged `<p>` holding both lines, so it would pass even if the
    # recovery wrapper regressed to pre-recovery behavior (lazy continuation
    # merging the rejected lines with whatever follows). Placing a legitimate
    # reference immediately after, with no blank line, and requiring it to
    # still resolve is what actually pins "consumed as its own block."
    out = mr.md_to_html(
        "[a][dup]\n\n[dup]: http://\n[dup]: https://\n"
        "[good]: https://good.example\n\nSee [here][good] for more."
    )
    assert (
        '<a target="_blank" rel="noopener" href="https://good.example">here</a>'
        in out
    )
    assert "<a " not in out.split("See ")[0]
    assert "[dup]: http://" in out
    assert "[dup]: https://" in out
    assert "[a][dup]" in out


def test_recovery_thread_local_is_cleared_after_an_exception():
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    # The block parser tries the reference rule against every line, so the
    # patch must come off again before the next render — not just at test
    # teardown — or that render would raise too.
    with mock.patch.object(mr, "_original_reference_rule", _boom):
        try:
            mr.md_to_html("[1]: https://example.com")
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the patched reference rule to raise")

    assert getattr(mr._recovery, "pending_rejects", None) is None

    out = mr.md_to_html("[x](https://example.com)")
    assert (
        '<a target="_blank" rel="noopener" href="https://example.com">x</a>'
        in out
    )


def test_each_permitted_data_image_type_renders_as_a_live_source():
    for subtype in ("gif", "png", "jpeg", "webp"):
        out = mr.md_to_html(f"![x](data:image/{subtype};base64,AAAA)")
        assert f'<img src="data:image/{subtype};base64,AAAA" alt="x" />' in out


def test_data_image_svg_xml_does_not_become_a_live_source():
    # The reason the allowlist names four raster types instead of
    # `data:image/*`: SVG can carry a <script>.
    out = mr.md_to_html("![x](data:image/svg+xml;base64,AAAA)")
    assert "<img" not in out
    assert "src=" not in out
    assert "![x](data:image/svg+xml;base64,AAAA)" in out


def test_data_text_html_and_text_plain_do_not_become_live_sources():
    for mime in ("text/html", "text/plain"):
        out = mr.md_to_html(f"![x](data:{mime};base64,AAAA)")
        assert "<img" not in out
        assert "src=" not in out
        assert f"![x](data:{mime};base64,AAAA)" in out


def test_data_image_subtype_that_merely_starts_with_an_allowed_name_is_rejected():
    # `_GOOD_DATA_RE` requires `;` right after the type name, so a crafted
    # subtype that starts with (but isn't) an allowed one must not slip
    # through a prefix match.
    for crafted in ("data:image/pngX;base64,AAAA", "data:image/pngjpeg;base64,AAAA"):
        out = mr.md_to_html(f"![x]({crafted})")
        assert "<img" not in out
        assert "src=" not in out
        assert f"![x]({crafted})" in out


def test_is_safe_destination_rejects_svg_xml_and_prefix_matches_directly():
    # markdown-it's own default validator (`_default_validate_link`) carries
    # an identical (gif|png|jpeg|webp) carve-out and independently rejects
    # each of these before `_is_safe_destination` is ever reached, so routing
    # them through `md_to_html` alone would not catch a regression in
    # `_GOOD_DATA_RE` specifically — e.g. loosened to `^data:image/`. Calling
    # `_is_safe_destination` directly pins this module's own allowlist.
    for rejected in (
        "data:image/svg+xml;base64,AAAA",
        "data:image/pngX;base64,AAAA",
        "data:image/pngjpeg;base64,AAAA",
    ):
        assert mr._is_safe_destination(rejected) is False


def test_is_safe_destination_rejects_unbalanced_ipv6_brackets_directly():
    # `urlparse` raises `ValueError: Invalid IPv6 URL` on these instead of
    # just failing the scheme/netloc check (url_safety.py's identical bug,
    # PR #269). Not reachable through `md_to_html` — markdown-it's
    # `normalizeLink` percent-encodes the brackets before `_is_safe_destination`
    # ever sees them — but the test suite already calls this function
    # directly, so it must not crash a direct caller either.
    for rejected in ("http://[::1", "http://a[b]c", "https://x["):
        assert mr._is_safe_destination(rejected) is False
