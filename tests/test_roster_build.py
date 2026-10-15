"""Agent roster page (roster_build.py).

Invariants:
- load_agents() reads every agent .md from the catalog, parses frontmatter, and
  returns a dict keyed by slug with title, model, and desc fields.
- load_roster() groups agents by project and lane from profile YAMLs, and
  separates autonomous / cross-cutting agents.
- live_html() embeds agent names and launch commands in the rendered HTML.
- The rendered HTML is safe against </script> injection in the payload.
- The __WHOAMI__ placeholder is present for server.py identity substitution.
- The Engineering sub-strip (nav) shows Roster on /engineering/roster.
"""
import json
from pathlib import Path

import pytest

import roster_build as R

# Captured at import, BEFORE the autouse fixture stubs it out. A test that wants
# the real filesystem-reading implementation restores from here — re-assigning
# R.load_repo_agents to itself would just re-install the stub.
_REAL_LOAD_REPO_AGENTS = R.load_repo_agents
_REAL_LOAD_KNOWLEDGE_SLUG_MAP = R._load_knowledge_slug_map


@pytest.fixture(autouse=True)
def _isolate_filesystem(tmp_path_factory, monkeypatch):
    """Make load_roster() hermetic by default. Two real-filesystem reads to isolate:

    1. CONVIVY_ROOT — discover_projects() walks it, so without this a test that
       patches PROJECTS to one fake project still gets the machine's dozen real
       repos appended.
    2. load_repo_agents() — it reads <repo_path>/.claude/agents, and the existing
       tests deliberately pass REAL repo paths because they assert on the launch
       command those paths produce. Left alone, those tests would pick up the
       machine's actual crews and grow an unexpected repo-crew lane.
    3. _load_knowledge_slug_map() — it reads agent-platform's projects.py
       off the developer's real checkout. Left alone, a test asserting on a
       project with no knowledge-slug remapping would still depend on whatever
       that file happens to contain on this machine. Tests that exercise the
       knowledge-slug mapping itself re-patch this.

    All three make assertions depend on the developer's checkout rather than the
    fixture. Tests that exercise discovery, repo crews, or knowledge-slug mapping
    re-patch the relevant one.
    """
    monkeypatch.setattr(R, "CONVIVY_ROOT", tmp_path_factory.mktemp("empty-convivy"))
    monkeypatch.setattr(R, "load_repo_agents", lambda repo_path: {})
    monkeypatch.setattr(R, "_load_knowledge_slug_map", lambda: {})


def _write_repo_agent(repo: Path, slug: str, *, model: str = "sonnet",
                      description: str = "Does a thing.") -> None:
    """Write a repo-local agent file in Claude Code's NATIVE schema (name/description),
    which is a different shape from the knowledge catalog's (kind/audience)."""
    agents_dir = repo / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{slug}.md").write_text(
        f"---\nname: {slug}\ndescription: {description}\nmodel: {model}\n"
        f"tools: Read, Edit\n---\n\nYou do the work.\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_agent(tmp_path: Path, slug: str, *,
                 title: str = "", model: str = "sonnet", desc: str = "",
                 project: str = "shared") -> None:
    """`project` selects which catalog directory the profile lands in — `shared`
    today, `agent-platform` after Phase 0 step 5's re-file. The frontmatter follows
    it, so a document written for the new location carries the new id too."""
    agents_dir = tmp_path / project / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    body = f"{desc}\n" if desc else "You do the work.\n"
    text = (f"---\nid: {project}/agents/{slug}\nkind: agent\nproject: {project}\n"
            f"title: {title or slug}\nstatus: current\naudience: [{slug}]\n"
            f"model: {model}\n---\n\n{body}")
    (agents_dir / f"{slug}.md").write_text(text, encoding="utf-8")


def _write_profile(tmp_path: Path, project: str, name: str, slugs: list[str]) -> None:
    prof_dir = tmp_path / project / "profiles"
    prof_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"profile: {project}/{name}", "", "include:", "  - kind: agent",
             "    audience_contains:"]
    for s in slugs:
        lines.append(f"      - {s}")
    (prof_dir / f"{name}.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# load_agents
# ---------------------------------------------------------------------------

def test_load_agents_parses_title_model_and_desc(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "AGENTS_DIRS", (tmp_path / "shared" / "agents",))
    _write_agent(tmp_path, "coder", title="Coder", model="sonnet",
                 desc="You implement features per a spec.")
    agents = R.load_agents()
    assert "coder" in agents
    a = agents["coder"]
    assert a["title"] == "Coder"
    assert a["model"] == "sonnet"
    assert "implement features" in a["desc"]


def test_load_agents_reads_every_catalog_directory(tmp_path, monkeypatch):
    """AGENTS_DIRS holds one directory in production and the machinery reads all of
    them, which is what makes the next re-file a one-line change.

    Phase 0 step 5 moved the 29 agent profiles from `shared` to `agent-platform`,
    and this reader lives in a different repo from the documents — so for the
    length of that migration the catalog was split across two directories and both
    had to be read. Reading only one empties the Roster page at the next RENDER
    (ARCH-182) rather than failing in CI, which is the kind of break nobody
    attributes to the change that caused it."""
    monkeypatch.setattr(R, "AGENTS_DIRS", (tmp_path / "shared" / "agents",
                                           tmp_path / "agent-platform" / "agents"))
    _write_agent(tmp_path, "coder", title="Coder", project="shared")
    _write_agent(tmp_path, "reviewer", title="Reviewer", project="agent-platform")
    agents = R.load_agents()
    assert set(agents) == {"coder", "reviewer"}, "both locations must be read"


def test_a_profile_already_refiled_wins_over_a_leftover_copy(tmp_path, monkeypatch):
    """Mid-migration a slug can exist in both places. The new location wins, because
    a profile already re-filed is the current one and a copy left at the old path is
    what is stale. Asserted rather than left to directory order."""
    monkeypatch.setattr(R, "AGENTS_DIRS", (tmp_path / "shared" / "agents",
                                           tmp_path / "agent-platform" / "agents"))
    _write_agent(tmp_path, "coder", title="Stale Coder", project="shared")
    _write_agent(tmp_path, "coder", title="Re-filed Coder", project="agent-platform")
    assert R.load_agents()["coder"]["title"] == "Re-filed Coder"


def test_row_order_does_not_lurch_when_a_document_migrates(tmp_path, monkeypatch):
    """The union is ordered by FILENAME, so a row keeps its position on the page
    whichever directory its document currently sits in.

    `load_agents`'s docstring has claimed this since the dual read landed and
    nothing tested it (reviewer, PR #341). It is correct by construction, which is
    exactly the kind of claim that drifts: sorting the merged paths by the path
    STRING instead of the filename looks equivalent, groups every row by parent
    directory, and would have reshuffled the whole page mid-migration.

    The fixture interleaves the two directories deliberately — alphabetical order
    and directory order disagree — so a directory-first sort fails here rather than
    passing by luck.
    """
    monkeypatch.setattr(R, "AGENTS_DIRS", (tmp_path / "shared" / "agents",
                                           tmp_path / "agent-platform" / "agents"))
    _write_agent(tmp_path, "architect", project="agent-platform")
    _write_agent(tmp_path, "coder", project="shared")
    _write_agent(tmp_path, "explainer", project="agent-platform")
    _write_agent(tmp_path, "reviewer", project="shared")

    agents = R.load_agents()
    assert list(agents) == sorted(agents)
    assert list(agents) == ["architect", "coder", "explainer", "reviewer"]


def test_the_new_catalog_directory_is_free_while_it_is_empty(tmp_path, monkeypatch):
    """The destination is listed before anything moves there, which is what makes
    the migration window empty. A missing directory must therefore cost nothing —
    Path.glob yields no entries rather than raising."""
    monkeypatch.setattr(R, "AGENTS_DIRS", (tmp_path / "shared" / "agents",
                                           tmp_path / "agent-platform" / "agents"))
    _write_agent(tmp_path, "coder", project="shared")
    assert set(R.load_agents()) == {"coder"}


def test_load_agents_skips_non_agent_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "AGENTS_DIRS", (tmp_path / "shared" / "agents",))
    agents_dir = tmp_path / "shared" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "runbook.md").write_text(
        "---\nkind: runbook\ntitle: A runbook\n---\nBody.\n", encoding="utf-8")
    agents = R.load_agents()
    assert not agents


def test_load_agents_empty_dir_returns_empty(tmp_path, monkeypatch):
    agents_dir = tmp_path / "shared" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(R, "AGENTS_DIRS", (agents_dir,))
    assert R.load_agents() == {}


def test_load_agents_missing_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "AGENTS_DIRS", (tmp_path / "no-such-dir",))
    assert R.load_agents() == {}


def test_load_agents_desc_skips_heading_and_blockquote(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "AGENTS_DIRS", (tmp_path / "shared" / "agents",))
    agents_dir = tmp_path / "shared" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    text = ("---\nkind: agent\ntitle: Reviewer\nstatus: current\n"
            "audience: [reviewer]\nmodel: sonnet\n---\n\n"
            "> **Write Better.**\n\n## What you do\n\nYou review diffs adversarially.\n")
    (agents_dir / "reviewer.md").write_text(text, encoding="utf-8")
    agents = R.load_agents()
    assert "reviewer" in agents
    assert "review diffs" in agents["reviewer"]["desc"]


# ---------------------------------------------------------------------------
# _load_knowledge_slug_map (issue_01M0VMAZZMN4VCGJF197GASR6Y)
# ---------------------------------------------------------------------------

def test_load_knowledge_slug_map_parses_product_map(tmp_path, monkeypatch):
    """Parses the `EXPLICIT_PROJECT_BY_NAME = {...}` literal rather than
    restating the values here."""
    fake_projects_py = tmp_path / "projects.py"
    fake_projects_py.write_text(
        'EXPLICIT_PROJECT_BY_NAME = {\n'
        '    "constance": "constance",\n'
        '    "vivy-commercial": "vivy",              # commercial workspace\n'
        '    "convivy-commercial": "company",        # re-pointed ARCH-438\n'
        '}\n'
        '\n'
        'NO_PROJECT_MAPPING = 42\n',
        encoding="utf-8")
    monkeypatch.setattr(R, "_PROJECTS_PY_PATH", fake_projects_py)
    # _REAL_LOAD_KNOWLEDGE_SLUG_MAP, not R._load_knowledge_slug_map: the autouse
    # isolation fixture above has already stubbed the latter to `lambda: {}`.
    assert _REAL_LOAD_KNOWLEDGE_SLUG_MAP() == {
        "constance": "constance",
        "vivy-commercial": "vivy",
        "convivy-commercial": "company",
    }


def test_load_knowledge_slug_map_missing_file_returns_empty(tmp_path, monkeypatch):
    """A project_slug must fall back to mapping to itself rather than crashing the
    roster page when agent-platform isn't checked out on this machine."""
    monkeypatch.setattr(R, "_PROJECTS_PY_PATH", tmp_path / "no-such-file")
    assert _REAL_LOAD_KNOWLEDGE_SLUG_MAP() == {}


def test_load_knowledge_slug_map_unparseable_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_PROJECTS_PY_PATH", tmp_path / "projects.py")
    (tmp_path / "projects.py").write_text("no product map literal here\n", encoding="utf-8")
    assert _REAL_LOAD_KNOWLEDGE_SLUG_MAP() == {}


def test_load_knowledge_slug_map_matches_the_real_projects_py():
    """Reads the REAL agent-platform/lib/agent-platform/projects.py on this machine (not a
    fixture) and confirms convivy-commercial resolves to 'company', not the
    retired 'convivy' scope Orient's old hand-copy was stuck on. This is the
    live check that a copy of the map, however carefully tested, cannot give:
    since roster_build reads the source directly, there is nothing here that
    can drift out of sync with it. Skipped when agent-platform isn't checked
    out on the machine running the tests.
    """
    if not R._PROJECTS_PY_PATH.exists():
        pytest.skip("agent-platform not checked out on this machine")
    mapping = _REAL_LOAD_KNOWLEDGE_SLUG_MAP()
    assert mapping.get("convivy-commercial") == "company"


# ---------------------------------------------------------------------------
# load_roster
# ---------------------------------------------------------------------------

def _mock_catalog():
    return {
        "coder":    {"slug": "coder",    "title": "Coder",    "model": "sonnet", "desc": "Implements features."},
        "reviewer": {"slug": "reviewer", "title": "Reviewer", "model": "sonnet", "desc": "Reviews diffs."},
        "curator":  {"slug": "curator",  "title": "Curator",  "model": "opus",   "desc": "Proposes improvements."},
    }


def test_load_roster_groups_agents_by_project_and_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    monkeypatch.setattr(R, "load_agents", _mock_catalog)
    _write_profile(tmp_path, "vivy", "daytime", ["coder", "reviewer"])

    # Override PROJECTS to point at our tmp_path fixture.
    monkeypatch.setattr(R, "PROJECTS", [
        ("vivy", "Vivy", "/fake/vivy", ["daytime"]),
    ])
    roster = R.load_roster()
    assert len(roster["projects"]) == 1
    proj = roster["projects"][0]
    assert proj["slug"] == "vivy"
    assert len(proj["lanes"]) == 1
    lane = proj["lanes"][0]
    assert lane["name"] == "daytime"
    slugs = [a["slug"] for a in lane["agents"]]
    assert "coder" in slugs and "reviewer" in slugs


def test_load_roster_autonomous_agents_not_in_daytime_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    monkeypatch.setattr(R, "load_agents", _mock_catalog)
    _write_profile(tmp_path, "vivy", "daytime", ["coder"])

    monkeypatch.setattr(R, "PROJECTS", [
        ("vivy", "Vivy", "/fake/vivy", ["daytime"]),
    ])
    roster = R.load_roster()
    auto_slugs = [a["slug"] for a in roster["autonomous"]]
    # curator is in the catalog but not in any daytime profile — it's autonomous.
    assert "curator" in auto_slugs
    # coder IS in a daytime profile — it must not appear as autonomous.
    assert "coder" not in auto_slugs


def test_load_roster_missing_profile_silently_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    monkeypatch.setattr(R, "load_agents", _mock_catalog)

    monkeypatch.setattr(R, "PROJECTS", [
        ("vivy", "Vivy", "/fake/vivy", ["daytime"]),  # no daytime.yaml created
    ])
    roster = R.load_roster()
    assert roster["projects"][0]["lanes"] == []


def test_load_roster_launch_command_has_repo_path(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    monkeypatch.setattr(R, "load_agents", _mock_catalog)
    _write_profile(tmp_path, "vivy", "daytime", ["coder"])

    monkeypatch.setattr(R, "PROJECTS", [
        ("vivy", "Vivy", "/srv/convivy/vivy", ["daytime"]),
    ])
    roster = R.load_roster()
    lane = roster["projects"][0]["lanes"][0]
    coder = next(a for a in lane["agents"] if a["slug"] == "coder")
    assert "/srv/convivy/vivy" in coder["launch"]
    assert "co c" in coder["launch"]


def test_overnight_lane_uses_agentctl_spawn(tmp_path, monkeypatch):
    """Agents in the overnight lane get an agentctl spawn command, not a co command."""
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    catalog = {
        "overnight-orchestrator": {
            "slug": "overnight-orchestrator",
            "title": "Overnight Orchestrator",
            "model": "sonnet",
            "desc": "Runs the overnight wave.",
        },
    }
    monkeypatch.setattr(R, "load_agents", lambda: catalog)
    _write_profile(tmp_path, "vivy", "overnight", ["overnight-orchestrator"])

    monkeypatch.setattr(R, "PROJECTS", [
        ("vivy", "Vivy", "/srv/convivy/vivy", ["overnight"]),
    ])
    roster = R.load_roster()
    lane = roster["projects"][0]["lanes"][0]
    assert lane["name"] == "overnight"
    agent = next(a for a in lane["agents"] if a["slug"] == "overnight-orchestrator")
    assert "agentctl spawn" in agent["launch"]
    assert "co overnight-orchestrator" not in agent["launch"]
    assert "co " not in agent["launch"]


def test_daytime_lane_uses_co_command(tmp_path, monkeypatch):
    """Agents in the daytime lane still get the interactive co command."""
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    catalog = {
        "coder": {
            "slug": "coder",
            "title": "Coder",
            "model": "sonnet",
            "desc": "Implements features.",
        },
    }
    monkeypatch.setattr(R, "load_agents", lambda: catalog)
    _write_profile(tmp_path, "vivy", "daytime", ["coder"])

    monkeypatch.setattr(R, "PROJECTS", [
        ("vivy", "Vivy", "/srv/convivy/vivy", ["daytime"]),
    ])
    roster = R.load_roster()
    lane = roster["projects"][0]["lanes"][0]
    assert lane["name"] == "daytime"
    agent = next(a for a in lane["agents"] if a["slug"] == "coder")
    # coder has the alias "co c" — any co-form counts.
    assert "co" in agent["launch"]
    assert "agentctl" not in agent["launch"]
    assert "/srv/convivy/vivy" in agent["launch"]


def test_commercial_lane_uses_co_command(tmp_path, monkeypatch):
    """Agents in the commercial lane still get the interactive co command."""
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    catalog = {
        "product-manager": {
            "slug": "product-manager",
            "title": "Product Manager",
            "model": "opus",
            "desc": "Manages the product roadmap.",
        },
    }
    monkeypatch.setattr(R, "load_agents", lambda: catalog)
    _write_profile(tmp_path, "vivy", "commercial", ["product-manager"])

    monkeypatch.setattr(R, "PROJECTS", [
        ("vivy", "Vivy", "/srv/convivy/vivy", ["commercial"]),
    ])
    roster = R.load_roster()
    lane = roster["projects"][0]["lanes"][0]
    assert lane["name"] == "commercial"
    agent = next(a for a in lane["agents"] if a["slug"] == "product-manager")
    assert "co" in agent["launch"]
    assert "agentctl" not in agent["launch"]
    assert "/srv/convivy/vivy" in agent["launch"]


def test_overnight_agentctl_command_uses_project_slug(tmp_path, monkeypatch):
    """The agentctl spawn command in the overnight lane uses the product slug, not the repo path."""
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    catalog = {
        "refactorer": {
            "slug": "refactorer",
            "title": "Refactorer",
            "model": "sonnet",
            "desc": "Refactors code for clarity.",
        },
    }
    monkeypatch.setattr(R, "load_agents", lambda: catalog)
    _write_profile(tmp_path, "constance", "overnight", ["refactorer"])

    monkeypatch.setattr(R, "PROJECTS", [
        ("constance", "Constance", "/srv/convivy/constance", ["overnight"]),
    ])
    roster = R.load_roster()
    lane = roster["projects"][0]["lanes"][0]
    agent = next(a for a in lane["agents"] if a["slug"] == "refactorer")
    assert "agentctl spawn refactorer --on constance" in agent["launch"]


def test_load_roster_autonomous_launch_uses_agentctl(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    monkeypatch.setattr(R, "load_agents", _mock_catalog)
    # No daytime profile — curator stays autonomous.
    monkeypatch.setattr(R, "PROJECTS", [])
    roster = R.load_roster()
    auto = {a["slug"]: a for a in roster["autonomous"]}
    assert "agentctl spawn" in auto["curator"]["launch"]


def test_load_roster_commercial_profile_aliases(tmp_path, monkeypatch):
    """convivy-commercial uses the positioning-strategist default alias.

    The profile lives under the 'company' knowledge dir (via
    _load_knowledge_slug_map), not under a 'convivy-commercial' dir that
    doesn't exist in knowledge, and not under the retired 'convivy' scope
    (issue_01M0VMAZZMN4VCGJF197GASR6Y).
    """
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    monkeypatch.setattr(R, "_load_knowledge_slug_map",
                        lambda: {"convivy-commercial": "company"})
    catalog = {
        "positioning-strategist": {
            "slug": "positioning-strategist",
            "title": "Positioning Strategist",
            "model": "opus",
            "desc": "Owns company-level positioning.",
        },
    }
    monkeypatch.setattr(R, "load_agents", lambda: catalog)
    # Profile must live under the mapped knowledge slug ("company"), not "convivy-commercial".
    _write_profile(tmp_path, "company", "commercial", ["positioning-strategist"])

    monkeypatch.setattr(R, "PROJECTS", [
        ("convivy-commercial", "Convivy Commercial",
         "/srv/convivy/convivy-commercial", ["commercial"]),
    ])
    roster = R.load_roster()
    lane = roster["projects"][0]["lanes"][0]
    agent = lane["agents"][0]
    # In convivy-commercial the default alias for positioning-strategist is bare `co`.
    assert "co" in agent["launch"]
    assert "/srv/convivy/convivy-commercial" in agent["launch"]


def test_load_roster_convivy_commercial_resolves_via_knowledge_slug(tmp_path, monkeypatch):
    """convivy-commercial resolves to the 'company' knowledge dir
    (_load_knowledge_slug_map mapping) — the value agent-platform's
    EXPLICIT_PROJECT_BY_NAME holds since ARCH-438 re-pointed it, not the retired 'convivy' scope Orient's
    old hand-copy stuck on (issue_01M0VMAZZMN4VCGJF197GASR6Y).

    A profile at knowledge/convivy-commercial/profiles/commercial.yaml must NOT be
    found (that path doesn't exist in production). The profile at
    knowledge/company/profiles/commercial.yaml MUST be found.
    """
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    monkeypatch.setattr(R, "_load_knowledge_slug_map",
                        lambda: {"convivy-commercial": "company"})
    catalog = {
        "positioning-strategist": {
            "slug": "positioning-strategist",
            "title": "Positioning Strategist",
            "model": "opus",
            "desc": "Owns company-level positioning.",
        },
        "commercial-writer": {
            "slug": "commercial-writer",
            "title": "Commercial Writer",
            "model": "sonnet",
            "desc": "Writes commercial copy.",
        },
        "product-analyst": {
            "slug": "product-analyst",
            "title": "Product Analyst",
            "model": "sonnet",
            "desc": "Runs quantitative analysis.",
        },
    }
    monkeypatch.setattr(R, "load_agents", lambda: catalog)
    # Write only under "company" — no "convivy-commercial" dir in knowledge.
    _write_profile(tmp_path, "company", "commercial",
                   ["positioning-strategist", "commercial-writer", "product-analyst"])

    monkeypatch.setattr(R, "PROJECTS", [
        ("convivy-commercial", "Convivy Commercial",
         "/srv/convivy/convivy-commercial", ["commercial"]),
    ])
    roster = R.load_roster()
    assert len(roster["projects"]) == 1
    proj = roster["projects"][0]
    # Lane must not be empty.
    assert len(proj["lanes"]) == 1
    lane = proj["lanes"][0]
    assert lane["name"] == "commercial"
    slugs = {a["slug"] for a in lane["agents"]}
    assert "positioning-strategist" in slugs
    assert "commercial-writer" in slugs
    assert "product-analyst" in slugs


def test_load_roster_commercial_lane_renders_for_vivy(tmp_path, monkeypatch):
    """The commercial lane renders for a standard product (vivy/commercial.yaml)."""
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    catalog = {
        "product-manager": {
            "slug": "product-manager",
            "title": "Product Manager",
            "model": "opus",
            "desc": "Manages the product roadmap.",
        },
        "product-analyst": {
            "slug": "product-analyst",
            "title": "Product Analyst",
            "model": "sonnet",
            "desc": "Runs quantitative analysis.",
        },
    }
    monkeypatch.setattr(R, "load_agents", lambda: catalog)
    _write_profile(tmp_path, "vivy", "commercial", ["product-manager", "product-analyst"])

    monkeypatch.setattr(R, "PROJECTS", [
        ("vivy", "Vivy", "/srv/convivy/vivy", ["commercial"]),
    ])
    roster = R.load_roster()
    proj = roster["projects"][0]
    assert len(proj["lanes"]) == 1
    lane = proj["lanes"][0]
    assert lane["name"] == "commercial"
    slugs = {a["slug"] for a in lane["agents"]}
    assert "product-manager" in slugs
    assert "product-analyst" in slugs


def test_load_roster_commercial_only_agent_not_autonomous(tmp_path, monkeypatch):
    """An agent appearing only in a commercial profile is NOT classified as autonomous.

    This locks the fix for the bug where agents in non-daytime interactive profiles
    (e.g. convivy-commercial's commercial lane) fell through into the autonomous
    bucket, producing a contradiction: the page showed them with a `co` launch
    command in the commercial lane AND in the autonomous "not launched via co" section.
    """
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    catalog = {
        "positioning-strategist": {
            "slug": "positioning-strategist",
            "title": "Positioning Strategist",
            "model": "opus",
            "desc": "Owns company-level positioning.",
        },
        "commercial-writer": {
            "slug": "commercial-writer",
            "title": "Commercial Writer",
            "model": "sonnet",
            "desc": "Writes commercial copy.",
        },
        "curator": {
            "slug": "curator",
            "title": "Curator",
            "model": "opus",
            "desc": "Proposes platform improvements.",
        },
    }
    monkeypatch.setattr(R, "load_agents", lambda: catalog)
    monkeypatch.setattr(R, "_load_knowledge_slug_map",
                        lambda: {"convivy-commercial": "company"})
    # positioning-strategist and commercial-writer appear only in a commercial profile.
    # curator appears in no profile at all.
    _write_profile(tmp_path, "company", "commercial",
                   ["positioning-strategist", "commercial-writer"])

    monkeypatch.setattr(R, "PROJECTS", [
        ("convivy-commercial", "Convivy Commercial",
         "/srv/convivy/convivy-commercial", ["commercial"]),
    ])
    roster = R.load_roster()

    auto_slugs = {a["slug"] for a in roster["autonomous"]}
    # Commercial-lane agents must NOT appear in the autonomous section.
    assert "positioning-strategist" not in auto_slugs
    assert "commercial-writer" not in auto_slugs
    # curator is in no profile — it IS autonomous.
    assert "curator" in auto_slugs

    # Confirm they appear in the commercial lane of the project.
    proj = roster["projects"][0]
    assert len(proj["lanes"]) == 1
    lane = proj["lanes"][0]
    assert lane["name"] == "commercial"
    lane_slugs = {a["slug"] for a in lane["agents"]}
    assert "positioning-strategist" in lane_slugs
    assert "commercial-writer" in lane_slugs


# ---------------------------------------------------------------------------
# live_html
# ---------------------------------------------------------------------------

def _mock_roster():
    return {
        "projects": [
            {
                "slug": "vivy",
                "label": "Vivy",
                "repo": "/srv/convivy/vivy",
                "lanes": [
                    {
                        "name": "daytime",
                        "label": "Daytime (interactive)",
                        "agents": [
                            {
                                "slug": "coder",
                                "title": "Coder",
                                "model": "sonnet",
                                "desc": "You implement features per a spec.",
                                "launch": "cd /srv/convivy/vivy && co c",
                            },
                        ],
                    }
                ],
            }
        ],
        "autonomous": [
            {
                "slug": "curator",
                "title": "Curator",
                "model": "opus",
                "desc": "You propose improvements to the agent platform.",
                "launch": 'agentctl spawn curator --on <repo> --task "<task>"',
            }
        ],
    }


def test_live_html_embeds_agent_names(monkeypatch):
    monkeypatch.setattr(R, "load_roster", _mock_roster)
    html = R.live_html(frozenset(), True)
    assert "Coder" in html
    assert "Curator" in html
    assert "Vivy" in html


def test_live_html_embeds_launch_commands(monkeypatch):
    monkeypatch.setattr(R, "load_roster", _mock_roster)
    html = R.live_html(frozenset(), True)
    assert "/srv/convivy/vivy" in html
    assert "agentctl spawn curator" in html


def test_live_html_whoami_placeholder_present(monkeypatch):
    monkeypatch.setattr(R, "load_roster", _mock_roster)
    html = R.live_html(frozenset(), True)
    assert "__WHOAMI__" in html


def test_live_html_escapes_script_injection(monkeypatch):
    """A launch command containing </script> must not break out of the JSON payload."""
    roster = _mock_roster()
    roster["projects"][0]["lanes"][0]["agents"][0]["launch"] = (
        "cd /tmp && echo </script><script>alert(1)</script>")
    monkeypatch.setattr(R, "load_roster", lambda: roster)
    html = R.live_html(frozenset(), True)
    assert "</script><script>alert(1)" not in html
    assert "<\\/script>" in html


def test_live_html_carries_nav_with_engineering_area(monkeypatch):
    monkeypatch.setattr(R, "load_roster", _mock_roster)
    html = R.live_html(frozenset(), True)
    # Engineering (= the roster) is its own single-page area (ARCH-355)
    assert 'href="/engineering/roster" aria-current="page"' in html
    # the KB/briefs live under Documents now, not on this page's sub-strip
    assert 'href="/reference"' in html


def test_live_html_empty_projects_shows_empty_state(monkeypatch):
    monkeypatch.setattr(R, "load_roster", lambda: {"projects": [], "autonomous": []})
    html = R.live_html(frozenset(), True)
    assert "No projects found" in html


# --- scope (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) -------------------------


def test_live_html_drops_a_project_whose_repo_is_out_of_scope(tmp_path, monkeypatch):
    roster = _mock_roster()
    roster["projects"].append({
        "slug": "selector", "label": "Selector", "repo": "/x",
        "lanes": [{"name": "daytime", "label": "Daytime (interactive)", "agents": [
            {"slug": "sel-coder", "title": "Sel Coder", "model": "sonnet",
             "desc": "d", "launch": "co sel-coder"}]}],
    })
    monkeypatch.setattr(R, "load_roster", lambda: roster)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "vivy": {"crew": "vivy", "lifecycle": "active", "repos": ["vivy"]},
        "selector": {"crew": "selector", "lifecycle": "active", "repos": ["selector"]},
    }))
    monkeypatch.setattr(R.crew_registry, "REGISTRY", registry)
    html = R.live_html(frozenset({"vivy"}), False)
    assert '"label": "Vivy"' in html
    assert '"label": "Selector"' not in html
    assert "sel-coder" not in html


def test_build_static_seeds_an_empty_fallback_not_real_data(tmp_path, monkeypatch):
    """roster.html is served to ANY viewer's failed render, scoped or not
    (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M) — a snapshot naming a real
    project's roster would leak to a scoped viewer."""
    monkeypatch.setattr(R, "load_roster", _mock_roster)
    monkeypatch.setattr(R, "OUT", tmp_path)
    R.build_static()
    html = (tmp_path / "roster.html").read_text(encoding="utf-8")
    assert "Coder" not in html
    assert "Curator" not in html
    assert "Live view unavailable" in html


def test_live_html_keeps_autonomous_agents_unfiltered(tmp_path, monkeypatch):
    """`autonomous` names agent slugs and models, never a project, so it is
    not scope-filtered at all."""
    monkeypatch.setattr(R, "load_roster", _mock_roster)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({}))
    monkeypatch.setattr(R.crew_registry, "REGISTRY", registry)
    html = R.live_html(frozenset(), False)
    assert "Curator" in html
    assert "Vivy" not in html


# ---------------------------------------------------------------------------
# Repo-local crews + project discovery (ARCH-361 follow-up)
# ---------------------------------------------------------------------------

def test_load_repo_agents_parses_native_claude_code_schema(tmp_path):
    """Repo-local files use `name:`/`description:`, NOT the catalog's
    `kind:`/`audience:`. Reading them with the catalog parser yields nothing."""
    repo = tmp_path / "selector"
    _write_repo_agent(repo, "sel-coder", model="sonnet",
                      description="Implements features for Selector.")
    agents = _REAL_LOAD_REPO_AGENTS(str(repo))
    assert "sel-coder" in agents
    a = agents["sel-coder"]
    assert a["model"] == "sonnet"
    assert "Implements features" in a["desc"]
    # No `title:` in the native schema — derived from the slug.
    assert a["title"] == "Sel Coder"


def test_load_repo_agents_skips_file_without_name(tmp_path):
    """The slug is what agentctl dispatches on; a record without one is unusable."""
    repo = tmp_path / "selector"
    agents_dir = repo / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "broken.md").write_text(
        "---\ndescription: no name key\n---\n\nbody\n", encoding="utf-8")
    assert _REAL_LOAD_REPO_AGENTS(str(repo)) == {}


def test_load_repo_agents_missing_dir_is_empty_not_error(tmp_path):
    assert _REAL_LOAD_REPO_AGENTS(str(tmp_path / "nope")) == {}


def test_project_with_no_profiles_renders_its_repo_crew(tmp_path, monkeypatch):
    """The bug the operator hit: selector/hollis/convivy-app-kit have NO profiles YAML, so
    the profile path can never surface them and their whole crew was invisible."""
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path / "kb")
    monkeypatch.setattr(R, "load_agents", lambda: {})
    repo = tmp_path / "selector"
    _write_repo_agent(repo, "sel-coder")
    _write_repo_agent(repo, "sel-reviewer")
    monkeypatch.setattr(R, "load_repo_agents", _REAL_LOAD_REPO_AGENTS)
    monkeypatch.setattr(R, "PROJECTS", [("selector", "Selector", str(repo), [])])

    roster = R.load_roster()
    proj = roster["projects"][0]
    assert [ln["name"] for ln in proj["lanes"]] == ["repo-crew"]
    assert {a["slug"] for a in proj["lanes"][0]["agents"]} == {"sel-coder", "sel-reviewer"}


def test_individuated_generic_agent_is_not_duplicated(tmp_path, monkeypatch):
    """A repo's local `coder` is the individuated copy of the catalog `coder`,
    already rendered in its profile lane — it must not also appear as repo crew."""
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    monkeypatch.setattr(R, "load_agents", lambda: {
        "coder": {"slug": "coder", "title": "Coder", "model": "sonnet", "desc": "d"}})
    _write_profile(tmp_path, "vivy", "daytime", ["coder"])
    repo = tmp_path / "vivy"
    _write_repo_agent(repo, "coder")          # individuated generic — dedup
    _write_repo_agent(repo, "vivy-special")   # product-specific — must show
    monkeypatch.setattr(R, "load_repo_agents", _REAL_LOAD_REPO_AGENTS)
    monkeypatch.setattr(R, "PROJECTS", [("vivy", "Vivy", str(repo), ["daytime"])])

    roster = R.load_roster()
    lanes = {ln["name"]: [a["slug"] for a in ln["agents"]]
             for ln in roster["projects"][0]["lanes"]}
    assert lanes["daytime"] == ["coder"]
    assert lanes["repo-crew"] == ["vivy-special"]
    # coder appears exactly once across the whole project
    everywhere = [s for slugs in lanes.values() for s in slugs]
    assert everywhere.count("coder") == 1


def test_profile_slug_missing_everywhere_renders_unresolved_not_dropped(tmp_path, monkeypatch):
    """Previously a profile naming an unknown agent was silently `continue`d, so a
    stale reference and a deliberately-empty lane looked identical on the page."""
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    monkeypatch.setattr(R, "load_agents", lambda: {})
    _write_profile(tmp_path, "vivy", "daytime", ["ghost-agent"])
    monkeypatch.setattr(R, "PROJECTS", [("vivy", "Vivy", str(tmp_path / "vivy"), ["daytime"])])

    roster = R.load_roster()
    agents = roster["projects"][0]["lanes"][0]["agents"]
    assert [a["slug"] for a in agents] == ["ghost-agent"]
    assert agents[0]["unresolved"] is True


def test_discovery_finds_a_repo_not_listed_in_projects(tmp_path, monkeypatch):
    """The page's failure mode was going stale: PROJECTS was hand-maintained, so a
    new project's crew stayed invisible until someone noticed. Discovery fixes that."""
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path / "kb")
    monkeypatch.setattr(R, "load_agents", lambda: {})
    root = tmp_path / "Convivy"
    _write_repo_agent(root / "brand-new-product", "bnp-coder")
    monkeypatch.setattr(R, "CONVIVY_ROOT", root)
    monkeypatch.setattr(R, "load_repo_agents", _REAL_LOAD_REPO_AGENTS)
    monkeypatch.setattr(R, "PROJECTS", [])

    roster = R.load_roster()
    slugs = {p["slug"] for p in roster["projects"]}
    assert "brand-new-product" in slugs
    proj = next(p for p in roster["projects"] if p["slug"] == "brand-new-product")
    assert proj["label"] == "Brand New Product"
    assert [a["slug"] for a in proj["lanes"][0]["agents"]] == ["bnp-coder"]


def test_discovery_skips_infrastructure_repos(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path / "kb")
    monkeypatch.setattr(R, "load_agents", lambda: {})
    root = tmp_path / "Convivy"
    _write_repo_agent(root / "knowledge", "kb-agent")
    monkeypatch.setattr(R, "CONVIVY_ROOT", root)
    monkeypatch.setattr(R, "PROJECTS", [])
    assert R.discover_projects() == []


def test_page_renders_filter_controls(tmp_path, monkeypatch):
    """The page lists 130+ agents; without a filter it's an unusable scroll."""
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path)
    monkeypatch.setattr(R, "load_agents", lambda: {
        "coder": {"slug": "coder", "title": "Coder", "model": "sonnet", "desc": "d"}})
    _write_profile(tmp_path, "vivy", "daytime", ["coder"])
    monkeypatch.setattr(R, "PROJECTS", [("vivy", "Vivy", str(tmp_path / "vivy"), ["daytime"])])
    html = R.live_html(frozenset(), True)
    assert 'id="projectChips"' in html
    assert 'id="agentSearch"' in html
    assert 'data-search=' in html
    # [hidden] must beat the display rules, or filtering silently does nothing
    assert "[hidden] { display: none !important; }" in html


def test_parse_profile_stops_at_sibling_include_entry(tmp_path):
    """A blank line inside audience_contains must NOT let the next sibling
    include entry be swallowed as an agent slug.

    Real profiles look like this, and the old parser exited the block only on a
    non-blank line — so `- kind: skill` became a phantom agent named
    "kind: skill" on every profile-based project.
    """
    prof = tmp_path / "vivy" / "profiles"
    prof.mkdir(parents=True)
    (prof / "daytime.yaml").write_text(
        "profile: vivy/daytime\n"
        "\n"
        "include:\n"
        "  - kind: agent\n"
        "    audience_contains:\n"
        "      - planner\n"
        "      - coder\n"
        "\n"                      # the blank line that broke it
        "  - kind: skill\n"
        "    project: [vivy, shared]\n"
        "    status: current\n"
        "\n"
        "  - kind: architecture\n"
        "    project: [vivy, shared]\n",
        encoding="utf-8")
    slugs = R._parse_profile(prof / "daytime.yaml")
    assert slugs == ["planner", "coder"]
    assert not any(":" in s for s in slugs), f"swallowed a sibling include entry: {slugs}"


def test_parse_profile_keeps_slugs_across_internal_blank_lines(tmp_path):
    """Blank lines are legal inside a YAML sequence — they must be skipped, not
    treated as terminators, or grouped profiles would lose their later agents."""
    prof = tmp_path / "vivy" / "profiles"
    prof.mkdir(parents=True)
    (prof / "daytime.yaml").write_text(
        "include:\n"
        "  - kind: agent\n"
        "    audience_contains:\n"
        "      - planner\n"
        "\n"
        "      - coder\n"
        "      # a comment\n"
        "      - reviewer\n",
        encoding="utf-8")
    assert R._parse_profile(prof / "daytime.yaml") == ["planner", "coder", "reviewer"]


def test_repo_crew_launch_uses_agentctl_not_co(tmp_path, monkeypatch):
    """Repo-local agents are DISPATCHED subagents, not interactive co personas.
    Rendering `co <slug>` was wrong — substrate's scout documents itself as
    `agentctl spawn scout --on <repo>`."""
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path / "kb")
    monkeypatch.setattr(R, "load_agents", lambda: {})
    repo = tmp_path / "substrate"
    _write_repo_agent(repo, "scout")
    monkeypatch.setattr(R, "load_repo_agents", _REAL_LOAD_REPO_AGENTS)
    monkeypatch.setattr(R, "PROJECTS", [("substrate", "Substrate", str(repo), [])])

    launch = R.load_roster()["projects"][0]["lanes"][0]["agents"][0]["launch"]
    assert launch.startswith("agentctl spawn scout --on substrate")
    assert "co scout" not in launch


@pytest.mark.needs_kb
def test_discovery_skip_repos_have_no_kb_crew():
    """A `_DISCOVERY_SKIP` entry claims nobody launches a crew against that repo.
    Nothing re-tested that claim, so it went stale: convivy-orient sat in the
    skip list from 2026-07-17 (git log -S'_DISCOVERY_SKIP' on roster_build.py)
    a month after this repo gained its own KB crew
    (knowledge/projects/convivy-orient/agents/orient-coder.md, added
    2026-08-17), which hid this repo's own crew from the roster the whole time.

    Checks the MECHANISM rather than the current set's contents, so it flips the
    day any skipped repo gains a crew rather than only pinning today's list: a
    repo with an agents/*.md file in the knowledge repo is, by definition, one
    somebody dispatches a crew against, so it cannot legitimately also be in
    `_DISCOVERY_SKIP`.

    Reads the live knowledge checkout, so — following
    test_build_product_groups.py's `test_every_indexed_project_is_placed` — it
    confirms the source is present and non-empty before trusting an empty
    violation set: an absent or empty knowledge checkout would otherwise read
    here as "no skip entry violates the rule" when really nothing was checked.

    That existence check alone isn't enough: it confirms KNOWLEDGE_ROOT has
    project subdirectories, not that `<slug>/agents/*.md` is still the shape a
    crew lives at. If that convention moved repo-wide, every project's
    `agents/` dir would be missing, `violators` would come back empty for every
    skip entry, and this test would report "no violation" having never
    actually looked at a crew. So confirm at least one real project still has a
    non-empty `agents/` dir before trusting an empty `violators` — that's the
    live proof the glob below is aimed at the right shape.
    """
    root = R.KNOWLEDGE_ROOT
    assert root.is_dir(), f"no knowledge checkout at {root}"
    project_dirs = [p for p in root.iterdir() if p.is_dir()]
    assert project_dirs, f"{root} exists but is empty; can't trust an empty violation set"

    # Inner any() forces the glob to evaluate: a bare Path.glob() iterator is
    # always truthy, so without it the outer any() would short-circuit True on
    # the first project regardless of whether it has any *.md files.
    assert any(any((p / "agents").glob("*.md")) for p in project_dirs), (
        f"no project under {root} has an agents/*.md file; the convention this "
        f"test's glob relies on may have moved, which would make an empty "
        f"violators set below meaningless rather than reassuring")

    violators = sorted(
        slug for slug in R._DISCOVERY_SKIP
        if (root / slug / "agents").is_dir()
        and any((root / slug / "agents").glob("*.md"))
    )
    assert not violators, (
        f"_DISCOVERY_SKIP claims nobody dispatches a crew against these repos, "
        f"but the knowledge repo has agent definitions for them: {violators}. "
        f"Either the crew is stale (remove it) or the skip entry is stale (drop "
        f"it and give the repo a PROJECTS entry).")


def test_discovery_skips_worktree_pools_and_symlinks(tmp_path, monkeypatch):
    """A worktree is a FULL checkout, so it has its own .claude/agents. A pool dir
    (hollis-worktrees, convivy-app-kit-worktrees) would otherwise become a phantom
    project the first time a worktree is created in it — and worktrees are created
    constantly. Matched by suffix so a NEW pool is covered without a code change.
    """
    monkeypatch.setattr(R, "KNOWLEDGE_ROOT", tmp_path / "kb")
    monkeypatch.setattr(R, "load_agents", lambda: {})
    root = tmp_path / "Convivy"
    _write_repo_agent(root / "hollis-worktrees" / "wt1", "hollis-coder")
    _write_repo_agent(root / "real-product", "rp-coder")
    # a symlink pointing outside the tree must not be followed
    outside = tmp_path / "outside"
    _write_repo_agent(outside, "sneaky")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(R, "CONVIVY_ROOT", root)
    monkeypatch.setattr(R, "load_repo_agents", _REAL_LOAD_REPO_AGENTS)
    monkeypatch.setattr(R, "PROJECTS", [])

    slugs = {p[0] for p in R.discover_projects()}
    assert slugs == {"real-product"}, f"unexpected projects discovered: {slugs}"
