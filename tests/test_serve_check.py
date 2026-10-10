"""serve.sh --check — the pre-swap gate bin/install-orient runs against a
staged build before swapping it in (ADR-0059 rule 4: "there is no serve.sh
--check to gate on" was the gap this fills).

The failure-path cases below copy only serve.sh into an empty tmp directory:
those checks (argument parsing, interpreter presence, config/state root
validation) run in the shell before serve.sh ever invokes python, so they
need no venv and no application code. The success-path cases run the real
serve.sh from this checkout under the interpreter already running this test
suite, which already carries every dependency serve.sh's preflight imports
(fastapi, uvicorn, ...) since test_server.py and friends import the same
modules — the same pattern test_paths.py already uses for its own subprocess
checks against this repo.

--check calls `build.load_docs()`, which since
issue_01M1MFJRR46TMQ6DSJSD7DGX7C reaches the Library over HTTP rather than
opening `~/.knowledge-index/index.db` (PR #236 round 2's original reason for
the call). These are separate OS processes, not in-process test code, so the
`library_fake` monkeypatch every other Library-backed test in this suite uses
cannot reach them — `_stub_library_server` below starts a real, minimal
HTTP server on loopback and points `LIBRARY_BASE_URL` at it, the equivalent
of the old fake-HOME-with-a-stub-index for this call. The same fake HOME
gives every success-path case a `~/.orient-control/control.db` that doesn't
exist yet, which is also the state --check's alembic revision-chain
comparison must treat as "nothing to upgrade from" rather than fail on (PR
#236 round 2, second finding).
"""
from __future__ import annotations

import contextlib
import http.server
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVE_SH = REPO_ROOT / "serve.sh"


def _env(**overrides):
    env = dict(os.environ)
    env.update(overrides)
    return env


def _run(args, cwd, env):
    return subprocess.run(
        ["sh", str(cwd / "serve.sh"), *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=False,
    )


def _bare_dir(tmp_path):
    """A directory holding nothing but serve.sh."""
    d = tmp_path / "bare"
    d.mkdir()
    shutil.copy(SERVE_SH, d / "serve.sh")
    (d / "serve.sh").chmod(0o755)
    return d


def _stub_venv(d):
    """A `.venv/bin/python` that exists and is executable but does nothing —
    enough to clear the interpreter-presence check without a real Python."""
    bin_dir = d / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "python"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return stub


class _LibraryStubHandler(http.server.BaseHTTPRequestHandler):
    """Answers exactly what `build.load_docs()` needs: a narrow `/whoami`
    (so `library_client`'s credential-verification gate passes) and whatever
    `documents` the server was seeded with, in the shape `GET
    /reference/documents` serves (metadata rows, no bodies, one page)."""

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # silence per-request access logging in test output

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/whoami":
            self._reply({"capabilities": []})
        elif path == "/reference/documents":
            self._reply({"documents": self.server.documents, "truncated": False})
        else:
            self.send_response(404)
            self.end_headers()

    def _reply(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def _stub_library_server(documents=()):
    """A real loopback HTTP server standing in for the Library, for the
    subprocess `--check` tests below — see the module docstring for why an
    in-process `library_fake` monkeypatch can't reach a child process.
    Yields the env overrides a caller passes straight into `_env(...)`."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LibraryStubHandler)
    server.documents = list(documents)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "LIBRARY_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "LIBRARY_CLIENT_TOKEN": "test-token",
        }
    finally:
        server.shutdown()
        server.server_close()


# --- argument parsing ---------------------------------------------------------


def test_check_rejects_too_many_arguments(tmp_path):
    result = _run(["--check", "extra"], _bare_dir(tmp_path), _env())
    assert result.returncode == 64
    assert "too many arguments" in result.stderr


def test_check_rejects_an_unknown_flag(tmp_path):
    result = _run(["--bogus"], _bare_dir(tmp_path), _env())
    assert result.returncode == 64


# --- rule 4: no fallback to system python -------------------------------------


def test_check_fails_loud_with_no_interpreter(tmp_path):
    result = _run(["--check"], _bare_dir(tmp_path), _env(PY="/nonexistent/python"))
    assert result.returncode == 78
    assert "no interpreter" in result.stderr


# --- rule 3: config/state roots must be absolute, directories, readable -------


def test_check_rejects_a_relative_config_root(tmp_path):
    bare = _bare_dir(tmp_path)
    _stub_venv(bare)
    result = _run(["--check"], bare, _env(ORIENT_CONFIG_ROOT="relative/path"))
    assert result.returncode == 78
    assert "must be an absolute path" in result.stderr


def test_check_reports_a_missing_root_without_creating_it(tmp_path):
    """--check is the pre-swap gate, so it stays read-only: a missing root is
    reported as one a start will create, and nothing is made."""
    bare = _bare_dir(tmp_path)
    _stub_venv(bare)
    missing = tmp_path / "state" / "orient"
    result = _run(["--check"], bare, _env(ORIENT_STATE_ROOT=str(missing)))
    assert "a start will create it empty" in result.stderr
    assert not missing.exists()


def test_check_rejects_a_root_that_is_not_a_directory(tmp_path):
    bare = _bare_dir(tmp_path)
    _stub_venv(bare)
    a_file = tmp_path / "not-a-dir"
    a_file.write_text("")
    result = _run(["--check"], bare, _env(ORIENT_CONFIG_ROOT=str(a_file)))
    assert result.returncode == 78
    assert "is not a directory" in result.stderr


def test_check_rejects_a_dangling_symlink_root(tmp_path):
    """A symlink to nowhere is not a missing root to create: mkdir would write
    through it, so it refuses as not a directory."""
    bare = _bare_dir(tmp_path)
    _stub_venv(bare)
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "nowhere")
    result = _run([], bare, _env(ORIENT_CONFIG_ROOT=str(link)))
    assert result.returncode == 78
    assert "is not a directory" in result.stderr
    assert not (tmp_path / "nowhere").exists()


def test_a_full_run_creates_missing_roots_empty_and_0700(tmp_path):
    """issue_01M3J7T3CQ0R1A7G3AP2668WHS: on a clean machine nothing creates
    `{state}/orient`, and Shipwright's first Linux start refused here. A start
    creates both roots owner-only whatever the umask, and every parent it
    makes too, since a writable parent lets another user replace the root
    (PR #335 security review). It writes nothing into the config root. $PY
    is the same do-nothing stub the control-dir test below uses."""
    bare = _bare_dir(tmp_path)
    (bare / "DEPLOYED_COMMIT").write_text("commit: " + "a" * 40 + "\n")
    stub = tmp_path / "python-stub"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    config = tmp_path / "local" / "state" / "orient-config"
    state = tmp_path / "local" / "state" / "orient"

    old_umask = os.umask(0o022)
    try:
        result = _run([], bare, _env(PY=str(stub), HOME=str(fake_home),
                                     ORIENT_CONFIG_ROOT=str(config),
                                     ORIENT_STATE_ROOT=str(state)))
    finally:
        os.umask(old_umask)

    assert result.returncode == 0, result.stdout + result.stderr
    for d in (config, state, tmp_path / "local", tmp_path / "local" / "state"):
        assert d.is_dir(), d
        assert oct(d.stat().st_mode & 0o777) == oct(0o700), d
    assert list(config.iterdir()) == [], "a created config root holds nothing"
    assert "created ORIENT_CONFIG_ROOT" in result.stderr


def test_check_rejects_an_unreadable_config_root(tmp_path):
    bare = _bare_dir(tmp_path)
    _stub_venv(bare)
    unreadable = tmp_path / "no-read"
    unreadable.mkdir()
    unreadable.chmod(0o000)
    try:
        result = _run(["--check"], bare, _env(ORIENT_CONFIG_ROOT=str(unreadable)))
        assert result.returncode == 78
        assert "is not readable" in result.stderr
    finally:
        unreadable.chmod(0o755)  # tmp_path cleanup needs this back


# --- the real success path ----------------------------------------------------


def test_check_passes_on_the_real_checkout_with_the_test_interpreter(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with _stub_library_server() as lib_env:
        result = _run(
            ["--check"], REPO_ROOT,
            _env(PY=sys.executable, HOME=str(fake_home),
                 ORIENT_CONFIG_ROOT=str(REPO_ROOT), ORIENT_STATE_ROOT=str(REPO_ROOT),
                 **lib_env),
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "preflight ok" in result.stdout


def test_check_fails_when_the_library_is_unreachable(tmp_path):
    """The regression case for the pre-swap gate finding (PR #236 round 2),
    carried onto the Library call that replaced the sqlite read
    (issue_01M1MFJRR46TMQ6DSJSD7DGX7C): build.py's Library read only happens
    inside load_docs(), never at import time, so a Library that refuses the
    connection must fail --check loud rather than pass and take the next
    real start down. Port 1 is loopback with nothing bound there, so the
    connection is refused immediately rather than timing out."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = _run(
        ["--check"], REPO_ROOT,
        _env(PY=sys.executable, HOME=str(fake_home),
             ORIENT_CONFIG_ROOT=str(REPO_ROOT), ORIENT_STATE_ROOT=str(REPO_ROOT),
             LIBRARY_BASE_URL="http://127.0.0.1:1", LIBRARY_CLIENT_TOKEN="test-token"),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "build.load_docs()" in result.stderr
    assert "cannot reach the Library" in result.stderr


def test_a_full_run_creates_the_control_dir_0700_and_markers_0600(tmp_path):
    """issue_01M2MFGVAW1C8ZQ7G9CT0E1YXZ: ~/.orient-control and the two
    restart/serving markers serve.sh writes into it must come out owner-only
    regardless of the ambient umask. $PY here is a stub that ignores its
    arguments and stdin and exits 0 immediately, so the full (non-`--check`)
    script — including the final `exec "$PY" server.py` — runs to completion
    in well under a second with no real dependencies and no bound port; only
    the shell-level mkdir/chmod/umask/marker-write logic under test actually
    executes anything."""
    bare = _bare_dir(tmp_path)
    (bare / "DEPLOYED_COMMIT").write_text("commit: " + "a" * 40 + "\n")
    stub = tmp_path / "python-stub"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    old_umask = os.umask(0o022)
    try:
        result = _run(
            [], bare,
            _env(PY=str(stub), HOME=str(fake_home),
                 ORIENT_CONFIG_ROOT=str(bare), ORIENT_STATE_ROOT=str(bare)),
        )
    finally:
        os.umask(old_umask)

    assert result.returncode == 0, result.stdout + result.stderr
    ctrl = fake_home / ".orient-control"
    assert oct(ctrl.stat().st_mode & 0o777) == oct(0o700)
    for name in ("orient-restart-started", "orient-serving-commit"):
        f = ctrl / name
        assert f.exists(), f"{name} marker missing"
        assert oct(f.stat().st_mode & 0o777) == oct(0o600), (
            f"{name} is {oct(f.stat().st_mode & 0o777)}")


def test_check_exits_before_the_build_or_server_lines_run(tmp_path):
    """--check must return on the import/Library-reachability preflight
    alone, never reaching `cd "$DIR"` and the build.py/control_db.init_db/
    server.py lines below it — those write to STATE_ROOT and the
    ~/.orient-control serving-commit marker, and bind a port. HOME is
    redirected to an empty tmp directory for this run (never the real
    machine's ~/.orient-control) so the assertion — no marker file appeared —
    is meaningful rather than coincidentally true of whatever the real
    machine already had on disk."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with _stub_library_server() as lib_env:
        result = _run(
            ["--check"], REPO_ROOT,
            _env(PY=sys.executable, HOME=str(fake_home),
                 ORIENT_CONFIG_ROOT=str(REPO_ROOT), ORIENT_STATE_ROOT=str(REPO_ROOT),
                 **lib_env),
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (fake_home / ".orient-control").exists(), (
        "--check wrote the serving-commit marker — it must exit before that line")


# --- the checkout-vs-deploy-area PY discriminator (PR #236 round 2) ----------


def test_check_resolves_the_external_interpreter_from_a_bare_checkout(tmp_path):
    """No `.venv` and no `DEPLOYED_COMMIT` — the shape of the still-live
    checkout before the plist cutover for com.convivy.orient happens.
    Defaulting to `$DIR/.venv/bin/python` here is exactly what would crash
    the live site on its next automatic restart, before anyone runs the
    installer: serve.sh must fall back to the checkout's external
    interpreter instead."""
    bare = _bare_dir(tmp_path)
    stub_bin = tmp_path / "external" / "bin"
    stub_bin.mkdir(parents=True)
    stub = stub_bin / "python"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)

    result = _run(["--check"], bare, _env(ORIENT_CHECKOUT_PY=str(stub)))
    assert result.returncode != 78, result.stdout + result.stderr


def test_the_external_interpreter_defaults_under_home(tmp_path):
    """With no `ORIENT_CHECKOUT_PY`, a bare checkout's interpreter is
    `$HOME/.local/venv-orient/bin/python`, so serve.sh names no operator's home
    and installs on any machine. Both directions are asserted: the interpreter
    under the given HOME is found, and a HOME without one fails loud, so a
    fixed path that happened to exist on the test machine cannot pass."""
    bare = _bare_dir(tmp_path)
    env = _env()
    env.pop("ORIENT_CHECKOUT_PY", None)
    env.pop("PY", None)

    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    missing = _run(["--check"], bare, {**env, "HOME": str(empty_home)})
    assert missing.returncode == 78, missing.stdout + missing.stderr
    assert "no interpreter" in missing.stderr

    home = tmp_path / "home"
    stub_bin = home / ".local" / "venv-orient" / "bin"
    stub_bin.mkdir(parents=True)
    stub = stub_bin / "python"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    found = _run(["--check"], bare, {**env, "HOME": str(home)})
    assert found.returncode != 78, found.stdout + found.stderr


def test_check_fails_when_control_db_is_stamped_at_a_revision_this_build_does_not_know(tmp_path):
    """The regression case for the pre-swap gate's second gap (PR #236 round
    2): control_db.init_db() (serve.sh's control_db.init_db() line, run only
    on a real start) applies an alembic migration against the live
    control.db, and --check must catch a migration chain that can't resolve
    before bin/install-orient ever swaps the build in, not after. Stamping
    alembic_version at a revision absent from this build's migration chain
    (a hand-edit, a corrupted row, or a rollback to older code) reproduces
    that unresolvable chain without needing two different sets of migration
    files."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    control_dir = fake_home / ".orient-control"
    control_dir.mkdir(parents=True)
    conn = sqlite3.connect(control_dir / "control.db")
    conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    conn.execute("INSERT INTO alembic_version VALUES ('deadbeef0000')")
    conn.commit()
    conn.close()

    with _stub_library_server() as lib_env:
        result = _run(
            ["--check"], REPO_ROOT,
            _env(PY=sys.executable, HOME=str(fake_home),
                 ORIENT_CONFIG_ROOT=str(REPO_ROOT), ORIENT_STATE_ROOT=str(REPO_ROOT),
                 **lib_env),
        )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "migration chain" in result.stderr


def test_check_does_not_apply_the_control_db_migration(tmp_path):
    """The comparison must never actually run `alembic upgrade head` against
    the real control.db: PREFLIGHT runs against a STAGED, not-yet-swapped
    build, so applying its migration here would move the live database's
    schema ahead of whatever code the swap ends up serving if the install
    then fails or is rolled back. Asserting the file was never even created
    (rather than "unmodified") is the stronger claim: this fake HOME has no
    control.db to start with, and a passing --check must leave it that way,
    not lazily create an empty one just by connecting to inspect it."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with _stub_library_server() as lib_env:
        result = _run(
            ["--check"], REPO_ROOT,
            _env(PY=sys.executable, HOME=str(fake_home),
                 ORIENT_CONFIG_ROOT=str(REPO_ROOT), ORIENT_STATE_ROOT=str(REPO_ROOT),
                 **lib_env),
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (fake_home / ".orient-control" / "control.db").exists(), (
        "--check created or touched control.db — it must only read one that "
        "already exists, never create one just by inspecting it")


def test_every_fatal_step_after_check_only_is_named_in_the_header_comment(tmp_path):
    """Regression test for the recurring defect across PR #236 rounds 1 and
    2: a step between the CHECK_ONLY exit (serve.sh:191-193) and `exec
    server.py` that --check does not run, while the header comment above
    CHECK_ONLY still claims --check covers more than it does. Fully
    verifying that --check actually covers or correctly excludes each step
    is a judgment call, not something this test can automate — that's the
    call rounds 1 and 2 each got wrong once. What it CAN automate: every
    unconditional, fatal "$PY" invocation in that region must be named by
    its module in the header comment, so a third fatal step added later
    without touching the comment fails loud here instead of shipping
    unnoticed, the way control_db.init_db() did in round 1.

    The collector matches `"$PY"` ANYWHERE in the line, not at its start. It
    anchored at the start until 2026-09-17, which silently excluded the one
    step this test's docstring and its own assertion message both name: the
    control_db.init_db() line begins `ORIENT_INIT_DB_DIR="$DIR" "$PY" -c …`,
    so it was never collected and `name in header` never ran against it.
    Deleting "control_db" from the header comment left the test green. A
    start-anchored match is not a check of that line in either direction,
    which is why the count is asserted below rather than only non-emptiness —
    a collector that silently stops seeing a step should fail here rather
    than pass with less to look at."""
    text = SERVE_SH.read_text()
    header = text[text.index("# `--check` runs"):text.index("CHECK_ONLY=0")]

    # Anchor on the LINE that is `cd "$DIR"`, not on the string's first
    # occurrence anywhere in the file. A comment above the chain that mentions
    # `cd "$DIR"` used to move this anchor 200 lines early, sweeping the
    # interpreter guard and the PREFLIGHT heredoc into the region and failing
    # the count below — a comment cannot change which commands are fatal.
    lines = text.splitlines()
    cd_index = next(i for i, line in enumerate(lines)
                    if line.strip() == 'cd "$DIR"')
    exec_index = next(i for i, line in enumerate(lines)
                      if line.strip().startswith('exec "$PY"'))

    fatal_steps = [
        lines[i].strip() for i in range(cd_index, exec_index)
        if '"$PY"' in lines[i].strip()
        and not lines[i].strip().startswith("#")
        and "|| echo" not in lines[i]
    ]
    assert len(fatal_steps) == 2, (
        "expected exactly the build.py and control_db.init_db() steps, got: "
        f"{fatal_steps}")
    for line in fatal_steps:
        if "build.py" in line:
            name = "build.py"
        elif "control_db" in line:
            name = "control_db"
        else:
            raise AssertionError(
                f"unrecognized fatal step between --check's exit and exec "
                f"server.py — teach this test its name so it can be checked "
                f"against the header comment: {line}")
        assert name in header, (
            f"{name} is a fatal, unconditional step between --check's exit "
            f"and exec server.py, but the header comment above CHECK_ONLY "
            f"never names it — a reader can't tell whether that's covered "
            f"or deliberately excluded: {line}")


def test_control_db_migration_runs_before_every_builder():
    """control_db.init_db() applies the alembic migration; the builders below
    it read the tables those migrations create. A builder placed above that
    line runs against the schema the PREVIOUS deploy left behind, for the one
    restart that deploys a migration.

    This is not hypothetical. On the 2026-09-16 22:19 MST restart deploying
    PR #307, dashboard_build.py:1475 called alert_policy_store.get() and
    raised `no such table: alert_policies`; the migration creating that table
    ran 62 lines further down in the same script. Its `|| echo` swallowed the
    failure as non-fatal, so the cost dashboards served stale and nothing
    surfaced beyond one line in serve.log.

    The failure self-clears on the next restart, which is exactly why it went
    unnoticed from the day the write API shipped: by the time anyone looks at
    the running site, the migration has completed and the symptom is gone. So
    the guard has to be on the script's ordering rather than on any observable
    state of the deployment."""
    lines = SERVE_SH.read_text().splitlines()
    cd_index = next(i for i, line in enumerate(lines)
                    if line.strip() == 'cd "$DIR"')
    exec_index = next(i for i, line in enumerate(lines)
                      if line.strip().startswith('exec "$PY"'))

    init_db_index = None
    builder_indexes = {}
    for i in range(cd_index, exec_index):
        stripped = lines[i].strip()
        if stripped.startswith("#") or '"$PY"' not in stripped:
            continue
        if "control_db" in stripped:
            init_db_index = i
        else:
            match = re.search(r'\$DIR/(\w+\.py)', stripped)
            if match:
                builder_indexes[match.group(1)] = i

    assert init_db_index is not None, (
        "no control_db.init_db() step between `cd \"$DIR\"` and exec server.py "
        "— if the migration moved out of serve.sh entirely, this guard needs "
        "to move with it rather than be deleted")
    assert "dashboard_build.py" in builder_indexes, (
        "dashboard_build.py is the builder that hit this defect and it is no "
        f"longer in the chain; builders found: {sorted(builder_indexes)}")

    late = {name: i for name, i in builder_indexes.items() if i < init_db_index}
    assert not late, (
        "these builders run BEFORE control_db.init_db() applies the alembic "
        "migration, so on a restart that deploys one they read the previous "
        f"deploy's schema: {sorted(late)}. Move them below it, or move "
        "init_db above them.")


def test_check_prefers_the_in_place_venv_when_one_exists(tmp_path):
    """A deploy area always has both `.venv` and `DEPLOYED_COMMIT`, but the
    in-place `.venv` alone is enough to route there — a checkout that's had
    one hand-created (the documented stopgap) must use it too, not the
    external fallback."""
    bare = _bare_dir(tmp_path)
    _stub_venv(bare)
    stub_bin = tmp_path / "external" / "bin"
    stub_bin.mkdir(parents=True)
    external_stub = stub_bin / "python"
    external_stub.write_text("#!/bin/sh\nexit 0\n")
    external_stub.chmod(0o755)
    # Poison the external fallback so the test would fail loud if serve.sh
    # picked it instead of the in-place .venv.
    unreachable = tmp_path / "nonexistent" / "python"

    result = _run(["--check"], bare, _env(ORIENT_CHECKOUT_PY=str(unreachable)))
    assert result.returncode != 78, result.stdout + result.stderr


# --- the serving commit in a deploy area install-backend wrote ----------------
# issue_01M3JQDZJ6XQ7SGWYTMMHRKXAR: Builder's shipwright install-backend writes
# `.installed-by-install-backend` (JSON with a "commit" field) rather than
# DEPLOYED_COMMIT, into a directory that is not a git checkout, and serve.sh died
# on `fatal: not a git repository`.


def _run_full(tmp_path, bare):
    """A full start with a $PY stub that runs the real interpreter only for the
    marker's JSON read, and does nothing for every other call, including the
    final `exec "$PY" server.py` — the same shape the control-dir test uses."""
    stub = tmp_path / "python-stub"
    stub.write_text("#!/bin/sh\n"
                    'case "$2" in *json*) exec "' + sys.executable + '" "$@" ;; esac\n'
                    "exit 0\n")
    stub.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    result = _run([], bare, _env(PY=str(stub), HOME=str(fake_home),
                                 ORIENT_CONFIG_ROOT=str(bare), ORIENT_STATE_ROOT=str(bare),
                                 GIT_CEILING_DIRECTORIES=str(tmp_path)))
    return result, fake_home / ".orient-control"


def test_a_start_reads_the_commit_install_backend_recorded(tmp_path):
    bare = _bare_dir(tmp_path)
    sha = "b" * 40
    (bare / ".installed-by-install-backend").write_text(json.dumps(
        {"repository": "convivy-orient", "commit": sha, "ref": "main", "source": "/x"}))
    result, ctrl = _run_full(tmp_path, bare)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("orient-restart-started", "orient-serving-commit"):
        assert (ctrl / name).read_text().strip() == sha, name


def test_deployed_commit_still_wins_when_both_are_present(tmp_path):
    """bin/install-orient on the Mac writes DEPLOYED_COMMIT, and that stays the
    source when both markers exist."""
    bare = _bare_dir(tmp_path)
    (bare / "DEPLOYED_COMMIT").write_text("commit: " + "a" * 40 + "\n")
    (bare / ".installed-by-install-backend").write_text(json.dumps({"commit": "b" * 40}))
    result, ctrl = _run_full(tmp_path, bare)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (ctrl / "orient-serving-commit").read_text().strip() == "a" * 40


def test_a_marker_without_a_commit_refuses_the_start(tmp_path):
    """A start never records an empty commit: deploy-watch compares the serving
    marker against the checkout, and an empty one would read as never
    restarted."""
    bare = _bare_dir(tmp_path)
    (bare / ".installed-by-install-backend").write_text(json.dumps({"commit": "  "}))
    result, ctrl = _run_full(tmp_path, bare)
    assert result.returncode != 0
    assert not (ctrl / "orient-serving-commit").exists()


def test_no_marker_and_no_git_refuses_the_start(tmp_path):
    bare = _bare_dir(tmp_path)
    result, ctrl = _run_full(tmp_path, bare)
    assert result.returncode != 0
    assert "not a git repository" in result.stderr
    assert not (ctrl / "orient-serving-commit").exists()
