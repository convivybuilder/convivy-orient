"""Make the convivy-orient modules (server, build, dashboard_build) importable
from the tests without a package install — they're top-level scripts in the
repo root. Stdlib-only project, so no venv assumptions."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# --- the `needs_kb` marker (substrate ARCH-436, PR G) ------------------------
#
# Mirrors agent-platform's tests/conftest.py, which carries the full reasoning.
# The short version: several guards here assert that a constant in this repo
# agrees with the knowledge repo, CI checks out convivy-orient alone, so each of
# them called `pytest.skip` there. Main then reads green because the check did not
# run, while the same assertion fails on any machine that has the KB — which is
# how PR E2's drift survived until PR F found three more stale copies by hand.
#
# Checking out a second private repo in CI needs a standing credential with read
# access to the whole KB, for a handful of tests. `bin/check-kb-guards` in
# agent-platform runs `pytest -m needs_kb` across the sibling repos daily on the
# machine that has the checkouts, and files an issue on breach — the same answer
# `bin/check-cli-contracts` already takes to the same structural constraint.
#
# The marker also makes the set ENUMERABLE, so a guard added later joins the daily
# check by carrying the marker rather than by being registered somewhere else.
def knowledge_repo() -> Path:
    """The knowledge checkout these guards read, honouring KNOWLEDGE_REPO."""
    return Path(os.environ.get("KNOWLEDGE_REPO", Path.home() / "Convivy" / "knowledge"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "needs_kb: asserts agreement with the knowledge repo or its index; "
        "skipped where no checkout exists (CI). Run daily by check-kb-guards.")
    config.addinivalue_line(
        "markers",
        "real_access_log: asserts on server.ACCESS_LOG's own value, so the "
        "autouse fixture that repoints it at a temp file stands aside.")


def pytest_runtest_setup(item):
    if item.get_closest_marker("needs_kb") and not (knowledge_repo() / "projects").is_dir():
        pytest.skip(
            f"no knowledge checkout at {knowledge_repo()}; this guard runs where "
            f"the KB is (agent-platform's bin/check-kb-guards), not in CI")


# A trimmed mirror of the install's real project registry (fields this repo's
# builders actually read: display, repos, aliases, crew, home_dir,
# agent_prefix, lifecycle, products, overnight, kb_project, plus the `_scopes`
# map CQ-171 ruling 4 added a reader for), captured 2026-10-22. Backs
# `_pinned_project_registry` below, so a crew_registry-reading test gets the
# same answer on a laptop that has ~/Convivy/agent-platform checked out and on
# a CI runner that has only this repo — see that fixture's docstring. A test
# asserting the registry-derived tables agree with what main's literals held
# reads this dict, not the live file, to pin against.
_PINNED_REGISTRY = {
    "alpha": {"display": "Alpha", "crew": "alpha", "home_dir": "<root>/alpha",
              "lifecycle": "active", "aliases": [], "overnight": True,
              "products": ["alpha"], "code_health": ["alpha"],
              "repos": ["alpha", "alpha-ownerrez-sandbox-tools", "alpha-commercial", "alpha-landing"]},
    "beta": {"display": "Beta", "crew": "beta", "home_dir": "<root>/beta",
                  "lifecycle": "active", "aliases": [], "overnight": True,
                  "products": ["beta"], "code_health": ["beta"],
                  "repos": ["beta", "beta-landing", "beta-commercial", "beta-updates"],
                  "repo_display": {"beta-landing": "Beta Landing"}},
    "epsilon": {"display": "Epsilon", "crew": "epsilon", "home_dir": "<root>/epsilon",
                  "lifecycle": "active", "aliases": [], "overnight": True,
                  "products": ["epsilon"], "code_health": ["epsilon"], "repos": ["epsilon"]},
    "gamma": {"display": "Gamma", "crew": "gamma", "home_dir": "<root>/gamma",
               "lifecycle": "active", "aliases": ["mu-assistant"],
               "overnight": False, "products": ["gamma"], "code_health": ["gamma"],
               "repos": ["gamma"], "rollup": ["cost"]},
    "delta": {"display": "Delta", "crew": "delta", "home_dir": "<root>/delta",
                 "lifecycle": "active", "aliases": [], "overnight": False,
                 "products": ["delta"], "code_health": ["delta"],
                 "repos": ["delta", "delta-backend"],
                 "rollup": ["repos", "cost"]},
    "bosun": {"display": "Bosun", "crew": "bosun", "home_dir": "<root>/agent-platform",
              "lifecycle": "active", "aliases": ["keel"], "overnight": False,
              "products": [], "code_health": ["agent-platform"],
              "repos": ["agent-platform"], "rollup": ["repos"],
              "kb_project": "agent-platform"},
    "convivy-deck": {"display": "Deck", "crew": "deck", "home_dir": "<root>/convivy-deck",
                     "lifecycle": "active", "aliases": [], "overnight": False,
                     "products": [], "repos": ["convivy-deck"],
                     "kb_project": "deck"},
    "library": {"display": "Library", "crew": "library", "home_dir": "<root>/library",
                "lifecycle": "active", "aliases": [], "overnight": False,
                "products": [], "code_health": ["knowledge"],
                "repos": ["library", "knowledge"]},
    "steward": {"display": "Steward", "crew": "steward", "home_dir": "<root>/substrate",
                "lifecycle": "active", "aliases": [], "overnight": False,
                "products": [], "repos": ["substrate"], "kb_project": "platform"},
    "convivy-orient": {"display": "Orient", "crew": "convivy-orient", "home_dir": "<root>/convivy-orient",
                       "lifecycle": "active", "aliases": [], "overnight": False,
                       "products": [], "code_health": ["convivy-orient"],
                       "repos": ["convivy-orient", "www"]},
    "lambda-kit": {"display": "Lambda Kit", "crew": "lambda-kit", "home_dir": "<root>/convivy-app-kit",
            "lifecycle": "active", "aliases": [], "overnight": False,
            "products": [], "code_health": ["convivy-app-kit"],
            "repos": ["convivy-app-kit", "convivy-services"]},
    "write-better": {"display": "Write Better", "crew": "write-better", "home_dir": "<root>/write-better",
                     "lifecycle": "active", "aliases": [], "overnight": False,
                     "products": [], "repos": ["write-better"]},
    "signals": {"display": "Signals", "crew": "signals", "home_dir": "<root>/signals",
                "lifecycle": "active", "aliases": [], "overnight": False,
                "products": [], "repos": ["signals"]},
    "eta": {"display": "Eta", "crew": "eta", "home_dir": "<root>/eta",
              "agent_prefix": "eta-", "lifecycle": "active", "aliases": [],
              "products": ["eta"], "repos": ["eta"]},
    "shipwright": {"display": "Shipwright", "crew": "shipwright", "home_dir": "<root>/shipwright",
                   "lifecycle": "active", "aliases": [], "overnight": False,
                   "products": [], "repos": ["shipwright", "homebrew-tap"],
                   "kb_project": "company"},
    "theta": {"display": "Theta", "crew": "theta", "home_dir": "<root>/theta",
                "lifecycle": "archived", "aliases": [], "overnight": False,
                "products": [], "repos": ["theta"]},
    "omega-tools": {"display": "Omega Tools", "crew": "omega-tools",
                       "home_dir": "<root>/omega/omega-tools", "lifecycle": "active",
                       "aliases": ["omega-architecture"], "overnight": False,
                       "products": ["zeta-studio"], "repos": ["omega-tools"]},
    "omega-etc": {"display": "Omega Circle", "crew": "omega-etc",
                "home_dir": "<root>/omega/omega-etc", "lifecycle": "active",
                "aliases": [], "overnight": False,
                "products": ["zeta-studio"], "repos": ["omega-etc"]},
    "omega-villa": {"display": "Omega Villa", "crew": "omega-villa",
                       "home_dir": "<root>/omega/omega-villa", "lifecycle": "active",
                       "aliases": [], "overnight": False,
                       "products": ["zeta-studio"], "repos": ["omega-villa"]},
    "example-commercial": {"display": "Example General", "crew": "example-commercial",
                           "home_dir": "<root>/example-commercial", "lifecycle": "active",
                           "aliases": [], "overnight": False,
                           "products": [], "repos": ["example-commercial"]},
    # The registry's own `_products` key order is really beta, alpha,
    # epsilon, gamma, delta, zeta-studio, eta (captured 2026-10-22,
    # same order `tests/test_build.py`'s PRODUCT_ORDER tests build their own
    # fixture around). A map-order derivation of PRODUCT_GROUPS over THAT
    # order would put epsilon before gamma/delta in the "Mobile App
    # Suite" group, not today's shipped member order — reported in this
    # slice's PR body. gamma/delta are reordered ahead of epsilon
    # here only (CQ-171 slice 5, the steward's registry-fields ruling), so
    # this mirror's derived PRODUCT_GROUPS still matches what main shipped
    # as a literal.
    "_products": {
        "beta": {"display": "Beta", "owner": "convivy"},
        "alpha": {"display": "Alpha", "owner": "convivy"},
        "gamma": {"display": "Gamma", "owner": "convivy", "group": "Mobile App Suite"},
        "delta": {"display": "Delta", "owner": "convivy", "group": "Mobile App Suite"},
        "epsilon": {"display": "Epsilon", "owner": "convivy", "group": "Mobile App Suite"},
        "zeta-studio": {"display": "Zeta Studio", "owner": "omega", "rollup": ["cost"]},
        "eta": {"display": "Eta", "owner": "convivy"},
    },
    # `_kb_projects` (CQ-171 revise round 2, the steward's registry-fields
    # ruling) is a real addition this mirror carries ahead of the live file,
    # same as the `rollup` fields above: both are needed by every test in this
    # suite that reads `crew_registry` through the default fixture rather than
    # building its own, not just the dedicated PRODUCT_ORDER/STANDALONE_PRODUCTS
    # tests, which still build their own fixture for the `_products` rearrange.
    # The seven `"section": "engineering"` entries (CQ-171 slice 5) are the
    # same addition for ENGINEERING_PROJECTS, in the order main shipped that
    # list in — `_kb_projects` doesn't exist on the live file at all yet, so
    # there is no captured live order to reorder against, unlike the `group`
    # reorder above.
    "_kb_projects": {
        "platform": {"section": "engineering"},
        "engineering-practice": {"section": "engineering"},
        "knowledge": {"section": "engineering"},
        "convivy-orient": {"section": "engineering"},
        "keel": {"section": "engineering"},
        "library": {"section": "engineering"},
        "write-better": {"section": "engineering"},
        "zeta-studio": {"standalone": True},
        "eta": {"standalone": True},
        "agent-platform": {"standalone": True},
        "convivy-app-kit": {"standalone": True},
        "convivy-services": {"standalone": True},
        "deck": {"standalone": True},
        "theta": {"standalone": True},
        "substrate": {"standalone": True},
        # A fictional key, not the real repo's name (CQ-171 slice 6 closed the
        # gap that used to require the real name here: `beta`'s own
        # `repo_display` below now supplies this entry's PROJECT_LABEL value,
        # so nothing here depends on this spelling matching a real repo).
        "beta-landing": {"standalone": True},
        "signals": {"standalone": True},
        "shipwright": {"standalone": True},
    },
    "_scopes": {
        "company": {"display": "Convivy"},
        "platform": {"display": "Platform"},
        "commercial": {"display": "Commercial"},
        "engineering-practice": {"display": "Engineering Practice"},
        "keel": {"display": "Keel"},
    },
}


@pytest.fixture(autouse=True)
def _pinned_project_registry(monkeypatch, tmp_path_factory):
    """Point `$PROJECTS_JSON` at `_PINNED_REGISTRY` above by default.

    Autouse and unconditional, for the reason `_no_real_library_credentials`
    below is: a crew_registry-reading builder (chart_house_build,
    dashboard_build) otherwise takes two code paths depending on whether the
    machine running the suite has `~/Convivy/agent-platform` checked out —
    and CI, which checks out convivy-orient alone, does not. `_PINNED_REGISTRY`
    is a trimmed mirror of the real install's registry, not an empty one, so
    a test asserting a registry-derived table still matches today's shipped
    behaviour needs no monkeypatching of its own to get it.

    `monkeypatch.setenv("PROJECTS_JSON", ...)` inside a test — this repo's own
    convention, see tests/test_crew_registry.py — simply wins over this one,
    for a test that wants a made-up registry or an unreadable one.
    """
    path = tmp_path_factory.mktemp("pinned-registry") / "projects.json"
    path.write_text(json.dumps(_PINNED_REGISTRY))
    monkeypatch.setenv("PROJECTS_JSON", str(path))
    yield


@pytest.fixture(autouse=True)
def _no_real_library_credentials(monkeypatch, tmp_path_factory):
    """No test may read a real Library credential off the machine running it.

    Autouse and unconditional, for the reason `_pinned_project_registry`
    above is: the alternative is a suite that takes two code paths
    depending on which machine ran it. This one is not hypothetical — PR #172's
    first CI run failed on exactly it. `library_client._human_reserved_token`
    falls back to `~/.orient-control/library-client-token`, so a test that
    exercised the ack read passed on the laptop that had just minted that
    credential and failed on a runner that had not.

    Both paths point into an empty temp directory, so a test wanting a
    credential sets the env var or repoints the path itself, and a test that
    forgot gets a clear FileNotFoundError naming a path under /tmp rather than
    silently borrowing the operator's real token.
    """
    import library_client

    monkeypatch.delenv("LIBRARY_CLIENT_TOKEN", raising=False)
    monkeypatch.delenv("LIBRARY_HUMAN_RESERVED_TOKEN", raising=False)
    absent = tmp_path_factory.mktemp("no-credentials")
    monkeypatch.setattr(library_client, "TOKEN_FILE", absent / "library-token")
    monkeypatch.setattr(library_client, "CLIENT_TOKEN_FILE",
                        absent / "library-client-token")
    yield


@pytest.fixture(autouse=True)
def _access_log_goes_to_a_temp_file(request, monkeypatch, tmp_path_factory):
    """No test may append to the operator's real access.log.

    Autouse for the same reason `_no_real_library_credentials` above is: the
    alternative depends on which machine ran the suite. `ACCESS_LOG` resolves to
    `<repo>/access.log` whenever ORIENT_STATE_ROOT is unset, which is every
    developer checkout, so a test hitting any route would write a request line
    into the live audit surface — 10k lines of real visitor emails that an
    operator reads to answer "did this request reach the origin". Test traffic
    mixed into it is not merely untidy: it is indistinguishable from the traffic
    the file exists to record.

    This became reachable when the middleware backstop started logging router
    routes (issue_01M1Z882KE85C88GBSB2CS1XJ5) — before it, an API test wrote
    nothing. A test asserting on log CONTENT still points ACCESS_LOG at its own
    tmp_path; that monkeypatch simply wins over this one.

    A test carrying `@pytest.mark.real_access_log` is exempt, because it asserts
    on the constant's own value rather than writing through it — repointing the
    attribute is exactly what such a test must not have done to it.
    """
    import server as srv

    if request.node.get_closest_marker("real_access_log"):
        yield
        return
    monkeypatch.setattr(
        srv, "ACCESS_LOG", tmp_path_factory.mktemp("access-log") / "access.log")
    yield


@pytest.fixture(scope="session")
def _shared_control_db(tmp_path_factory):
    """One control.db, migrated to head once per test session — the fallback
    a test lands on when it does not bring its own `db`-style fixture
    (test_control_db.py's pattern).

    Added for issue_01M17W6XGXVHNDWCFRTQ38Y2RK: a handful of previously-pure
    builders (`code_health_build`/`kb_build`'s `live_html()`, `alerts_build.
    gather()`) now open a control_db session mid-render to read a settable
    alert threshold, and several existing tests call them directly
    (test_home_overview_build.py, test_alerts_build.py) with no DB fixture in
    sight. Migrating once per session rather than per test is what keeps this
    cheap — `control_db.init_db()` runs the full migration chain and costs
    ~80ms, which is fine once and is not fine before each of ~2000 tests.
    """
    import control_db

    path = tmp_path_factory.mktemp("shared-control-db") / "control.db"
    prior = (control_db.CONTROL_DB, control_db._engine, control_db._engine_url)
    control_db.CONTROL_DB = path
    control_db._engine = None
    control_db._engine_url = None
    control_db.init_db()
    control_db.CONTROL_DB, control_db._engine, control_db._engine_url = prior
    return path


@pytest.fixture(autouse=True)
def _no_real_control_db(_shared_control_db, monkeypatch):
    """No test may read or write the operator's real ~/.orient-control/control.db,
    mirroring `_no_real_library_credentials` above for a different subsystem
    and the same reason: the alternative depends on which machine ran the
    suite, and here that machine may be the one actually serving Orient.

    Points every test at `_shared_control_db` — already migrated to head, so
    a merely-incidental read (an alert threshold's shipped default) works with
    no per-test migration cost. A test that WRITES through control_db still
    brings its own isolated `db` fixture, an explicitly-requested fixture that
    runs after this autouse one and simply repoints CONTROL_DB again — same
    precedence `test_control_db.py`'s `db` fixture already relies on.
    """
    import control_db

    monkeypatch.setattr(control_db, "CONTROL_DB", _shared_control_db)
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    yield
