"""Authorization matrix tests (WS0.6, ARCH-348).

The security-critical surface: a bug here lets a contractor approve a decision or
an agent decide. So the whole allow/deny matrix is enumerated, deny-by-default is
proven, and the load-bearing invariant — an agent can NEVER decide — is tested
head-on, including the case where a compromised roles.json tries to hand an agent
an admin role.
"""
from __future__ import annotations

import json

import pytest

import authz
from authz import Identity, agent_identity, authorize

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
    roles_file.write_text(json.dumps({"operator@example.com": {"role": "admin", "products": ["*"]}}))
    ident = authz.human_identity("stranger@example.com")
    assert ident.role == "contractor" and ident.products == frozenset()
    assert authorize(ident, "decision.read", "vivy") is False  # no scope → nothing
    assert authorize(ident, "decision.decide", "vivy") is False


def test_human_identity_is_case_insensitive(roles_file):
    roles_file.write_text(json.dumps({"operator@example.com": {"role": "admin", "products": ["*"]}}))
    ident = authz.human_identity("Operator@Example.COM")
    assert ident.role == "admin" and authorize(ident, "decision.decide", "vivy") is True


def test_human_identity_unrecognized_role_falls_to_contractor(roles_file):
    roles_file.write_text(json.dumps({"x@x.example": {"role": "superuser", "products": ["*"]}}))
    ident = authz.human_identity("x@x.example")
    assert ident.role == "contractor"  # unknown role grants nothing beyond read


def test_malformed_roles_file_denies_all(roles_file):
    roles_file.write_text("{ not json")
    ident = authz.human_identity("operator@example.com")
    assert ident.role == "contractor" and ident.products == frozenset()


def test_missing_roles_file_denies_all(tmp_path, monkeypatch):
    monkeypatch.setattr(authz, "ROLES", tmp_path / "does-not-exist.json")
    ident = authz.human_identity("operator@example.com")
    assert ident.role == "contractor"
