"""Home's hero stats are links to the dashboard behind each number.

They were `<div>`s until 2026-09-18, when the operator reported that a figure they wanted
to look into led nowhere. The whole point of the landing page is to point
somewhere, so a stat that reports a number and offers no way to the page it
came from is a dead end on the one screen that exists to avoid them.
"""
from __future__ import annotations

import re

import design_system as ds
import home_build


# label -> the page the figure is actually computed on. Not `/overview`: a
# reader who clicks a number should land where that number is, not on an index
# that makes them hunt for it again.
EXPECTED = {
    "Today's LLM Spend": "/spending",
    "CI Health": "/health",
    "Waypoints Passed (7d)": "/velocity",
}


def test_a_hero_stat_renders_as_a_link_to_its_dashboard():
    html = home_build._hero_stat("CI Health", "98%", "+1", "/health")
    assert html.startswith('<a class="hero-stat" href="/health">')
    assert html.endswith("</a>")
    assert "<div class=\"hero-stat\"" not in html


def test_every_hero_stat_on_the_page_is_a_link_to_the_right_page(monkeypatch):
    """The real call sites, with the PAIRING pinned rather than the presence.

    An earlier version searched `_full_body`'s string constants for each label
    and each href independently, so swapping two destinations between stats
    left it green — exactly the failure it claimed to catch. It also justified
    not rendering on the grounds that the loaders need the Library and the
    spend ledger reachable, which is wrong: they all degrade rather than raise.

    This records what `_hero_stat` is actually called with, in order.
    """
    seen: list[tuple[str, str]] = []

    def record(label, val, delta, href):
        seen.append((label, href))
        return ""

    monkeypatch.setattr(home_build, "_hero_stat", record)
    monkeypatch.setattr(home_build, "_today_spend",
                        lambda: {"value": "$1.00", "delta": "today"})
    monkeypatch.setattr(home_build, "_ci_health",
                        lambda: {"value": "99%", "delta": "7d"})
    monkeypatch.setattr(home_build, "_velocity_stat",
                        lambda: {"value": "3 passed", "delta": "7d"})

    home_build._full_body()

    assert seen == list(EXPECTED.items())


def test_hero_stat_requires_a_destination():
    """`href` has no default, so a stat added later cannot silently be a dead
    end again — there is nothing to forget to pass."""
    import inspect

    params = inspect.signature(home_build._hero_stat).parameters
    assert "href" in params
    assert params["href"].default is inspect.Parameter.empty


def test_the_label_and_href_are_escaped():
    html = home_build._hero_stat('A & B "quoted"', "1", "", '/x?a=1&b="2"')
    assert "&amp;" in html
    assert 'href="/x?a=1&amp;b=&quot;2&quot;"' in html
    # The raw quote would have closed the attribute early.
    assert 'href="/x?a=1&b="2""' not in html


def test_the_hero_stat_carries_link_affordances():
    """As an `<a>` it would otherwise render underlined and link-coloured
    against the hero gradient. Matches the `.wtile` strip beside it.

    `COMPONENTS_CSS` is named directly rather than searched for, so this fails
    loudly if the rule moves instead of quietly finding it somewhere else.
    """
    css = ds.COMPONENTS_CSS
    block = re.search(r"\.hero-stat\s*\{[^}]*\}", css)
    assert block, ".hero-stat rule not found in COMPONENTS_CSS"
    flat = block.group(0).replace(" ", "")
    assert "text-decoration:none" in flat
    assert "color:#fff" in flat
    assert ".hero-stat:focus-visible" in css


def test_the_hover_rule_re_disables_the_underline():
    """The global `a:hover { text-decoration:underline }` has specificity 0,1,1
    and outranks `.hero-stat`'s 0,1,0, so setting it on the base rule alone
    leaves the label, value and delta underlined on hover. `.qcard:hover`,
    `.overview-card:hover` and `.doc-item:hover` all repeat it for this reason.

    Asserted on the HOVER rule specifically: an earlier version of the test
    above checked only that a `.hero-stat:hover` rule existed, which it did
    while the underline was live.
    """
    hover = re.search(r"\.hero-stat:hover\s*\{[^}]*\}", ds.COMPONENTS_CSS)
    assert hover, ".hero-stat:hover rule not found"
    assert "text-decoration:none" in hover.group(0).replace(" ", "")


def test_the_hover_transition_is_disabled_under_reduced_motion():
    """`.qcard` and `.overview-card` do this; a transform transition with no
    guard is a motion regression for anyone who asked not to have one."""
    assert re.search(
        r"@media\s*\(prefers-reduced-motion:reduce\)\s*\{[^}]*\.hero-stat\s*\{[^}]*transition:none",
        ds.COMPONENTS_CSS,
    ), ".hero-stat has no prefers-reduced-motion guard"
