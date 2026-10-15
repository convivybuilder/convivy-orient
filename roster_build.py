#!/usr/bin/env python3.11
"""/engineering/roster — live roster of agents available to each project.

Renders LIVE per request (ARCH-182 pattern): live_html() re-reads the agent
catalog and activation profiles from the knowledge repo each time, so adding a
new agent or updating a profile appears on the next page load without a rebuild.

Data sources (TWO, deliberately — ARCH-361 follow-up):

1. **Central catalog** — ~/Convivy/knowledge/projects/agent-platform/agents/*.md, the
   generically-named agents (coder, reviewer, architect…) that products
   individuate. YAML frontmatter supplies title, model, and audience (the agent
   slug); the first non-empty, non-heading, non-blockquote paragraph of the body
   is the one-line description.
2. **Activation profiles** — ~/Convivy/knowledge/projects/<product>/profiles/*.yaml;
   the audience_contains list names which catalog agents are individuated for that
   product+lane.
3. **Repo-local crews** — <repo>/.claude/agents/*.md. Some projects have agents
   that exist ONLY here and in no central catalog entry (selector's sel-coder,
   hollis-*, svc-*, substrate's scout, write-better's editor…). Three of those
   projects have no profiles YAML at all, so the profile path can never surface
   them. Reading the repo dir is the only way the page can reflect what is
   actually dispatchable, and that dir is the source of truth agentctl itself
   dispatches from.

These use DIFFERENT frontmatter schemas and both are parsed: the catalog uses the
knowledge schema (`kind: agent`, `audience: [slug]`, `title`), while a repo-local
file uses Claude Code's native agent schema (`name:`, `description:`). See
_parse_agent_file.

A repo-local agent whose slug ALSO exists in the catalog is an individuated copy of
the generic agent — it is already rendered via its profile lane and is skipped, so
a product doesn't list fourteen duplicates. Only agents with no catalog entry are
rendered as that project's own crew.

Full-tier only (server.py gates /engineering/roster); a contractor gets 404.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import crew_registry
import design_system as ds
from paths import STATE_ROOT

KNOWLEDGE_ROOT = Path.home() / "Convivy" / "knowledge" / "projects"
# The agent catalog, read at RENDER time (ARCH-182).
#
# One directory today, and a TUPLE on purpose. It carried two for the length of
# Phase 0 step 5, which re-filed the 29 agent profiles from `shared` onto
# `agent-platform`: this reader lives in a different repo from the documents, so
# the move and this change could not land in one PR, and reading only the old
# path would have emptied the Roster page the moment the knowledge PR merged —
# emptied it at the next RENDER rather than in CI, which is the kind of break
# nobody attributes to the change that caused it. The `shared` entry comes out
# here, in step 5's last PR (PR G).
#
# `load_agents` keeps the merge-and-order machinery a second entry needs, and
# `test_row_order_does_not_lurch_when_a_document_migrates` exercises it against a
# two-directory fixture. That is what makes the next re-file a one-line change
# with a test already watching it, rather than three PRs of dual reads.
#
# A directory that does not exist yet contributes nothing: `Path.glob` on a
# missing directory yields no entries rather than raising, so listing a
# destination early is free.
AGENTS_DIRS = (
    KNOWLEDGE_ROOT / "agent-platform" / "agents",
)
OUT = STATE_ROOT / "site"

CONVIVY_ROOT = Path.home() / "Convivy"

# Projects shown on the page, in display order. Each entry is:
#   (project_slug, display_label, repo_path, [profile_names_in_order])
# Profile names map to <knowledge>/projects/<slug>/profiles/<name>.yaml.
# An empty profile list means no interactive crew (autonomous-only).
PROJECTS = [
    ("vivy",              "Vivy",              str(CONVIVY_ROOT / "vivy"),
     ["daytime", "commercial", "overnight"]),
    ("constance",         "Constance",         str(CONVIVY_ROOT / "constance"),
     ["daytime", "commercial", "overnight"]),
    ("sous-chef",         "Sous Chef",         str(CONVIVY_ROOT / "sous-chef"),
     ["daytime", "overnight"]),
    ("agent-platform",    "Agent Platform",    str(CONVIVY_ROOT / "agent-platform"),
     ["daytime"]),
    ("convivy-commercial","Convivy Commercial",str(CONVIVY_ROOT / "convivy-commercial"),
     ["commercial"]),
    # These render entirely from their .claude/agents dir BY CHOICE, not because
    # a profiles YAML is absent: selector, hollis, convivy-app-kit, write-better
    # and convivy-orient all have one under knowledge/projects/<slug>/profiles/
    # (only substrate genuinely has none). The repo-crew path renders repo-local
    # descriptions and the real `agentctl spawn` launch command; a profiles YAML
    # would instead render the `co <slug>` form and generic central-catalog
    # descriptions for agents that also live there. Listed explicitly for
    # display order + label; discovery below would otherwise pick them up
    # unlabelled.
    ("selector",          "Selector",          str(CONVIVY_ROOT / "selector"), []),
    ("hollis",            "Hollis",            str(CONVIVY_ROOT / "hollis"), []),
    ("convivy-app-kit",   "Convivy App Kit",   str(CONVIVY_ROOT / "convivy-app-kit"), []),
    ("write-better",      "Write Better",      str(CONVIVY_ROOT / "write-better"), []),
    ("substrate",         "Substrate",         str(CONVIVY_ROOT / "substrate"), []),
    ("convivy-orient",    "Convivy Orient",    str(CONVIVY_ROOT / "convivy-orient"), []),
]


# Repos never shown as projects: infrastructure, mirrors, and scratch checkouts
# that contain a .claude/agents dir but aren't a product roster anyone launches
# a crew against.
#
# Each entry is a claim that nothing tests on its own — "nobody dispatches a
# crew against this repo" — and it can go stale silently: convivy-orient sat
# here for a month after this repo got its own KB crew
# (orient-coder.md, added 2026-08-17) with nothing to notice the claim had
# become false. test_discovery_skip_repos_have_no_kb_crew checks it instead of
# trusting this set by inspection.
_DISCOVERY_SKIP = {
    "knowledge", "worktrees", "agent-runs", "write-better-site",
}

# Any dir ending in this is a worktree POOL, never a project. Worth matching by
# suffix rather than listing names: a worktree is a full checkout and therefore
# HAS a .claude/agents dir, so a new pool (hollis-worktrees, convivy-app-kit-worktrees)
# would otherwise appear as a phantom project the first time one is created.
_WORKTREE_POOL_SUFFIX = "-worktrees"

# Human-readable lane labels for profile names.
LANE_LABEL = {
    "daytime":    "Daytime (interactive)",
    "commercial": "Commercial",
    "overnight":  "Overnight (autonomous)",
    "repo-crew":  "Repo crew (product-specific)",
}

# Maps a project_slug to its knowledge-project directory when the two differ.
# Read live from agent-platform's EXPLICIT_PROJECT_BY_NAME (see
# _load_knowledge_slug_map) rather than hand-copied here. If a slug is absent
# from that map, it maps to itself.
_PROJECTS_PY_PATH = (
    Path.home() / "Convivy" / "agent-platform" / "lib" / "agent-platform" / "projects.py"
)


def _load_knowledge_slug_map() -> dict[str, str]:
    """Parse `EXPLICIT_PROJECT_BY_NAME` out of agent-platform's projects.py.

    This used to be a dict hand-copied into this file. It drifted: it held
    convivy-commercial to the retired `convivy` scope for an unknown stretch
    after ARCH-438 re-pointed agent-platform's own copy to `company`, and
    nothing caught it because nothing read the two together
    (issue_01M0VMAZZMN4VCGJF197GASR6Y). Reading the literal at render time
    means there is no second copy to forget.

    The map lived in bin/knowledge-ctx as `_PRODUCT_MAP` until agent-platform
    2bc24392 (2026-09-25) moved it into projects.py, where knowledge-ctx and
    agentctl now share it. The roster read an empty map from then until
    issue_01M3HRHPXH4D68PHRWYK7XFVB8, and the stderr warning below is how that
    showed up.

    Depends on `EXPLICIT_PROJECT_BY_NAME` staying a FLAT str -> str dict: the
    pattern stops at the first line-starting brace, which would be the wrong
    one if the map ever nested.

    Returns {} if the file is missing or the literal can't be found, so a
    project_slug falls back to mapping to itself — the same behavior an absent
    entry always had — rather than the roster page crashing. Those two are NOT
    the same condition and only one is expected, so the second is announced on
    stderr: a missing checkout is normal on some machines, while a file that
    exists whose literal no longer parses means this anti-drift mechanism has
    silently stopped working. Staying quiet about that would reproduce the
    silent failure this function was written to end.
    """
    try:
        text = _PROJECTS_PY_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    m = re.search(r'EXPLICIT_PROJECT_BY_NAME = \{(.*?)^\}', text, re.S | re.M)
    if not m:
        # Guarded the way chart_house_build.py guards its own stderr logging,
        # and for the reason server.py's _log_render_failure states outright:
        # never let a logging failure break the response. An unguarded print
        # here could raise out of the very function whose contract is to return
        # {} rather than crash the roster page.
        try:
            print(f"roster: {_PROJECTS_PY_PATH} exists but no EXPLICIT_PROJECT_BY_NAME "
                  f"literal parsed out of it — the roster's project mapping is "
                  f"falling back to identity, which silently drops any workspace "
                  f"whose slug differs from its knowledge project. Check whether "
                  f"that file's format changed.", file=sys.stderr)
        except Exception:
            pass
        return {}
    return dict(re.findall(r'"([A-Za-z0-9._-]+)"\s*:\s*"([A-Za-z0-9._-]+)"', m.group(1)))


# Shorthand aliases from the `co` script (lines 857-864 in agent-platform/bin/co).
# These match only in product repos (not convivy-commercial, which has its own
# alias set). Keys are agent slugs; values are the shortest alias.
_PRODUCT_CO_ALIASES: dict[str, str] = {
    "planning-orchestrator": "co",
    "planner":               "co p",
    "coder":                 "co c",
    "bug-investigator":      "co b",
    "bug-fixer":             "co f",
    "explainer":             "co e",
    "architect":             "co a",
    "reviewer":              "co rev",
}

# Aliases specific to convivy-commercial (co script lines 711-714).
_COMMERCIAL_CO_ALIASES: dict[str, str] = {
    "positioning-strategist": "co",
    "commercial-writer":      "co writer",
    "product-analyst":        "co pa",
}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Returns (fields, body).

    Handles the simple scalar and flow-sequence forms used in agent frontmatter.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    fm_text = text[4:end]
    nl = text.find("\n", end + 1)
    body = text[nl + 1:] if nl != -1 else ""

    fields: dict = {}
    for line in fm_text.split("\n"):
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        raw = raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1]
            fields[key] = [s.strip().strip('"').strip("'")
                           for s in inner.split(",") if s.strip()]
        else:
            fields[key] = raw.strip('"').strip("'")
    return fields, body


def _first_paragraph(body: str) -> str:
    """Return the first non-empty, non-heading, non-blockquote paragraph."""
    for block in body.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("#") or block.startswith(">") or block.startswith("---"):
            continue
        # Take only the first line if the block is multi-line prose.
        return block.split("\n")[0].strip()
    return ""


def load_agents() -> dict[str, dict]:
    """Return {slug: agent_record} for every agent in the catalog.

    Reads every directory in AGENTS_DIRS and merges them. Sorted by FILENAME
    across the union rather than directory-by-directory, so the page's order does
    not lurch as documents migrate between the two — a re-file should move a row's
    source, not its position.

    A slug appearing in both directories takes the one from the directory listed
    LAST in AGENTS_DIRS, which is the new location. Mid-migration that is the right
    answer: a profile already re-filed is the current one, and a leftover copy at
    the old path is what is stale.

    Precedence therefore follows the AGENTS_DIRS ORDER, not the parent path's
    spelling. Sorting by the path string looks equivalent and is not — it would
    make `agent-platform` sort before `shared` and hand the win to the stale copy,
    silently, which is what the first version of this did."""
    agents: dict[str, dict] = {}
    paths = sorted(((p, i) for i, d in enumerate(AGENTS_DIRS) for p in d.glob("*.md")),
                   key=lambda pair: (pair[0].name, pair[1]))
    for path, _ in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = _parse_frontmatter(text)
        if fm.get("kind") != "agent":
            continue
        audience = fm.get("audience", [])
        if isinstance(audience, str):
            audience = [audience]
        if not audience:
            continue
        slug = audience[0]
        agents[slug] = {
            "slug":  slug,
            "title": fm.get("title", slug),
            "model": fm.get("model", ""),
            "desc":  _first_paragraph(body),
        }
    return agents


def load_repo_agents(repo_path: str) -> dict[str, dict]:
    """Return {slug: agent_record} for a repo's own <repo>/.claude/agents/*.md.

    These use Claude Code's NATIVE agent schema (`name:`, `description:`,
    `model:`) rather than the knowledge schema the central catalog uses, so the
    fields are read from different keys. A file with no `name` is skipped: the
    slug is what agentctl dispatches on, so a record without one is unusable.
    """
    agents: dict[str, dict] = {}
    agents_dir = Path(repo_path) / ".claude" / "agents"
    if not agents_dir.is_dir():
        return agents
    for path in sorted(agents_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, body = _parse_frontmatter(text)
        slug = (fm.get("name") or "").strip()
        if not slug:
            continue
        desc = (fm.get("description") or "").strip() or _first_paragraph(body)
        agents[slug] = {
            "slug":  slug,
            # Native files carry no title; derive a readable one from the slug.
            "title": fm.get("title") or slug.replace("-", " ").title(),
            "model": fm.get("model", ""),
            "desc":  desc,
        }
    return agents


def discover_projects() -> list[tuple[str, str, str, list[str]]]:
    """PROJECTS, plus any ~/Convivy/<repo> with a non-empty .claude/agents dir.

    The page's whole failure mode was going stale: PROJECTS was a hand-maintained
    list of five, so five projects' crews (~21 agents) were invisible and nobody
    noticed until the operator went looking for sel-coder. Discovery makes a NEW project
    with a crew appear on its own, so the page can't silently drift again.
    """
    out = list(PROJECTS)
    known = {slug for slug, _, _, _ in PROJECTS}
    if not CONVIVY_ROOT.is_dir():
        return out
    for repo in sorted(CONVIVY_ROOT.iterdir()):
        # is_symlink first: a symlinked dir would be followed out of the tree,
        # and a project is always a real checkout under ~/Convivy.
        if repo.is_symlink() or not repo.is_dir():
            continue
        if repo.name in known or repo.name in _DISCOVERY_SKIP:
            continue
        if repo.name.startswith(".") or repo.name.endswith(_WORKTREE_POOL_SUFFIX):
            continue
        agents_dir = repo / ".claude" / "agents"
        if not agents_dir.is_dir() or not any(agents_dir.glob("*.md")):
            continue
        out.append((repo.name, repo.name.replace("-", " ").title(), str(repo), []))
    return out


def _parse_profile(yaml_path: Path) -> list[str]:
    """Extract the audience_contains slugs from a profile YAML.

    Reads only the include block's audience_contains lines — no full YAML parser
    needed for this narrow structure — but it MUST respect indentation.

    The profiles look like:

        include:
          - kind: agent
            audience_contains:
              - planner
              - coder
                                  <- blank line
          - kind: skill           <- sibling include entry, NOT an agent

    The previous version exited the block only on a non-blank, non-comment line,
    so the blank line above kept it "inside" audience_contains and the sibling
    `- kind: skill` was swallowed as a slug named "kind: skill". That produced a
    phantom agent on every profile-based project. It was invisible while an
    unknown slug was silently dropped; the moment load_roster started rendering
    unresolvable slugs (so a stale reference stops looking like an empty lane),
    it would have shipped a bogus card onto 10 lanes.

    So the block ends at the first line indented no deeper than the
    `audience_contains:` key itself. Blank lines are skipped rather than treated
    as terminators, because YAML allows them inside a sequence.
    """
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError:
        return []
    slugs: list[str] = []
    audience_indent: int | None = None
    for line in text.split("\n"):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped == "audience_contains:":
            audience_indent = indent
            continue
        if audience_indent is None:
            continue
        if not stripped:
            continue                      # blank lines are legal inside a sequence
        if indent <= audience_indent:
            audience_indent = None        # dedented to a sibling key — block over
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            # Strip inline comments (e.g. "- coder  # feature queue").
            raw = stripped[2:].split("#")[0].strip()
            if raw:
                slugs.append(raw)
    return slugs


def _agentctl_launch(slug: str, product: str) -> str:
    """Return the programmatic agentctl launch command for a scheduled/autonomous agent."""
    return f'agentctl spawn {slug} --on {product} --task "<task>"'


def _launch_command(slug: str, repo_path: str, project_slug: str,
                    lane: str = "daytime") -> str:
    """Return the copy-pasteable launch command for an agent in a product repo.

    Overnight-lane agents run via agentctl, not an interactive co session.
    """
    if lane == "overnight":
        return _agentctl_launch(slug, project_slug)
    if project_slug == "convivy-commercial":
        alias = _COMMERCIAL_CO_ALIASES.get(slug, f"co {slug}")
        return f"cd {repo_path} && {alias}"
    alias = _PRODUCT_CO_ALIASES.get(slug)
    if alias:
        return f"cd {repo_path} && {alias}"
    return f"cd {repo_path} && co {slug}"


def load_roster() -> dict:
    """Build the full roster payload.

    Returns a dict with:
      - projects: list of project records (slug, label, repo, lanes)
      - autonomous: list of agent records not in any interactive (non-overnight) profile
    """
    catalog = load_agents()
    knowledge_slug = _load_knowledge_slug_map()

    # Collect all slugs that appear in at least one interactive (non-overnight) profile.
    # Any agent in a daytime or commercial lane is project-assigned, not autonomous.
    interactive_slugs: set[str] = set()

    projects_out = []
    for proj_slug, label, repo_path, profile_names in discover_projects():
        lanes = []
        kb_slug = knowledge_slug.get(proj_slug, proj_slug)
        repo_agents = load_repo_agents(repo_path)
        shown_slugs: set[str] = set()
        for profile_name in profile_names:
            yaml_path = KNOWLEDGE_ROOT / kb_slug / "profiles" / f"{profile_name}.yaml"
            if not yaml_path.exists():
                continue
            slugs = _parse_profile(yaml_path)
            if not slugs:
                continue
            if profile_name != "overnight":
                interactive_slugs.update(slugs)
            agents_in_lane = []
            for slug in slugs:
                # A profile may name an agent the catalog doesn't have. Falling
                # through to the repo's own copy keeps it visible; rendering an
                # explicit placeholder when it exists NOWHERE beats the previous
                # silent `continue`, which made a broken profile reference and a
                # deliberately-empty lane look identical on the page.
                if slug in catalog:
                    agent = dict(catalog[slug])
                elif slug in repo_agents:
                    agent = dict(repo_agents[slug])
                else:
                    agent = {"slug": slug, "title": slug, "model": "",
                             "desc": "Named in this profile but found in neither the "
                                     "central catalog nor the repo's .claude/agents — "
                                     "the reference is stale or the agent was removed.",
                             "unresolved": True}
                agent["launch"] = _launch_command(slug, repo_path, proj_slug, profile_name)
                agents_in_lane.append(agent)
                shown_slugs.add(slug)
            if agents_in_lane:
                lanes.append({
                    "name":    profile_name,
                    "label":   LANE_LABEL.get(profile_name, profile_name),
                    "agents":  agents_in_lane,
                })

        # The project's OWN crew: every repo-local agent not ALREADY rendered
        # above for this project.
        #
        # The test is `shown_slugs`, not "is it in the central catalog". Those
        # differ in a way that matters: a project with profiles (vivy) already
        # shows its generic agents in a profile lane, so skipping them here
        # correctly avoids fourteen duplicates. But a project with NO profiles
        # (vivy-ownerrez-sandbox-tools, the *-commercial repos) has rendered
        # nothing yet — deduping against the catalog there would drop its entire
        # crew and show an empty project that really has fourteen dispatchable
        # agents. Dedupe against what the page has actually shown.
        own_crew = []
        for slug, agent in sorted(repo_agents.items()):
            if slug in shown_slugs:
                continue
            rec = dict(agent)
            # Repo-local agents are DISPATCHED subagents, not interactive co
            # personas, so they get the agentctl form. Rendering `co <slug>` here
            # was actively wrong — substrate's own `scout` documents itself as
            # `agentctl spawn scout ... --on <repo>`, and no repo-crew agent is
            # launched by starting a co session named after it.
            rec["launch"] = _agentctl_launch(slug, proj_slug)
            own_crew.append(rec)
        if own_crew:
            lanes.append({
                "name":   "repo-crew",
                "label":  LANE_LABEL["repo-crew"],
                "agents": own_crew,
            })

        projects_out.append({
            "slug":  proj_slug,
            "label": label,
            "repo":  repo_path,
            "lanes": lanes,
        })

    # Autonomous / cross-cutting agents: in the catalog but in no interactive profile.
    autonomous = []
    for slug, agent in sorted(catalog.items()):
        if slug in interactive_slugs:
            continue
        rec = dict(agent)
        rec["launch"] = _agentctl_launch(slug, "<repo>")
        autonomous.append(rec)

    return {"projects": projects_out, "autonomous": autonomous}


# ---------------------------------------------------------------------------
# Page-local CSS: Roster (/engineering/roster)
# ---------------------------------------------------------------------------

_ROSTER_CSS = r"""
/* ---- Page layout (centered reading width, max 900px) ------------------- */
.roster-main {
  max-width: 900px;
  margin: 0 auto;
  padding: 32px 32px 120px;
}
.roster-area-title {
  font-size: 1.5em;
  font-weight: 700;
  letter-spacing: -.01em;
  margin: 0 0 6px;
  color: var(--ink);
}
.roster-intro { color: var(--ink-3); font-size: 13.5px; max-width: 640px; margin: 0 0 20px; }

/* [hidden] must win over the display rules below — the /signals lesson: an
   element with `display:flex` silently defeats the hidden attribute. */
[hidden] { display: none !important; }

/* Filter bar: sticky so the chips stay reachable while scrolling a long roster. */
.roster-filter { position: sticky; top: 0; z-index: 5; padding: 10px 0 12px;
  background: var(--bg); border-bottom: 1px solid var(--line); margin: 0 0 24px; }
.agent-search { width: 100%; max-width: 460px; font-family: var(--sans); font-size: 13.5px;
  color: var(--ink); background: var(--surface); border: 1px solid var(--line);
  border-radius: 8px; padding: 8px 11px; }
.agent-search:focus-visible { outline: 2px solid var(--brand-ink); outline-offset: 1px; }
.roster-filter .chips { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 10px; }
.roster-filter .chip { font-family: var(--mono); font-size: 12px; padding: 5px 10px;
  border-radius: 999px; border: 1px solid var(--line); background: var(--surface);
  color: var(--ink-2); cursor: pointer; white-space: nowrap;
  transition: background .15s, color .15s, border-color .15s; }
.roster-filter .chip[aria-pressed="true"] { background: var(--ink); color: var(--bg);
  border-color: var(--ink); }
.roster-filter .chip .n { opacity: .6; margin-left: 4px; }
.project-count { font-family: var(--mono); font-size: 11.5px; color: var(--ink-3);
  font-weight: 400; margin-left: 6px; }
/* A profile naming an agent that exists nowhere — surfaced, not silently dropped. */
.agent-card.unresolved { border-style: dashed; opacity: .75; }

/* ---- Project sections -------------------------------------------------- */
.project { margin: 0 0 40px; }
.project-header { border-top: 2px solid var(--line); padding-top: 20px; margin-bottom: 18px; }
.project-name { font-size: 1.2em; font-weight: 700; margin: 0 0 4px; letter-spacing: -.01em; color: var(--ink); }
.project-repo { font-size: 12px; color: var(--ink-3); font-family: var(--mono); }

/* ---- Lane groups ------------------------------------------------------- */
.lane { margin: 0 0 20px; }
.lane-label {
  font-weight: 700;
  font-size: 11px;
  letter-spacing: .04em;
  text-transform: uppercase;
  color: var(--brand-ink);
  opacity: .8;
  margin: 0 0 8px;
}

/* ---- Agent cards ------------------------------------------------------- */
.agent-list { display: flex; flex-direction: column; gap: 8px; }
.agent-card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 11px 14px;
  display: grid;
  grid-template-columns: 140px 1fr;
  gap: 8px 14px;
  box-shadow: var(--shadow);
}
.agent-name { font-weight: 700; font-size: 14px; color: var(--ink); }
.agent-model { font-size: 11px; color: var(--ink-2); font-family: var(--mono); }
.agent-desc { font-size: 13px; color: var(--ink-2); grid-column: 2; }
.agent-launch { grid-column: 1 / -1; }
/* Code token gap: ds has no dedicated code-block token; use --surface-2/--ink */
.agent-launch code {
  display: block;
  background: var(--surface-2);
  color: var(--ink);
  padding: 7px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  font-size: 12px;
  font-family: var(--mono);
  overflow-x: auto;
  white-space: nowrap;
}

/* ---- Autonomous section ------------------------------------------------ */
.autonomous-section { margin-top: 40px; border-top: 2px solid var(--line); padding-top: 20px; }
.autonomous-note { font-size: 13px; color: var(--ink-3); margin: 0 0 16px; }
.empty { color: var(--ink-3); font-size: 14px; padding: 20px 4px; }

/* ---- Responsive -------------------------------------------------------- */
@media (max-width: 640px) {
  .roster-main { padding: 20px 16px 80px; }
  .agent-card { grid-template-columns: 1fr; }
  .agent-desc { grid-column: 1; }
  .agent-launch { grid-column: 1; }
}
"""

# The client-side Roster JS — kept verbatim from the original template.
_ROSTER_JS = r"""
const DATA = JSON.parse(document.getElementById('data').textContent);
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function agentCard(a) {
  // data-search carries the haystack the text filter matches on, lowercased once
  // here rather than recomputed per keystroke.
  const hay = [a.slug, a.title, a.model, a.desc].filter(Boolean).join(' ').toLowerCase();
  const unresolved = a.unresolved ? ' unresolved' : '';
  return `<div class="agent-card${unresolved}" data-search="${esc(hay)}">
    <div>
      <div class="agent-name">${esc(a.title)}</div>
      <div class="agent-model">${esc(a.model)}</div>
    </div>
    <div class="agent-desc">${esc(a.desc)}</div>
    <div class="agent-launch"><code>${esc(a.launch)}</code></div>
  </div>`;
}

const projectsEl = document.getElementById('projects');
if (!DATA.projects.length) {
  projectsEl.innerHTML = '<div class="empty">No projects found.</div>';
} else {
  projectsEl.innerHTML = DATA.projects.map(p => {
    if (!p.lanes.length) return '';
    const lanesHtml = p.lanes.map(lane =>
      `<div class="lane">
         <div class="lane-label">${esc(lane.label)}</div>
         <div class="agent-list">${lane.agents.map(agentCard).join('')}</div>
       </div>`
    ).join('');
    const count = p.lanes.reduce((n, l) => n + l.agents.length, 0);
    return `<div class="project" data-slug="${esc(p.slug)}">
      <div class="project-header">
        <div class="project-name">${esc(p.label)} <span class="project-count">${count}</span></div>
        <div class="project-repo">${esc(p.repo)}</div>
      </div>
      ${lanesHtml}
    </div>`;
  }).join('');
}

// --- filter: project chips + text search -------------------------------------
// The page lists 13 projects and 130+ agents, which is unusable as one long
// scroll. Chips narrow to a project; the search box narrows to matching agents
// across whichever projects are visible. Both are pure show/hide over the
// already-rendered DOM — no re-render, so it stays instant.
(function () {
  const chipsEl = document.getElementById('projectChips');
  const searchEl = document.getElementById('agentSearch');
  const noMatchEl = document.getElementById('noMatch');
  if (!chipsEl || !searchEl) return;
  const withAgents = DATA.projects.filter(p => p.lanes.length);
  if (!withAgents.length) return;

  const total = withAgents.reduce(
    (n, p) => n + p.lanes.reduce((m, l) => m + l.agents.length, 0), 0);
  chipsEl.innerHTML =
    `<button class="chip" data-slug="all" aria-pressed="true">All <span class="n">${total}</span></button>` +
    withAgents.map(p => {
      const c = p.lanes.reduce((n, l) => n + l.agents.length, 0);
      return `<button class="chip" data-slug="${esc(p.slug)}" aria-pressed="false">` +
             `${esc(p.label)} <span class="n">${c}</span></button>`;
    }).join('');

  let activeSlug = 'all';

  function apply() {
    const q = searchEl.value.trim().toLowerCase();
    let visibleAgents = 0;
    document.querySelectorAll('.project').forEach(proj => {
      const inProject = activeSlug === 'all' || proj.dataset.slug === activeSlug;
      let shownHere = 0;
      proj.querySelectorAll('.agent-card').forEach(card => {
        const hit = inProject && (!q || card.dataset.search.includes(q));
        card.hidden = !hit;
        if (hit) shownHere++;
      });
      // A lane whose agents all filtered out shouldn't leave a stranded label.
      proj.querySelectorAll('.lane').forEach(lane => {
        lane.hidden = !lane.querySelector('.agent-card:not([hidden])');
      });
      proj.hidden = shownHere === 0;
      visibleAgents += shownHere;
    });
    noMatchEl.hidden = visibleAgents > 0;
  }

  chipsEl.addEventListener('click', e => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    chipsEl.querySelectorAll('.chip').forEach(c => c.setAttribute('aria-pressed', 'false'));
    chip.setAttribute('aria-pressed', 'true');
    activeSlug = chip.dataset.slug;
    apply();
  });
  searchEl.addEventListener('input', apply);
  apply();
})();

const autonomousEl = document.getElementById('autonomous');
if (DATA.autonomous.length) {
  const cards = DATA.autonomous.map(a => agentCard(a)).join('');
  autonomousEl.innerHTML = `<div class="autonomous-section">
    <h3 class="project-name">Autonomous / cross-cutting agents</h3>
    <p class="autonomous-note">These agents run on scheduled or independent triggers —
      they are not launched interactively via <code>co</code>. The command below is the
      programmatic form.</p>
    <div class="agent-list">${cards}</div>
  </div>`;
}
"""


def live_html(scope: frozenset[str], whole_fleet: bool) -> str:
    """`scope`/`whole_fleet` (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M): a
    project whose repo the viewer's scope does not reach carries no card, no
    chip and no entry in the inline JSON — filtered before this page builds
    anything from it, the same filter `crews_build._visible_repos` applies
    to the identical roster/repo identity. `autonomous` is left unfiltered:
    it names agent slugs and models, never a project."""
    roster = load_roster()
    if not whole_fleet:
        visible = crew_registry.repos_in_scope(scope)
        roster = {"projects": [p for p in roster["projects"] if p["slug"] in visible],
                 "autonomous": roster["autonomous"]}
    payload = json.dumps(roster, ensure_ascii=False).replace("</", "<\\/")
    body = (
        f'<main class="roster-main" id="main">\n'
        f'  <h2 class="roster-area-title">Agent Roster</h2>\n'
        f'  <p class="roster-intro">Every project and the agents individuated for it, by lane.\n'
        f'    Each card shows the agent\'s role, model alias, and the exact launch command —\n'
        f'    interactive <code>co</code> for daytime and commercial lanes, programmatic\n'
        f'    <code>agentctl spawn</code> for overnight and autonomous agents. Data is read\n'
        f'    live from the knowledge repo and each repo\'s <code>.claude/agents</code> on\n'
        f'    every page load.</p>\n'
        f'  <div class="roster-filter" id="rosterFilter">\n'
        f'    <input type="search" id="agentSearch" class="agent-search"\n'
        f'           placeholder="Filter agents by name, model, or description…"\n'
        f'           aria-label="Filter agents">\n'
        f'    <div class="chips" id="projectChips"></div>\n'
        f'  </div>\n'
        f'  <div id="projects"></div>\n'
        f'  <div id="noMatch" class="empty" hidden>No agent matches that filter.</div>\n'
        f'  <div id="autonomous"></div>\n'
        f'</main>\n'
        f'<script id="data" type="application/json">{payload}</script>\n'
        f'<script>\n{_ROSTER_JS}\n</script>'
    )
    return ds.page_shell(
        title="Agent Roster",
        nav_html=ds.nav_component("/engineering/roster", "full"),
        body=body,
        extra_css=_ROSTER_CSS,
    )


def build_static() -> None:
    """Write the static fallback file.

    **Deliberately empty, not `live_html()` of some scope** (E19·4,
    issue_01M35K5JQ7Z7MHYZGWRSB5F85M): this file is served to whichever
    viewer's live render failed, scoped or not, so a snapshot naming every
    project's roster would leak to a scoped viewer — the same reasoning
    `crews_build.build_static` already applies to its own fallback."""
    OUT.mkdir(parents=True, exist_ok=True)
    fallback = ds.page_shell(
        title="Agent Roster",
        nav_html=ds.nav_component("/engineering/roster", "full"),
        body=ds.empty_state("Live view unavailable — reload in a moment."),
        extra_css=_ROSTER_CSS)
    (OUT / "roster.html").write_text(fallback, encoding="utf-8")
    print("Wrote site/roster.html (empty static fallback)")


if __name__ == "__main__":
    build_static()
