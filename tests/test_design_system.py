"""Tests for design_system.py — the shared Atelier design-system module (ARCH-216).

Invariants:
- TOKENS contains all required light-mode custom properties.
- TOKENS contains the dark-mode block [data-theme="dark"] with all dark overrides.
- BASE_CSS includes body, prefers-reduced-motion, responsive breakpoints.
- COMPONENTS_CSS includes StatCard, RangeControl, Table, Tooltip, Drawer, state classes.
- THEME_TOGGLE_JS includes localStorage and prefers-color-scheme.
- RANGE_CONTROL_JS includes aria-pressed and day-stepper logic.
- nav_component() returns HTML with brand, nav links, aria-current, theme toggle button, __WHOAMI__.
- nav_component() hides full-only links for non-full tier.
- stat_card() returns HTML with label, value, and optional delta.
- page_shell() wraps content in a full HTML doc with tokens, scripts, nav.
- range_control() returns real <button> elements with aria-pressed.
- drawer_html() returns role=dialog, aria-modal, and focus-trap script.
- empty_state(), loading_state(), error_state() return non-empty HTML.
- awaiting_state() raises on an invalid owner, and renders holds/stale/contradicted.
- stalled_state() renders a banner above the tiles it qualifies.
"""
import datetime as dt
import re

import pytest

import design_system as ds
import orient_time


def test_tokens_light_mode_properties():
    for prop in ["--bg", "--surface", "--surface-2", "--line", "--ink", "--ink-2", "--ink-3",
                 "--brand", "--brand-ink", "--brand-wash", "--ok", "--warn", "--err", "--grad"]:
        assert prop in ds.TOKENS, f"Missing light token: {prop}"


def test_tokens_dark_mode_block():
    assert '[data-theme="dark"]' in ds.TOKENS
    dark_block = ds.TOKENS.split('[data-theme="dark"]')[1]
    for prop in ["--bg", "--brand", "--ink", "--ok", "--warn", "--err", "--grad"]:
        assert prop in dark_block, f"Missing dark token: {prop}"


def test_tokens_exact_brand_value():
    assert "#E85533" in ds.TOKENS  # light brand
    assert "#FF8A5E" in ds.TOKENS  # dark brand


def test_tokens_exact_gradient():
    assert "#E8542F" in ds.TOKENS  # light grad start
    assert "#FFA64D" in ds.TOKENS  # light grad end
    assert "#B83E1F" in ds.TOKENS  # dark grad start


def test_base_css_has_body_and_reset():
    assert "box-sizing:border-box" in ds.BASE_CSS or "box-sizing: border-box" in ds.BASE_CSS
    assert "font-family" in ds.BASE_CSS
    assert "prefers-reduced-motion" in ds.BASE_CSS


def test_base_css_has_responsive_breakpoints():
    assert "640" in ds.BASE_CSS and "1024" in ds.BASE_CSS


def test_components_css_has_statcard():
    assert "statcard" in ds.COMPONENTS_CSS.lower()


def test_components_css_has_range_control():
    assert "segmented" in ds.COMPONENTS_CSS or "rangecontrol" in ds.COMPONENTS_CSS.lower()


def test_components_css_has_table():
    assert "thead" in ds.COMPONENTS_CSS or "sticky" in ds.COMPONENTS_CSS


def test_components_css_has_tooltip():
    assert "::after" in ds.COMPONENTS_CSS and "data-tip" in ds.COMPONENTS_CSS


def test_components_css_has_drawer():
    assert "drawer" in ds.COMPONENTS_CSS.lower() or "dialog" in ds.COMPONENTS_CSS.lower()


def test_components_css_has_state_classes():
    lower = ds.COMPONENTS_CSS.lower()
    assert "empty" in lower and ("loading" in lower or "skeleton" in lower) and "error" in lower


def test_theme_toggle_js_has_localstorage():
    assert "localStorage" in ds.THEME_TOGGLE_JS


def test_theme_toggle_js_has_prefers_color_scheme():
    assert "prefers-color-scheme" in ds.THEME_TOGGLE_JS


def test_range_control_js_has_aria_pressed():
    assert "aria-pressed" in ds.RANGE_CONTROL_JS


def test_range_control_js_has_day_stepper():
    assert "dayIdx" in ds.RANGE_CONTROL_JS or "daystep" in ds.RANGE_CONTROL_JS.lower()


def test_nav_component_has_brand():
    html = ds.nav_component("/")
    assert "Convivy" in html


def test_nav_component_aria_current():
    html = ds.nav_component("/spending")
    assert 'aria-current="page"' in html
    # /spending is under Dashboards area, so Dashboards link is aria-current
    assert "Dashboards" in html


def test_nav_component_home_area_active():
    html = ds.nav_component("/")
    assert 'aria-current="page"' in html
    assert "Home" in html


def test_nav_component_theme_toggle_button():
    html = ds.nav_component("/")
    assert "<button" in html
    assert "theme" in html.lower() or "dark" in html.lower()


def test_nav_component_whoami_placeholder():
    html = ds.nav_component("/")
    assert "__WHOAMI__" in html


def test_nav_component_hides_full_only_for_contractor():
    html = ds.nav_component("/", tier="contractor")
    assert "Dashboards" not in html
    assert "Chart House" not in html
    assert "Archives" not in html
    assert "Knowledge base" not in html
    # contractor still sees Home, the Reading Room and the Writing Desk
    # (both reachable by both tiers); the retired Reference Docs area is gone
    # for every tier.
    assert "Home" in html
    assert "Reading Room" in html
    assert "Writing Desk" in html
    assert "Reference Docs" not in html


def test_nav_component_subnav_for_dashboards():
    html = ds.nav_component("/spending")
    assert "atelier-subnav" in html
    assert 'href="/overview"' in html


def test_nav_component_reading_room_replaces_orientation():
    # The Reading Room (ARCH-357) is the per-member reading area — reachable by
    # both tiers. Orientation + Reference Docs retired into it. For the full
    # tier it is a Documents subnav entry now, not a primary (nav-reshape epic
    # issue_01M0TKX6DW2E1TXGN9QT6K61J5 step 2) — the assertion below still
    # holds because the subnav renders the same anchor, aria-current and all.
    html = ds.nav_component("/room")
    assert "Reading Room" in html
    assert 'href="/room" aria-current="page"' in html
    assert "Orientation" not in html
    assert "Reference Docs navigation" not in html


def test_page_shell_extra_css_injected():
    html = ds.page_shell(title="T", nav_html="", body="", extra_css=".test-rule{}")
    assert ".test-rule{}" in html


def test_page_shell_extra_css_default_empty():
    html = ds.page_shell(title="T", nav_html="", body="")
    # no extra junk injected by default
    assert "extra_css" not in html


def test_page_shell_stamps_render_time():
    """Every page must be able to answer "is this current?" about itself. Three
    times in one week the operator had to ask that out of band, because a stale page and a
    fresh one looked identical."""
    html = ds.page_shell(title="T", nav_html="", body="<p>x</p>")
    assert 'class="render-stamp"' in html
    assert "rendered " in html
    assert " MST</footer>" in html


def test_render_stamp_accepts_an_explicit_time():
    assert "2026-01-01 00:00 MST" in ds.render_stamp("2026-01-01 00:00 MST")


def test_render_stamp_escapes_its_input():
    """The stamp is the last thing appended to every page, so an unescaped value
    here would be an injection point on every page at once."""
    assert "<script>" not in ds.render_stamp("<script>alert(1)</script>")


def test_render_stamp_is_inside_the_page_wrap():
    """Inside `.page-wrap` so it inherits the page's max-width and padding rather
    than sitting flush against the viewport edge."""
    html = ds.page_shell(title="T", nav_html="", body="<p>BODY</p>")
    wrap = html.split('<div class="page-wrap">', 1)[1].split("</div>", 1)[0]
    assert "BODY" in wrap and "render-stamp" in wrap


def test_render_stamp_css_ships_in_components():
    """The rule lives in the shared COMPONENTS_CSS, so a page cannot get the stamp
    markup without the styling that keeps it unobtrusive."""
    assert ".render-stamp" in ds.COMPONENTS_CSS


# --- the PWA nav cluster: back / forward / refresh, in the shared shell ------
# (issue_01M1517CYMAV0GAEB2FYQQKF4N) — a PWA installed as `display:standalone`
# (assets/site.webmanifest) has no browser chrome, so this control is the only
# way to reload, go back, or go forward from inside the app.


def test_nav_cluster_renders_on_a_full_tier_page():
    html = ds.page_shell(title="T", nav_html=ds.nav_component("/", "full"), body="<p>x</p>")
    assert 'class="nav-cluster"' in html
    assert 'id="navBackBtn"' in html and 'id="navForwardBtn"' in html and 'id="navRefreshBtn"' in html


def test_nav_cluster_renders_on_a_contractor_page():
    # The cluster lives in render_stamp/page_shell, called by every builder
    # regardless of tier — a contractor page must get it too, not just full.
    html = ds.page_shell(title="T", nav_html=ds.nav_component("/", "contractor"), body="<p>x</p>")
    assert 'class="nav-cluster"' in html
    assert 'id="navBackBtn"' in html and 'id="navForwardBtn"' in html and 'id="navRefreshBtn"' in html


def test_nav_cluster_lives_next_to_the_render_stamp():
    """The stamp is the evidence a refresh reached the server, so the control
    that asks for one must render inside the same footer, not somewhere else
    on the page."""
    html = ds.render_stamp()
    footer = html.split("</footer>")[0]
    assert 'class="nav-cluster"' in footer
    assert "rendered " in footer


def test_back_and_forward_default_to_disabled():
    """Server-rendered HTML cannot know this tab's navigation depth — only
    NAV_CLUSTER_JS can, at load. Defaulting to enabled would let a reader
    with JS disabled or slow to load see an arrow that silently does
    nothing, the exact defect this control exists to avoid."""
    html = ds.render_stamp()
    assert '<button type="button" id="navBackBtn" class="nav-btn" aria-label="Go back" disabled>' in html
    assert '<button type="button" id="navForwardBtn" class="nav-btn" aria-label="Go forward" disabled>' in html


def test_refresh_button_is_not_disabled_by_default():
    html = ds.render_stamp()
    refresh_tag = html.split('id="navRefreshBtn"')[1].split(">")[0]
    assert "disabled" not in refresh_tag


def test_render_stamp_carries_a_data_attribute_for_the_refresh_check():
    """NAV_CLUSTER_JS compares this attribute before and after a refresh to
    detect one that reached the browser but not a fresh server render (e.g.
    server.py's last-good-build fallback) — comparing DOM text instead would
    be contaminated by the cluster's own button labels and refresh-note."""
    html = ds.render_stamp("2026-01-01 00:00 MST")
    assert 'data-rendered-at="2026-01-01 00:00 MST"' in html


def test_render_stamp_data_attribute_is_escaped():
    html = ds.render_stamp("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_stamp_ending_survives_the_nav_cluster():
    """Regression pin: the cluster must sit BEFORE `rendered <when>`, never
    after, or the literal `" MST</footer>"` ending other tests rely on
    breaks."""
    html = ds.page_shell(title="T", nav_html="", body="<p>x</p>")
    assert " MST</footer>" in html


def test_refresh_note_is_an_aria_live_region():
    html = ds.render_stamp()
    assert 'id="refreshNote"' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html


def test_nav_cluster_js_installs_before_reload_hook_for_pages_to_stash_state():
    """Pages with client-side filter state (e.g. /archives' product chips)
    need a signal that fires only on OUR refresh, not a bare browser reload,
    so they can choose to preserve state deliberately rather than by
    accident."""
    assert "convivy:beforerefresh" in ds.NAV_CLUSTER_JS


def test_nav_cluster_js_compares_render_stamp_before_and_after_refresh():
    assert "data-rendered-at" in ds.NAV_CLUSTER_JS
    assert "REFRESH_KEY" in ds.NAV_CLUSTER_JS


def test_nav_cluster_js_reports_a_refresh_that_did_not_move_the_stamp():
    assert "did not change" in ds.NAV_CLUSTER_JS


def test_nav_cluster_js_tracks_position_for_back_forward():
    """No history.canGoBack() exists and history.length counts the whole
    session, not this tab's position — NAV_CLUSTER_JS keeps its own position
    counter rather than trusting either (a comment explaining why is fine;
    reading `history.length` to compute the button state is not)."""
    assert "back.disabled = !(pos > 0)" in ds.NAV_CLUSTER_JS
    assert "fwd.disabled = !(pos < max)" in ds.NAV_CLUSTER_JS
    assert "__dsPos" in ds.NAV_CLUSTER_JS
    assert "back_forward" in ds.NAV_CLUSTER_JS


def test_nav_cluster_js_wraps_pushstate_and_replacestate():
    """So a page's own client-side navigation (dashboards pushing filter
    state via _ds.writeUrlState) still counts toward the position, instead
    of silently falling out of sync with NAV_CLUSTER_JS's own count."""
    assert "history.pushState = function" in ds.NAV_CLUSTER_JS
    assert "history.replaceState = function" in ds.NAV_CLUSTER_JS


# Every test above this line asserts on NAV_CLUSTER_JS's SOURCE TEXT, which
# cannot catch a bug in what the state machine actually does across
# sessionStorage/history.state/popstate. This one drives the ACTUAL script
# against a stub DOM/history — mirroring test_issue_build.py's node-driven
# chip-filter test — for the one path source-text assertions can't reach:
# a page assigning `location.hash` directly (engineering_build.py's ARCH-log
# jump-to-entry box), which creates a history entry without ever calling
# pushState/replaceState.
_NAV_CLUSTER_DOM_DRIVER_JS = r"""
const fs = require('fs');
const vm = require('vm');
const [, , jsPath] = process.argv;
const jsSrc = fs.readFileSync(jsPath, 'utf8');

function makeBtn() { return { disabled: false, addEventListener: function(){} }; }

var elements = {
  navBackBtn: makeBtn(),
  navForwardBtn: makeBtn(),
  navRefreshBtn: makeBtn(),
  renderStamp: { getAttribute: function(){ return '2026-08-30T00:00:00Z'; } },
  refreshNote: { textContent: '' },
};

var documentStub = {
  readyState: 'complete',
  getElementById: function(id){ return elements[id] || null; },
  addEventListener: function(){},
};

var storage = {};
var sessionStorageStub = {
  getItem: function(k){ return Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null; },
  setItem: function(k, v){ storage[k] = String(v); },
  removeItem: function(k){ delete storage[k]; },
};

var windowListeners = {};
var windowStub = {
  addEventListener: function(type, fn){ (windowListeners[type] = windowListeners[type] || []).push(fn); },
};

var historyState = null;
var historyStub = {
  get state() { return historyState; },
  pushState: function(state) { historyState = state; },
  replaceState: function(state) { historyState = state; },
  back: function() {},
  forward: function() {},
};

var locationStub = { href: 'https://team.convivy.com/reference/arch-log', hash: '' };

var sandbox = {
  document: documentStub,
  window: windowStub,
  history: historyStub,
  location: locationStub,
  sessionStorage: sessionStorageStub,
  console: console,
};
vm.createContext(sandbox);
vm.runInContext(jsSrc, sandbox);

function fire(type) {
  (windowListeners[type] || []).forEach(function(fn){ fn({}); });
}

// The ARCH log's jump-to-entry box assigns `location.hash` directly. The
// browser turns that into a new history entry whose state is CLONED from
// the entry being left (here, the initial load's {__dsPos: 0}) rather than
// getting a fresh tag, and it never calls pushState/replaceState.
historyState = { __dsPos: 0 };
locationStub.hash = '#arch-3';
fire('hashchange');

// Browser Back lands on the original (pre-hash-jump) entry, tagged 0. A real
// browser fires BOTH popstate and hashchange for this one traversal —
// popstate first — since the hash genuinely differs between the two
// entries (`#arch-3` vs. none) being modeled here. Firing only `popstate`,
// as this driver used to, could never exercise the suppress mechanism that
// stops that accompanying hashchange from double-counting the same step.
historyState = { __dsPos: 0 };
locationStub.hash = '';
fire('popstate');
fire('hashchange');

process.stdout.write(JSON.stringify({
  backDisabledAfterBack: elements.navBackBtn.disabled,
  forwardDisabledAfterBack: elements.navForwardBtn.disabled,
  posAfterBack: storage['convivy-nav-pos'],
  maxAfterBack: storage['convivy-nav-max'],
}));
"""


def _drive_nav_cluster_after_hash_jump_and_back() -> dict:
    """Runs the ACTUAL NAV_CLUSTER_JS through a hash-jump then a browser
    Back, via _NAV_CLUSTER_DOM_DRIVER_JS. Skips when node is unavailable,
    matching test_issue_build.py's _click_chip."""
    import json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to drive the inline JS against a DOM")

    with tempfile.TemporaryDirectory() as tmp:
        js_path = f"{tmp}/inline.js"
        driver_path = f"{tmp}/driver.js"
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(ds.NAV_CLUSTER_JS)
        with open(driver_path, "w", encoding="utf-8") as f:
            f.write(_NAV_CLUSTER_DOM_DRIVER_JS)
        r = subprocess.run([node, driver_path, js_path], capture_output=True, text=True)
        assert r.returncode == 0, f"DOM driver failed:\n{r.stdout}\n{r.stderr}"
        return json.loads(r.stdout)


def test_forward_is_reachable_after_a_back_that_lands_before_a_hash_jump():
    """Pins two round-2 PR #234 findings against the same drive. (1) The
    round-1 defect: engineering_build.py's ARCH-log jump-to-entry box sets
    `location.hash` directly, a path the pushState/replaceState wrap never
    sees. Before the `hashchange` listener was wired to the same counter,
    the entry it created went untagged, so a later browser Back landed with
    `max` still at the pre-jump value and Forward read permanently disabled
    for that entry even though the browser could genuinely go forward to it.
    (2) The suppress mechanism that fix added: the driver's Back step fires
    BOTH `popstate` and `hashchange`, matching real browser ordering for a
    traversal that crosses a hash boundary, so a broken/deleted suppress
    would double-count that one step — asserted directly via `pos`/`max`,
    not just the derived button states, so the double-count can't hide
    behind the same disabled/enabled reading a single miscount would give
    at these particular values."""
    result = _drive_nav_cluster_after_hash_jump_and_back()
    assert result["backDisabledAfterBack"] is True, "back should be at the oldest entry"
    assert result["forwardDisabledAfterBack"] is False, (
        "forward must be reachable after Back lands on the entry before a hash jump"
    )
    assert result["posAfterBack"] == "0", "Back must land at position 0, not double-incremented"
    assert result["maxAfterBack"] == "1", "max must stay at the hash jump's position, not bumped again"


def test_page_shell_loads_nav_cluster_js_in_the_head_before_body_scripts():
    """Must install before any body-embedded page script calls
    `_ds.writeUrlState`, or an early pushState/replaceState would bypass the
    wrap and the position count would fall behind."""
    html = ds.page_shell(title="T", nav_html="", body="<script>_ds.writeUrlState({}, true);</script>")
    head, _, rest = html.partition("</head>")
    assert "__dsPos" in head
    assert "_ds.writeUrlState" in rest  # confirms the body script is genuinely after head


def test_stat_card_has_label_and_value():
    html = ds.stat_card("Today's Spend", "$18.42")
    assert "Today&#x27;s Spend" in html or "Today's Spend" in html
    assert "$18.42" in html


def test_stat_card_with_delta():
    html = ds.stat_card("CI Health", "98.6%", delta="up arrow", delta_dir="up")
    assert "up arrow" in html
    assert "up" in html  # delta_dir class


def test_page_shell_is_full_html():
    html = ds.page_shell(title="Test", nav_html="<nav>test</nav>", body="<p>Hello</p>")
    assert "<!doctype html>" in html.lower() or "<!DOCTYPE html>" in html
    assert "<title>Test · Orient · Convivy Builder</title>" in html
    assert "Hello" in html
    assert "--brand" in html


def test_tab_title_puts_the_page_first_and_home_reads_orient_convivy_builder():
    # company/decisions/0020: tabs truncate from the right, so the page name
    # leads, and Orient never stands alone without the brand.
    assert ds.tab_title("Crews") == "Crews · Orient · Convivy Builder"
    assert ds.tab_title("") == "Orient · Convivy Builder"
    html = ds.page_shell(title="", nav_html="", body="")
    assert "<title>Orient · Convivy Builder</title>" in html
    # The home-screen label is the one place Orient stands alone: an iPhone
    # shows about 12 characters under the icon (the operator, 2026-10-06).
    assert '<meta name="apple-mobile-web-app-title" content="Orient">' in html


def test_header_wordmark_reads_convivy_builder_dot_orient():
    nav = ds.nav_component("/", "full")
    assert 'aria-label="Convivy Builder home"' in nav
    assert ('<span class="brand-name">Convivy Builder</span>\n'
            '      <span class="brand-sep" aria-hidden="true">\u00b7</span>\n'
            '      <span class="brand-surface">Orient</span>') in nav
    # The dot takes the surface name's size, weight and ink, and hides with it.
    assert ".brand-sep {\n  font-size:15px;\n  font-weight:400;\n  color:var(--ink);" in ds.COMPONENTS_CSS
    # The hide rule must sit inside the narrow-width block, or the dot and
    # "Orient" would vanish at desktop width too.
    narrow = ds.COMPONENTS_CSS.split("@media (max-width:899px) {", 1)[1].split("\n}", 1)[0]
    assert ".brand-sep, .brand-surface { display:none; }" in narrow
    # Same size and ink as the brand, regular weight against its 700.
    assert ".brand-surface {\n  font-size:15px;\n  font-weight:400;\n  color:var(--ink);" in ds.COMPONENTS_CSS


def test_page_shell_has_whoami_placeholder():
    html = ds.page_shell(title="T", nav_html="__WHOAMI__", body="")
    assert "__WHOAMI__" in html


def test_range_control_has_real_buttons():
    html = ds.range_control()
    assert "<button" in html
    assert 'aria-pressed="true"' in html  # the default selected option


def test_range_control_custom_selected():
    html = ds.range_control(selected="30d")
    assert "30d" in html


def test_range_control_30d_is_pressed():
    html = ds.range_control(selected="30d")
    # The 30d button element contains data-rc="30d" aria-pressed="true".
    # Find the button opening tag that references 30d and check its aria-pressed.
    assert 'data-rc="30d" aria-pressed="true"' in html


def test_drawer_html_has_dialog_role():
    html = ds.drawer_html("test-drawer", "Test Drawer", "<p>Content</p>")
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html


def test_drawer_html_has_title():
    html = ds.drawer_html("d1", "My Drawer", "body")
    assert "My Drawer" in html


def test_empty_state_returns_html():
    html = ds.empty_state("Nothing here.")
    assert "Nothing here." in html
    assert "<" in html


def test_loading_state_returns_html():
    html = ds.loading_state("Loading data...")
    assert "<" in html


def test_error_state_returns_html():
    html = ds.error_state("Something went wrong.")
    assert "Something went wrong." in html
    assert "<" in html


def test_range_control_selected_has_surface_brand_ink_css():
    # The segmented active state now uses raised-white-pill (surface/brand-ink),
    # matching .vbtn pattern for ≥4.5:1 contrast in both modes.
    assert 'background:var(--surface)' in ds.COMPONENTS_CSS
    assert 'color:var(--brand-ink)' in ds.COMPONENTS_CSS
    assert '[aria-pressed="true"]' in ds.COMPONENTS_CSS


def test_tooltip_focus_visible_rule_present():
    assert '.tip:focus-visible::after' in ds.COMPONENTS_CSS


def test_drawer_close_restores_hidden():
    assert "setAttribute('hidden'" in ds._DRAWER_JS or 'setAttribute("hidden"' in ds._DRAWER_JS


def test_drawer_open_flushes_the_closed_frame_before_it_slides():
    """With `[hidden]{display:none!important}` in BASE_CSS the closed drawer is
    display:none, and a transition cannot run across a display:none -> visible
    change made in one style recalc. `hidden` must come off, layout must be read
    to flush the closed frame, and only then may `.open` flip the transform —
    in that order, or the drawer pops instead of sliding.

    Every assertion names `drawer` explicitly. The backdrop is a sibling element
    with its own transition, so an assertion on the bare call would hold with the
    sequence wired to the wrong element."""
    body = ds._DRAWER_JS[ds._DRAWER_JS.index("function open()"):]
    body = body[:body.index("function hideWhenClosed()")]
    unhide = body.index("drawer.removeAttribute('hidden')")
    reflow = body.index("drawer.offsetHeight")
    slide = body.index("drawer.classList.add('open')")
    assert unhide < reflow < slide


def test_drawer_close_defers_hidden_until_the_slide_out_finishes():
    """Setting `hidden` in the same call that removes `.open` cuts the transition
    off at frame one, so it waits for transitionend — with a timer fallback,
    because a drawer that never gets `hidden` back keeps role=dialog in the
    accessibility tree, which is the defect the shared rule exists to fix.

    The listener must be on `drawer`. Wired to the backdrop instead it would
    still read as deferred here while never firing for the drawer's own slide,
    which is that permanent-exposure failure exactly."""
    body = ds._DRAWER_JS[ds._DRAWER_JS.index("function close()"):]
    body = body[:body.index("if (closeBtn)")]
    assert "setAttribute('hidden'" not in body, "close() still hides synchronously"
    assert "drawer.addEventListener('transitionend', hideWhenClosed)" in body
    assert "setTimeout(hideWhenClosed" in body


def test_drawer_reopening_during_the_slide_out_is_not_hidden_underneath_it():
    # The deferred hide is guarded on .open, and open() clears the pending timer;
    # without both, a reopen inside the slide-out window vanishes.
    assert "if (!drawer.classList.contains('open')) drawer.setAttribute('hidden', '');" in ds._DRAWER_JS
    open_body = ds._DRAWER_JS[ds._DRAWER_JS.index("function open()"):]
    open_body = open_body[:open_body.index("function hideWhenClosed()")]
    assert "clearTimeout(hideTimer)" in open_body


def test_the_shared_sheet_makes_hidden_beat_every_display_rule():
    """Without this the `hidden` attribute is inert wherever a `display:` rule
    matches, and setAttribute('hidden') above closes nothing. An author
    declaration beats the UA stylesheet's [hidden]{display:none} outright, so
    !important is what the rule needs; a plain [hidden]{display:none} here would
    still lose to `.drawer{display:flex}` on specificity."""
    assert "[hidden] { display: none !important; }" in ds.BASE_CSS


def test_every_page_carries_the_hidden_rule_through_the_shell():
    # The point of putting it in BASE_CSS rather than per page: any page built on
    # page_shell gets it, including the ones that have not been written yet.
    html = ds.page_shell(title="T", nav_html="", body="<p>x</p>")
    assert "[hidden] { display: none !important; }" in html


def test_a_drawer_is_closed_on_the_page_that_renders_it():
    # The filed defect: the drawer ships `hidden`, `.drawer` sets display:flex,
    # and nothing overrode it — so role=dialog aria-modal=true was in the
    # accessibility tree from page load on every page with a drawer.
    body = ds.drawer_html("doc-drawer", "Doc", "<p>x</p>")
    assert " hidden>" in body
    drawer_rule = re.search(r"\n\.drawer \{[^}]*\}", ds.COMPONENTS_CSS)
    assert drawer_rule and "display:flex" in drawer_rule.group(0), (
        "the conflict this test guards is gone; if .drawer no longer sets display "
        "the shared rule is still right, but rewrite this test to say why"
    )
    html = ds.page_shell(title="T", nav_html="", body=body)
    assert "[hidden] { display: none !important; }" in html


def test_theme_toggle_aria_pressed_in_js():
    assert "aria-pressed" in ds.THEME_TOGGLE_JS


def test_theme_toggle_aria_pressed_in_html():
    html = ds.nav_component("/")
    assert 'id="themeToggle"' in html
    assert 'aria-pressed=' in html


def test_focus_visible_ring_in_base_css():
    assert ':focus-visible' in ds.BASE_CSS


def test_delta_dir_whitelist():
    html = ds.stat_card("Test", "1", delta="x", delta_dir="<script>")
    assert "<script>" not in html
    assert 'class="statcard-delta"' in html


def test_drawer_html_docstring_mentions_trusted():
    import inspect
    doc = inspect.getdoc(ds.drawer_html)
    assert "trusted" in doc.lower()


def test_drawer_js_has_focus_trap():
    # Tab/Shift-Tab focus-trap: the handler wraps Tab from last→first and Shift-Tab
    # from first→last so focus cannot escape an open drawer.
    assert "'Tab'" in ds._DRAWER_JS or '"Tab"' in ds._DRAWER_JS


def test_tokens_brand_ink_light_passes_contrast():
    # --brand-ink light is #B23C1C (~4.6:1 on --brand-wash #FCE9E0)
    assert "B23C1C" in ds.TOKENS


def test_statcard_label_uses_ink_2():
    # Small uppercase labels must use --ink-2 (≥4.5:1) not --ink-3 (3.99 on surface)
    assert ".statcard-label" in ds.COMPONENTS_CSS
    label_block = ds.COMPONENTS_CSS.split(".statcard-label")[1].split("}")[0]
    assert "var(--ink-2)" in label_block and "var(--ink-3)" not in label_block


def test_nav_component_archives_area_and_substrip():
    # epic issue_01M0EHQHQ7M39K811XKX1BAG98 put Signals, Notes & wrap-ups and
    # the ARCH log together in Archives; epic issue_01M0TKX6DW2E1TXGN9QT6K61J5
    # step 2 added Market and Briefings, on the "group by what it reads" rule
    # — both rendered the signal vault at the time, same as their three
    # siblings here. Notes, the ARCH log and Market were trimmed back out
    # (the operator, 2026-08-26): Notes' page is retired, the ARCH log is a frozen log
    # with no front-page space, and Market renders zero cards. Superseded the
    # same day for Briefings specifically (it reads the KB index now) — see
    # design_system.py's note above _ARCHIVES_SUBNAV; its placement is
    # unchanged under the operator's replacement rule (memories vs. canonical
    # knowledge), which this test still verifies. Signals and Briefings are
    # what's left in the strip.
    for page in ("/archives", "/reference/briefings"):
        html = ds.nav_component(page, "full")
        assert 'href="/archives" aria-current="page"' in html
        assert 'aria-label="Archives navigation"' in html
        assert f'href="{page}" aria-current="page"' in html


def test_archives_substrip_dropped_notes_arch_log_and_market():
    # The three trims (the operator, 2026-08-26) are independent of each other: this
    # confirms none of them still show up in the Archives sub-strip itself,
    # regardless of which Archives page is current.
    html = ds.nav_component("/archives", "full")
    assert 'aria-label="Archives navigation"' in html
    assert 'href="/engineering/notes"' not in html
    assert 'href="/reference/arch-log"' not in html
    assert 'href="/market"' not in html


def test_arch_log_route_still_resolves_but_leaves_archives():
    # The ARCH-441 freeze took the log off the front page and out of the
    # Archives strip, but the route stays: 245 KB documents cite an ARCH-NNN
    # id and engineering_build serves deep links to it (issue citation
    # guarantee). Visiting it now groups under Documents, the /reference prefix
    # fallback, since it has no strip entry of its own any more.
    html = ds.nav_component("/reference/arch-log", "full")
    assert 'aria-label="Documents navigation"' in html
    assert 'href="/reference" aria-current="page"' in html


def test_nav_component_documents_area_and_substrip():
    # epic issue_01M0TKX6DW2E1TXGN9QT6K61J5 step 2: Knowledge base, Reading
    # Room and Writing Desk merge into one Documents area for the full tier —
    # the one answer to "where do documents live" instead of three separate
    # primaries.
    for page in ("/reference", "/room", "/writing-desk"):
        html = ds.nav_component(page, "full")
        assert 'href="/reference" aria-current="page"' in html
        assert 'aria-label="Documents navigation"' in html
        assert f'href="{page}" aria-current="page"' in html


def test_briefings_left_the_reading_room_for_archives():
    # Correcting the 2026-08-19 reshape's placement (the operator, 2026-08-24): the
    # page is a memory, not canonical knowledge (the operator 2026-08-26's replacement
    # rule — see design_system.py's note above _ARCHIVES_SUBNAV), so it
    # doesn't belong under Documents any more. (Market, its former companion
    # here, retired 2026-08-26 too — see test_ops_reports_build.py.)
    html = ds.nav_component("/reference/briefings", "full")
    assert "Reading Room navigation" not in html
    assert "Documents navigation" not in html


def test_nav_component_ops_hidden_from_contractor():
    # current on the ops paths too — the tier filter must hold after
    # _active_area resolves to the Archives/Reading Room areas (reviewer
    # suggestion, #60)
    for page in ("/", "/engineering/issues", "/archives", "/room"):
        html = ds.nav_component(page, "contractor")
        assert "/archives" not in html
        assert "/market" not in html
        assert "/reference/briefings" not in html
        assert "/engineering/notes" not in html


# --- the 2026-07-19 IA review -------------------------------------------------


def test_chart_house_lands_on_its_own_base_page_not_roster_or_issues():
    """The operator: "Roster is the first thing you see in the Engineering pane and it's
    the page I need the least often."

    The access log showed Roster as the area's second-busiest URL — but only
    because the rail landed there, so every click on "Engineering" counted as a
    Roster hit. The landing choice manufactured the evidence for itself.
    ARCH-373 settled what the front door should answer at the time: what is
    the fleet working on, i.e. Issues. issue_01M0JFSVSGZW7A4RD77YNG395B moved
    the landing again, to Chart House's own base page (every project's open
    work + a cost rollup) — Issues is now the base page's first sub-nav entry
    instead of the rail's own target.
    """
    rail = dict((h, lbl) for h, lbl in ds._DEFAULT_LINKS)
    assert "/chart-house" in rail
    assert rail["/chart-house"] == "Chart House"
    assert "/engineering/roster" not in rail
    assert "/engineering/issues" not in rail


def test_crews_is_its_own_top_level_area_with_roster_moved_in():
    """Crews (issue_01M08YM8MTNSTAEG1NV7E2GYRV) is a new rail entry, not
    nested under Chart House — it deliberately overlaps Chart House,
    Dashboards and the knowledge base, so it doesn't subordinate to any one
    of them. Roster moves here from Chart House's sub-nav, resolving the
    "Roster's fate is tied to Crews" note that stood since the nav reshape."""
    rail = dict((h, lbl) for h, lbl in ds._DEFAULT_LINKS)
    assert rail.get("/crews") == "Crews"
    assert "/crews" in ds._FULL_ONLY_LINKS

    crews_subnav = [h for h, _ in ds._CREWS_SUBNAV]
    assert crews_subnav[0] == "/crews", "the base page leads its own sub-strip"
    assert "/engineering/roster" in crews_subnav

    chart_house_subnav = [h for h, _ in ds._CHART_HOUSE_SUBNAV]
    assert "/engineering/roster" not in chart_house_subnav


def test_engineering_roster_resolves_to_the_crews_area_not_chart_house():
    """The one page that moved OUT of the Chart House sub-nav while keeping
    an /engineering-prefixed URL — _active_area's generic prefix fallback
    would otherwise still catch it and misroute to Chart House."""
    html = ds.nav_component("/engineering/roster", "full")
    assert 'href="/crews" aria-current="page"' in html
    assert 'aria-label="Crews navigation"' in html
    assert 'href="/engineering/roster" aria-current="page"' in html


def test_the_chart_house_strip_reads_as_overview_then_the_work_lifecycle():
    """Overview (the base page, issue_01M0JFSVSGZW7A4RD77YNG395B) leads, since
    it's now the area's landing page. Epics -> Corrections -> Course ->
    Merge queue -> Changelog is the order work moves after that: an epic's
    work items live under it or, unparented, under Corrections; a Course
    plans the sequence; an issue leaves as a PR (the operator, 2026-08-29 —
    issue_01M1518ZFBGHAWC4Z46W893H2S, splitting the single Issues page into
    three, Corrections adjacent to Epics as asked). The strip was in
    accretion order, scattering that across positions 2, 8 and 7. Decisions
    used to sit between Issues and Merge queue here; the decision plane is
    gone (2026-08-18). E2E, Notes & wrap-ups, and Roster later left this
    strip for Dashboards, Archives and Crews respectively (Roster's move,
    issue_01M08YM8MTNSTAEG1NV7E2GYRV, 2026-08-21). Backlog and Last night were
    retired (the operator, 2026-08-28): both were tied to structures the Chart House
    replaced, so "Blockers" is now the strip's tail."""
    order = [h for h, _ in ds._CHART_HOUSE_SUBNAV]
    lifecycle = ["/chart-house", "/engineering/issues", "/engineering/corrections",
                 "/engineering/course", "/engineering/merge-queue", "/engineering/changelog"]
    assert order[:6] == lifecycle
    assert order[-1] == "/engineering/blockers"
    assert "/engineering/backlog" not in order, "Backlog retired"
    assert "/engineering/overnight" not in order, "Last night retired"
    assert "/engineering/roster" not in order, "Roster moved to Crews"


def test_roster_is_still_reachable_and_still_full_tier():
    """Reordering must not retire a page. Roster keeps its URL, its tab, and its
    tier — it just stops being what the rail opens."""
    html = ds.nav_component("/engineering/roster", "full")
    assert 'href="/engineering/roster" aria-current="page"' in html
    assert "/chart-house" in ds._FULL_ONLY_LINKS


def test_a_contractor_sees_no_chart_house_entry():
    """The rail entry moved from one full-only path to another; a contractor
    must still not see the area at all."""
    html = ds.nav_component("/", "contractor")
    assert "/chart-house" not in html
    assert "/crews" not in html
    assert "/engineering/issues" not in html
    assert "/engineering/roster" not in html


# --- the 2026-08-19 top-level nav reshape (epic issue_01M0EHQHQ7M39K811XKX1BAG98) --


def test_dashboards_subnav_pinned_set_still_shows_spending_and_security():
    # The pinned five survive from the 2026-08-19 shape; E2E and the rest of
    # the twelve moved out in the 2026-08-24 reshape below — see
    # test_dashboards_subnav_is_trimmed_to_the_pinned_five.
    html = ds.nav_component("/overview", "full")
    assert 'href="/spending"' in html
    assert 'href="/security"' in html


# --- the 2026-08-24 vaults-mirror reshape (epic issue_01M0TKX6DW2E1TXGN9QT6K61J5,
# step 2) --------------------------------------------------------------------


def test_default_nav_matches_the_vaults_mirror():
    # Dashboards moved between Home and Chart House (the operator, 2026-08-28 —
    # issue_01M15C638Q3VPF38PNJAM1DGQV); see
    # test_dashboards_sits_between_home_and_chart_house below for the
    # dedicated regression.
    labels = [lbl for _, lbl in ds._DEFAULT_LINKS]
    assert labels == ["Home", "Dashboards", "Chart House", "Crews", "Archives",
                      "Documents"]
    assert "Products" not in labels
    assert "Reading Room" not in labels
    assert "Writing Desk" not in labels
    assert "Knowledge base" not in labels


def test_dashboards_sits_between_home_and_chart_house():
    labels = [lbl for _, lbl in ds._DEFAULT_LINKS]
    assert labels.index("Dashboards") == labels.index("Home") + 1
    assert labels.index("Chart House") == labels.index("Dashboards") + 1


def test_active_area_unaffected_by_the_dashboards_reorder():
    """The reorder is a position change in `_DEFAULT_LINKS` only —
    `_active_area` resolves off separate path sets, not off list order, so
    every page must still highlight exactly the area it did before."""
    assert ds._active_area("/") == "/"
    assert ds._active_area("/overview") == "/overview"
    assert ds._active_area("/spending") == "/overview"
    assert ds._active_area("/chart-house") == "/chart-house"
    assert ds._active_area("/crews") == "/crews"
    assert ds._active_area("/archives") == "/archives"
    assert ds._active_area("/reference") == "/reference"


def test_narrow_tier_rail_is_untouched():
    # The narrow tier keeps its own three-entry rail rather than a filtered
    # slice of the full-tier one — its subnavs never render, so Reading Room
    # and Writing Desk have to stay primaries for it.
    labels = [lbl for _, lbl in ds._NARROW_LINKS]
    assert labels == ["Home", "Reading Room", "Writing Desk"]
    for href, _ in ds._NARROW_LINKS:
        assert href not in ds._FULL_ONLY_LINKS


def test_documents_has_a_subnav_with_exactly_the_four_document_surfaces():
    # Blog Posts joined as the fourth on 2026-09-24 (the operator).
    html = ds.nav_component("/reference", "full")
    assert "Documents" in html
    assert 'href="/reference" aria-current="page"' in html
    assert "atelier-subnav" in html
    labels = [lbl for _, lbl in ds._DOCUMENTS_SUBNAV]
    assert labels == ["Knowledge base", "Reading Room", "Writing Desk", "Blog Posts"]


def test_blog_posts_lights_documents_and_only_its_own_subnav_entry():
    """/reference/blog-posts sits under the /reference prefix, so the check is
    that the Knowledge base entry (/reference) does not light alongside it."""
    html = ds.nav_component("/reference/blog-posts", "full")
    assert 'aria-label="Documents navigation"' in html
    sub = html.split('aria-label="Documents navigation"', 1)[1]
    assert 'href="/reference/blog-posts" aria-current="page"' in sub
    assert 'href="/reference" aria-current="page"' not in sub


def test_writing_desk_is_a_documents_subnav_entry_for_full_tier():
    html = ds.nav_component("/writing-desk", "full")
    assert 'href="/reference" aria-current="page"' in html  # Documents primary lit
    assert 'aria-label="Documents navigation"' in html
    assert 'href="/writing-desk" aria-current="page"' in html  # subnav entry lit


def test_writing_desk_stays_a_narrow_tier_primary_with_no_subnav():
    html = ds.nav_component("/writing-desk", "contractor")
    assert "Writing Desk" in html
    assert 'href="/writing-desk" aria-current="page"' in html
    assert "atelier-subnav" not in html


def test_products_dissolves_into_the_chart_house_subnav():
    assert "/products/release-notes" not in dict(ds._DEFAULT_LINKS)
    html = ds.nav_component("/products/release-notes", "full")
    assert 'href="/chart-house" aria-current="page"' in html
    assert 'aria-label="Chart House navigation"' in html
    assert 'href="/products/release-notes" aria-current="page"' in html
    order = [h for h, _ in ds._CHART_HOUSE_SUBNAV]
    assert order.index("/products/release-notes") == order.index("/engineering/changelog") + 1


def test_dashboards_subnav_is_trimmed_to_the_pinned_five():
    """§4 of the nav-reshape spec (issue_01M0TKX6DW2E1TXGN9QT6K61J5) lands the
    trim `test_dashboards_subnav_stays_whole_until_the_grouped_index_exists`
    (this test's former self) held off: the grouped Overview index now
    carries the seven dashboards this drops, so the sub-strip can shrink to
    the operator's confirmed pinned five without stranding a page — see
    `test_no_full_tier_dashboard_is_reachable_only_by_typing_its_url`, which
    polices that every one of the seven still has a working route in."""
    labels = [lbl for _, lbl in ds._DASHBOARD_SUBNAV]
    assert labels == ["Overview", "Spending", "Velocity", "Security", "Alerts"]
    html = ds.nav_component("/overview", "full")
    for label in ("Cost-Performance", "Activity", "Code Health", "Warrant",
                  "Tech Debt", "KB Retrieval", "E2E"):
        assert f">{label}<" not in html


def _stranded_dashboards(subnav, only_paths, card_body):
    """Dashboard routes with no sub-strip entry AND no /overview card."""
    import re
    in_subnav = {href for href, _ in subnav}
    carded = set(re.findall(r'class="overview-card" href="([^"]+)"', card_body))
    return sorted((in_subnav | set(only_paths)) - in_subnav - carded)


def test_no_full_tier_dashboard_is_reachable_only_by_typing_its_url():
    """The invariant behind the test above, as the property it protects rather
    than a list of labels: every dashboard route has SOME way in, either its
    own sub-strip entry or a card on /overview."""
    import overview_build
    stranded = _stranded_dashboards(
        ds._DASHBOARD_SUBNAV, ds._DASHBOARD_ONLY_PATHS,
        overview_build._build_body())
    assert stranded == [], f"no nav entry and no /overview card: {stranded}"


def test_dropped_dashboards_still_resolve_to_the_overview_area():
    # The trap the spec calls out by name: _DASHBOARD_PATHS is DERIVED from
    # the (now trimmed) subnav, so a dashboard that left the subnav would
    # silently stop resolving to /overview without _DASHBOARD_ONLY_PATHS.
    for page in ("/cost-performance", "/activity", "/health", "/warrant",
                "/engineering/e2e", "/kb"):
        html = ds.nav_component(page, "full")
        assert 'href="/overview" aria-current="page"' in html
        assert 'aria-label="Dashboards navigation"' in html


def test_briefings_resolves_to_archives_not_documents():
    assert ds._active_area("/reference/briefings") == "/archives"


def test_arch_log_resolves_to_documents_not_archives():
    # No longer in _ARCHIVES_SUBNAV (the operator, 2026-08-26), so it falls through to
    # the /reference prefix fallback like any other page under that URL space.
    assert ds._active_area("/reference/arch-log") == "/reference"


def test_room_and_writing_desk_and_memory_all_resolve_to_documents():
    for page in ("/room", "/writing-desk", "/reference"):
        assert ds._active_area(page) == "/reference"


# --- awaiting_state and stalled_state: the honesty fixes ---------------------
#
# The two false placeholders (issue_01M0TKX6DW2E1TXGN9QT6K61J5's reconciliation)
# each blamed the Library for a gap Orient owned, and both dates went stale
# unnoticed. These pin the three render states and the owner/escaping guards
# that stop that from silently recurring.


def _fix_now(monkeypatch, iso_date: str):
    """Pin `orient_time.now()` (which `awaiting_state` reads for staleness) to
    a fixed MST midnight on `iso_date`."""
    fixed = dt.datetime(*map(int, iso_date.split("-")), tzinfo=orient_time.MST)
    monkeypatch.setattr(orient_time, "now", lambda: fixed)


def test_awaiting_state_raises_on_an_invalid_owner():
    with pytest.raises(ValueError, match="owner"):
        ds.awaiting_state(shows="x", needs="y", owner="library", verified="2026-08-24")


def test_awaiting_state_holds_with_no_probe_and_a_recent_verified_date(monkeypatch):
    _fix_now(monkeypatch, "2026-08-25")
    html = ds.awaiting_state(shows="Live Bosun state.", needs="GET /runs on the Library",
                             owner="keel", verified="2026-08-24")
    assert 'class="awaiting-state holds"' in html
    assert "role=\"alert\"" not in html
    assert "claim last verified 2026-08-24" in html
    assert "WAITING ON THE KEEL — GET /runs on the Library" in html


def test_awaiting_state_stale_when_no_probe_and_verified_is_old(monkeypatch):
    _fix_now(monkeypatch, "2026-09-10")   # 17 days past 2026-08-24
    html = ds.awaiting_state(shows="Course progress.",
                             needs="a client for GET /courses", owner="orient",
                             verified="2026-08-24")
    assert 'class="awaiting-state stale"' in html
    assert "role=\"alert\"" not in html
    assert "unverified for 17 days" in html
    assert 'class="awaiting-stamp stale"' in html


def test_awaiting_state_stale_recolors_only_the_stamp_not_the_body(monkeypatch):
    """Visual decay would punish the reader for the author's staleness — the
    claim's own text must render exactly as it does when the claim holds."""
    _fix_now(monkeypatch, "2026-09-10")
    stale = ds.awaiting_state(shows="Course progress.", needs="a client for GET /courses",
                              owner="orient", verified="2026-08-24")
    _fix_now(monkeypatch, "2026-08-25")
    fresh = ds.awaiting_state(shows="Course progress.", needs="a client for GET /courses",
                              owner="orient", verified="2026-08-24")

    def shows_line(html):
        return html.split('<p class="awaiting-shows">')[1].split("</p>")[0]

    def badge_line(html):
        return html.split('<p class="awaiting-badge">')[1].split("</p>")[0]

    assert shows_line(stale) == shows_line(fresh)
    assert badge_line(stale) == badge_line(fresh)


def test_awaiting_state_holds_when_the_probe_confirms_the_claim():
    html = ds.awaiting_state(shows="Live Bosun state.", needs="GET /runs on the Library",
                             owner="keel", verified="2020-01-01",
                             probe=("/runs", 404),
                             probe_fn=lambda path, **k: 404)
    # A confirming probe holds even though `verified` is years stale — the
    # probe is a stronger check than the date, and re-verifies at render time.
    assert 'class="awaiting-state holds"' in html
    assert "claim verified at render: GET /runs → 404" in html


def test_awaiting_state_default_probe_fn_resolves_to_library_client(monkeypatch):
    """Every production call site (crews_build.py, signals_build.py) passes
    `probe=` and never `probe_fn`, so this is the branch that actually runs —
    `probe_fn` defaults to `library_client.probe_route`. Monkeypatching it is
    safe in a test process; the reason it would be unsafe in the gallery is
    that the gallery renders inside a concurrently-dispatched request
    handler, which a test does not."""
    monkeypatch.setattr(ds.library_client, "probe_route", lambda path, **k: 404)
    html = ds.awaiting_state(shows="Live Bosun state.", needs="GET /runs on the Library",
                             owner="keel", verified="2020-01-01",
                             probe=("/runs", 404))
    assert 'class="awaiting-state holds"' in html
    assert "claim verified at render: GET /runs → 404" in html


def test_awaiting_state_contradicted_when_the_probe_disagrees():
    html = ds.awaiting_state(shows="Live Bosun state.", needs="GET /runs on the Library",
                             owner="keel", verified="2026-08-24",
                             probe=("/runs", 404),
                             probe_fn=lambda path, **k: 200)
    assert 'class="awaiting-state contradicted"' in html
    assert 'role="alert"' in html
    assert "This panel claims to be waiting on GET /runs." in html
    assert "That route answered 200 today." in html
    assert "The claim is wrong, not the capability." in html


def test_awaiting_state_contradicted_by_self_check_skips_the_probe_and_the_date():
    """Some callers already hold, in the data they're rendering from, the
    value that would falsify their own claim — no route to poll. Passing
    `contradicted_by` must force `contradicted` immediately, regardless of
    how fresh `verified` still is, and must not touch `library_client` at
    all (no `probe`, so no monkeypatch needed for this to pass)."""
    html = ds.awaiting_state(shows="A gap.", needs="a feed that does not exist yet",
                             owner="keel", verified="2026-08-26",
                             contradicted_by="the field is populated now.")
    assert 'class="awaiting-state contradicted"' in html
    assert 'role="alert"' in html
    assert "the field is populated now." in html


def test_awaiting_state_rejects_probe_and_contradicted_by_together():
    with pytest.raises(ValueError):
        ds.awaiting_state(shows="x", needs="y", owner="keel", verified="2026-08-24",
                          probe=("/runs", 200), contradicted_by="already known")


def test_awaiting_state_rejects_probe_fn_and_contradicted_by_together():
    with pytest.raises(ValueError):
        ds.awaiting_state(shows="x", needs="y", owner="keel", verified="2026-08-24",
                          probe_fn=lambda path, **k: 200,
                          contradicted_by="already known")


def test_awaiting_state_probe_fn_and_contradicted_by_each_still_drive_their_own_branch():
    """Regression for the #216/#217 merge: `probe_fn` and `contradicted_by`
    were added by different PRs to the same function and each must still
    reach its own branch after the merge, independently of the other."""
    probed = ds.awaiting_state(shows="Live Bosun state.", needs="GET /runs on the Library",
                               owner="keel", verified="2026-08-24",
                               probe=("/runs", 404),
                               probe_fn=lambda path, **k: 200)
    assert 'class="awaiting-state contradicted"' in probed
    assert "That route answered 200 today." in probed

    self_checked = ds.awaiting_state(shows="A gap.", needs="a feed that does not exist yet",
                                     owner="keel", verified="2026-08-24",
                                     contradicted_by="the field is populated now.")
    assert 'class="awaiting-state contradicted"' in self_checked
    assert "the field is populated now." in self_checked


def test_awaiting_state_falls_back_to_the_verified_date_when_the_probe_cannot_answer(
        monkeypatch):
    """`probe_route` returns None on an unreachable Library — never raises —
    and a placeholder must degrade to the no-probe case rather than blank."""
    _fix_now(monkeypatch, "2026-08-25")
    html = ds.awaiting_state(shows="Live Bosun state.", needs="GET /runs on the Library",
                             owner="keel", verified="2026-08-24",
                             probe=("/runs", 404),
                             probe_fn=lambda path, **k: None)
    assert 'class="awaiting-state holds"' in html
    assert "claim last verified 2026-08-24" in html


@pytest.mark.parametrize("status", [500, 502, 503, 504, 429])
def test_awaiting_state_treats_a_library_failure_as_no_answer(monkeypatch, status):
    """A 5xx or a 429 is the Library FAILING, not the Library answering.

    Caught live while this PR was in review: the Library's vector index would
    not open, `GET /signals/search` answered 503, and the panel was one render
    from telling a reader "that route answered today, the claim is wrong"
    about a claim that was true. A server error is evidence about the server,
    never about what the route serves, so it can neither contradict nor
    confirm. The `verified` date decides alone, and the stamp names the status
    that could not settle it rather than pretending no probe ran."""
    _fix_now(monkeypatch, "2026-08-25")
    html = ds.awaiting_state(
        shows="Semantic search.",
        needs="a per-viewer wall over GET /signals/search",
        owner="orient", verified="2026-08-24",
        probe=("/signals/search?q=orient", 200),
        probe_fn=lambda path, **k: status)
    assert 'class="awaiting-state holds"' in html
    assert "contradicted" not in html
    assert 'role="alert"' not in html
    assert "could not re-verify" in html
    assert f"answered {status}" in html


def test_awaiting_state_a_library_failure_can_still_read_stale(monkeypatch):
    """The failing probe does not rescue a claim nobody has checked in weeks —
    it just stops it being called wrong. The date still decides."""
    _fix_now(monkeypatch, "2026-10-01")
    html = ds.awaiting_state(shows="Semantic search.", needs="a per-viewer wall",
                             owner="orient", verified="2026-08-24",
                             probe=("/signals/search", 200),
                             probe_fn=lambda path, **k: 503)
    assert 'class="awaiting-state stale"' in html
    assert "answered 503" in html


def test_awaiting_state_a_4xx_other_than_the_expected_one_still_contradicts():
    """The 5xx carve-out is narrow: a 4xx is the route answering, so it still
    settles the claim. A 403 where 404 was claimed means the route is there."""
    html = ds.awaiting_state(shows="Live Bosun state.", needs="GET /runs",
                             owner="keel", verified="2026-08-24",
                             probe=("/runs", 404),
                             probe_fn=lambda path, **k: 403)
    assert 'class="awaiting-state contradicted"' in html
    assert "That route answered 403 today." in html


def test_awaiting_state_escapes_every_interpolated_value():
    nasty = "<script>alert(1)</script>"
    html = ds.awaiting_state(shows=nasty, needs=nasty, owner="orient",
                             verified="2026-08-24")
    assert "<script>alert(1)</script>" not in html
    assert html.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 2  # shows + badge


def test_awaiting_state_owner_labels_are_distinct():
    keel = ds.awaiting_state(shows="x", needs="y", owner="keel", verified="2026-08-24")
    orient = ds.awaiting_state(shows="x", needs="y", owner="orient", verified="2026-08-24")
    assert "WAITING ON THE KEEL" in keel
    assert "WAITING ON ORIENT" in orient


def test_stalled_state_renders_above_the_content_it_qualifies():
    banner = ds.stalled_state(feed="CI test results", newest="2026-08-18, 6d ago",
                              expected_cadence="daily",
                              why="The CI write path was removed before its "
                                  "replacement existed.")
    tiles = '<div class="tile">a tile</div>'
    page = banner + tiles
    assert page.index("stalled-banner") < page.index("tile")
    assert "a tile" in page   # the banner sits above the data, never instead of it


def test_stalled_state_is_a_status_not_an_alert():
    html = ds.stalled_state(feed="CI", newest="x", expected_cadence="daily", why="y")
    assert 'role="status"' in html
    assert 'role="alert"' not in html


def test_stalled_state_composes_its_message_from_all_four_parts():
    html = ds.stalled_state(feed="CI test results", newest="2026-08-18, 6d ago",
                            expected_cadence="daily",
                            why="The CI write path was removed before its "
                                "replacement existed.")
    assert "Newest result 2026-08-18, 6d ago." in html
    assert "This feed expects a result daily." in html
    assert "The CI write path was removed before its replacement existed." in html


def test_stalled_state_escapes_every_interpolated_value():
    nasty = "<script>alert(1)</script>"
    html = ds.stalled_state(feed=nasty, newest=nasty, expected_cadence=nasty, why=nasty)
    assert "<script>alert(1)</script>" not in html


def test_the_phone_sub_strip_stays_sticky_so_its_offset_does_not_shift_it():
    """At <=767px the sub-strip was switched to `position:relative` for its fade,
    which turned `top:52px` from a stick offset into a 52px shift: on a phone
    it slid down over every page's title (the operator, 2026-09-24). No rule may make
    the sub-strip relative, since `top:52px` is only right on a sticky box."""
    import inspect
    import re
    src = inspect.getsource(ds)
    assert not re.search(r"\.atelier-subnav\s*\{[^}]*position\s*:\s*relative", src)
    assert re.search(r"\.atelier-subnav\s*\{[^}]*position\s*:\s*sticky[^}]*top\s*:\s*52px", src)


# --- auto-refresh on the spend pages (the operator, 2026-09-28) -------------------------


def test_page_shell_adds_auto_refresh_only_when_asked():
    on = ds.page_shell(title="t", nav_html="", body="", auto_refresh_seconds=900)
    assert '<span id="autoRefresh" data-seconds="900" hidden></span>' in on
    assert "convivy:beforerefresh" in on
    off = ds.page_shell(title="t", nav_html="", body="")
    assert 'id="autoRefresh"' not in off


def _drive_auto_refresh(scenario: str) -> dict:
    """Run the real AUTO_REFRESH_JS in node against stubbed globals: a clock
    the driver moves, a captured setInterval, and a visibility state and
    focused element it sets per scenario."""
    import json
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available to drive the inline JS")
    driver = r"""
var fs = require('fs');
var src = fs.readFileSync(process.argv[2], 'utf8');
var scenario = process.argv[3];
var now = 1000000, tick = null, listeners = {}, reloads = 0, events = [];
var doc = {
  visibilityState: scenario === 'hidden' ? 'hidden' : 'visible',
  activeElement: scenario === 'typing' ? { tagName: 'INPUT' } : { tagName: 'BODY' },
  getElementById: function(id) {
    return id === 'autoRefresh' ? { getAttribute: function() { return '900'; } } : null; },
  addEventListener: function(n, fn) { listeners[n] = fn; },
  dispatchEvent: function(e) { events.push(e.type); },
};
var sandbox = {
  document: doc, location: { reload: function() { reloads++; } },
  Date: { now: function() { return now; } },
  setInterval: function(fn) { tick = fn; },
  CustomEvent: function(t) { this.type = t; },
};
require('vm').runInNewContext(src, sandbox);
now += 14 * 60 * 1000; tick();
var early = reloads;
now += 2 * 60 * 1000; tick();
if (scenario === 'hidden') { doc.visibilityState = 'visible'; listeners.visibilitychange(); }
process.stdout.write(JSON.stringify({ early: early, reloads: reloads, events: events }));
"""
    with tempfile.TemporaryDirectory() as tmp:
        js, drv = f"{tmp}/auto.js", f"{tmp}/driver.js"
        with open(js, "w", encoding="utf-8") as f:
            f.write(ds.AUTO_REFRESH_JS)
        with open(drv, "w", encoding="utf-8") as f:
            f.write(driver)
        r = subprocess.run([node, drv, js, scenario], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)


def test_auto_refresh_reloads_once_the_interval_has_passed():
    result = _drive_auto_refresh("visible")
    assert result["early"] == 0, "no reload before 15 minutes"
    assert result["reloads"] == 1
    assert result["events"] == ["convivy:beforerefresh"]


def test_auto_refresh_waits_while_a_form_field_has_focus():
    """The alert-threshold form on /spending: a reload mid-typing loses it."""
    assert _drive_auto_refresh("typing")["reloads"] == 0


def test_auto_refresh_holds_a_hidden_tab_until_it_is_shown():
    """A backgrounded phone tab reloads when it comes back, not while hidden."""
    result = _drive_auto_refresh("hidden")
    assert result["reloads"] == 1
