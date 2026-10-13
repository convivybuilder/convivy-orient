"""Authorization matrix tests (WS0.6, ARCH-348).

The security-critical surface: a bug here lets a contractor approve a decision or
an agent decide. So the whole allow/deny matrix is enumerated, deny-by-default is
proven, and the load-bearing invariant — an agent can NEVER decide — is tested
head-on, including the case where a compromised roles.json tries to hand an agent
an admin role.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import authz
from authz import Identity, agent_identity, authorize

REPO_ROOT = Path(__file__).resolve().parent.parent

ALL_ACTIONS = ["decision.read", "decision.create", "decision.consume",
               "decision.comment", "decision.decide"]

ADMIN = Identity("operator@example.com", "human", "admin", frozenset({"*"}))
MEMBER = Identity("m@example.com", "human", "member", frozenset({"vivy"}))
OBSERVER = Identity("k@example.com", "human", "observer", frozenset({"*"}))
CONTRACTOR = Identity("c@x.example", "human", "contractor", frozenset({"vivy"}))
AGENT = agent_identity("overnight-orchestrator", frozenset({"vivy"}))


def test_admin_can_do_everything_in_scope():
    for action in ALL_ACTIONS:
        assert authorize(ADMIN, action, "vivy") is True
        assert authorize(ADMIN, action, "constance") is True  # "*" scope


def test_member_decides_and_writes_only_in_scope():
    for action in ALL_ACTIONS:
        assert authorize(MEMBER, action, "vivy") is True
    # out of scope: everything denied, including read
    for action in ALL_ACTIONS:
        assert authorize(MEMBER, action, "constance") is False


def test_signal_capabilities_by_role():
    # internal signal (System A): every internal identity may read + create;
    # a contractor may not (decision "read" must NOT leak into the signal store).
    for ident in (ADMIN, MEMBER, AGENT):
        assert authorize(ident, "signal.create", "vivy") is True
        assert authorize(ident, "signal.read", "vivy") is True
    assert authorize(CONTRACTOR, "signal.read", "vivy") is False
    assert authorize(CONTRACTOR, "signal.create", "vivy") is False


def test_customer_class_is_internal_team_only():
    # System B customer content: admin + member (internal team) hold signal_customer;
    # a generic agent and a contractor do not — no customer PII to automation/externals.
    assert authorize(ADMIN, "signal.create_customer", "vivy") is True
    assert authorize(MEMBER, "signal.read_customer", "vivy") is True
    assert authorize(AGENT, "signal.create_customer", "vivy") is False
    assert authorize(AGENT, "signal.read_customer", "vivy") is False
    assert authorize(CONTRACTOR, "signal.read_customer", "vivy") is False


def test_contractor_is_read_only():
    assert authorize(CONTRACTOR, "decision.read", "vivy") is True
    for action in ["decision.create", "decision.consume", "decision.comment", "decision.decide"]:
        assert authorize(CONTRACTOR, action, "vivy") is False


def test_observer_reads_everything_writes_nothing():
    # ARCH-351: observer is the full-read internal role — both planes readable,
    # customer content included, with no write capability and no decide.
    assert authorize(OBSERVER, "decision.read", "vivy") is True
    assert authorize(OBSERVER, "signal.read", "vivy") is True
    assert authorize(OBSERVER, "signal.read_customer", "vivy") is True
    for action in ["decision.create", "decision.consume", "decision.comment",
                   "decision.decide", "signal.create"]:
        assert authorize(OBSERVER, action, "vivy") is False


CONTRIBUTOR = Identity("k@example.com", "human", "contributor", frozenset({"*"}))


def test_contributor_is_observer_plus_signal_capture():
    # ARCH-351 follow-up: contributor reads everything and CAPTURES signals
    # (internal and customer class — she logs customer calls), with no
    # decision-plane writes and no decide.
    assert authorize(CONTRIBUTOR, "decision.read", "vivy") is True
    assert authorize(CONTRIBUTOR, "signal.read", "vivy") is True
    assert authorize(CONTRIBUTOR, "signal.read_customer", "vivy") is True
    assert authorize(CONTRIBUTOR, "signal.create", "vivy") is True
    assert authorize(CONTRIBUTOR, "signal.create_customer", "vivy") is True
    for action in ["decision.create", "decision.consume", "decision.comment",
                   "decision.decide"]:
        assert authorize(CONTRIBUTOR, action, "vivy") is False


def test_kb_write_matches_blog_write_roles():
    # The in-window KB editor (issue_01M0TKX6DW2E1TXGN9QT6K61J5 step 4): full
    # tier (enforced by server.py's route gate, not by this capability alone)
    # plus kb_write, same roles as blog_write and for the same reason — this
    # authorizes a direct, attributed commit to a git-reviewed company repo,
    # not a decision or an ordinary read.
    assert authorize(ADMIN, "kb.write", None) is True
    assert authorize(MEMBER, "kb.write", None) is True
    assert authorize(OBSERVER, "kb.write", None) is False
    assert authorize(CONTRACTOR, "kb.write", None) is False
    assert authorize(AGENT, "kb.write", None) is False  # never an agent


def test_kb_writer_roles_track_kb_write():
    for role in ("admin", "member"):
        assert role in authz.KB_WRITER_ROLES, role
    for role in ("observer", "contractor", "contributor", "agent", "system"):
        assert role not in authz.KB_WRITER_ROLES, role


def test_capture_roles_track_signal_write():
    # __CAN_CAPTURE__ (the /signals log-a-signal form) keys off CAPTURE_ROLES —
    # derived from signal_write, the same way DECIDER_ROLES used to derive from
    # decide before the decision plane was removed (2026-08-18).
    for role in ("admin", "member", "contributor", "agent", "system"):
        assert role in authz.CAPTURE_ROLES, role
    for role in ("observer", "contractor"):
        assert role not in authz.CAPTURE_ROLES, role


def test_agent_can_write_but_never_decide():
    for action in ["decision.read", "decision.create", "decision.consume", "decision.comment"]:
        assert authorize(AGENT, action, "vivy") is True
    assert authorize(AGENT, "decision.decide", "vivy") is False  # THE invariant


def test_agent_cannot_decide_even_if_roles_claim_admin():
    # a forged/compromised entry that hands an agent an admin role must not grant
    # decide — the effective role for kind=agent is pinned to "agent".
    forged = Identity("evil-agent", "agent", "admin", frozenset({"*"}))
    assert authorize(forged, "decision.decide", "vivy") is False
    assert authorize(forged, "decision.create", "vivy") is True  # writes still fine


def test_product_owner_is_a_member_who_approves_its_own_products_epics():
    # issue_01M350CGZB6DR577FASP53K7WM; the operator, 2026-09-30: member permissions
    # plus epic_approve, scoped to the person's own products.
    assert authz.ROLE_CAPS["product_owner"] == authz.ROLE_CAPS["member"] | {"epic_approve"}
    owner = Identity("po@example.com", "human", "product_owner", frozenset({"vivy"}))
    assert authorize(owner, "epic.approve", "vivy") is True
    assert authorize(owner, "epic.approve", "constance") is False
    assert "product_owner" in authz.EPIC_APPROVER_ROLES


def test_product_owner_is_never_an_agent_role():
    # The operator, 2026-09-30: "product owner is reserved for a human operator, it's
    # not an agent role." An agent or system identity carrying the role
    # string, in its own product, still cannot approve.
    for kind in ("agent", "system"):
        forged = Identity("evil-agent", kind, "product_owner", frozenset({"vivy"}))
        assert authorize(forged, "epic.approve", "vivy") is False, kind


def test_unknown_action_denied_by_default():
    assert authorize(ADMIN, "decision.delete", "vivy") is False
    assert authorize(ADMIN, "", "vivy") is False
    assert authorize(ADMIN, "arbitrary", None) is False


def test_null_product_is_not_a_free_pass_for_a_write_capability():
    # issue_01M0PGV7W1DFEKX2WCXKTBBKH9: in_scope used to return True
    # unconditionally for product=None, so a `products: []` identity — the
    # narrowest scope this file can express — was refused signal.create for a
    # named product but WAVED THROUGH for a null one. Same admin role on all
    # three identities so only the scope check (not the capability check)
    # varies; the read half is this brief's expected-value oracle.
    star = Identity("star@x.example", "human", "admin", frozenset({"*"}))
    named = Identity("named@x.example", "human", "admin", frozenset({"convivy-orient"}))
    empty = Identity("empty@x.example", "human", "admin", frozenset())

    # named-product behaviour is untouched by this fix.
    assert authorize(star, "signal.create", "vivy") is True
    assert authorize(named, "signal.create", "vivy") is False
    assert authorize(empty, "signal.create", "vivy") is False

    # product=None is no longer a free pass for the WRITE capability
    # (signal.create -> signal_write), for any of the three scopes — a create
    # action always needs a real product, "*" scope included.
    assert authorize(star, "signal.create", None) is False
    assert authorize(named, "signal.create", None) is False
    assert authorize(empty, "signal.create", None) is False

    # product=None stays legitimate for the matching READ capability
    # (signal.read -> signal_read): a cross-product list route filters its
    # own results, unchanged by this fix, for any of the three scopes.
    assert authorize(star, "signal.read", None) is True
    assert authorize(named, "signal.read", None) is True
    assert authorize(empty, "signal.read", None) is True


# Every ACTION_CAP action not in NON_SCOPED_ACTIONS is a genuinely product-scoped
# write, where product=None must never satisfy scope. Named explicitly here so
# that adding a new ACTION_CAP action without also classifying it in
# NON_SCOPED_ACTIONS fails this test, rather than shipping a route that silently
# 403s the first time someone omits a product — the failure moves from a
# production surprise to a red test at review time.
KNOWN_SCOPED_WRITE_ACTIONS = frozenset({
    "decision.create", "decision.consume", "decision.comment", "decision.decide",
    "signal.create", "signal.create_customer",
    "issue.create", "issue.claim", "issue.resolve", "issue.comment",
    "course.write",
    # Approving an epic (issue_01M32CB6A9T04NVYPARGDT2GWQ). Scoped, not
    # site-wide like blog.write and kb.write: an epic belongs to one product,
    # and approving it writes that product's Course. `approve_api` reads the
    # epic before the check for exactly this reason, so it has a product to
    # pass — an unscoped call refuses everyone, admin included.
    "epic.approve",
    # Editing a proposed epic's text, scoped like approving it: the epic is read
    # first and its product passed.
    "epic.revise",
    # Choosing a queued item's model, scoped the same way: the item is read
    # first and its product passed.
    "item.model",
    # Setting a project's ledger cap set (issue_01M4BH8KSSMC8XBKB2C2CX9QTF).
    # Scoped like item.model: the project in the path IS the scope, a cap
    # belongs to one project, and an unscoped call must refuse everyone.
    "ledger.caps_write",
})


def test_every_action_cap_key_is_classified():
    # Catches an action missing from BOTH sets. It does not catch one
    # misclassified into the wrong set — a genuinely scoped write added to
    # NON_SCOPED_ACTIONS by mistake would still pass this test.
    unclassified = (set(authz.ACTION_CAP) - authz.NON_SCOPED_ACTIONS
                    - KNOWN_SCOPED_WRITE_ACTIONS)
    assert not unclassified, (
        f"{unclassified} is in ACTION_CAP but neither in NON_SCOPED_ACTIONS nor "
        "in this test's KNOWN_SCOPED_WRITE_ACTIONS — classify it in authz.py's "
        "NON_SCOPED_ACTIONS (if product=None is legitimate for it) or add it to "
        "KNOWN_SCOPED_WRITE_ACTIONS above (if it's a genuinely product-scoped "
        "write), so a future action can't fall through unclassified.")
    stale = KNOWN_SCOPED_WRITE_ACTIONS - set(authz.ACTION_CAP)
    assert not stale, f"{stale} no longer in ACTION_CAP — remove from this test"


def test_scope_star_vs_specific():
    star = Identity("a@x.example", "human", "member", frozenset({"*"}))
    assert authorize(star, "decision.decide", "anything") is True
    narrow = Identity("b@x.example", "human", "member", frozenset({"vivy", "constance"}))
    assert authorize(narrow, "decision.decide", "vivy") is True
    assert authorize(narrow, "decision.decide", "sous-chef") is False


# --- human_identity resolution from roles.json --------------------------------


@pytest.fixture
def roles_file(tmp_path, monkeypatch):
    f = tmp_path / "roles.json"
    monkeypatch.setattr(authz, "ROLES", f)
    return f


def test_human_identity_unknown_email_is_bare_contractor(roles_file):
    roles_file.write_text(json.dumps({"operator@example.com": {"role": "admin"}}))
    ident = authz.human_identity("stranger@example.com", frozenset())
    assert ident.role == "contractor" and ident.products == frozenset()
    assert authorize(ident, "decision.read", "vivy") is False  # no scope → nothing
    assert authorize(ident, "decision.decide", "vivy") is False


def test_human_identity_is_case_insensitive(roles_file):
    roles_file.write_text(json.dumps({"operator@example.com": {"role": "admin"}}))
    ident = authz.human_identity("Operator@Example.COM", frozenset({"*"}))
    assert ident.role == "admin" and authorize(ident, "decision.decide", "vivy") is True


def test_human_identity_unrecognized_role_falls_to_contractor(roles_file):
    roles_file.write_text(json.dumps({"x@x.example": {"role": "superuser"}}))
    ident = authz.human_identity("x@x.example", frozenset({"*"}))
    assert ident.role == "contractor"  # unknown role grants nothing beyond read


def test_malformed_roles_file_denies_all(roles_file):
    roles_file.write_text("{ not json")
    ident = authz.human_identity("operator@example.com", frozenset())
    assert ident.role == "contractor" and ident.products == frozenset()


def test_missing_roles_file_denies_all(tmp_path, monkeypatch):
    monkeypatch.setattr(authz, "ROLES", tmp_path / "does-not-exist.json")
    ident = authz.human_identity("operator@example.com", frozenset())
    assert ident.role == "contractor"


# --- `products` always comes from the caller, never from roles.json ---------
#
# E19·1 (issue_01M35K54QHJYYPXWS28YWEHJP3): Orient holds no local table, file
# or constant mapping a person to their projects, so `human_identity` must
# carry whatever `products` its caller passes, unchanged, whatever role.json
# says or doesn't say about that email — roles.json still decides `role` and
# `queues`, never `products`.


def test_human_identity_carries_the_passed_products_even_for_a_bare_contractor(roles_file):
    roles_file.write_text(json.dumps({}))  # no entry at all → bare contractor
    ident = authz.human_identity("nobody@example.com", frozenset({"vivy"}))
    assert ident.role == "contractor" and ident.products == frozenset({"vivy"})


def test_human_identity_carries_the_passed_products_for_a_known_role(roles_file):
    roles_file.write_text(json.dumps({"scoped@example.com": {"role": "admin"}}))
    ident = authz.human_identity("scoped@example.com", frozenset({"vivy", "constance"}))
    assert ident.role == "admin" and ident.products == frozenset({"vivy", "constance"})


def test_roles_json_has_no_products_key_read_by_human_identity(roles_file):
    # A roles.json entry that still carries a "products" list (a stale fixture,
    # an un-migrated grant file) must have no effect — proven by setting one
    # that would grant "vivy" and showing the resolved identity's scope is
    # exactly what the CALLER passed, "constance" only.
    roles_file.write_text(json.dumps(
        {"x@example.com": {"role": "admin", "products": ["vivy"]}}))
    ident = authz.human_identity("x@example.com", frozenset({"constance"}))
    assert ident.products == frozenset({"constance"})


def test_no_code_reads_a_roles_json_entrys_products_key():
    """E19·1's grep clause (issue_01M35K54QHJYYPXWS28YWEHJP3): "Orient holds no
    local table, file or constant mapping people to projects." roles.json
    still carries `role` and `queues`; `products` must come from the Library
    on every request (`library_client.person_scope`), never from a roles.json
    entry.

    Walks `git ls-files` rather than the working tree, so an untracked local
    override (a developer's own roles.json, a stray scratch file) can't hide
    a real regression or manufacture a false one. Test code is skipped: a
    fake standing in for the Library has to map people to projects.

    THREE SHAPES, checked by parsing rather than by matching one spelling,
    because a literal pattern such as `entry.get("products"` misses the same
    read written with another receiver name (review round 1 on PR #371):

    - a module that reads roles data (`ROLES`, `_roles_lowercased`, or a
      "roles.json" string) and also names a `"products"` key anywhere in it.
      The check covers the whole module rather than one function, because
      review round 2 showed the read and the key split across two functions
      slipping past a per-function check. `api_common.api_tokens()` stays
      legal: it reads an AGENT token's grant from api-tokens.json, a
      bearer-token scope rather than a person-to-project mapping, and
      api_common never touches roles data. A module that names a "products"
      key on roles data it got from ANOTHER module is outside what parsing
      one file can see; `test_roles_json_has_no_products_key_read_by_human_identity`
      covers the one path that hands roles data out;
    - a module-level dict constant keyed by email addresses whose values are
      collections, the shape of a hard-coded people-to-projects table;
    - a tracked JSON file keyed by email addresses whose entries carry a
      "products" key, the shape roles.json had.

    Confirmed to fail on the base commit, where `authz.human_identity` read
    `_roles_lowercased()` and then `entry.get("products", [])` in one body."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True,
        text=True, check=True).stdout.splitlines()
    offenders = _people_to_project_mappings(
        REPO_ROOT, [rel for rel in tracked if not rel.startswith("tests/")])
    assert offenders == [], (
        f"{offenders} map people to projects — project scope must come from "
        f"library_client.person_scope on every request (E19·1).")


def _people_to_project_mappings(root: Path, rels: list[str]) -> list[str]:
    import ast

    roles_markers = {"ROLES", "_roles_lowercased"}

    def reads_roles(node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in roles_markers:
                return True
            if isinstance(sub, ast.Attribute) and sub.attr in roles_markers:
                return True
            if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                    and sub.value.endswith("roles.json")):
                return True
        return False

    def names_products(node: ast.AST) -> bool:
        return any(isinstance(sub, ast.Constant) and sub.value == "products"
                   for sub in ast.walk(node))

    def email_keyed(keys) -> bool:
        return any(isinstance(k, str) and "@" in k for k in keys)

    found = []
    for rel in rels:
        path = root / rel
        if rel.endswith(".json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (isinstance(data, dict) and email_keyed(data)
                    and any(isinstance(v, dict) and "products" in v
                            for v in data.values())):
                found.append(rel)
            continue
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        if reads_roles(tree) and names_products(tree):
            found.append(rel)
        for node in tree.body:
            value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
            if (isinstance(value, ast.Dict)
                    and email_keyed(k.value for k in value.keys
                                    if isinstance(k, ast.Constant))
                    and any(isinstance(v, (ast.List, ast.Set, ast.Tuple))
                            for v in value.values)):
                found.append(f"{rel}:line {node.lineno}")
    return found


def test_the_mapping_detector_catches_each_shape(tmp_path):
    """The detector above passes vacuously if it can't see a mapping, so each
    shape is planted here, including the receiver-name variant review round 1
    named, the two-function split review round 2 named, and the agent-token
    reader it must leave alone."""
    (tmp_path / "renamed.py").write_text(
        "def scope(email):\n"
        "    record = _roles_lowercased().get(email, {})\n"
        "    return record.get('products', [])\n")
    (tmp_path / "split.py").write_text(
        "def entry(email):\n"
        "    return _roles_lowercased().get(email, {})\n"
        "def scope(email):\n"
        "    return entry(email).get('products', [])\n")
    (tmp_path / "table.py").write_text(
        "PEOPLE_PROJECTS = {'a@example.com': ['vivy']}\n")
    (tmp_path / "grants.json").write_text(
        json.dumps({"a@example.com": {"role": "admin", "products": ["*"]}}))
    (tmp_path / "tokens.py").write_text(
        "def api_tokens():\n"
        "    return {k: v.get('products', []) for k, v in load('api-tokens.json').items()}\n")
    assert sorted(_people_to_project_mappings(
        tmp_path, ["renamed.py", "split.py", "table.py", "grants.json",
                   "tokens.py"])) == [
        "grants.json", "renamed.py", "split.py", "table.py:line 1"]
