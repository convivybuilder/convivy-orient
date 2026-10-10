#!/bin/sh
# Persistent server for the convivy-orient site (launchd: com.convivy.orient).
#
# Rebuilds both tier bundles from the current KB index on each (re)start — so a
# `launchctl kickstart -k gui/$(id -u)/com.convivy.orient` refreshes content —
# then runs the identity-aware server on loopback:8099. The server picks the
# full vs contractor bundle from the Cloudflare Access email header. Loopback
# bind + Access-in-front (Cloudflare Tunnel → team.convivy.com) is the security
# model; the raw port is never exposed.
set -eu
# Self-locating (ADR-0059/bin/install-orient): this script runs unmodified from
# either a development checkout or the deploy area at ~/.local/deploy/convivy-orient,
# whichever directory it was copied into — never a hardcoded checkout path.
DIR="$(cd "$(dirname "$0")" && pwd)"
# The interpreter travels with the code (ADR-0059 rule 4): a venv inside this
# same directory, not the machine-wide ~/.local/venv-orient every launchd job
# used before the deploy-area cutover — that path was shared across a daemon
# and three periodic builder jobs, two of which (com.convivy.orient-dashboard,
# com.convivy.code-health, com.convivy.kb-observability) didn't even use it and
# ran the system python3.11 instead, a live divergence bin/install-orient closes
# by building every one of the seven jobs' interpreter here.
#
# That's only true once the cutover has actually happened. This checkout has
# never had its own .venv — unlike a deploy area, which always has both
# DEPLOYED_COMMIT (written by bin/install-orient) and .venv (built next to
# it) — so defaulting unconditionally to $DIR/.venv/bin/python crashes the
# still-live checkout on its next automatic restart, before anyone runs the
# installer (PR #236 round 2 finding: com.convivy.orient-deploy-watch
# kickstarts this job every 300s). Fall back to the checkout's own external
# venv when neither marker is present. CHECKOUT_PY is overridable via
# ORIENT_CHECKOUT_PY for tests; PY overrides the final resolved value for
# either branch, for manual testing against a different interpreter — every
# real launchd job leaves both at the default. A missing venv is a hard
# failure by design — no fallback to system python, so a broken venv fails
# loud, not silently.
if [ -x "$DIR/.venv/bin/python" ] || [ -f "$DIR/DEPLOYED_COMMIT" ]; then
    PY="${PY:-$DIR/.venv/bin/python}"
else
    CHECKOUT_PY="${ORIENT_CHECKOUT_PY:-$HOME/.local/venv-orient/bin/python}"
    PY="${PY:-$CHECKOUT_PY}"
fi

# `--check` runs the interpreter, import, config/state-root, Library-reachability
# and control_db-migration-chain checks below without rebuilding anything,
# applying a migration, or binding the port — bin/install-orient runs it
# against the STAGED build before swapping it in (ADR-0059's pre-swap gate),
# same idiom as the Library's own serve.sh --check. It covers every step a
# real start also runs before `exec "$PY" "$DIR/server.py"` EXCEPT two:
# build.py's render()/write path (the vendored JS templates and the site/
# write under STATE_ROOT) — see the PREFLIGHT block below for why
# build.load_docs() is called directly instead of running build.py itself —
# and the actual application of control_db.init_db()'s alembic migration
# (the first step after `cd "$DIR"` below), which only a real start performs.
# PREFLIGHT compares the staged code's migration chain against the live
# database's current revision instead, without applying it (see that block
# for why applying it here would be worse than not checking at all).
# PR #236 round 2 found the
# control_db gap after round 1 had already fixed the KB-index one — both
# were this same comment claiming more coverage than the file had.
CHECK_ONLY=0
if [ "$#" -gt 1 ]; then
    echo "serve: too many arguments — the only option is --check" >&2
    exit 64   # EX_USAGE
fi
case "${1:-}" in
    --check) CHECK_ONLY=1 ;;
    "")      ;;
    *)       echo "serve: unknown argument '$1' — the only option is --check" >&2
             exit 64 ;;   # EX_USAGE
esac

if [ ! -x "$PY" ]; then
    echo "serve: no interpreter at $PY — create it with:" >&2
    echo "  python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 78   # EX_CONFIG
fi

# The config and state roots this process will read credentials from and write
# site/access.log to (ADR-0059 rule 3, paths.py) — checked here with the same
# defaulting paths.py itself applies, so a root that can't be used fails
# before serving rather than on whichever request first touches it. paths.py
# enforces the absolute-path half of this on every import.
#
# **A MISSING ROOT IS CREATED, EMPTY, MODE 700** (issue_01M3J7T3CQ0R1A7G3AP2668WHS).
# ADR-0059 rule 3 says config and state "are never written by an install", so on
# a clean machine nothing else makes them: Shipwright's first Linux start
# refused here with `ORIENT_CONFIG_ROOT=... does not exist`. An empty config
# root is a working start that fails closed, since a missing jwt.json refuses
# every request (access_identity.jwt_config), so there is nothing to seed. A
# root that exists but is not a directory, or cannot be read, still refuses.
# `--check` creates nothing, so the pre-swap gate stays read-only.
CONFIG_ROOT="${ORIENT_CONFIG_ROOT:-$DIR}"
STATE_ROOT="${ORIENT_STATE_ROOT:-$DIR}"
for _root_pair in "ORIENT_CONFIG_ROOT:$CONFIG_ROOT" "ORIENT_STATE_ROOT:$STATE_ROOT"; do
    _root_name="${_root_pair%%:*}"
    _root_val="${_root_pair#*:}"
    case "$_root_val" in
        /*) ;;
        *) echo "serve: $_root_name=$_root_val must be an absolute path" >&2; exit 78 ;;
    esac
    if [ ! -e "$_root_val" ] && [ ! -L "$_root_val" ]; then
        if [ "$CHECK_ONLY" -eq 1 ]; then
            echo "serve: $_root_name=$_root_val does not exist; a start will create it empty" >&2
            continue
        fi
        # umask 077 in a subshell, because `-m 700` sets only the last
        # component: every parent `mkdir -p` makes would otherwise take the
        # ambient umask, and a writable parent lets another user replace the
        # config root (PR #335 security review).
        (umask 077 && mkdir -p -m 700 "$_root_val") \
            || { echo "serve: $_root_name=$_root_val does not exist and could not be created" >&2; exit 78; }
        echo "serve: created $_root_name=$_root_val, empty — Orient refuses every request until its config is written" >&2
    fi
    [ -d "$_root_val" ] || { echo "serve: $_root_name=$_root_val is not a directory" >&2; exit 78; }
    [ -r "$_root_val" ] || { echo "serve: $_root_name=$_root_val is not readable" >&2; exit 78; }
done

# Every module a launchd job execs against this venv, imported up front so a
# broken deploy (wrong interpreter, a dependency the venv build silently
# skipped) fails here instead of on whichever route or job happens to import
# it first. `server` alone pulls in most of these transitively — including
# dashboard_build, velocity_build, code_health_build and kb_build, the four
# other jobs this same venv serves — because server.py imports them; the
# capture jobs are separate entry points server.py never imports, so they're
# named explicitly. com.convivy.orient-deploy-watch is not in this list: its
# plist still points at the development checkout (ADR-0059's deploy-watch
# question is unsettled — see this PR's body), so it never runs against this
# venv at all.
#
# This heredoc is quoted (<<'PREFLIGHT'), so the outer shell passes its body
# through verbatim — a comment inside it can contain any character, including
# a backtick or dollar-sign, without the shell trying to expand it first (PR
# #236 round 2: a backtick in a comment here syntax-errored the whole script
# before python ever ran, back when this heredoc was unquoted to let $DIR
# reach the script below directly; DIR now arrives via the environment
# instead, which was the only thing that needed the shell's help).
ORIENT_PREFLIGHT_DIR="$DIR" "$PY" - <<'PREFLIGHT'
import os
import sys
sys.path.insert(0, os.environ["ORIENT_PREFLIGHT_DIR"])
missing = []
for mod in ("server", "github_capture", "build", "control_db"):
    try:
        __import__(mod)
    except ImportError as e:
        missing.append(f"{mod} ({e})")
if missing:
    print("serve: preflight FAILED, refusing to start on this interpreter:", file=sys.stderr)
    for m in missing:
        print(f"  missing: {m}", file=sys.stderr)
    print(f"  interpreter: {sys.executable}", file=sys.stderr)
    sys.exit(1)

# build.py's Library reads (issue_01M1MFJRR46TMQ6DSJSD7DGX7C moved load_docs()
# off a direct ~/.knowledge-index/index.db read) only happen inside
# load_docs(), never at import time — the __import__ above cannot catch an
# unreachable or misconfigured Library, and build.py's own invocation below
# (unlike every builder after it) has no non-fatal guard, so that failure
# would abort a real start after the swap already committed (PR #236 round 2
# finding, unchanged by the move: build.py's call site is still unconditional).
# Calling load_docs() directly exercises that read without build()'s side
# effects — it never touches STATE_ROOT or vendored JS.
#
# **This makes a real start hostage to the Library being reachable, and that
# is a real, deliberate narrowing, not an oversight** — a machine with a valid
# KB index used to start even if nothing else about the deployment was
# talking to the Library. It is accepted rather than routed around because
# build.py's own invocation two dozen lines below is exactly as unconditional
# as this preflight call: a start that reached that line with the Library
# down would already fail there, just after the swap had committed rather
# than before it. This preflight does not add a new way for a start to fail;
# it moves the existing one earlier, which is the whole reason PR #236 round 2
# added it. Revisit if build.load_docs() ever grows a fallback path.
import build
try:
    build.load_docs()
except Exception as e:
    print(f"serve: preflight FAILED, build.load_docs() cannot reach the Library: {e}", file=sys.stderr)
    print(f"  interpreter: {sys.executable}", file=sys.stderr)
    sys.exit(1)

# control_db.init_db() (the first step after `cd "$DIR"` below) runs
# `alembic upgrade head` against
# the real, live ~/.orient-control/control.db — a second fatal, ungated step
# between --check's exit and exec server.py that the header comment above
# CHECK_ONLY used to not mention at all (PR #236 round 2 finding). It can't
# simply be called here too: this preflight runs against a STAGED,
# not-yet-swapped build, so actually applying its migration would move the
# live database's schema ahead of whatever code the swap ends up serving if
# the install then fails or is rolled back — worse than the gap being
# closed. Instead, ask alembic what revision the staged code's migration
# chain resolves to and what revision the live database is actually at, and
# confirm alembic could walk from one to the other, without applying
# anything. iterate_revisions is the same resolution alembic's own upgrade
# command uses internally to decide what to run; calling it here exercises
# the three things most likely to fail — the migration files being
# readable, the database file being openable, and the revision chain being
# coherent — without writing to a database an unswapped build has no
# business changing yet. A migration that only fails once actually applied
# (e.g. a data conflict specific to the live rows) is not caught here;
# control_db.init_db() itself remains the only place that finds that out.
import control_db
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from alembic.runtime.migration import MigrationContext
try:
    acfg = AlembicConfig(str(control_db.DIR / "alembic.ini"))
    acfg.set_main_option("script_location", str(control_db.DIR / "alembic"))
    ascript = ScriptDirectory.from_config(acfg)
    ahead = ascript.get_current_head()
    if control_db.CONTROL_DB.exists():
        with control_db.get_engine().connect() as aconn:
            acurrent = MigrationContext.configure(aconn).get_current_revision()
    else:
        acurrent = None
    list(ascript.iterate_revisions(ahead, acurrent, implicit_base=True))
except Exception as e:
    print(f"serve: preflight FAILED, control_db's migration chain cannot reach head: {e}", file=sys.stderr)
    print(f"  interpreter: {sys.executable}", file=sys.stderr)
    sys.exit(1)

print(f"serve: preflight ok on {sys.executable}")
PREFLIGHT

if [ "$CHECK_ONLY" -eq 1 ]; then
    exit 0
fi

# Record which commit this start IS ABOUT TO serve, before any of the work
# below has happened (issue_01M0Y94CXQ8S1QPMWS319YS0JN). Until this marker
# existed, deploy_watch.py had no evidence a restart was progressing until
# the *served* marker near the bottom of this file landed — and that marker
# is written after nine builders and an alembic migration. On 2026-08-25,
# PR #208's own merge took 236s from kickstart to that marker; deploy_watch's
# ceiling back then was 150s, so a server that had been quietly building the
# whole time read as wedged. This "started" marker gives the watcher a
# checkpoint within seconds of a healthy kickstart — before the chain below
# has any chance to finish — so it can tell "launched and building" from
# "never came up" without waiting for the whole chain either way. See
# deploy_watch.py's RESTART_TIMEOUT_S / RESTART_CONVERGING_GRACE_S for how
# the two markers are used together.
#
# ~/.orient-control holds credential-shaped state (push subscriptions,
# capture cursors, the control DB) and must not be group/other-readable
# (issue_01M2MFGVAW1C8ZQ7G9CT0E1YXZ). `umask 077` from here on covers every
# write this script makes AND everything the exec'd server process writes for
# its whole lifetime (access.log, the site/ bundle rebuilt by the chain
# below) — all read back only by this same user, so the tighter mode changes
# nothing about who can read them. `mkdir -p` alone is masked by whatever
# umask was already in effect and is a no-op on a directory that already
# exists looser, so `chmod` afterwards is what actually guarantees 0700.
umask 077
mkdir -p "$HOME/.orient-control"
chmod 700 "$HOME/.orient-control"
# The commit comes from whichever installer wrote this area: bin/install-orient
# writes DEPLOYED_COMMIT, and Builder's shipwright install-backend writes
# .installed-by-install-backend, JSON with a "commit" field
# (issue_01M3JQDZJ6XQ7SGWYTMMHRKXAR; Shipwright's clean-Linux start died here on
# `fatal: not a git repository`). A development checkout has neither and asks
# git. Each branch fails under `set -e` when it yields no commit — the JSON read
# raises on a missing key — so a start never records an empty or invented one.
if [ -f "$DIR/DEPLOYED_COMMIT" ]; then
    SERVING_SHA="$(sed -n 's/^commit: //p' "$DIR/DEPLOYED_COMMIT")"
elif [ -f "$DIR/.installed-by-install-backend" ]; then
    SERVING_SHA="$("$PY" -c 'import json, sys; c = json.load(open(sys.argv[1]))["commit"]; assert isinstance(c, str) and c.strip(), "no commit"; print(c.strip())' "$DIR/.installed-by-install-backend")"
else
    SERVING_SHA="$(git -C "$DIR" rev-parse HEAD)"
fi
echo "$SERVING_SHA" > "$HOME/.orient-control/orient-restart-started.tmp"
mv "$HOME/.orient-control/orient-restart-started.tmp" "$HOME/.orient-control/orient-restart-started"

cd "$DIR"
# Bring the control-plane datastore (~/.orient-control/control.db) to schema head
# via alembic BEFORE the build chain below (WS0.5/0.7, ARCH-348). Idempotent;
# fatal on failure because the write API can't run without its schema. PREFLIGHT
# above already confirmed alembic can resolve a path from the live database's
# current revision to this build's head without applying it — this line is what
# actually applies it, and a failure here is the one thing that comparison
# can't rule out (PR #236 round 2).
#
# THIS LINE MUST STAY ABOVE EVERY BUILDER, and the ordering is the point rather
# than a tidiness preference. Builders read control-plane tables — the build()
# that dashboard_build.py runs on import calls alert_policy_store.get() — so on
# the one restart that deploys a migration, a builder placed above this line
# runs against the schema
# the PREVIOUS deploy left behind. It raises `no such table: <whatever the
# migration adds>`, its `|| echo` swallows the failure as non-fatal, and the page
# serves stale while the migration completes seconds later. That is what happened
# on the 2026-09-16 22:19 MST restart deploying PR #307: dashboard_build.py:1475
# raised `no such table: alert_policies` and the migration creating that table ran
# 62 lines further down. Self-clearing on the next restart, which is exactly why
# it survived — the evidence is gone by the time anyone looks. Moving a builder
# above this line brings the whole class back.
ORIENT_INIT_DB_DIR="$DIR" "$PY" -c "import os, sys; sys.path.insert(0, os.environ['ORIENT_INIT_DB_DIR']); import control_db; control_db.init_db()"
"$PY" "$DIR/build.py"
# Spending (/spending) + cost-performance (/cost-performance) dashboards, both
# from dashboard_build.py (full-tier). Non-fatal: a scorecard-ledger hiccup must
# not stop the KB site from serving.
"$PY" "$DIR/dashboard_build.py" || echo "dashboard_build failed (non-fatal); serving without fresh cost dashboards" >&2
# Engineering velocity (full-tier /velocity). Non-fatal, same as the dashboard.
"$PY" "$DIR/velocity_build.py" || echo "velocity_build failed (non-fatal); serving without a fresh velocity page" >&2
# Session-activity log static fallback (/activity, ARCH-191/239). Live render is
# primary (reads activity/sessions.json); this seeds the fallback. Non-fatal.
"$PY" "$DIR/activity_build.py" || echo "activity_build failed (non-fatal); serving without a fresh activity page" >&2
# Engineering KB browse + ARCH log static fallbacks (/engineering*, ARCH-194). Live
# render is primary; these are served only if a live render throws. Non-fatal.
"$PY" "$DIR/engineering_build.py" || echo "engineering_build failed (non-fatal); serving without fresh engineering fallbacks" >&2
# briefs_build.py was removed: ARCH-372 retired /engineering/briefs into the
# Reading Room (room_build.py reads briefs as entitled KB docs instead), and
# the module's only remaining reader, convivy-lab's public mirror, is gone.
# Agent roster static fallback (/engineering/roster). Live render is primary.
"$PY" "$DIR/roster_build.py" || echo "roster_build failed (non-fatal); serving without fresh roster fallback" >&2
# CI/test-results page static fallback (/engineering/e2e, ARCH-353 Phase A). Live render is primary.
"$PY" "$DIR/engineering_e2e_build.py" || echo "engineering_e2e_build failed (non-fatal); serving without a fresh e2e fallback" >&2
# Reports (/engineering/reports) + Notes (/engineering/notes) static fallbacks
# (ARCH-353 Phase B). Live render is primary. Alerts moved off this module to
# /alerts under Dashboards (epic issue_01M0EHRNW5HVJCAXZHYQCBZ3YW) — like the
# other Dashboards pages (health/security/warrant/kb), it has no
# serve.sh fallback build; launchd/com.convivy.alerts.plist seeds site/alerts.html
# on its own schedule instead, after the snapshots it reads are fresh.
"$PY" "$DIR/ops_reports_build.py" || echo "ops_reports_build failed (non-fatal); serving without fresh reports/notes fallbacks" >&2
# design_build.py was removed: ARCH-357 retired /engineering/design (server.py
# redirects it to /room) and its only remaining reader, convivy-lab's public
# mirror, is gone.
# In-box changelog static fallback (/engineering/changelog, ARCH-359). Live render is primary.
"$PY" "$DIR/changelog_build.py" || echo "changelog_build failed (non-fatal); serving without a fresh changelog fallback" >&2
# Merge-queue static fallback (/engineering/merge-queue, ARCH-359 step 3). Live render is primary (reads GitHub live); this seeds an empty-state fallback only.
"$PY" "$DIR/merge_queue_build.py" || echo "merge_queue_build failed (non-fatal); serving without a fresh merge-queue fallback" >&2
# Release-notes static fallback (/products/release-notes, ARCH-360). Live render is primary.
"$PY" "$DIR/release_notes_build.py" || echo "release_notes_build failed (non-fatal); serving without a fresh release-notes fallback" >&2
# Signal-memory browse static fallback (/signals, S1-b/ARCH-349). Live render is
# primary; this seeds an empty-state fallback served only if a live render throws.
"$PY" "$DIR/signals_build.py" || echo "signals_build failed (non-fatal); serving without a fresh signals fallback" >&2
# Archives' PRs + Changelogs static fallbacks (/signals/prs, /signals/changelogs).
# Live render is primary; this seeds empty-state fallbacks served only if a live
# render throws.
"$PY" "$DIR/archives_build.py" || echo "archives_build failed (non-fatal); serving without fresh PRs/changelogs fallbacks" >&2
# Issue-queue static fallback (/engineering/issues, ARCH-359/361). Live render is
# primary. This line was MISSING since the queue shipped, so site/issues.html was
# never written and a failed live render 500'd instead of serving the empty-state
# fallback the module already knew how to produce.
"$PY" "$DIR/issue_build.py" || echo "issue_build failed (non-fatal); serving without a fresh issues fallback" >&2
# Home static fallbacks (site/home.{full,contractor}.html). Live render is
# primary; without this line the fallback silently ages across deploys (it was
# 3 weeks stale when the Recent-Docs deep-link fix shipped, 2026-07-16).
"$PY" "$DIR/home_build.py" || echo "home_build failed (non-fatal); serving without fresh home fallbacks" >&2
# Crews static fallback (/crews, issue_01M2KVHTVFF4MAQF94M2SP6TZ0). Live render
# is primary. This line was MISSING since the page shipped, so site/crews.html
# was never written and a Library stall 500'd instead of serving a stale page.
"$PY" "$DIR/crews_build.py" || echo "crews_build failed (non-fatal); serving without a fresh crews fallback" >&2
# Chart House static fallback (/chart-house, issue_01M2KVHTVFF4MAQF94M2SP6TZ0):
# the same gap crews_build had — never built, so a Library stall 500'd here too.
"$PY" "$DIR/chart_house_build.py" || echo "chart_house_build failed (non-fatal); serving without a fresh chart-house fallback" >&2
# Record the commit this start IS NOW serving (issue_01M0EERRFC8NJ8QS5Q6EEE87JY):
# deploy_watch.py compares this against the checkout's HEAD to tell whether the
# running process ever picked up a pull, independent of whether IT triggered the
# restart — a human kickstart (like this one) updates the same marker.
# $SERVING_SHA was already resolved once, above, alongside the "started"
# marker (DEPLOYED_COMMIT or .installed-by-install-backend in a deploy area,
# since neither installer leaves a .git there; `git rev-parse HEAD` in a
# development checkout) — reused here
# rather than re-read, so the two markers can never disagree about which
# commit they name.
echo "$SERVING_SHA" > "$HOME/.orient-control/orient-serving-commit.tmp"
mv "$HOME/.orient-control/orient-serving-commit.tmp" "$HOME/.orient-control/orient-serving-commit"
exec "$PY" "$DIR/server.py"
