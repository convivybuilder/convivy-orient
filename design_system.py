#!/usr/bin/env python3.11
"""Shared design-system module for convivy-orient (ARCH-216 Phase 1).

Single source of truth for the Atelier design system tokens, CSS, and
component helpers. Every builder imports from here; no per-page `:root` blocks.

Exports
-------
TOKENS        — CSS custom-property block (:root light + [data-theme=dark])
BASE_CSS      — reset, body/type, page scaffold
COMPONENTS_CSS — StatCard, RangeControl, Table, Tooltip, Drawer, state components
THEME_TOGGLE_JS — dark-mode toggle (localStorage + prefers-color-scheme)
RANGE_CONTROL_JS — RangeControl segmented group + day-stepper + custom-range popover
URL_STATE_JS  — shared URL state helper (readUrlState / writeUrlState / onUrlState)
NAV_CLUSTER_JS — back/forward/refresh cluster wired to the render_stamp footer
nav_component(current, tier, links) -> HTML string
stat_card(label, value, delta, delta_dir) -> HTML string
alert_threshold_form_html(key, title, fields) -> HTML string (dashboard alert-policy write form)
ledger_caps_form_html(project, caps) -> HTML string (a project's ledger-caps write form)
LEDGER_CAPS_WRITE_JS — shared JS for every ledger_caps_form_html instance on one page
range_control(id, selected, options) -> HTML string
page_shell(title, nav_html, body, extra_css, whoami_placeholder, rendered_at) -> complete HTML document
drawer_html(id, title, content) -> HTML string
empty_state(message) -> HTML string
loading_state(label) -> HTML string
error_state(message) -> HTML string
awaiting_state(shows, needs, owner, verified, probe, probe_fn, contradicted_by) -> HTML string
stalled_state(feed, newest, expected_cadence, why) -> HTML string
Freshness(text, stalled, cadence, reason) -> dataclass, one dashboard's freshness verdict
stalled_banner_html(feed, freshness) -> HTML string ("" unless freshness.stalled)
render_stamp(rendered_at) -> HTML string ("rendered <when>" footer, stamped by page_shell)
"""
from __future__ import annotations
import dataclasses
import datetime as dt
from collections.abc import Callable
from html import escape

import library_client
import orient_time

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------

TOKENS: str = r"""
:root {
  /* Background */
  --bg:#FBF8F4;
  --surface:#FFFFFF;
  --surface-2:#F5EFE8;
  --line:#ECE3D9;
  /* Type */
  --ink:#2B2320;
  --ink-2:#6B5F57;
  --ink-3:#8A7D73;
  /* Brand */
  --brand:#E85533;
  --brand-ink:#B23C1C;
  --brand-wash:#FCE9E0;
  /* Signals */
  --ok:#1F7A4D;
  --ok-wash:#E1F3E9;
  --warn:#8A5A0E;
  --warn-wash:#FBF0DA;
  --err:#B4301B;
  --err-wash:#FBE2DE;
  /* Gradient */
  --grad:linear-gradient(120deg,#E8542F 0%,#FF7A3D 52%,#FFA64D 100%);
  /* Chart */
  --chart-2:#F5A623;
  --chart-3:#E8792F;
  /* Hero scrim */
  --hero-scrim:linear-gradient(105deg, rgba(40,12,4,.46) 0%, rgba(40,12,4,.30) 45%, transparent 75%);
  /* Shadow */
  --shadow:0 1px 2px rgba(43,35,32,.06);
  /* Radius */
  --radius:10px;
  --radius-hero:14px;
  /* Typography */
  --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
}
[data-theme="dark"] {
  --bg:#15120F;
  --surface:#1E1A16;
  --surface-2:#272019;
  --line:#3E3128;
  --ink:#F2EBE3;
  --ink-2:#B8AB9F;
  --ink-3:#8A7D71;
  --brand:#FF8A5E;
  --brand-ink:#FFA585;
  --brand-wash:#3A1E12;
  --ok:#5FC98C;
  --ok-wash:#1B2E22;
  --warn:#E4B074;
  --warn-wash:#332A16;
  --err:#E87B66;
  --err-wash:#35201A;
  --chart-2:#F5B84A;
  --chart-3:#FF8A44;
  --grad:linear-gradient(120deg,#B83E1F 0%,#D85A2E 52%,#C77A2C 100%);
  --hero-scrim:linear-gradient(105deg, rgba(0,0,0,.50) 0%, rgba(0,0,0,.34) 45%, transparent 75%);
  --shadow:0 1px 2px rgba(0,0,0,.35);
}
"""

# ---------------------------------------------------------------------------
# Base CSS: reset, body, type scale, page scaffold
# ---------------------------------------------------------------------------

BASE_CSS: str = r"""
*,*::before,*::after { box-sizing:border-box; margin:0; padding:0; }
html { -webkit-text-size-adjust:100%; }
body {
  font-family:var(--sans);
  font-size:14px;
  line-height:1.55;
  color:var(--ink);
  background:var(--bg);
}
img, svg { display:block; max-width:100%; }
/* **[hidden] MUST WIN OVER EVERY display RULE, HERE AND ON EVERY PAGE.** An author
   `display:` declaration beats the UA stylesheet's `[hidden]{display:none}` outright,
   whatever the source order or selector specificity, so an element that carries the
   attribute AND matches any `display:` rule renders anyway and the attribute is inert.
   The line above is itself one: an <img> or <svg> with `hidden` would render without
   this. Nine pages learned the same lesson separately and each shipped a private copy
   of this rule; it belongs here, where the sheet every page loads can carry it once. */
[hidden] { display: none !important; }
a { color:var(--brand-ink); text-decoration:none; }
a:hover { text-decoration:underline; }
button { cursor:pointer; font:inherit; }
:focus-visible {
  outline: 2px solid var(--brand);
  outline-offset: 2px;
}

/* Page scaffold */
.page-wrap {
  max-width:1200px;
  margin:0 auto;
  padding:0 16px 48px;
}

/* Type scale */
.display   { font-size:24px; font-weight:700; line-height:1.2; }
h2,.h2     { font-size:17px; font-weight:650; line-height:1.3; }
h3,.h3     { font-size:13px; font-weight:650; text-transform:uppercase; letter-spacing:.04em; }
.small     { font-size:12px; }
.mono      { font-family:var(--mono); font-size:12.5px; }
.num       { font-variant-numeric:tabular-nums; }

/* Responsive breakpoints */
@media (max-width:640px) {
  .hide-sm { display:none !important; }
  .page-wrap { padding:0 12px 32px; }
}
@media (min-width:641px) and (max-width:1024px) {
  .page-wrap { padding:0 20px 40px; }
}
@media (prefers-reduced-motion:reduce) {
  *, *::before, *::after {
    animation-duration:.01ms !important;
    animation-iteration-count:1 !important;
    transition-duration:.01ms !important;
    scroll-behavior:auto !important;
  }
}
"""

# ---------------------------------------------------------------------------
# Components CSS
# ---------------------------------------------------------------------------

COMPONENTS_CSS: str = r"""
/* ---- Render stamp + nav cluster (every page, via page_shell) ----------- */
.render-stamp {
  margin-top:40px;
  padding-top:10px;
  border-top:1px solid var(--line);
  font-family:var(--mono);
  font-size:.7rem;
  letter-spacing:.06em;
  color:var(--ink-3);
  text-align:right;
  display:flex;
  align-items:center;
  justify-content:space-between;
  flex-wrap:wrap;
  gap:10px 16px;
}
@media print { .render-stamp { color:var(--ink-1); } .nav-cluster { display:none; } }
/* Back / forward / refresh, next to the stamp: the stamp is the evidence
   that a refresh actually reached the server (issue_01M1517CYMAV0GAEB2FYQQKF4N). */
.nav-cluster {
  display:inline-flex;
  align-items:center;
  gap:6px;
  font-family:var(--sans);
}
.nav-btn {
  padding:4px 9px;
  border:1px solid var(--line);
  border-radius:6px;
  background:var(--surface);
  color:var(--ink-2);
  font-size:13px;
  line-height:1;
  transition:background 120ms, color 120ms, opacity 120ms;
}
.nav-btn:hover:not(:disabled) { background:var(--surface-2); color:var(--ink); }
.nav-btn:disabled { opacity:.35; cursor:default; }
.refresh-note {
  font-size:.68rem;
  color:var(--warn);
}
.refresh-note:empty { display:none; }

/* ---- Sub-strip (second-level nav) ------------------------------------- */
.atelier-subnav {
  background:var(--surface);
  border-bottom:1px solid var(--line);
  position:sticky;
  top:52px;
  z-index:49;
}
.atelier-subnav-inner {
  max-width:1200px;
  margin:0 auto;
  padding:0 16px;
  height:38px;
  display:flex;
  align-items:center;
  gap:2px;
  overflow-x:auto;
  scrollbar-width:none;
}
.atelier-subnav-inner::-webkit-scrollbar { display:none; }
/* Right-edge fade hints at overflow tabs on narrow viewports. Static gradient
   (no animation) so prefers-reduced-motion is satisfied. pointer-events:none
   keeps all tabs clickable through the pseudo-element. */
@media (max-width:767px) {
  /* The sub-strip stays `position:sticky` here too. Sticky already positions
     the fade's `::after`, and an override to `position:relative` turned the
     `top:52px` below from a stick offset into a 52px shift, so on a phone the
     strip slid down over the page title and left an empty band above it
     (the operator, 2026-09-24). */
  .atelier-subnav::after {
    content:"";
    position:absolute;
    top:0;
    right:0;
    width:40px;
    height:100%;
    background:linear-gradient(to right, transparent, var(--bg));
    pointer-events:none;
    z-index:1;
  }
}
.atelier-subnav a {
  padding:4px 10px;
  font-size:13px;
  font-weight:500;
  color:var(--ink-2);
  border-radius:5px;
  text-decoration:none;
  white-space:nowrap;
  transition:background 120ms, color 120ms;
  flex-shrink:0;
}
.atelier-subnav a:hover { background:var(--surface-2); color:var(--ink); }
.atelier-subnav a[aria-current="page"] {
  color:var(--brand-ink);
  font-weight:650;
  text-decoration:underline;
  text-decoration-thickness:2px;
  text-underline-offset:3px;
}
@media (max-width:767px) {
  .atelier-subnav { top:52px; }
}

/* ---- Hero band --------------------------------------------------------- */
.hero {
  position:relative;
  margin:24px 0 0;
  border-radius:var(--radius-hero);
  overflow:hidden;
  background:var(--grad);
  color:#fff;
  box-shadow:0 8px 26px rgba(232,85,47,.20);
}
.hero-inner {
  padding:40px 44px 38px;
  position:relative;
  z-index:2;
}
.hero::before {
  content:"";
  position:absolute;
  inset:0;
  z-index:1;
  background:var(--hero-scrim);
}
.hero::after {
  content:"";
  position:absolute;
  inset:0;
  z-index:1;
  background:radial-gradient(120% 140% at 85% -10%, rgba(255,255,255,.22), transparent 55%);
}
/* The ship mark sits beside the hero's text in a flex row, so the text column
   narrows around it and no width can put the two on top of each other. Its 64px
   tile runs from the eyebrow's top to the h1 baseline, and its right edge holds
   the hero's 44px inner padding. The tile stays dark green because the ship's
   own colours vanish on the gradient's light corner (sage 1.04:1 in light, gold
   1.02:1 in dark); on the tile each reads at 4.25:1 or better. The ring is the
   `.hero-stat` border, so the tile reads as one more object on the same band. */
.hero-head {
  display:flex;
  align-items:flex-start;
  justify-content:space-between;
  gap:24px;
}
.hero-mark {
  flex:none;
  width:64px;
  height:64px;
  border-radius:14px;
  box-shadow:0 0 0 1px rgba(255,255,255,.22);
}
.hero-eyebrow {
  font-size:12px;
  font-weight:600;
  text-transform:uppercase;
  letter-spacing:.08em;
  opacity:.92;
  margin-bottom:10px;
}
.hero h1 {
  font-size:30px;
  font-weight:700;
  letter-spacing:-.02em;
  line-height:1.12;
  text-shadow:0 1px 12px rgba(120,30,15,.18);
  color:#fff;
}
.hero-sub {
  margin-top:10px;
  font-size:15px;
  max-width:520px;
  color:rgba(255,255,255,.92);
}
.hero-stats {
  display:grid;
  grid-template-columns:repeat(3,1fr);
  gap:16px;
  margin-top:28px;
}
/* A hero stat is an <a> to the dashboard behind its number, so it carries the
   link affordances the `.wtile` strip beside it already uses — same hero, same
   idiom, so the two read as one surface. Without `text-decoration` and `color`
   it would render underlined and link-blue against the gradient. The grid
   parent blockifies the anchor, so no display change is needed. */
.hero-stat {
  background:rgba(40,12,4,.50);
  border:1px solid rgba(255,255,255,.22);
  border-radius:var(--radius);
  padding:16px 18px 18px;
  backdrop-filter:blur(4px);
  text-decoration:none;
  color:#fff;
  transition:border-color .15s,transform .15s;
}
/* `text-decoration:none` is repeated here, not merely set on `.hero-stat`.
   The global `a:hover { text-decoration:underline }` above has specificity
   0,1,1 and outranks `.hero-stat`'s 0,1,0, so without this the label, value
   and delta all gain an underline on hover. `.qcard:hover`,
   `.overview-card:hover` and `.doc-item:hover` repeat it for the same reason. */
.hero-stat:hover {
  border-color:rgba(255,255,255,.55);
  transform:translateY(-1px);
  text-decoration:none;
}
.hero-stat:focus-visible { outline:2px solid #fff; outline-offset:2px; }
@media (prefers-reduced-motion:reduce) {
  .hero-stat { transition:none; }
}
.hero-stat .label {
  font-size:11.5px;
  font-weight:600;
  text-transform:uppercase;
  letter-spacing:.05em;
  color:rgba(255,255,255,.85);
}
.hero-stat .val {
  font-size:27px;
  font-weight:750;
  margin-top:7px;
  letter-spacing:-.01em;
  line-height:1.1;
  color:#fff;
  font-variant-numeric:tabular-nums;
}
.hero-stat .delta {
  font-size:12.5px;
  font-weight:500;
  margin-top:7px;
  display:flex;
  align-items:center;
  gap:4px;
  color:rgba(255,255,255,.80);
}
/* Hero search (contractor home) */
.hero-search {
  margin-top:24px;
  max-width:560px;
  position:relative;
}
.hero-search .ico {
  position:absolute;
  left:16px;
  top:50%;
  transform:translateY(-50%);
  font-size:16px;
  opacity:.7;
  pointer-events:none;
}
.hero-search input {
  width:100%;
  appearance:none;
  border:1px solid rgba(255,255,255,.32);
  background:rgba(255,255,255,.18);
  color:#fff;
  border-radius:11px;
  padding:13px 16px 13px 44px;
  font-size:14.5px;
  font-family:var(--sans);
  backdrop-filter:blur(4px);
}
.hero-search input::placeholder { color:rgba(255,255,255,.78); }
.hero-search input:focus-visible { outline:2px solid #fff; outline-offset:2px; }
/* Screen-reader-only label for hero search */
.sr-only {
  position:absolute;
  width:1px;
  height:1px;
  overflow:hidden;
  clip:rect(0 0 0 0);
  white-space:nowrap;
}

/* ---- Quick-link card grid ---------------------------------------------- */
.quick {
  display:grid;
  grid-template-columns:repeat(4,1fr);
  gap:16px;
}
.qcard {
  display:block;
  background:var(--surface);
  border:1px solid var(--line);
  border-radius:var(--radius);
  padding:20px;
  box-shadow:var(--shadow);
  transition:transform .15s, border-color .15s, box-shadow .15s;
  position:relative;
  overflow:hidden;
  text-decoration:none;
  color:var(--ink);
}
.qcard:hover {
  transform:translateY(-2px);
  border-color:color-mix(in srgb, var(--brand) 40%, var(--line));
  box-shadow:0 6px 18px rgba(43,35,32,.10);
  text-decoration:none;
}
.qcard:focus-visible { outline:2px solid var(--brand); outline-offset:2px; }
@media (prefers-reduced-motion:reduce) {
  .qcard { transition:none; }
}
.qicon {
  width:40px;
  height:40px;
  border-radius:11px;
  display:flex;
  align-items:center;
  justify-content:center;
  font-size:20px;
  background:var(--brand-wash);
  color:var(--brand-ink);
  margin-bottom:14px;
}
.qcard h3 { font-size:15px; font-weight:650; letter-spacing:-.01em; }
.qcard p { margin:6px 0 0; font-size:13px; color:var(--ink-2); line-height:1.45; }
.qmeta {
  margin-top:14px;
  font-size:12px;
  color:var(--ink-3);
  display:flex;
  align-items:center;
  gap:6px;
}
.pill {
  background:var(--surface-2);
  color:var(--ink-2);
  border-radius:20px;
  padding:2px 9px;
  font-weight:550;
  font-size:11.5px;
}
.qarrow {
  position:absolute;
  top:20px;
  right:20px;
  color:var(--ink-3);
  font-size:16px;
  transition:transform .15s, color .15s;
}
.qcard:hover .qarrow { transform:translateX(3px); color:var(--brand-ink); }
@media (prefers-reduced-motion:reduce) {
  .qcard:hover .qarrow { transform:none; }
}

/* ---- Two-column panel layout ------------------------------------------- */
.cols {
  display:grid;
  grid-template-columns:1.4fr 1fr;
  gap:24px;
  align-items:start;
}
.panel {
  background:var(--surface);
  border:1px solid var(--line);
  border-radius:var(--radius);
  box-shadow:var(--shadow);
  overflow:hidden;
}
.panel-head {
  padding:16px 20px;
  border-bottom:1px solid var(--line);
  display:flex;
  align-items:center;
  justify-content:space-between;
}
.panel-head h3 {
  font-size:13px;
  font-weight:650;
  text-transform:uppercase;
  letter-spacing:.04em;
  color:var(--ink-2);
}
.panel-more {
  font-size:13px;
  font-weight:550;
  color:var(--brand-ink);
  text-decoration:none;
}
.panel-more:hover { text-decoration:underline; }

/* ---- Activity feed ----------------------------------------------------- */
.feed { padding:6px 0; }
.feed-item {
  display:flex;
  gap:14px;
  padding:13px 20px;
  align-items:flex-start;
  border-bottom:1px solid var(--line);
  transition:background .12s;
}
.feed-item:last-child { border-bottom:none; }
.feed-item:hover { background:var(--surface-2); }
@media (prefers-reduced-motion:reduce) {
  .feed-item { transition:none; }
}
.feed-dot {
  flex:none;
  width:9px;
  height:9px;
  border-radius:50%;
  margin-top:7px;
}
.feed-dot.ok { background:var(--ok); }
.feed-dot.brand { background:var(--brand); }
.feed-dot.warn { background:var(--warn); }
.feed-dot.muted { background:var(--ink-3); }
.feed-body { flex:1; min-width:0; }
.feed-body .t { font-size:14px; color:var(--ink); }
.feed-body .t b { font-weight:650; }
.feed-body .m { font-size:12.5px; color:var(--ink-3); margin-top:2px; }
.feed-time { flex:none; font-size:12px; color:var(--ink-3); white-space:nowrap; }

/* ---- Doc-item list ----------------------------------------------------- */
.doc-item {
  display:flex;
  align-items:center;
  gap:13px;
  padding:13px 20px;
  border-bottom:1px solid var(--line);
  transition:background .12s;
  text-decoration:none;
  color:var(--ink);
}
.doc-item:last-child { border-bottom:none; }
.doc-item:hover { background:var(--surface-2); text-decoration:none; }
.doc-item:focus-visible { outline:2px solid var(--brand); outline-offset:-2px; }
@media (prefers-reduced-motion:reduce) {
  .doc-item { transition:none; }
}
.doc-ico {
  flex:none;
  width:32px;
  height:32px;
  border-radius:8px;
  background:var(--surface-2);
  display:flex;
  align-items:center;
  justify-content:center;
  font-size:15px;
  color:var(--ink-2);
}
.doc-body { flex:1; min-width:0; }
.doc-body .dt {
  font-size:14px;
  font-weight:550;
  overflow:hidden;
  text-overflow:ellipsis;
  white-space:nowrap;
}
.doc-body .dm { font-size:12px; color:var(--ink-2); margin-top:1px; }
.doc-tag {
  flex:none;
  font-size:11px;
  font-weight:600;
  text-transform:uppercase;
  letter-spacing:.03em;
  color:var(--brand-ink);
  background:var(--brand-wash);
  border-radius:20px;
  padding:3px 9px;
}

/* ---- Section helpers --------------------------------------------------- */
.home-section { margin-top:40px; }
.eyebrow {
  font-size:12px;
  font-weight:600;
  text-transform:uppercase;
  letter-spacing:.06em;
  color:var(--ink-2);
  margin-bottom:14px;
}
.home-foot {
  margin:48px 0 32px;
  padding-top:20px;
  border-top:1px solid var(--line);
  display:flex;
  justify-content:space-between;
  align-items:center;
  font-size:12.5px;
  color:var(--ink-3);
  flex-wrap:wrap;
  gap:8px;
}

/* ---- Overview hub stats grid ------------------------------------------ */
.overview-grid {
  display:grid;
  grid-template-columns:repeat(auto-fill, minmax(220px, 1fr));
  gap:16px;
  margin-top:24px;
}
.overview-card {
  display:block;
  background:var(--surface);
  border:1px solid var(--line);
  border-radius:var(--radius);
  padding:20px;
  box-shadow:var(--shadow);
  text-decoration:none;
  color:var(--ink);
  transition:transform .15s, border-color .15s, box-shadow .15s;
}
.overview-card:hover {
  transform:translateY(-2px);
  border-color:color-mix(in srgb, var(--brand) 40%, var(--line));
  box-shadow:0 6px 18px rgba(43,35,32,.10);
  text-decoration:none;
}
.overview-card:focus-visible { outline:2px solid var(--brand); outline-offset:2px; }
@media (prefers-reduced-motion:reduce) {
  .overview-card { transition:none; }
}
.overview-card-label {
  font-size:12px;
  font-weight:650;
  text-transform:uppercase;
  letter-spacing:.04em;
  color:var(--ink-2);
  margin-bottom:6px;
}
.overview-card-value {
  font-size:26px;
  font-weight:700;
  color:var(--ink);
  font-variant-numeric:tabular-nums;
  line-height:1.1;
}
.overview-card-delta {
  font-size:12px;
  color:var(--ink-2);
  margin-top:6px;
}
.overview-card-name {
  font-size:13px;
  font-weight:600;
  color:var(--brand-ink);
  margin-top:12px;
  display:flex;
  align-items:center;
  gap:4px;
}
.overview-card-freshness {
  font-family:var(--mono);
  font-size:11px;
  color:var(--ink-3);
  margin-top:6px;
}
.overview-group { margin-top:32px; }
.overview-group:first-of-type { margin-top:24px; }
.overview-group-title {
  font-size:13px;
  font-weight:650;
  text-transform:uppercase;
  letter-spacing:.04em;
  color:var(--ink-2);
  margin:0 0 12px;
}

/* ---- Responsive: hero + quick + cols ----------------------------------- */
@media (max-width:1024px) {
  .hero-stats { grid-template-columns:repeat(2,1fr); }
  .quick { grid-template-columns:repeat(2,1fr); }
  .cols { grid-template-columns:1fr; }
}
@media (max-width:640px) {
  .hero { margin-left:0; margin-right:0; border-radius:12px; }
  .hero-inner { padding:28px 22px; }
  .hero h1 { font-size:24px; }
  /* At 390px the date line already fills 89% of the text column, so a mark
     beside it would wrap the date; the header wordmark names Convivy Builder. */
  .hero-mark { display:none; }
  .hero-stats { grid-template-columns:repeat(2,1fr); gap:10px; }
  .hero-stat .val { font-size:21px; }
  .quick { grid-template-columns:1fr; }
}

/* ---- StatCard ---------------------------------------------------------- */
.statcard {
  background:var(--surface);
  border:1px solid var(--line);
  border-radius:var(--radius);
  padding:16px 20px;
  box-shadow:var(--shadow);
  display:flex;
  flex-direction:column;
  gap:4px;
}
.statcard-label {
  font-size:12px;
  font-weight:650;
  text-transform:uppercase;
  letter-spacing:.04em;
  color:var(--ink-2);
}
.statcard-value {
  font-size:24px;
  font-weight:700;
  color:var(--ink);
  font-variant-numeric:tabular-nums;
}
.statcard-delta {
  font-size:12px;
  color:var(--ink-2);
}
.statcard-delta.up   { color:var(--ok); }
.statcard-delta.down { color:var(--err); }
.statcard-delta.flat { color:var(--ink-3); }

/* ---- StatCard grid helper ---------------------------------------------- */
.statcards {
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(180px,1fr));
  gap:12px;
  margin:16px 0;
}

/* ---- RangeControl ------------------------------------------------------ */
.rangecontrol { display:flex; flex-direction:column; gap:8px; }
.segmented {
  display:inline-flex;
  border:1px solid var(--line);
  border-radius:var(--radius);
  overflow:hidden;
  background:var(--surface-2);
}
.segmented button {
  padding:6px 14px;
  font-size:13px;
  font-weight:500;
  color:var(--ink-2);
  background:transparent;
  border:none;
  border-right:1px solid var(--line);
  transition:background 120ms, color 120ms;
}
.segmented button:last-child { border-right:none; }
.segmented button[aria-pressed="true"] {
  background:var(--surface);
  color:var(--brand-ink);
  box-shadow:var(--shadow);
  font-weight:650;
}
.segmented button:hover:not([aria-pressed="true"]) {
  background:var(--surface);
  color:var(--ink);
}
/* Day stepper (shown when 1d/Today is selected) */
.day-stepper {
  display:none;
  align-items:center;
  gap:8px;
  font-size:13px;
  color:var(--ink);
}
.day-stepper.visible { display:flex; }
.day-stepper button {
  padding:4px 10px;
  border:1px solid var(--line);
  border-radius:6px;
  background:var(--surface);
  color:var(--ink);
  font-size:13px;
}
.day-stepper button:hover { background:var(--surface-2); }
.day-stepper .day-label {
  font-variant-numeric:tabular-nums;
  min-width:90px;
  text-align:center;
}
/* Custom range popover */
.custom-range-popover {
  display:none;
  flex-direction:column;
  gap:8px;
  padding:12px 16px;
  background:var(--surface);
  border:1px solid var(--line);
  border-radius:var(--radius);
  box-shadow:0 4px 12px rgba(0,0,0,.12);
  width:fit-content;
}
.custom-range-popover.open { display:flex; }
.custom-range-popover label {
  font-size:12px;
  font-weight:650;
  color:var(--ink-2);
  text-transform:uppercase;
  letter-spacing:.04em;
}
.custom-range-popover input[type="date"] {
  padding:6px 10px;
  border:1px solid var(--line);
  border-radius:6px;
  background:var(--surface);
  color:var(--ink);
  font-size:13px;
  font-family:var(--sans);
}
.custom-range-popover .apply-btn {
  padding:6px 14px;
  background:var(--brand);
  color:#fff;
  border:none;
  border-radius:6px;
  font-size:13px;
  font-weight:600;
  align-self:flex-end;
}
.custom-range-popover .apply-btn:hover { background:var(--brand-ink); }

/* ---- Table ------------------------------------------------------------- */
.data-table-wrap { overflow-x:auto; }
.data-table {
  width:100%;
  border-collapse:collapse;
  font-size:13px;
}
.data-table thead th {
  position:sticky;
  top:0;
  background:var(--surface);
  border-bottom:2px solid var(--line);
  padding:10px 12px;
  text-align:left;
  font-size:12px;
  font-weight:650;
  text-transform:uppercase;
  letter-spacing:.04em;
  color:var(--ink-2);
  white-space:nowrap;
  z-index:1;
}
.data-table tbody tr:nth-child(even) { background:var(--surface-2); }
.data-table tbody tr:hover { background:var(--brand-wash); }
.data-table td {
  padding:8px 12px;
  border-bottom:1px solid var(--line);
  color:var(--ink);
  font-variant-numeric:tabular-nums;
}
/* In-cell share bar */
.bar-cell { min-width:80px; }
.bar-track {
  height:6px;
  background:var(--surface-2);
  border-radius:3px;
  overflow:hidden;
}
.bar-fill {
  height:100%;
  background:var(--brand);
  border-radius:3px;
  transition:width 200ms;
}
/* Responsive: stack as cards on small screens */
@media (max-width:640px) {
  .data-table thead { display:none; }
  .data-table tr {
    display:flex;
    flex-direction:column;
    border:1px solid var(--line);
    border-radius:var(--radius);
    margin:8px 0;
    padding:10px 12px;
  }
  .data-table td {
    border:none;
    padding:3px 0;
  }
  .data-table td::before {
    content:attr(data-label) ": ";
    font-weight:650;
    color:var(--ink-2);
    font-size:11px;
    text-transform:uppercase;
  }
  /* A cell with no label (a totals row's caption, say) gets no prefix at all,
     rather than a bare ": " in front of its text. */
  .data-table td:not([data-label])::before { content:none; }
}

/* ---- Tooltip ----------------------------------------------------------- */
.tip {
  position:relative;
  cursor:help;
  border-bottom:1px dashed var(--ink-3);
}
.tip::after {
  content:attr(data-tip);
  position:absolute;
  bottom:calc(100% + 6px);
  left:50%;
  transform:translateX(-50%);
  background:var(--ink);
  color:var(--bg);
  font-size:12px;
  line-height:1.4;
  padding:6px 10px;
  border-radius:6px;
  white-space:nowrap;
  max-width:260px;
  white-space:normal;
  pointer-events:none;
  opacity:0;
  transition:opacity 160ms;
  z-index:100;
}
.tip:hover::after { opacity:1; }
.tip:focus-visible::after { opacity:1; }

/* ---- Drawer ------------------------------------------------------------ */
.drawer-backdrop {
  position:fixed;
  inset:0;
  background:rgba(0,0,0,.25);
  opacity:0;
  pointer-events:none;
  transition:opacity 200ms;
  z-index:200;
}
.drawer-backdrop.open { opacity:1; pointer-events:all; }
.drawer {
  position:fixed;
  top:0;
  right:0;
  width:min(480px,100vw);
  height:100vh;
  background:var(--surface);
  box-shadow:-4px 0 20px rgba(0,0,0,.15);
  transform:translateX(100%);
  transition:transform 200ms;
  z-index:201;
  display:flex;
  flex-direction:column;
  overflow:hidden;
}
.drawer.open { transform:translateX(0); }
.drawer-header {
  display:flex;
  align-items:center;
  justify-content:space-between;
  padding:16px 20px;
  border-bottom:1px solid var(--line);
}
.drawer-title { font-size:15px; font-weight:650; color:var(--ink); }
.drawer-close {
  border:none;
  background:none;
  font-size:20px;
  color:var(--ink-2);
  padding:4px 8px;
  border-radius:6px;
  line-height:1;
}
.drawer-close:hover { background:var(--surface-2); }
.drawer-body {
  flex:1;
  overflow-y:auto;
  overflow-x:hidden;   /* wide content (tables, code) scrolls within its own box, never the drawer edge */
  padding:20px;
}

/* ---- State components -------------------------------------------------- */
.empty-state, .loading-state, .error-state {
  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  gap:8px;
  padding:48px 24px;
  text-align:center;
  color:var(--ink);
  font-size:14px;
}
.empty-state .state-icon  { font-size:28px; opacity:.5; }
.loading-state .state-icon { font-size:28px; opacity:.5; }
.error-state .state-icon  { font-size:28px; color:var(--err); }
/* Loading skeleton pulse */
.skeleton {
  background:linear-gradient(90deg, var(--surface-2) 25%, var(--line) 50%, var(--surface-2) 75%);
  background-size:200% 100%;
  animation:skeleton-pulse 1.4s ease-in-out infinite;
  border-radius:4px;
  height:14px;
  width:100%;
}
@keyframes skeleton-pulse {
  0%   { background-position:200% 0; }
  100% { background-position:-200% 0; }
}
@media (prefers-reduced-motion:reduce) {
  .skeleton { animation:none; background:var(--surface-2); }
}

/* ---- Awaiting state (the capability does not exist yet) ---------------- */
/* Calm by default: dashed border, no fill, muted ink — unmistakably not
   content and unmistakably not an error until the probe contradicts it. */
.awaiting-state {
  display:flex;
  flex-direction:column;
  gap:6px;
  padding:20px 22px;
  border:1px dashed var(--line);
  border-radius:var(--radius);
  background:transparent;
  color:var(--ink-2);
  font-size:13px;
}
.awaiting-glyph { font-size:16px; opacity:.5; }
.awaiting-shows { color:var(--ink); font-size:14px; }
.awaiting-badge {
  font-family:var(--mono);
  font-size:11.5px;
  letter-spacing:.04em;
  text-transform:uppercase;
  color:var(--ink-3);
}
.awaiting-stamp { font-family:var(--mono); font-size:11.5px; color:var(--ink-3); }
/* Only the stamp recolors when stale — the claim's body text does not decay,
   since visual decay would punish the reader for the author's staleness. */
.awaiting-stamp.stale {
  display:inline-block;
  color:var(--warn);
  background:var(--warn-wash);
  padding:2px 8px;
  border-radius:6px;
  width:fit-content;
}
/* Contradicted flips the whole block to the error family's tone. */
.awaiting-state.contradicted {
  border-style:solid;
  border-color:transparent;
  background:var(--err-wash);
  color:var(--err);
}
.awaiting-state.contradicted .awaiting-shows,
.awaiting-state.contradicted .awaiting-badge,
.awaiting-state.contradicted .awaiting-stamp { color:var(--err); }

/* ---- Stalled state (the capability is live and its feed died) ---------- */
/* A banner ABOVE the real data, never instead of it — the tiles keep
   rendering underneath. */
.stalled-banner {
  display:flex;
  align-items:flex-start;
  gap:10px;
  margin-bottom:16px;
  padding:12px 16px;
  border-radius:var(--radius);
  background:var(--warn-wash);
  color:var(--warn);
  font-size:13px;
}
.stalled-glyph { font-size:15px; flex:none; }

/* ---- Atelier nav ------------------------------------------------------- */
.atelier-nav {
  position:sticky;
  top:0;
  z-index:50;
  background:color-mix(in srgb, var(--surface) 90%, transparent);
  backdrop-filter:blur(8px);
  -webkit-backdrop-filter:blur(8px);
  border-bottom:1px solid var(--line);
}
.atelier-nav-inner {
  max-width:1200px;
  margin:0 auto;
  padding:0 16px;
  height:52px;
  display:flex;
  align-items:center;
  gap:0;
}
.brand-mark {
  display:flex;
  align-items:center;
  gap:8px;
  text-decoration:none;
  margin-right:24px;
  flex-shrink:0;
}
.brand-dot {
  width:22px;
  height:22px;
  border-radius:5px;
  background:var(--grad);
  flex-shrink:0;
}
.brand-name {
  font-size:15px;
  font-weight:700;
  color:var(--ink);
  letter-spacing:-.01em;
}
/* The middle dot separates the brand from the surface so the lockup doesn't
   read as one three-word title; it copies Deck's title bar. */
.brand-sep {
  font-size:15px;
  font-weight:400;
  color:var(--ink);
}
.brand-surface {
  font-size:15px;
  font-weight:400;
  color:var(--ink);
  letter-spacing:-.01em;
}
.atelier-nav-links {
  display:flex;
  align-items:center;
  gap:2px;
  flex:1;
}
.atelier-nav-links a {
  padding:6px 10px;
  font-size:14px;
  font-weight:500;
  color:var(--ink-2);
  border-radius:6px;
  text-decoration:none;
  transition:background 120ms, color 120ms;
  white-space:nowrap;
}
.atelier-nav-links a:hover { background:var(--surface-2); color:var(--ink); }
.atelier-nav-links a[aria-current="page"] {
  background:var(--brand-wash);
  color:var(--brand-ink);
  font-weight:650;
}
.atelier-nav-right {
  display:flex;
  align-items:center;
  gap:8px;
  margin-left:auto;
  min-width:0;
}
.theme-toggle {
  padding:6px 8px;
  border:1px solid var(--line);
  border-radius:6px;
  background:transparent;
  color:var(--ink-2);
  font-size:14px;
  line-height:1;
  transition:background 120ms, color 120ms;
}
.theme-toggle:hover { background:var(--surface-2); color:var(--ink); }
.whoami-chip {
  font-size:12px;
  color:var(--ink-2);
  white-space:nowrap;
  overflow:hidden;
  text-overflow:ellipsis;
  max-width:200px;
}
/* "Convivy Builder · Orient" is wider than the old "Convivy". Below 900px
   the dot and the surface name drop, and min-width:0 on .atelier-nav-right
   lets the email chip shrink to its ellipsis rather than the menu button and
   theme toggle being pushed off the right edge. Measured with the dot from
   360px to 1280px: the page never scrolls sideways. */
@media (max-width:899px) {
  .brand-sep, .brand-surface { display:none; }
}
/* Mobile hamburger */
.hamburger-btn {
  display:none;
  padding:6px 8px;
  border:1px solid var(--line);
  border-radius:6px;
  background:transparent;
  color:var(--ink);
  font-size:18px;
  line-height:1;
  margin-left:8px;
}
@media (max-width:767px) {
  .hamburger-btn { display:block; }
  .atelier-nav-links {
    display:none;
    position:absolute;
    top:52px;
    left:0;
    right:0;
    flex-direction:column;
    align-items:flex-start;
    gap:0;
    background:var(--surface);
    border-bottom:1px solid var(--line);
    padding:8px;
    z-index:49;
  }
  .atelier-nav-links.open { display:flex; }
  .atelier-nav-links a { width:100%; border-radius:6px; }
}

/* ---- Alert-threshold write form (issue_01M17W6XGXVHNDWCFRTQ38Y2RK) ----- */
/* Shared by every dashboard settling its own alert policy — see
   alert_threshold_form_html below. One definition rather than three
   near-identical page-local copies. */
.athr-write {
  margin:0 0 18px; padding:12px 14px; border:1px solid var(--line);
  border-radius:var(--radius); background:var(--surface);
}
.athr-write[data-can-alerts-write="false"] { display:none; }
.athr-write h2 { font-size:12px; font-weight:700; color:var(--ink-3);
  text-transform:uppercase; letter-spacing:.03em; margin:0 0 10px; }
.athr-write-row { display:flex; flex-wrap:wrap; gap:14px; align-items:flex-end; }
.athr-write-row label { font-size:12px; color:var(--ink-3); font-weight:600; }
.athr-write-row input {
  display:block; margin-top:4px; font-family:var(--mono); font-size:13px;
  color:var(--ink); background:var(--bg); border:1px solid var(--line);
  border-radius:7px; padding:7px 9px; width:110px;
}
.athr-write-row button {
  font-family:var(--sans); font-size:13px; font-weight:600; color:var(--bg);
  background:var(--ink); border:none; border-radius:7px; padding:8px 16px;
  cursor:pointer;
}
.athr-note { margin-top:9px; font-size:12.5px; }
.athr-note.ok { color:var(--ok); }
.athr-note.err { color:var(--err); }

/* ---- Ledger-caps write form (issue_01M4BH8KSSMC8XBKB2C2CX9QTF) --------- */
/* One per project on the Spending page — see ledger_caps_form_html below. */
.ledger-caps { margin:10px 0 0; }
.ledger-caps[data-can-set-ledger-caps="false"] { display:none; }
.ledger-caps h4 { font-size:11px; color:var(--ink-2); text-transform:uppercase;
                  letter-spacing:.04em; margin:0 0 4px; }
.ledger-caps-row { display:flex; flex-wrap:wrap; gap:10px; align-items:flex-end; }
.ledger-caps-row label { font-size:12px; color:var(--ink-2); }
.ledger-caps-row input {
  display:block; margin-top:4px; font-family:var(--mono); font-size:13px;
  color:var(--ink); background:var(--bg); border:1px solid var(--line);
  border-radius:7px; padding:7px 9px; width:110px;
}
.ledger-caps-row button {
  font-family:var(--sans); font-size:13px; font-weight:600; color:var(--bg);
  background:var(--ink); border:none; border-radius:7px; padding:8px 16px;
  cursor:pointer;
}
.ledger-caps-hint { font-size:11.5px; margin:4px 0 0; }
.lcaps-note { font-size:12px; margin-top:4px; }
.lcaps-note.ok { color:var(--ok); }
.lcaps-note.err { color:var(--err); }
"""

# ---------------------------------------------------------------------------
# Theme toggle JS
# ---------------------------------------------------------------------------

THEME_TOGGLE_JS: str = r"""
(function() {
  var stored = localStorage.getItem('theme');
  var prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  var dark = stored === 'dark' || (!stored && prefersDark);
  if (dark) document.documentElement.setAttribute('data-theme', 'dark');

  function applyTheme(d) {
    if (d) {
      document.documentElement.setAttribute('data-theme', 'dark');
      localStorage.setItem('theme', 'dark');
    } else {
      document.documentElement.removeAttribute('data-theme');
      localStorage.setItem('theme', 'light');
    }
    var btn = document.getElementById('themeToggle');
    if (btn) {
      btn.textContent = d ? '☀' : '☾';
      btn.setAttribute('aria-pressed', d ? 'true' : 'false');
    }
  }

  document.addEventListener('DOMContentLoaded', function() {
    var btn = document.getElementById('themeToggle');
    if (btn) {
      btn.textContent = dark ? '☀' : '☾';
      btn.setAttribute('aria-pressed', dark ? 'true' : 'false');
      btn.addEventListener('click', function() {
        var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
        applyTheme(!isDark);
      });
    }
    // Mobile hamburger
    var menuBtn = document.getElementById('menuBtn');
    var navLinks = document.getElementById('navLinks');
    if (menuBtn && navLinks) {
      menuBtn.addEventListener('click', function() {
        var open = navLinks.classList.toggle('open');
        menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
    }
  });

  // Listen for prefers-color-scheme changes (only when user hasn't explicitly set)
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(e) {
      if (!localStorage.getItem('theme')) {
        applyTheme(e.matches);
      }
    });
  }
})();
"""

# ---------------------------------------------------------------------------
# RangeControl JS
# ---------------------------------------------------------------------------

RANGE_CONTROL_JS: str = r"""
(function() {
  function initRangeControls() {
    document.querySelectorAll('.rangecontrol').forEach(function(rc) {
      var id = rc.dataset.rcId;
      var segmented = rc.querySelector('.segmented');
      var dayStepperEl = rc.querySelector('.day-stepper');
      var dayLabel = rc.querySelector('.day-label');
      var prevBtn = rc.querySelector('[aria-label="Previous day"]');
      var nextBtn = rc.querySelector('[aria-label="Next day"]');
      var customPopover = rc.querySelector('.custom-range-popover');
      var applyBtn = customPopover && customPopover.querySelector('.apply-btn');
      var dateFrom = customPopover && customPopover.querySelector('.date-from');
      var dateTo = customPopover && customPopover.querySelector('.date-to');

      var dayIdx = 0;
      var dayList = [];
      // Build day list from today backwards 90 days
      var today = new Date();
      for (var i = 0; i < 90; i++) {
        var d = new Date(today);
        d.setDate(today.getDate() - i);
        dayList.unshift(d.toISOString().slice(0, 10));
      }
      dayIdx = dayList.length - 1;

      function updateDayLabel() {
        if (dayLabel && dayList[dayIdx]) {
          dayLabel.textContent = dayList[dayIdx];
        }
      }

      function setSelected(btn) {
        segmented.querySelectorAll('button').forEach(function(b) {
          b.setAttribute('aria-pressed', 'false');
        });
        btn.setAttribute('aria-pressed', 'true');
        var val = btn.dataset.rc;

        // Show/hide day stepper
        if (dayStepperEl) {
          if (val === '1d' || val === 'today') {
            dayStepperEl.classList.add('visible');
            dayIdx = dayList.length - 1;
            updateDayLabel();
          } else {
            dayStepperEl.classList.remove('visible');
          }
        }

        // Show/hide custom popover
        if (customPopover) {
          if (val === 'custom') {
            customPopover.classList.add('open');
          } else {
            customPopover.classList.remove('open');
          }
        }

        // Dispatch a custom event so pages can respond
        rc.dispatchEvent(new CustomEvent('rangechange', {
          bubbles: true,
          detail: { value: val, id: id }
        }));
      }

      if (segmented) {
        segmented.querySelectorAll('button').forEach(function(btn) {
          btn.addEventListener('click', function() { setSelected(btn); });
        });
      }

      // Day stepper
      if (prevBtn) {
        prevBtn.addEventListener('click', function() {
          if (dayIdx > 0) {
            dayIdx--;
            updateDayLabel();
            rc.dispatchEvent(new CustomEvent('daystep', {
              bubbles: true,
              detail: { date: dayList[dayIdx], id: id }
            }));
          }
        });
      }
      if (nextBtn) {
        nextBtn.addEventListener('click', function() {
          if (dayIdx < dayList.length - 1) {
            dayIdx++;
            updateDayLabel();
            rc.dispatchEvent(new CustomEvent('daystep', {
              bubbles: true,
              detail: { date: dayList[dayIdx], id: id }
            }));
          }
        });
      }

      // Custom range apply
      if (applyBtn) {
        applyBtn.addEventListener('click', function() {
          var from = dateFrom ? dateFrom.value : '';
          var to = dateTo ? dateTo.value : '';
          if (customPopover) customPopover.classList.remove('open');
          rc.dispatchEvent(new CustomEvent('customrange', {
            bubbles: true,
            detail: { from: from, to: to, id: id }
          }));
        });
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initRangeControls);
  } else {
    initRangeControls();
  }
})();
"""

# ---------------------------------------------------------------------------
# Drawer JS (minimal — focus trap + Esc close)
# ---------------------------------------------------------------------------

_DRAWER_JS: str = r"""
(function() {
  var FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';
  function getFocusable(el) {
    return Array.prototype.slice.call(el.querySelectorAll(FOCUSABLE)).filter(function(n) {
      return !n.hasAttribute('disabled') && n.offsetParent !== null;
    });
  }
  function initDrawers() {
    document.querySelectorAll('.drawer').forEach(function(drawer) {
      var backdrop = document.getElementById(drawer.id + '-backdrop');
      var closeBtn = drawer.querySelector('.drawer-close');
      var triggerEl = null;
      var hideTimer = null;
      // **THE SLIDE ANIMATION AND THE `hidden` ATTRIBUTE FIGHT EACH OTHER, AND
      // BOTH DIRECTIONS HAVE TO BE SEQUENCED.** A transition cannot run across a
      // display:none -> visible change made in the same style recalc, and it
      // cannot finish on an element that goes display:none partway through. Until
      // BASE_CSS carried [hidden]{display:none!important} the attribute was inert
      // here, the drawer was laid out continuously, and both directions animated
      // by accident. They now need saying explicitly.
      function open() {
        triggerEl = document.activeElement;
        if (hideTimer) { window.clearTimeout(hideTimer); hideTimer = null; }
        drawer.removeAttribute('hidden');
        // Read a layout property to flush the closed frame (translateX(100%)) to
        // the browser BEFORE .open flips the transform. Without this read the
        // drawer pops open instead of sliding.
        void drawer.offsetHeight;
        drawer.classList.add('open');
        if (backdrop) backdrop.classList.add('open');
        var focusable = getFocusable(drawer);
        if (focusable.length) focusable[0].focus();
      }
      function hideWhenClosed() {
        if (hideTimer) { window.clearTimeout(hideTimer); hideTimer = null; }
        drawer.removeEventListener('transitionend', hideWhenClosed);
        // Guarded on .open so a reopen during the slide-out does not hide the
        // drawer that just came back.
        if (!drawer.classList.contains('open')) drawer.setAttribute('hidden', '');
      }
      function close() {
        drawer.classList.remove('open');
        if (backdrop) backdrop.classList.remove('open');
        // `hidden` waits for the slide-out; setting it here would cut the
        // transition off at frame one.
        drawer.addEventListener('transitionend', hideWhenClosed);
        // **THE FALLBACK IS LOAD-BEARING, NOT BELT-AND-BRACES.** transitionend
        // never fires when no transition runs — reduced motion, a zero duration,
        // a browser that skips it — and without this the drawer would keep
        // role=dialog aria-modal=true in the accessibility tree for good, which
        // is the exact defect this rule exists to fix. Comfortably longer than
        // the 200ms in `.drawer`'s `transition:transform` above.
        hideTimer = window.setTimeout(hideWhenClosed, 400);
        if (triggerEl) triggerEl.focus();
        // A host with state tied to the drawer's contents needs to know it shut,
        // and the three ways to shut it (the button, the backdrop, Escape) all
        // land here rather than anywhere a host could listen. /reference uses
        // this to check a KB document back in when the drawer holding its open
        // editor closes. Dispatched after the close so a listener that reopens
        // the drawer wins, rather than being undone by the rest of this
        // function.
        drawer.dispatchEvent(new CustomEvent('ds:drawer-close', { bubbles: true }));
      }
      if (closeBtn) closeBtn.addEventListener('click', close);
      if (backdrop) backdrop.addEventListener('click', close);
      document.addEventListener('keydown', function(e) {
        if (!drawer.classList.contains('open')) return;
        if (e.key === 'Escape') { close(); return; }
        if (e.key === 'Tab') {
          var focusable = getFocusable(drawer);
          if (!focusable.length) { e.preventDefault(); return; }
          var first = focusable[0], last = focusable[focusable.length - 1];
          if (e.shiftKey) {
            if (document.activeElement === first) { e.preventDefault(); last.focus(); }
          } else {
            if (document.activeElement === last) { e.preventDefault(); first.focus(); }
          }
        }
      });
      // Wire up trigger buttons
      var triggerId = drawer.dataset.triggerId;
      if (triggerId) {
        var trigger = document.getElementById(triggerId);
        if (trigger) trigger.addEventListener('click', open);
      }
      // Expose open/close on the element for external use
      drawer._dsOpen = open;
      drawer._dsClose = close;
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initDrawers);
  } else {
    initDrawers();
  }
})();
"""

# ---------------------------------------------------------------------------
# URL state helper JS
# ---------------------------------------------------------------------------

URL_STATE_JS: str = r"""
(function(){
  function readUrlState() {
    var out = {};
    try {
      new URLSearchParams(location.search).forEach(function(v, k) { out[k] = v; });
    } catch(e) {}
    return out;
  }
  function writeUrlState(obj, replace) {
    try {
      var params = new URLSearchParams();
      Object.keys(obj).forEach(function(k) {
        var v = obj[k];
        if (v !== null && v !== undefined && v !== '') params.set(k, String(v));
      });
      var qs = params.toString();
      var url = location.pathname + (qs ? '?' + qs : '');
      if (replace) history.replaceState(null, '', url);
      else history.pushState(null, '', url);
    } catch(e) {}
  }
  function onUrlState(fn) {
    window.addEventListener('popstate', function() {
      try { fn(readUrlState()); } catch(e) {}
    });
  }
  window._ds = window._ds || {};
  window._ds.readUrlState = readUrlState;
  window._ds.writeUrlState = writeUrlState;
  window._ds.onUrlState = onUrlState;
})();
"""

# ---------------------------------------------------------------------------
# Nav cluster JS — back / forward / refresh, wired to the footer render_stamp
# builds (issue_01M1517CYMAV0GAEB2FYQQKF4N)
# ---------------------------------------------------------------------------

NAV_CLUSTER_JS: str = r"""
(function() {
  // Two independent jobs. (1) Track whether back/forward can really act:
  // there is no history.canGoBack(), and history.length counts the WHOLE
  // session's entries, not this tab's position among them, so this keeps
  // its own position/high-water-mark in sessionStorage, tagging each
  // history entry with the position it was created at (`__dsPos`) so a
  // later back/forward jump to that exact entry recovers it directly
  // rather than guessing how far the jump went. (2) Detect a refresh that
  // reached the browser but not the server's live render — e.g. server.py
  // falling back to the last-good static build on a renderer throw — by
  // comparing the render stamp's own timestamp before and after.
  //
  // Known gap: a page that pushes its OWN history entries before this
  // script's tagging can run (none do today; `_ds.writeUrlState`'s callers
  // all run from body-embedded scripts, which execute after this) would
  // start counting from whatever `pos` this script last saw, not from that
  // entry's true depth. history.pushState/replaceState are wrapped below
  // precisely so that everyday case (a dashboard's own filter state, pushed
  // well after this script runs) stays counted — and the `hashchange`
  // listener further below covers the other same-document path, a page
  // assigning `location.hash` directly (the ARCH log's jump-to-entry box),
  // since that creates a history entry without ever calling
  // pushState/replaceState and so is invisible to the wrap.
  var POS_KEY = 'convivy-nav-pos', MAX_KEY = 'convivy-nav-max';
  var REFRESH_KEY = 'convivy-refresh-prev';

  function getInt(key, fallback) {
    try {
      var v = sessionStorage.getItem(key);
      var n = v === null ? NaN : parseInt(v, 10);
      return isNaN(n) ? fallback : n;
    } catch (e) { return fallback; }
  }
  function setInt(key, value) {
    try { sessionStorage.setItem(key, String(value)); } catch (e) {}
  }
  function merge(state, extra) {
    var out = {};
    if (state && typeof state === 'object') {
      for (var k in state) { if (Object.prototype.hasOwnProperty.call(state, k)) out[k] = state[k]; }
    }
    for (var k2 in extra) { if (Object.prototype.hasOwnProperty.call(extra, k2)) out[k2] = extra[k2]; }
    return out;
  }

  var rawPos = null;
  try { rawPos = sessionStorage.getItem(POS_KEY); } catch (e) {}
  var firstEver = (rawPos === null);
  var pos = firstEver ? 0 : (parseInt(rawPos, 10) || 0);
  var max = firstEver ? 0 : getInt(MAX_KEY, 0);

  var navEntries = (window.performance && performance.getEntriesByType)
    ? performance.getEntriesByType('navigation') : [];
  var navType = navEntries.length ? navEntries[0].type : 'navigate';

  if (firstEver) {
    // No history of its own yet in this tab — pos/max are already 0/0.
  } else if (navType === 'back_forward') {
    if (history.state && typeof history.state.__dsPos === 'number') {
      pos = history.state.__dsPos;
    }
    // Else: this entry lost its tag somehow — keep the last known `pos`
    // rather than guess how far the jump went.
  } else if (navType !== 'reload') {
    // A genuinely new step: a link click or an address-bar entry. Any old
    // forward history is unreachable once a new step branches off from a
    // mid-stack position, exactly as a real browser's forward button goes
    // dead in the same situation, so `max` resets to here too.
    pos = pos + 1;
    max = pos;
  }
  try {
    history.replaceState(merge(history.state, {__dsPos: pos}), '', location.href);
  } catch (e) {}
  setInt(POS_KEY, pos);
  setInt(MAX_KEY, max);

  // Tracks the hash as of the last event this script processed, so the
  // popstate listener below can tell a same-document jump that crossed a
  // hash boundary from one that didn't. Updated on every path that can
  // change location.hash: pushState/replaceState (wrapped just below) and
  // the popstate/hashchange listeners further down.
  var lastHash = location.hash;

  // Wrap pushState/replaceState so a PAGE'S OWN same-document navigation
  // (the RangeControl / URL_STATE_JS filter state several dashboards push)
  // keeps counting as real steps, and so a page's own initial
  // `writeUrlState(..., true)` (a plain replaceState(null, ...)) cannot
  // wipe the tag this script just set.
  var origPush = history.pushState.bind(history);
  history.pushState = function(state, title, url) {
    pos = pos + 1;
    max = pos;
    origPush(merge(state, {__dsPos: pos}), title, url);
    lastHash = location.hash;
    setInt(POS_KEY, pos);
    setInt(MAX_KEY, max);
    updateButtons();
  };
  var origReplace = history.replaceState.bind(history);
  history.replaceState = function(state, title, url) {
    var extra = (state && typeof state === 'object' && typeof state.__dsPos === 'number')
      ? {} : {__dsPos: pos};
    origReplace(merge(state, extra), title, url);
    lastHash = location.hash;
  };

  // Landing on a hash-only difference via back/forward fires BOTH popstate
  // and hashchange for the same navigation (popstate first) — this flag
  // stops the hashchange handler below from double-counting that as a new
  // step on top of what popstate already recovered from __dsPos. Only set
  // when the arriving entry's hash genuinely differs from the departing one
  // (compared against `lastHash` above) — a same-document back/forward that
  // crosses no hash boundary (a dashboard's own pushState-based filter step)
  // fires no hashchange to ever clear an unconditionally-set flag, which
  // would otherwise stick true and silently swallow the next hashchange
  // from any source, for the rest of the page's life.
  var suppressNextHashchange = false;

  window.addEventListener('popstate', function() {
    suppressNextHashchange = (location.hash !== lastHash);
    lastHash = location.hash;
    var idx = (history.state && typeof history.state.__dsPos === 'number')
      ? history.state.__dsPos : pos;
    pos = idx;
    setInt(POS_KEY, pos);
    updateButtons();
  });

  // A page assigning `location.hash` directly — the ARCH log's jump-to-entry
  // box — creates a new history entry that inherits the LEAVING entry's own
  // __dsPos rather than getting a fresh one, since it never goes through the
  // pushState/replaceState wrap above. Re-tag it here, the same way the
  // wrap does, so this doesn't count as a genuinely new step twice via the
  // suppress flag when it was actually a back/forward landing.
  window.addEventListener('hashchange', function() {
    lastHash = location.hash;
    if (suppressNextHashchange) { suppressNextHashchange = false; return; }
    pos = pos + 1;
    max = pos;
    try {
      history.replaceState(merge(history.state, {__dsPos: pos}), '', location.href);
    } catch (e) {}
    setInt(POS_KEY, pos);
    setInt(MAX_KEY, max);
    updateButtons();
  });

  function updateButtons() {
    var back = document.getElementById('navBackBtn');
    var fwd = document.getElementById('navForwardBtn');
    if (back) back.disabled = !(pos > 0);
    if (fwd) fwd.disabled = !(pos < max);
  }

  function checkRefresh() {
    var stamp = document.getElementById('renderStamp');
    var note = document.getElementById('refreshNote');
    if (!stamp || !note) return;
    var prev = null;
    try { prev = sessionStorage.getItem(REFRESH_KEY); } catch (e) {}
    if (prev === null) return;
    try { sessionStorage.removeItem(REFRESH_KEY); } catch (e) {}
    if (prev === stamp.getAttribute('data-rendered-at')) {
      note.textContent = 'Refresh may not have reached the server — the render time did not change.';
    } else {
      note.textContent = '';
    }
  }

  function wireButtons() {
    var back = document.getElementById('navBackBtn');
    var fwd = document.getElementById('navForwardBtn');
    var refresh = document.getElementById('navRefreshBtn');
    var stamp = document.getElementById('renderStamp');
    if (back) back.addEventListener('click', function() { history.back(); });
    if (fwd) fwd.addEventListener('click', function() { history.forward(); });
    if (refresh && stamp) {
      refresh.addEventListener('click', function() {
        try { sessionStorage.setItem(REFRESH_KEY, stamp.getAttribute('data-rendered-at') || ''); } catch (e) {}
        // Pages with client-side filter state that should survive a
        // refresh (a request for newer data, not a fresh start) listen for
        // this and stash their own state right before the reload.
        document.dispatchEvent(new CustomEvent('convivy:beforerefresh'));
        refresh.disabled = true;
        location.reload();
      });
    }
    updateButtons();
    checkRefresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireButtons);
  } else {
    wireButtons();
  }
})();
"""

# ---------------------------------------------------------------------------
# Python component helpers
# ---------------------------------------------------------------------------

_DEFAULT_LINKS = [
    ("/", "Home"),
    # Dashboards moves up next to Home (the operator, 2026-08-28 —
    # issue_01M15C638Q3VPF38PNJAM1DGQV): "Let's put Dashboards between Home
    # and Chart House up top." Its entry stays otherwise unchanged — same
    # href, same label, same _FULL_ONLY_LINKS/_DASHBOARD_* membership — this
    # is a position change only.
    ("/overview", "Dashboards"),
    # Chart House's own base page (issue_01M0JFSVSGZW7A4RD77YNG395B): every
    # project's open work + a cost/ledger rollup, the area's front door.
    ("/chart-house", "Chart House"),
    # Crews (issue_01M08YM8MTNSTAEG1NV7E2GYRV): roster, active work, held
    # leases, and cost per dispatchable crew — a different lens on data Chart
    # House, Dashboards and Documents already hold, on purpose. New top-level
    # entry rather than nested under Chart House: it overlaps three areas at
    # once, so it doesn't subordinate cleanly to any one of them.
    ("/crews", "Crews"),
    ("/archives", "Archives"),
    # Documents (nav-reshape step 2, epic issue_01M0TKX6DW2E1TXGN9QT6K61J5):
    # the merged document vault, replacing three separate document-shaped
    # primaries (Reading Room, Writing Desk, Knowledge base) with the one
    # answer to "where do documents live" — mirroring the Keel's own vaults.
    # Landing on the knowledge base, same as before. "Documents" over
    # "Reference Room" (the Keel's own vault name) to avoid a Reference Room /
    # Reading Room collision (the operator, 2026-08-24).
    ("/reference", "Documents"),
]
# Reading Room (/room, ARCH-357) and Writing Desk (/writing-desk, epic
# issue_01M0EHQHQ7M39K811XKX1BAG98) are reachable by BOTH tiers: each gates its
# own content by authz role rather than by site tier (Writing Desk's
# __CAN_BLOG_WRITE__ panel, same mechanism the Reading Room used before the
# panel moved). For the full tier they are Documents subnav entries now, not
# primaries; the narrow tier has no subnav to find them in at all, so it keeps
# its own three-entry rail instead of a filtered slice of `_DEFAULT_LINKS`.
_NARROW_LINKS = [
    ("/", "Home"),
    ("/room", "Reading Room"),
    ("/writing-desk", "Writing Desk"),
]
_FULL_ONLY_LINKS = {"/chart-house", "/crews", "/archives", "/reference", "/overview"}

# The top-level nav reshape (epic issue_01M0EHQHQ7M39K811XKX1BAG98, 2026-08-19)
# and its follow-on, the vaults-mirror reshape (epic
# issue_01M0TKX6DW2E1TXGN9QT6K61J5, 2026-08-24). Every page below KEEPS its
# URL — only its nav grouping and label change, so neither reshape needed a
# server.py redirect.
#
# The second reshape's rule, corrected once by the operator mid-pass: group a page by
# what it READS, not by what it is about. Market and Briefings both rendered
# the signal store (`market_build` reads `kind=digest`; `/reference/briefings`
# rendered `kind=briefing` through the same `ops_reports_build._render` helper
# Notes uses) — that put them in Archives beside Signals and Notes, where a
# content-topic reading ("outside-world reading belongs with what a member
# reads") had put them in the Reading Room instead. Documents merges the three
# actual document surfaces — Knowledge base, Reading Room, Writing Desk — into
# one vault-mirroring primary, replacing three primaries with no single answer
# to "where do documents live."
#
# Superseded 2026-08-26 (the operator), for Briefings specifically: `/reference/briefings`
# moved off the signal store onto the KB index (ops_reports_build.py's own
# docstring), the same index Documents reads — so "group by what it reads" no
# longer places it. The operator's replacement rule is what it IS, not what it reads:
# Archives holds memories (records of what happened that nobody maintains as
# current truth — briefs, meeting notes, proposals, observations, feature
# requests), Documents holds canonical knowledge someone maintains. Briefings
# stays in Archives under this rule, for a different reason than it first
# landed there. Market has not been re-examined against the new rule and still
# rests on the superseded reasoning above.
#
#   Chart House (/chart-house) — every project's open work + a cost/ledger
#     rollup (issue_01M0JFSVSGZW7A4RD77YNG395B), the area's landing page.
#     Release Notes joins Changelog ("what shipped" beside "what merged") once
#     Products dissolves — it held exactly that one page.
#   Crews (/crews) — roster, active work, held leases and cost per
#     dispatchable crew (issue_01M08YM8MTNSTAEG1NV7E2GYRV).
#   Archives (/archives, renamed from /signals 2026-09-03 —
#     issue_01M1A678RA8CVDNNMZC6777NNA) — Signals, Notes & wrap-ups, ARCH Log,
#     Market and Briefings: memories, not canonical knowledge (see the
#     superseded-rule note above — Briefings is here for that reason now, not
#     because it reads the signal vault, which as of 2026-08-26 it no longer
#     does).
#   Documents (/reference, renamed from /memory 2026-09-03 — same issue) —
#     Knowledge base, Reading Room, Writing Desk: every
#     page that reads the document vault, together. Reading Room and Writing
#     Desk stay reachable by BOTH tiers (role-gated content, not tier-gated);
#     the narrow tier has no subnav to find them in, so it keeps its own
#     three-entry rail (`_NARROW_LINKS`) instead.
#   Dashboards (/overview) — trims to the operator's pinned set (Overview, Spending,
#     Velocity, Security, Alerts). The other seven keep their routes and
#     full-tier gating, reached through the grouped Overview index (§4 of the
#     nav-reshape spec, not yet built) rather than the subnav.

_CHART_HOUSE_SUBNAV = [
    ("/chart-house", "Overview"),
    # The Issues page became three (the operator, 2026-08-29 —
    # issue_01M1518ZFBGHAWC4Z46W893H2S): Epics keeps the /engineering/issues
    # route for link stability; Corrections sits adjacent to it, as asked;
    # Course follows. Nav label and page title changed; route paths and
    # `issue_build` function names did not (ADR-0060 renames the umbrella
    # from `issue` to `task`, and Orient carries both names until the
    # Library's rename lands).
    ("/engineering/issues", "Epics"),
    ("/engineering/corrections", "Corrections"),
    ("/engineering/course", "Course"),
    ("/engineering/merge-queue", "Merge queue"),
    ("/engineering/changelog", "Changelog"),
    ("/products/release-notes", "Release Notes"),
    # Blockers sits immediately after the lifecycle four rather than inside it.
    # ARCH-373 fixed the first three as the order work MOVES; this is the work
    # that is NOT moving — blocked on the operator — which is why it was invisible while
    # filed among issues that were moving. Backlog (feature-level work, same
    # lane mechanism) and Last night (an overnight retrospective) were retired
    # here (the operator, 2026-08-28): both were tied to structures the Chart House
    # replaced, and the fleet works interactively now, so there is no
    # retrospective story to tell. Blockers stays; it was explicitly not part
    # of that call.
    ("/engineering/blockers", "Blockers"),
]

# Crews (issue_01M08YM8MTNSTAEG1NV7E2GYRV): the base page leads, Roster
# (moved from Chart House) follows — the same "overview, then reference"
# shape Chart House itself uses. Policy (issue_01M1F1E59D38JMZ8WJVKH6W1AY,
# epic issue_01M0C91CGDR0YT8T5ERX2HD2HS) joins beside Roster: both answer
# "who can do what," Roster naming the agents and Policy naming what's denied
# to them for a given agent/crew/model triple.
_CREWS_SUBNAV = [
    ("/crews", "Overview"),
    ("/engineering/roster", "Roster"),
    ("/engineering/policy", "Policy"),
]

# Archives — the site's record-keeping, grouped by what each page IS: a
# memory (something read, not maintained), not canonical knowledge (epic
# issue_01M0TKX6DW2E1TXGN9QT6K61J5). The landing page leads, then the shapes
# The operator named as worth their own browsable, product-filterable section: PRs,
# Changelogs, then Briefings. Notes & wrap-ups, the ARCH Log and Market were
# trimmed from this strip (the operator, 2026-08-26): Notes' page is gone (its feeding
# jobs are dashboard inputs and stay on), ARCH is a frozen log (ARCH-441, no
# entry since 2026-08-03) with no front-page space, and Market renders zero
# cards (no customer-class signal traffic). /reference/arch-log keeps its
# route — 245 KB documents cite an ARCH-NNN id — it just isn't reachable from
# this strip. Originally this group was "every page that reads the signal
# vault" — no longer true of Briefings, which reads the KB index since
# 2026-08-26, but it stays here under the replacement rule above. Market
# still rests on the old reasoning and hasn't been re-examined.
#
# PRs and Changelogs (archives_build.py, 2026-08-26): two of the four shapes
# The operator named ("Briefings, PRs, Proposed Epics, Changelogs"). Proposed Epics is
# a separate change. It was blocked when this was written because the plane's
# `maturity` was unset on every epic, so the section would have needed an
# `awaiting_state` rather than a blank list. THAT IS NO LONGER TRUE: the
# fleet-wide epic-specification pass populated the field on 2026-08-27, and
# `GET /items?type=epic` measured 35 of 43 live epics carrying one that day.
# Re-measure before building rather than trusting this line — the count moves
# whenever a crew drafts or the operator approves, and it moved twice in the hours
# either side of it being written. `alert`
# (110 rows) and `finding` (49) were considered and left OUT: both stay
# reachable by kind-filtered enumeration through `library_client.list_signals`
# (no dedicated UI today, same as every other kind with no section), and 110
# rows is a real number against 19,000 pr_comment rows but not against a
# purpose-built section's overhead — see the PR body for the fuller argument.
#
# The landing entry's own label was "Signals" until issue_01M1A678RA8CVDNNMZC6777NNA
# (2026-09-03), which sat under a nav entry reading "Archives" — the same
# self-contradiction the route rename fixes. Both now read "Archives".
_ARCHIVES_SUBNAV = [
    ("/archives", "Archives"),
    ("/archives/prs", "PRs"),
    ("/archives/changelogs", "Changelogs"),
    ("/reference/briefings", "Briefings"),
]

# Documents (epic issue_01M0TKX6DW2E1TXGN9QT6K61J5): the corpus, the curated
# per-member view of it, the authoring desk, and the posts that desk writes
# (Blog Posts, the operator 2026-09-24) — the document surfaces, nothing else.
# "Documents" over "Reference Room" (the Keel's own
# vault name) to avoid a Reference Room / Reading Room collision (the operator,
# 2026-08-24) — that was a NAV-LABEL collision, not a route one: the route
# itself is /reference (renamed from /memory, issue_01M1A678RA8CVDNNMZC6777NNA,
# 2026-09-03), and nothing on the page reads "Reference Room". Market,
# Briefings and ARCH Log are memories, not canonical knowledge, so none of
# them are here regardless of which store each happens to read — see
# _ARCHIVES_SUBNAV and the superseded-rule note above it. /reference/arch-log
# has no sub-nav entry of its own any more (the operator, 2026-08-26) and resolves here
# only because `_active_area`'s /reference prefix fallback has to put it
# somewhere.
_DOCUMENTS_SUBNAV = [
    ("/reference", "Knowledge base"),
    ("/room", "Reading Room"),
    ("/writing-desk", "Writing Desk"),
    # Every Writing Desk post, readable in full (the operator, 2026-09-24). Under
    # /reference so `_active_area`'s prefix rule files it under Documents.
    ("/reference/blog-posts", "Blog Posts"),
]

# The end state the operator confirmed is a PINNED FIVE — Overview · Spending · Velocity
# · Security · Alerts — over a grouped `/overview` index (built in §4 of the
# nav-reshape spec, issue_01M0TKX6DW2E1TXGN9QT6K61J5) carrying the rest. The
# trim waited for that index to exist: `/overview` used to render five cards
# and link only three of the seven entries this trim removes, so trimming
# before the index shipped would have left /activity, /warrant
# and /engineering/e2e with no route from anywhere in the UI —
# /engineering/e2e is the page this epic OPENED on. Now that the grouped index
# carries all seven (see `overview_build._build_body`), both land together.
_DASHBOARD_SUBNAV = [
    ("/overview", "Overview"),
    ("/spending", "Spending"),
    ("/velocity", "Velocity"),
    ("/security", "Security"),
    # Alerts moved here from Chart House (epic issue_01M0EHRNW5HVJCAXZHYQCBZ3YW):
    # it now fires on Dashboards metrics crossing their own policy thresholds,
    # not a raw feed of alert-tagged signals, so it belongs with the data it's
    # derived from. /engineering/alerts 302s here via _MOVED (server.py).
    ("/alerts", "Alerts"),
]

# The other six dashboards — the operator's confirmed pinned set is the five above —
# keep their routes and full-tier gating; they left the subnav, not the site,
# and are reached through the grouped Overview index (`overview_build`)
# instead. Listed explicitly here, rather than left to `_DASHBOARD_PATHS`'s
# derivation, because `_active_area` must keep resolving every one of them to
# /overview even though none of them appear in `_DASHBOARD_SUBNAV` any more —
# a derived path set silently stops covering a page the moment that page
# leaves the list it's derived from, which is the failure this exists to
# catch.
_DASHBOARD_ONLY_PATHS: set[str] = {
    "/cost-performance", "/activity", "/health", "/warrant",
    "/engineering/e2e", "/kb",
}

# Each area's path set, DERIVED from its sub-strip rather than hand-listed.
# These were three parallel hand-maintained copies of the same information, and
# _MEMORY_PATHS (now _DOCUMENTS_PATHS) had already drifted — it was missing
# /memory/briefs. Deriving them removes the copy that can go stale.
# `_active_area` also falls back to a prefix match for each area, which is
# what had been silently covering that drift; `_DASHBOARD_PATHS` additionally
# widens with `_DASHBOARD_ONLY_PATHS` for the trimmed-subnav pages a prefix
# match cannot catch (see above).
_CHART_HOUSE_PATHS = {href for href, _ in _CHART_HOUSE_SUBNAV}
_CREWS_PATHS = {href for href, _ in _CREWS_SUBNAV}
_ARCHIVES_PATHS = {href for href, _ in _ARCHIVES_SUBNAV}
_DOCUMENTS_PATHS = {href for href, _ in _DOCUMENTS_SUBNAV}
_DASHBOARD_PATHS = {href for href, _ in _DASHBOARD_SUBNAV} | _DASHBOARD_ONLY_PATHS


def _active_area(current: str) -> str:
    """Return the top-level nav href that the given path belongs to, for the
    FULL tier. (The narrow tier has no grouping to resolve — see
    `nav_component`, which only calls this when `tier == "full"`.)

    Checked as explicit path sets rather than one `startswith("/engineering")`
    rule: /engineering/e2e now belongs to Dashboards, /engineering/notes to
    Archives, and /engineering/roster to Crews, so a page under the old
    Engineering URL space no longer implies Chart House the way it used to.
    `_CREWS_PATHS` is checked BEFORE the `startswith("/engineering")`
    fallback specifically because /engineering/roster would otherwise still
    match that prefix and misroute to Chart House — the one page this reshape
    moved OUT of the Engineering-prefixed area it still lives under a URL in.
    The fallback's own target is `/chart-house`, the area's landing page
    since issue_01M0JFSVSGZW7A4RD77YNG395B, not the `/engineering/issues` URL
    that used to double as both a sub-page and the top-level nav's own href.

    Archives is checked BEFORE Documents for the same reason: /reference/arch-log
    and /reference/briefings sit under the /reference URL prefix (renamed from
    /memory, issue_01M1A678RA8CVDNNMZC6777NNA) but are memories, not canonical
    knowledge (the operator 2026-08-26 — see the superseded-rule note above
    _ARCHIVES_SUBNAV), so they would otherwise be caught by the
    `startswith("/reference")` fallback below and misroute to Documents.
    /reference/arch-log has no sub-nav entry of its own any more and is left
    to that same fallback deliberately — a page that isn't in either strip
    has to land somewhere, and Documents is the more honest home for a
    /reference URL with no Archives listing to claim it.
    """
    if current in _DASHBOARD_PATHS or current == "/dashboard":
        return "/overview"
    if current in _ARCHIVES_PATHS or current.startswith("/archives"):
        return "/archives"
    if current in _DOCUMENTS_PATHS or current.startswith(("/room", "/reference")):
        return "/reference"
    if current in _CREWS_PATHS:
        return "/crews"
    if current in _CHART_HOUSE_PATHS or current.startswith("/engineering"):
        return "/chart-house"
    return "/"


def _subnav_html(items, current: str, label: str) -> str:
    """Render one area's sub-strip as an HTML string given its (href, label) list."""
    sub_items = []
    for href, item_label in items:
        aria = ' aria-current="page"' if href == current else ""
        sub_items.append(f'<a href="{escape(href)}"{aria}>{escape(item_label)}</a>')
    return (
        f'\n<nav class="atelier-subnav" aria-label="{escape(label)} navigation">'
        '<div class="atelier-subnav-inner">'
        + "".join(sub_items)
        + "</div></nav>"
    )


def nav_component(current: str, tier: str = "full", links=None) -> str:
    """Return the Atelier-style nav as an HTML string.

    Two levels: a sticky primary rail and an optional sub-strip. The two
    tiers get genuinely different rails, mirroring the Keel's own vaults for
    the full tier (Home · Dashboards · Chart House · Crews · Archives ·
    Documents; epics issue_01M0EHQHQ7M39K811XKX1BAG98,
    issue_01M08YM8MTNSTAEG1NV7E2GYRV and issue_01M0TKX6DW2E1TXGN9QT6K61J5)
    rather than a filtered slice of it for the narrow tier (Home · Reading
    Room · Writing Desk, `_NARROW_LINKS`) — the narrow tier's two document
    pages have no full-tier subnav to be found in, so they stay primaries of
    their own. Every full-tier primary has a sub-strip now (Documents,
    Chart House, Crews, Archives, Dashboards); the narrow tier never renders
    one, since its three pages are each already the full page.
    """
    if links is None:
        links = _DEFAULT_LINKS if tier == "full" else _NARROW_LINKS
    visible = [(h, lbl) for h, lbl in links
               if tier == "full" or h not in _FULL_ONLY_LINKS]
    # The narrow rail is three literal top-level paths with no grouping to
    # resolve, so it highlights on an exact match against `current` rather
    # than through `_active_area`'s full-tier grouping logic — which would
    # answer "/reference" for /room and /writing-desk, a primary the narrow rail
    # does not have.
    active_area = _active_area(current) if tier == "full" else current
    items = []
    for href, label in visible:
        aria = ' aria-current="page"' if href == active_area else ""
        items.append(
            f'<a href="{escape(href)}"{aria}>{escape(label)}</a>'
        )
    links_html = "\n        ".join(items)
    primary_nav = f"""<nav class="atelier-nav" aria-label="Main navigation">
  <div class="atelier-nav-inner">
    <a class="brand-mark" href="/" aria-label="Convivy Builder home">
      <div class="brand-dot" aria-hidden="true"></div>
      <span class="brand-name">Convivy Builder</span>
      <span class="brand-sep" aria-hidden="true">·</span>
      <span class="brand-surface">Orient</span>
    </a>
    <div class="atelier-nav-links" id="navLinks">
        {links_html}
    </div>
    <div class="atelier-nav-right">
      <button class="theme-toggle" id="themeToggle" aria-label="Toggle dark mode" aria-pressed="false">☾</button>
      <span class="whoami-chip small">__WHOAMI__</span>
      <button class="hamburger-btn" id="menuBtn"
              aria-label="Open menu" aria-controls="navLinks"
              aria-expanded="false">&#9776;</button>
    </div>
  </div>
</nav>"""
    subnav = ""
    if tier != "full":
        pass
    elif active_area == "/overview":
        subnav = _subnav_html(_DASHBOARD_SUBNAV, current, "Dashboards")
    elif active_area == "/chart-house":
        subnav = _subnav_html(_CHART_HOUSE_SUBNAV, current, "Chart House")
    elif active_area == "/crews":
        subnav = _subnav_html(_CREWS_SUBNAV, current, "Crews")
    elif active_area == "/archives":
        subnav = _subnav_html(_ARCHIVES_SUBNAV, current, "Archives")
    elif active_area == "/reference":
        subnav = _subnav_html(_DOCUMENTS_SUBNAV, current, "Documents")
    return primary_nav + subnav


_VALID_DELTA_DIRS = frozenset({"up", "down", "flat"})


def stat_card(label: str, value: str, delta: str | None = None,
              delta_dir: str | None = None) -> str:
    """Return a StatCard as an HTML string.

    label      — uppercase small label text
    value      — large numeric/text value
    delta      — optional delta line (e.g. "▲ +3%")
    delta_dir  — "up", "down", or "flat" for signal colour
    """
    delta_html = ""
    if delta is not None:
        safe_dir = delta_dir if delta_dir in _VALID_DELTA_DIRS else ""
        dir_cls = f" {safe_dir}" if safe_dir else ""
        delta_html = (
            f'\n    <div class="statcard-delta{dir_cls}">{escape(delta)}</div>'
        )
    return (
        f'<article class="statcard">\n'
        f'  <div class="statcard-label">{escape(label)}</div>\n'
        f'  <div class="statcard-value num">{escape(value)}</div>'
        f'{delta_html}\n'
        f'</article>'
    )


_ALERT_THRESHOLD_WRITE_JS = r"""
(function() {
  document.querySelectorAll(".athr-write").forEach(function(section) {
    if (section.dataset.canAlertsWrite !== "true") return;
    var key = section.dataset.key;
    var btn = section.querySelector(".athr-save");
    var note = section.querySelector(".athr-note");
    function setNote(cls, text) {
      note.className = "athr-note " + cls;
      note.textContent = text;
      note.hidden = false;
    }
    btn.addEventListener("click", function() {
      var values = { key: key };
      section.querySelectorAll(".athr-input").forEach(function(inp) {
        values[inp.dataset.field] = parseFloat(inp.value);
      });
      btn.disabled = true;
      fetch("/api/alerts-policy", {
        method: "POST", credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(values),
      }).then(function(r) {
        return r.json().catch(function() { return {}; }).then(function(j) {
          if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
          return j;
        });
      }).then(function() {
        setNote("ok", "Saved. Reloading…");
        window.location.reload();
      }).catch(function(err) {
        setNote("err", "Couldn't save: " + err.message);
      }).finally(function() { btn.disabled = false; });
    });
  });
})();
"""


def alert_threshold_form_html(key: str, title: str,
                              fields: list[tuple[str, str, float, str]]) -> str:
    """A small inline form for setting one dashboard alert threshold
    (issue_01M17W6XGXVHNDWCFRTQ38Y2RK), posting straight to
    `POST /api/alerts-policy`. Shared by every dashboard in scope
    (Code Health, Spending, KB Retrieval) rather than three near-identical
    copies, matching `policy_build.py`'s `_write_form_html` in shape.

    `data-can-alerts-write` is the `__CAN_ALERTS_WRITE__` substitution
    server.py applies from the viewer's role (matching
    `__CAN_POLICY_WRITE__`/`__CAN_KB_WRITE__`): cosmetic, since
    `alerts_policy_api.py`'s own gate (human, CSRF, `alerts.write`) is the
    wall regardless of what this attribute says.

    `fields` is `(name, label, current_value, step)` — `step` sets the
    `<input type=number>` increment (e.g. "0.01" for a rate, "1" for a
    count). Every field posts together under one `key`, and that set must be
    exactly `alert_policy_store.FIELDS[key]` — the one place a threshold's
    shape is defined — or the write route refuses it.
    """
    field_inputs = "".join(
        f'<label>{escape(label)}<br><input type="number" step="{escape(str(step), quote=True)}" '
        f'class="athr-input" data-field="{escape(name, quote=True)}" '
        f'value="{escape(str(value), quote=True)}"></label>'
        for name, label, value, step in fields
    )
    return (
        f'<section class="athr-write" data-can-alerts-write="__CAN_ALERTS_WRITE__" '
        f'data-key="{escape(key, quote=True)}">'
        f'<h2>{escape(title)}</h2>'
        f'<div class="athr-write-row">{field_inputs}'
        f'<button type="button" class="athr-save">Save</button></div>'
        f'<div class="athr-note" hidden></div>'
        f'</section>'
        f'<script>{_ALERT_THRESHOLD_WRITE_JS}</script>'
    )


LEDGER_CAPS_WRITE_JS = r"""
(function() {
  document.querySelectorAll(".ledger-caps").forEach(function(section) {
    if (section.dataset.canSetLedgerCaps !== "true") return;
    var project = section.dataset.project;
    var btn = section.querySelector(".lcaps-save");
    var note = section.querySelector(".lcaps-note");
    function setNote(cls, text) {
      note.className = "lcaps-note " + cls;
      note.textContent = text;
      note.hidden = false;
    }
    btn.addEventListener("click", function() {
      var values = {};
      section.querySelectorAll(".lcaps-input").forEach(function(inp) {
        values[inp.dataset.field] = inp.value.trim() === "" ? null : parseFloat(inp.value);
      });
      btn.disabled = true;
      fetch("/api/ledgers/" + encodeURIComponent(project) + "/caps", {
        method: "POST", credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(values),
      }).then(function(r) {
        return r.json().catch(function() { return {}; }).then(function(j) {
          if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
          return j;
        });
      }).then(function() {
        setNote("ok", "Saved. Reloading…");
        window.location.reload();
      }).catch(function(err) {
        setNote("err", "Couldn't save: " + err.message);
      }).finally(function() { btn.disabled = false; });
    });
  });
})();
"""


def ledger_caps_form_html(project: str, caps: dict) -> str:
    """A project's ledger-caps write form (issue_01M4BH8KSSMC8XBKB2C2CX9QTF),
    posting straight to `POST /api/ledgers/{project}/caps`. One section per
    project on the Spending page; `LEDGER_CAPS_WRITE_JS` is shared across
    every instance and must be emitted exactly once per page, the way
    `alert_threshold_form_html` is not — that one is called once per
    dashboard, this one once per project, so the script lives apart from the
    markup rather than riding along with it (a copy per project would attach
    its listeners again on every row already rendered, firing a save once
    per project on the page by the time the last one loads).

    `caps` is `{warn_usd, soft_cap_usd, hard_cap_usd, per_run_cap_usd}`, each
    `float | None`; `None` renders as an empty field, and the form says
    blank means no cap. `data-can-set-ledger-caps` is the `__CAN_SET_LEDGER_CAPS__`
    substitution server.py applies from the viewer's role: cosmetic, since
    `ledger_caps_api.py`'s own gate (human, CSRF, `ledger.caps_write`) is the
    wall regardless of what this attribute says.
    """
    fields = [
        ("warn_usd", "Warn"), ("soft_cap_usd", "Soft cap"),
        ("hard_cap_usd", "Hard cap"), ("per_run_cap_usd", "Per-run cap"),
    ]
    field_inputs = "".join(
        f'<label>{escape(label)}<br><input type="number" step="0.01" min="0" '
        f'class="lcaps-input" data-field="{escape(name, quote=True)}" '
        f'value="{escape("" if caps.get(name) is None else str(caps[name]), quote=True)}" '
        f'placeholder="no cap"></label>'
        for name, label in fields
    )
    return (
        f'<div class="ledger-caps" data-can-set-ledger-caps="__CAN_SET_LEDGER_CAPS__" '
        f'data-project="{escape(project, quote=True)}">'
        f'<h4>Caps</h4>'
        f'<div class="ledger-caps-row">{field_inputs}'
        f'<button type="button" class="lcaps-save">Save</button></div>'
        f'<p class="muted ledger-caps-hint">Blank means no cap. Saving sends all four — '
        f'a field left blank clears that cap.</p>'
        f'<div class="lcaps-note" hidden></div>'
        f'</div>'
    )


_DEFAULT_RANGE_OPTIONS = [
    ("1d", "Today"),
    ("7d", "7d"),
    ("30d", "30d"),
    ("90d", "90d"),
    ("custom", "Custom…"),
]


def range_control(id: str = "rc", selected: str = "7d",
                  options=None) -> str:
    """Return a RangeControl as an HTML string.

    Produces real <button> elements with aria-pressed, a day stepper (shown
    when 1d/today is selected), and a custom-range date popover.
    """
    if options is None:
        options = _DEFAULT_RANGE_OPTIONS
    buttons = []
    for val, label in options:
        pressed = "true" if val == selected else "false"
        buttons.append(
            f'<button type="button" data-rc="{escape(val)}"'
            f' aria-pressed="{pressed}">{escape(label)}</button>'
        )
    buttons_html = "\n    ".join(buttons)
    return f"""<div class="rangecontrol" data-rc-id="{escape(id)}">
  <div class="segmented" role="group" aria-label="Date range">
    {buttons_html}
  </div>
  <div class="day-stepper" aria-live="polite">
    <button type="button" aria-label="Previous day">&#9664;</button>
    <span class="day-label num"></span>
    <button type="button" aria-label="Next day">&#9654;</button>
  </div>
  <div class="custom-range-popover" role="group" aria-label="Custom date range">
    <label>From<br><input class="date-from" type="date"></label>
    <label>To<br><input class="date-to" type="date"></label>
    <button type="button" class="apply-btn">Apply</button>
  </div>
</div>"""


def drawer_html(id: str, title: str, content: str) -> str:
    """Return an accessible drawer as an HTML string.

    content is treated as trusted HTML (callers compose HTML; it is not escaped).
    Phase-3 callers must escape any untrusted data before passing it as content.

    Includes role=dialog, aria-modal, aria-labelledby, a close button, and
    a wired backdrop. The drawer JS in page_shell handles open/close + Esc.
    """
    label_id = f"{escape(id)}-title"
    return f"""<div id="{escape(id)}-backdrop" class="drawer-backdrop" aria-hidden="true"></div>
<div id="{escape(id)}" class="drawer" role="dialog" aria-modal="true"
     aria-labelledby="{label_id}" hidden>
  <div class="drawer-header">
    <div class="drawer-title" id="{label_id}">{escape(title)}</div>
    <button class="drawer-close" aria-label="Close drawer">&times;</button>
  </div>
  <div class="drawer-body">{content}</div>
</div>"""


def ship_mark() -> str:
    """Return the Convivy Builder ship on its dark-green tile, as inline SVG.

    Redrawn from the convivybuilder GitHub organisation's 460px avatar in its own
    coordinates: four sage masts, a cream hull crossed by a ground-green stripe,
    and a gold keel. The stripe overruns the hull's ends, where it lands on the
    tile in the same green. It is decorative, so it is hidden from assistive tech.
    """
    return (
        '<svg class="hero-mark" viewBox="0 0 460 460" aria-hidden="true" focusable="false">'
        '<rect width="460" height="460" rx="100" fill="#22382C"/>'
        '<path d="M137.5 170V240M197.5 151V240M257.5 141V240M316.5 124V240"'
        ' stroke="#8FC7A6" stroke-width="20" stroke-linecap="round"/>'
        '<path d="M78 208Q241 256.5 400 143C395 200 365 270 300 294H120'
        'C95 280 85 240 78 208Z" fill="#E9E5D8"/>'
        '<path d="M88 257Q237 281.5 386 211" fill="none" stroke="#22382C" stroke-width="8"/>'
        '<path d="M118 304.5H300Q345 304.5 380 271" fill="none" stroke="#C9892A"'
        ' stroke-width="17" stroke-linecap="round"/>'
        '</svg>'
    )


def empty_state(message: str) -> str:
    """Return an empty-state block as an HTML string."""
    return (
        f'<div class="empty-state">'
        f'<div class="state-icon" aria-hidden="true">&#8709;</div>'
        f'<p>{escape(message)}</p>'
        f'</div>'
    )


def loading_state(label: str) -> str:
    """Return a loading skeleton block as an HTML string."""
    return (
        f'<div class="loading-state" aria-live="polite" aria-busy="true">'
        f'<div class="state-icon" aria-hidden="true">&#8987;</div>'
        f'<p>{escape(label)}</p>'
        f'<div class="skeleton" style="max-width:240px" aria-hidden="true"></div>'
        f'</div>'
    )


def error_state(message: str) -> str:
    """Return an error-state block as an HTML string."""
    return (
        f'<div class="error-state" role="alert">'
        f'<div class="state-icon" aria-hidden="true">&#9888;</div>'
        f'<p>{escape(message)}</p>'
        f'</div>'
    )


_VALID_AWAITING_OWNERS = frozenset({"keel", "orient"})

# The threshold `verified` ages past before the stamp reads as stale rather
# than current — see `awaiting_state`'s docstring.
_AWAITING_STALE_AFTER_DAYS = 14


def awaiting_state(*, shows: str, needs: str, owner: str, verified: str,
                   probe: tuple[str, int] | None = None,
                   probe_fn: Callable[[str], int | None] | None = None,
                   contradicted_by: str | None = None) -> str:
    """A panel occupying the slot a not-yet-built capability will fill —
    "the capability does not exist yet," the family member `empty_state`
    (the capability works, there is no data) and `error_state` (it broke just
    now) don't cover.

    Two of Orient's three "waiting on the Library" placeholders were wrong for
    days before anyone re-tested them by hand — this exists so a stale or
    wrong claim shows up on the page itself rather than in a thread comment
    weeks later.

    `owner` is mandatory, with no default, and raises `ValueError` on anything
    but `"keel"` or `"orient"` — naming who owns the gap is half the fix,
    since both wrong placeholders blamed the Library for work Orient itself
    had not built.

    **A `probe` CANNOT VERIFY AN `owner="orient"` CLAIM, and reading it as
    though it can is how the third wrong placeholder survived.** `/crews`
    carried a panel until 2026-09-03 saying Orient needed "a client for GET
    /courses", probed at `("/courses", 200)`. The probe passed at every
    render, and the claim was false the whole time —
    `library_client.list_courses` and `get_course` existed and two builders
    consumed them. A probe asks the LIBRARY whether it serves a route, which
    is the half nobody doubted; an Orient-owned claim is about code in THIS
    repo, and its passing and failing states produce the same 200. So a green
    stamp on an Orient claim is decoration, and worse than none, because it
    reads as having been checked.

    **Use `contradicted_by` for those**, holding the value that would falsify
    the claim at the moment of render — `security_build`'s panel is the
    worked example, passing the harvester field whose mere presence disproves
    what the panel says. Reach for `probe` only where the claim really is
    about a route the Library serves.

    Three render states:

    - **Holds** — `probe` confirms the claim (its status matches
      `expected_status`), or no probe answered and `verified` is within
      `_AWAITING_STALE_AFTER_DAYS`. "No probe answered" covers both cases: no
      `probe` was given, and one was given but the Library could not be
      reached, which is the same evidential position and so takes the same
      branch. Calm styling throughout.
    - **Stale** — no probe answered, and `verified` is older than the
      threshold. Only the stamp line turns `--warn` on `--warn-wash`; the
      claim's own body text does not fade, so staleness reads as a fact about
      the check, not a judgment on the panel's content.
    - **Contradicted** — the probe answers something other than
      `expected_status`, or `contradicted_by` was passed at all. The whole
      block flips to the error family's tone (`--err` on `--err-wash`) and
      says, in its own words, that the claim is wrong rather than that the
      capability broke.

    **A 5xx or a 429 is not an answer and cannot contradict anything.** The
    Library failing says nothing about what the route serves, so those statuses
    take the no-answer branch and the `verified` date decides alone, with the
    stamp naming the status that could not settle it. This is not defensive
    generality: on the day this shipped the Library's vector index would not
    open, `GET /signals/search` answered 503, and without this the panel would
    have told a reader a true claim was wrong — the failure the whole component
    exists to end.

    Three ways to drive the branch, and which to reach for depends on what the
    caller already has:

    - `probe`, when given, is `(path, expected_status)` — this calls
      `library_client.probe_route(path)` (or `probe_fn`, see below) itself,
      which never raises, so a Library that is down or slow degrades this to
      the no-probe case (the `verified` date alone decides holds vs. stale)
      rather than blanking the panel or misreporting a contradiction that was
      never actually observed. Reach for this when a live route can answer
      the claim.
    - `contradicted_by` is the self-check counterpart to `probe`: some callers
      already hold, in the data they are rendering from, the exact value that
      would falsify their own "does not exist yet" claim — no route to poll,
      no round-trip, just a field that used to be `None` and now isn't. Pass a
      one-sentence explanation of what was found and this forces `state =
      "contradicted"` immediately, without waiting on `verified` to go stale.
      Reach for this when the falsifying value is already in hand. Mutually
      exclusive with `probe` — a caller has either a live route to check or a
      value already in hand, never both.
    - `probe_fn` overrides what gets called to answer `probe`, defaulting to
      `library_client.probe_route`. This exists so a caller can drive the
      `contradicted` branch deterministically — the gallery's demo and this
      module's own tests both need to force a probe answer without a live
      Library, and swapping `library_client.probe_route` itself for the
      duration of the call would be a global mutation reachable from every
      other request this process is concurrently serving. Passing a stub in
      keeps the fake answer scoped to this one call. It only substitutes the
      prober `probe` uses, so it is meaningless without `probe` and raises if
      passed alongside `contradicted_by`.

    Every interpolated value is escaped, matching the other state helpers.
    """
    if owner not in _VALID_AWAITING_OWNERS:
        raise ValueError(
            f"awaiting_state owner must be 'keel' or 'orient', got {owner!r}")
    if probe is not None and contradicted_by is not None:
        raise ValueError(
            "awaiting_state: pass probe or contradicted_by, not both")
    if probe_fn is not None and contradicted_by is not None:
        raise ValueError(
            "awaiting_state: probe_fn has no effect without probe; pass it "
            "with probe, not with contradicted_by")
    owner_label = "THE KEEL" if owner == "keel" else "ORIENT"
    badge = f"WAITING ON {owner_label} — {needs}"

    if contradicted_by is not None:
        state = "contradicted"
        stamp = contradicted_by
    else:
        resolved_probe_fn = probe_fn if probe_fn is not None else library_client.probe_route
        probe_status = resolved_probe_fn(probe[0]) if probe else None
        verified_date = dt.date.fromisoformat(verified)
        age_days = (orient_time.now().date() - verified_date).days
        # A 5xx or a 429 is the Library failing, not the Library answering. It
        # says nothing about what the route serves, so it cannot contradict —
        # or confirm — a claim about what the route serves. Caught live on PR
        # #202: the Library's vector index would not open, `GET
        # /signals/search` answered 503, and this component was about to tell
        # a reader "that route answered today, the claim is wrong" about a
        # claim that was true. That is the exact failure this component
        # exists to end, so it must not be the first to commit it.
        inconclusive = probe_status is None or probe_status >= 500 or probe_status == 429

        if probe is not None and not inconclusive and probe_status != probe[1]:
            state = "contradicted"
            stamp = (f"This panel claims to be waiting on GET {probe[0]}. That "
                     f"route answered {probe_status} today. The claim is wrong, "
                     f"not the capability.")
        elif probe is not None and not inconclusive:
            state = "holds"
            stamp = f"claim verified at render: GET {probe[0]} → {probe_status}"
        else:
            # No answer to reason from, so the `verified` date decides alone —
            # exactly as it does when no probe was given at all.
            state = "stale" if age_days > _AWAITING_STALE_AFTER_DAYS else "holds"
            if probe is not None and probe_status is not None:
                stamp = (f"could not re-verify: GET {probe[0]} answered "
                         f"{probe_status}; claim last verified {verified}")
            elif age_days > _AWAITING_STALE_AFTER_DAYS:
                stamp = f"unverified for {age_days} days"
            else:
                stamp = f"claim last verified {verified}"

    role = ' role="alert"' if state == "contradicted" else ""
    stamp_class = "awaiting-stamp stale" if state == "stale" else "awaiting-stamp"
    return (
        f'<div class="awaiting-state {state}"{role}>'
        f'<span class="awaiting-glyph" aria-hidden="true">&#9676;</span>'
        f'<p class="awaiting-shows">{escape(shows)}</p>'
        f'<p class="awaiting-badge">{escape(badge)}</p>'
        f'<p class="{stamp_class}">{escape(stamp)}</p>'
        f'</div>'
    )


@dataclasses.dataclass(frozen=True)
class Freshness:
    """One dashboard's own judgment of "is my data stale," computed once in
    the module that owns the data and read verbatim by both that module's own
    page and by /overview's index card for it. Two independent readers of the
    same age is exactly how the index and the destination page disagreed —
    each would duplicate the threshold, the cadence string and the reason,
    and a duplicated judgment drifts (issue_01M0TKX6DW2E1TXGN9QT6K61J5 comment
    902). A dashboard builds one `Freshness` from its own snapshot; both
    renderers consume it, so they cannot disagree about the same data.

    `stalled` is `None`, not `False`, when the module has no cadence it can
    honestly assert for this feed (an event-driven source with no fixed
    producer schedule, for instance) — a third state recording that no
    judgment was made, never a stand-in for "known fresh."
    """
    text: str
    stalled: bool | None
    cadence: str | None = None
    reason: str | None = None


def stalled_banner_html(feed: str, freshness: Freshness) -> str:
    """The `stalled_state()` banner for a `Freshness`, or "" when it isn't
    stalled — including the `stalled=None` "no judgment possible" case, which
    renders nothing rather than a false all-clear."""
    if not freshness.stalled:
        return ""
    return stalled_state(
        feed=feed,
        newest=freshness.text,
        expected_cadence=freshness.cadence or "unknown",
        why=freshness.reason or "",
    )


def stalled_state(*, feed: str, newest: str, expected_cadence: str, why: str) -> str:
    """A banner for a *live* capability whose feed has gone quiet — unlike
    `awaiting_state`, which is a slot for a capability that does not exist yet,
    this sits ABOVE real content and never replaces it, because the data below
    is still the best data there is; it is only stale.

    `feed` names what stopped (currently unused in the rendered text but kept
    as an explicit parameter so a caller cannot describe a banner without
    naming what it is about); `newest` is the caller's own formatted
    "<date>, <age> ago"; `expected_cadence` is a plain-English cadence
    ("daily"); `why` is the reason, in one sentence.

    **This answers a different question than `render_stamp`.** The render
    stamp says "is this *page* current" — true the instant a live page
    responds, since render time and now are the same moment. This banner says
    "is this page's *data* current," which a page can render honestly while
    its data has gone stale for days: `/engineering/e2e` rendered a green PASS
    tile dated six days ago, quietly, with a render stamp reading "just now"
    the whole time — the proof the two questions are not one question.
    """
    message = f"Newest result {newest}. This feed expects a result {expected_cadence}. {why}"
    return (
        f'<div class="stalled-banner" role="status" data-feed="{escape(feed, quote=True)}">'
        f'<span class="stalled-glyph" aria-hidden="true">&#9888;</span>'
        f'<p>{escape(message)}</p>'
        f'</div>'
    )


def render_stamp(rendered_at: str | None = None) -> str:
    """The "rendered <when>" footer line, stamped on every page by `page_shell`.

    Answers one question the site could not answer about itself: is what I am
    looking at current? Three times in one week (2026-07-28..30) the operator had to ask
    it out of band — a spending dashboard frozen at 20:35, an overnight digest
    that was a day old, and an issue count they could not date. In each case the
    page looked exactly the same whether it was seconds or days stale.

    It stamps RENDER time, which is the honest thing a page can assert about
    itself. On a live page that is also the data's freshness. On a static bundle
    it is the build time, so an old bundle reads as old — which is the case where
    the question actually needed answering.

    Also carries the back/forward/refresh nav cluster
    (issue_01M1517CYMAV0GAEB2FYQQKF4N) — this footer is the one place on
    every page proving a refresh actually reached the server, so the control
    that asks for one lives right next to that proof. The value is repeated
    into a `data-rendered-at` attribute for `NAV_CLUSTER_JS` to compare
    before/after a refresh; it must stay a plain attribute, not a wrapping
    element around the visible text, so the value stays the LAST thing in
    the footer — `test_page_shell_stamps_render_time` pins the literal
    ``" MST</footer>"`` ending, which a wrapper tag would break.
    """
    value = rendered_at or orient_time.now_label()
    return (
        f'<footer class="render-stamp" id="renderStamp" data-rendered-at="{escape(value, quote=True)}">'
        '<span class="nav-cluster" role="group" aria-label="Page navigation">'
        '<button type="button" id="navBackBtn" class="nav-btn" aria-label="Go back" disabled>&larr;</button>'
        '<button type="button" id="navForwardBtn" class="nav-btn" aria-label="Go forward" disabled>&rarr;</button>'
        '<button type="button" id="navRefreshBtn" class="nav-btn" aria-label="Refresh this page">&#8635;</button>'
        '<span class="refresh-note" id="refreshNote" role="status" aria-live="polite"></span>'
        '</span>'
        f'rendered {escape(value)}</footer>'
    )


# Reload an open page once its data may have moved on (the operator, 2026-09-28: a Home
# tab left open overnight still read the previous day's spend). A hidden tab is
# not reloaded until it is visible again, and a page whose form field has focus
# is left alone and retried a minute later, so a half-typed alert threshold on
# /spending survives. `convivy:beforerefresh` lets a page keep its filter state
# across the reload, exactly as the nav's refresh button does.
AUTO_REFRESH_JS = r"""
(function() {
  var el = document.getElementById('autoRefresh');
  var ms = el ? 1000 * parseInt(el.getAttribute('data-seconds'), 10) : 0;
  if (!(ms > 0)) return;
  var loaded = Date.now();
  function busy() {
    var a = document.activeElement;
    return !!a && (/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) || a.isContentEditable);
  }
  function tryReload() {
    if (document.visibilityState !== 'visible' || Date.now() - loaded < ms || busy()) return;
    document.dispatchEvent(new CustomEvent('convivy:beforerefresh'));
    location.reload();
  }
  setInterval(tryReload, 60000);
  document.addEventListener('visibilitychange', tryReload);
})();
"""


# The pages showing today's spend reload on the cost report's own cadence
# (agent-platform's agent-cost-report-refresh job runs at :05, :20, :35 and :50).
SPEND_REFRESH_SECONDS = 15 * 60


TAB_TITLE_SUFFIX = "Orient · Convivy Builder"


def tab_title(page: str) -> str:
    """The browser-tab title for a page: "{page} · Orient · Convivy Builder".

    The page name leads because tabs truncate from the right. An empty page
    name gives the home page's "Orient · Convivy Builder". Convivy Builder is
    the product and Orient its part, shown second and never alone
    (company/decisions/0020-convivy-builder-is-the-name-and-deck-and-orient-are-its-parts).
    """
    return f"{page} · {TAB_TITLE_SUFFIX}" if page else TAB_TITLE_SUFFIX


def page_shell(*, title: str, nav_html: str, body: str,
               extra_css: str = "",
               whoami_placeholder: bool = True,
               rendered_at: str | None = None,
               auto_refresh_seconds: int | None = None) -> str:
    """Wrap content in a full HTML document with tokens, CSS, scripts, and nav.

    `title` is the page's own name; `tab_title` adds the product suffix, so
    no builder spells it.

    Embeds TOKENS, BASE_CSS, and COMPONENTS_CSS inline. Scripts included: the
    URL state helper and the nav cluster (head — the nav cluster's
    history.pushState/replaceState wrap must install before any
    body-embedded page script calls `_ds.writeUrlState`), theme-toggle
    (head), range-control and drawer JS (body). The __WHOAMI__ placeholder is
    left in the nav for server.py to substitute at serve time. Pass extra_css
    for page-local rules that should not live in the shared COMPONENTS_CSS.

    Every page gets a render stamp in the footer. It lives here rather than in
    each builder so no page can be added without one — the pages that most needed
    it were exactly the ones nobody thought to stamp. Pass `rendered_at` to state
    a different time, e.g. a snapshot rendered from data captured earlier.

    `auto_refresh_seconds` reloads the page that long after it loaded, while it
    is open and visible — see AUTO_REFRESH_JS.
    """
    whoami_nav = nav_html if whoami_placeholder else nav_html.replace("__WHOAMI__", "")
    auto_refresh = (f'<span id="autoRefresh" data-seconds="{int(auto_refresh_seconds)}" hidden></span>'
                    f"\n<script>{AUTO_REFRESH_JS}</script>" if auto_refresh_seconds else "")
    extra_block = f"\n{extra_css}" if extra_css else ""
    stamp = render_stamp(rendered_at)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(tab_title(title))}</title>
<link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="32x32" href="/assets/favicon-32.png">
<link rel="manifest" href="/assets/site.webmanifest">
<meta name="theme-color" content="#E8542F">
<meta name="apple-mobile-web-app-title" content="Orient">
<style>
{TOKENS}
{BASE_CSS}
{COMPONENTS_CSS}{extra_block}
</style>
<script>
{URL_STATE_JS}
{NAV_CLUSTER_JS}
{THEME_TOGGLE_JS}
</script>
</head>
<body>
{whoami_nav}
<div class="page-wrap">
{body}
{stamp}
</div>
<script>
{RANGE_CONTROL_JS}
{_DRAWER_JS}
</script>
{auto_refresh}
</body>
</html>"""
