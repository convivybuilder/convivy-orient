#!/usr/bin/env python3.11
"""The crew keys a dispatch looks up its tool policy under, read from the
project registry agentctl reads.

`agentctl spawn` resolves a run's crew with `projects.crew_for_repo`, which
returns a registry unit's `crew` field, and asks the Library for the policy of
(agent, THAT crew, model). The policy table is exact-match, so a row written
under any other string binds nothing. Project slugs are not crew keys: an
infrastructure repo's crew key can differ from the repo name itself, and two
repos can share one crew.

**The same registry agentctl reads** (platform decision 0098): the document
the Library serves at `GET /registry`, read through `library_client` the way
agent-platform's `projects.py` reads it, or the file `$PROJECTS_JSON` names
when set (tests, and a packaged instance with a registry of its own). No
machine keeps a registry file, so there is no other fallback.

The Library read is cached per process for `_REGISTRY_TTL_S`, failures
included: one /overview render reads the registry about 1,800 times, which a
file read absorbed and an HTTP call per read would not. A registry change
reaches every page within that window. The `$PROJECTS_JSON` file is read
fresh every time, as before.

**A private crew is never listed** (ADR-0043's black-box crews). A unit is
private when its `private` field is `True`, read the way
`projects.private_slugs()` reads it.

An unreadable registry raises `RegistryUnavailable`: the Library unreachable,
any answer other than 200 (503 while its table is empty, 403 without
`registry.read`), or a body that is not a JSON object. A caller can then say
the list is unavailable rather than offer an empty one, and never falls back
to a literal list, which would hide exactly the drift this module exists to
catch.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path

import library_client

# The 0.7.0 ruling keeps "convivy" as the publisher's own name and GitHub
# identifier; agent-platform's own `projects.repo_github_slug` defaults here
# too, so this is the one literal this module carries rather than a value
# every call site would otherwise hardcode for itself.
DEFAULT_GITHUB_ORG = "convivy"


class RegistryUnavailable(Exception):
    """The registry could not be read, from the Library or the override file."""


def _builder_root() -> Path:
    """`$BUILDER_ROOT` when set, else the parent of agent-platform's checkout
    — `Path.home() / "Convivy"` on every install today."""
    env = os.environ.get("BUILDER_ROOT")
    return Path(env) if env else Path.home() / "Convivy"


def resolve_registry_path() -> Path | None:
    """The override file `$PROJECTS_JSON` names, matching agentctl, or `None`
    when it is unset and the registry comes from the Library."""
    env = os.environ.get("PROJECTS_JSON")
    return Path(env) if env else None


_REGISTRY_ROUTE = "/registry"
_REGISTRY_TTL_S = 60.0
_cache_lock = threading.Lock()
# (monotonic time fetched, document or the RegistryUnavailable it raised)
_cached: tuple[float, dict | RegistryUnavailable] | None = None


def _fetch_from_library() -> dict:
    """The whole registry document from the Library's `GET /registry`, on
    Orient's agent credential. Every failure raises `RegistryUnavailable`."""
    try:
        doc = library_client._request(_REGISTRY_ROUTE)
    except Exception as e:
        code = getattr(e, "code", None)
        detail = f"returned {code}" if code is not None else f"failed: {type(e).__name__}"
        raise RegistryUnavailable(f"GET {_REGISTRY_ROUTE} {detail}") from e
    if not isinstance(doc, dict):
        raise RegistryUnavailable(
            f"GET {_REGISTRY_ROUTE} returned a {type(doc).__name__}, not a JSON object")
    return doc


def clear_cache() -> None:
    """Forget the cached Library read, so the next read asks the Library."""
    global _cached
    with _cache_lock:
        _cached = None


def _read_registry() -> dict:
    """The registry document: the `$PROJECTS_JSON` file when set, else the
    Library's, cached for `_REGISTRY_TTL_S`. Raises `RegistryUnavailable` on
    any failure — a caller that needs to degrade instead of raise catches it
    at the call site, the way `covers_every_project` and its siblings below
    already do."""
    global _cached
    path = resolve_registry_path()
    if path is not None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise RegistryUnavailable(str(e)) from e
    with _cache_lock:
        now = time.monotonic()
        if _cached is None or now - _cached[0] >= _REGISTRY_TTL_S:
            try:
                _cached = (now, _fetch_from_library())
            except RegistryUnavailable as e:
                _cached = (now, e)
        outcome = _cached[1]
    if isinstance(outcome, RegistryUnavailable):
        raise RegistryUnavailable(str(outcome))
    # A copy, so a caller that edits what it gets cannot change what every
    # later reader sees: each call returned a fresh parse before the cache.
    return copy.deepcopy(outcome)


def units() -> dict[str, dict]:
    """Every top-level project entry, keyed by slug — the registry with its
    `_`-prefixed metadata keys (`_meta`, `_products`, `_scopes`, `_kb_legacy`)
    excluded. Raises `RegistryUnavailable` like `public_crews()`."""
    raw = _read_registry()
    return {slug: unit for slug, unit in raw.items() if not slug.startswith("_")}


def products() -> dict[str, dict]:
    """The registry's `_products` map, in its written order: a commercial
    product key to its `display` and `owner`. Raises `RegistryUnavailable`
    like `units()`."""
    return _read_registry().get("_products", {})


def kb_projects() -> dict[str, dict]:
    """The registry's `_kb_projects` map, in its written order: a KB project
    name (the key `build.STANDALONE_PRODUCTS` holds) to its own fields, which
    may include `standalone: true`. Raises `RegistryUnavailable` like
    `units()`."""
    return _read_registry().get("_kb_projects", {})


def kb_projects_in_section(section: str) -> list[str]:
    """`_kb_projects` keys whose own `section` field equals `section`, in
    the map's own written order (CQ-171 slice 5, the steward's
    registry-fields ruling). Raises `RegistryUnavailable` like `units()`."""
    return [slug for slug, info in kb_projects().items() if info.get("section") == section]


def product_groups() -> dict[str, str]:
    """Every `_products` key's own `group` field, in `_products`' own
    written order; a key with no `group` is omitted (CQ-171 slice 5, the
    steward's registry-fields ruling). Raises `RegistryUnavailable` like
    `units()`."""
    return {slug: info["group"] for slug, info in products().items()
            if isinstance(info.get("group"), str) and info["group"].strip()}


def code_health_repos() -> list[tuple[str, str]]:
    """Every unit's own `code_health` array of repo names, flattened to
    (repo, owning unit's slug) pairs — unit order, then that unit's own
    array order (CQ-171 slice 5, the steward's registry-fields ruling).

    A `code_health` entry only counts when it is also a member of that same
    unit's own `repos`: a repo can enter the code-health harvest only
    through its unit's own declared `repos`, never through `code_health`
    alone, so a stray or hand-edited entry (or one a registry-writing tool
    gets wrong) is dropped here rather than reaching `git`/`gh` unvalidated.
    Raises `RegistryUnavailable` like `units()`."""
    out: list[tuple[str, str]] = []
    for slug, unit in units().items():
        own_repos = set(unit.get("repos") or ())
        for repo in unit.get("code_health") or ():
            if isinstance(repo, str) and repo.strip() and repo in own_repos:
                out.append((repo, slug))
    return out


def unit_rollup_keys(kind: str) -> list[str]:
    """Top-level unit slugs whose own `rollup` field lists `kind` ("repos" or
    "cost"), in registry order. Raises `RegistryUnavailable` like `units()`."""
    return [slug for slug, unit in units().items() if kind in (unit.get("rollup") or ())]


def product_rollup_keys(kind: str) -> list[str]:
    """`_products` keys whose own `rollup` field lists `kind`, in map order.
    Raises `RegistryUnavailable` like `units()`."""
    return [slug for slug, info in products().items() if kind in (info.get("rollup") or ())]


def scopes() -> dict[str, dict]:
    """The registry's `_scopes` map, its own `_doc` key excluded: the
    NON-project scopes (`company`, `platform`, `commercial`,
    `engineering-practice`, `keel`) nothing is built at, the way
    agent-platform's `lib/agent-platform/projects.py` `scopes()` reads the
    same map. Raises `RegistryUnavailable` like `units()`."""
    raw = _read_registry().get("_scopes", {})
    return {slug: scope for slug, scope in raw.items() if not slug.startswith("_")}


def display_names() -> dict[str, str]:
    """Every slug's `display`, from the top-level project entries and from
    `_products` — the registry's one canonical spelling for a project or
    product name, in place of a hand-typed label. Raises `RegistryUnavailable`
    like `units()`."""
    names = {slug: unit["display"] for slug, unit in units().items()
             if isinstance(unit.get("display"), str) and unit["display"].strip()}
    names.update({slug: info["display"] for slug, info in products().items()
                  if isinstance(info.get("display"), str) and info["display"].strip()})
    return names


def telemetry_aliases() -> dict[str, str]:
    """Every raw telemetry string folded to its project's own `display`.

    A project is in this map when it declares its own `aliases` (a renamed
    slug, or an umbrella name, still showing up in old records) or when it
    shares a `_products` entry with at least one sibling project (several
    repos building one product, each reporting under its own name). A
    project with neither stays out of the map, so its telemetry value passes
    through unfolded. Raises `RegistryUnavailable` like `units()`."""
    unit_map = units()
    product_counts: dict[str, int] = {}
    for unit in unit_map.values():
        for p in unit.get("products") or ():
            product_counts[p] = product_counts.get(p, 0) + 1
    out: dict[str, str] = {}
    for slug, unit in unit_map.items():
        display = unit.get("display")
        if not isinstance(display, str) or not display.strip():
            continue
        aliases = [a for a in (unit.get("aliases") or ()) if isinstance(a, str) and a.strip()]
        shares_product = any(product_counts.get(p, 0) > 1 for p in (unit.get("products") or ()))
        if not aliases and not shares_product:
            continue
        out[slug] = display
        for alias in aliases:
            out[alias] = display
    return out


_ROOT_PREFIX = "<root>/"


def resolve_home_dir(raw: object) -> Path | None:
    """A unit's stored `home_dir` resolved to an actual path, or `None` when
    `raw` isn't a path this can place.

    Mirrors agent-platform's own `registry_home`, the slice this repo's
    builders need: the stored `<root>/<rest>` form resolves against
    `_builder_root()`; an
    already-absolute `raw` (a test fixture's own tmp path, say) is returned
    as given. No legacy `~/Convivy/` prefix and no privacy-wall root
    override — this repo reads one install's registry per process and never
    makes the black-box match agent-platform's own reader does. Returns
    `None` rather than raising on anything else, since a roster row
    defaults around an unplaceable home sooner than it 404s."""
    if not isinstance(raw, str) or not raw:
        return None
    if raw.startswith(_ROOT_PREFIX):
        return _builder_root() / raw[len(_ROOT_PREFIX):]
    if Path(raw).is_absolute():
        return Path(raw)
    return None


def repo_github_owner(repo: str) -> str:
    """The GitHub owner `repo` lives under — the resolution
    agent-platform's own `projects.repo_github_slug` uses, read here instead
    of composed from `_products`' business-ownership `owner` field, which the
    registry's own `_meta.fields.owner` doc says is NOT a GitHub account (a
    past review found three repos under a personal GitHub account 404ing from
    exactly that confusion).

    The repo's own unit (the one whose `repos` lists it) answers first with
    its `repo_owners[repo]` override, then its unit-wide `github_owner`;
    `DEFAULT_GITHUB_ORG` answers for a repo no unit owns, or when the
    registry can't be read — the same "convivy" every call site hardcoded
    before this function existed, so an unreadable registry degrades to
    that prior behaviour rather than to an empty owner."""
    try:
        unit_map = units()
    except RegistryUnavailable:
        return DEFAULT_GITHUB_ORG
    for unit in unit_map.values():
        if repo in (unit.get("repos") or ()):
            return ((unit.get("repo_owners") or {}).get(repo)
                    or unit.get("github_owner")
                    or DEFAULT_GITHUB_ORG)
    return DEFAULT_GITHUB_ORG


def activation_lanes_for_repo(repo: str) -> list[str]:
    """The activation-profile lanes of the unit whose own `repos` lists
    `repo` — the same reverse lookup `repo_github_owner` performs, since a
    row's repo is not always named after the unit that owns it (CQ-171
    slice 6: a project's `agent-platform` repo is owned by the `bosun`
    unit, and the shape repeats for other repo/unit pairs). `[]` when no
    unit claims `repo`, that unit's own `activation_lanes` isn't a non-empty
    list, or the registry can't be read, so a project with no declared
    lanes renders with none rather than a guess."""
    try:
        unit_map = units()
    except RegistryUnavailable:
        return []
    for unit in unit_map.values():
        if repo in (unit.get("repos") or ()):
            lanes = unit.get("activation_lanes")
            if isinstance(lanes, list):
                return [lane for lane in lanes if isinstance(lane, str) and lane.strip()]
            return []
    return []


def company_workspace() -> str | None:
    """The primary repo (the first `repos` entry) of the one unit whose own
    `company_workspace` field is `True` — the single company-level
    commercial workspace, as opposed to any per-product
    `<product>-commercial` workspace, which carries no such field (CQ-171
    slice 6). Repo, not registry key, to match every other roster/repo
    identity this module and its callers compare against
    (`activation_lanes_for_repo`), since a unit's own key is not always its
    repo's name. `None` when no unit is marked this way, when more than one
    is (ambiguous — a caller should apply no alias rather than guess), when
    the one marked unit owns no repo, or when the registry can't be read."""
    try:
        unit_map = units()
    except RegistryUnavailable:
        return None
    matches = [unit for unit in unit_map.values() if unit.get("company_workspace") is True]
    if len(matches) != 1:
        return None
    repos = matches[0].get("repos") or ()
    return repos[0] if repos else None


def repo_display_names() -> dict[str, str]:
    """Every repo name to the display label its owning unit's own
    `repo_display` map assigns it directly — the same shape as
    `repo_owners`, for a repo whose own label differs from its unit's
    `display` (the project or crew as a whole, not one specific repo under
    it). A `repo_display` key counts only when it is ALSO one of that same
    unit's own `repos`; any other key is ignored, so a stale or mistyped
    entry can't hand a label to a repo nobody claims (CQ-171 slice 6).
    Raises `RegistryUnavailable` like `units()`."""
    out: dict[str, str] = {}
    for unit in units().values():
        own_repos = set(unit.get("repos") or ())
        for repo, display in (unit.get("repo_display") or {}).items():
            if (repo in own_repos and isinstance(display, str) and display.strip()):
                out[repo] = display
    return out


def sandbox_repos() -> frozenset[str]:
    """The union of every unit's own `sandbox_repos` marker list — repos
    that are pure sandboxes or mirrors with no engineering signal worth
    indexing, whether or not the same repo also appears in that unit's
    `repos` (CQ-171 slice 6: a sandbox repo may be a declared member of its
    owning unit's `repos`, or may be a bare marker naming a repo no unit
    otherwise claims). Raises `RegistryUnavailable` like `units()`."""
    return frozenset(
        repo
        for unit in units().values()
        for repo in unit.get("sandbox_repos") or ()
        if isinstance(repo, str) and repo.strip())


def public_crews() -> list[str]:
    """Sorted, distinct crew keys of every active, non-private registry unit."""
    crews: set[str] = set()
    for unit in units().values():
        if unit.get("private") is True or unit.get("lifecycle") != "active":
            continue
        crew = unit.get("crew")
        if isinstance(crew, str) and crew.strip():
            crews.add(crew.strip())
    return sorted(crews)


def covers_every_project(scope: frozenset[str]) -> bool:
    """Whether `scope` holds every project in the registry, `_`-prefixed keys
    (`_meta`, `_products`, `_scopes`) excluded and private units included.

    This gates the fleet-wide figures that carry no project of their own
    (E19·3, issue_01M35K5EAZD85FBYXEBQVRKVZV): a viewer sees them only when
    their Library scope already covers everything those figures sum over.
    Scope decides it, never a role (E19). An unreadable registry answers
    `False`, so while the registry cannot be read those figures show to
    nobody."""
    try:
        projects = set(units())
    except RegistryUnavailable:
        return False
    return bool(projects) and projects <= scope


def repos_in_scope(scope: frozenset[str]) -> frozenset[str]:
    """Every repo the registry lists under a project in `scope`.

    A repo's name is not its project's slug (E19·3,
    issue_01M35K5EAZD85FBYXEBQVRKVZV), so a per-repo row is filtered against
    this set rather than against `scope` itself. An unreadable registry
    answers the empty set, so no repo row reaches anyone."""
    try:
        unit_map = units()
    except RegistryUnavailable:
        return frozenset()
    return frozenset(
        repo
        for slug, unit in unit_map.items()
        if slug in scope
        for repo in unit.get("repos") or ())


def projects_for_crew(crew: str) -> frozenset[str]:
    """Every project slug whose `crew` field is `crew` — the reverse of
    `crews_in_scope`. A crew key is not its project's slug (`convivy-deck`'s
    crew is `deck`), so a check of a crew against a viewer's project scope
    has to go through this. Raises `RegistryUnavailable` like `units()`, so
    the caller decides what an unreadable registry means for it."""
    return frozenset(
        slug for slug, unit in units().items()
        if isinstance(unit.get("crew"), str) and unit["crew"].strip() == crew)


def crews_in_scope(scope: frozenset[str]) -> frozenset[str]:
    """Every crew key the registry lists under a project in `scope` — the
    same `crew` field `public_crews()` collects, restricted the way
    `repos_in_scope` restricts repos (E19·4, issue_01M35K5JQ7Z7MHYZGWRSB5F85M).
    A crew key is not its project's slug either, so this is filtered the same
    way: by the project slugs in `scope`, not by the crew keys themselves. A
    private unit's crew is included, matching `repos_in_scope`'s own choice
    not to special-case `private` — unlike `public_crews()`, which is a
    suggestion list over every NON-private unit regardless of viewer. An
    unreadable registry answers the empty set, so no crew name reaches
    anyone."""
    try:
        unit_map = units()
    except RegistryUnavailable:
        return frozenset()
    return frozenset(
        unit["crew"].strip()
        for slug, unit in unit_map.items()
        if slug in scope
        and isinstance(unit.get("crew"), str) and unit["crew"].strip())
