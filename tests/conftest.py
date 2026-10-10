"""Make the convivy-orient modules (server, build, dashboard_build) importable
from the tests without a package install — they're top-level scripts in the
repo root. Stdlib-only project, so no venv assumptions."""
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


@pytest.fixture(autouse=True)
def _no_real_library_credentials(monkeypatch, tmp_path_factory):
    """No test may read a real Library credential off the machine running it.

    Autouse and unconditional, for the reason the `_pinned_project_registry`
    fixture above is: the alternative is a suite that takes two code paths
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
    (test_blog_store.py's pattern).

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
    precedence `test_blog_store.py`'s `db` fixture already relies on.
    """
    import control_db

    monkeypatch.setattr(control_db, "CONTROL_DB", _shared_control_db)
    monkeypatch.setattr(control_db, "_engine", None)
    monkeypatch.setattr(control_db, "_engine_url", None)
    yield
